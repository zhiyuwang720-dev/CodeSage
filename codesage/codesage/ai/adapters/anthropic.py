"""Anthropic Messages API adapter (native, via httpx).

The internal contract is already Anthropic-shaped, so conversion here is
nearly a passthrough — this adapter exists to keep provider transport logic
out of the client.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..types import (
    ContentBlock,
    LLMError,
    LLMRequest,
    LLMResponse,
    Message,
    StreamEvent,
    Usage,
)
from .base import BaseAdapter

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicAdapter(BaseAdapter):
    def _url(self) -> str:
        base = (self.profile.base_url or "https://api.anthropic.com").rstrip("/")
        return f"{base}/v1/messages"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        api_key = self.profile.api_key()
        if api_key:
            headers["x-api-key"] = api_key
        return headers

    def _build_payload(self, request: LLMRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "max_tokens": request.max_tokens,
            "messages": [self._message_to_api(m) for m in request.messages],
            "stream": stream,
        }
        if request.system:
            payload["system"] = request.system
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in request.tools
            ]
        if request.stop_sequences:
            payload["stop_sequences"] = request.stop_sequences
        return payload

    @staticmethod
    def _message_to_api(msg: Message) -> dict[str, Any]:
        if isinstance(msg.content, str):
            return {"role": msg.role, "content": msg.content}
        blocks = []
        for b in msg.content:
            if b.type == "text":
                blocks.append({"type": "text", "text": b.text or ""})
            elif b.type == "thinking":
                blocks.append({"type": "thinking", "thinking": b.text or ""})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input or {}})
            elif b.type == "tool_result":
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": b.tool_use_id,
                        "content": b.content if isinstance(b.content, str) else b.content or "",
                        "is_error": b.is_error,
                    }
                )
        return {"role": msg.role, "content": blocks}

    async def acomplete(self, request: LLMRequest) -> LLMResponse:
        try:
            response = await self.http.post(
                self._url(), headers=self._headers(), json=self._build_payload(request, stream=False)
            )
        except httpx.HTTPError as exc:
            raise _transport_error(self.profile, exc) from exc
        if response.status_code >= 400:
            raise _http_error(self.profile, response)
        data = response.json()
        blocks = [ContentBlock.model_validate(b) for b in data.get("content") or []]
        return LLMResponse(
            content=blocks,
            stop_reason=data.get("stop_reason"),
            usage=_usage_from_anthropic(data.get("usage")),
            model=data.get("model"),
        )

    async def astream(self, request: LLMRequest) -> AsyncIterator[StreamEvent]:
        try:
            async with self.http.stream(
                "POST", self._url(), headers=self._headers(), json=self._build_payload(request, stream=True)
            ) as response:
                if response.status_code >= 400:
                    yield StreamEvent(type="error", error=f"HTTP {response.status_code}: {response.text[:200]}")
                    return
                # content block state: tool_use blocks stream input_json_delta
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype == "message_start":
                        usage = _usage_from_anthropic((event.get("message") or {}).get("usage"))
                        if usage:
                            yield StreamEvent(type="usage", usage=usage)
                    elif etype == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            yield StreamEvent(
                                type="tool_use_start",
                                tool_use_id=block.get("id"),
                                tool_name=block.get("name"),
                            )
                    elif etype == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            yield StreamEvent(type="text_delta", text=delta.get("text"))
                        elif delta.get("type") == "thinking_delta":
                            yield StreamEvent(type="thinking_delta", thinking=delta.get("thinking"))
                        elif delta.get("type") == "input_json_delta":
                            yield StreamEvent(type="tool_use_delta", input_json_delta=delta.get("partial_json"))
                    elif etype == "message_delta":
                        stop = (event.get("delta") or {}).get("stop_reason")
                        usage = _usage_from_anthropic(event.get("usage"))
                        if usage:
                            yield StreamEvent(type="usage", usage=usage)
                        yield StreamEvent(type="done", stop_reason=stop)
                else:
                    yield StreamEvent(type="done")
        except httpx.HTTPError as exc:
            yield StreamEvent(type="error", error=f"transport error: {exc}")


def _usage_from_anthropic(usage: dict[str, Any] | None) -> Usage | None:
    if not usage:
        return None
    return Usage(
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        cache_write_tokens=usage.get("cache_write_input_tokens", 0),
        total_tokens=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
    )


def _http_error(profile: Any, response: httpx.Response) -> LLMError:
    status = response.status_code
    try:
        retry_after = float(response.headers.get("retry-after", ""))
    except ValueError:
        retry_after = None
    return LLMError(
        f"{profile.model}: HTTP {status}: {response.text[:500]}",
        provider=profile.provider,
        status_code=status,
        retryable=LLMError.classify(status),
        retry_after_seconds=retry_after,
    )


def _transport_error(profile: Any, exc: httpx.HTTPError) -> LLMError:
    """Wrap httpx transport failures (timeout/connect/read) as retryable errors."""
    return LLMError(
        f"{profile.model}: transport error: {exc}",
        provider=profile.provider,
        retryable=True,
        original_error=exc,
    )
