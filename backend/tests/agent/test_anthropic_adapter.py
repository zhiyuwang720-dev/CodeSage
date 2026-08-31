import json

import pytest

from app.services.llm.adapters.anthropic_adapter import AnthropicAdapter
from app.services.llm.factory import LLMFactory
from app.services.llm.adapters.litellm_adapter import LiteLLMAdapter
from app.services.llm.types import LLMConfig, LLMMessage, LLMProvider, LLMRequest


def test_llm_factory_uses_anthropic_adapter_for_anthropic_endpoint_protocol():
    LLMFactory.clear_cache()

    adapter = LLMFactory.create_adapter(
        LLMConfig(
            provider=LLMProvider.CLAUDE,
            api_key="test-key",
            model="claude-opus-4-7",
            base_url="https://relay.example.com",
            endpoint_protocol="anthropic",
        )
    )

    assert isinstance(adapter, AnthropicAdapter)


def test_llm_factory_keeps_claude_openai_compatible_on_litellm():
    LLMFactory.clear_cache()

    adapter = LLMFactory.create_adapter(
        LLMConfig(
            provider=LLMProvider.CLAUDE,
            api_key="test-key",
            model="claude-opus-4-7",
            base_url="https://relay.example.com",
            endpoint_protocol="openai_compatible",
        )
    )

    assert isinstance(adapter, LiteLLMAdapter)


def test_anthropic_payload_omits_temperature_for_opus_4_8():
    adapter = AnthropicAdapter(
        LLMConfig(
            provider=LLMProvider.CLAUDE,
            api_key="test-key",
            model="claude-opus-4-8",
            endpoint_protocol="anthropic",
            temperature=0.1,
        )
    )

    payload = adapter._build_payload(
        LLMRequest(messages=[LLMMessage(role="user", content="Audit this")]),
        stream=False,
    )

    assert "temperature" not in payload


def test_anthropic_payload_keeps_temperature_for_sonnet_4_5():
    adapter = AnthropicAdapter(
        LLMConfig(
            provider=LLMProvider.CLAUDE,
            api_key="test-key",
            model="claude-sonnet-4-5",
            endpoint_protocol="anthropic",
            temperature=0.1,
        )
    )

    payload = adapter._build_payload(
        LLMRequest(messages=[LLMMessage(role="user", content="Audit this")]),
        stream=False,
    )

    assert payload["temperature"] == 0.1


def test_anthropic_payload_removes_final_assistant_prefill_for_opus_4_8():
    adapter = AnthropicAdapter(
        LLMConfig(
            provider=LLMProvider.CLAUDE,
            api_key="test-key",
            model="claude-opus-4-8",
            endpoint_protocol="anthropic",
        )
    )

    payload = adapter._build_payload(
        LLMRequest(
            messages=[
                LLMMessage(role="user", content="Finalize the finding."),
                LLMMessage(role="assistant", content='{"findings": ['),
            ]
        ),
        stream=False,
    )

    assert payload["messages"][-1]["role"] == "user"
    assert "assistant draft" in payload["messages"][-1]["content"].lower()
    assert '{"findings": [' in payload["messages"][-1]["content"]


def test_anthropic_payload_keeps_final_assistant_prefill_for_sonnet_4_5():
    adapter = AnthropicAdapter(
        LLMConfig(
            provider=LLMProvider.CLAUDE,
            api_key="test-key",
            model="claude-sonnet-4-5",
            endpoint_protocol="anthropic",
        )
    )

    payload = adapter._build_payload(
        LLMRequest(
            messages=[
                LLMMessage(role="user", content="Finalize the finding."),
                LLMMessage(role="assistant", content='{"findings": ['),
            ]
        ),
        stream=False,
    )

    assert payload["messages"][-1] == {"role": "assistant", "content": '{"findings": ['}


class _FakeStreamResponse:
    status_code = 200
    text = ""

    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeAnthropicClient:
    def __init__(self, lines):
        self.lines = lines
        self.stream_calls = []

    def stream(self, method, url, *, headers=None, json=None):
        self.stream_calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "json": json or {},
            }
        )
        return _FakeStreamResponse(self.lines)


@pytest.mark.asyncio
async def test_anthropic_adapter_stream_complete_sends_native_messages_and_tools():
    client = _FakeAnthropicClient(
        [
            'data: {"type":"message_start","message":{"usage":{"input_tokens":12}}}',
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"Need skill."}}',
            'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}',
            'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Inspecting"}}',
            'data: {"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"toolu_1","name":"Skill","input":{}}}',
            'data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"{\\"skill_ref\\":"}}',
            'data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"\\"code-audit-finding\\"}"}}',
            'data: {"type":"content_block_stop","index":2}',
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":8}}',
            'data: {"type":"message_stop"}',
        ]
    )
    adapter = AnthropicAdapter(
        LLMConfig(
            provider=LLMProvider.CLAUDE,
            api_key="test-key",
            model="claude-opus-4-7",
            base_url="https://relay.example.com",
            endpoint_protocol="anthropic",
        )
    )
    adapter._client = client

    request = LLMRequest(
        messages=[
            LLMMessage(role="system", content="System prompt"),
            LLMMessage(role="user", content="Audit this"),
            LLMMessage(
                role="assistant",
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_prior",
                        "name": "Skill",
                        "input": {"skill_ref": "code-audit-finding"},
                    }
                ],
            ),
            LLMMessage(
                role="user",
                content=[
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_prior",
                        "content": "Skill completed",
                    }
                ],
            ),
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "Skill",
                    "description": "Load a skill",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        parallel_tool_calls=True,
        stream=True,
    )

    events = []
    async for event in adapter.stream_complete(request):
        events.append(event)

    call = client.stream_calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://relay.example.com/v1/messages"
    assert call["headers"]["x-api-key"] == "test-key"
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    assert call["json"]["system"] == "System prompt"
    assert call["json"]["messages"][1]["content"][0]["type"] == "tool_use"
    assert call["json"]["messages"][2]["content"][0]["type"] == "tool_result"
    assert call["json"]["tools"] == [
        {
            "name": "Skill",
            "description": "Load a skill",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    assert "parallel_tool_calls" not in call["json"]

    assert [event["type"] for event in events] == [
        "reasoning_delta",
        "token",
        "tool_call",
        "done",
    ]
    assert events[0]["reasoning_content"] == "Need skill."
    assert events[1]["content"] == "Inspecting"
    assert events[2]["tool_call"]["id"] == "toolu_1"
    assert events[2]["tool_call"]["name"] == "Skill"
    assert json.loads(events[2]["tool_call"]["arguments"]) == {"skill_ref": "code-audit-finding"}
    assert events[-1]["finish_reason"] == "tool_use"
    assert events[-1]["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 8,
        "total_tokens": 20,
    }
