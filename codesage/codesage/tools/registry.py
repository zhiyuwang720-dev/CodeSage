"""Tool registry: name-keyed lookup, builtin assembly, model-facing specs."""

from __future__ import annotations

from typing import Iterable

from ..ai import ToolSpec
from .base import Tool
from .builtin import BUILTIN_TOOLS


class ToolRegistry:
    """Maps tool names to Tool objects; later registrations override."""

    def __init__(self, initial: Iterable[Tool] = ()):
        self._tools: dict[str, Tool] = {}
        for tool in initial:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def specs(self) -> list[ToolSpec]:
        """Model-visible definitions — consumed by the engine (phase 06)."""
        return [t.spec() for t in self._tools.values()]


def get_builtin_tools() -> list[Tool]:
    return list(BUILTIN_TOOLS)
