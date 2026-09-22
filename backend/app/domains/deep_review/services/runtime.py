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

