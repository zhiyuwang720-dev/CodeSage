"""LLMClient: pointer resolution, retry, auxiliary fallback, cost tracking.

Design note #11: model pointers (main/task/compact/quick) resolve to
profiles; auxiliary requests (anything not "main") fall back to the main
profile on recoverable errors. Retry lives here, never in the adapters.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from .adapters.anthropic import AnthropicAdapter
from .adapters.base import BaseAdapter
from .adapters.openai_compatible import OpenAICompatibleAdapter
from .cost import estimate_cost
from .retry import with_retry
from .types import ContentBlock, LLMError, LLMRequest, LLMResponse, StreamEvent
from .vcr import VCRTransport
from ..config import GlobalConfig

logger = logging.getLogger("codesage.ai")

DEFAULT_API_KEY_ENV = {"openai_compatible": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


class ModelProfile(BaseModel):
    """One model configuration (stored under GlobalConfig.model_profiles)."""

    provider: Literal["openai_compatible", "anthropic"] = "openai_compatible"
    model: str
    base_url: str | None = None
    api_key_env: str | None = None

    def api_key(self) -> str | None:
        env_name = self.api_key_env or DEFAULT_API_KEY_ENV[self.provider]
        return os.getenv(env_name)


class LLMClient:
    def __init__(
        self,
        *,
        project_dir: str | None = None,
        http: httpx.AsyncClient | None = None,
        vcr_mode: str | None = None,
        total_cost: list[float] | None = None,
    ):
        self._cfg = GlobalConfig.load()
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0),
            transport=VCRTransport(vcr_mode) if vcr_mode else None,
        )
        self._adapters: dict[tuple[str, str], BaseAdapter] = {}
        # list so cost can be shared across clients (design: session total)
        self.total_cost = total_cost if total_cost is not None else [0.0]

    # ---- pointer resolution ----

    def resolve_profile(self, model: str) -> ModelProfile:
        """Resolve a pointer name / profile name / literal into a profile.

        Chain: pointer -> pointer -> ... -> profile name -> literal
        ({"main": "main"} default means "main" is the profile name itself).
        """
        name = model
        pointers = self._cfg.model_pointers
        seen: set[str] = set()
        while name in pointers and name not in seen:
            seen.add(name)
            target = pointers[name]
            if target == name:
                break
            name = target
        profiles = self._cfg.model_profiles
        if name in profiles:
            return ModelProfile(**{**{"provider": "openai_compatible", "model": name}, **profiles[name]})
        if ":" in name:
            provider, model_name = name.split(":", 1)
            if provider in ("anthropic", "openai_compatible"):
                return ModelProfile(provider=provider, model=model_name)
        # literal fallback: env vars configure the default endpoint
        # (CODESAGE_MODEL / CODESAGE_BASE_URL / CODESAGE_API_KEY_ENV)
        return ModelProfile(
            model=os.getenv("CODESAGE_MODEL") or name,
            base_url=os.getenv("CODESAGE_BASE_URL"),
            api_key_env=os.getenv("CODESAGE_API_KEY_ENV"),
        )

    # ---- adapters ----

    def _adapter(self, profile: ModelProfile) -> BaseAdapter:
        key = (profile.provider, profile.model)
        adapter = self._adapters.get(key)
        if adapter is None:
            cls = AnthropicAdapter if profile.provider == "anthropic" else OpenAICompatibleAdapter
            adapter = cls(profile, self._http)
            self._adapters[key] = adapter
        return adapter

    # ---- completion ----

    async def complete(self, request: LLMRequest, *, model: str = "main") -> LLMResponse:
        profile = self.resolve_profile(model)
        try:
            response = await with_retry(lambda: self._adapter(profile).acomplete(request))
        except LLMError as exc:
            if model == "main" or not _is_fallback_eligible(exc):
                raise
            main_profile = self.resolve_profile("main")
            if main_profile == profile:
                raise
            logger.warning("auxiliary request failed, falling back to main: %s — %s", model, exc)
            response = await with_retry(lambda: self._adapter(main_profile).acomplete(request))
        if response.usage:
            self.total_cost[0] += estimate_cost(response.model or profile.model, response.usage)
        return response

    # ---- streaming ----

    def stream(self, request: LLMRequest, *, model: str = "main") -> AsyncIterator[StreamEvent]:
        """Stream events; cost is accumulated from the usage event.

        A stream that fails before its first event (connection error or an
        immediate error event) is retried once — the common network-blip case
        must not kill a whole turn (design note #11).
        """
        profile = self.resolve_profile(model)
        return self._costing_stream(profile, request)

    async def _costing_stream(
        self, profile: ModelProfile, request: LLMRequest
    ) -> AsyncIterator[StreamEvent]:
        stream = self._adapter(profile).astream(request)
        first = await _anext_or_none(stream)
        if first is None:
            return
        if first.type == "error":
            # nothing produced yet: one retry for transient failures
            stream = self._adapter(profile).astream(request)
            first = await _anext_or_none(stream)
            if first is None:
                return
        yield first

        usage = first.usage if first.type == "usage" else None
        async for ev in stream:
            if ev.type == "usage":
                usage = ev.usage
            yield ev
        if usage is not None:
            self.total_cost[0] += estimate_cost(profile.model, usage)

    @staticmethod
    async def collect(stream: AsyncIterator[StreamEvent]) -> LLMResponse:
        """Assemble a streamed event sequence into a final LLMResponse."""
        text: list[str] = []
        thinking: list[str] = []
        tool_uses: dict[str, dict[str, Any]] = {}
        tool_order: list[str] = []
        usage = None
        stop_reason = None
        error = None
        async for ev in stream:
            if ev.type == "text_delta":
                text.append(ev.text or "")
            elif ev.type == "thinking_delta":
                thinking.append(ev.thinking or "")
            elif ev.type == "tool_use_start":
                key = ev.tool_use_id or f"t{len(tool_order)}"
                tool_order.append(key)
                tool_uses[key] = {"id": ev.tool_use_id, "name": ev.tool_name, "json": ""}
            elif ev.type == "tool_use_delta":
                if tool_order:
                    tool_uses[tool_order[-1]]["json"] += ev.input_json_delta or ""
            elif ev.type == "usage":
                usage = ev.usage
            elif ev.type == "error":
                error = ev.error
            elif ev.type == "done":
                stop_reason = ev.stop_reason

        blocks: list[ContentBlock] = []
        if thinking:
            blocks.append(ContentBlock(type="thinking", text="".join(thinking)))
        if text:
            blocks.append(ContentBlock(type="text", text="".join(text)))
        for key in tool_order:
            tu = tool_uses[key]
            try:
                parsed = json.loads(tu["json"]) if tu["json"] else {}
            except json.JSONDecodeError:
                parsed = {"_partial_json": tu["json"]}
            blocks.append(
                ContentBlock(type="tool_use", id=tu["id"], name=tu["name"], input=parsed)
            )
        if error:
            return LLMResponse(
                content=blocks, stop_reason="error", usage=usage, is_error=True, error_message=error
            )
        return LLMResponse(content=blocks, stop_reason=stop_reason, usage=usage)

    async def aclose(self) -> None:
        await self._http.aclose()


def _is_fallback_eligible(exc: LLMError) -> bool:
    """Recoverable errors for fallback: auth/not-found/rate-limit/5xx/network."""
    return (
        exc.status_code in (401, 403, 404, 429)
        or (exc.status_code is not None and exc.status_code >= 500)
        or exc.status_code is None
    )


async def _anext_or_none(stream: AsyncIterator[StreamEvent]) -> StreamEvent | None:
    """Peek the first stream event; None when the stream is empty."""
    try:
        return await anext(stream)
    except StopAsyncIteration:
        return None
