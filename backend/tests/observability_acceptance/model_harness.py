"""观测验收夹具：真实 SDK + 真实本地 HTTP/SSE 服务 + 统一 TracerProvider。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.execution_plane.models.config import LLMConfig
from app.execution_plane.models.service import LLMService
from app.execution_plane.models.client import reset_sdk_client

from .fixture_server import FixtureServer, PlannedResponse, RouteScript


@dataclass
class ModelHarness:
    server: FixtureServer
    exporter: InMemorySpanExporter
    runtime: Any
    artifact_root: Path

    def service(
        self,
        *,
        provider: str = "openai",
        model: str = "fixture-model",
        api_key: str = "fixture-key",
        protocol: str = "openai_chat",
        timeout: int = 20,
        max_tokens: int = 256,
        base_url: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> LLMService:
        llm_config: Dict[str, Any] = {
            "llmProvider": provider,
            "llmModel": model,
            "llmApiKey": api_key,
            "llmBaseUrl": base_url or self.server.base_url,
            "llmTimeout": timeout,
            "llmMaxTokens": max_tokens,
            "endpointProtocol": protocol,
        }
        if extra:
            llm_config.update(extra)
        return LLMService(
            user_config={
                "llmConfig": llm_config,
                "otherConfig": {"llmConcurrency": 1, "llmGapMs": 0},
            }
        )

    def direct_config(
        self,
        *,
        provider: str = "openai",
        model: str = "fixture-model",
        protocol: str = "openai_chat",
        timeout: int = 20,
        retry_budget: int = 1,
        retry_owner: str = "harness",
        extra: Optional[Dict[str, Any]] = None,
    ) -> LLMConfig:
        service = self.service(provider=provider, model=model, protocol=protocol, timeout=timeout)
        config = service.get_agent_config(retry_owner=retry_owner)
        from dataclasses import replace

        return replace(config, retry_budget=retry_budget, **({} if not extra else extra))

    def spans(self, name_contains: Optional[str] = None) -> List[Any]:
        items = list(self.exporter.get_finished_spans())
        if name_contains:
            items = [item for item in items if name_contains in (item.name or "")]
        return items

    def clear_spans(self) -> None:
        self.exporter.clear()

    def http_records(self) -> List[Any]:
        return self.server.requests()

    def http_count(self) -> int:
        return len(self.server.requests())

    def artifact(self, *parts: str) -> Path:
        target = self.artifact_root.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def write_json(self, relative: str, payload: Any) -> Path:
        import json

        target = self.artifact("evidence", relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return target


@dataclass
class FixtureBundle:
    harness: ModelHarness = field(default=None)  # type: ignore[assignment]


def _artifact_root() -> Path:
    import datetime
    import subprocess

    base = Path(os.environ.get("CODESAGE_ACCEPTANCE_ARTIFACT_ROOT") or (Path(__file__).resolve().parents[2] / ".acceptance-artifacts" / "plan20p0"))
    stamp = os.environ.get("CODESAGE_ACCEPTANCE_STAMP")
    if not stamp:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    commit = os.environ.get("CODESAGE_ACCEPTANCE_COMMIT")
    if not commit:
        try:
            commit = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(Path(__file__).resolve().parents[2]),
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip() or "unknown"
        except Exception:  # noqa: BLE001
            commit = "unknown"
    return base / f"{stamp}-{commit}"


@pytest.fixture(scope="session")
def acceptance_artifact_root() -> Path:
    root = _artifact_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


_SHARED_EXPORTER: Optional[InMemorySpanExporter] = None


def _shared_exporter() -> InMemorySpanExporter:
    """整套验收共用一个进程内 exporter：全局 TracerProvider 只能注册一次。"""

    global _SHARED_EXPORTER
    if _SHARED_EXPORTER is None:
        _SHARED_EXPORTER = InMemorySpanExporter()
    return _SHARED_EXPORTER


@pytest.fixture()
def model_harness(acceptance_artifact_root: Path, monkeypatch) -> ModelHarness:
    """每个用例一套干净的 SDK + 服务器 + provider。"""

    from app.infrastructure.observability import configure_observability
    from app.infrastructure.observability.litellm_integration import (
        install_litellm_integration,
        reset_integration_state,
    )

    os.environ.setdefault("OTEL_SDK_DISABLED", "false")
    # 本机 fixture 必须直连：容器/开发机常配 HTTP 代理，代理会拒答随机本地端口。
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
    # 验收里重试间隔压到最小；重试次数与所有者语义不变。
    os.environ.setdefault("CODESAGE_MODEL_ATTEMPT_GAP_SECONDS", "0.05")
    reset_integration_state()
    reset_sdk_client()

    exporter = _shared_exporter()
    exporter.clear()
    runtime = configure_observability(
        service_name="codesage-acceptance",
        enabled=True,
        endpoint=None,
        local_trace_path=None,
        local_metric_path=None,
        capture_content=False,
    )
    assert runtime.tracer_provider is not None
    if not getattr(runtime.tracer_provider, "_codesage_acceptance_processor", False):
        runtime.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
        setattr(runtime.tracer_provider, "_codesage_acceptance_processor", True)
    install_litellm_integration(capture_content=False)

    server = FixtureServer().start()
    harness = ModelHarness(server=server, exporter=exporter, runtime=runtime, artifact_root=acceptance_artifact_root)
    try:
        yield harness
    finally:
        server.stop()
        reset_integration_state()
        reset_sdk_client()


__all__ = [
    "FixtureServer",
    "ModelHarness",
    "PlannedResponse",
    "RouteScript",
    "acceptance_artifact_root",
    "model_harness",
]
