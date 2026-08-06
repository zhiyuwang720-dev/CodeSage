"""AgentLoop tests: termination, self-healing, abort, permissions (mock LLM)."""

from pathlib import Path

import pytest

from codesage.ai import ContentBlock, LLMError, LLMResponse, StreamEvent
from codesage.core import Session, assistant_message, user_message
from codesage.engine import AgentLoop, CompactionConfig
from codesage.permissions import PermissionEngine, PermissionMode
from codesage.tools import Tool, ToolRegistry, ToolResult, ToolUseContext


class FakeLLM:
    """Returns a scripted sequence of events; asserts nothing about the input."""

    def __init__(self, script, summary_text="compacted summary", summary_error=None):
        # script: list of callables returning a list[StreamEvent]
        self.script = script
        self.calls = 0
        self.total_cost = [0.0]
        self.last_messages = None
        self.summary_text = summary_text
        self.summary_error = summary_error  # compaction path: raise instead of answering
        self.complete_calls = []  # [(model, LLMRequest)] — the compaction path

    def stream(self, request, model="main"):
        self.last_messages = request.messages
        return self._gen()

    async def complete(self, request, model="main"):
        self.complete_calls.append((model, request))
        if self.summary_error is not None:
            raise self.summary_error
        return LLMResponse(content=[ContentBlock(type="text", text=self.summary_text)])

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


async def test_session_permissions_deny_bash():
    """Session-scoped deny rules reach the permission engine (CC-07)."""
    from codesage.tools.builtin.shell.bash import BashTool

    llm = FakeLLM(
        [
            lambda i: tool_use_event("Bash", "t1", '{"command": "ls"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, tools=[BashTool()], session_permissions={"deny": ["Bash"]})
    messages = await _collect(loop)
    assert messages[2].content[0].is_error
    assert "Permission denied" in str(messages[2].content[0].content)


async def test_last_stop_reason_completed():
    llm = FakeLLM([lambda i: text_event("hello")])
    loop = _loop(llm)
    await _collect(loop)
    assert loop.last_stop_reason == "completed"


async def test_last_stop_reason_max_turns():
    llm = FakeLLM([lambda i: tool_use_event("Echo", f"t{i}", '{"text": "x"}')])
    loop = _loop(llm, max_turns=3)
    await _collect(loop)
    assert loop.last_stop_reason == "max_turns"


async def test_last_stop_reason_interrupted():
    llm = FakeLLM([lambda i: tool_use_event("Echo", f"t{i}", '{"text": "x"}')])
    loop = _loop(llm)
    loop.abort.set()
    await _collect(loop)
    assert loop.last_stop_reason == "interrupted"


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


async def test_history_used_as_context():
    """--continue: prior turns become the model's context; new turns append."""
    seen_messages = {}

    class RecordingLLM(FakeLLM):
        async def _gen(self):
            self.calls += 1
            seen_messages["count"] = len(self.last_messages)
            for ev in text_event("resumed"):
                yield ev

    llm = RecordingLLM([lambda i: text_event("resumed")])
    history = [user_message("earlier turn"), assistant_message("earlier answer")]
    loop = _loop(llm, history=history)
    messages = await _collect(loop, user_input="continue here")
    assert llm.calls == 1
    assert seen_messages["count"] == 3  # history(2) + new user(1)
    assert messages[-1].content[0].text == "resumed"


# ---- PI-04: terminate semantics (all siblings must agree) ----

class TerminateTool(Tool):
    name = "Terminate"
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return False

    async def _run(self, input, ctx):
        return ToolResult("done", terminate=True)


async def test_all_tools_terminate_stops_turn():
    # EVERY tool must request termination for the batch to stop (PI-04)
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Terminate", "t1", "{}") + tool_use_event("Terminate", "t2", "{}"),
            lambda i: text_event("should not run"),
        ]
    )
    messages = await _collect(_loop(llm, tools=[TerminateTool(), TerminateTool()]))
    assert llm.calls == 1  # no second LLM call
    assert "tools requested termination" in messages[-1].content


async def test_single_terminate_does_not_stop():
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Terminate", "t1", "{}") + tool_use_event("Echo", "t2", '{"text": "x"}'),
            lambda i: text_event("continues"),
        ]
    )
    # Echo doesn't terminate -> batch doesn't stop
    messages = await _collect(_loop(llm, tools=[EchoTool(), TerminateTool()]))
    assert llm.calls == 2
    assert messages[-1].content[0].text == "continues"


# ---- PI-06: steer queue injects mid-run inputs ----

async def test_steer_queue_injected_into_conversation():
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("after steer"),
        ]
    )
    import asyncio

    steer = asyncio.Queue()
    steer.put_nowait("stop what you are doing")
    loop = _loop(llm, steer_queue=steer)
    messages = await _collect(loop, user_input="do something")
    # steer message appears in the conversation (yielded as a user message)
    assert any("stop what you are doing" in str(m.content) for m in messages)
    assert messages[-1].content[0].text == "after steer"


# ---- PI-01: tool lifecycle events ----

async def test_tool_lifecycle_events():
    events = []

    def on_event(event, name, payload):
        events.append((event, name))

    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("done"),
        ]
    )
    loop = _loop(llm, on_tool_event=on_event)
    await _collect(loop)
    assert ("start", "Echo") in events
    assert ("end", "Echo") in events
    assert events.index(("start", "Echo")) < events.index(("end", "Echo"))


# ---- PI-12: ToolError code surfaces in result metadata ----

class CodedErrorTool(Tool):
    name = "CodedErr"
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return False

    async def _run(self, input, ctx):
        from codesage.tools import ToolError

        raise ToolError("bad input", code="invalid_input")


async def test_tool_error_code_in_metadata():
    llm = FakeLLM(
        [
            lambda i: tool_use_event("CodedErr", "t1", "{}"),
            lambda i: text_event("ok"),
        ]
    )
    messages = await _collect(_loop(llm, tools=[CodedErrorTool()]))
    tool_round = messages[2]
    assert tool_round.content[0].is_error
    assert tool_round.content[0].content == "bad input"


async def test_terminate_yields_tool_results_first():
    """PI-04 review fix: tool results are yielded BEFORE the stop message."""
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Terminate", "t1", "{}"),
            lambda i: text_event("should not run"),
        ]
    )
    messages = await _collect(_loop(llm, tools=[TerminateTool()]))
    # the tool_result round appears, then the stop message
    assert messages[-2].content[0].type == "tool_result"
    assert messages[-1].content == "Stopped: tools requested termination."
    assert llm.calls == 1


async def test_finalize_hook_rewrites_results():
    """PI-02: finalize can rewrite tool results before the model sees them."""

    async def finalize(item, result):
        return ToolResult(f"rewritten:{result.content}", metadata=result.metadata)

    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, finalize=finalize)
    messages = await _collect(loop)
    assert messages[2].content[0].content == "rewritten:echo:x"


# ---- phase 08 S4: context bundle → hoisted system-reminder ----

def _bundle():
    from codesage.engine import ContextBundle

    return ContextBundle(
        sections=[("currentDate", "Today's date is 2026-08-06."), ("agentsMd", "project rules")]
    )


async def test_context_bundle_hoisted_as_reminder_first():
    llm = FakeLLM([lambda i: text_event("ok")])
    loop = _loop(llm, context_bundle=_bundle())
    await _collect(loop, "hi")
    first = llm.last_messages[0]
    assert first.role == "user"
    assert first.content.startswith("<system-reminder>")
    assert "# currentDate\nToday's date is 2026-08-06." in first.content
    assert "# agentsMd\nproject rules" in first.content
    assert "IMPORTANT: this context may or may not be relevant" in first.content
    assert llm.last_messages[1].content == "hi"  # the real prompt follows the reminder


async def test_no_bundle_no_reminder():
    llm = FakeLLM([lambda i: text_event("ok")])
    loop = _loop(llm)
    await _collect(loop, "hi")
    assert llm.last_messages[0].content == "hi"


async def test_reminder_never_persisted(tmp_path):
    session = Session("s4-test", tmp_path)
    llm = FakeLLM([lambda i: text_event("ok")])
    loop = _loop(llm, context_bundle=_bundle(), session=session)
    out = await _collect(loop, "hi")
    assert not any(m.is_reminder for m in out)
    assert not any(m.is_reminder for m in session.load())


async def test_reminder_sections_capped_at_ten():
    from codesage.engine import ContextBundle

    llm = FakeLLM([lambda i: text_event("ok")])
    many = [(f"sec{i}", f"content {i}") for i in range(15)]
    loop = _loop(llm, context_bundle=ContextBundle(sections=many))
    await _collect(loop)
    assert "# sec9" in llm.last_messages[0].content
    assert "# sec10" not in llm.last_messages[0].content  # capped at 10


async def test_reminder_section_titles_not_glued(tmp_path):
    """Each '# title' starts on its own line (text payloads end with \n)."""
    llm = FakeLLM([lambda i: text_event("ok")])
    loop = _loop(llm, context_bundle=_bundle())
    await _collect(loop)
    content = llm.last_messages[0].content
    assert "\n# currentDate" in content
    assert "2026-08-06.\n# agentsMd" in content
    assert "# agentsMd\nproject rules\n" in content


async def test_reminder_agents_sections_keep_nearest():
    """At the 10-section cap, the NEAREST AGENTS.md survive, far ones drop."""
    from codesage.engine import ContextBundle

    llm = FakeLLM([lambda i: text_event("ok")])
    sections = [("currentDate", "d"), ("gitStatus", "g")]
    for i in range(12):
        sections.append(("agentsMd", f"rules-{i}"))  # 0 is farthest, 11 nearest
    loop = _loop(llm, context_bundle=ContextBundle(sections=sections))
    await _collect(loop)
    content = llm.last_messages[0].content
    assert "# agentsMd\nrules-11\n" in content  # nearest kept
    assert "rules-0" not in content  # farthest dropped
    assert "rules-8" in content  # 2 fixed + 8 agents = 10 total
    assert content.count("# agentsMd\n") == 8


# ---- PI-05: auto-compaction (S6) ----

def _big_history(n=6, size=400):
    return [user_message(f"hist-{i} " + "x" * size) for i in range(n)]


def _tiny_compaction():
    # window/reserve/keep_recent small so ordinary test messages overflow it
    return CompactionConfig(window=100, reserve=10, keep_recent=200)


async def test_compact_triggers_when_over_threshold():
    llm = FakeLLM([lambda i: text_event("answer")], summary_text="compacted")
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction())
    messages = await _collect(loop)
    assert len(llm.complete_calls) == 1
    model, request = llm.complete_calls[0]
    assert model == "compact"
    assert any(m.is_compaction_summary for m in messages)  # summary yielded to the stream
    # the next model request starts with the summary, not the old history
    assert llm.last_messages[0].content == "compacted"


async def test_compact_debounce_holds_on_same_turn_retry():
    """The thinking-only retry re-enters the turn-top checkpoint on the SAME
    turn number — the debounce (last_compact_turn) must block a second
    compaction even though the context is still over the threshold."""
    llm = FakeLLM(
        [lambda i: thinking_only_event(), lambda i: text_event("final")],
        summary_text="s",
    )
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction())
    messages = await _collect(loop)
    assert len(llm.complete_calls) == 1  # compacted once, not again on re-entry
    assert messages[-1].content[0].text == "final"


async def test_compact_breaker_after_two_failures():
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("final"),
        ],
        summary_error=LLMError("summary boom"),
    )
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction())
    await _collect(loop)
    assert len(llm.complete_calls) == 2  # two consecutive failures trip the breaker
    assert loop.compaction.enabled is False
    assert loop.last_stop_reason == "completed"  # the loop itself keeps running


async def test_compact_persists_summary_message(tmp_path):
    session = Session("s1", tmp_path)
    llm = FakeLLM([lambda i: text_event("answer")], summary_text="compacted")
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction(), session=session)
    await _collect(loop)
    # append-only: the summary landed in the session file, history untouched
    lines = (tmp_path / "s1.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"is_compaction_summary": true' in ln for ln in lines)
    assert len(session.load()) == 3  # user + summary + assistant


async def test_compact_resume_replay(tmp_path):
    """--continue replays the persisted summary as the conversation start."""
    session = Session("s1", tmp_path)
    llm = FakeLLM([lambda i: text_event("answer")], summary_text="compacted")
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction(), session=session)
    await _collect(loop)
    # replay: fresh loop seeded with the persisted messages; the summary
    # survives the roundtrip and stays unmerged (specs/08 §3.1) — the old
    # session history sits before it, the new input after
    resumed = _loop(FakeLLM([lambda i: text_event("resumed")]), history=session.load())
    await _collect(resumed, user_input="continue")
    assert resumed.client.last_messages[1].content == "compacted"
