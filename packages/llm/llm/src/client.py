"""LLMClient: pointer resolution, retry, auxiliary fallback, cost tracking.

Design note #11: model pointers (main/task/compact/quick) resolve to
profiles; auxiliary requests (anything not "main") fall back to the main
profile on recoverable errors. Retry lives here, never in the adapters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any, Callable

import httpx
from pydantic import BaseModel

from .adapters.base import BaseAdapter
from .config import GlobalConfig
from .cost import estimate_cost
from .retry import cancelled_error, with_cancel, with_retry
from .types import ContentBlock, LLMError, LLMRequest, LLMResponse, StreamEvent

logger = logging.getLogger("llm")

DEFAULT_API_KEY_ENV = {"openai_compatible": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


class ModelProfile(BaseModel):
    """One model configuration (stored under GlobalConfig.model_profiles).

    Prefer api_key_env (the key stays in the environment, never in config
    files); api_key is a convenience for local single-user setups where the
    config file is private (0600) and the user asked for it explicitly.
    """

    #: 提供者名;缺省 env 由提供者包工厂补齐,不在契约层预设
    provider: str = "openai_compatible"
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    api_key: str | None = None

    def get_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.getenv(self.api_key_env)
        return None


class LLMClient:
    def __init__(
        self,
        *,
        project_dir: str | None = None,
        http: httpx.AsyncClient | None = None,
        total_cost: list[float] | None = None,
        cancel_event: asyncio.Event | None = None,
        provider_factories: dict[str, Callable[["ModelProfile", httpx.AsyncClient], "BaseAdapter"]] | None = None,
    ):
        self._cfg = GlobalConfig.load()
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0),
        )
        self._adapters: dict[tuple[str, str], BaseAdapter] = {}
        #: 能力接缝:provider 名 → 适配器工厂(提供者包经 install 注册)
        self._provider_factories = provider_factories or {}
        # list so cost can be shared across clients (design: session total)
        self.total_cost = total_cost if total_cost is not None else [0.0]
        #: when set, in-flight requests and retry backoff abort (LLMError "cancelled")
        self._cancel_event = cancel_event

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
            # 注册过的提供者按 名:模型 字面量解析(接缝是唯一入口)
            if provider in self._provider_factories:
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
            # 能力接缝是唯一入口:提供者包 install 注册,查不到即未安装
            factory = self._provider_factories.get(profile.provider)
            if factory is None:
                raise ValueError(
                    f"llm: provider '{profile.provider}' not registered; "
                    "load a provider package (e.g. llm_deepseek.install) first"
                )
            adapter = factory(profile, self._http)
            self._adapters[key] = adapter
        return adapter

    def register_provider_factory(
        self,
        provider: str,
        factory: Callable[["ModelProfile", httpx.AsyncClient], "BaseAdapter"],
    ) -> Callable[[], None]:
        """能力接缝:注册 provider 适配器工厂,返回撤销 disposer。

        同名重复注册抛错;撤销后该 provider 名回到未注册态。工厂
        签名 (profile, http) —— http 是 client 的共享连接池,提供者
        包不用自建;llm 服务的 LLMService 用它把注册挂到 fiber 上。
        """
        if provider in self._provider_factories:
            raise ValueError(f"provider already registered: {provider}")
        self._provider_factories[provider] = factory

        def dispose() -> None:
            self._provider_factories.pop(provider, None)

        return dispose

    # ---- completion ----

    async def complete(self, request: LLMRequest, *, model: str = "main") -> LLMResponse:
        profile = self.resolve_profile(model)
        try:
            response = await with_retry(
                lambda: with_cancel(self._adapter(profile).acomplete(request), self._cancel_event),
                cancel_event=self._cancel_event,
            )
        except LLMError as exc:
            if model == "main" or not _is_fallback_eligible(exc):
                raise
            main_profile = self.resolve_profile("main")
            if main_profile == profile:
                raise
            logger.warning("auxiliary request failed, falling back to main: %s — %s", model, exc)
            response = await with_retry(
                lambda: with_cancel(self._adapter(main_profile).acomplete(request), self._cancel_event),
                cancel_event=self._cancel_event,
            )
        response = _drop_truncated_tool_uses(response)
        if response.usage:
            self.total_cost[0] += estimate_cost(response.model or profile.model, response.usage)
        return response

    # ---- streaming ----

    def stream(self, request: LLMRequest, *, model: str = "main") -> AsyncIterator[StreamEvent]:
        """Stream events; cost is accumulated from the usage event.

        A stream that fails before its first event (connection error, an
        immediate error event, or an empty stream) is retried once — the
        common network-blip case must not kill a whole turn (design note #11).
        A still-empty stream raises a retryable LLMError.
        """
        profile = self.resolve_profile(model)
        return self._costing_stream(profile, request)

    def _open_stream(self, profile: ModelProfile, request: LLMRequest) -> AsyncIterator[StreamEvent]:
        """Open a provider stream; checks cancellation before and between events."""
        self._check_cancel()
        return _cancel_checked(self._adapter(profile).astream(request), self._cancel_event)

    def _check_cancel(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise cancelled_error()

    async def _costing_stream(
        self, profile: ModelProfile, request: LLMRequest
    ) -> AsyncIterator[StreamEvent]:
        stream = self._open_stream(profile, request)
        first = await _anext_or_none(stream)
        if first is None or first.type == "error":
            # nothing useful produced yet: one retry for transient failures / empty streams
            stream = self._open_stream(profile, request)
            first = await _anext_or_none(stream)
            if first is None:
                raise LLMError("empty response from model", retryable=True)
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
        if not error:
            # a truncated stream must never push partial tool_use blocks
            # into the execution chain (design: dropped, keep text/thinking)
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
        return _drop_truncated_tool_uses(LLMResponse(content=blocks, stop_reason=stop_reason, usage=usage))

    async def aclose(self) -> None:
        await self._http.aclose()


PARTIAL_TOOL_INPUT_KEY = "$partial_json"  # shared sentinel (adapter + collector)


def _drop_truncated_tool_uses(response: LLMResponse) -> LLMResponse:
    """PI-03: partial tool arguments must never be executed.

    Drop tool_use blocks whose input carries the partial-JSON sentinel, or
    whose response was length-truncated — either way the arguments may be
    broken. The model sees an error and re-issues the calls. Shared by the
    streaming (collect) and non-streaming (complete) paths.
    """
    content = response.content
    if not content or not any(b.type == "tool_use" for b in content):
        return response
    truncated = response.stop_reason == "length"
    kept = [
        b
        for b in content
        if not (
            b.type == "tool_use"
            and (truncated or (b.input or {}).get(PARTIAL_TOOL_INPUT_KEY) is not None)
        )
    ]
    if len(kept) == len(content):
        return response
    return LLMResponse(
        content=kept,
        stop_reason=response.stop_reason,
        usage=response.usage,
        model=response.model,
        is_error=True,
        error_message="response truncated before tool calls completed; re-issue the tool calls",
        dropped_tool_uses=len(content) - len(kept),
    )


def _is_fallback_eligible(exc: LLMError) -> bool:
    """Recoverable errors for fallback: auth/not-found/rate-limit/5xx/network."""
    # a cancelled request must not silently restart against the main profile
    return not exc.cancelled and (
        exc.status_code in (401, 403, 404, 429)
        or (exc.status_code is not None and exc.status_code >= 500)
        or exc.status_code is None
    )


async def _cancel_checked(
    stream: AsyncIterator[StreamEvent], cancel_event: asyncio.Event | None
) -> AsyncIterator[StreamEvent]:
    """Yield *stream* events, raising LLMError("cancelled") once the event is set.

    Checked per event; a stream stalled inside the provider can't be aborted
    from here (httpx read loop owns it), which is the accepted simplification.
    """
    async for ev in stream:
        if cancel_event is not None and cancel_event.is_set():
            raise cancelled_error()
        yield ev


async def _anext_or_none(stream: AsyncIterator[StreamEvent]) -> StreamEvent | None:
    """Peek the first stream event; None when the stream is empty."""
    try:
        return await anext(stream)
    except StopAsyncIteration:
        return None
