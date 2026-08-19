"""技能注册表测试(阶段 14 S2):get/别名/优先级合并/subset/paths 过滤/
listing 三阶段预算 + bundled 永不截断 + 250 截断。"""

import pytest

from codesage.skills import SkillDefinition, SkillRegistry
from codesage.skills.registry import (
    MAX_LISTING_DESC_CHARS,
    MIN_DESC_LENGTH,
    SKILLS_LISTING_BUDGET,
)
from codesage.skills.types import skill_has_only_safe_properties


def _skill(name, *, description="desc", when_to_use="", **kw) -> SkillDefinition:
    return SkillDefinition(name=name, description=description, body="body",
                           when_to_use=when_to_use, **kw)


def _write_skill(root, dir_name, **fm):
    skill_dir = root / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {fm.pop('name', dir_name)}"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines += ["---", "body"]
    (skill_dir / "SKILL.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return skill_dir


# ---- get / 别名 / 优先级 ----

def test_get_by_name_and_alias():
    reg = SkillRegistry(builtin=[_skill("foo", aliases=("f", "fo"))])
    assert reg.get("foo").name == "foo"
    assert reg.get("f").name == "foo"
    with pytest.raises(KeyError):  # 别名关闭 → 触底 KeyError
        reg.get("fo", include_aliases=False)


def test_get_unknown_lists_available():
    reg = SkillRegistry(builtin=[_skill("foo")])
    with pytest.raises(KeyError) as e:
        reg.get("nope")
    assert "foo" in str(e.value)


def test_priority_merge_builtin_over_user_over_project(tmp_path):
    """同名技能:内置 > 用户 > 项目(CC loadAllCommands 对齐)——

    内置(核心)技能行为必须可预测,不能被项目/用户配置意外替换;文件系统
    序 managed → user → project,后者不覆盖前者。
    """
    user = tmp_path / "user" / "skills"
    proj = tmp_path / "proj" / "skills"
    _write_skill(user, "foo", description="user foo")
    _write_skill(user, "bar", description="user bar")
    _write_skill(proj, "foo", description="project foo")
    _write_skill(proj, "baz", description="project baz")
    reg = SkillRegistry(
        builtin=[_skill("foo", description="builtin foo")],
        user_dir=user,
        project_dir=proj,
    )
    assert reg.get("foo").description == "builtin foo"  # 内置最高,项目不可覆盖
    assert reg.get("bar").description == "user bar"
    assert reg.get("baz").description == "project baz"
    assert reg.names() == ["bar", "baz", "foo"]


def test_priority_merge_user_over_project(tmp_path):
    """无内置同名时:用户覆盖项目(文件系统 managed → user → project)。"""
    user = tmp_path / "user" / "skills"
    proj = tmp_path / "proj" / "skills"
    _write_skill(user, "foo", description="user foo")
    _write_skill(proj, "foo", description="project foo")
    reg = SkillRegistry(user_dir=user, project_dir=proj)
    assert reg.get("foo").description == "user foo"


def test_priority_merge_managed_between_builtin_and_user(tmp_path):
    """managed(组织管理)层:高于用户/项目,低于内置(CC managedCommandsDir)。"""
    managed = tmp_path / "managed" / "skills"
    user = tmp_path / "user" / "skills"
    proj = tmp_path / "proj" / "skills"
    _write_skill(managed, "foo", description="managed foo")
    _write_skill(user, "foo", description="user foo")
    _write_skill(proj, "foo", description="project foo")
    reg = SkillRegistry(managed_dir=managed, user_dir=user, project_dir=proj)
    assert reg.get("foo").description == "managed foo"  # managed > user/project
    assert reg.get("foo").source == "managed"
    # 内置同名 → 内置恒胜
    reg2 = SkillRegistry(
        builtin=[_skill("foo", description="builtin foo")],
        managed_dir=managed, user_dir=user, project_dir=proj,
    )
    assert reg2.get("foo").description == "builtin foo"


# ---- subset ----

def test_subset_narrows_and_keeps_bundled():
    bundled = _skill("simplify", source="builtin")
    reg = SkillRegistry(builtin=[bundled, _skill("a"), _skill("b", aliases=("bb",))])
    sub = reg.subset(["a", "bb", "missing"])
    assert sub.names() == ["a", "b", "simplify"]  # 未知名跳过 + bundled 恒保留
    assert sub.get("bb").name == "b"  # 子集内别名仍可用


# ---- paths 静态过滤 ----

def test_paths_filtering_by_cwd(tmp_path):
    (tmp_path / ".git").mkdir()
    root = tmp_path
    src = root / "src"
    src.mkdir()
    (src / "sub").mkdir()
    docs = root / "docs"
    docs.mkdir()
    s = _skill("fe", paths=("src/**",))
    reg = SkillRegistry(builtin=[s, _skill("always"), _skill("md", paths=("*.py",))])
    # cwd 在 src 下 → fe 可见
    listed = reg.listing_text(budget=SKILLS_LISTING_BUDGET, cwd=src / "sub")
    assert "fe:" in listed
    assert "always:" in listed
    # cwd 在 docs 下 → fe 不可见,md 按 basename 匹配 .py 也不可见
    listed = reg.listing_text(budget=SKILLS_LISTING_BUDGET, cwd=docs)
    assert "fe:" not in listed
    assert "always:" in listed
    assert "md:" not in listed
    # 无 cwd → 全放行
    assert "fe:" in reg.listing_text()


def test_empty_paths_always_visible(tmp_path):
    (tmp_path / ".git").mkdir()
    reg = SkillRegistry(builtin=[_skill("x", paths=())])
    assert "x:" in reg.listing_text(cwd=tmp_path)


# ---- listing 预算三分支 ----

def test_listing_full_when_fits():
    reg = SkillRegistry(builtin=[
        _skill("a", description="short", when_to_use="when a"),
        _skill("b", description="also short"),
    ])
    text = reg.listing_text(budget=10_000)
    assert text == "- a: short - when a\n- b: also short"


def test_listing_truncated_by_share():
    reg = SkillRegistry(builtin=[
        _skill("a", description="A" * 100),
        _skill("b", description="B" * 100),
    ])
    text = reg.listing_text(budget=200)
    # max_desc_len = 200//2 - 25 = 75 ≥ 20 → 截断模式
    lines = text.splitlines()
    assert len(lines) == 2
    assert all(len(line) <= 85 for line in lines)  # 5 + 75 + 1(省略号) 内
    assert lines[0] == "- a: " + "A" * 75 + "…"


def test_listing_names_only_extreme():
    reg = SkillRegistry(builtin=[
        _skill("a", description="A" * 100),
        _skill("b", description="B" * 100),
    ])
    text = reg.listing_text(budget=40)
    # max_desc_len = 40//2 - 25 = -5 < 20 → names-only
    assert text == "- a\n- b"


def test_bundled_never_truncated():
    bundled = _skill("core", description="C" * 300, source="builtin")
    reg = SkillRegistry(builtin=[bundled, _skill("extra", description="E" * 100)])
    text = reg.listing_text(budget=60)
    lines = text.splitlines()
    assert lines[0] == "- core: " + "C" * 300  # bundled 完整,永不截断
    assert lines[1] == "- extra"  # 极端模式:非 bundled names-only


def test_listing_desc_cap_250():
    reg = SkillRegistry(builtin=[_skill("s", description="D" * 800)])
    text = reg.listing_text(budget=600)
    # full = 805 > 600 → 截断;max_desc_len = 600-25 = 575 → 上限 250
    assert text == "- s: " + "D" * MAX_LISTING_DESC_CHARS + "…"
    assert len(text) == 5 + MAX_LISTING_DESC_CHARS + 1


def test_listing_empty_registry():
    assert SkillRegistry().listing_text() == ""


# ---- safe() SAFE 白名单判定(§7.3 纯函数部分)----

def test_safe_pure_fields():
    s = _skill("x", description="d", when_to_use="w", arguments=("f",),
               aliases=("x1",), argument_hint="hint", source="project")
    assert skill_has_only_safe_properties(s)


def test_safe_unsafe_fields_are_not_safe():
    assert not skill_has_only_safe_properties(_skill("x", allowed_tools={"Read"}))
    assert not skill_has_only_safe_properties(_skill("x", context="fork"))
    assert not skill_has_only_safe_properties(_skill("x", model="sonnet"))
    assert not skill_has_only_safe_properties(_skill("x", effort="high"))
    assert not skill_has_only_safe_properties(_skill("x", agent="Explore"))
    assert not skill_has_only_safe_properties(_skill("x", shell="powershell"))
    assert not skill_has_only_safe_properties(_skill("x", paths=("src/**",)))
    assert not skill_has_only_safe_properties(_skill("x", hooks={"PreToolUse": "e"}))


def test_safe_empty_values_are_safe():
    """不安全字段取默认空值 → 豁免(§7.3 空值豁免)。"""
    s = _skill("x", context="inline", allowed_tools=frozenset(),
               model=None, paths=(), hooks=None, agent=None, shell=None)
    assert skill_has_only_safe_properties(s)


def test_safe_unknown_field_defaults_unsafe():
    """新增字段默认不在白名单 → 有值即不安全(白名单前向兼容)。"""
    # 模拟未来新增字段:构造一个带自定义字段的等价技能 —— 用 context 非默认
    # 已在上方覆盖;这里断言白名单本身不含未声明字段
    from codesage.skills import SAFE_SKILL_PROPERTIES
    assert "future_field" not in SAFE_SKILL_PROPERTIES
