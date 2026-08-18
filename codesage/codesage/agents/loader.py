"""Agent definition loading (phase 13 S1): per-directory loading with
mtime-keyed caching.

Frontmatter parsing lives in the shared ``core/frontmatter.py`` (extracted
in phase 14 S1 — both agents and skills reuse it, spec 14 §4.1). Unknown
keys are ignored; malformed files or files without a name are silently
skipped (CC parity, spec §3.3).
"""

from __future__ import annotations

import functools
import hashlib
import warnings
from pathlib import Path
from typing import Any

from ..core.frontmatter import parse_frontmatter
from .types import AgentDefinition

#: frontmatter keys parsed as flow lists (comma/space separated, brackets ok).
_LIST_FIELDS = frozenset({"tools", "disallowed_tools"})
#: frontmatter keys parsed as maps (flow ``{...}`` or indented one-level).
_MAP_FIELDS = frozenset({"hooks"})


def _digest(path: Path) -> str:
    """Content digest — catches same-size edits that coarse-mtime filesystems
    (FAT 2s, Windows ~10ms tick) miss. Agent md files are tiny; reading the
    whole file is cheaper than portable head+tail seeking (negative seek on
    short files is platform-dependent)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_definition(
    name: str, fm: dict[str, Any], body: str, source: str
) -> AgentDefinition:
    """Construct from parsed frontmatter with whitelist semantics (spec §3.2)."""
    model = fm.get("model")
    fork_context = bool(fm.get("fork_context", False))
    if fork_context and model not in (None, "inherit"):
        warnings.warn(
            f"agent {name!r}: fork_context=true forces model='inherit' "
            f"(was {model!r})",
            stacklevel=3,
        )
        model = None
    max_turns = fm.get("max_turns", 50)
    # bool is an int subclass; only positive ints are meaningful.
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns <= 0:
        max_turns = None  # invalid / null → inherit parent value
    hooks = fm.get("hooks")
    return AgentDefinition(
        name=name,
        description=str(fm.get("description") or ""),
        body=body,
        # explicit ``tools: []`` reads as None (= full parent pool); the
        # format cannot express "no tools at all" — acceptable for now
        tools=frozenset(fm["tools"]) if fm.get("tools") else None,
        disallowed_tools=frozenset(fm.get("disallowed_tools") or ()),
        model=model,
        max_turns=max_turns,
        permission_mode=fm.get("permission_mode"),
        fork_context=fork_context,
        # S7:白名单仅接受字面量 "worktree",其余值 → None(未知值不产生半有效配置)
        isolation=fm.get("isolation") if fm.get("isolation") in ("worktree",) else None,
        hooks=dict(hooks) if isinstance(hooks, dict) else hooks,  # copy: frozen dataclass sharing a cached dict
        background=bool(fm.get("background", False)),
        color=fm.get("color"),
        source=source,
    )


@functools.lru_cache(maxsize=64)
def _scan_cached(
    dir_key: str, snapshot: tuple[tuple[str, int, int, str], ...]
) -> dict[str, tuple[dict[str, Any], str]]:
    """Parse-only cache: agent name → (frontmatter, body).

    Cache key = directory + (name, mtime_ns, size, digest) per file, so
    edits and renames invalidate without a watcher (spec §3.3); the digest
    covers same-size edits inside an mtime tick. ``source`` is applied by
    :func:`load_dir` outside the cache.
    """
    result: dict[str, tuple[dict[str, Any], str]] = {}
    for fname, _mtime, _size, _digest in snapshot:
        path = Path(dir_key) / fname
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parsed = parse_frontmatter(text, list_fields=_LIST_FIELDS, map_fields=_MAP_FIELDS)
        if parsed is None:
            continue
        fm, body_start = parsed
        agent_name = fm.get("name")
        if not isinstance(agent_name, str) or not agent_name:
            continue
        body = "\n".join(text.splitlines()[body_start:]).strip()
        result[agent_name] = (fm, body)
    return result


def load_dir(dir_path: Path, source: str = "project") -> dict[str, AgentDefinition]:
    """Load all ``*.md`` under one agents dir; non-existent dir → empty dict."""
    dir_path = dir_path.resolve()
    if not dir_path.is_dir():
        return {}
    snapshot: list[tuple[str, int, int, str]] = []
    for p in sorted(dir_path.glob("*.md")):
        try:
            st = p.stat()
        except OSError:
            continue  # deleted between glob and stat (atomic editor replace)
        snapshot.append((p.name, st.st_mtime_ns, st.st_size, _digest(p)))
    parsed = _scan_cached(str(dir_path), tuple(snapshot))
    return {
        name: build_definition(name, fm, body, source)
        for name, (fm, body) in parsed.items()
    }
