from __future__ import annotations

import json
from typing import Any

import httpx

from ..base_adapter import BaseLLMAdapter
from ..protocols.transforms import openai_responses_payload
from ..types import DEFAULT_BASE_URLS, LLMConfig, LLMError, LLMProvider, LLMRequest, LLMResponse, LLMUsage


class OpenAIResponsesAdapter(BaseLLMAdapter):
    """Adapter for OpenAI Responses API."""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._base_url = (config.base_url or DEFAULT_BASE_URLS[LLMProvider.OPENAI]).rstrip("/")

    async def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            await self.validate_config()
            return await self.retry(lambda: self._send_request(request))
        except Exception as error:
            api_response = getattr(error, "api_response", None)
            self.handle_error(error, "OpenAI Responses API call failed", api_response=api_response)

    async def _send_request(self, request: LLMRequest) -> LLMResponse:
        response = await self.client.post(
            self._responses_url(),
            headers=self._headers(),
            json=openai_responses_payload(request, model=self.config.model),
        )
        if response.status_code >= 400:
            raise self._to_llm_error(response)
        data = response.json()
        content, tool_calls = self._parse_output(data)
        usage = self._usage_from_payload(data.get("usage") or {})
        return LLMResponse(
            content=content,
            model=data.get("model") or self.config.model,
            usage=usage,
            finish_reason=self._finish_reason(data),
            tool_calls=tool_calls or None,
            reasoning_content=self._reasoning_text(data),
        )

    def _responses_url(self) -> str:
        if self._base_url.endswith("/responses"):
            return self._base_url
        if self._base_url.endswith("/v1"):
            return f"{self._base_url}/responses"
        return f"{self._base_url}/v1/responses"

    def _headers(self) -> dict[str, str]:
        headers = self.build_headers({"Accept": "application/json"})
        api_key = (self.config.api_key or "").strip()
        if api_key:
            headers["Authorization"] = api_key if api_key.lower().startswith("bearer ") else f"Bearer {api_key}"
        return headers

    @staticmethod
    def _parse_output(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        if data.get("output_text"):
            text_parts = [str(data.get("output_text") or "")]
        else:
            text_parts = []
        tool_calls: list[dict[str, Any]] = []
        for item in data.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                tool_calls.append(
                    {
                        "id": str(item.get("call_id") or item.get("id") or ""),
                        "type": "function",
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or "{}"),
                    }
                )
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                    text_parts.append(str(content.get("text") or ""))
        return "".join(text_parts), tool_calls

    @staticmethod
    def _usage_from_payload(usage: dict[str, Any]) -> LLMUsage | None:
        if not usage:
            return None
        prompt_tokens = int(usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or 0)
        return LLMUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=int(usage.get("total_tokens") or prompt_tokens + completion_tokens),
        )

    @staticmethod
    def _finish_reason(data: dict[str, Any]) -> str | None:
        for item in data.get("output") or []:
            if isinstance(item, dict) and item.get("status"):
                return str(item.get("status"))
        return str(data.get("status") or "") or None

    @staticmethod
    def _reasoning_text(data: dict[str, Any]) -> str:
        parts: list[str] = []
        for item in data.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "reasoning":
                summary = item.get("summary")
                if isinstance(summary, list):
                    parts.extend(str(part.get("text") or "") for part in summary if isinstance(part, dict))
        return "\n".join(part for part in parts if part)

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
