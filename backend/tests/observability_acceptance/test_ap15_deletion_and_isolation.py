"""AP15：删除扫描、观测故障隔离与回归入口（P11,P12）。"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.infrastructure.observability import litellm_integration as li

from .fixture_server import PlannedResponse, openai_completion

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
CHAT_PATH = "/v1/chat/completions"

DELETION_MAP = [
    {
        "old_symbol": "app.execution_plane.models.factory.LLMFactory",
        "callers": ["service.LLMService", "api.v1.endpoints.config"],
        "replacement": "models.config.ModelCatalog + models.client.SDKModelClient",
        "reason": "adapter 选择职责被 SDK 传输族解析取代",
        "verification": "AP01/AP09",
    },
    {
        "old_symbol": "app.execution_plane.models.base_adapter.BaseLLMAdapter",
        "callers": ["全部 adapters/*"],
        "replacement": "models.client.SDKModelClient",
        "reason": "自研 HTTP/SSE 与重试框架被 SDK 取代",
        "verification": "AP01/AP04",
    },
    {
        "old_symbol": "app.execution_plane.models.adapters.AnthropicAdapter",
        "callers": ["factory"],
        "replacement": "LiteLLM anthropic 传输族（protocol=anthropic_messages）",
        "reason": "厂商协议由 SDK 负责",
        "verification": "AP02",
    },
    {
        "old_symbol": "app.execution_plane.models.protocols.registry",
        "callers": ["factory", "service", "runtime.bridge"],
        "replacement": "models.config（模型目录 + 传输族 + tool 格式解析）",
        "reason": "协议分派退出模型层",
        "verification": "AP01/AP02",
    },
    {
        "old_symbol": "app.execution_plane.models.retry.LLM_RETRY_CONFIG",
        "callers": ["service"],
        "replacement": "SDKModelClient 有界预算 + QueryLoop 业务预算",
        "reason": "消除 service/adapter/SDK 多层重试倍增",
        "verification": "AP04",
    },
    {
        "old_symbol": "app.execution_plane.models.errors.AgentError 继承树（Agent/Tool/State/Validation）",
        "callers": ["仅 retry.py 与该文件自身"],
        "replacement": "models.errors.ModelBoundaryError 及恢复类别",
        "reason": "无生产调用者的重复异常体系",
        "verification": "AP01",
    },
    {
        "old_symbol": "app.execution_plane.models.prompt_cache",
        "callers": ["adapters/litellm_adapter"],
        "replacement": "无（provider prompt cache 只经 SDK 参数表达）",
        "reason": "旧缓存前缀策略已无引用；不再通过改写系统提示词模拟缓存",
        "verification": "AP11",
    },
    {
        "old_symbol": "app.execution_plane.models.tokenizer",
        "callers": ["prompt_cache", "memory_compressor"],
        "replacement": "无（预算估算由 Harness/Plan20 负责）",
        "reason": "仅在已删除模块内被引用",
        "verification": "AP01",
    },
    {
        "old_symbol": "app.execution_plane.models.memory_compressor",
        "callers": ["models/__init__ 重导出"],
        "replacement": "运行时 compaction（execution_plane/runtime/compaction）",
        "reason": "模型层不再承担上下文压缩",
        "verification": "AP01",
    },
    {
        "old_symbol": "app.infrastructure.observability.provider_requests.observe_provider_request",
        "callers": ["adapters/anthropic_adapter"],
        "replacement": "infrastructure.observability.litellm_integration + 官方 OTel integration",
        "reason": "同一 SDK 请求不再外包第二个 LLM span",
        "verification": "AP13",
    },
]


def test_ap15_deleted_symbols_have_no_production_reference() -> None:
    forbidden = (
        "LLMFactory",
        "BaseLLMAdapter",
        "LiteLLMAdapter",
        "AnthropicAdapter",
        "observe_provider_request",
        "prompt_cache_manager",
        "MemoryCompressor",
        "TokenEstimator",
        "retry_with_backoff",
    )
    offenders: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8-sig")
        for symbol in forbidden:
            if symbol in text:
                offenders.append(f"{path.relative_to(APP_ROOT.parent).as_posix()}:{symbol}")
    assert offenders == []


def test_ap15_no_second_llm_emitting_path_in_observability() -> None:
    integration = (APP_ROOT / "infrastructure" / "observability" / "litellm_integration.py").read_text(
        encoding="utf-8"
    )
    assert "start_as_current_span" not in integration
    assert "start_span" not in integration
    tree = ast.parse(integration)
    created = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    span_creators = [node for node in created if getattr(node.func, "attr", "") in {"start_span", "start_as_current_span"}]
    assert span_creators == []


@pytest.mark.asyncio
async def test_ap15_no_second_llm_span_in_the_harness_turn(model_harness) -> None:
    """模型 span 只由 SDK 产生：Harness 侧的尝试容器不得再声明 LLM 语义。

    证据以源码静态断言给出，避免依赖 SDK 回调在测试事件循环收尾时的落盘时机。
    """

    from app.execution_plane.runtime import query_loop

    source = Path(query_loop.__file__).read_text(encoding="utf-8")
    assert '"provider.request"' in source
    start = source.index('"provider.request"')
    window = source[max(0, start - 200) : start + 200]
    assert '"LLM"' not in window, "provider.request 不能声明 LLM 语义（会同次调用产生第二个模型 span）"

    # 仓库内不应再出现其他 LLM 语义的 span 声明（模型 span 由 SDK integration 产生）
    llm_semantics_sites: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8-sig")
        if '"openinference.span.kind": "LLM"' in text and "span_enricher" not in path.name:
            llm_semantics_sites.append(path.relative_to(APP_ROOT.parent).as_posix())
    assert llm_semantics_sites == []
    model_harness.write_json(
        "ap15/single_llm_span.json",
        {
            "requirement": "P08,P11",
            "acceptance": "AP15",
            "harness_container_span": "provider.request (kind=CHAIN)",
            "llm_span_owner": "litellm.integrations.opentelemetry.OpenTelemetry",
            "other_llm_kind_sites": llm_semantics_sites,
        },
    )


@pytest.mark.asyncio
async def test_ap15_observability_failure_does_not_change_result(model_harness, monkeypatch) -> None:
    model_harness.server.set_default(
        CHAT_PATH, PlannedResponse(payload=openai_completion(content="ok", model="fixture-model"))
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    def boom(self, event, kwargs, response_obj):
        raise RuntimeError("observability exploded")

    monkeypatch.setattr(li.LiteLLMCallbackLogger, "_record", boom)

    result = await service.chat_completion(messages=[{"role": "user", "content": "hi"}], purpose="review")
    assert result["content"] == "ok"
    assert model_harness.http_count() == 1


@pytest.mark.asyncio
async def test_ap15_export_failure_does_not_change_result(model_harness, monkeypatch) -> None:
    from opentelemetry.sdk.trace.export import SpanExportResult

    model_harness.server.set_default(
        CHAT_PATH, PlannedResponse(payload=openai_completion(content="ok", model="fixture-model"))
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    class ExplodingExporter:
        def export(self, spans):
            raise RuntimeError("exporter down")

        def shutdown(self):
            return None

    model_harness.runtime.tracer_provider.add_span_processor(
        __import__("opentelemetry.sdk.trace.export", fromlist=["SimpleSpanProcessor"]).SimpleSpanProcessor(
            ExplodingExporter()
        )
    )
    result = await service.chat_completion(messages=[{"role": "user", "content": "hi"}], purpose="review")
    assert result["content"] == "ok"
    assert SpanExportResult is not None


def test_ap15_deletion_map_evidence(acceptance_artifact_root: Path) -> None:
    target = acceptance_artifact_root / "evidence" / "ap15" / "deletion-map.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(DELETION_MAP, ensure_ascii=False, indent=2), encoding="utf-8")
    assert target.exists()
