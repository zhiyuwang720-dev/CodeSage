"""Global config: a single per-machine config file (~/.codesage/config.json).

Mirrors Kode's ~/.kode.json: product-level settings (model profiles, MCP
servers, per-project entries keyed by absolute path). Distinct from the
settings tiers, which hold hooks/permission rules.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from . import paths
from .atomic import atomic_write, read_json_lossy


class ProjectConfig(BaseModel):
    """Per-project entry, keyed by absolute project path."""

    model_config = ConfigDict(extra="allow")

    allowed_tools: list[str] = Field(default_factory=list)  # phase 05
    context: dict[str, Any] = Field(default_factory=dict)  # phase 08


class GlobalConfig(BaseModel):
    """Top-level global configuration."""

    model_config = ConfigDict(extra="allow")

    theme: Optional[str] = None  # phase 07
    model_profiles: dict[str, Any] = Field(default_factory=dict)  # phase 02
    model_pointers: dict[str, str] = Field(
        default_factory=lambda: {"main": "main", "task": "task", "compact": "compact", "quick": "quick"}
    )  # phase 02
    mcp_servers: dict[str, Any] = Field(default_factory=dict)  # phase 15
    projects: dict[str, ProjectConfig] = Field(default_factory=dict)

    def project(self, project_path: str) -> ProjectConfig:
        """Get-or-create the project entry for an absolute path."""
        return self.projects.setdefault(project_path, ProjectConfig())

    @classmethod
    def load(cls) -> "GlobalConfig":
        """Load from disk; corrupt/missing files degrade to defaults."""
        data = read_json_lossy(paths.global_config_path(), {})
        try:
            return cls(**data)
        except Exception:  # pydantic ValidationError; degrade, never crash startup
            return cls()

    def save(self) -> None:
        atomic_write(paths.global_config_path(), self.model_dump_json(indent=2) + "\n")
