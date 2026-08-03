"""Configuration paths: data root and settings file locations.

Mirrors Kode's config layout: a single global config plus a three-tier
settings file system (user / project / local). Paths can be overridden
via CODESAGE_CONFIG_DIR (data root) and CODESAGE_CWD (working dir).
"""

from __future__ import annotations

import os
from pathlib import Path

#: Directory holding global config and user settings (~/.codesage by default).
DEFAULT_CONFIG_DIR = Path.home() / ".codesage"

SETTINGS_BASENAMES = {
    "user": "settings.json",
    "project": ".codesage/settings.json",
    "local": ".codesage/settings.local.json",
}

GLOBAL_CONFIG_FILENAME = "config.json"


def config_dir() -> Path:
    """Data root for CodeSage state (config, sessions, memory)."""
    override = os.getenv("CODESAGE_CONFIG_DIR")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_DIR


def cwd() -> Path:
    """Working directory; overridable for tests and --cwd."""
    override = os.getenv("CODESAGE_CWD")
    return Path(override).resolve() if override else Path.cwd().resolve()


def user_settings_path() -> Path:
    return config_dir() / SETTINGS_BASENAMES["user"]


def project_settings_path(project_dir: Path | None = None) -> Path:
    return (project_dir or cwd()) / SETTINGS_BASENAMES["project"]


def local_settings_path(project_dir: Path | None = None) -> Path:
    return (project_dir or cwd()) / SETTINGS_BASENAMES["local"]


def global_config_path() -> Path:
    return config_dir() / GLOBAL_CONFIG_FILENAME
