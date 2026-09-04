from __future__ import annotations

import json

from app.services.contracts.models import RuntimeMessageRole, TranscriptItem
from app.services.tooling.codec import (
    ToolMessageFormat,
    build_runtime_model_messages,
)


def test_openai_native_tool_history_preserves_call_and_result_pairing():
    messages = build_runtime_model_messages(
        system_prompt="system",
        recon_payload={"repo": "demo"},
        transcript=[
            TranscriptItem(role=RuntimeMessageRole.USER, content="inspect auth"),
            TranscriptItem(role=RuntimeMessageRole.ASSISTANT, content="I will read the file."),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_USE,
                content="",
                name="Read",
                payload={
                    "tool_use_id": "tool-use-1",
                    "tool_name": "Read",
                    "input": {"path": "src/auth.py"},
                },
            ),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_RESULT,
                content="token = request.headers['Authorization']",
                name="Read",
                metadata={"is_error": False},
                payload={
                    "tool_use_id": "tool-use-1",
                    "tool_call_id": "db-call-1",
                    "tool_name": "Read",
                    "input": {"path": "src/auth.py"},
                    "output": {"line_count": 1},
                },
            ),
        ],
        tool_definitions=[{"name": "Read"}],
        tool_message_format=ToolMessageFormat.OPENAI_TOOLS,
    )

    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "inspect auth"}
    assert messages[2]["role"] == "assistant"
    assert messages[2]["content"] == "I will read the file."
    assert messages[2]["tool_calls"] == [
        {
            "id": "tool-use-1",
            "type": "function",
            "function": {
                "name": "Read",
                "arguments": json.dumps({"path": "src/auth.py"}, ensure_ascii=False),
            },
        }
    ]
    assert messages[3] == {
        "role": "tool",
        "tool_call_id": "tool-use-1",
        "name": "Read",
        "content": "token = request.headers['Authorization']",
    }
    assert "先前工具请求历史" not in json.dumps(messages, ensure_ascii=False)
    assert "工具执行结果" not in json.dumps(messages, ensure_ascii=False)


def test_openai_native_tool_history_preserves_assistant_reasoning_content():
    messages = build_runtime_model_messages(
        system_prompt=None,
        recon_payload={},
        transcript=[
            TranscriptItem(role=RuntimeMessageRole.USER, content="inspect auth"),
            TranscriptItem(
                role=RuntimeMessageRole.ASSISTANT,
                content="",
                payload={"reasoning_content": "I need the skill body before auditing."},
            ),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_USE,
                content="Skill",
                name="Skill",
                payload={
                    "tool_use_id": "call_1",
                    "tool_name": "Skill",
                    "input": {"skill_ref": "code-audit-finding", "action": "body"},
                },
            ),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_RESULT,
                content="Skill body loaded",
                name="Skill",
                payload={"tool_use_id": "call_1", "tool_name": "Skill"},
            ),
        ],
        tool_definitions=[{"name": "Skill"}],
        tool_message_format=ToolMessageFormat.OPENAI_TOOLS,
    )

    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == ""
    assert messages[1]["reasoning_content"] == "I need the skill body before auditing."
    assert messages[1]["tool_calls"][0]["id"] == "call_1"


def test_openai_native_tool_history_regroups_interleaved_streaming_results_by_assistant_turn():
    messages = build_runtime_model_messages(
        system_prompt=None,
        recon_payload={},
        transcript=[
            TranscriptItem(role=RuntimeMessageRole.USER, content="inspect routes"),
            TranscriptItem(role=RuntimeMessageRole.ASSISTANT, content=""),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_USE,
                content="Read",
                name="Read",
                payload={
                    "tool_use_id": "call_00",
                    "tool_name": "Read",
                    "input": {"file_path": "server/api/DataRequestRoute.js"},
                },
            ),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_USE,
                content="Read",
                name="Read",
                payload={
                    "tool_use_id": "call_01",
                    "tool_name": "Read",
                    "input": {"file_path": "server/api/ConnectionRoute.js"},
                },
            ),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_RESULT,
                content="data route",
                name="Read",
                payload={"tool_use_id": "call_00", "tool_name": "Read"},
            ),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_USE,
                content="Read",
                name="Read",
                payload={
                    "tool_use_id": "call_02",
                    "tool_name": "Read",
                    "input": {"file_path": "server/api/UserRoute.js"},
                },
            ),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_RESULT,
                content="connection route",
                name="Read",
                payload={"tool_use_id": "call_01", "tool_name": "Read"},
            ),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_RESULT,
                content="user route",
                name="Read",
                payload={"tool_use_id": "call_02", "tool_name": "Read"},
            ),
        ],
        tool_definitions=[{"name": "Read"}],
        tool_message_format=ToolMessageFormat.OPENAI_TOOLS,
    )

    assert [message["role"] for message in messages] == ["user", "assistant", "tool", "tool", "tool"]
    assert [tool_call["id"] for tool_call in messages[1]["tool_calls"]] == ["call_00", "call_01", "call_02"]
    assert [message["tool_call_id"] for message in messages[2:]] == ["call_00", "call_01", "call_02"]
    assert [message["content"] for message in messages[2:]] == ["data route", "connection route", "user route"]


def test_anthropic_native_tool_history_uses_tool_blocks():
    messages = build_runtime_model_messages(
        system_prompt=None,
        recon_payload={},
        transcript=[
            TranscriptItem(role=RuntimeMessageRole.ASSISTANT, content="Checking."),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_USE,
                content="",
                name="Grep",
                payload={
                    "tool_use_id": "tool-use-2",
                    "tool_name": "Grep",
                    "input": {"pattern": "jwt.verify"},
                },
            ),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_RESULT,
                content="src/auth.js:42",
                name="Grep",
                payload={"tool_use_id": "tool-use-2", "tool_name": "Grep"},
            ),
        ],
        tool_definitions=[{"name": "Grep"}],
        tool_message_format=ToolMessageFormat.ANTHROPIC_BLOCKS,
    )

    assert messages == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking."},
                {
                    "type": "tool_use",
                    "id": "tool-use-2",
                    "name": "Grep",
                    "input": {"pattern": "jwt.verify"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-use-2",
                    "content": "src/auth.js:42",
                }
            ],
        },
    ]


def test_anthropic_native_tool_history_regroups_interleaved_streaming_results_by_assistant_turn():
    messages = build_runtime_model_messages(
        system_prompt=None,
        recon_payload={},
        transcript=[
            TranscriptItem(role=RuntimeMessageRole.ASSISTANT, content=""),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_USE,
                content="Read",
                name="Read",
                payload={"tool_use_id": "call_00", "tool_name": "Read", "input": {"file_path": "a.js"}},
            ),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_USE,
                content="Read",
                name="Read",
                payload={"tool_use_id": "call_01", "tool_name": "Read", "input": {"file_path": "b.js"}},
            ),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_RESULT,
                content="a",
                name="Read",
                payload={"tool_use_id": "call_00", "tool_name": "Read"},
            ),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_USE,
                content="Read",
                name="Read",
                payload={"tool_use_id": "call_02", "tool_name": "Read", "input": {"file_path": "c.js"}},
            ),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_RESULT,
                content="b",
                name="Read",
                payload={"tool_use_id": "call_01", "tool_name": "Read"},
            ),
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_RESULT,
                content="c",
                name="Read",
                payload={"tool_use_id": "call_02", "tool_name": "Read"},
            ),
        ],
        tool_definitions=[{"name": "Read"}],
        tool_message_format=ToolMessageFormat.ANTHROPIC_BLOCKS,
    )

    assert [block["id"] for block in messages[0]["content"]] == ["call_00", "call_01", "call_02"]
    assert messages[1] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "call_00", "content": "a"},
            {"type": "tool_result", "tool_use_id": "call_01", "content": "b"},
            {"type": "tool_result", "tool_use_id": "call_02", "content": "c"},
        ],
    }


def test_orphan_tool_result_is_not_sent_as_model_visible_user_text():
    messages = build_runtime_model_messages(
        system_prompt=None,
        recon_payload={},
        transcript=[
            TranscriptItem(
                role=RuntimeMessageRole.TOOL_RESULT,
                content="orphan output",
                name="Read",
                payload={"tool_use_id": "missing-call", "tool_name": "Read"},
            ),
            TranscriptItem(role=RuntimeMessageRole.USER, content="continue"),
        ],
        tool_definitions=[{"name": "Read"}],
        tool_message_format=ToolMessageFormat.OPENAI_TOOLS,
    )

    assert messages == [{"role": "user", "content": "continue"}]
