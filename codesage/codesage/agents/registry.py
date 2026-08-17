"""Agent registry (phase 13 S1): builtin trio + layered discovery.

Builtin definitions live here, not on disk (CC parity, spec §3.1):
general-purpose, Explore and Plan mirror Claude Code's built-in layer.
"""

from __future__ import annotations

from pathlib import Path

from ..config import paths
from ..config.agents_md import find_git_root
from .loader import load_dir
from .types import AgentDefinition

BUILTIN_AGENTS: dict[str, AgentDefinition] = {
    "general-purpose": AgentDefinition(
        name="general-purpose",
        description="通用任务:全量工具,完成任务并简洁汇报",
        body=(
            "You are an agent for CodeSage. Complete the task fully — don't "
            "gold-plate, but don't leave it half-done. Report results concisely."
        ),
        tools=None,
        source="builtin",
    ),
    "Explore": AgentDefinition(
        name="Explore",
        description="只读代码库搜索:并行搜索与文件读取,不修改任何文件",
        body=(
            "=== CRITICAL: READ-ONLY MODE ===\n"
            "You may NOT create, modify, or delete any files. Do NOT use "
            "shell redirection to write files, and do NOT run commands that "
            "change system state.\n"
            "You are a fast read-only search agent. Call multiple tools in "
            "parallel when searching and reading files."
        ),
        disallowed_tools=frozenset({"Agent", "Write", "Edit"}),
        source="builtin",
    ),
    "Plan": AgentDefinition(
        name="Plan",
        description="设计实施方案:只读分析,输出可执行的计划",
        body=(
            "You are a read-only planning agent. You may NOT modify files.\n"
            "Design an implementation plan for the given task. End your "
            "output with a 'Critical Files for Implementation' section "
            "listing the 3-5 files most relevant to implementation."
        ),
        disallowed_tools=frozenset({"Agent", "Write", "Edit"}),
        source="builtin",
    ),
}


class AgentRegistry:
    """Layered agent lookup: project > user > builtin (spec §3.3)."""

    def __init__(
        self,
        user_dir: Path | None = None,
        project_dir: Path | None = None,
        extra_dirs: tuple[Path, ...] = (),
    ) -> None:
        self._defs: dict[str, AgentDefinition] = dict(BUILTIN_AGENTS)
        if user_dir is not None:
            self._defs.update(load_dir(user_dir, source="user"))
        for extra in extra_dirs:
            self._defs.update(load_dir(extra, source="user"))
        if project_dir is not None:
            self._defs.update(load_dir(project_dir, source="project"))

    @classmethod
    def from_default_paths(cls, cwd: Path | None = None) -> "AgentRegistry":
        """User ({config_dir}/agents) + project ({git root}/.codesage/agents) + builtin.

        目录随 CodeSage 数据根(默认 ~/.codesage,可 CODESAGE_CONFIG_DIR 覆盖)
        与项目级配置前例(.codesage/settings.json),不复用 Claude Code 的
        ~/.claude 布局。No git root → fall back to cwd as the project root
        (same precedent as config/agents_md.py project instruction files).
        """
        start = (cwd or Path.cwd()).resolve()
        git_root = find_git_root(start)
        return cls(
            user_dir=paths.config_dir() / "agents",
            project_dir=(git_root or start) / ".codesage" / "agents",
        )

    def get(self, name: str) -> AgentDefinition:
        """Resolve one agent; KeyError lists available names (CC parity)."""
        try:
            return self._defs[name]
        except KeyError:
            available = ", ".join(sorted(self._defs)) or "(none)"
            raise KeyError(
                f"unknown agent {name!r}; available: {available}"
            ) from None

    def names(self) -> list[str]:
        """Sorted names, used for the Agent tool description (spec §4)."""
        return sorted(self._defs)

    def all(self) -> dict[str, AgentDefinition]:
        return dict(self._defs)
