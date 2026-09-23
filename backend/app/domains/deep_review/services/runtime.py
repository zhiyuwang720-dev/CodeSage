from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar
from pydantic import BaseModel

T = TypeVar("T")


@dataclass
class AgentCallResult(Generic[T]):
    value: T | None = None
    error: str | None = None
    session_id: str | None = None
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None


class DeepReviewRuntime(Protocol):
    async def ai(self, prompt: str, **kwargs: Any) -> Any: ...

    async def harness(self, prompt: str, *, schema: type[BaseModel], tools: list[Any], **kwargs: Any) -> Any: ...


class DeepReviewRuntimeFactory(Protocol):
    def for_role(self, role: str) -> DeepReviewRuntime: ...


class RuntimeBridgeDeepReviewRuntimeFactory:
    def __init__(self, *, llm_service, session_factory=None):
        self._llm_service = llm_service
        self._session_factory = session_factory

    def for_role(self, role: str) -> Any:
        from app.execution_plane.runtime.bridge import RuntimeBridge

        self._llm_service.get_config_for(role)
        return RuntimeBridge(
            llm_service=self._llm_service,
            tools=[],
            session_factory=self._session_factory,
            agent_type=role,
        )
