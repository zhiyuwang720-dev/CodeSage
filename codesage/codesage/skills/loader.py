"""技能加载(阶段 14 S2):目录扫描 + realpath 去重 + lru 缓存 + 懒执行。

镜像 13 agents loader 的既有套路:
- 目录形式:`{dir}/skills/{name}/SKILL.md` —— 只接受 ``{dir}/*/SKILL.md``,
  散落的单文件 ``.md`` 忽略(spec 14 §2 裁决 1);
- 缓存:`functools.lru_cache(maxsize=64)`,键 = (目录, 每文件 name/mtime_ns/
  size/digest 快照),digest = sha256 防同尺寸编辑(13 §3.3 同款);
- 懒执行:frontmatter 全部解析进注册表,但 *body* 只在调用时使用
  (发现与执行分离,spec §2 裁决 2)。
"""

from __future__ import annotations

import functools
import hashlib
import warnings
from pathlib import Path
from typing import Any

from ..core.frontmatter import parse_frontmatter
from .types import FRONTMATTER_KEYS, SkillDefinition

#: skills 的 flow-list frontmatter 字段(连字符键 = CC 生态名)
_LIST_FIELDS = frozenset({"arguments", "allowed-tools", "paths", "aliases"})
#: map 字段:复用 core/frontmatter 默认的 {"hooks"}
_MAP_FIELDS = frozenset({"hooks"})


def _digest(path: Path) -> str:
    """内容摘要 —— 捕捉粗粒度 mtime 文件系统(FAT 2s / Windows ~10ms)的
    同尺寸编辑(13 loader 同款语义)。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_definition(fm: dict[str, Any], body: str, source: str, skill_dir: Path) -> SkillDefinition | None:
    """从解析后的 frontmatter 构造技能定义(白名单语义,spec §3.2)。

    frontmatter 键经 ``FRONTMATTER_KEYS`` 归一化为 snake_case 字段名;
    未知键不读取(白名单过滤在此完成)。缺 ``name`` → 返回 None,调用方静默跳过。
    """
    normalized = {FRONTMATTER_KEYS[k]: v for k, v in fm.items() if k in FRONTMATTER_KEYS}
    name = normalized.get("name")
    if not isinstance(name, str) or not name:
        return None
    context = normalized.get("context", "inline")
    if context not in ("inline", "fork"):
        # 非法值静默回退 inline + warning(镜像 13 §3.2 容错口径)
        warnings.warn(
            f"skill {name!r}: invalid context {context!r}, falling back to inline",
            stacklevel=3,
        )
        context = "inline"
    hooks = normalized.get("hooks")
    return SkillDefinition(
        name=name,
        description=str(normalized.get("description") or ""),
        body=body,
        when_to_use=str(normalized.get("when_to_use") or ""),
        argument_hint=(
            normalized["argument_hint"]
            if isinstance(normalized.get("argument_hint"), str) and normalized["argument_hint"]
            else None
        ),
        arguments=tuple(normalized.get("arguments") or ()),
        context=context,
        allowed_tools=frozenset(normalized.get("allowed_tools") or ()),
        model=normalized.get("model"),
        effort=normalized.get("effort"),
        agent=normalized.get("agent"),
        shell=normalized.get("shell"),
        paths=tuple(normalized.get("paths") or ()),
        user_invocable=bool(normalized.get("user_invocable", True)),
        disable_model_invocation=bool(normalized.get("disable_model_invocation", False)),
        hooks=dict(hooks) if isinstance(hooks, dict) else hooks,
        aliases=tuple(normalized.get("aliases") or ()),
        source=source,
        skill_dir=skill_dir,
    )


@functools.lru_cache(maxsize=64)
def _scan_cached(
    dir_key: str, snapshot: tuple[tuple[str, str, int, int, str], ...]
) -> dict[str, tuple[dict[str, Any], str]]:
    """解析缓存:技能名 → (frontmatter, body)。

    缓存键 = 目录 + 每文件 (目录名, realpath, mtime_ns, size, digest),编辑与
    重命名无需 watcher 即失效(13 §3.3 同款);realpath 参与键值保证去重后
    读取的目标稳定。
    """
    result: dict[str, tuple[dict[str, Any], str]] = {}
    for fname, real, _mtime, _size, _digest in snapshot:
        path = Path(real)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parsed = parse_frontmatter(text, list_fields=_LIST_FIELDS, map_fields=_MAP_FIELDS)
        if parsed is None:
            continue
        fm, body_start = parsed
        body = "\n".join(text.splitlines()[body_start:]).strip()
        result[fname] = (fm, body)
    return result


def load_dir(dir_path: Path, source: str = "project") -> dict[str, SkillDefinition]:
    """扫描一个技能目录层的全部技能;目录不存在 → 空 dict。

    - 发现:``{dir}/*/SKILL.md``(目录形式,散落 .md 忽略);
    - **realpath 去重**(CC loadSkillsDir 同款):加载时先 resolve() 全部路径,
      相同规范路径只保留首次出现(排序序遍历);NFS/容器/ExFAT inode 不可靠,
      故用 realpath 而非 dev+ino;
    - 技能名取 frontmatter ``name``,缺 → 静默跳过。
    """
    dir_path = dir_path.resolve()
    if not dir_path.is_dir():
        return {}
    # 去重 + 建快照:先解析所有候选的 realpath,保留每个规范路径的首次出现
    seen: set[str] = set()
    snapshot: list[tuple[str, str, int, int, str]] = []
    for p in sorted(dir_path.glob("*/SKILL.md")):
        try:
            real = p.resolve()
        except OSError:
            continue
        key = str(real)
        if key in seen:
            continue
        seen.add(key)
        try:
            st = real.stat()
        except OSError:
            continue  # glob 与 stat 之间的并发删除(原子编辑器替换)
        snapshot.append((p.parent.name, key, st.st_mtime_ns, st.st_size, _digest(real)))
    parsed = _scan_cached(str(dir_path), tuple(snapshot))
    defs: dict[str, SkillDefinition] = {}
    for dir_name, (fm, body) in parsed.items():
        definition = build_definition(fm, body, source, Path(dir_path) / dir_name)
        if definition is not None:
            defs[definition.name] = definition
    return defs
