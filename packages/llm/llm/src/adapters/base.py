"""Adapter contract: one provider transport, converting to/from the internal contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import httpx

from ..types import LLMRequest, LLMResponse, StreamEvent


class BaseAdapter(ABC):
    def __init__(self, profile: "ModelProfile", http: httpx.AsyncClient):
        self.profile = profile
        self.http = http

    @abstractmethod
    async def acomplete(self, request: LLMRequest) -> LLMResponse:
        """Non-streaming completion; raises LLMError on provider failure."""

    @abstractmethod
    def astream(self, request: LLMRequest) -> AsyncIterator[StreamEvent]:
        """Streaming completion; errors surface as StreamEvent(type="error")."""
