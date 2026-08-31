from app.services.llm.protocols.transforms import (
    anthropic_messages_payload,
    gemini_native_payload,
    openai_chat_payload,
    openai_responses_payload,
)
from app.services.llm.types import LLMMessage, LLMRequest


def _request() -> LLMRequest:
    return LLMRequest(
        messages=[
            LLMMessage(role="system", content="You are a security agent."),
            LLMMessage(role="user", content="Read package.json"),
            LLMMessage(
                role="assistant",
                content="",
                reasoning_content="Need to inspect dependencies.",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "name": "Read",
                        "arguments": '{"path":"package.json"}',
                    }
                ],
            ),
            LLMMessage(role="tool", tool_call_id="call_1", name="Read", content='{"ok":true}'),
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "Read",
                    "description": "Read a file",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "format": "uri"},
                        },
                    },
                },
            }
        ],
        parallel_tool_calls=True,
        stream=True,
    )


def test_openai_chat_payload_preserves_tool_calls_and_sanitizes_schema() -> None:
    payload = openai_chat_payload(_request(), model="mimo-v2.5-pro", provider="mimo")

    assert payload["model"] == "mimo-v2.5-pro"
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["messages"][2]["tool_calls"][0]["function"]["name"] == "Read"
    assert payload["messages"][3]["role"] == "tool"
    assert payload["tools"][0]["function"]["parameters"]["properties"]["path"] == {"type": "string"}


def test_openai_responses_payload_uses_flat_function_tools_and_outputs() -> None:
    payload = openai_responses_payload(_request(), model="gpt-5.5")

    assert payload["model"] == "gpt-5.5"
    assert payload["instructions"] == "You are a security agent."
    assert payload["tools"][0]["type"] == "function"
    assert payload["tools"][0]["name"] == "Read"
    assert any(item["type"] == "function_call" for item in payload["input"])
    assert any(item["type"] == "function_call_output" for item in payload["input"])


def test_anthropic_payload_maps_openai_tools_to_blocks() -> None:
    payload = anthropic_messages_payload(_request(), model="claude-opus-4-8")

    assert payload["system"] == "You are a security agent."
    assert payload["tools"][0]["input_schema"]["properties"]["path"] == {"type": "string"}
    assert payload["messages"][1]["content"][0]["type"] == "tool_use"
    assert payload["messages"][2]["content"][0]["type"] == "tool_result"


def test_gemini_native_payload_maps_tools_and_tool_history() -> None:
    payload = gemini_native_payload(_request(), model="gemini-3.5-flash")

    assert payload["model"] == "gemini-3.5-flash"
    assert payload["systemInstruction"]["parts"][0]["text"] == "You are a security agent."
    declaration = payload["tools"][0]["functionDeclarations"][0]
    assert declaration["name"] == "Read"
    assert declaration["parameters"]["properties"]["path"] == {"type": "string"}
    assert any("functionCall" in part for content in payload["contents"] for part in content["parts"])
    assert any("functionResponse" in part for content in payload["contents"] for part in content["parts"])
