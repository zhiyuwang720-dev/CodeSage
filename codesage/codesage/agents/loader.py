"""Agent definition loading (phase 13 S1): minimal frontmatter parser,
per-directory loading with mtime-keyed caching.

Minimal YAML subset on purpose (zero dependencies, spec §3.2): scalars,
flow lists (comma or space separated), one-level maps. Unknown keys are
ignored; malformed files or files without a name are silently skipped (CC
parity, spec §3.3).
"""

from __future__ import annotations

import functools
import hashlib
import warnings
from pathlib import Path
from typing import Any

from .types import AgentDefinition

#: frontmatter keys parsed as flow lists (comma/space separated, brackets ok).
_LIST_FIELDS = frozenset({"tools", "disallowed_tools"})
#: frontmatter keys parsed as maps (flow ``{...}`` or indented one-level).
_MAP_FIELDS = frozenset({"hooks"})


def _parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    low = raw.lower()
    # YAML 1.1 boolean spellings (js-yaml default schema — CC parity):
    # yes/no/on/off must not survive as strings (bool("no") is True).
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


def _parse_flow_list(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [p.strip("'\"") for p in raw.replace(",", " ").split() if p]


def _parse_flow_map(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("{") and raw.endswith("}"):
        raw = raw[1:-1]
    result: dict[str, Any] = {}
    for part in raw.split(","):
        k, _, v = part.partition(":")
        k = k.strip()
        if k:
            result[k] = _parse_scalar(v)
    return result


def _parse_value(raw: str, key: str) -> Any:
    if key in _LIST_FIELDS:
        return _parse_flow_list(raw)
    if key in _MAP_FIELDS and raw.startswith("{"):
        return _parse_flow_map(raw)
    return _parse_scalar(raw)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], int] | None:
    """Parse a ``---``-fenced frontmatter block.

    Returns (parsed, index of the line after the closing fence), or None
    when no fence pair is present — an unterminated opening fence counts as
    malformed (gray-matter/CC treat it as no frontmatter → no agent, spec
    §3.3). Single pass: the body always starts after the closing fence, so
    ``---`` inside the body stays body text.
    """
    lines = text.splitlines()
    # BOM: Windows editors commonly emit it; str.strip() keeps ﻿ (Cf).
    if not lines or lines[0].lstrip("﻿").strip() != "---":
        return None
    end: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None
    result: dict[str, Any] = {}
    i = 1
    while i < end:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        key, sep, rest = line.partition(":")
        key = key.strip()
        if not sep or not key:
            i += 1
            continue
        rest = rest.strip()
        if rest or key not in _MAP_FIELDS:
            result[key] = _parse_value(rest, key) if rest else None
            i += 1
            continue
        # One-level map (e.g. hooks): indented ``subkey: value`` lines.
        sub: dict[str, Any] = {}
        j = i + 1
        while j < end:
            subline = lines[j]
            if subline[:1] in (" ", "\t"):
                j += 1
                if not subline.strip() or subline.lstrip().startswith("#"):
                    continue  # blank/comment lines inside the map are skipped
                sk, ssep, sv = subline.strip().partition(":")
                if ssep and sk:
                    sub[sk] = _parse_scalar(sv)
                continue
            if not subline.strip():
                # blank line: belongs to the map when the next line is indented
                nxt = j + 1
                while nxt < end and not lines[nxt].strip():
                    nxt += 1
                if nxt < end and lines[nxt][:1] in (" ", "\t"):
                    j += 1
                    continue
            break
        result[key] = sub
        i = j
    return result, end + 1


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
        parsed = _parse_frontmatter(text)
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
