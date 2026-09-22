from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


if "litellm" not in sys.modules:
    litellm_module = types.ModuleType("litellm")
    litellm_module.acompletion = lambda **kwargs: None
    integrations_module = types.ModuleType("litellm.integrations")
    custom_logger_module = types.ModuleType("litellm.integrations.custom_logger")

    class CustomLogger:
        pass

    custom_logger_module.CustomLogger = CustomLogger
    sys.modules["litellm"] = litellm_module
    sys.modules["litellm.integrations"] = integrations_module
    sys.modules["litellm.integrations.custom_logger"] = custom_logger_module


from app.db.base import Base
from app.execution_plane.models.runtime_ai import (
    AIParseError,
    AISchemaValidationError,
    HarnessIncompleteError,
)
from app.execution_plane.runtime.bridge import RuntimeBridge
from app.tool_gateway.schema_finalize_review import SchemaFinalizeReviewTool


@pytest.fixture(autouse=True)
def _force_streaming_path():
    from app.core.config import settings

    original = settings.LLM_DISABLE_STREAMING
    settings.LLM_DISABLE_STREAMING = False
    try:
        yield
    finally:
        settings.LLM_DISABLE_STREAMING = original


class GateResult(BaseModel):
    confident: bool
    reason: str


class StrictReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    findings: list[dict[str, Any]] = []
    sub_reviews: list[dict[str, Any]] = []
    file_path: str | None = None
    line_start: int = 0


class AlternativeReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str
    confidence: float


class AIFakeLLMService:
    def __init__(self, response: dict[str, Any]):
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def chat_completion(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


class HarnessFakeLLMService:
    def __init__(self, responses: list[list[dict[str, Any]]] | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    async def chat_completion_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        agent_type: str,
        tools: list[dict[str, Any]],
        parallel_tool_calls: bool,
        max_tokens: int | None = None,
        retry_enabled: bool = True,
    ):
        del agent_type, parallel_tool_calls, max_tokens, retry_enabled
        self.calls.append({"messages": messages, "tools": tools})
        if not self.responses:
            yield {"type": "done", "content": "{}", "usage": {}, "tool_calls": []}
            return
        for event in self.responses.pop(0):
            yield event


def build_session_factory() -> sessionmaker:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def strict_payload() -> dict[str, Any]:
    return {
        "summary": "done",
        "findings": [],
        "sub_reviews": [],
        "file_path": None,
        "line_start": 0,
    }


def done_event(content: str, *, total_tokens: int = 0) -> dict[str, Any]:
    usage = {"total_tokens": total_tokens} if total_tokens else {}
    return {"type": "done", "content": content, "usage": usage, "tool_calls": []}


def tool_call_event(name: str, payload: dict[str, Any], *, call_id: str) -> dict[str, Any]:
    return {
        "type": "tool_call",
        "tool_call": {"id": call_id, "name": name, "arguments": json.dumps(payload)},
    }


def test_ai_schema_success_and_proxy() -> None:
    llm = AIFakeLLMService(
        {"content": '{"confident": true, "reason": "clear input"}', "usage": {"total_tokens": 7}}
    )
    bridge = RuntimeBridge(llm_service=llm, tools=[], session_factory=build_session_factory())

    result = asyncio.run(bridge.ai("gate", schema=GateResult))

    assert result.confident is True
    assert result.reason == "clear input"
    assert result.model_dump() == {"confident": True, "reason": "clear input"}
    assert result.usage == {"total_tokens": 7}
    assert result.cost_usd is None


def test_ai_without_schema_returns_plain_text() -> None:
    text = "This is prose, not JSON."
    llm = AIFakeLLMService({"content": text, "usage": None})
    bridge = RuntimeBridge(llm_service=llm, tools=[], session_factory=build_session_factory())

    result = asyncio.run(bridge.ai("merge this"))

    assert result.text == text
    assert result.content == text
    assert result.parsed is None
    assert result.usage is None


def test_ai_parse_error_keeps_raw_text() -> None:
    llm = AIFakeLLMService({"content": "not-json"})
    bridge = RuntimeBridge(llm_service=llm, tools=[], session_factory=build_session_factory())

    with pytest.raises(AIParseError) as exc_info:
        asyncio.run(bridge.ai("gate", schema=GateResult))
    assert exc_info.value.raw_text == "not-json"


def test_ai_schema_validation_error() -> None:
    llm = AIFakeLLMService({"content": '{"confident": "yes"}'})
    bridge = RuntimeBridge(llm_service=llm, tools=[], session_factory=build_session_factory())

    with pytest.raises(AISchemaValidationError) as exc_info:
        asyncio.run(bridge.ai("gate", schema=GateResult))
    assert exc_info.value.validation_errors


def test_json_parser_imports_migrated() -> None:
    bridge_source = Path("backend/app/execution_plane/runtime/bridge.py").read_text(
        encoding="utf-8"
    )
    query_loop_source = Path("backend/app/execution_plane/runtime/query_loop.py").read_text(
        encoding="utf-8"
    )
    assert "from app.utils.agent_json_parser import AgentJsonParser" in bridge_source
    assert "from app.utils.agent_json_parser import AgentJsonParser" in query_loop_source
    assert "app.execution_plane.harness" not in bridge_source
    assert "app.execution_plane.harness" not in query_loop_source


def test_schema_finalizer_accepts_and_rejects() -> None:
    tool = SchemaFinalizeReviewTool(StrictReviewResult)
    accepted = asyncio.run(tool.execute(tool.validate_input(strict_payload()), context=None))
    assert accepted.output_payload["final_payload"]["summary"] == "done"
    assert accepted.output_payload["completion_mode"] == "finalize_tool"
    assert accepted.output_payload["terminal_action"] == "finalize_review"

    rejected = asyncio.run(tool.execute(tool.validate_input({}), context=None))
    assert rejected.output_payload["finalization_rejected"] is True
    assert rejected.output_payload["validation_errors"]
    assert "final_payload" not in rejected.output_payload


def test_schema_finalizer_is_not_implicitly_bound_to_legacy_schema() -> None:
    described = SchemaFinalizeReviewTool(StrictReviewResult).describe()
    schema_text = json.dumps(described["input_schema"])
    assert described["name"] == "FinalizeReview"
    assert "rule_id" not in schema_text
    assert "category" not in schema_text
    assert "source" not in schema_text


def test_harness_tool_call_success_and_persistence() -> None:
    payload = strict_payload()
    llm = HarnessFakeLLMService(
        [
            [
                tool_call_event("FinalizeReview", payload, call_id="call-ok"),
                done_event("done", total_tokens=9),
            ]
        ]
    )
    bridge = RuntimeBridge(llm_service=llm, tools=[], session_factory=build_session_factory())

    result = asyncio.run(bridge.harness("review", schema=StrictReviewResult, cwd=str(Path.cwd())))

    assert isinstance(result.parsed, StrictReviewResult)
    assert result.result["final_payload"]["summary"] == "done"
    assert result.usage == {
        "total_tokens": 9,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": None,
        "usage_complete": False,
        "cost_available": False,
    }
    assert result.cost_usd is None
    snapshot = bridge._session_store.load_session_snapshot(result.session_id)
    assert snapshot.session.recon_payload["deep_runtime_cwd"] == str(Path.cwd())
    assert len(snapshot.tool_calls) == 1
    assert snapshot.skills == []
    assert snapshot.memories == []


def test_harness_accumulates_input_output_and_cost() -> None:
    class CostedHarnessLLMService:
        def __init__(self):
            self.calls = 0

        async def chat_completion_stream(self, **kwargs):
            del kwargs
            self.calls += 1
            yield {
                "type": "done",
                "content": "done",
                "usage": {"input_tokens": 11, "output_tokens": 5},
                "tool_calls": [
                    {
                        "id": "call-cost",
                        "name": "FinalizeReview",
                        "arguments": json.dumps(strict_payload(), ensure_ascii=False),
                    }
                ],
            }

    llm = CostedHarnessLLMService()
    bridge = RuntimeBridge(llm_service=llm, tools=[], session_factory=build_session_factory())

    result = asyncio.run(bridge.harness("review", schema=StrictReviewResult))

    assert llm.calls == 1
    assert result.usage["total_tokens"] == 16
    assert result.usage["input_tokens"] == 11
    assert result.usage["output_tokens"] == 5
    assert result.usage["usage_complete"] is True
    assert result.usage["cost_available"] is False
    assert result.usage["cost_usd"] is None
    assert result.cost_usd is None


def test_harness_assistant_json_fallback() -> None:
    content = json.dumps(strict_payload(), ensure_ascii=False)
    llm = HarnessFakeLLMService([[done_event(content)]])
    bridge = RuntimeBridge(llm_service=llm, tools=[], session_factory=build_session_factory())

    result = asyncio.run(bridge.harness("review", schema=StrictReviewResult, tools=[]))

    assert result.parsed.findings == []
    assert result.result["tool_call_count"] == 0


def test_harness_invalid_tool_call_then_retry_succeeds() -> None:
    llm = HarnessFakeLLMService(
        [
            [tool_call_event("FinalizeReview", {}, call_id="call-invalid"), done_event("rejected")],
            [
                tool_call_event("FinalizeReview", strict_payload(), call_id="call-valid"),
                done_event("done"),
            ],
        ]
    )
    bridge = RuntimeBridge(llm_service=llm, tools=[], session_factory=build_session_factory())

    result = asyncio.run(bridge.harness("review", schema=StrictReviewResult, max_turns=2))

    assert result.parsed.summary == "done"
    assert len(llm.calls) == 2


def test_harness_final_failure_raises() -> None:
    llm = HarnessFakeLLMService(
        [[tool_call_event("FinalizeReview", {}, call_id="call-invalid"), done_event("rejected")]]
    )
    bridge = RuntimeBridge(llm_service=llm, tools=[], session_factory=build_session_factory())

    with pytest.raises(HarnessIncompleteError):
        asyncio.run(bridge.harness("review", schema=StrictReviewResult, max_turns=1))


def test_harness_schema_isolation() -> None:
    first_llm = HarnessFakeLLMService(
        [[tool_call_event("FinalizeReview", strict_payload(), call_id="first"), done_event("done")]]
    )
    second_llm = HarnessFakeLLMService(
        [
            [
                tool_call_event(
                    "FinalizeReview",
                    {"verdict": "pass", "confidence": 0.9},
                    call_id="second",
                ),
                done_event("done"),
            ]
        ]
    )
    first = RuntimeBridge(llm_service=first_llm, tools=[], session_factory=build_session_factory())
    second = RuntimeBridge(
        llm_service=second_llm, tools=[], session_factory=build_session_factory()
    )

    first_result = asyncio.run(first.harness("review", schema=StrictReviewResult))
    second_result = asyncio.run(second.harness("review", schema=AlternativeReviewResult))

    assert isinstance(first_result.parsed, StrictReviewResult)
    assert isinstance(second_result.parsed, AlternativeReviewResult)
    assert first_result.parsed.summary == "done"
    assert second_result.parsed.verdict == "pass"
