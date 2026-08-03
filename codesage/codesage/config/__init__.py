"""Configuration system (phase 01): settings tiers, global config, AGENTS.md discovery."""

from . import paths
from .agents_md import find_git_root, get_project_instruction_files
from .atomic import atomic_write, read_json_lossy
from .global_config import GlobalConfig, ProjectConfig
from .settings import Settings, SettingsStore, load_settings

__all__ = [
    "GlobalConfig",
    "ProjectConfig",
    "Settings",
    "SettingsStore",
    "atomic_write",
    "find_git_root",
    "get_project_instruction_files",
    "load_settings",
    "paths",
    "read_json_lossy",
]
