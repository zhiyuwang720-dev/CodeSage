"""Explicit allowlist for executable node handlers; never imports a payload path."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.contracts.platform import RunCommand


RunHandler = Callable[[RunCommand], Awaitable[str]]


class UnsupportedRunHandler(ValueError):
    pass


@dataclass
class RunHandlerRegistry:
    _handlers: dict[tuple[str, str, str], RunHandler] = field(default_factory=dict)

    def register(self, *, agent_type: str, entrypoint: str, version: str, handler: RunHandler) -> None:
        key = (agent_type, entrypoint, version)
        if key in self._handlers:
            raise ValueError(f"handler already registered: {key!r}")
        self._handlers[key] = handler

    def resolve(self, command: RunCommand) -> RunHandler:
        key = (command.agent_type, command.entrypoint, command.agent_version)
        try:
            return self._handlers[key]
        except KeyError as exc:
            raise UnsupportedRunHandler(f"unsupported run handler: {key!r}") from exc
