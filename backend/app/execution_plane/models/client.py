"""统一模型出口：唯一 `litellm.acompletion` 调用点（P01/P04/P05）。

本模块只做三件事：把不可变快照变成 SDK 调用参数、消费 SDK 响应/流、把 SDK
异常映射到既有恢复类别。厂商协议分派、HTTP/SSE 手写、响应解析都不在这里出现。

约束（Spec 20P0）：
- 不在请求期间修改 `litellm` 全局（cache / drop_params / model / api_base / key）；
- SDK 自动重试按快照预算显式设置，禁止与业务层倍增；
- 流事件终态 `done` 至多一次，出错后不再产出 `done`。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

import litellm

from .config import (
    RETRY_OWNER_SDK,
    LLMConfig,
    LLMRequest,
    ModelConfigurationError,
    with_request_scope,
)
from .errors import (
    ModelAuthenticationError,
    ModelBadRequestError,
    ModelConnectionError,
    ModelQuotaExceededError,
    ModelRateLimitError,
    ModelResponseError,
    ModelStreamTimeoutError,
    ModelTimeoutError,
)
from .types import LLMResponse
from .usage import normalize_usage

logger = logging.getLogger(__name__)

STREAM_EVENT_TOKEN = "token"
STREAM_EVENT_REASONING = "reasoning_delta"
STREAM_EVENT_TOOL_CALL = "tool_call"
STREAM_EVENT_DONE = "done"
STREAM_EVENT_ERROR = "error"

# 结束块之后等待 usage-only 尾块的最长时间（秒）。LiteLLM 1.98 会把合并后的 usage
# 作为结束块之后的一个独立 chunk 交付，因此必须继续读一小段。
TAIL_METADATA_TIMEOUT_SECONDS = 2.0

# SDK 侧不参与业务恢复的异常：认证/参数/配额错误一律不重试。
NON_RETRYABLE_SDK_ERRORS = (
    ModelAuthenticationError,
    ModelBadRequestError,
    ModelQuotaExceededError,
)


@dataclass
class _ToolCallState:
    index: int
    id: str = ""
    type: str = "function"
    name: str = ""
    arguments: str = ""

    def to_payload(self) -> Dict[str, Any]:
        return {
            "id": self.id or f"tool_call_{self.index}",
            "type": self.type or "function",
            "name": self.name,
            "arguments": self.arguments or "{}",
        }


@dataclass
class _StreamState:
    content: str = ""
    reasoning_content: str = ""
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    raw_usage_seen: bool = False
    usage_synthetic_zero: bool = False
    tool_calls: Dict[int, _ToolCallState] = field(default_factory=dict)
    emitted_tool_calls: bool = False
    dropped_incomplete_tool_calls: List[str] = field(default_factory=list)
    done_emitted: bool = False
    chunk_count: int = 0

    def has_partial_output(self) -> bool:
        """本轮是否已经产出可观察的部分输出（含仅到达 delta 的工具参数）。"""

        return bool(self.content or self.reasoning_content or self.tool_calls)


def _as_dictish(value: Any, field_name: Optional[str] = None, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(field_name, default) if field_name else value
    if field_name is None:
        return value
    direct = getattr(value, field_name, None)
    if direct is not None:
        return direct
    # pydantic 模型可能把未声明字段放进 model_extra / __dict__（如流式 usage）。
    try:
        extra = value.model_extra
    except Exception:  # noqa: BLE001
        extra = None
    if isinstance(extra, dict) and extra.get(field_name) is not None:
        return extra.get(field_name)
    for extra_name in ("__dict__", "additional_kwargs", "provider_specific_fields"):
        other = getattr(value, extra_name, None)
        if isinstance(other, dict) and other.get(field_name) is not None:
            return other.get(field_name)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            dumped = dump()
        except Exception:
            dumped = None
        if isinstance(dumped, dict) and dumped.get(field_name) is not None:
            return dumped.get(field_name)
    return default


def _usage_to_mapping(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            dumped = dump()
        except Exception:
            return None
        if isinstance(dumped, dict):
            return dumped
    legacy = getattr(value, "dict", None)
    if callable(legacy):
        try:
            dumped = legacy()
        except Exception:
            return None
        if isinstance(dumped, dict):
            return dumped
    return None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content)


def _response_model_name(response: Any) -> Optional[str]:
    raw = _as_dictish(response, "model")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _provider_request_id(response: Any) -> Optional[str]:
    raw = _as_dictish(response, "id")
    if raw is None:
        hidden = getattr(response, "_hidden_params", None) or {}
        raw = hidden.get("litellm_call_id") if isinstance(hidden, dict) else None
    text = str(raw or "").strip()
    return text or None


def _sdk_response_cost(response: Any) -> Optional[float]:
    hidden = getattr(response, "_hidden_params", None)
    if not isinstance(hidden, dict):
        return None
    value = hidden.get("response_cost")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _classify_sdk_exception(exc: BaseException) -> BaseException:
    """把 SDK 异常映射到既有恢复类别；不做字符串猜测式退避解析。"""

    if isinstance(exc, asyncio.CancelledError):
        return exc
    if isinstance(
        exc,
        (
            ModelAuthenticationError,
            ModelBadRequestError,
            ModelQuotaExceededError,
            ModelConnectionError,
            ModelRateLimitError,
            ModelTimeoutError,
        ),
    ):
        return exc

    status_code = getattr(exc, "status_code", None)
    try:
        status_code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status_code = None
    message = str(exc) or exc.__class__.__name__

    if isinstance(exc, litellm.exceptions.AuthenticationError) or status_code in (401, 403):
        return ModelAuthenticationError(message, status_code=status_code, cause=exc)
    if isinstance(exc, litellm.exceptions.NotFoundError) or status_code == 404:
        return ModelBadRequestError(message, status_code=status_code, cause=exc)
    if isinstance(exc, litellm.exceptions.ContextWindowExceededError):
        return ModelBadRequestError(message, status_code=status_code, cause=exc)
    if isinstance(exc, litellm.exceptions.RateLimitError) or status_code == 429:
        quota_markers = ("insufficient", "quota", "balance", "billing", "余额", "配额")
        if any(marker in message.lower() for marker in quota_markers):
            return ModelQuotaExceededError(message, status_code=status_code, cause=exc)
        return ModelRateLimitError(message, status_code=status_code, cause=exc)
    if isinstance(exc, litellm.exceptions.Timeout) or status_code in (408, 504):
        return ModelTimeoutError(message, status_code=status_code, cause=exc)
    if isinstance(exc, litellm.exceptions.APIConnectionError) or status_code in (500, 502, 503, 529):
        return ModelConnectionError(message, status_code=status_code, cause=exc)
    if isinstance(exc, litellm.exceptions.BadRequestError) or (
        status_code is not None and 400 <= status_code < 500
    ):
        return ModelBadRequestError(message, status_code=status_code, cause=exc)
    if isinstance(
        exc,
        (
            litellm.exceptions.APIError,
            litellm.exceptions.ServiceUnavailableError,
            litellm.exceptions.InternalServerError,
        ),
    ):
        return ModelConnectionError(message, status_code=status_code, cause=exc)
    return ModelResponseError(message, status_code=status_code, cause=exc)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.CancelledError, ModelConfigurationError)):
        return False
    if isinstance(exc, (ModelConnectionError, ModelTimeoutError, ModelRateLimitError)):
        return True
    return False


class SDKModelClient:
    """唯一 SDK 客户端。所有模型请求都必须经过 `complete` / `stream`。"""

    def __init__(
        self,
        *,
        first_token_timeout: Optional[float] = None,
        stream_timeout: Optional[float] = None,
        attempt_gap_seconds: float = 0.5,
    ):
        self._first_token_timeout = first_token_timeout
        self._stream_timeout = stream_timeout
        self._attempt_gap_seconds = attempt_gap_seconds

    @property
    def attempt_gap_seconds(self) -> float:
        # 验收环境可显式把重试间隔压到最小，不改变重试次数与所有者语义。
        override = os.environ.get("CODESAGE_MODEL_ATTEMPT_GAP_SECONDS")
        if override:
            try:
                return max(0.0, float(override))
            except ValueError:
                return self._attempt_gap_seconds
        return self._attempt_gap_seconds

    @staticmethod
    def _build_kwargs(config: LLMConfig, request: LLMRequest) -> Dict[str, Any]:
        messages = [dict(message) for message in request.messages]
        kwargs: Dict[str, Any] = {
            "model": config.sdk_model,
            "messages": messages,
            "num_retries": 0,
        }
        max_tokens = request.max_tokens if request.max_tokens is not None else config.max_tokens
        if max_tokens:
            kwargs["max_tokens"] = int(max_tokens)

        temperature = request.temperature if request.temperature is not None else config.temperature
        if temperature is not None:
            kwargs["temperature"] = float(temperature)
        top_p = request.top_p if request.top_p is not None else config.top_p
        if top_p is not None:
            kwargs["top_p"] = float(top_p)
        if config.frequency_penalty:
            kwargs["frequency_penalty"] = float(config.frequency_penalty)
        if config.presence_penalty:
            kwargs["presence_penalty"] = float(config.presence_penalty)

        if request.tools:
            kwargs["tools"] = request.tools
            if request.parallel_tool_calls is not None:
                kwargs["parallel_tool_calls"] = bool(request.parallel_tool_calls)
        if request.tool_choice is not None:
            kwargs["tool_choice"] = request.tool_choice

        if config.api_key:
            kwargs["api_key"] = config.api_key
        if config.base_url:
            kwargs["api_base"] = config.base_url
        if config.timeout:
            kwargs["timeout"] = float(config.timeout)
        if config.custom_headers:
            kwargs["extra_headers"] = dict(config.custom_headers)

        kwargs["metadata"] = {
            "codesage_purpose": config.purpose,
            "codesage_provider": config.provider.value,
            "codesage_transport": config.transport,
            "codesage_model_boundary_version": config.model_boundary_version,
        }
        return kwargs

    async def _send(self, config: LLMConfig, request: LLMRequest, *, stream: bool) -> Any:
        """唯一 SDK 发送点；请求期间不得修改任何 litellm 全局配置。"""

        kwargs = self._build_kwargs(config, request)
        if stream:
            kwargs["stream"] = True
            if config.stream_options_include_usage:
                kwargs["stream_options"] = {"include_usage": True}
        return await litellm.acompletion(**kwargs)

    async def complete(
        self,
        *,
        config: LLMConfig,
        request: LLMRequest,
        first_token_timeout: Optional[float] = None,
    ) -> LLMResponse:
        scoped = with_request_scope(config, request)
        budget = max(1, int(scoped.retry_budget))
        last_error: Optional[BaseException] = None

        for attempt in range(1, budget + 1):
            try:
                response = await self._send(scoped, request, stream=False)
                return self._to_llm_response(scoped, request, response)
            except BaseException as exc:  # noqa: BLE001 - 统一映射后重抛
                mapped = _classify_sdk_exception(exc)
                if isinstance(mapped, asyncio.CancelledError):
                    raise
                last_error = mapped
                if attempt >= budget or not is_retryable(mapped):
                    raise mapped
                logger.warning(
                    "model request attempt %s/%s failed (%s); retrying",
                    attempt,
                    budget,
                    mapped.__class__.__name__,
                )
                await asyncio.sleep(self._retry_delay(attempt))

        raise last_error if last_error is not None else ModelResponseError("模型请求未执行")

    def _retry_delay(self, attempt: int) -> float:
        return min(8.0, self.attempt_gap_seconds * (2 ** (attempt - 1)))

    @staticmethod
    def _to_llm_response(config: LLMConfig, request: LLMRequest, response: Any) -> LLMResponse:
        choices = _as_dictish(response, "choices", []) or []
        if not choices:
            raise ModelResponseError("模型响应缺少 choices 字段")
        choice = choices[0]
        message = _as_dictish(choice, "message", None)
        if message is None:
            raise ModelResponseError("模型响应缺少 message 字段")

        raw_usage = _usage_to_mapping(_as_dictish(response, "usage"))
        usage = normalize_usage(
            raw_usage,
            provider=config.provider.value,
            protocol=config.endpoint_protocol,
            # LiteLLM 在网关未回传 usage 时会合成全零 usage，无法与厂商真实全零区分；
            # 统一按不可证实处理，避免把缺失 usage 记成免费请求。
            zero_fidelity="unverified",
        )

        tool_calls: List[Dict[str, Any]] = []
        for raw_tool_call in _as_dictish(message, "tool_calls", None) or []:
            function = _as_dictish(raw_tool_call, "function", None) or {}
            tool_calls.append(
                {
                    "id": str(_as_dictish(raw_tool_call, "id", "") or ""),
                    "type": str(_as_dictish(raw_tool_call, "type", "function") or "function"),
                    "name": str(_as_dictish(function, "name", "") or ""),
                    "arguments": _coerce_arguments(_as_dictish(function, "arguments", "")),
                }
            )

        return LLMResponse(
            content=_message_text(_as_dictish(message, "content")),
            model=_response_model_name(response),
            usage=usage,
            finish_reason=_as_dictish(choice, "finish_reason"),
            tool_calls=tool_calls or None,
            reasoning_content=_reasoning_text(message),
            configured_model=config.model,
            request_model=config.model,
            response_model=_response_model_name(response),
            provider=config.provider.value,
            endpoint_id=config.endpoint_id,
            protocol=config.endpoint_protocol,
            perspective=request.perspective,
            purpose=request.purpose or config.purpose,
            provider_request_id=_provider_request_id(response),
            response_cost_usd=_sdk_response_cost(response),
        )

    async def stream(
        self,
        *,
        config: LLMConfig,
        request: LLMRequest,
        first_token_timeout: Optional[float] = None,
        stream_timeout: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        scoped = with_request_scope(config, request)
        budget = max(1, int(scoped.retry_budget))
        attempt = 0

        while True:
            attempt += 1
            state = _StreamState()
            emitted_output = False
            retry_after: Optional[float] = None
            stream = None
            try:
                stream = await self._send(scoped, request, stream=True)
                effective_first_token = first_token_timeout or self._first_token_timeout
                effective_stream_timeout = stream_timeout or self._stream_timeout
                iterator = stream.__aiter__()
                expecting_first = True
                finish_seen = False
                try:
                    while True:
                        if finish_seen:
                            # 结束块之后可能还有 usage-only 尾块；只给它一个短期窗口，
                            # 避免被不守约的网关拖住。
                            timeout_value: Optional[float] = TAIL_METADATA_TIMEOUT_SECONDS
                        elif expecting_first:
                            timeout_value = effective_first_token
                        else:
                            timeout_value = effective_stream_timeout
                        try:
                            if timeout_value:
                                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=float(timeout_value))
                            else:
                                chunk = await iterator.__anext__()
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError as exc:
                            if finish_seen:
                                break
                            raise ModelStreamTimeoutError(
                                f"模型流{'首个事件' if expecting_first else '事件间隔'}超过 {timeout_value}s 未到达"
                            ) from exc
                        expecting_first = False
                        state.chunk_count += 1

                        for event in self._consume_chunk(state, chunk):
                            emitted_output = True
                            yield event

                        if state.finish_reason is not None:
                            finish_seen = True
                finally:
                    await _aclose_stream(stream)
            except BaseException as exc:  # noqa: BLE001 - 统一映射后重抛
                mapped = _classify_sdk_exception(exc)
                if isinstance(mapped, asyncio.CancelledError):
                    raise
                # 只有「本轮还没交付任何内容」才允许透明重试；一旦已交付 token/工具事件，
                # 重试就可能把两次响应拼成同一个成功响应。
                if attempt < budget and not emitted_output and not state.has_partial_output() and is_retryable(mapped):
                    retry_after = self._retry_delay(attempt)
                    yield {
                        "type": "llm_retry",
                        "attempt": attempt,
                        "max_attempts": budget,
                        "error_type": describe_error_kind(mapped),
                        "error": str(mapped),
                        "message_text": (
                            f"模型服务{_error_prefix(mapped)}正在进行第 {attempt}/{budget} 次自动重试……"
                        ),
                    }
                else:
                    yield self._error_event(
                        mapped,
                        state=state,
                        max_attempts=budget,
                        attempts_used=attempt,
                        config=scoped,
                        request=request,
                    )
                    return

            if retry_after is None:
                if state.finish_reason is None:
                    # 结束块缺失说明流被截断/网关未按协议收尾；LiteLLM 会把它伪装成正常结束，
                    # 必须在这里显式失败，否则半截响应会被当成成功响应（P04/P06）。
                    yield self._error_event(
                        ModelResponseError("模型流在未收到结束块前终止（可能被网关中断）"),
                        state=state,
                        max_attempts=budget,
                        attempts_used=attempt,
                        config=scoped,
                        request=request,
                        truncated=True,
                    )
                    return
                yield self._done_event(state, config=scoped, request=request)
                return

            await asyncio.sleep(retry_after)

    def _consume_chunk(self, state: _StreamState, chunk: Any) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []

        usage_value = _as_dictish(chunk, "usage")
        raw_usage = _usage_to_mapping(usage_value)
        if raw_usage:
            # SDK 可能把 usage 合并到带 choices 的末块，也可能单独发 usage-only 块；
            # 两种情况都只记录最后一次，绝不按 chunk 累加。
            state.usage = raw_usage
            state.raw_usage_seen = True

        if raw_usage and all(
            isinstance(value, int) and value == 0 for value in raw_usage.values() if value is not None
        ):
            state.usage_synthetic_zero = True

        choices = _as_dictish(chunk, "choices", None) or []
        if not choices:
            return events

        choice = choices[0]
        delta = _as_dictish(choice, "delta", None)
        if delta is None:
            delta = _as_dictish(choice, "message", None)

        if delta is not None:
            content = _message_text(_as_dictish(delta, "content"))
            if content:
                state.content += content
                events.append({"type": STREAM_EVENT_TOKEN, "content": content, "accumulated": state.content})

            reasoning = _reasoning_text(delta)
            if reasoning:
                state.reasoning_content += reasoning
                events.append(
                    {
                        "type": STREAM_EVENT_REASONING,
                        "content": reasoning,
                        "reasoning_content": reasoning,
                        "accumulated": state.reasoning_content,
                    }
                )

            self._merge_tool_call_deltas(state, _as_dictish(delta, "tool_calls", None) or [])

        finish_reason = _as_dictish(choice, "finish_reason")
        if finish_reason:
            state.finish_reason = str(finish_reason)
            events.extend(self._flush_tool_calls(state))
        return events

    @staticmethod
    def _merge_tool_call_deltas(state: _StreamState, raw_tool_calls: List[Any]) -> None:
        for raw in raw_tool_calls:
            index = _as_dictish(raw, "index", 0)
            try:
                index = int(index or 0)
            except (TypeError, ValueError):
                index = 0
            target = state.tool_calls.setdefault(index, _ToolCallState(index=index))
            call_id = _as_dictish(raw, "id")
            if call_id:
                target.id = str(call_id)
            call_type = _as_dictish(raw, "type")
            if call_type:
                target.type = str(call_type)
            function = _as_dictish(raw, "function", None) or {}
            name = _as_dictish(function, "name")
            if name:
                target.name = f"{target.name}{name}"
            arguments = _as_dictish(function, "arguments")
            if arguments:
                target.arguments = f"{target.arguments}{arguments}"

    @staticmethod
    def _flush_tool_calls(state: _StreamState) -> List[Dict[str, Any]]:
        """只在工具参数完整且 JSON 可解析时交付，避免执行半个 JSON（P04/P06）。

        分片参数必须等全部 chunk 到齐；流中断导致的半截 JSON 直接丢弃，
        由 Harness 依据 partial 记录做恢复决策，不进入 ToolGateway。
        """

        if state.emitted_tool_calls:
            return []
        state.emitted_tool_calls = True
        events: List[Dict[str, Any]] = []
        for index in sorted(state.tool_calls):
            entry = state.tool_calls[index]
            payload = entry.to_payload()
            if not payload["name"]:
                continue
            if not _is_complete_json(payload["arguments"]):
                state.dropped_incomplete_tool_calls.append(payload["name"])
                continue
            events.append({"type": STREAM_EVENT_TOOL_CALL, "tool_call": payload})
        return events

    def _done_event(self, state: _StreamState, *, config: LLMConfig, request: LLMRequest) -> Dict[str, Any]:
        state.done_emitted = True
        tool_calls = [
            state.tool_calls[index].to_payload()
            for index in sorted(state.tool_calls)
            if state.tool_calls[index].name
        ]
        usage = normalize_usage(
            state.usage,
            provider=config.provider.value,
            protocol=config.endpoint_protocol,
            # LiteLLM 在 wire 缺 usage 时会合成全零/估算 usage，统一按不可证实处理。
            zero_fidelity="unverified",
        )
        return {
            "type": STREAM_EVENT_DONE,
            "content": state.content,
            "reasoning_content": state.reasoning_content,
            "usage": usage.to_dict() if usage is not None else None,
            "usage_present": state.raw_usage_seen,
            "finish_reason": state.finish_reason or "complete",
            "tool_calls": tool_calls,
            "dropped_incomplete_tool_calls": list(state.dropped_incomplete_tool_calls),
            "chunk_count": state.chunk_count,
            "configured_model": config.model,
            "request_model": config.model,
            "response_model": None,
            "provider": config.provider.value,
            "endpoint_id": config.endpoint_id,
            "protocol": config.endpoint_protocol,
            "perspective": request.perspective,
            "purpose": request.purpose or config.purpose,
        }

    def _error_event(
        self,
        error: BaseException,
        *,
        state: _StreamState,
        max_attempts: int,
        attempts_used: int,
        config: LLMConfig,
        request: LLMRequest,
        truncated: bool = False,
    ) -> Dict[str, Any]:
        usage = normalize_usage(
            state.usage,
            provider=config.provider.value,
            protocol=config.endpoint_protocol,
            zero_fidelity="unverified",
        )
        partial = bool(state.content or state.reasoning_content or state.tool_calls)
        incomplete_tool_calls = list(state.dropped_incomplete_tool_calls) or [
            entry.name
            for _, entry in sorted(state.tool_calls.items())
            if entry.name and not _is_complete_json(entry.arguments)
        ]
        return {
            "type": STREAM_EVENT_ERROR,
            "error": str(error),
            "error_class": error.__class__.__name__,
            "error_type": describe_error_kind(error),
            "user_message": user_message_for_error(error, max_attempts=max_attempts, attempts_used=attempts_used),
            "accumulated": state.content,
            "partial": partial,
            "stream_truncated": truncated,
            "dropped_incomplete_tool_calls": incomplete_tool_calls,
            "usage": usage.to_dict() if usage is not None else None,
            "configured_model": config.model,
            "request_model": config.model,
            "response_model": None,
            "provider": config.provider.value,
            "endpoint_id": config.endpoint_id,
            "protocol": config.endpoint_protocol,
            "perspective": request.perspective,
            "purpose": request.purpose or config.purpose,
        }


async def _aclose_stream(stream: Any) -> None:
    if stream is None:
        return
    closer = getattr(stream, "aclose", None)
    if callable(closer):
        try:
            await closer()
        except Exception:  # noqa: BLE001 - 关闭失败不改变业务结论
            logger.debug("failed to close model stream", exc_info=True)


def _coerce_arguments(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "{}"
    return json.dumps(value, ensure_ascii=False)


def _is_complete_json(arguments: str) -> bool:
    if not arguments or not arguments.strip():
        return False
    try:
        json.loads(arguments)
    except (TypeError, ValueError):
        return False
    return True


def _reasoning_text(payload: Any) -> str:
    for field_name in ("reasoning_content", "reasoning"):
        value = _as_dictish(payload, field_name)
        if value:
            return str(value)
    return ""


def describe_error_kind(error: BaseException) -> str:
    if isinstance(error, ModelRateLimitError):
        return "rate_limit"
    if isinstance(error, ModelQuotaExceededError):
        return "quota_exceeded"
    if isinstance(error, ModelStreamTimeoutError):
        return "stream_timeout"
    if isinstance(error, ModelTimeoutError):
        return "timeout"
    if isinstance(error, ModelConnectionError):
        return "connection"
    if isinstance(error, ModelAuthenticationError):
        return "authentication"
    if isinstance(error, ModelBadRequestError):
        return "invalid_request"
    if isinstance(error, ModelConfigurationError):
        return "configuration"
    return "unknown"


def _error_prefix(error: BaseException) -> str:
    kind = describe_error_kind(error)
    if kind == "rate_limit":
        return "当前请求过多，"
    if kind in {"timeout", "stream_timeout"}:
        return "响应超时，"
    if kind == "connection":
        return "账号或连接暂时不可用，"
    return "暂时不可用，"


def user_message_for_error(error: BaseException, *, max_attempts: int, attempts_used: int) -> str:
    kind = describe_error_kind(error)
    if kind == "authentication":
        return "模型认证失败：请检查 API Key 与 endpoint 配置。"
    if kind == "invalid_request":
        return f"模型请求参数不被端点接受：{error}"
    if kind == "configuration":
        return str(error)
    if kind == "quota_exceeded":
        return "模型账户额度或余额不足，请充值或更换账号。"
    if kind in {"rate_limit", "timeout", "stream_timeout", "connection"} and attempts_used >= max_attempts:
        return f"模型服务连接失败，已自动重试 {max_attempts} 次仍未恢复。请稍后重试或切换可用账号。"
    return str(error) or "模型服务暂时不可用，请稍后重试。"


def ensure_sdk_retries_disabled() -> None:
    """启动期断言：SDK 不允许在请求中隐式重试。调用方每次显式传 num_retries。"""

    if getattr(litellm, "num_retries", 0):
        litellm.num_retries = 0


_client: Optional[SDKModelClient] = None


def get_sdk_client() -> SDKModelClient:
    global _client
    if _client is None:
        _client = SDKModelClient()
    return _client


def reset_sdk_client() -> None:
    global _client
    _client = None


def monotonic_ms() -> float:
    return time.monotonic() * 1000.0


def sdk_retry_owner_for(retry_owner: str) -> bool:
    return retry_owner == RETRY_OWNER_SDK
