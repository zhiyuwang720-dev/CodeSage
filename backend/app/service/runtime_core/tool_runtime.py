from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, AsyncGenerator, Awaitable, Callable, Literal

from pydantic import BaseModel, ValidationError

InterruptBehavior = Literal["cancel", "block"]
DEFAULT_RUNTIME_TOOL_TIMEOUT_SECONDS = 120
RUNTIME_SEARCH_TOOL_TIMEOUT_SECONDS = 45
RUNTIME_SEARCH_TOOL_MAX_TIMEOUT_SECONDS = 120
RUNTIME_TOOL_TIMEOUT_HINT = "工具执行超时：请缩小 path/glob/pattern 后重试。"


@dataclass(slots=True)
class ToolExecutionContext:
    session_id: str
    turn_id: str
    tool_use_id: str
    tool_call_id: str
    agent_type: str = "runtime"
    session: Any = None
    recon_payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    on_progress: Callable[[dict[str, Any]], None] | None = None

    def report_progress(self, *, event: str, message: str | None = None, data: dict[str, Any] | None = None) -> None:
        if self.on_progress is None:
            return
        payload = {"event": str(event)}
        if message:
            payload["message"] = str(message)
        if data:
            payload.update(dict(data))
        self.on_progress(payload)


def match_runtime_event_hooks(hook_config: dict[str, Any], *, event_name: str, tool_name: str) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for entry in hook_config.get(event_name) or []:
        matcher = str(entry.get("matcher") or "").strip()
        if matcher not in {"", "*", tool_name}:
            continue
        matched.append(dict(entry))
    return matched


class RuntimeTool:
    name: str = ""
    description: str = ""
    input_model: type[BaseModel] | None = None
    aliases: list[str] = []
    search_hint: str | None = None
    should_defer: bool = False
    always_load: bool = False

    def validate_input(self, raw_input: dict[str, Any]) -> Any:
        if self.input_model is None:
            return raw_input
        return self.input_model.model_validate(raw_input or {})

    def is_enabled(self) -> bool:
        return True

    def is_concurrency_safe(self, parsed_input: Any = None) -> bool:
        return False

    def concurrency_key(self, parsed_input: Any = None) -> str | None:
        return None

    def is_read_only(self, parsed_input: Any = None) -> bool:
        return False

    def is_destructive(self, parsed_input: Any = None) -> bool:
        return False

    def interrupt_behavior(self) -> InterruptBehavior:
        return "block"

    def requires_user_interaction(self) -> bool:
        return False

    def execution_timeout_seconds(self, parsed_input: Any = None, context: "ToolExecutionContext | None" = None) -> float | None:
        del parsed_input, context
        return None


    def user_facing_name(self, raw_input: Any | None = None) -> str:
        del raw_input
        return self.name

    def describe(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema() if self.input_model is not None else {"type": "object"}
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
            "aliases": list(self.aliases or []),
            "search_hint": self.search_hint,
            "read_only": self._safe_metadata_bool(lambda: self.is_read_only(None), default=False),
            "destructive": self._safe_metadata_bool(lambda: self.is_destructive(None), default=False),
            "interrupt_behavior": self._safe_interrupt_behavior(),
            "requires_user_interaction": self._safe_metadata_bool(self.requires_user_interaction, default=False),
            "should_defer": bool(self.should_defer),
            "always_load": bool(self.always_load),
        }

    @staticmethod
    def _safe_metadata_bool(callback: Callable[[], bool], *, default: bool) -> bool:
        try:
            return bool(callback())
        except Exception:
            return default

    def _safe_interrupt_behavior(self) -> InterruptBehavior:
        try:
            behavior = self.interrupt_behavior()
        except Exception:
            behavior = "block"
        return "cancel" if behavior == "cancel" else "block"
