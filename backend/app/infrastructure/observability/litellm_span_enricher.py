"""把 SDK 模型 span 补齐到统一语义（P08）。

LiteLLM 官方 OTel integration 产生 `litellm_request` span，但它不带
`openinference.span.kind`，Phoenix/OpenInference 因此不会把它识别为 LLM span。
这里用一个极薄的 SpanProcessor 在 span **开始时**补属性：

- 不创建 span、不改父子关系、不改名称；
- 不参与业务事务，异常只记录日志；
- 只在 SDK 请求级 span 上生效，避免给非模型 span 贴 LLM 语义。

注意：OTel SDK 在 span end 之后拒绝属性写入（`Setting attribute on ended span`），
所以只能在 `on_start` 补；usage/成本由 SDK integration 自己写入。
"""

from __future__ import annotations

import logging
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

logger = logging.getLogger(__name__)

# LiteLLM 官方 integration 的请求级 span 名（默认名与 gen-ai 语义约定名都会匹配）。
MODEL_SPAN_NAME_PREFIXES = ("litellm_request", "chat ", "completion ", "embedding ")


class ModelSpanEnricher(SpanProcessor):
    """给 SDK 的模型请求 span 补 OpenInference/CodeSage 语义属性。"""

    def on_start(self, span: Any, parent_context: Any = None) -> None:  # noqa: D102 - SpanProcessor 接口
        try:
            name = getattr(span, "name", "") or ""
            if not any(name.startswith(prefix) for prefix in MODEL_SPAN_NAME_PREFIXES):
                return
            context = span.get_span_context()
            span_id = getattr(context, "span_id", None)
            attributes: dict[str, Any] = {"openinference.span.kind": "LLM"}
            if span_id:
                attributes["codesage.provider_request_id"] = format(span_id, "016x")
            span.set_attributes(attributes)
        except Exception:  # noqa: BLE001 - 观测失败不影响业务与导出
            logger.debug("model span enrichment failed", exc_info=True)

    def on_end(self, span: ReadableSpan) -> None:  # noqa: D102 - SpanProcessor 接口
        return None

    def shutdown(self) -> None:  # noqa: D102 - SpanProcessor 接口
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: D102 - SpanProcessor 接口
        return True


def install_model_span_enricher(tracer_provider: Any) -> ModelSpanEnricher | None:
    """在统一 TracerProvider 上安装一次（重复调用不会重复安装）。"""

    if tracer_provider is None:
        return None
    if getattr(tracer_provider, "_codesage_model_span_enricher", None) is not None:
        return tracer_provider._codesage_model_span_enricher  # type: ignore[attr-defined]
    enricher = ModelSpanEnricher()
    try:
        tracer_provider.add_span_processor(enricher)
    except Exception:  # noqa: BLE001
        logger.warning("model span enricher not installed", exc_info=True)
        return None
    try:
        setattr(tracer_provider, "_codesage_model_span_enricher", enricher)
    except Exception:  # noqa: BLE001
        pass
    return enricher


__all__ = ["MODEL_SPAN_NAME_PREFIXES", "ModelSpanEnricher", "install_model_span_enricher"]
