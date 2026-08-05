"""OpenAI Chat Completions compatible adapter (DeepSeek / Qwen / GLM / OpenAI).

Converts the internal contract to the OpenAI wire format and back. DeepSeek's
cache usage split (prompt_cache_hit/miss_tokens) and reasoning_content are
normalized into the internal contract here — the only boundary where that
happens (design note #12).
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


class OpenAICompatibleAdapter(BaseAdapter):
    """OpenAI-compatible Chat Completions endpoint."""

    def _url(self) -> str:
        base = (self.profile.base_url or "https://api.openai.com/v1").rstrip("/")
        return f"{base}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = self.profile.api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    # ---- request conversion (internal -> OpenAI) ----

    def _build_payload(self, request: LLMRequest, *, stream: bool) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for msg in request.messages:
            messages.extend(self._message_to_openai(msg))
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "messages": messages,
            "stream": stream,
            "max_tokens": request.max_tokens,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in request.tools
            ]
        if request.stop_sequences:
            payload["stop"] = request.stop_sequences
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _message_to_openai(self, msg: Message) -> list[dict[str, Any]]:
        """Convert one internal message into one or more OpenAI messages."""
        if isinstance(msg.content, str):
            return [{"role": msg.role, "content": msg.content}]
        blocks = msg.content
        # tool_result blocks become role=tool messages; the remaining text /
        # tool_use blocks stay as one message of the original role. A merged
        # user message carries both (see core.normalize_for_api).
        out: list[dict[str, Any]] = [
            {"role": "tool", "tool_call_id": b.tool_use_id, "content": self._tool_result_text(b)}
            for b in blocks
            if b.type == "tool_result"
        ]
        text_parts = [b.text or "" for b in blocks if b.type in ("text", "thinking")]
        tool_uses = [b for b in blocks if b.type == "tool_use"]
        rest: dict[str, Any] = {"role": msg.role}
        if text_parts:
            rest["content"] = "\n".join(p for p in text_parts if p)
        if tool_uses:
            rest["tool_calls"] = [
                {
                    "id": b.id,
                    "type": "function",
                    "function": {"name": b.name, "arguments": json.dumps(b.input or {})},
                }
                for b in tool_uses
            ]
        if text_parts or tool_uses:
            out.append(rest)
        return out

    @staticmethod
    def _tool_result_text(block: ContentBlock) -> str:
        if isinstance(block.content, str):
            return block.content
        if isinstance(block.content, list):
            return "\n".join(b.text or "" for b in block.content)
        return ""

    # ---- response conversion (OpenAI -> internal) ----

    def _response_to_internal(self, data: dict[str, Any]) -> LLMResponse:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        reasoning = message.get("reasoning_content")
        blocks: list[ContentBlock] = []
        if reasoning:
            blocks.append(ContentBlock(type="thinking", text=reasoning))
        if content:
            blocks.append(ContentBlock(type="text", text=content))
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            blocks.append(
                ContentBlock(
                    type="tool_use",
                    id=tc.get("id"),
                    name=fn.get("name"),
                    input=json.loads(fn.get("arguments") or "{}"),
                )
            )
        return LLMResponse(
            content=blocks,
            stop_reason=self._stop_reason(choice.get("finish_reason")),
            usage=_usage_from_openai(data.get("usage")),
            model=data.get("model"),
        )

    @staticmethod
    def _stop_reason(finish: str | None) -> str | None:
        return {
            "stop": "end_turn",
            "length": "length",
            "tool_calls": "tool_use",
            "content_filter": "content_filter",
        }.get(finish or "")

    # ---- non-streaming ----

    async def acomplete(self, request: LLMRequest) -> LLMResponse:
        payload = self._build_payload(request, stream=False)
        try:
            response = await self.http.post(self._url(), headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise _transport_error(self.profile, exc) from exc
        if response.status_code >= 400:
            raise _http_error(self.profile, response)
        return self._response_to_internal(response.json())

    # ---- streaming ----

    async def astream(self, request: LLMRequest) -> AsyncIterator[StreamEvent]:
        payload = self._build_payload(request, stream=True)
        try:
            async with self.http.stream(
                "POST", self._url(), headers=self._headers(), json=payload
            ) as response:
                if response.status_code >= 400:
                    yield StreamEvent(
                        type="error",
                        error=f"HTTP {response.status_code}: {response.text[:200]}",
                    )
                    return
                tool_partials: dict[int, dict[str, Any]] = {}
                seen_tools: set[int] = set()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        yield StreamEvent(type="error", error=json.dumps(chunk["error"]))
                        continue
                    # usage may ride on a chunk with or without choices (providers differ)
                    if chunk.get("usage"):
                        yield StreamEvent(type="usage", usage=_usage_from_openai(chunk["usage"]))
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        yield StreamEvent(type="text_delta", text=delta["content"])
                    if delta.get("reasoning_content"):
                        yield StreamEvent(type="thinking_delta", thinking=delta["reasoning_content"])
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        fn = tc.get("function") or {}
                        if idx not in seen_tools:
                            seen_tools.add(idx)
                            tool_partials[idx] = {"id": tc.get("id"), "name": fn.get("name")}
                            yield StreamEvent(
                                type="tool_use_start",
                                tool_use_id=tc.get("id"),
                                tool_name=fn.get("name"),
                            )
                        if fn.get("arguments"):
                            yield StreamEvent(type="tool_use_delta", input_json_delta=fn["arguments"])
                    finish = choice.get("finish_reason")
                    if finish:
                        yield StreamEvent(type="done", stop_reason=self._stop_reason(finish))
                else:
                    yield StreamEvent(type="done")
        except httpx.HTTPError as exc:
            # connection failure / mid-stream disconnect: surface as an error
            # event (retried by the client wrapper if nothing was yielded yet)
            yield StreamEvent(type="error", error=f"transport error: {exc}")


def _usage_from_openai(usage: dict[str, Any] | None) -> Usage | None:
    if not usage:
        return None
    # DeepSeek splits cache usage: prompt_cache_hit_tokens / prompt_cache_miss_tokens.
    # Accept both snake_case (DeepSeek) and camelCase (some compatible endpoints).
    hit = (
        usage.get("prompt_cache_hit_tokens")
        or usage.get("promptCacheHitTokens")
        or usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
    )
    miss = (
        usage.get("prompt_cache_miss_tokens")
        or usage.get("promptCacheMissTokens")
        or usage.get("prompt_tokens", 0) - hit
    )
    return Usage(
        input_tokens=miss,
        output_tokens=usage.get("completion_tokens", 0),
        cache_read_tokens=hit,
        total_tokens=usage.get("total_tokens", miss + usage.get("completion_tokens", 0)),
    )


def _http_error(profile: Any, response: httpx.Response) -> LLMError:
    status = response.status_code
    detail = response.text[:500]
    try:
        retry_after = float(response.headers.get("retry-after", ""))
    except ValueError:
        retry_after = None
    return LLMError(
        f"{profile.model}: HTTP {status}: {detail}",
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
        retryable=True,  # network failures are always retryable
        original_error=exc,
    )
