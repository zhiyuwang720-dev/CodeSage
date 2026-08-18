"""技能加载测试(阶段 14 S2):目录形式发现 / 缺 name 跳过 / frontmatter
映射 / 非法 context 回退 / realpath 去重 / lru 快照命中。"""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from codesage.skills import load_dir
from codesage.skills.loader import _scan_cached


def _write_skill(root, dir_name, *, name=None, body="body", **fm):
    """写一个 {root}/{dir_name}/SKILL.md(frontmatter 字段来自 fm)。"""
    skill_dir = root / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    lines.append(f"name: {name or dir_name}")
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append(body)
    (skill_dir / "SKILL.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return skill_dir


def _link_dir(src: Path, dst: Path):
    """创建目录链接(junction/symlink);失败则跳过(Windows 权限限制)。"""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                check=True,
                capture_output=True,
            )
            return
        except subprocess.CalledProcessError:
            pytest.skip("Windows 无 junction 权限,跳过 realpath 去重测试")
    try:
        dst.symlink_to(src, target_is_directory=True)
    except OSError:
        pytest.skip("无 symlink 权限,跳过 realpath 去重测试")


def test_directory_form_only(tmp_path):
    """目录形式 ``{dir}/*/SKILL.md`` 才被发现;散落的 .md 忽略(§4.2)。"""
    root = tmp_path / "skills"
    _write_skill(root, "foo")
    _write_skill(root, "bar")
    (root / "stray.md").write_text("---\nname: stray\ndescription: d\n---\nbody\n")
    defs = load_dir(root)
    assert set(defs) == {"foo", "bar"}


def test_missing_name_skipped(tmp_path):
    """frontmatter 缺 name → 文件静默跳过(§3.1)。"""
    root = tmp_path / "skills"
    (root / "anon").mkdir(parents=True)
    (root / "anon" / "SKILL.md").write_text("---\ndescription: d\n---\nbody\n", encoding="utf-8")
    assert load_dir(root) == {}


def test_empty_dir_and_missing_dir(tmp_path):
    """目录不存在 / 空目录 → 空 dict。"""
    assert load_dir(tmp_path / "nope") == {}
    assert load_dir(tmp_path / "skills") == {}


def test_frontmatter_fields_mapped(tmp_path):
    """连字符键 → snake_case 字段,白名单外字段忽略。"""
    root = tmp_path / "skills"
    _write_skill(
        root,
        "review",
        name="review",
        description="审查代码",
        when_to_use="用户要求审查时",
        **{
            "argument-hint": "[关注点]",
            "arguments": "[focus, style]",
            "allowed-tools": "[Read, Grep]",
            "model": "sonnet",
            "paths": "[src/**]",
            "user-invocable": "false",
            "disable-model-invocation": "true",
            "aliases": "[r]",
            "unknown": "ignored",
        },
    )
    (s,) = load_dir(root).values()
    assert s.name == "review"
    assert s.description == "审查代码"
    assert s.when_to_use == "用户要求审查时"
    assert s.argument_hint == "[关注点]"
    assert s.arguments == ("focus", "style")
    assert s.allowed_tools == frozenset({"Read", "Grep"})
    assert s.model == "sonnet"
    assert s.paths == ("src/**",)
    assert s.user_invocable is False
    assert s.disable_model_invocation is True
    assert s.aliases == ("r",)
    assert not hasattr(s, "unknown")


def test_context_invalid_falls_back_inline(tmp_path):
    """context 非法值 → inline + warning(§3.2)。"""
    root = tmp_path / "skills"
    _write_skill(root, "x", context="sidecar")
    with pytest.warns(UserWarning, match="invalid context"):
        (s,) = load_dir(root).values()
    assert s.context == "inline"


def test_context_fork_kept(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "x", context="fork")
    (s,) = load_dir(root).values()
    assert s.context == "fork"


def test_lazy_body_loaded_but_frontmatter_light(tmp_path):
    """body 与 frontmatter 一并进入定义(懒执行是「模型可见层」语义:listing
    只取 frontmatter,body 仅调用时使用 —— 见 test_registry listing 分支)。"""
    root = tmp_path / "skills"
    _write_skill(root, "x", body="first line\nsecond line")
    (s,) = load_dir(root).values()
    assert s.body == "first line\nsecond line"


def test_realpath_dedup(tmp_path):
    """realpath 去重:同文件双入口只保留首次出现(排序序:alias 先于 real)。

    目录名 alias 与 real 指向同一 SKILL.md,加载只产生**一个**技能(名称取
    frontmatter 的 name=real),skill_dir 取首次出现的目录(alias)。
    """
    root = tmp_path / "skills"
    real = _write_skill(root, "real", name="real")
    _link_dir(real, root / "alias")  # alias → real,同一 realpath
    defs = load_dir(root)
    assert set(defs) == {"real"}  # 去重:只注册一次,不按目录名重复
    (s,) = defs.values()
    assert s.skill_dir == root / "alias"  # 首次出现(排序序)被保留


def test_lru_snapshot_hit(monkeypatch, tmp_path):
    """lru 快照命中:文件未变 → 不重新读盘;编辑后命中失效。"""
    root = tmp_path / "skills"
    _write_skill(root, "x", description="v1")
    assert load_dir(root)["x"].description == "v1"
    cached = _scan_cached.cache_info().currsize
    # 未改动再加载:缓存命中(currsize 不增长)
    load_dir(root)
    assert _scan_cached.cache_info().currsize == cached
    # 改动(digest 变化)→ 新快照
    time.sleep(0.01)
    _write_skill(root, "x", description="v2")
    assert load_dir(root)["x"].description == "v2"
