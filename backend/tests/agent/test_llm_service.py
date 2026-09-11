"""LLMService 薄外观单元测试（P01/P02/P05）。

这里验证服务层职责：配置解析、准入/间隔协调、结果装饰与预算选择。
真实 SDK 请求语义由 tests/observability_acceptance 的 AP02/AP04 用真实 SDK +
本地 HTTP/SSE 服务验证，本文件不承担那部分证据。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.execution_plane.models import client as client_module
from app.execution_plane.models.errors import ModelRateLimitError
from app.execution_plane.models.service import LLMService


def _payload(content: str = "ok", *, model: str = "stub-model", usage=None, finish_reason: str = "stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=None, reasoning_content=None),
                finish_reason=finish_reason,
            )
        ],
        model=model,
        usage=usage,
        id="stub-response",
        _hidden_params={},
    )


def _install_fake_sdk(monkeypatch, *, fail_times: int = 0, delay: float = 0.0, recorder: list | None = None):
    """替换唯一 SDK 发送点，用于服务层单元测试。"""

    state = {"calls": 0, "in_flight": 0, "max_in_flight": 0}

    async def fake_acompletion(**kwargs):
        state["calls"] += 1
        state["in_flight"] += 1
        state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
        if recorder is not None:
            recorder.append(dict(kwargs))
        try:
            if delay:
                await asyncio.sleep(delay)
            if state["calls"] <= fail_times:
                raise ModelRateLimitError("rate limited")
            return _payload("recovered")
        finally:
            state["in_flight"] -= 1

    monkeypatch.setattr(client_module.litellm, "acompletion", fake_acompletion)
    return state


def _service(**other_config) -> LLMService:
    other = {"llmConcurrency": 1, "llmGapMs": 0}
    other.update(other_config)
    return LLMService(
        user_config={
            "llmConfig": {
                "llmProvider": "openai",
                "llmApiKey": "test-key",
                "llmModel": "test-model",
                "llmBaseUrl": "https://example.invalid/v1",
            },
            "otherConfig": other,
        }
    )


@pytest.mark.asyncio
async def test_llm_service_respects_user_configured_llm_concurrency(monkeypatch):
    state = _install_fake_sdk(monkeypatch, delay=0.05)
    service = _service(llmConcurrency=1)

    await asyncio.gather(
        service.chat_completion(messages=[{"role": "user", "content": "first"}]),
        service.chat_completion(messages=[{"role": "user", "content": "second"}]),
    )

    assert state["calls"] == 2
    assert state["max_in_flight"] == 1


@pytest.mark.asyncio
async def test_unknown_provider_error_is_classified_before_service(monkeypatch):
    async def fake_acompletion(**kwargs):
        raise ModelRateLimitError("rate limited")

    monkeypatch.setattr(client_module.litellm, "acompletion", fake_acompletion)
    service = _service()

    # 独立调用预算为 3：失败 3 次后抛出已映射的模型错误
    with pytest.raises(ModelRateLimitError):
        await service.chat_completion(messages=[{"role": "user", "content": "retry me"}])


@pytest.mark.asyncio
async def test_llm_service_retries_independent_calls_within_budget(monkeypatch):
    state = _install_fake_sdk(monkeypatch, fail_times=2)
    service = _service()

    result = await service.chat_completion(messages=[{"role": "user", "content": "retry me"}])

    assert result["content"] == "recovered"
    assert state["calls"] == 3


@pytest.mark.asyncio
async def test_llm_service_harness_owner_issues_single_attempt(monkeypatch):
    state = _install_fake_sdk(monkeypatch, fail_times=5)
    service = _service()
    config = service.get_agent_config(retry_owner="harness")
    request = client_module.LLMRequest(
        messages=[{"role": "user", "content": "one attempt only"}], purpose="review"
    )

    with pytest.raises(ModelRateLimitError):
        await service._run_completion(config, request)

    assert config.retry_budget == 1
    assert config.retry_owner == "harness"
    assert state["calls"] == 1


@pytest.mark.asyncio
async def test_llm_service_passes_tools_and_parallel_tool_calls(monkeypatch):
    captured: list = []
    _install_fake_sdk(monkeypatch, recorder=captured)
    service = _service()

    result = await service.chat_completion(
        messages=[{"role": "user", "content": "use tools"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read_many_files",
                    "description": "Read multiple files",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        parallel_tool_calls=True,
    )

    assert result["content"] == "recovered"
    assert captured[0]["tools"][0]["function"]["name"] == "read_many_files"
    assert captured[0]["parallel_tool_calls"] is True
    assert captured[0]["num_retries"] == 0


def test_llm_service_uses_runtime_env_fallbacks_for_provider_config():
    service = LLMService(
        user_config={
            "llmConfig": {
                "llmProvider": "claude",
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": "env-claude-key",
                    "ANTHROPIC_BASE_URL": "https://pureopus.cc",
                    "ANTHROPIC_MODEL": "claude-opus-4-6",
                    "API_TIMEOUT_MS": "3000000",
                },
            }
        }
    )

    config = service.get_agent_config()

    assert config.provider.value == "claude"
    assert config.api_key == "env-claude-key"
    assert config.base_url == "https://pureopus.cc"
    assert config.model == "claude-opus-4-6"
    assert config.timeout == 3000
    assert config.sdk_model == "anthropic/claude-opus-4-6"
    assert config.transport == "anthropic_messages"


@pytest.mark.asyncio
async def test_llm_service_stream_preserves_tool_call_events(monkeypatch):
    async def fake_stream(self, *, config, request, **kwargs):
        yield {"type": "token", "content": "Need ", "accumulated": "Need "}
        yield {
            "type": "tool_call",
            "tool_call": {"id": "call_1", "type": "function", "name": "Read", "arguments": '{"file_path":"README.md"}'},
        }
        yield {
            "type": "done",
            "content": "Need tool",
            "finish_reason": "tool_calls",
            "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
            "tool_calls": [
                {"id": "call_1", "type": "function", "name": "Read", "arguments": '{"file_path":"README.md"}'}
            ],
        }

    service = _service()
    monkeypatch.setattr(client_module.SDKModelClient, "stream", fake_stream)

    events = []
    async for event in service.chat_completion_stream(
        messages=[{"role": "user", "content": "use tools"}], retry_enabled=False
    ):
        events.append(event)

    assert [event["type"] for event in events] == ["token", "tool_call", "done"]
    assert events[1]["tool_call"]["name"] == "Read"
    assert events[2]["finish_reason"] == "tool_calls"
    assert events[2]["configured_model"] == "test-model"
    assert events[2]["purpose"] == "review"


@pytest.mark.asyncio
async def test_llm_service_stream_marks_harness_retry_owner(monkeypatch):
    captured = {}

    async def fake_stream(self, *, config, request, **kwargs):
        captured["budget"] = config.retry_budget
        captured["owner"] = config.retry_owner
        yield {"type": "done", "content": "", "finish_reason": "stop", "tool_calls": [], "usage": None}

    service = _service()
    monkeypatch.setattr(client_module.SDKModelClient, "stream", fake_stream)

    async for _ in service.chat_completion_stream(
        messages=[{"role": "user", "content": "runtime owns retry"}], retry_enabled=False
    ):
        pass

    assert captured == {"budget": 1, "owner": "harness"}


def test_llm_service_config_snapshot_excludes_credentials():
    service = _service()
    config = service.get_agent_config()
    assert config.api_key == "test-key"
    assert "test-key" not in repr(config)
    assert "test-key" not in str(config.snapshot())
