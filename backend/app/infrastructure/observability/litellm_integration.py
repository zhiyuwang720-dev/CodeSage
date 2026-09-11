"""LiteLLM SDK 观测接入与成本边界

模型 span 由 LiteLLM 官方 OTel integration 产生，并复用 CodeSage 统一TracerProvider/exporter。

本模块只做两件补充：
1. 一个薄 CustomLogger，为每次调用补运行关联、purpose、内容引用、本地请求记录，并作为成本候选值的来源；
2. 严格价格输入与 SDK 成本校验：未知价格保持 `unknown`，SDK 的默认零不当免费。

禁止：新建 Trace 协议、提交业务事务、在 callback 中授予工具权限。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import litellm
from litellm.integrations.custom_logger import CustomLogger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger(__name__)

COST_STATUS_SDK_VERIFIED = "sdk_verified"
COST_STATUS_UNKNOWN = "unknown"
COST_STATUS_PARTIAL = "partial"
COST_STATUS_UNPRICED = "unpriced"

COST_COMPARISON_TOLERANCE = Decimal("0.0000005")


@dataclass(frozen=True)
class PriceEntry:
    """冻结价格输入：只有显式登记的 (provider, model) 才允许计算成本。"""

    provider: str
    model: str
    input_per_1k_usd: str
    output_per_1k_usd: str
    currency: str = "USD"
    valid_from: str = "1970-01-01"
    unit: str = "per_1k_tokens"
    source: str = "test-price"


@dataclass
class CostRecord:
    status: str
    value_usd: Optional[str] = None
    reason: Optional[str] = None
    source: Optional[str] = None
    price_hash: Optional[str] = None
    sdk_raw_value: Optional[float] = None


@dataclass
class CallbackRecord:
    """一次 SDK 回调的脱敏记录（证据用）。"""

    call_id: str
    event: str
    model: Optional[str]
    provider: Optional[str]
    purpose: Optional[str]
    run_id: Optional[str]
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    total_tokens: Optional[int]
    response_cost_usd: Optional[float]
    cost: Dict[str, Any] = field(default_factory=dict)
    timestamp: Optional[str] = None


class CallbackRecorder:
    """环形缓冲的本地请求记录；测试与诊断读取，不参与业务事务。"""

    def __init__(self, maxlen: int = 512):
        self._records: deque[CallbackRecord] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, record: CallbackRecord) -> None:
        with self._lock:
            self._records.append(record)

    def records(self) -> List[CallbackRecord]:
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def dump_jsonl(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for record in self.records():
                handle.write(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n")


class LiteLLMCallbackLogger(CustomLogger):
    """薄回调：补关联字段、写本地记录、采集成本候选值。

    不创建 span，不写业务状态。失败只记录日志，绝不影响模型调用结果。
    """

    def __init__(self, recorder: CallbackRecorder):
        self._recorder = recorder
        self._emitted: set[tuple] = set()
        self._lock = threading.Lock()

    @property
    def price_table(self) -> "PriceTable":
        """价格表是启动/验收期可替换的冻结输入；始终读取当前快照。"""

        return get_price_table()

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._record("success", kwargs, response_obj)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._record("failure", kwargs, response_obj)

    def log_success_event(self, kwargs, response_obj, start_time, end_time):  # pragma: no cover - 同步路径兜底
        self._record("success", kwargs, response_obj)

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):  # pragma: no cover - 同步路径兜底
        self._record("failure", kwargs, response_obj)

    def _record(self, event: str, kwargs: dict, response_obj: Any) -> None:
        try:
            payload = dict(kwargs or {})
            call_id = str(payload.get("litellm_call_id") or payload.get("litellm_logging_obj") or "")
            response_id = _extract(payload, "id") or _hidden(payload, "response_cost")
            dedupe_key = (event, call_id, str(response_id or ""))
            with self._lock:
                if dedupe_key in self._emitted and call_id:
                    return
                self._emitted.add(dedupe_key)

            usage = _usage_mapping(payload, response_obj)
            model = payload.get("model")
            metadata = _metadata(payload)
            cost = self.price_table.evaluate(model=model, usage=usage, response_cost=_hidden_cost(payload))
            self._recorder.append(
                CallbackRecord(
                    call_id=call_id,
                    event=event,
                    model=str(model) if model else None,
                    provider=str(payload.get("custom_llm_provider") or metadata.get("codesage_provider") or "") or None,
                    purpose=str(metadata.get("codesage_purpose") or "") or None,
                    run_id=str(metadata.get("codesage_run_id") or "") or None,
                    prompt_tokens=_int(usage, "prompt_tokens"),
                    completion_tokens=_int(usage, "completion_tokens"),
                    total_tokens=_int(usage, "total_tokens"),
                    response_cost_usd=cost.sdk_raw_value,
                    cost=asdict(cost),
                )
            )
        except Exception:  # noqa: BLE001 - 观测失败不影响业务
            logger.debug("litellm callback recording failed", exc_info=True)


class PriceTable:
    """冻结价格输入的严格选择器；不做模糊匹配，不复制厂商计费公式。"""

    def __init__(self, entries: Optional[List[PriceEntry]] = None):
        self._entries = list(entries or [])
        self._index = {(entry.provider.lower(), entry.model.lower()): entry for entry in self._entries}
        self._hash = _price_hash(self._entries)

    @property
    def price_hash(self) -> str:
        return self._hash

    def lookup(self, *, provider: Optional[str], model: str) -> Optional[PriceEntry]:
        normalized_model = str(model or "").strip()
        if not normalized_model:
            return None
        candidates = [normalized_model]
        if "/" in normalized_model:
            candidates.append(normalized_model.split("/", 1)[1])
        for candidate in candidates:
            if provider:
                entry = self._index.get((str(provider).lower(), candidate.lower()))
                if entry is not None:
                    return entry
            for (_, entry_model), entry in self._index.items():
                if entry_model == candidate.lower():
                    return entry
        return None

    def expected_cost(self, entry: PriceEntry, usage: Dict[str, Any]) -> Optional[Decimal]:
        prompt = _int(usage, "prompt_tokens")
        completion = _int(usage, "completion_tokens")
        if prompt is None or completion is None:
            return None
        return (
            Decimal(str(entry.input_per_1k_usd)) * Decimal(prompt) / Decimal(1000)
            + Decimal(str(entry.output_per_1k_usd)) * Decimal(completion) / Decimal(1000)
        )

    def evaluate(self, *, model: Optional[str], usage: Dict[str, Any], response_cost: Optional[float]) -> CostRecord:
        entry = self.lookup(provider=None, model=str(model or ""))
        if entry is None:
            return CostRecord(
                status=COST_STATUS_UNKNOWN,
                reason="model_not_in_frozen_price_table",
                source="frozen_price_table",
                price_hash=self._hash,
                sdk_raw_value=response_cost,
            )
        expected = self.expected_cost(entry, usage)
        if expected is None:
            return CostRecord(
                status=COST_STATUS_PARTIAL,
                reason="usage_incomplete_for_pricing",
                source=entry.source,
                price_hash=self._hash,
                sdk_raw_value=response_cost,
            )
        if response_cost is None:
            return CostRecord(
                status=COST_STATUS_PARTIAL,
                reason="sdk_response_cost_missing",
                source=entry.source,
                price_hash=self._hash,
                sdk_raw_value=None,
            )
        if response_cost == 0 and expected != 0:
            return CostRecord(
                status=COST_STATUS_UNPRICED,
                reason="sdk_default_zero_is_not_free",
                source=entry.source,
                price_hash=self._hash,
                sdk_raw_value=response_cost,
            )
        sdk_value = Decimal(str(response_cost))
        if abs(sdk_value - expected) <= COST_COMPARISON_TOLERANCE:
            return CostRecord(
                status=COST_STATUS_SDK_VERIFIED,
                value_usd=str(expected),
                source=entry.source,
                price_hash=self._hash,
                sdk_raw_value=response_cost,
            )
        return CostRecord(
            status=COST_STATUS_PARTIAL,
            reason="sdk_value_outside_tolerance",
            value_usd=str(expected),
            source=entry.source,
            price_hash=self._hash,
            sdk_raw_value=response_cost,
        )


def _price_hash(entries: List[PriceEntry]) -> str:
    import hashlib

    payload = json.dumps([asdict(entry) for entry in entries], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _extract(payload: dict, key: str) -> Any:
    return payload.get(key)


def _usage_mapping(payload: dict, response_obj: Any) -> Dict[str, Any]:
    """usage 可能在 kwargs，也可能只在响应对象上（流式/回调差异）。"""

    for candidate in (payload.get("usage"), _dump(response_obj).get("usage")):
        if isinstance(candidate, dict) and candidate:
            return candidate
    return {}


def _dump(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
        except Exception:  # noqa: BLE001
            return {}
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _hidden(payload: dict, key: str) -> Any:
    hidden = payload.get("hidden_params")
    if isinstance(hidden, dict):
        return hidden.get(key)
    return None


def _hidden_cost(payload: dict) -> Optional[float]:
    value = _hidden(payload, "response_cost")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metadata(payload: dict) -> Dict[str, Any]:
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    litellm_params = payload.get("litellm_params")
    if isinstance(litellm_params, dict):
        nested = litellm_params.get("metadata")
        if isinstance(nested, dict):
            return nested
    return {}


def _int(usage: Any, key: str) -> Optional[int]:
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


_recorder = CallbackRecorder()
_price_table = PriceTable()
_installed = False
_otel_handler: Any = None


def get_recorder() -> CallbackRecorder:
    return _recorder


def get_price_table() -> PriceTable:
    return _price_table


def configure_prices(entries: List[PriceEntry]) -> None:
    global _price_table
    _price_table = PriceTable(entries)


def build_sdk_metadata(
    *,
    purpose: str,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    provider: Optional[str] = None,
    transport: Optional[str] = None,
    model_boundary_version: Optional[str] = None,
) -> Dict[str, Any]:
    """业务层只传关联 metadata；观测细节由 callback 处理。"""

    metadata: Dict[str, Any] = {"codesage_purpose": purpose}
    for key, value in (
        ("codesage_run_id", run_id),
        ("codesage_session_id", session_id),
        ("codesage_provider", provider),
        ("codesage_transport", transport),
        ("codesage_model_boundary_version", model_boundary_version),
    ):
        if value:
            metadata[key] = value
    return metadata


def install_litellm_integration(*, capture_content: bool = False) -> Dict[str, Any]:
    """启动期一次性安装：官方 OTel integration + 薄 CustomLogger。

    必须在 `configure_observability` 之后调用，以便复用同一 TracerProvider。
    """

    global _installed, _otel_handler
    status: Dict[str, Any] = {
        "installed": False,
        "capture_content": bool(capture_content),
        "tracer_provider": None,
        "callback_logger": False,
    }
    if _installed:
        # 已安装时也必须保证本地 callback 仍然在册（reset/重建 provider 后可能被清掉）。
        _register_callback_logger()
        status.update(_installed=True, callback_logger=True)
        return status

    # LiteLLM 1.98 在「存在父 span」时默认不再新建模型 span，而是把 usage/成本写到父
    # span（Harness 的 provider.request）上。Spec 20P0 要求模型 span 由 SDK 产生且名称
    # 取自 SDK，因此在创建 handler 之前显式要求始终创建 litellm_request span。
    # 这是启动期一次性配置，不在请求期间改动；不识别该标志的版本回退默认行为。
    os.environ.setdefault("USE_OTEL_LITELLM_REQUEST_SPAN", "true")

    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        # 未启用观测：仅安装本地记录回调，不创建任何 provider。
        _register_callback_logger()
        _installed = True
        status.update(installed=True, callback_logger=True, tracer_provider="absent")
        return status

    try:
        from litellm.integrations.opentelemetry import OpenTelemetry, OpenTelemetryConfig

        config = OpenTelemetryConfig(
            exporter="none",
            enable_metrics=False,
            enable_events=False,
            capture_message_content="SPAN_AND_EVENT" if capture_content else "NO_CONTENT",
        )
        handler = OpenTelemetry(config=config, tracer_provider=provider)
        # SDK 路径不会走 proxy 的 service_callback；必须注册到 completion 生命周期回调桶。
        for bucket in (
            litellm.callbacks,
            litellm.success_callback,
            litellm.failure_callback,
            getattr(litellm, "_async_success_callback", None),
            getattr(litellm, "_async_failure_callback", None),
        ):
            if isinstance(bucket, list) and not any(isinstance(item, OpenTelemetry) for item in bucket):
                bucket.append(handler)
        if handler not in litellm.service_callback:
            litellm.service_callback.append(handler)
        _otel_handler = handler
        status.update(tracer_provider=type(provider).__name__)
    except Exception:  # noqa: BLE001 - 接入失败不能阻断启动，但必须可见
        logger.exception("failed to install LiteLLM OpenTelemetry integration; model spans will be missing")

    _register_callback_logger()
    if not capture_content:
        # 统一脱敏前置：SDK 原生 log/input/output 一律不落正文。
        litellm.turn_off_message_logging = True
    litellm.num_retries = 0
    _installed = True
    status.update(installed=True, callback_logger=True)
    return status


def _register_callback_logger() -> None:
    logger_instance = LiteLLMCallbackLogger(_recorder)
    buckets = [
        litellm.callbacks,
        litellm.success_callback,
        litellm.failure_callback,
        getattr(litellm, "_async_success_callback", None),
        getattr(litellm, "_async_failure_callback", None),
    ]
    for bucket in buckets:
        if not isinstance(bucket, list):
            continue
        if not any(isinstance(item, LiteLLMCallbackLogger) for item in bucket):
            bucket.append(logger_instance)


def reset_integration_state() -> None:
    """测试辅助：清理安装标记与全局 callback 注册。"""

    global _installed, _otel_handler
    for item in list(litellm.service_callback):
        if item is _otel_handler or item.__class__.__name__ == "OpenTelemetry":
            litellm.service_callback.remove(item)
    for bucket in (
        litellm.callbacks,
        litellm.success_callback,
        litellm.failure_callback,
        getattr(litellm, "_async_success_callback", None),
        getattr(litellm, "_async_failure_callback", None),
    ):
        if not isinstance(bucket, list):
            continue
        for item in list(bucket):
            if isinstance(item, LiteLLMCallbackLogger) or item.__class__.__name__ == "OpenTelemetry":
                bucket.remove(item)
    _otel_handler = None
    _installed = False
    _recorder.clear()
