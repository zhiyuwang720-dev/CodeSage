"""Tool contract: a flat object (metadata + validate + execute), not a class hierarchy.

Mirrors Kode's tool-interface: one object per tool. Execution is an async
generator — yield ToolProgress for live UI, return ToolResult as the final
value. `needs_permissions()` is a self-declaration consumed by the
permission engine (phase 05); tools never enforce permissions themselves.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..ai import ContentBlock, Message, ToolSpec


class ToolError(Exception):
    """Tool execution failure; message is shown to the model as-is."""


@dataclass(slots=True)
class ToolUseContext:
    """What a tool needs beyond its input (injected by the engine, phase 06)."""

    cwd: Path
    timeout: int = 60
    env: dict[str, str] | None = None
    command_source: str = "agent_call"  # user_bash_mode | agent_call
    #: path -> mtime_ns / sha256 recorded by ReadTool; Edit/Write verify
    #: against these so external changes are never silently overwritten.
    read_file_timestamps: dict[str, float] = field(default_factory=dict)
    read_file_hashes: dict[str, str] = field(default_factory=dict)
    #: (path, offset, limit) -> (mtime_ns, output): a same-args re-Read with
    #: unchanged mtime returns a stub instead of resending the whole content.
    read_cache: dict[tuple[str, int, int], tuple[int, str]] = field(default_factory=dict)
    #: set by the engine to abort a running tool (Bash kills its process tree).
    abort_event: asyncio.Event | None = None


@dataclass(slots=True)
class ToolProgress:
    """Transient progress update (live UI); not sent to the model."""

    text: str


@dataclass(slots=True)
class ToolResult:
    """Final tool output. content is what the model sees."""

    content: str | list[ContentBlock]
    is_error: bool = False
    new_messages: list[Message] | None = None  # injected into the conversation (phase 06)
    metadata: dict[str, Any] = field(default_factory=dict)


class Tool:
    """One tool: metadata + optional validation + async-generator execution.

    Subclass or construct directly; `call` may yield progress before
    returning the final ToolResult. input_schema is treated as read-only
    after construction (never mutated in place).
    """

    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    #: fail-closed: only read-only tools explicitly opt into parallel execution.
    is_concurrency_safe: bool = False
    user_facing_name: str | None = None

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, input_schema=self.input_schema)

    def needs_permissions(self, input: dict[str, Any]) -> bool:
        """Self-declaration for the permission engine (phase 05)."""
        return True

    def validate_input(self, input: dict[str, Any]) -> None:
        """Raise ToolError on invalid input; called before execution."""

    async def call(self, input: dict[str, Any], ctx: ToolUseContext) -> AsyncIterator[ToolResult]:
        """Execute; async generator yielding ToolResult once (may yield progress).
        Base implementation runs _run and yields its result."""
        result = await self._run(input, ctx)
        yield result

    async def _run(self, input: dict[str, Any], ctx: ToolUseContext) -> ToolResult:
        raise NotImplementedError
