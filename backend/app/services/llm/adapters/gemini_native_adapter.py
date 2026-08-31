from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx

from ..base_adapter import BaseLLMAdapter
from ..protocols.transforms import gemini_native_payload
from ..types import DEFAULT_BASE_URLS, LLMConfig, LLMError, LLMProvider, LLMRequest, LLMResponse, LLMUsage


class GeminiNativeAdapter(BaseLLMAdapter):
    """Adapter for Gemini native generateContent API."""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._base_url = (config.base_url or DEFAULT_BASE_URLS[LLMProvider.GEMINI]).rstrip("/")

    async def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            await self.validate_config()
            return await self.retry(lambda: self._send_request(request))
        except Exception as error:
            api_response = getattr(error, "api_response", None)
            self.handle_error(error, "Gemini Native API call failed", api_response=api_response)

    async def _send_request(self, request: LLMRequest) -> LLMResponse:
        payload = gemini_native_payload(request, model=self.config.model)
        payload.pop("model", None)
        response = await self.client.post(
            self._generate_content_url(),
            headers=self._headers(),
            params={"key": self.config.api_key},
            json=payload,
        )
        if response.status_code >= 400:
            raise self._to_llm_error(response)
        data = response.json()
        content, tool_calls = self._parse_candidates(data)
        usage = self._usage_from_payload(data.get("usageMetadata") or {})
        return LLMResponse(
            content=content,
            model=self.config.model,
            usage=usage,
            finish_reason=self._finish_reason(data),
            tool_calls=tool_calls or None,
        )

    def _generate_content_url(self) -> str:
        model = self.config.model
        if not model.startswith("models/"):
            model = f"models/{model}"
        encoded_model = quote(model, safe="/")
        if self._base_url.endswith(f"/{encoded_model}:generateContent"):
            return self._base_url
        return f"{self._base_url}/{encoded_model}:generateContent"

    def _headers(self) -> dict[str, str]:
        return self.build_headers({"Accept": "application/json"})

    @staticmethod
    def _parse_candidates(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for candidate in data.get("candidates") or []:
            content = candidate.get("content") if isinstance(candidate, dict) else {}
            for part in (content or {}).get("parts") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("text"):
                    text_parts.append(str(part.get("text") or ""))
                function_call = part.get("functionCall")
                if isinstance(function_call, dict):
                    args = function_call.get("args") if isinstance(function_call.get("args"), dict) else {}
                    tool_calls.append(
                        {
                            "id": str(function_call.get("id") or function_call.get("name") or ""),
                            "type": "function",
                            "name": str(function_call.get("name") or ""),
                            "arguments": json.dumps(args, ensure_ascii=False),
                        }
                    )
        return "".join(text_parts), tool_calls

    @staticmethod
    def _usage_from_payload(usage: dict[str, Any]) -> LLMUsage | None:
        if not usage:
            return None
        prompt_tokens = int(usage.get("promptTokenCount") or 0)
        completion_tokens = int(usage.get("candidatesTokenCount") or 0)
        return LLMUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=int(usage.get("totalTokenCount") or prompt_tokens + completion_tokens),
        )

    @staticmethod
    def _finish_reason(data: dict[str, Any]) -> str | None:
        for candidate in data.get("candidates") or []:
            if isinstance(candidate, dict) and candidate.get("finishReason"):
                return str(candidate.get("finishReason"))
        return None

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
