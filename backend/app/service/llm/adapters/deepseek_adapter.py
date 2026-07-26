"""DeepSeek API adapter — OpenAI-compatible chat completions."""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..base_adapter import BaseLLMAdapter
from ..protocols.transforms import openai_chat_payload
from ..types import DEFAULT_BASE_URLS, LLMConfig, LLMError, LLMProvider, LLMRequest, LLMResponse, LLMUsage


class DeepSeekAdapter(BaseLLMAdapter):
    """Adapter for DeepSeek API (OpenAI-compatible /v1/chat/completions)."""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._base_url = (config.base_url or DEFAULT_BASE_URLS[LLMProvider.DEEPSEEK]).rstrip("/")

    # ------------------------------------------------------------------
    # 核心入口
    # ------------------------------------------------------------------

    async def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            await self.validate_config()
            return await self.retry(lambda: self._send_request(request))
        except Exception as error:
            api_response = getattr(error, "api_response", None)
            self.handle_error(error, "DeepSeek API call failed", api_response=api_response)

    # ------------------------------------------------------------------
    # 请求 / 响应
    # ------------------------------------------------------------------

    async def _send_request(self, request: LLMRequest) -> LLMResponse:
        response = await self.client.post(
            self._chat_url(),
            headers=self._headers(),
            json=openai_chat_payload(request, model=self.config.model),
        )
        if response.status_code >= 400:
            raise self._to_llm_error(response)
        return self._parse_response(response.json())

    def _chat_url(self) -> str:
        base = self._base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = self.build_headers()
        api_key = (self.config.api_key or "").strip()
        if api_key:
            headers["Authorization"] = api_key if api_key.lower().startswith("bearer ") else f"Bearer {api_key}"
        return headers

    # ------------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------------

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}

        content = str(message.get("content") or "")
        tool_calls = message.get("tool_calls")
        reasoning = message.get("reasoning_content")

        usage = self._parse_usage(data.get("usage") or {})

        return LLMResponse(
            content=content,
            model=data.get("model") or self.config.model,
            usage=usage,
            finish_reason=choice.get("finish_reason"),
            tool_calls=tool_calls or None,
            reasoning_content=reasoning or None,
        )

    @staticmethod
    def _parse_usage(usage: dict[str, Any]) -> LLMUsage | None:
        if not usage:
            return None
        return LLMUsage(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
        )

    # ------------------------------------------------------------------
    # 错误处理
    # ------------------------------------------------------------------

    def _to_llm_error(self, response: httpx.Response) -> LLMError:
        raw = response.text
        message = raw or f"HTTP {response.status_code}"
        try:
            payload = json.loads(raw) if raw else {}
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                message = str(error.get("message") or message)
        except Exception:
            pass
        return LLMError(message, self.config.provider, response.status_code, api_response=raw)
