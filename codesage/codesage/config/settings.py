"""Settings system: three-tier merge (user < project < local).

Mirrors Kode's settings design (#18): hooks and permission config live in
settings files, distinct from the global config. Merge semantics: dicts
merge recursively, lists append with dedup (user first, later tiers later).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from . import paths
from .atomic import atomic_write

#: Per-tier defaults; tiers load in this order, later overrides earlier.
TIER_ORDER = ("user", "project", "local")


class Settings(BaseModel):
    """Typed view of merged settings. Unknown keys are preserved (extra=allow)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # Reserved for later phases; declared now so consumers have stable names.
    permissions: dict[str, Any] = Field(default_factory=dict)  # phase 05
    hooks: dict[str, Any] = Field(default_factory=dict)  # phase 09
    # phase 15:MCP 服务器。JSON 键用驼峰 mcpServers(与 .mcp.json/CC 一致),属性名下划线。
    mcp_servers: dict[str, Any] = Field(default_factory=dict, alias="mcpServers")


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (dicts recurse, lists concat-dedup)."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        elif isinstance(value, list):
            out[key] = _dedup_concat(out.get(key, []), value)
        else:
            out[key] = value
    return out


def _dedup_concat(prev: list, new: list) -> list:
    seen: set[Any] = set()
    result = []
    for item in (*prev, *new):
        # Lists of dicts (e.g. hooks) fall back to identity-based dedup.
        marker = json.dumps(item, sort_keys=True) if isinstance(item, dict) else item
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


def load_settings_file(path: Path) -> dict:
    """Read one settings file; missing/corrupt files are silently empty."""
    try:
        # utf-8-sig: strips the BOM that Notepad/PowerShell write by default.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class SettingsStore:
    """Loads and caches merged settings for a project directory."""

    def __init__(self, project_dir: Path | None = None):
        self._project_dir = project_dir
        self._cache: tuple[Path, int, Settings] | None = None

    def load(self) -> Settings:
        """Merged settings, cached by the latest mtime across the three files."""
        files = [
            paths.user_settings_path(),
            paths.project_settings_path(self._project_dir),
            paths.local_settings_path(self._project_dir),
        ]
        mtimes = [self._mtime(f) for f in files]
        if self._cache is not None and mtimes == self._cache[1]:
            return self._cache[2]
        merged: dict = {}
        for f in files:
            merged = _deep_merge(merged, load_settings_file(f))
        settings = Settings(**merged)
        self._cache = (files[-1], mtimes, settings)
        return settings

    def clear_cache(self) -> None:
        self._cache = None

    @staticmethod
    def _mtime(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return -1


def load_settings(project_dir: Path | None = None) -> Settings:
    """Convenience: load merged settings (cached per store instance)."""
    return SettingsStore(project_dir).load()

def save_settings(mutator=None, project_dir: Path | None = None) -> None:
    """Persist the local settings file atomically (spec §5.5: MCP 启用/禁用落点).

    *mutator* 接收合并后的原始 dict 并返回新 dict;None 表示原样写回(通常配合
    load_settings 改字段后保存)。写后清空 store 缓存让下次读取看到新值。
    """
    store = SettingsStore(project_dir)
    files = [
        paths.user_settings_path(),
        paths.project_settings_path(project_dir),
        paths.local_settings_path(project_dir),
    ]
    merged: dict = {}
    for f in files:
        merged = _deep_merge(merged, load_settings_file(f))
    if mutator:
        merged = mutator(merged)
    atomic_write(paths.local_settings_path(project_dir), json.dumps(merged, indent=2) + "\n")
    store.clear_cache()
