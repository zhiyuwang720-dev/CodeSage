import asyncio
import hashlib
import os
from copy import deepcopy
from typing import Any, AsyncGenerator, Dict, List, Optional

from app.service.core.errors import LLMConnectionError, LLMRateLimitError, LLMTimeoutError
from app.service.core.retry import LLM_RETRY_CONFIG, RetryConfig, retry_with_backoff
from app.service.llm.factory import LLMFactory
from app.service.llm.protocols.registry import canonical_endpoint_protocol, canonical_tool_message_format
from app.service.llm.types import DEFAULT_MODELS, LLMConfig, LLMMessage, LLMProvider, LLMRequest


class LLMService:
    """LLM service with per-agent model override, chat completion, and code analysis helpers."""

    _provider_semaphores: Dict[str, asyncio.Semaphore] = {}
    _provider_semaphore_limits: Dict[str, int] = {}
    _provider_gap_locks: Dict[str, asyncio.Lock] = {}
    _provider_last_request_at: Dict[str, float] = {}

    def __init__(self, user_config: Optional[Dict[str, Any]] = None):
        self._config: Optional[LLMConfig] = None
        self._user_config = user_config or {}


    async def chat_completion(
            self,
            messages: List[Dict[str, str]],
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            agent_type: Optional[str] = None,
            tools: Optional[List[Dict[str, Any]]] = None,
            parallel_tool_calls: Optional[bool] = None,
    ) -> Dict[str, Any]:
        config = self.get_agent_config(agent_type)
        adapter = LLMFactory.create_adapter(config)
        request = LLMRequest(
            messages=[LLMMessage.from_dict(item) for item in messages],
            temperature=temperature if temperature is not None else config.temperature,
            max_tokens=max_tokens,
            top_p=config.top_p,
            tools=tools,
            parallel_tool_calls=parallel_tool_calls,
            stream=False,
        )
        response = await self._execute_chat_completion(adapter, request, config)
        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        return {
            "content": response.content,
            "model": response.model or config.model,
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "finish_reason": response.finish_reason,
            "tool_calls": getattr(response, "tool_calls", None) or [],
            "reasoning_content": getattr(response, "reasoning_content", None) or "",
            "tools_ignored": False,
        }


    async def _execute_chat_completion(self, adapter: Any, request: LLMRequest, config: LLMConfig) -> Any:
        semaphore = self._get_provider_semaphore(config)
        retry_config = RetryConfig(
            max_attempts=LLM_RETRY_CONFIG.max_attempts,
            base_delay=LLM_RETRY_CONFIG.base_delay,
            max_delay=LLM_RETRY_CONFIG.max_delay,
            exponential_base=LLM_RETRY_CONFIG.exponential_base,
            jitter=LLM_RETRY_CONFIG.jitter,
            jitter_factor=LLM_RETRY_CONFIG.jitter_factor,
            backoff_strategy=LLM_RETRY_CONFIG.backoff_strategy,
            retryable_exceptions=LLM_RETRY_CONFIG.retryable_exceptions,
        )

        async def attempt() -> Any:
            async with semaphore:
                await self._await_provider_gap(config)
                try:
                    return await adapter.complete(request)
                except Exception as exc:  # noqa: BLE001
                    raise self._normalize_retryable_llm_error(exc) from exc

        return await retry_with_backoff(
            attempt,
            config=retry_config,
            operation_name=f"{config.provider.value} chat completion",
        )


    async def chat_completion_stream(
            self,
            messages: List[Dict[str, str]],
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            agent_type: Optional[str] = None,
            tools: Optional[List[Dict[str, Any]]] = None,
            parallel_tool_calls: Optional[bool] = None,
            retry_enabled: bool = True,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        config = self.get_agent_config(agent_type)
        adapter = LLMFactory.create_adapter(config)
        request = LLMRequest(
            messages=[LLMMessage.from_dict(item) for item in messages],
            temperature=temperature if temperature is not None else config.temperature,
            max_tokens=max_tokens,
            top_p=config.top_p,
            tools=tools,
            parallel_tool_calls=parallel_tool_calls,
            stream=True,
        )
        stream_complete = getattr(adapter, "stream_complete", None)
        if callable(stream_complete):
            async for event in self._execute_chat_completion_stream(
                    adapter,
                    request,
                    config,
                    retry_enabled=retry_enabled,
            ):
                yield event
            return

        result = await self.chat_completion(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            agent_type=agent_type,
            tools=tools,
            parallel_tool_calls=parallel_tool_calls,
        )
        content = result.get("content", "") or ""
        accumulated = ""
        chunk_size = 24
        for index in range(0, len(content), chunk_size):
            token = content[index:index + chunk_size]
            accumulated += token
            yield {"type": "token", "content": token, "accumulated": accumulated}
        for tool_call in result.get("tool_calls") or []:
            yield {"type": "tool_call", "tool_call": tool_call}
        yield {
            "type": "done",
            "content": content,
            "usage": result.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "tool_calls": result.get("tool_calls") or [],
            "reasoning_content": result.get("reasoning_content") or "",
            "finish_reason": result.get("finish_reason") or "stop",
        }


    async def _execute_chat_completion_stream(
            self,
            adapter: Any,
            request: LLMRequest,
            config: LLMConfig,
            *,
            retry_enabled: bool = True,
    ):
        semaphore = self._get_provider_semaphore(config)
        retry_config = RetryConfig(
            max_attempts=LLM_RETRY_CONFIG.max_attempts if retry_enabled else 1,
            base_delay=LLM_RETRY_CONFIG.base_delay,
            max_delay=LLM_RETRY_CONFIG.max_delay,
            exponential_base=LLM_RETRY_CONFIG.exponential_base,
            jitter=LLM_RETRY_CONFIG.jitter,
            jitter_factor=LLM_RETRY_CONFIG.jitter_factor,
            backoff_strategy=LLM_RETRY_CONFIG.backoff_strategy,
            retryable_exceptions=LLM_RETRY_CONFIG.retryable_exceptions,
        )

        attempt = 0
        while True:
            emitted_any_output = False
            retry_decision: tuple[Exception, float] | None = None

            async with semaphore:
                await self._await_provider_gap(config)
                try:
                    async for event in adapter.stream_complete(request):
                        event_type = str((event or {}).get("type") or "").strip().lower()
                        if event_type in {"token", "reasoning_delta", "tool_call", "done"}:
                            emitted_any_output = True

                        if event_type == "error":
                            normalized_error = self._normalize_stream_error_event(event)
                            has_partial_output = (
                                    emitted_any_output
                                    or bool((event or {}).get("accumulated"))
                                    or bool((event or {}).get("tool_calls"))
                            )
                            if (
                                    not has_partial_output
                                    and attempt < retry_config.max_attempts - 1
                                    and retry_config.should_retry(normalized_error)
                            ):
                                retry_decision = (
                                    normalized_error,
                                    retry_config.calculate_delay(attempt, normalized_error),
                                )
                                break
                            yield self._build_terminal_stream_error_event(
                                normalized_error,
                                base_event=event,
                                max_attempts=retry_config.max_attempts,
                                attempts_used=attempt + 1,
                            )
                            return

                        yield event
                        if event_type == "done":
                            return
                except Exception as exc:  # noqa: BLE001
                    normalized_error = self._normalize_retryable_llm_error(exc)
                    if (
                            not emitted_any_output
                            and attempt < retry_config.max_attempts - 1
                            and retry_config.should_retry(normalized_error)
                    ):
                        retry_decision = (
                            normalized_error,
                            retry_config.calculate_delay(attempt, normalized_error),
                        )
                    else:
                        yield self._build_terminal_stream_error_event(
                            normalized_error,
                            base_event=None,
                            max_attempts=retry_config.max_attempts,
                            attempts_used=attempt + 1,
                        )
                        return

            if retry_decision is None:
                return

            attempt += 1
            error, delay = retry_decision
            yield self._build_llm_retry_event(
                error=error,
                attempt=attempt,
                max_attempts=retry_config.max_attempts,
            )
            await asyncio.sleep(delay)



    def get_agent_config(self, agent_type: Optional[str] = None) -> LLMConfig:
        user_llm_config = self._resolve_llm_payload(agent_type)
        provider = self._parse_provider(user_llm_config.get("llmProvider") or "openai")
        runtime_env = self._get_runtime_env(user_llm_config)
        env_candidates = self._provider_env_candidates(provider)
        api_key = (
                user_llm_config.get("llmApiKey")
                or self._get_provider_api_key_from_user_config(provider, user_llm_config)
                or self._first_env_value(runtime_env, env_candidates["api_key"])
                or self._get_provider_api_key(provider)
        )
        model = (
                user_llm_config.get("llmModel")
                or self._first_env_value(runtime_env, env_candidates["model"])
                or DEFAULT_MODELS.get(provider, "deepseek-v4-pro")
        )
        base_url = (
                user_llm_config.get("llmBaseUrl")
                or self._first_env_value(runtime_env, env_candidates["base_url"])
                or self._get_provider_base_url(provider)
        )
        timeout_ms = user_llm_config.get("llmTimeout")
        if timeout_ms in (None, ""):
            timeout_ms = self._first_env_value(runtime_env, env_candidates["timeout_ms"])
            try:
                timeout_ms = int(timeout_ms) if timeout_ms not in (None, "") else None
            except (TypeError, ValueError):
                timeout_ms = None
        timeout = int(timeout_ms / 1000) if timeout_ms and timeout_ms > 1000 else int(timeout_ms or 300)
        temperature = user_llm_config.get("llmTemperature")
        top_p = user_llm_config.get("llmTopP")
        max_tokens = int(user_llm_config.get("llmMaxTokens") or 4096)
        endpoint_protocol = canonical_endpoint_protocol("auto")
        tool_message_format = canonical_tool_message_format(
            "openai_chat"
        )
        return LLMConfig(
            provider=provider,
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            endpoint_protocol=endpoint_protocol,
            tool_message_format=tool_message_format,
        )


    def _resolve_llm_payload(self, agent_type: Optional[str] = None) -> Dict[str, Any]:
        """解析 LLM 配置, 支持按 Agent 类型覆盖配置。类似配置更高级别的模型在更加重要的Agent上, 类如评估Agent"""
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
                    user_llm_config["env"] = {**base_env, **override_env} # 合并环境变量
        return user_llm_config


    def _parse_provider(self, provider_str: str) -> LLMProvider:
        """从字典中获取模型的提供商"""
        provider_map = {
            "gemini": LLMProvider.GEMINI,
            "openai": LLMProvider.OPENAI,
            "claude": LLMProvider.CLAUDE,
            "qwen": LLMProvider.QWEN,
            "deepseek": LLMProvider.DEEPSEEK,
            "zhipu": LLMProvider.ZHIPU,
        }
        return provider_map.get((provider_str or "").lower(), LLMProvider.OPENAI)


    def _get_runtime_env(self, llm_payload: Dict[str, Any]) -> Dict[str, str]:
        """从配置中提取环境变量字典，并过滤掉空值，确保返回的都是有效的字符串键值对。"""
        env_payload = llm_payload.get("env")
        if not isinstance(env_payload, dict):
            return {}
        return {
            str(key): str(value)
            for key, value in env_payload.items()
            if value not in (None, "")
        }


    def _provider_env_candidates(self, provider: LLMProvider) -> Dict[str, List[str]]:
        """获取提供商信息"""
        prefix_map = {
            LLMProvider.CLAUDE: "ANTHROPIC",
            LLMProvider.OPENAI: "OPENAI",
            LLMProvider.GEMINI: "GEMINI",
            LLMProvider.QWEN: "QWEN",
            LLMProvider.DEEPSEEK: "DEEPSEEK",
            LLMProvider.ZHIPU: "ZHIPU",
        }
        prefix = prefix_map.get(provider, "LLM")
        return {
            "api_key": [f"{prefix}_AUTH_TOKEN", f"{prefix}_API_KEY", "LLM_API_KEY"],
            "base_url": [f"{prefix}_BASE_URL", "LLM_BASE_URL"],
            "model": [f"{prefix}_MODEL", "LLM_MODEL"],
            "timeout_ms": ["API_TIMEOUT_MS", "LLM_TIMEOUT_MS"],
        }


    def _get_provider_api_key_from_user_config(self, provider: LLMProvider, user_llm_config: Dict[str, Any]) -> Optional[str]:
        """获取API KEY"""
        provider_key_map = {
            LLMProvider.OPENAI: "openaiApiKey",
            LLMProvider.GEMINI: "geminiApiKey",
            LLMProvider.CLAUDE: "claudeApiKey",
            LLMProvider.QWEN: "qwenApiKey",
            LLMProvider.DEEPSEEK: "deepseekApiKey",
            LLMProvider.ZHIPU: "zhipuApiKey",
        }
        key_name = provider_key_map.get(provider)
        return user_llm_config.get(key_name) if key_name else None

    def _get_provider_api_key(self, provider: LLMProvider) -> str:
        """获取API KEY, 从环境变量中推测"""
        provider_key_map = {
            LLMProvider.OPENAI: "OPENAI_API_KEY",
            LLMProvider.GEMINI: "GEMINI_API_KEY",
            LLMProvider.CLAUDE: "CLAUDE_API_KEY",
            LLMProvider.QWEN: "QWEN_API_KEY",
            LLMProvider.DEEPSEEK: "DEEPSEEK_API_KEY",
            LLMProvider.ZHIPU: "ZHIPU_API_KEY",
        }
        key_name = provider_key_map.get(provider)
        if key_name:
            return os.getenv(key_name, "")
        return ""


    def _first_env_value(self, env_payload: Dict[str, str], keys: List[str]) -> Optional[str]:
        """获取环境变量"""
        for key in keys:
            value = env_payload.get(key)
            if value not in (None, ""):
                return value
        return None

    def _get_provider_base_url(self, provider: LLMProvider) -> Optional[str]:
        if provider in {
            LLMProvider.OPENAI,
            LLMProvider.QWEN,
            LLMProvider.DEEPSEEK,
            LLMProvider.ZHIPU,

        }:
            from .types import DEFAULT_BASE_URLS

            return DEFAULT_BASE_URLS.get(provider)
        return None


    def _get_provider_semaphore(self, config: LLMConfig) -> asyncio.Semaphore:
        """获取或创建一个信号量（Semaphore），用于控制同一 LLM 提供商/账号的并发请求数，防止超过 API 限流阈值"""
        key = self._build_provider_limit_key(config)
        desired_limit = 3
        semaphore = self._provider_semaphores.get(key)
        if semaphore is None or self._provider_semaphore_limits.get(key) != desired_limit:
            semaphore = asyncio.Semaphore(desired_limit)
            self._provider_semaphores[key] = semaphore
            self._provider_semaphore_limits[key] = desired_limit
        return semaphore


    def _build_provider_limit_key(self, config: LLMConfig) -> str:
        """示例键：openai|https://api.openai.com/v1|a1b2c3d4e5f6"""
        return "|".join(
            [
                config.provider.value,
                config.base_url or "",
                hashlib.sha1((config.api_key or "").encode("utf-8")).hexdigest()[:12],
                ]
        )

    async def _await_provider_gap(self, config: LLMConfig) -> None:
        gap_ms = 0  # Todo
        if gap_ms <= 0:
            return

        key = self._build_provider_limit_key(config)
        lock = self._get_provider_gap_lock(config)
        async with lock:  #  获取锁
            now = asyncio.get_running_loop().time()
            last_started = self._provider_last_request_at.get(key) #  上次请求时间
            if last_started is not None:
                wait_seconds = (gap_ms / 1000.0) - (now - last_started)
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds) #  等待
                    now = asyncio.get_running_loop().time() #  更新当前时间
            self._provider_last_request_at[key] = now #  记录本次请求时间, 入缓存


    def _get_provider_gap_lock(self, config: LLMConfig) -> asyncio.Lock:
        key = self._build_provider_limit_key(config)
        lock = self._provider_gap_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._provider_gap_locks[key] = lock
        return lock


    def _normalize_retryable_llm_error(self, error: Exception) -> Exception:
        """将各种异常归一化为标准的 LLM 异常类型，便于后续的重试决策和错误处理。"""
        if isinstance(error, (LLMRateLimitError, LLMTimeoutError, LLMConnectionError)):
            return error

        status_code = getattr(error, "status_code", None)
        message = str(error or "")
        lowered = message.lower()

        if status_code == 429 or any(token in lowered for token in ("rate limit", "too many requests", "频率超限", "限流")):
            return LLMRateLimitError(message, retry_after=15, cause=error)
        if "timeout" in lowered or "timed out" in lowered:
            return LLMTimeoutError(message, cause=error)
        if status_code == 503 or any(
                token in lowered
                for token in (
                        "connection",
                        "connect",
                        "network",
                        "dns",
                        "temporarily unavailable",
                        "service unavailable",
                        "server disconnected",
                        "no available accounts",
                        "unavailable account",
                )
        ):
            return LLMConnectionError(message, cause=error)
        return error


    def _normalize_stream_error_event(self, event: Dict[str, Any]) -> Exception:
        error_type = str(event.get("error_type") or "").strip().lower()
        error_message = str(event.get("error") or event.get("user_message") or "LLM streaming request failed").strip()

        if error_type == "rate_limit":
            return LLMRateLimitError(error_message, retry_after=15)
        if error_type == "connection":
            return LLMConnectionError(error_message)
        if error_type == "quota_exceeded":
            return Exception(error_message)
        lowered = error_message.lower()
        non_retryable_tokens = (
            "authentication",
            "api key",
            "invalid api key",
            "quota",
            "billing",
            "insufficient",
            "context length",
            "maximum context",
            "invalid_request",
            "invalid request",
            "tool schema",
            "schema",
        )
        if any(token in lowered for token in non_retryable_tokens):
            return Exception(error_message)
        generic_unknown_messages = {
            "",
            "llm streaming request failed",
            "llm streaming request failed. please retry.",
        }
        if error_type in {"", "unknown"} and lowered in generic_unknown_messages:
            return LLMConnectionError(error_message or "LLM streaming request failed. Please retry.")
        return self._normalize_retryable_llm_error(Exception(error_message))


    @classmethod
    def _build_terminal_stream_error_event(
            cls,
            error: Exception,
            *,
            base_event: Dict[str, Any] | None,
            max_attempts: int,
            attempts_used: int,
    ) -> Dict[str, Any]:
        payload = dict(base_event or {})
        error_type, _ = cls._describe_stream_error(error)
        if isinstance(error, (LLMConnectionError, LLMTimeoutError, LLMRateLimitError)) and attempts_used >= max_attempts:
            user_message = f"模型服务连接失败，已自动重试 {max_attempts} 次仍未恢复。请稍后重试或切换可用账号。"
        else:
            user_message = str(payload.get("user_message") or str(error) or "LLM streaming request failed").strip()
        return {
            **payload,
            "type": "error",
            "error_type": str(payload.get("error_type") or error_type or "unknown"),
            "error": str(payload.get("error") or str(error)),
            "user_message": user_message,
        }


    @staticmethod
    def _describe_stream_error(error: Exception) -> tuple[str, str]:
        if isinstance(error, LLMRateLimitError):
            return "rate_limit", "模型服务当前请求过多，"
        if isinstance(error, LLMTimeoutError):
            return "timeout", "模型响应超时，"
        if isinstance(error, LLMConnectionError):
            return "connection", "上游模型账号或连接暂时不可用，"
        return "unknown", "模型服务暂时不可用，"


    @classmethod
    def _build_llm_retry_event(cls, *, error: Exception, attempt: int, max_attempts: int) -> Dict[str, Any]:
        error_type, prefix = cls._describe_stream_error(error)
        return {
            "type": "llm_retry",
            "attempt": attempt,
            "max_attempts": max_attempts,
            "error_type": error_type,
            "message_text": f"{prefix}正在进行第 {attempt}/{max_attempts} 次自动重试……",
            "error": str(error),
        }