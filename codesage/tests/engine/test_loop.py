"""AgentLoop tests: termination, self-healing, abort, permissions (mock LLM)."""

from pathlib import Path

import pytest

from codesage.ai import ContentBlock, StreamEvent
from codesage.core import Session
from codesage.engine import AgentLoop
from codesage.permissions import PermissionEngine, PermissionMode
from codesage.tools import Tool, ToolRegistry, ToolResult, ToolUseContext


class FakeLLM:
    """Returns a scripted sequence of events; asserts nothing about the input."""

    def __init__(self, script):
        # script: list of callables returning a list[StreamEvent]
        self.script = script
        self.calls = 0
        self.total_cost = [0.0]
        self.last_messages = None

    def stream(self, request, model="main"):
        self.last_messages = request.messages
        return self._gen()

    async def _gen(self):
        events = self.script[min(self.calls, len(self.script) - 1)](self.calls)
        self.calls += 1
        for ev in events:
            yield ev


def tool_use_event(name, tid, input_json):
    return [
        StreamEvent(type="tool_use_start", tool_use_id=tid, tool_name=name),
        StreamEvent(type="tool_use_delta", input_json_delta=input_json),
        StreamEvent(type="done", stop_reason="tool_use"),
    ]


def text_event(text="answer"):
    return [StreamEvent(type="text_delta", text=text), StreamEvent(type="done", stop_reason="end_turn")]


def thinking_only_event():
    return [StreamEvent(type="thinking_delta", thinking="hmm"), StreamEvent(type="done", stop_reason="end_turn")]


class EchoTool(Tool):
    name = "Echo"
    description = "Echoes input"
    input_schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return False

    async def _run(self, input, ctx):
        return ToolResult(f"echo:{input['text']}")


class ExplodingTool(Tool):
    name = "Boom"
    description = "Always fails"
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return False

    async def _run(self, input, ctx):
        raise RuntimeError("kaboom")


def _loop(llm, tools=None, **kw):
    registry = ToolRegistry(tools or [EchoTool()])
    return AgentLoop(client=llm, tools=registry, permissions=PermissionEngine(), **kw)


async def _collect(loop, user_input="hi"):
    return [m async for m in loop.run(user_input)]


async def test_single_turn_final_answer():
    llm = FakeLLM([lambda i: text_event("hello")])
    messages = await _collect(_loop(llm))
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].content[0].text == "hello"


async def test_multi_turn_tool_flow():
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("got it"),
        ]
    )
    messages = await _collect(_loop(llm))
    roles = [m.role for m in messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    # tool round carries the result
    tool_round = messages[2]
    assert tool_round.content[0].type == "tool_result"
    assert tool_round.content[0].content == "echo:x"
    assert llm.calls == 2


async def test_tool_failure_self_heals():
    """A failing tool becomes an error tool_result; the model sees it and continues."""
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Boom", "t1", "{}"),
            lambda i: text_event("it failed, moving on"),
        ]
    )
    messages = await _collect(_loop(llm, tools=[ExplodingTool()]))
    assert llm.calls == 2
    tool_round = messages[2]
    assert tool_round.content[0].is_error
    assert "kaboom" in str(tool_round.content[0].content)


async def test_unknown_tool_reported_to_model():
    llm = FakeLLM(
        [
            lambda i: tool_use_event("NoSuchTool", "t1", "{}"),
            lambda i: text_event("ok"),
        ]
    )
    messages = await _collect(_loop(llm))
    assert messages[2].content[0].is_error
    assert "Unknown tool" in str(messages[2].content[0].content)


async def test_max_turns_terminates():
    llm = FakeLLM([lambda i: tool_use_event("Echo", f"t{i}", '{"text": "x"}')])
    loop = _loop(llm, max_turns=3)
    messages = await _collect(loop)
    assert len(messages) >= 3
    assert messages[-1].content == "Stopped: maximum turn count reached."
    assert llm.calls == 3


async def test_abort_interrupts():
    llm = FakeLLM([lambda i: tool_use_event("Echo", f"t{i}", '{"text": "x"}')])

    async def collect():
        return [m async for m in loop.run("hi")]

    loop = _loop(llm, max_turns=100)
    loop.abort.set()
    messages = await collect()
    assert messages[-1].content == "(interrupted by user)"
    assert messages[-1].is_meta


async def test_denied_tool_reported():
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Bash", "t1", '{"command": "ls"}'),
            lambda i: text_event("ok"),
        ]
    )
    from codesage.tools.builtin.shell.bash import BashTool

    loop = _loop(llm, tools=[BashTool()])
    loop.settings = None
    messages = await _collect(loop)
    assert messages[2].content[0].is_error
    assert "Permission denied" in str(messages[2].content[0].content)


async def test_thinking_only_retries_then_succeeds():
    """Two thinking-only replies get recovery nudges; the third call answers."""
    from codesage.engine.loop import THINKING_ONLY_RECOVERY

    llm = FakeLLM([lambda i: thinking_only_event(), lambda i: thinking_only_event(), lambda i: text_event("final answer")])
    messages = await _collect(_loop(llm))
    assert llm.calls == 3
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert messages[2].content == THINKING_ONLY_RECOVERY
    assert messages[4].content == THINKING_ONLY_RECOVERY
    assert messages[-1].content[0].text == "final answer"


async def test_thinking_only_gives_up_after_3():
    from codesage.engine.loop import THINKING_ONLY_GIVE_UP

    llm = FakeLLM([lambda i: thinking_only_event()])
    messages = await _collect(_loop(llm))
    assert llm.calls == 3
    assert messages[-1].content == THINKING_ONLY_GIVE_UP
    assert messages[-1].is_meta


async def test_thinking_only_retries_do_not_consume_turns():
    """max_turns=1 still allows the bounded retry to answer."""
    llm = FakeLLM([lambda i: thinking_only_event(), lambda i: text_event("finally")])
    messages = await _collect(_loop(llm, max_turns=1))
    assert llm.calls == 2
    assert messages[-1].content[0].text == "finally"


async def test_invalid_tool_input_returns_error():
    from codesage.tools.builtin.shell.bash import BashTool

    async def approve(decision, tool, input):
        return True

    llm = FakeLLM(
        [
            lambda i: tool_use_event("Bash", "t1", '{"command": "ls", "timeout_ms": 999999999}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, tools=[BashTool()], request_permission=approve)
    messages = await _collect(loop)
    assert messages[2].content[0].is_error
    assert "timeout_ms must be in" in str(messages[2].content[0].content)


async def test_validation_failure_does_not_break_siblings():
    """Invalid input on one tool must not void a valid sibling (per-tool results)."""
    from codesage.tools.builtin.shell.bash import BashTool

    async def approve(decision, tool, input):
        return True

    llm = FakeLLM(
        [
            lambda i: tool_use_event("Bash", "t0", '{"command": "ls", "timeout_ms": 999999999}')
            + tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, tools=[BashTool(), EchoTool()], request_permission=approve)
    messages = await _collect(loop)
    # per-tool tool_result messages: Bash error first, Echo result second
    assert messages[2].content[0].tool_use_id == "t0"
    assert messages[2].content[0].is_error
    assert "timeout_ms must be in" in str(messages[2].content[0].content)
    assert messages[3].content[0].tool_use_id == "t1"
    assert not messages[3].content[0].is_error
    assert messages[3].content[0].content == "echo:x"


async def test_session_persistence(tmp_path):
    session = Session("s1", tmp_path)
    llm = FakeLLM([lambda i: text_event("hello")])
    loop = _loop(llm, session=session)
    await _collect(loop)
    assert len(session.load()) == 2  # user + assistant


async def test_ask_approved_via_callback():
    approvals = []

    async def approve(decision, tool, input):
        approvals.append(tool.name)
        return True

    from codesage.tools.builtin.shell.bash import BashTool

    llm = FakeLLM(
        [
            lambda i: tool_use_event("Bash", "t1", '{"command": "ls"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, tools=[BashTool()], request_permission=approve, mode=PermissionMode.DEFAULT)
    messages = await _collect(loop)
    assert approvals == ["Bash"]
    assert not messages[2].content[0].is_error  # approved, executed fine
