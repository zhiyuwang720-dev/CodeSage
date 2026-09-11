"""薄模型服务外观。

`LLMService` 只保留三件事：配置快照解析、准入/间隔协调、把 SDK 结果装饰成既有 runtime 契约。adapter 选择、重试循环、价格计算、OTLP 导出都不在这里。

调用者要么经 `RuntimeBridge`（Harness 拥有重试），要么是独立一次性调用（`retry_owner=sdk`，最多 3 次实际请求）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from copy import deepcopy
from typing import Any, AsyncGenerator, Dict, List, Optional

from app.core.config import settings

from .client import SDKModelClient, get_sdk_client
from .config import (
    RETRY_BUDGET_HARNESS,
    RETRY_BUDGET_INDEPENDENT,
    RETRY_OWNER_HARNESS,
    RETRY_OWNER_SDK,
    LLMConfig,
    LLMRequest,
    ModelCatalog,
    ModelConfigurationError,
    canonical_endpoint_protocol,
    canonical_tool_message_format,
    default_base_url,
    parse_provider,
    resolve_sdk_model,
    resolve_tool_message_format,
)
from .types import LLMProvider, LLMResponse

try:
    from json_repair import repair_json

    JSON_REPAIR_AVAILABLE = True
except ImportError:  # pragma: no cover - 可选依赖
    JSON_REPAIR_AVAILABLE = False

logger = logging.getLogger(__name__)


class LLMService:
    """兼容调用外观：配置解析 + 准入协调 + 结果装饰。"""

    _provider_semaphores: Dict[str, asyncio.Semaphore] = {}
    _provider_semaphore_limits: Dict[str, int] = {}
    _provider_gap_locks: Dict[str, asyncio.Lock] = {}
    _provider_last_request_at: Dict[str, float] = {}

    def __init__(self, user_config: Optional[Dict[str, Any]] = None):
        self._user_config = user_config or {}
        self._configs: Dict[Optional[str], LLMConfig] = {}

    # ------------------------------------------------------------------ 配置解析
    @staticmethod
    def _perspective_from_agent_type(agent_type: str | None) -> str | None:
        value = str(agent_type or "").strip()
        return value.split(":", 1)[1] if value.startswith("review:") else None

    def _resolve_llm_payload(self, agent_type: Optional[str] = None) -> Dict[str, Any]:
        user_llm_config = deepcopy(self._user_config.get("llmConfig", {}) or {})
        if agent_type:
            agent_configs = user_llm_config.get("agentConfigs") or {}
            override = agent_configs.get(agent_type)
            if isinstance(override, dict) and override.get("enabled"):
                for key in (
                    "llmProvider",
                    "llmApiKey",
                    "llmModel",
                    "llmBaseUrl",
                    "llmTimeout",
                    "llmTemperature",
                    "llmTopP",
                    "llmMaxTokens",
                    "llmCustomHeaders",
                    "llmFirstTokenTimeout",
                    "llmStreamTimeout",
                    "endpointProtocol",
                    "toolMessageFormat",
                    "llmEndpointProtocol",
                    "llmToolMessageFormat",
                    "agentTimeout",
                    "subAgentTimeout",
                    "toolTimeout",
                    "alwaysThinkingEnabled",
                ):
                    value = override.get(key)
                    if value not in (None, ""):
                        user_llm_config[key] = value
                override_env = override.get("env")
                if isinstance(override_env, dict):
                    base_env = user_llm_config.get("env") if isinstance(user_llm_config.get("env"), dict) else {}
                    user_llm_config["env"] = {**base_env, **override_env}
        return user_llm_config

    def _get_runtime_env(self, llm_payload: Dict[str, Any]) -> Dict[str, str]:
        env_payload = llm_payload.get("env")
        if not isinstance(env_payload, dict):
            return {}
        return {str(key): str(value) for key, value in env_payload.items() if value not in (None, "")}

    @staticmethod
    def _provider_env_candidates(provider) -> Dict[str, List[str]]:
        prefix_map = {
            "claude": "ANTHROPIC",
            "openai": "OPENAI",
            "gemini": "GEMINI",
            "qwen": "QWEN",
            "deepseek": "DEEPSEEK",
            "zhipu": "ZHIPU",
            "moonshot": "MOONSHOT",
            "baidu": "BAIDU",
            "minimax": "MINIMAX",
            "doubao": "DOUBAO",
            "mimo": "MIMO",
            "ollama": "OLLAMA",
        }
        prefix = prefix_map.get(provider.value, "LLM")
        return {
            "api_key": [f"{prefix}_AUTH_TOKEN", f"{prefix}_API_KEY", "LLM_API_KEY"],
            "base_url": [f"{prefix}_BASE_URL", "LLM_BASE_URL"],
            "model": [f"{prefix}_MODEL", "LLM_MODEL"],
            "timeout_ms": ["API_TIMEOUT_MS", "LLM_TIMEOUT_MS"],
        }

    @staticmethod
    def _first_env_value(env_payload: Dict[str, str], keys: List[str]) -> Optional[str]:
        for key in keys:
            value = env_payload.get(key)
            if value not in (None, ""):
                return value
        return None

    def get_agent_timeout_config(self, agent_type: Optional[str] = None) -> Dict[str, int]:
        user_llm_config = self._resolve_llm_payload(agent_type)
        return {
            "llm_first_token_timeout": int(
                user_llm_config.get("llmFirstTokenTimeout") or getattr(settings, "LLM_FIRST_TOKEN_TIMEOUT", 30)
            ),
            "llm_stream_timeout": int(
                user_llm_config.get("llmStreamTimeout") or getattr(settings, "LLM_STREAM_TIMEOUT", 60)
            ),
            "agent_timeout": int(user_llm_config.get("agentTimeout") or getattr(settings, "AGENT_TIMEOUT_SECONDS", 1800)),
            "sub_agent_timeout": int(
                user_llm_config.get("subAgentTimeout") or getattr(settings, "SUB_AGENT_TIMEOUT_SECONDS", 600)
            ),
            "tool_timeout": int(user_llm_config.get("toolTimeout") or getattr(settings, "TOOL_TIMEOUT_SECONDS", 60)),
        }

    @staticmethod
    def _provider_api_key_from_user_config(provider, user_llm_config: Dict[str, Any]) -> Optional[str]:
        key_map = {
            "openai": "openaiApiKey",
            "gemini": "geminiApiKey",
            "claude": "claudeApiKey",
            "qwen": "qwenApiKey",
            "deepseek": "deepseekApiKey",
            "zhipu": "zhipuApiKey",
            "moonshot": "moonshotApiKey",
            "baidu": "baiduApiKey",
            "minimax": "minimaxApiKey",
            "doubao": "doubaoApiKey",
            "mimo": "mimoApiKey",
        }
        key_name = key_map.get(provider.value)
        return user_llm_config.get(key_name) if key_name else None

    @staticmethod
    def _provider_api_key(provider) -> str:
        key_map = {
            "openai": "OPENAI_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "claude": "CLAUDE_API_KEY",
            "qwen": "QWEN_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "zhipu": "ZHIPU_API_KEY",
            "moonshot": "MOONSHOT_API_KEY",
            "baidu": "BAIDU_API_KEY",
            "minimax": "MINIMAX_API_KEY",
            "doubao": "DOUBAO_API_KEY",
            "mimo": "MIMO_API_KEY",
        }
        key_name = key_map.get(provider.value)
        if key_name:
            return getattr(settings, key_name, "") or ""
        return "ollama"

    def get_agent_config(
        self,
        agent_type: Optional[str] = None,
        *,
        retry_owner: str = RETRY_OWNER_SDK,
        purpose: str = "review",
    ) -> LLMConfig:
        user_llm_config = self._resolve_llm_payload(agent_type)
        provider = parse_provider(user_llm_config.get("llmProvider") or getattr(settings, "LLM_PROVIDER", "openai"))
        runtime_env = self._get_runtime_env(user_llm_config)
        env_candidates = self._provider_env_candidates(provider)

        api_key = (
            user_llm_config.get("llmApiKey")
            or self._provider_api_key_from_user_config(provider, user_llm_config)
            or self._first_env_value(runtime_env, env_candidates["api_key"])
            or getattr(settings, "LLM_API_KEY", "")
            or self._provider_api_key(provider)
        )
        model = (
            user_llm_config.get("llmModel")
            or self._first_env_value(runtime_env, env_candidates["model"])
            or getattr(settings, "LLM_MODEL", "")
            or ModelCatalog.default_model(provider)
        )
        base_url = (
            user_llm_config.get("llmBaseUrl")
            or self._first_env_value(runtime_env, env_candidates["base_url"])
            or getattr(settings, "LLM_BASE_URL", None)
            or default_base_url(provider)
        )

        timeout_ms = user_llm_config.get("llmTimeout")
        if timeout_ms in (None, ""):
            timeout_ms = self._first_env_value(runtime_env, env_candidates["timeout_ms"])
            try:
                timeout_ms = int(timeout_ms) if timeout_ms not in (None, "") else None
            except (TypeError, ValueError):
                timeout_ms = None
        timeout = int(timeout_ms / 1000) if timeout_ms and timeout_ms > 1000 else int(timeout_ms or getattr(settings, "LLM_TIMEOUT", 300))

        endpoint_protocol = canonical_endpoint_protocol(
            user_llm_config.get("endpointProtocol")
            or user_llm_config.get("llmEndpointProtocol")
            or getattr(settings, "LLM_ENDPOINT_PROTOCOL", "openai_chat")
        )
        tool_message_format = canonical_tool_message_format(
            user_llm_config.get("toolMessageFormat")
            or user_llm_config.get("llmToolMessageFormat")
            or getattr(settings, "LLM_TOOL_MESSAGE_FORMAT", "auto")
        )
        if tool_message_format == "auto":
            resolve_tool_message_format(endpoint_protocol, provider=provider.value)
        custom_headers = user_llm_config.get("llmCustomHeaders")
        if not isinstance(custom_headers, dict):
            custom_headers = {}

        if not api_key and provider != LLMProvider.OLLAMA:
            raise ModelConfigurationError(
                f"缺少 API Key（provider={provider.value}）：请在配置或环境变量中提供，发送前即失败"
            )

        sdk_model, transport = resolve_sdk_model(provider, str(model or ""), base_url)
        return LLMConfig(
            provider=provider,
            api_key=str(api_key or ""),
            model=str(model or ""),
            sdk_model=sdk_model,
            transport=transport,
            base_url=base_url,
            timeout=timeout,
            temperature=user_llm_config.get("llmTemperature"),
            max_tokens=int(user_llm_config.get("llmMaxTokens") or getattr(settings, "LLM_MAX_TOKENS", 4096)),
            top_p=user_llm_config.get("llmTopP"),
            endpoint_protocol=endpoint_protocol,
            tool_message_format=tool_message_format,
            custom_headers={str(k): str(v) for k, v in custom_headers.items()},
            purpose=purpose,
            retry_owner=retry_owner,
            retry_budget=RETRY_BUDGET_HARNESS if retry_owner == RETRY_OWNER_HARNESS else RETRY_BUDGET_INDEPENDENT,
        )

    @property
    def config(self) -> LLMConfig:
        return self.get_config_for(None)

    def get_config_for(
        self,
        agent_type: Optional[str],
        *,
        retry_owner: str = RETRY_OWNER_SDK,
        purpose: str = "review",
    ) -> LLMConfig:
        key = (agent_type, retry_owner, purpose)
        cached = self._configs.get(key)  # type: ignore[arg-type]
        if cached is None:
            cached = self.get_agent_config(agent_type, retry_owner=retry_owner, purpose=purpose)
            self._configs[key] = cached  # type: ignore[index]
        return cached

    def invalidate_config_cache(self) -> None:
        self._configs.clear()

    # ------------------------------------------------------------------ 准入协调
    def _get_output_language(self) -> str:
        user_other_config = self._user_config.get("otherConfig", {}) or {}
        return user_other_config.get("outputLanguage") or getattr(settings, "OUTPUT_LANGUAGE", "zh-CN")

    def _get_runtime_llm_limits(self) -> Dict[str, int]:
        other_config = self._user_config.get("otherConfig", {}) or {}
        try:
            concurrency = (
                int(other_config["llmConcurrency"])
                if other_config.get("llmConcurrency") is not None
                else int(getattr(settings, "LLM_CONCURRENCY", 3))
            )
        except (TypeError, ValueError):
            concurrency = int(getattr(settings, "LLM_CONCURRENCY", 3))
        try:
            gap_ms = (
                int(other_config["llmGapMs"])
                if other_config.get("llmGapMs") is not None
                else int(getattr(settings, "LLM_GAP_MS", 0))
            )
        except (TypeError, ValueError):
            gap_ms = int(getattr(settings, "LLM_GAP_MS", 0))
        return {"concurrency": max(1, concurrency), "gap_ms": max(0, gap_ms)}

    def _build_provider_limit_key(self, config: LLMConfig) -> str:
        return "|".join(
            [
                config.provider.value,
                config.transport,
                config.base_url or "",
                hashlib.sha1((config.api_key or "").encode("utf-8")).hexdigest()[:12],
            ]
        )

    def _get_provider_semaphore(self, config: LLMConfig) -> asyncio.Semaphore:
        key = self._build_provider_limit_key(config)
        desired_limit = self._get_runtime_llm_limits()["concurrency"]
        semaphore = self._provider_semaphores.get(key)
        if semaphore is None or self._provider_semaphore_limits.get(key) != desired_limit:
            semaphore = asyncio.Semaphore(desired_limit)
            self._provider_semaphores[key] = semaphore
            self._provider_semaphore_limits[key] = desired_limit
        return semaphore

    def _get_provider_gap_lock(self, config: LLMConfig) -> asyncio.Lock:
        key = self._build_provider_limit_key(config)
        lock = self._provider_gap_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._provider_gap_locks[key] = lock
        return lock

    async def _await_provider_gap(self, config: LLMConfig) -> None:
        gap_ms = self._get_runtime_llm_limits()["gap_ms"]
        if gap_ms <= 0:
            return
        key = self._build_provider_limit_key(config)
        lock = self._get_provider_gap_lock(config)
        async with lock:
            now = asyncio.get_running_loop().time()
            last_started = self._provider_last_request_at.get(key)
            if last_started is not None:
                wait_seconds = (gap_ms / 1000.0) - (now - last_started)
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                    now = asyncio.get_running_loop().time()
            self._provider_last_request_at[key] = now

    # ------------------------------------------------------------------ 请求执行
    def _retry_overrides(self) -> Dict[str, Any]:
        timeouts = self.get_agent_timeout_config()
        return {
            "first_token_timeout": timeouts["llm_first_token_timeout"],
            "stream_timeout": timeouts["llm_stream_timeout"],
        }

    def _decorate_response_identity(self, response: LLMResponse, *, config: LLMConfig, request: Any) -> LLMResponse:
        if not isinstance(response, LLMResponse):
            return response
        response.configured_model = response.configured_model or config.model
        response.request_model = response.request_model or config.model
        response.provider = response.provider or config.provider.value
        response.endpoint_id = response.endpoint_id or config.endpoint_id
        response.protocol = response.protocol or config.endpoint_protocol
        response.perspective = response.perspective or request.perspective
        response.purpose = response.purpose or request.purpose
        return response

    def _decorate_stream_event(self, event: Dict[str, Any], *, config: LLMConfig, request: Any) -> Dict[str, Any]:
        payload = dict(event or {})
        payload.setdefault("configured_model", config.model)
        payload.setdefault("request_model", config.model)
        payload.setdefault("provider", config.provider.value)
        payload.setdefault("endpoint_id", config.endpoint_id)
        payload.setdefault("protocol", config.endpoint_protocol)
        payload.setdefault("perspective", request.perspective)
        payload.setdefault("purpose", request.purpose or config.purpose)
        return payload

    async def _run_completion(self, config: LLMConfig, request: LLMRequest) -> LLMResponse:
        semaphore = self._get_provider_semaphore(config)
        async with semaphore:
            await self._await_provider_gap(config)
            response = await get_sdk_client().complete(
                config=config, request=request, first_token_timeout=self._retry_overrides()["first_token_timeout"]
            )
        return self._decorate_response_identity(response, config=config, request=request)

    async def _run_stream(self, config: LLMConfig, request: LLMRequest) -> AsyncGenerator[Dict[str, Any], None]:
        semaphore = self._get_provider_semaphore(config)
        overrides = self._retry_overrides()
        async with semaphore:
            await self._await_provider_gap(config)
            async for event in get_sdk_client().stream(
                config=config, request=request, **overrides
            ):
                yield self._decorate_stream_event(event, config=config, request=request)

    # ------------------------------------------------------------------ 公开契约
    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        agent_type: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        parallel_tool_calls: Optional[bool] = None,
        purpose: str = "review",
        retry_owner: str = RETRY_OWNER_SDK,
    ) -> Dict[str, Any]:
        config = self.get_config_for(agent_type, retry_owner=retry_owner, purpose=purpose)
        request = self._build_request(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            agent_type=agent_type,
            tools=tools,
            parallel_tool_calls=parallel_tool_calls,
            purpose=purpose,
            stream=False,
            config=config,
        )
        response = await self._run_completion(config, request)
        usage = response.usage.to_dict() if response.usage is not None else None
        return {
            "content": response.content,
            "model": response.model or config.model,
            "usage": usage,
            "finish_reason": response.finish_reason,
            "tool_calls": response.tool_calls or [],
            "reasoning_content": response.reasoning_content or "",
            "tools_ignored": False,
            "configured_model": response.configured_model,
            "request_model": response.request_model,
            "response_model": response.response_model,
            "provider": response.provider,
            "endpoint_id": response.endpoint_id,
            "protocol": response.protocol,
            "perspective": response.perspective or self._perspective_from_agent_type(agent_type),
            "purpose": response.purpose or purpose,
            "provider_request_id": response.provider_request_id,
            "response_cost_usd": response.response_cost_usd,
            "model_snapshot": config.snapshot(),
        }

    async def chat_completion_raw(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        agent_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self.chat_completion(
            messages=messages, temperature=temperature, max_tokens=max_tokens, agent_type=agent_type
        )

    async def chat_completion_stream(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        agent_type: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        parallel_tool_calls: Optional[bool] = None,
        retry_enabled: bool = True,
        purpose: str = "review",
        retry_owner: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        owner = retry_owner or (RETRY_OWNER_SDK if retry_enabled else RETRY_OWNER_HARNESS)
        config = self.get_config_for(agent_type, retry_owner=owner, purpose=purpose)
        request = self._build_request(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            agent_type=agent_type,
            tools=tools,
            parallel_tool_calls=parallel_tool_calls,
            purpose=purpose,
            stream=True,
            config=config,
        )
        async for event in self._run_stream(config, request):
            yield event

    def _build_request(
        self,
        *,
        messages: List[Dict[str, Any]],
        temperature: Optional[float],
        max_tokens: Optional[int],
        agent_type: Optional[str],
        tools: Optional[List[Dict[str, Any]]],
        parallel_tool_calls: Optional[bool],
        purpose: str,
        stream: bool,
        config: LLMConfig,
    ) -> LLMRequest:
        return LLMRequest(
            messages=list(messages),
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=None,
            tools=tools,
            parallel_tool_calls=parallel_tool_calls,
            stream=stream,
            perspective=self._perspective_from_agent_type(agent_type),
            purpose=purpose,
        )

    # ------------------------------------------------------------------ JSON 辅助
    def _clean_text(self, text: str) -> str:
        clean = (text or "").replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
        clean = clean.strip()
        clean = re.sub(r"^```json\s*", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"^```\s*", "", clean)
        clean = re.sub(r"```$", "", clean).strip()
        return clean

    def clean_text(self, text: str) -> str:
        return self._clean_text(text)

    def fix_json_format(self, text: str) -> str:
        text = self._clean_text(text)
        text = re.sub(r",(\s*[}\]])", r"\1", text)
        text = re.sub(r':\s*"([^"]*)\n([^"]*)"', r': "\1\\n\2"', text)
        return text

    def aggressive_fix_json(self, text: str) -> str:
        text = self.fix_json_format(text)
        text = re.sub(r"\n+", "\\n", text)
        text = re.sub(r"\t+", " ", text)
        return text

    def _extract_from_markdown(self, text: str) -> Dict[str, Any]:
        match = re.search(r"```json\s*(\{.*?\})\s*```", text or "", flags=re.IGNORECASE | re.DOTALL)
        if not match:
            match = re.search(r"```\s*(\{.*?\})\s*```", text or "", flags=re.DOTALL)
        if not match:
            raise ValueError("No JSON block found in markdown")
        return json.loads(match.group(1))

    def _extract_json_object(self, text: str) -> Dict[str, Any]:
        clean = self._clean_text(text)
        try:
            return json.loads(clean)
        except Exception:
            pass
        match = re.search(r"\{.*\}", clean, flags=re.DOTALL)
        if match:
            candidate = match.group(0)
            try:
                return json.loads(candidate)
            except Exception:
                if JSON_REPAIR_AVAILABLE:
                    repaired = repair_json(candidate)
                    return json.loads(repaired) if isinstance(repaired, str) else repaired
        if JSON_REPAIR_AVAILABLE:
            repaired = repair_json(clean)
            return json.loads(repaired) if isinstance(repaired, str) else repaired
        raise ValueError("LLM did not return valid JSON")

    def _fix_truncated_json(self, text: str) -> Dict[str, Any]:
        start_idx = text.find("{")
        if start_idx == -1:
            raise ValueError("Cannot fix truncated JSON")
        json_str = text[start_idx:]
        json_str += "]" * max(0, json_str.count("[") - json_str.count("]"))
        json_str += "}" * max(0, json_str.count("{") - json_str.count("}"))
        json_str = re.sub(r",(\s*[}\]])", r"\1", json_str)
        return json.loads(json_str)

    def _repair_json_with_library(self, text: str) -> Dict[str, Any]:
        if not JSON_REPAIR_AVAILABLE:
            raise ValueError("json-repair library not available")
        start_idx = text.find("{")
        if start_idx == -1:
            raise ValueError("No JSON object found for repair")
        end_idx = text.rfind("}")
        json_str = text[start_idx:end_idx + 1] if end_idx > start_idx else text[start_idx:]
        repaired = repair_json(json_str, return_objects=True)
        if isinstance(repaired, dict):
            return repaired
        if isinstance(repaired, str):
            return json.loads(repaired)
        raise ValueError(f"json-repair returned unexpected type: {type(repaired)}")

    def _get_default_response(self) -> Dict[str, Any]:
        return {
            "issues": [],
            "quality_score": 80,
            "summary": {
                "total_issues": 0,
                "critical_issues": 0,
                "high_issues": 0,
                "medium_issues": 0,
                "low_issues": 0,
            },
            "metrics": {"complexity": 80, "maintainability": 80, "security": 80, "performance": 80},
        }

    def _parse_json(self, text: str) -> Dict[str, Any]:
        if not text or not text.strip():
            raise ValueError("LLM response content is empty")
        clean = self._clean_text(text)
        attempts = [
            lambda: json.loads(clean),
            lambda: json.loads(self.fix_json_format(clean)),
            lambda: self._extract_from_markdown(text),
            lambda: self._extract_json_object(clean),
            lambda: self._fix_truncated_json(clean),
            lambda: json.loads(self.aggressive_fix_json(clean)),
            lambda: self._repair_json_with_library(clean),
        ]
        last_error: Optional[Exception] = None
        for attempt in attempts:
            try:
                result = attempt()
                if isinstance(result, dict):
                    return result
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise ValueError(f"Failed to parse JSON from LLM response: {last_error}")

    def _normalize_analysis(self, payload: Dict[str, Any], code: str = "") -> Dict[str, Any]:
        issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
        normalized_issues = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            try:
                line = int(issue.get("line") or 1)
            except Exception:
                line = 1
            try:
                column = int(issue.get("column") or 1)
            except Exception:
                column = 1
            normalized_issues.append(
                {
                    "type": str(issue.get("type") or "maintainability"),
                    "severity": str(issue.get("severity") or "low"),
                    "title": str(issue.get("title") or "Issue"),
                    "description": str(issue.get("description") or ""),
                    "suggestion": str(issue.get("suggestion") or ""),
                    "line": line,
                    "column": column,
                    "code_snippet": str(issue.get("code_snippet") or ""),
                    "ai_explanation": str(issue.get("ai_explanation") or ""),
                    "xai": issue.get("xai") if isinstance(issue.get("xai"), dict) else {},
                }
            )
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        return {
            "issues": normalized_issues,
            "quality_score": float(payload.get("quality_score") or max(0, 100 - len(normalized_issues) * 5)),
            "summary": {
                "total_issues": len(normalized_issues),
                "critical_issues": sum(1 for issue in normalized_issues if issue["severity"] == "critical"),
                "high_issues": sum(1 for issue in normalized_issues if issue["severity"] == "high"),
                "medium_issues": sum(1 for issue in normalized_issues if issue["severity"] == "medium"),
                "low_issues": sum(1 for issue in normalized_issues if issue["severity"] == "low"),
                **summary,
            },
            "metrics": {
                "complexity": int(metrics.get("complexity") or 70),
                "maintainability": int(metrics.get("maintainability") or 70),
                "security": int(metrics.get("security") or 70),
                "performance": int(metrics.get("performance") or 70),
            },
        }

    def _analysis_system_prompt(self, output_language: Optional[str] = None) -> str:
        is_chinese = (output_language or self._get_output_language()).lower().startswith("zh")
        schema = json.dumps(
            {
                "issues": [
                    {
                        "type": "security|bug|performance|style|maintainability",
                        "severity": "critical|high|medium|low",
                        "title": "string",
                        "description": "string",
                        "suggestion": "string",
                        "line": 1,
                        "column": 1,
                        "code_snippet": "string",
                        "ai_explanation": "string",
                        "xai": {"what": "string", "why": "string", "how": "string", "learn_more": "string(optional)"},
                    }
                ],
                "quality_score": 0,
                "summary": {
                    "total_issues": 0,
                    "critical_issues": 0,
                    "high_issues": 0,
                    "medium_issues": 0,
                    "low_issues": 0,
                },
                "metrics": {"complexity": 0, "maintainability": 0, "security": 0, "performance": 0},
            },
            ensure_ascii=False,
            indent=2,
        )
        if is_chinese:
            return (
                "你是专业代码审计助手。请只输出 JSON，不要输出 Markdown，不要输出解释性前后缀。\n"
                "返回结果必须符合给定 Schema，并尽量发现安全、逻辑、性能和可维护性问题。\n"
                "line 和 column 必须是数字，code_snippet 使用字符串。\n"
                f"JSON Schema:\n{schema}"
            )
        return (
            "You are a professional code auditing assistant. Output JSON only. No markdown, no prose outside JSON.\n"
            "Return issues for security, bugs, performance, style, and maintainability.\n"
            f"JSON Schema:\n{schema}"
        )

    def _build_system_prompt(self, is_chinese: bool) -> str:
        return self._analysis_system_prompt("zh-CN" if is_chinese else "en-US")

    async def analyze_code(self, code: str, language: str, output_language: Optional[str] = None) -> Dict[str, Any]:
        actual_language = output_language or self._get_output_language()
        is_chinese = actual_language.lower().startswith("zh")
        code_with_lines = "\n".join(f"{i + 1}| {line}" for i, line in enumerate(code.split("\n")))
        if is_chinese:
            user_prompt = (
                f"编程语言: {language}\n\n"
                "⚠️ 代码已标注行号（格式：行号| 代码内容），请根据行号准确填写 line 字段。\n\n"
                f"请分析以下代码：\n\n{code_with_lines}"
            )
        else:
            user_prompt = (
                f"Programming Language: {language}\n\n"
                "⚠️ Code is annotated with line numbers (format: lineNumber| code), please fill the line field accurately.\n\n"
                f"Please analyze the following code:\n\n{code_with_lines}"
            )
        result = await self.chat_completion(
            messages=[
                {"role": "system", "content": self._analysis_system_prompt(actual_language)},
                {"role": "user", "content": user_prompt},
            ]
        )
        payload = self._parse_json(result.get("content", ""))
        return self._normalize_analysis(payload, code)

    async def analyze_code_with_custom_prompt(
        self,
        code: str,
        language: str,
        custom_prompt: str,
        output_language: Optional[str] = None,
        rules: Optional[list] = None,
    ) -> Dict[str, Any]:
        actual_language = output_language or self._get_output_language()
        code_with_lines = "\n".join(f"{i + 1}| {line}" for i, line in enumerate(code.split("\n")))
        rules_prompt = ""
        if rules:
            rules_prompt = "\n\nRules:\n" + "\n".join(
                f"- [{rule.get('rule_code', '')}] {rule.get('name', '')}: {rule.get('description', '')}"
                for rule in rules
                if isinstance(rule, dict) and rule.get("enabled", True)
            )
        result = await self.chat_completion(
            messages=[
                {"role": "system", "content": self._analysis_system_prompt(actual_language)},
                {"role": "user", "content": f"{custom_prompt}{rules_prompt}\n\n```{language}\n{code_with_lines}\n```"},
            ]
        )
        payload = self._parse_json(result.get("content", ""))
        return self._normalize_analysis(payload, code)

    async def analyze_code_with_rules(
        self,
        code: str,
        language: str,
        rule_set_id: Optional[str] = None,
        prompt_template_id: Optional[str] = None,
        db_session: Any = None,
        use_default_template: bool = True,
        output_language: Optional[str] = None,
    ) -> Dict[str, Any]:
        custom_prompt = None
        rules = None

        if db_session is not None:
            try:
                from sqlalchemy import select
                from sqlalchemy.orm import selectinload
                from app.models.prompt_template import PromptTemplate
                from app.models.audit_rule import AuditRuleSet

                actual_language = output_language or self._get_output_language()
                is_chinese = actual_language.lower().startswith("zh")

                if prompt_template_id:
                    result = await db_session.execute(select(PromptTemplate).where(PromptTemplate.id == prompt_template_id))
                    template = result.scalar_one_or_none()
                    if template:
                        custom_prompt = template.content_zh if is_chinese else template.content_en
                elif use_default_template:
                    result = await db_session.execute(
                        select(PromptTemplate).where(
                            PromptTemplate.is_default == True,  # noqa: E712
                            PromptTemplate.is_active == True,  # noqa: E712
                            PromptTemplate.template_type == "system",
                        )
                    )
                    template = result.scalar_one_or_none()
                    if template:
                        custom_prompt = template.content_zh if is_chinese else template.content_en

                if rule_set_id:
                    result = await db_session.execute(
                        select(AuditRuleSet).options(selectinload(AuditRuleSet.rules)).where(AuditRuleSet.id == rule_set_id)
                    )
                    rule_set = result.scalar_one_or_none()
                    if rule_set and getattr(rule_set, "rules", None):
                        rules = [
                            {
                                "rule_code": r.rule_code,
                                "name": r.name,
                                "description": r.description,
                                "category": r.category,
                                "severity": r.severity,
                                "custom_prompt": r.custom_prompt,
                                "enabled": r.enabled,
                            }
                            for r in rule_set.rules
                            if getattr(r, "enabled", True)
                        ]
            except Exception:
                logger.exception("Failed to load prompt template or rule set for analyze_code_with_rules")

        if custom_prompt:
            return await self.analyze_code_with_custom_prompt(
                code=code,
                language=language,
                custom_prompt=custom_prompt,
                output_language=output_language,
                rules=rules,
            )

        extra_lines = []
        if rule_set_id:
            extra_lines.append(f"Rule set id: {rule_set_id}")
        if prompt_template_id:
            extra_lines.append(f"Prompt template id: {prompt_template_id}")
        if rules:
            extra_lines.append("Rules:")
            extra_lines.extend(
                f"- [{rule.get('rule_code', '')}] {rule.get('name', '')}: {rule.get('description', '')}" for rule in rules
            )
        extra_text = "\n".join(extra_lines)
        prompt = (
            "Analyze the code according to the configured security rules and prompt template.\n"
            f"{extra_text}\n\n"
            f"```{language}\n{code}\n```"
        )
        result = await self.chat_completion(
            messages=[
                {"role": "system", "content": self._analysis_system_prompt(output_language)},
                {"role": "user", "content": prompt},
            ]
        )
        payload = self._parse_json(result.get("content", ""))
        return self._normalize_analysis(payload, code)


llm_service = LLMService()
