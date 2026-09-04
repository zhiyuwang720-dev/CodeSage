"""契约层 tools(06-P2 自 runtime_core.tool_runtime 抽出): RuntimeTool ABC + ToolExecutionContext。

契约层只依赖 contracts.models 与标准库, 不反向 import permission/session/runtime ——
RuntimeTool.check_permission 默认实现采用调用期惰性装载 ToolPermissionDecision
(引擎执行路径运行时 permission 层必已加载, 避免模块装载期成环)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from app.services.runtime_core.permission_runtime import ToolPermissionDecision

from app.services.contracts.models import ToolExecutionPayload

InterruptBehavior = Literal["cancel", "block"]


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

    async def check_permission(
        self,
        parsed_input: Any,
        context: ToolExecutionContext,
    ) -> ToolPermissionDecision:
        del parsed_input, context
        # 契约层静态不依赖 permission(层级在 contracts 之上); 默认放行在调用期惰性装载。
        from app.services.runtime_core.permission_runtime import ToolPermissionDecision

        return ToolPermissionDecision(allowed=True)

    async def execute(self, parsed_input: Any, context: ToolExecutionContext) -> ToolExecutionPayload:
        raise NotImplementedError

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
