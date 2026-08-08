"""AgentLoop tests: termination, self-healing, abort, permissions (mock LLM)."""

import asyncio
import logging
from pathlib import Path

import pytest

from codesage.ai import ContentBlock, LLMError, LLMResponse, StreamEvent, Usage
from codesage.core import Session, assistant_message, normalize_for_api, user_message
from codesage.engine import AgentLoop, AgentLoopConfig, CompactionConfig
from codesage.engine.loop import OUTPUT_OVERFLOW_RECOVERY
from codesage.hooks import HookDispatchResult
from codesage.permissions import PermissionEngine, PermissionMode
from codesage.tools import Tool, ToolRegistry, ToolResult, ToolUseContext


class FakeLLM:
    """Returns a scripted sequence of events; asserts nothing about the input."""

    def __init__(self, script, summary_text="compacted summary", summary_error=None, summary_errors=None):
        # script: list of callables returning a list[StreamEvent]
        self.script = script
        self.calls = 0
        self.total_cost = [0.0]
        self.last_messages = None
        self.summary_text = summary_text
        self.summary_error = summary_error  # compaction path: raise instead of answering
        self.summary_errors = list(summary_errors) if summary_errors else None  # per-call raises
        self.complete_calls = []  # [(model, LLMRequest)] — the compaction path

    def stream(self, request, model="main"):
        self.last_messages = request.messages
        return self._gen()

    async def complete(self, request, model="main"):
        self.complete_calls.append((model, request))
        if self.summary_errors:
            raise self.summary_errors.pop(0)
        if self.summary_error is not None:
            raise self.summary_error
        return LLMResponse(content=[ContentBlock(type="text", text=self.summary_text)])

    async def _gen(self):
        # increment BEFORE invoking: a raising script entry (e.g. a PTL
        # stream error) must still consume its slot, not replay forever
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        events = self.script[idx](self.calls)
        for ev in events:
            # real streaming always suspends on I/O; a synchronous script
            # would starve concurrently scheduled tasks (e.g. prefetch)
            await asyncio.sleep(0)
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


def length_tool_use_event(text="partial reply", tid="t1", input_json='{"text": "x"}'):
    """§3.2 形态 1:length 截断 + 残缺 tool_use(PI-03 会剥除 tool_use 块)。"""
    return [
        StreamEvent(type="text_delta", text=text),
        StreamEvent(type="tool_use_start", tool_use_id=tid, tool_name="Echo"),
        StreamEvent(type="tool_use_delta", input_json_delta=input_json),
        StreamEvent(type="done", stop_reason="length"),
    ]


def length_text_event(text="partial reply"):
    """§3.2 形态 2:纯文本 length 截断。"""
    return [StreamEvent(type="text_delta", text=text), StreamEvent(type="done", stop_reason="length")]


def request_text(messages):
    """请求消息渲染为文本(断言反馈注入用)。"""
    parts = []
    for m in messages:
        if isinstance(m.content, str):
            parts.append(m.content)
        else:
            parts.extend(b.text or "" for b in m.content if b.type == "text")
    return "\n".join(parts)


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
    # 运行时 kw(实例参数,config 拒收)与构造 kw 分流
    runtime = {k: kw.pop(k) for k in ("mode", "steer_queue", "on_tool_event", "finalize") if k in kw}
    return AgentLoop(
        AgentLoopConfig(client=llm, tools=registry, permissions=PermissionEngine(), **kw),
        **runtime,
    )


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
    assert loop.last_transition == "auto_compact"  # §5.3:阈值检查点压缩(投影)


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
            lambda i: tool_use_event("Echo", "t2", '{"text": "y"}'),
            lambda i: text_event("final"),
        ],
        summary_error=LLMError("summary boom"),
    )
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction())
    await _collect(loop)
    assert len(llm.complete_calls) == 2  # two consecutive failures trip the breaker
    # §7.2:熔断收归闭包,config 只读 —— 第三次检查点(auto)被闭包挡住不再压缩
    assert loop._compaction_breaker is True
    assert loop.compaction.enabled is True  # config 字段保留且不再被运行时写
    assert loop.last_stop_reason == "completed"  # the loop itself keeps running


async def test_compaction_success_resets_breaker():
    """§7.2:压缩成功复位熔断(闭包)—— 复位是 S5 验收核心。熔断态下
    auto/PTL 读点均被闭包挡住(auto 路径无法再触发压缩),故直接调
    _compact 绕过挡点,直测成功路径的复位(loop.py:542-543)。"""
    llm = FakeLLM([lambda i: text_event("answer")], summary_text="compacted")
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction())
    loop._compaction_breaker = True  # 模拟已熔断
    loop._compact_failures = 1
    result = await loop._compact(_big_history())
    assert result is not None  # 压缩成功
    assert loop._compaction_breaker is False  # 成功即复位
    assert loop._compact_failures == 0


async def test_compact_persists_summary_message(tmp_path):
    session = Session("s1", tmp_path)
    llm = FakeLLM([lambda i: text_event("answer")], summary_text="compacted")
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction(), session=session)
    await _collect(loop)
    # append-only: the summary landed in the session file, history untouched
    lines = (tmp_path / "s1.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"is_compaction_summary": true' in ln for ln in lines)
    assert len(session.load()) == 3  # user + summary + assistant


async def test_compaction_boundary_single_summary_and_normalize_holds(tmp_path):
    """§8.2/§9.1 固化:压缩后会话含且仅含一条 is_compaction_summary(boundary
    唯一载体,不插空消息/说明消息);normalize 后摘要保位不合并
    (core/normalize.py:15,75-87 规则 5),摘要前 user 消息原样独立。"""
    session = Session("s1", tmp_path)
    llm = FakeLLM([lambda i: text_event("answer")], summary_text="compacted")
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction(), session=session)
    await _collect(loop)
    msgs = session.load()
    summaries = [m for m in msgs if m.is_compaction_summary]
    assert len(summaries) == 1  # 唯一载体:压缩恰追加一条摘要
    assert summaries[0].content == "compacted"
    norm = normalize_for_api(msgs)
    contents = [m.content for m in norm]
    assert contents.count("compacted") == 1  # 摘要独立成条,未被邻接 user 合并吞并
    assert contents[0] == "hi"  # 摘要前的 user 消息保位,未与摘要合并


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


# ---- §6.2: manual compact_now() (S6) ----

async def test_compact_now_manual_trigger_compacts_and_refreshes_projection():
    """manual 触发压缩:大窗口 run 完(auto 不触发)→ compact_now 成功,
    投影刷新、防抖/熔断不受影响;transition 写 manual_compact(§5.3,run 外写位)。"""
    llm = FakeLLM([lambda i: text_event("answer")], summary_text="compacted")
    loop = _loop(
        llm,
        history=_big_history(),
        compaction=CompactionConfig(window=10**6, reserve=10**4, keep_recent=200),
    )
    await _collect(loop)  # 窗口巨大 → auto 检查点不触发
    assert len(llm.complete_calls) == 0 and loop._last_compact_turn == -1
    ok = await loop.compact_now()
    assert ok is True
    assert len(llm.complete_calls) == 1  # 仅 manual 这一次摘要调用
    assert loop._active_messages[0].is_compaction_summary  # 投影以摘要开头
    assert loop._last_compact_turn == -1  # 防抖不被 manual 写
    assert loop._compaction_breaker is False
    assert loop.last_transition == "manual_compact"  # §5.3:run 外写位(实例投影)


async def test_compact_now_before_any_run_returns_false():
    """未 run 过(无可压缩内容)→ False,不调摘要。"""
    llm = FakeLLM([lambda i: text_event("answer")], summary_text="compacted")
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction())
    assert await loop.compact_now() is False
    assert llm.complete_calls == []


async def test_compact_now_bypasses_debounce_after_auto_compact():
    """auto 检查点已在 turn1 压缩过(_last_compact_turn=1)→ manual 仍可压
    (防抖仅 auto 检查点写/读,§6.2 天然不受限)。"""
    llm = FakeLLM([lambda i: text_event("answer")], summary_text="compacted")
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction())
    await _collect(loop)
    assert len(llm.complete_calls) == 1 and loop._last_compact_turn == 1  # auto 已压
    assert await loop.compact_now() is True  # manual 绕过防抖
    assert len(llm.complete_calls) == 2
    assert loop._last_compact_turn == 1  # manual 不写防抖


async def test_compact_now_bypasses_breaker_and_success_resets_it():
    """熔断闭包只挡 auto 读点(§7.1 manual 恒可用):两次 auto 失败熔断后,
    manual 照常触发且成功即复位闭包。"""
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: tool_use_event("Echo", "t2", '{"text": "y"}'),
            lambda i: text_event("final"),  # 终结轮(否则脚本耗尽重放 → max_turns 空转)
        ],
        summary_errors=[LLMError("boom 1"), LLMError("boom 2")],  # 前两次失败,第三次成功
    )
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction())
    await _collect(loop)
    assert loop._compaction_breaker is True  # 两次连续失败已熔断
    assert await loop.compact_now() is True  # manual 不被闭包挡
    assert len(llm.complete_calls) == 3  # auto×2 失败 + manual 成功
    assert loop._compaction_breaker is False  # §7.2:压缩成功即复位
    assert loop._compact_failures == 0


# ---- §5.3: transition reason 写位 + --verbose 日志 (S8) ----

def _transitions(caplog):
    """从 caplog 提取 transition 日志行(按序)。"""
    return [
        r.getMessage().split("transition: ", 1)[1]
        for r in caplog.records
        if r.getMessage().startswith("transition: ")
    ]


async def test_transition_user_input_tool_result_logged_and_projected(caplog):
    """§5.3:run 入口 user_input → 工具返回 tool_result;--verbose 逐行日志;
    run() finally 投影到实例 = 最后一次迁移。"""
    caplog.set_level(logging.INFO, logger="codesage.engine")
    llm = FakeLLM(
        [lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'), lambda i: text_event("final")],
        summary_text="s",
    )
    loop = _loop(
        llm,
        history=_big_history(),
        compaction=CompactionConfig(window=10**6, reserve=10**4, keep_recent=200),  # 大窗口:auto 不触发
    )
    await _collect(loop)
    assert loop.last_transition == "tool_result"  # 投影 = 最后一次迁移
    assert _transitions(caplog) == ["user_input", "tool_result"]


async def test_transition_output_overflow_and_truncated(caplog):
    """§3.2:形态 1(残缺 tool_use)重发 → output_overflow;形态 2(纯文本截断)
    不恢复 → output_overflow_truncated。"""
    caplog.set_level(logging.INFO, logger="codesage.engine")
    big = CompactionConfig(window=10**6, reserve=10**4, keep_recent=200)
    llm = FakeLLM([lambda i: length_tool_use_event(), lambda i: text_event("ok")], summary_text="s")
    loop = _loop(llm, history=_big_history(), compaction=big)
    await _collect(loop)
    assert loop.last_transition == "output_overflow"
    caplog.clear()
    llm2 = FakeLLM([lambda i: length_text_event(), lambda i: text_event("ok")], summary_text="s")
    loop2 = _loop(llm2, history=_big_history(), compaction=big)
    await _collect(loop2)
    assert loop2.last_transition == "output_overflow_truncated"
    assert _transitions(caplog) == ["user_input", "output_overflow_truncated"]


async def test_transition_ptl_compact_and_error_terminate(caplog):
    """PTL 反应式 → ptl_compact;PTL 恢复闸已尽(第二次)→ raise 落原错误路径
    → error_terminate。注:非 PTL 的 provider error 走 is_error 响应(loop.py:674
    只对 PTL 文本 raise),不进 LLMError 路径。"""
    caplog.set_level(logging.INFO, logger="codesage.engine")
    big = CompactionConfig(window=10**6, reserve=10**4, keep_recent=200)
    llm = FakeLLM([_ptl_stream, lambda i: text_event("answer")], summary_text="s")
    loop = _loop(llm, history=_big_history(), compaction=big)
    await _collect(loop)
    assert loop.last_transition == "ptl_compact"
    assert "transition: ptl_compact" in caplog.text
    caplog.clear()
    llm2 = FakeLLM([_ptl_stream, _ptl_stream], summary_text="s")
    loop2 = _loop(llm2, history=_big_history(), compaction=big)
    await _collect(loop2)
    assert loop2.last_transition == "error_terminate"  # 第二次 PTL 闸尽 → 原错误路径
    assert "transition: error_terminate" in caplog.text


# ---- PI-05 §3.6/§3.7: recovery reminder + old-result cleanup (S7) ----

def _edit_history(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("file content v1", encoding="utf-8")
    return [
        user_message("edit x.txt"),
        assistant_message(
            [ContentBlock(type="tool_use", id="e1", name="Edit", input={"file_path": "x.txt"})]
        ),
        user_message([ContentBlock(type="tool_result", tool_use_id="e1", content="edited", is_error=False)]),
        assistant_message("edited x.txt"),
        user_message("long filler " + "x" * 500),
    ]


async def test_compact_injects_recovery_reminder(tmp_path):
    """After compaction the next request carries the modified file's content."""
    llm = FakeLLM([lambda i: text_event("answer")], summary_text="compacted")
    loop = _loop(
        llm,
        cwd=tmp_path,
        history=_edit_history(tmp_path),
        compaction=CompactionConfig(window=100, reserve=10, keep_recent=20),
    )
    await _collect(loop)
    assert llm.complete_calls  # compaction happened
    reminder = llm.last_messages[0].content
    assert "Recently modified files:" in reminder
    assert "# x.txt" in reminder
    assert "file content v1" in reminder


async def test_recovery_reminder_injected_once():
    """The recovery reminder is one-shot: only the request right after
    compaction carries it, later turns do not."""
    seen = []

    class RecordingLLM(FakeLLM):
        async def _gen(self):
            seen.append(self.last_messages)
            async for ev in super()._gen():
                yield ev

    llm = RecordingLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("final"),
        ],
        summary_text="s",
    )
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction())
    await _collect(loop)
    assert "Recently modified files:" not in (seen[-1][0].content or "")  # second turn: gone


async def test_cleanup_is_request_view_only(tmp_path):
    """Old tool results are cleared in the request view; the session log
    keeps the full payload (append-only invariant, specs/08 §3.7)."""
    session = Session("s1", tmp_path)
    history = []
    for i in range(25):  # 75 messages > 60 threshold; Read is whitelisted
        history += [
            user_message(f"q{i}"),
            assistant_message(
                [ContentBlock(type="tool_use", id=f"e{i}", name="Read", input={"file_path": f"f{i}.py"})]
            ),
            user_message([ContentBlock(type="tool_result", tool_use_id=f"e{i}", content=f"out:{i}")]),
        ]
    for m in history:
        session.append(m)  # the log already holds the full payloads
    llm = FakeLLM([lambda i: text_event("answer")])
    loop = _loop(
        llm, history=history, compaction=CompactionConfig(enabled=False), session=session
    )
    await _collect(loop)
    # request view carries the placeholder for the oldest results
    from codesage.engine.compaction import OLD_RESULT_PLACEHOLDER

    placeholders = [
        block.content
        for msg in llm.last_messages
        if isinstance(msg.content, list)
        for block in msg.content
        if block.type == "tool_result" and block.content == OLD_RESULT_PLACEHOLDER
    ]
    assert placeholders  # cleanup projection happened
    # the session log still holds the original payloads
    persisted = session.load()
    assert any(
        isinstance(m.content, list) and any(b.content == "out:0" for b in m.content)
        for m in persisted
    )


async def test_microcompact_before_autocompact_prevents_summary():
    """CC pipeline order (query.ts:379-468 — microcompact before autocompact):
    the cheap cleanup runs at the checkpoint BEFORE the threshold decision,
    and the estimate uses the cleaned view. When cleanup frees enough tokens,
    the LLM summary never fires. Regression: the old code estimated the raw
    view and compacted regardless."""
    from codesage.engine.compaction import OLD_RESULT_PLACEHOLDER
    from codesage.engine.tokens import estimate_context_tokens

    history = []
    for i in range(40):  # 120 messages > the 60-message cleanup gate
        content = "x" * 10_000 if i < 20 else f"out:{i}"  # 20 oldest results huge
        history += [
            user_message(f"q{i}"),
            assistant_message(
                [ContentBlock(type="tool_use", id=f"e{i}", name="Read", input={"file_path": f"f{i}.py"})]
            ),
            user_message([ContentBlock(type="tool_result", tool_use_id=f"e{i}", content=content)]),
        ]
    # sanity: the RAW view is far over the threshold (old code would compact)
    assert estimate_context_tokens(history).tokens > 5_000 - 100
    llm = FakeLLM([lambda i: text_event("answer")])
    loop = _loop(
        llm,
        history=history,
        compaction=CompactionConfig(window=5_000, reserve=100, keep_recent=200),
    )
    messages = await _collect(loop)
    assert llm.complete_calls == []  # summary never requested — cleanup sufficed
    assert not any(m.is_compaction_summary for m in messages)
    # the request view still carries the cleaned projection: exactly the 20
    # oldest results placeholder-ized, the newest 20 kept intact
    placeholders = [
        block.content
        for msg in llm.last_messages
        if isinstance(msg.content, list)
        for block in msg.content
        if block.type == "tool_result" and block.content == OLD_RESULT_PLACEHOLDER
    ]
    assert len(placeholders) == 20
    intact = [
        block.content
        for msg in llm.last_messages
        if isinstance(msg.content, list)
        for block in msg.content
        if block.type == "tool_result" and block.content == "out:39"
    ]
    assert intact  # the newest result survived the cleanup


async def test_compact_summarizes_raw_span_not_cleaned_view():
    """The LLM summary is generated from the RAW messages, not the cleaned
    projection — the placeholder must never reach the compaction prompt."""
    from codesage.engine.compaction import OLD_RESULT_PLACEHOLDER

    history = []
    for i in range(40):
        content = "x" * 10_000 if i < 20 else f"out:{i}"  # 20 oldest results huge
        history += [
            user_message(f"q{i}"),
            assistant_message(
                [ContentBlock(type="tool_use", id=f"e{i}", name="Read", input={"file_path": f"f{i}.py"})]
            ),
            user_message([ContentBlock(type="tool_result", tool_use_id=f"e{i}", content=content)]),
        ]
    llm = FakeLLM([lambda i: text_event("answer")], summary_text="compacted")
    loop = _loop(llm, history=history, compaction=_tiny_compaction())
    messages = await _collect(loop)
    assert llm.complete_calls  # cleanup freed a lot, but the window is tiny — summary fires
    assert any(m.is_compaction_summary for m in messages)
    prompt = llm.complete_calls[0][1].messages[0].content
    assert "truncated, 10000 chars" in prompt  # the raw payload reached the summary
    assert OLD_RESULT_PLACEHOLDER not in prompt


# ---- PI-05 §3.8: reactive compaction on Prompt-Too-Long ----

def _ptl_stream(i):
    # PRODUCTION shape: adapters surface HTTP >= 400 as a streamed error
    # event, never a raise; _ask_model converts it to the exception shape
    # (review C1 — a raise-mode-only test would never see the real path)
    return [StreamEvent(type="error", error="HTTP 400: context_length_exceeded")]


async def test_ptl_forces_compact_and_retries():
    """A PTL on the main query forces a compact (skipping the threshold
    check) and retries once — the session survives an oversized turn."""
    llm = FakeLLM([_ptl_stream, lambda i: text_event("answer")], summary_text="compacted")
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction())
    messages = await _collect(loop)
    assert llm.complete_calls  # forced compaction ran
    assert any(m.is_compaction_summary for m in messages)
    assert messages[-1].content[0].text == "answer"
    assert loop.last_stop_reason == "completed"


async def test_ptl_recovers_once_then_terminates():
    """The second PTL is not recovered: the loop exits on the normal
    provider-error path (last_stop_reason="error"), not an infinite loop."""
    llm = FakeLLM([_ptl_stream, _ptl_stream], summary_text="compacted")
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction())
    messages = await _collect(loop)
    assert loop.last_stop_reason == "error"
    assert messages[-1].is_error


async def test_ptl_without_compaction_enabled_still_errors():
    """Compaction disabled (config None) must not crash the recovery path —
    _compact is None-guarded, the PTL propagates to the error message."""
    llm = FakeLLM([_ptl_stream], summary_text="compacted")
    loop = _loop(llm, history=_big_history(), compaction=None)
    messages = await _collect(loop)
    assert loop.last_stop_reason == "error"


async def test_summary_ptl_truncates_head_and_retries_once():
    """The summary request itself can hit PTL on a huge conversation: drop
    the oldest turn, re-serialize, retry exactly once (specs/08 §3.8)."""
    ptl = LLMError("HTTP 400: context_length_exceeded", status_code=400)
    llm = FakeLLM([lambda i: text_event("answer")], summary_errors=[ptl])
    # 6 turns; keep_recent=60 lands the cut on the 5th turn's user message,
    # so the compressed span holds 4 full turns (no split-turn prefix request)
    history = []
    for n in range(1, 7):
        history += [user_message(f"turn-{n} " + "x" * 183), assistant_message(f"a{n}")]
    loop = _loop(
        llm,
        history=history,
        compaction=CompactionConfig(window=100, reserve=10, keep_recent=60),
    )
    messages = await _collect(loop)
    assert len(llm.complete_calls) == 2  # PTL attempt + head-trimmed retry
    assert any(m.is_compaction_summary for m in messages)
    first, second = llm.complete_calls[0][1].messages[0].content, llm.complete_calls[1][1].messages[0].content
    assert "turn-1" not in second and "turn-2" in second  # oldest turn dropped
    assert "turn-1" in first  # the original attempt carried everything


# ---- §3.9: cache-break detection (light diagnostic) ----

def _usage_tool_event(cache_read):
    return [
        StreamEvent(type="text_delta", text="thinking"),
        StreamEvent(type="usage", usage=Usage(cache_read_tokens=cache_read, input_tokens=100, output_tokens=10)),
        StreamEvent(type="tool_use_start", tool_use_id="t1", tool_name="Echo"),
        StreamEvent(type="tool_use_delta", input_json_delta='{"text": "x"}'),
        StreamEvent(type="done", stop_reason="tool_use"),
    ]


async def test_cache_break_logs_once_per_drop(caplog):
    """A cache_read drop to 0 after a high value logs one diagnostic per
    drop; steady 0s and recoveries do not (specs/08 §3.9)."""
    import logging

    caplog.set_level(logging.WARNING, logger="codesage.engine")
    llm = FakeLLM(
        [
            lambda i: _usage_tool_event(5000),
            lambda i: _usage_tool_event(0),
            lambda i: _usage_tool_event(0),
            lambda i: _usage_tool_event(8000),
            lambda i: _usage_tool_event(0),
            lambda i: text_event("final"),
        ]
    )
    await _collect(_loop(llm))
    breaks = [r for r in caplog.records if "cache break" in r.getMessage()]
    assert len(breaks) == 2  # 5000->0 and 8000->0; the 0->0 and 0->8000 do not
    assert "dropped 5000" in breaks[0].getMessage()
    assert "dropped 8000" in breaks[1].getMessage()


async def test_cache_break_silent_when_never_high(caplog):
    """No log when cache_read is 0 from the start (nothing dropped)."""
    import logging

    caplog.set_level(logging.WARNING, logger="codesage.engine")
    llm = FakeLLM(
        [
            lambda i: _usage_tool_event(0),
            lambda i: text_event("final"),
        ]
    )
    await _collect(_loop(llm))
    assert not [r for r in caplog.records if "cache break" in r.getMessage()]


# ---- §3.10: memory prefetch interface ----

async def test_prefetch_injected_next_turn():
    """A prefetch launched during turn 1 finishes during the tool round and
    rides turn 2's request — CC injects attachments after tool execution so
    they appear in the NEXT API call's context (specs/08 §3.10)."""
    seen = []

    async def prefetch(messages):
        seen.append(messages)
        return [("memory", "remember this fact"), ("skills", "skill list")]

    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("answer"),
        ]
    )
    loop = _loop(llm, prefetch=prefetch)
    await _collect(loop)
    assert seen  # the callback received the message history
    # appended at the END of the history (before normalize): appending never
    # breaks the server prefix cache — a leading reminder would (review O1)
    texts = [
        b.text or ""
        for m in llm.last_messages
        if isinstance(m.content, list)
        for b in m.content
        if b.type == "text"
    ]
    assert any("# memory\nremember this fact" in t for t in texts)
    assert any("# skills\nskill list" in t for t in texts)


async def test_prefetch_sections_capped(monkeypatch):
    """More sections than MAX_REMINDER_SECTIONS are truncated at injection."""
    from codesage.engine.loop import MAX_REMINDER_SECTIONS

    async def prefetch(messages):
        return [(f"sec-{i}", "x") for i in range(MAX_REMINDER_SECTIONS + 5)]

    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("answer"),
        ]
    )
    loop = _loop(llm, prefetch=prefetch)
    await _collect(loop)
    texts = [
        b.text or ""
        for m in llm.last_messages
        if isinstance(m.content, list)
        for b in m.content
        if b.type == "text"
    ]
    assert any(t.count("# sec-") == MAX_REMINDER_SECTIONS for t in texts)


async def test_prefetch_timeout_does_not_block(monkeypatch):
    """A hung prefetch bails out at PREFETCH_TIMEOUT_S and never touches the
    request view (specs/08 §3.10 — interface must not block the main chain)."""
    from codesage.engine import loop as loop_module

    monkeypatch.setattr(loop_module, "PREFETCH_TIMEOUT_S", 0.01)

    async def slow(messages):
        await asyncio.sleep(30)
        return [("memory", "never")]

    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("answer"),
        ]
    )
    loop = _loop(llm, prefetch=slow)
    messages = await _collect(loop)
    assert messages[-1].content[0].text == "answer"  # main chain unaffected
    assert not any("# memory" in str(m.content) for m in llm.last_messages)


async def test_prefetch_callback_failure_swallowed():
    """A raising prefetch callback is swallowed — no crash, no injection."""

    async def broken(messages):
        raise RuntimeError("search failed")

    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("answer"),
        ]
    )
    loop = _loop(llm, prefetch=broken)
    messages = await _collect(loop)
    assert messages[-1].content[0].text == "answer"
    assert llm.last_messages[0].content == "hi"  # plain user message, no reminder


async def test_prefetch_completing_during_stream_survives_to_next_turn():
    """A prefetch that finishes mid-stream of turn N+1 must survive to turn
    N+2 — previously _start_prefetch rebinding dropped the completed result
    (review R2: done-but-unconsumed tasks are kept)."""
    seen_requests = []
    gate = asyncio.Event()

    class RecordingLLM(FakeLLM):
        def stream(self, request, model="main"):
            if self.calls == 1:  # turn 2: release the prefetch mid-stream
                gate.set()
            return super().stream(request, model)

        async def _gen(self):
            seen_requests.append(list(self.last_messages))
            async for ev in super()._gen():
                yield ev

    async def slow(messages):
        await gate.wait()  # completes during turn 2's streaming
        return [("memory", "found it")]

    llm = RecordingLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: tool_use_event("Echo", "t2", '{"text": "x"}'),
            lambda i: tool_use_event("Echo", "t3", '{"text": "x"}'),
            lambda i: text_event("final"),
        ]
    )
    loop = _loop(llm, prefetch=slow)
    await _collect(loop)
    # turn 3's request (index 2) carries the turn-1-launched result
    texts = [
        b.text or ""
        for m in seen_requests[2]
        if isinstance(m.content, list)
        for b in m.content
        if b.type == "text"
    ]
    assert any("# memory\nfound it" in t for t in texts)


async def test_active_messages_exposed_and_updated_after_compact():
    """The CLI status bar's ctx meter reads loop._active_messages; a
    compaction must replace it so the meter reflects the post-compact size."""
    llm = FakeLLM([lambda i: text_event("answer")], summary_text="compacted")
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction())
    await _collect(loop)
    assert loop._active_messages is not None
    assert loop._active_messages[0].is_compaction_summary  # compacted view
    assert loop._active_messages[-1].content[0].text == "answer"


# ---- review fixes: P2/P3 batch ----

async def test_max_turns_stop_is_meta():
    """Stop notices are is_meta like the interrupt notice — they must never
    enter the API as a real assistant reply on --continue (review P2-2)."""
    llm = FakeLLM([lambda i: tool_use_event("Echo", f"t{i}", '{"text": "x"}')])
    loop = _loop(llm, max_turns=2)
    messages = await _collect(loop)
    assert messages[-1].content == "Stopped: maximum turn count reached."
    assert messages[-1].is_meta


async def test_max_budget_stop_is_meta():
    llm = FakeLLM([lambda i: text_event("answer")])
    llm.total_cost[0] = 99.0
    loop = _loop(llm, max_budget_usd=1.0)
    messages = await _collect(loop)
    assert messages[-1].content == "Stopped: maximum budget reached."
    assert messages[-1].is_meta


def test_max_turns_invalid_value_raises():
    """A mistyped config must fail loudly, not silently become 100 turns."""
    with pytest.raises(ValueError):
        _loop(FakeLLM([lambda i: text_event("x")]), max_turns=0)
    with pytest.raises(ValueError):
        _loop(FakeLLM([lambda i: text_event("x")]), max_turns="lots")
    # None = unspecified default
    assert _loop(FakeLLM([lambda i: text_event("x")]), max_turns=None).max_turns == 100


async def test_error_response_stop_reason_is_error_not_completed():
    """A provider error response with no text is not a completed turn —
    last_stop_reason must say "error" (review P3-7)."""
    llm = FakeLLM([lambda i: [StreamEvent(type="error", error="HTTP 500: boom")]])
    loop = _loop(llm)
    messages = await _collect(loop)
    assert loop.last_stop_reason == "error"
    assert messages[-1].is_error


async def test_reused_loop_instance_resets_per_run_state():
    """PTL retry and cache-read tracking reset between runs on the same
    instance (review P3-8)."""
    loop = _loop(FakeLLM([_ptl_stream, lambda i: text_event("ok")]), history=_big_history(), compaction=_tiny_compaction())
    await _collect(loop)
    # second run on the same instance gets a fresh retry budget (per-run
    # state lives in RunState now — observable via the retry happening again)
    loop.client = FakeLLM([_ptl_stream, lambda i: text_event("ok2")], summary_text="s")
    messages = await _collect(loop)
    assert any(m.is_compaction_summary for m in messages)
    assert messages[-1].content[0].text == "ok2"


class GuardedSafeTool(Tool):
    """Concurrency-safe tool that requires permission (sibling-batch tests)."""

    name = "GuardedSafe"
    description = "Guarded safe tool"
    input_schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return True

    async def _run(self, input, ctx):
        return ToolResult(f"ran:{input['text']}")


async def test_permission_denial_does_not_void_siblings():
    """Denying one tool is a user decision, not an execution error — the
    sibling's own permission gate must still get a chance (review P3-6).
    Both tools are concurrency-safe so they share a batch."""
    denied = []

    async def deny_first(decision, tool, tool_input):
        denied.append(tool.name)  # every tool reaches its own gate
        if tool.name == "GuardedSafe":
            return tool_input["text"] == "allow"  # deny the "deny" one
        return True

    class GuardedDeny(GuardedSafeTool):
        name = "GuardedDeny"

    llm = FakeLLM(
        [
            lambda i: [
                StreamEvent(type="tool_use_start", tool_use_id="a1", tool_name="GuardedSafe"),
                StreamEvent(type="tool_use_delta", input_json_delta='{"text": "deny"}'),
                StreamEvent(type="tool_use_start", tool_use_id="b1", tool_name="GuardedDeny"),
                StreamEvent(type="tool_use_delta", input_json_delta='{"text": "allow"}'),
                StreamEvent(type="done", stop_reason="tool_use"),
            ],
            lambda i: text_event("final"),
        ]
    )
    loop = _loop(llm, tools=[GuardedSafeTool(), GuardedDeny()])
    loop.request_permission = deny_first
    messages = await _collect(loop)
    assert denied == ["GuardedSafe", "GuardedDeny"]  # both reached their own gate
    results = [
        b
        for m in messages
        if isinstance(m.content, list)
        for b in m.content
        if b.type == "tool_result"
    ]
    by_id = {b.tool_use_id: b for b in results}
    assert by_id["a1"].is_error  # the denied one reports the denial
    assert not by_id["b1"].is_error  # the sibling ran with a real result
    assert by_id["b1"].content == "ran:allow"


# ---- 阶段 09 S6:引擎接线测试(mock HookManager 注入,不真跑钩子) ----


class FakeHooks:
    """脚本化 HookManager 假件:按事件路由,记录 dispatch 调用(阶段 09 S6/S7)。

    S7 事件(SessionStart/UserPromptSubmit/Stop)的结果按列表逐个消费,耗尽后
    返回空结果(无决策 → 流程照常),便于「feedback 一次后放行」类脚本。
    """

    def __init__(
        self,
        pre_result=None,
        events=("PreToolUse", "PostToolUse"),
        session_results=None,
        submit_results=None,
        stop_results=None,
    ):
        self.pre_result = pre_result  # HookDispatchResult 或 None(无决策 → 引擎照常)
        self.events = list(events)  # 已配置事件(has_hooks_for_event 依据,§4.10.1)
        self.session_results = list(session_results) if session_results else []
        self.submit_results = list(submit_results) if submit_results else []
        self.stop_results = list(stop_results) if stop_results else []
        self.pre_calls = 0
        self.post_calls = 0
        self.session_calls = 0
        self.submit_calls = 0
        self.stop_calls = 0
        self.last_input = None
        self.abort_event = None

    def has_hooks_for_event(self, event):
        return event in self.events

    async def dispatch(self, event, *, input, abort_event=None):
        self.abort_event = abort_event  # §6.3:abort 感知接线(断言用)
        self.last_input = input
        if event == "PreToolUse":
            self.pre_calls += 1
            return self.pre_result if self.pre_result is not None else HookDispatchResult(event="PreToolUse")
        if event == "PostToolUse":
            self.post_calls += 1
            return HookDispatchResult(event="PostToolUse")
        if event == "SessionStart":
            self.session_calls += 1
            r = self.session_results.pop(0) if self.session_results else None
            return r if r is not None else HookDispatchResult(event="SessionStart")
        if event == "UserPromptSubmit":
            self.submit_calls += 1
            r = self.submit_results.pop(0) if self.submit_results else None
            return r if r is not None else HookDispatchResult(event="UserPromptSubmit")
        if event == "Stop":
            self.stop_calls += 1
            r = self.stop_results.pop(0) if self.stop_results else None
            return r if r is not None else HookDispatchResult(event="Stop")
        raise AssertionError(f"unexpected event {event}")


def pre_allow(updated_input=None, immune=False):
    r = HookDispatchResult(event="PreToolUse")
    r.permission_decision = "allow"
    r.allow_hook = "fake"
    r.hook_allowed = True
    r.immune = immune
    r.updated_input = updated_input
    return r


def pre_deny(reason="no"):
    r = HookDispatchResult(event="PreToolUse")
    r.permission_decision = "deny"
    r.deny_hook = "fake"
    r.deny_reason = f"Permission denied by hook fake: {reason}"
    return r


def pre_passthrough(updated_input=None):
    """无决策(passthrough)钩子:仅改写输入,引擎照常求值(§5.4)。"""
    r = HookDispatchResult(event="PreToolUse")
    r.updated_input = updated_input
    return r


class PermTool(Tool):
    """需权限工具:引擎默认 ask 且无 request_permission → 无钩子时必被拒。"""

    name = "Perm"
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return True

    async def _run(self, input, ctx):
        return ToolResult(f"perm ok:{input['text']}")


class WriteTool(Tool):
    """FILE_TOOLS 成员(名字 = Write):配合写保护地板测试(§5.3)。"""

    name = "Write"
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return True

    async def _run(self, input, ctx):
        return ToolResult("wrote:" + input["file_path"])


async def test_hook_deny_blocks_tool_and_fires_post():
    """钩子 deny → 工具不执行,拒绝结果进消息流,PostToolUse 仍触发(§6.1 denied 分支)。"""
    hooks = FakeHooks(pre_result=pre_deny("no"))
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, hooks=hooks)
    messages = await _collect(loop)
    tool_round = messages[2]
    assert tool_round.content[0].is_error
    assert "Permission denied by hook fake: no" in str(tool_round.content[0].content)
    assert hooks.pre_calls == 1
    assert hooks.post_calls == 1  # 拒绝结果也触发 PostToolUse(§6.1)
    assert hooks.last_input.extra["tool_response"]["is_error"] is True


async def test_hook_allow_shortcircuits_engine():
    """钩子 allow → 引擎决策链不运行(本例引擎必拒),工具照常执行(§5.2)。"""
    hooks = FakeHooks(pre_result=pre_allow())
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Perm", "t1", '{"text": "x"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, tools=[PermTool()], hooks=hooks)
    loop.request_permission = None  # 引擎 ask 无人确认 → 引擎路径必拒
    messages = await _collect(loop)
    assert messages[2].content[0].content == "perm ok:x"
    assert not messages[2].content[0].is_error
    assert hooks.pre_calls == 1


async def test_hook_allow_floor_downgrades_to_ask():
    """写保护地板(§5.3):allow + 写保护路径 → 降级 request_permission;拒绝 → 工具不执行。"""
    asks = []

    async def request_permission(decision, tool, input):
        asks.append(decision)
        return False  # 人工拒绝

    hooks = FakeHooks(pre_result=pre_allow())
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Write", "t1", '{"file_path": "repo/.git/config"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, tools=[WriteTool()], hooks=hooks, request_permission=request_permission)
    messages = await _collect(loop)
    assert len(asks) == 1
    assert asks[0].source == "write-protection"
    assert asks[0].requires_explicit_approval is True
    assert messages[2].content[0].is_error
    assert "write-protected" in str(messages[2].content[0].content)


async def test_hook_allow_floor_approved_runs_tool():
    """写保护地板人工确认通过 → 放行执行(引擎不跑:钩子已决策,§5.3)。"""
    asks = []

    async def request_permission(decision, tool, input):
        asks.append(decision)
        return True  # 人工确认

    hooks = FakeHooks(pre_result=pre_allow())
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Write", "t1", '{"file_path": "repo/.git/config"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, tools=[WriteTool()], hooks=hooks, request_permission=request_permission)
    messages = await _collect(loop)
    assert len(asks) == 1
    assert messages[2].content[0].content == "wrote:repo/.git/config"
    assert not messages[2].content[0].is_error


async def test_hook_allow_immune_still_floor():
    """§5.5 约束 2:allow+immune 命中写保护 → 免疫位不豁免权限层,仍降级 ask。"""
    asks = []

    async def request_permission(decision, tool, input):
        asks.append(decision)
        return False

    hooks = FakeHooks(pre_result=pre_allow(immune=True))
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Write", "t1", '{"file_path": "repo/.git/config"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, tools=[WriteTool()], hooks=hooks, request_permission=request_permission)
    await _collect(loop)
    assert len(asks) == 1 and asks[0].source == "write-protection"


async def test_hook_updated_input_rewrites_call_not_session(tmp_path):
    """§5.4:updatedInput 改写执行输入,但改写不落会话(会话只记模型原始 tool_use)。"""
    session = Session("s1", tmp_path)
    hooks = FakeHooks(pre_result=pre_allow(updated_input={"text": "rewritten"}))
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "original"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, hooks=hooks, session=session)
    messages = await _collect(loop)
    # 工具收到改写后输入
    assert messages[2].content[0].content == "echo:rewritten"
    # 会话不落改写:assistant 消息的 tool_use input 仍是模型原始输入
    loaded = session.load()
    assistant = [m for m in loaded if m.role == "assistant"][0]
    tool_use = [b for b in assistant.content if b.type == "tool_use"][0]
    assert tool_use.input == {"text": "original"}


async def test_post_tool_use_fires_with_result():
    """PostToolUse 观察型:成功路径触发,带序列化 tool_response(§2.2 字段清单)。"""
    hooks = FakeHooks(pre_result=None)  # 无 PreToolUse 决策 → 引擎照常
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, hooks=hooks)
    await _collect(loop)
    assert hooks.pre_calls == 1 and hooks.post_calls == 1
    extra = hooks.last_input.extra
    assert extra["tool_name"] == "Echo"
    assert extra["tool_use_id"] == "t1"
    assert extra["tool_response"] == {"content": "echo:x", "is_error": False}


async def test_hooks_none_zero_path():
    """无 hooks(默认)→ 引擎照常,既有行为不回归(§4.10.1 零路径)。"""
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("ok"),
        ]
    )
    messages = await _collect(_loop(llm))
    assert messages[2].content[0].content == "echo:x"


# ---- PermissionEngine.floor_check 单元测试(§5.3) ----


def test_floor_check_protected_path():
    engine = PermissionEngine()
    d = engine.floor_check(
        tool_name="Write", tool_input={"file_path": "repo/.git/config"}, cwd=Path(".")
    )
    assert d is not None
    assert d.allowed is False and d.mode == "ask"
    assert d.source == "write-protection"
    assert d.requires_explicit_approval is True


def test_floor_check_clean_path_none():
    engine = PermissionEngine()
    assert (
        engine.floor_check(tool_name="Write", tool_input={"file_path": "src/main.py"}, cwd=Path("."))
        is None
    )


def test_floor_check_non_file_tool_none():
    engine = PermissionEngine()
    assert engine.floor_check(tool_name="Bash", tool_input={"command": "ls"}, cwd=Path(".")) is None


def test_floor_check_bash_rm_protected_home():
    """Bash 地板:analyze_bash_command 的 deny(rm -rf ~)不得被 hook allow 绕过(§5.3)。"""
    engine = PermissionEngine()
    d = engine.floor_check(tool_name="Bash", tool_input={"command": "rm -rf ~"}, cwd=Path("."))
    assert d is not None
    assert d.source == "write-protection" and d.requires_explicit_approval is True
    assert "protected" in d.reason


def test_floor_check_bash_rm_protected_component():
    """Bash 地板:rm/rmdir 目标命中写保护组件(rm -rf .git)→ 降级 ask(§5.3)。"""
    engine = PermissionEngine()
    d = engine.floor_check(tool_name="Bash", tool_input={"command": "rm -rf .git"}, cwd=Path("."))
    assert d is not None
    assert d.source == "write-protection" and d.requires_explicit_approval is True
    assert ".git" in d.reason


def test_floor_check_bash_clean_command_none():
    engine = PermissionEngine()
    assert (
        engine.floor_check(tool_name="Bash", tool_input={"command": "git status"}, cwd=Path("."))
        is None
    )


def test_floor_check_audits_once():
    events = []

    class Sink:
        def emit(self, e):
            events.append(e)

    engine = PermissionEngine(audit_sink=Sink())
    engine.floor_check(tool_name="Write", tool_input={"file_path": ".git/config"}, cwd=Path("."))
    assert len(events) == 1
    assert events[0].source == "write-protection"  # §8.1:地板降级第二条事件


# ---- 阶段 09 S6 评审修复(M1 Bash 地板 / m1 审计 / m2 测试缺口) ----


class FakeBashTool(Tool):
    """name="Bash" 假件:走引擎 bash 分支但不真执行子进程(安全)。"""

    name = "Bash"
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return True

    async def _run(self, input, ctx):
        return ToolResult("ran:" + input["command"])


async def test_hook_allow_bash_floor_downgrades_to_ask():
    """M1 Bash 地板:allow + Bash(rm -rf .git) → 降级 ask;拒绝 → denied。"""
    asks = []

    async def request_permission(decision, tool, input):
        asks.append(decision)
        return False  # 人工拒绝

    hooks = FakeHooks(pre_result=pre_allow())
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Bash", "t1", '{"command": "rm -rf .git"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, tools=[FakeBashTool()], hooks=hooks, request_permission=request_permission)
    messages = await _collect(loop)
    assert len(asks) == 1
    assert asks[0].source == "write-protection" and asks[0].requires_explicit_approval is True
    assert messages[2].content[0].is_error
    assert ".git" in str(messages[2].content[0].content)


async def test_hook_allow_bash_floor_approved_runs_tool():
    """M1 Bash 地板人工确认通过 → 放行执行。"""
    asks = []

    async def request_permission(decision, tool, input):
        asks.append(decision)
        return True  # 人工确认

    hooks = FakeHooks(pre_result=pre_allow())
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Bash", "t1", '{"command": "rm -rf .git"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, tools=[FakeBashTool()], hooks=hooks, request_permission=request_permission)
    messages = await _collect(loop)
    assert len(asks) == 1
    assert messages[2].content[0].content == "ran:rm -rf .git"
    assert not messages[2].content[0].is_error


async def test_hook_allow_bash_git_status_passes():
    """M1 非保护命令不受影响:allow + Bash(git status) → 直接放行,无地板询问。"""
    asks = []

    async def request_permission(decision, tool, input):
        asks.append(decision)
        return True

    hooks = FakeHooks(pre_result=pre_allow())
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Bash", "t1", '{"command": "git status"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, tools=[FakeBashTool()], hooks=hooks, request_permission=request_permission)
    messages = await _collect(loop)
    assert asks == []
    assert messages[2].content[0].content == "ran:git status"


async def test_hook_dispatch_error_fails_closed_with_audit():
    """m1/m2(a):dispatch 异常 → fail-closed deny + 恰好一条权限审计(§8.1)。"""
    events = []

    class Sink:
        def emit(self, e):
            events.append(e)

    class RaisingHooks(FakeHooks):
        async def dispatch(self, event, *, input, abort_event=None):
            raise RuntimeError("hook manager bug")

    hooks = RaisingHooks(pre_result=pre_allow())
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, hooks=hooks)
    loop.permissions = PermissionEngine(audit_sink=Sink())
    messages = await _collect(loop)
    assert messages[2].content[0].is_error
    assert "Permission denied by hook" in str(messages[2].content[0].content)
    assert len(events) == 1
    assert events[0].source == "hook:PreToolUse" and events[0].decision == "deny"


async def test_hook_dispatch_receives_abort_event():
    """m2(b):dispatch 收到 loop 的 abort_event(§6.3 abort 感知接线)。"""
    hooks = FakeHooks(pre_result=pre_allow())
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, hooks=hooks)
    await _collect(loop)
    assert hooks.abort_event is loop.abort


async def test_post_only_config_pre_zero_path():
    """m2(c):仅配置 PostToolUse 时 PreToolUse 零路径 —— 引擎照常、无 pre 调用(§4.10.1)。"""
    hooks = FakeHooks(pre_result=None, events=("PostToolUse",))
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, hooks=hooks)
    messages = await _collect(loop)
    assert hooks.pre_calls == 0 and hooks.post_calls == 1
    assert messages[2].content[0].content == "echo:x"


async def test_passthrough_updated_input_reaches_engine():
    """m2(d):passthrough 钩子 + updatedInput → 引擎读到改写后输入(§5.4 完整语义)。"""
    seen = []

    async def request_permission(decision, tool, input):
        seen.append(dict(input))
        return True  # 引擎 ask 被确认 → 放行

    hooks = FakeHooks(pre_result=pre_passthrough(updated_input={"text": "rewritten"}))
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Perm", "t1", '{"text": "original"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, tools=[PermTool()], hooks=hooks, request_permission=request_permission)
    await _collect(loop)
    assert seen == [{"text": "rewritten"}]  # 引擎 ask 决策(含 reason)看到的是改写后输入


# ---- 阶段 09 S7:事件接线测试(SessionStart/UserPromptSubmit/Stop + prefix 注入) ----
# 语义锚点:docs/specs/09-hooks.md §6.2(逐事件接线表)/§6.4(Stop 门控)/§7.1-7.2(改写通道)。


class RecordingLLM(FakeLLM):
    """记录每次请求的 messages 列表(prefix 注入断言用)。"""

    def __init__(self, script, **kw):
        super().__init__(script, **kw)
        self.requests = []

    def stream(self, request, model="main"):
        self.requests.append(request.messages)
        return super().stream(request, model=model)


def session_context(text):
    """SessionStart additionalContext(§7.1):注入一次性 reminder。"""
    r = HookDispatchResult(event="SessionStart")
    r.additional_context = text
    return r


def submit_blocked(text="input violates policy"):
    """UserPromptSubmit exit 2(§4.3):阻止提交,输入擦除。"""
    r = HookDispatchResult(event="UserPromptSubmit")
    r.blocking_error = text
    return r


def submit_rewrite(prompt=None, reminder=None, context=None):
    """UserPromptSubmit 消息改写(§7.1/§7.2)。"""
    r = HookDispatchResult(event="UserPromptSubmit")
    r.updated_prompt = prompt
    r.updated_system_reminder = reminder
    r.additional_context = context
    return r


def stop_continue_false(reason="stop now"):
    """Stop 钩子显式 continue:false(§6.4):停止 + stopReasonOverride。"""
    r = HookDispatchResult(event="Stop")
    r.stop = True
    r.stop_reason = reason
    return r


def stop_feedback(text="one more thing"):
    """Stop 钩子 exit 2(§6.4):注入 feedback 继续循环。"""
    r = HookDispatchResult(event="Stop")
    r.stop_feedback = text
    return r


class TermStopTool(Tool):
    """terminate=True 的工具(区别于既有 TerminateTool,不遮蔽 PI-04 测试)。"""

    name = "TermStop"
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return False

    async def _run(self, input, ctx):
        return ToolResult("terminated", terminate=True)


async def test_session_start_fires_once_with_source():
    """SessionStart 门闩(§6.2):AgentLoop 生命周期只触发一次;source 按 history 判定。"""
    hooks = FakeHooks(events=("SessionStart",), session_results=[session_context("hi")])
    llm = FakeLLM([lambda i: text_event("a"), lambda i: text_event("b")])
    loop = _loop(llm, hooks=hooks)
    await _collect(loop)
    await _collect(loop)
    assert hooks.session_calls == 1  # 门闩:第二个 run() 不再触发
    assert hooks.last_input.extra["source"] == "startup"
    assert hooks.last_input.extra["model"] == "main"


async def test_session_start_source_resume_with_history():
    """history 非空 → source="resume"(§2.2)。"""
    hooks = FakeHooks(events=("SessionStart",), session_results=[session_context("hi")])
    llm = FakeLLM([lambda i: text_event("a")])
    loop = _loop(llm, hooks=hooks, history=[user_message("prior turn")])
    await _collect(loop)
    assert hooks.last_input.extra["source"] == "resume"


async def test_session_start_context_injected_first_request_only():
    """SessionStart additionalContext → 第一次请求的 prefix,一次性(§7.1/§7.2)。"""
    hooks = FakeHooks(
        events=("SessionStart",),
        session_results=[session_context("startup context")],
    )
    llm = RecordingLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, hooks=hooks)
    await _collect(loop)
    assert len(llm.requests) == 2
    first = [str(m.content) for m in llm.requests[0] if "startup context" in str(m.content)]
    assert len(first) == 1  # 以独立 reminder 消息注入(REMINDER_HEADER 包裹)
    assert "startup context" in first[0]
    assert not any("startup context" in str(m.content) for m in llm.requests[1])  # 一次性


async def test_session_start_fail_open_on_dispatch_error():
    """SessionStart 非阻塞(§4.6):dispatch 异常 → 仅日志,run() 照常。"""

    class RaisingSessionHooks(FakeHooks):
        async def dispatch(self, event, *, input, abort_event=None):
            if event == "SessionStart":
                raise RuntimeError("hook manager bug")
            return await super().dispatch(event, input=input, abort_event=abort_event)

    hooks = RaisingSessionHooks(events=("SessionStart",))
    llm = FakeLLM([lambda i: text_event("ok")])
    loop = _loop(llm, hooks=hooks)
    messages = await _collect(loop)
    assert [m.role for m in messages] == ["user", "assistant"]


async def test_user_prompt_submit_blocked_first_input():
    """exit 2 → 阻止提交(§6.2):输入擦除,钩子 stderr 作为终结消息,模型不调用。"""
    hooks = FakeHooks(events=("UserPromptSubmit",), submit_results=[submit_blocked("no")])
    llm = FakeLLM([lambda i: text_event("should not run")])
    loop = _loop(llm, hooks=hooks)
    messages = await _collect(loop, user_input="bad request")
    assert len(messages) == 1  # 原始用户消息不进入循环
    assert messages[0].is_meta and messages[0].content == "no"
    assert loop.last_stop_reason == "hook_blocked"
    assert llm.calls == 0
    assert hooks.submit_calls == 1


async def test_user_prompt_submit_blocked_steer_dropped():
    """steer 输入被 blocked → 静默丢弃 + 日志,不影响运行(§6.2)。"""
    hooks = FakeHooks(
        events=("UserPromptSubmit",),
        submit_results=[None, submit_blocked("steer rejected"), None],  # 首条放行,第一条 steer 被拦
    )
    llm = FakeLLM([lambda i: text_event("after steer")])
    steer = asyncio.Queue()
    steer.put_nowait("bad steer")
    steer.put_nowait("good steer")
    loop = _loop(llm, hooks=hooks, steer_queue=steer)
    messages = await _collect(loop, user_input="do something")
    assert not any("bad steer" in str(m.content) for m in messages)  # 被丢弃
    assert any("good steer" in str(m.content) for m in messages)  # 其余 steer 照常
    assert messages[-1].content[0].text == "after steer"
    assert hooks.submit_calls == 3  # 首条 + 两条 steer


async def test_user_prompt_submit_updated_prompt(tmp_path):
    """updatedPrompt 替换输入文本,会话记改写后文本(§7.1)。"""
    session = Session("s1", tmp_path)
    hooks = FakeHooks(
        events=("UserPromptSubmit",),
        submit_results=[submit_rewrite(prompt="rewritten question")],
    )
    llm = FakeLLM([lambda i: text_event("ok")])
    loop = _loop(llm, hooks=hooks, session=session)
    messages = await _collect(loop, user_input="original question")
    assert messages[0].content == "rewritten question"  # 流中即改写后文本
    loaded = session.load()
    assert loaded[0].content == "rewritten question"


async def test_user_prompt_submit_reminder_join_and_one_shot():
    """updatedSystemReminder/additionalContext join('\n\n') 注入第一次请求,第二次无残留(§7.2)。"""
    hooks = FakeHooks(
        events=("UserPromptSubmit",),
        submit_results=[submit_rewrite(reminder="reminder A", context="context B")],
    )
    llm = RecordingLLM(
        [
            lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, hooks=hooks)
    await _collect(loop)
    assert len(llm.requests) == 2
    injected = [str(m.content) for m in llm.requests[0] if "reminder A" in str(m.content)]
    assert len(injected) == 1
    assert "reminder A\n\ncontext B" in injected[0]  # 多钩子顺序 join('\n\n')(§4.10.6)
    assert not any("reminder A" in str(m.content) for m in llm.requests[1])  # 一次性消费


async def test_reminder_join_order_session_then_submit():
    """m4(a):SessionStart 与 UserPromptSubmit 混合累积,join 顺序保持触发序
    (SessionStart 在前,§6.2 接线顺序 / §4.10.6 聚合链)。"""
    hooks = FakeHooks(
        events=("SessionStart", "UserPromptSubmit"),
        session_results=[session_context("from session")],
        submit_results=[submit_rewrite(reminder="from submit")],
    )
    llm = RecordingLLM([lambda i: text_event("ok")])
    loop = _loop(llm, hooks=hooks)
    await _collect(loop)
    injected = [str(m.content) for m in llm.requests[0] if "from session" in str(m.content)]
    assert len(injected) == 1
    assert "from session\n\nfrom submit" in injected[0]


async def test_hook_reminder_survives_blocked_first_run():
    """m4(b):SessionStart 的 reminder 在首 run 被 blocked 提前终止(无 LLM 请求)
    后残留,第二次 run 的首次请求注入(一次性)。"""
    hooks = FakeHooks(
        events=("SessionStart", "UserPromptSubmit"),
        session_results=[session_context("from session")],
        submit_results=[submit_blocked("no")],
    )
    llm = RecordingLLM([lambda i: text_event("ok")])
    loop = _loop(llm, hooks=hooks)
    await _collect(loop, user_input="bad")  # run 1:blocked,无请求
    assert loop.last_stop_reason == "hook_blocked"
    assert llm.requests == []
    await _collect(loop, user_input="good")  # run 2:正常
    injected = [str(m.content) for m in llm.requests[0] if "from session" in str(m.content)]
    assert len(injected) == 1


async def test_user_prompt_submit_fail_open_on_dispatch_error():
    """UserPromptSubmit 非阻塞(§4.6):dispatch 异常 → 输入照常进入循环。"""

    class RaisingSubmitHooks(FakeHooks):
        async def dispatch(self, event, *, input, abort_event=None):
            if event == "UserPromptSubmit":
                raise RuntimeError("hook manager bug")
            return await super().dispatch(event, input=input, abort_event=abort_event)

    hooks = RaisingSubmitHooks(events=("UserPromptSubmit",))
    llm = FakeLLM([lambda i: text_event("ok")])
    loop = _loop(llm, hooks=hooks)
    messages = await _collect(loop, user_input="hi")
    assert messages[0].content == "hi"
    assert messages[-1].content[0].text == "ok"


async def test_stop_fires_on_completed():
    """completed 分支触发 Stop 钩子;无输出 → 照常停止(§6.4)。"""
    hooks = FakeHooks(events=("Stop",))
    llm = FakeLLM([lambda i: text_event("answer")])
    loop = _loop(llm, hooks=hooks)
    messages = await _collect(loop)
    assert hooks.stop_calls == 1
    assert hooks.last_input.extra["reason"] == "completed"
    # 最后一条 assistant 消息:文本块序列化为 dict 列表(§2.2 字段清单)
    blocks = hooks.last_input.extra["last_assistant_message"]
    assert isinstance(blocks, list) and blocks[0]["type"] == "text"
    assert loop.last_stop_reason == "completed"
    assert [m.role for m in messages] == ["user", "assistant"]


@pytest.mark.parametrize(
    "reason,kwargs,script,llm_error,abort_first",
    [
        ("max_turns", {"max_turns": 1}, [lambda i: tool_use_event("Echo", "t1", '{"text": "x"}')], False, False),
        ("max_budget", {"max_budget_usd": 0.0}, [], False, False),
        ("interrupted", {}, [], False, True),
        ("error", {}, [], True, False),
        ("thinking_only_exhausted", {}, [lambda i: thinking_only_event()] * 3, False, False),
    ],
)
async def test_stop_not_fired_on_control_stops(reason, kwargs, script, llm_error, abort_first):
    """门控表(§6.4):控制流终止一律不触发 Stop 钩子(钩子不得复活循环)。"""

    class BoomLLM(FakeLLM):
        def stream(self, request, model="main"):
            raise LLMError("provider down", status_code=500)

    llm = BoomLLM(script) if llm_error else FakeLLM(script)
    hooks = FakeHooks(events=("Stop",))
    loop = _loop(llm, hooks=hooks, **kwargs)
    if abort_first:
        loop.abort.set()
    await _collect(loop)
    assert loop.last_stop_reason == reason
    assert hooks.stop_calls == 0


async def test_stop_feedback_continues_loop(tmp_path):
    """exit 2 feedback → 注入消息继续循环,模型看到反馈再决策(§6.4)。"""
    session = Session("s1", tmp_path)
    hooks = FakeHooks(events=("Stop",), stop_results=[stop_feedback("one more thing")])
    llm = FakeLLM([lambda i: text_event("first"), lambda i: text_event("second")])
    loop = _loop(llm, hooks=hooks, session=session)
    messages = await _collect(loop)
    assert llm.calls == 2  # feedback 后模型再决策一轮
    assert hooks.stop_calls == 2  # 第二轮 completed 再次触发(结果耗尽 → 放行)
    feedbacks = [m for m in messages if m.role == "user" and "Stop hook feedback:" in str(m.content)]
    assert len(feedbacks) == 1
    assert "one more thing" in str(feedbacks[0].content)
    assert loop.last_stop_reason == "completed"
    # 反馈进入消息流 = 普通历史:会话日志同样落盘(§6.4)
    loaded = session.load()
    assert any("Stop hook feedback:" in str(m.content) for m in loaded)


async def test_stop_feedback_capped_at_max_attempts():
    """M1(§6.4 补):永远 exit 2 的 Stop 钩子 → 最多 5 次 feedback 后按普通
    completed 停止(对齐 CC MAX_STOP_HOOK_ATTEMPTS=5),不报错、不无限循环。"""
    hooks = FakeHooks(events=("Stop",), stop_results=[stop_feedback("again")] * 10)
    llm = FakeLLM([lambda i: text_event("answer")])  # 脚本耗尽后重复最后一条
    loop = _loop(llm, hooks=hooks)
    messages = await _collect(loop)
    assert llm.calls == 6  # 首轮 + 5 轮 feedback,无第 7 轮
    assert hooks.stop_calls == 6  # 第 6 次 dispatch 时达限放行
    feedbacks = [m for m in messages if m.role == "user" and "Stop hook feedback:" in str(m.content)]
    assert len(feedbacks) == 5
    assert loop.last_stop_reason == "completed"


async def test_stop_feedback_counter_resets_per_run():
    """M1:计数按 run() 生命周期 —— 复用实例第二次 run 重新计数(仍 5 次)。"""
    hooks = FakeHooks(events=("Stop",), stop_results=[stop_feedback("again")] * 20)
    llm = FakeLLM([lambda i: text_event("answer")])
    loop = _loop(llm, hooks=hooks)
    await _collect(loop)  # run 1:5 次 feedback 后达限停止
    assert llm.calls == 6
    assert loop.last_stop_reason == "completed"
    await _collect(loop)  # run 2:计数器已重置,再 5 次
    assert llm.calls == 12
    assert hooks.stop_calls == 12
    assert loop.last_stop_reason == "completed"


async def test_thinking_only_recovery_not_user_prompt():
    """m1(§2.2 边界):thinking-only 恢复消息不是用户输入,不触发 UserPromptSubmit。"""
    hooks = FakeHooks(events=("UserPromptSubmit",))
    llm = FakeLLM([lambda i: thinking_only_event(), lambda i: text_event("ok")])
    loop = _loop(llm, hooks=hooks)
    messages = await _collect(loop)
    assert hooks.submit_calls == 1  # 仅首条真实输入;恢复消息零派发
    assert messages[-1].content[0].text == "ok"


async def test_stop_continue_false_stops_with_reason():
    """continue:false + stopReason → _stop("hook", reason)(§6.4)。"""
    hooks = FakeHooks(events=("Stop",), stop_results=[stop_continue_false("task complete")])
    llm = FakeLLM([lambda i: text_event("answer"), lambda i: text_event("never")])
    loop = _loop(llm, hooks=hooks)
    messages = await _collect(loop)
    assert loop.last_stop_reason == "hook"
    assert llm.calls == 1  # 不再有第二轮
    assert hooks.stop_calls == 1
    last = messages[-1]
    assert last.is_meta and "task complete" in str(last.content)


async def test_stop_mixed_signals_continue_false_wins():
    """S5 m2:exit 2 feedback 与显式 continue:false 并存 → 显式指令优先,循环停止。"""
    r = HookDispatchResult(event="Stop")
    r.stop = True
    r.stop_reason = "stop now"
    r.stop_feedback = "one more thing"
    hooks = FakeHooks(events=("Stop",), stop_results=[r])
    llm = FakeLLM([lambda i: text_event("first"), lambda i: text_event("never")])
    loop = _loop(llm, hooks=hooks)
    messages = await _collect(loop)
    assert llm.calls == 1  # feedback 未注入 → 无第二轮
    assert loop.last_stop_reason == "hook"
    assert "one more thing" not in str(messages[-1].content)
    assert "stop now" in str(messages[-1].content)


async def test_stop_fires_on_tool_terminated():
    """tool_terminated 分支触发 Stop 钩子;feedback → 继续循环(§6.4)。"""
    hooks = FakeHooks(events=("Stop",), stop_results=[stop_feedback("not yet")])
    llm = FakeLLM(
        [
            lambda i: tool_use_event("TermStop", "t1", "{}"),
            lambda i: text_event("ok"),
        ]
    )
    loop = _loop(llm, tools=[TermStopTool()], hooks=hooks)
    messages = await _collect(loop)
    assert hooks.stop_calls == 2  # 第一轮 tool_terminated 被拦下,第二轮 completed 放行
    assert loop.last_stop_reason == "completed"
    assert messages[-1].content[0].text == "ok"


async def test_stop_tool_terminated_default_when_no_hook_output():
    """tool_terminated + Stop 钩子无输出 → 照常 _stop("tool_terminated")。"""
    hooks = FakeHooks(events=("Stop",))
    llm = FakeLLM([lambda i: tool_use_event("TermStop", "t1", "{}")])
    loop = _loop(llm, tools=[TermStopTool()], hooks=hooks)
    messages = await _collect(loop)
    assert hooks.stop_calls == 1
    assert loop.last_stop_reason == "tool_terminated"
    assert messages[-1].is_meta and "tools requested termination" in str(messages[-1].content)


async def test_stop_dispatch_error_fail_open():
    """钩子层异常 → 只警告 + 日志,照常停止(§6.4 CC fail-open)。"""

    class RaisingStopHooks(FakeHooks):
        async def dispatch(self, event, *, input, abort_event=None):
            if event == "Stop":
                raise RuntimeError("hook manager bug")
            return await super().dispatch(event, input=input, abort_event=abort_event)

    hooks = RaisingStopHooks(events=("Stop",))
    llm = FakeLLM([lambda i: text_event("answer")])
    loop = _loop(llm, hooks=hooks)
    messages = await _collect(loop)
    assert loop.last_stop_reason == "completed"
    assert [m.role for m in messages] == ["user", "assistant"]


async def test_hook_blocked_does_not_fire_stop():
    """§2.2 防递归:钩子自产的终止(hook_blocked)不再次触发 Stop 钩子。"""
    hooks = FakeHooks(
        events=("UserPromptSubmit", "Stop"),
        submit_results=[submit_blocked("blocked by policy")],
    )
    llm = FakeLLM([lambda i: text_event("never")])
    loop = _loop(llm, hooks=hooks)
    await _collect(loop)
    assert loop.last_stop_reason == "hook_blocked"
    assert hooks.stop_calls == 0


async def test_s7_events_receive_abort_event():
    """§6.3:三个新事件 dispatch 都收到 loop 的 abort_event。"""
    hooks = FakeHooks(
        events=("SessionStart", "UserPromptSubmit", "Stop"),
        session_results=[session_context("hi")],
        submit_results=[submit_rewrite(prompt="rewritten")],
        stop_results=[stop_continue_false("done")],
    )
    llm = FakeLLM([lambda i: text_event("answer")])
    loop = _loop(llm, hooks=hooks)
    await _collect(loop)
    assert hooks.abort_event is loop.abort


# ---- §3.2 输出端(length)恢复 ----


async def test_length_truncated_tool_use_recovers_once(tmp_path):
    """§3.2 形态 1:length+残缺 tool_use → 不落会话 + 反馈重试一次,最终回答落盘。"""
    session = Session("s1", tmp_path)
    llm = FakeLLM([lambda i: length_tool_use_event(), lambda i: text_event("done reply")])
    loop = _loop(llm, session=session)
    out = await _collect(loop)
    assert llm.calls == 2  # 反馈后重试一次
    # 第二请求携带反馈,且不含被剥除的残缺 tool_use
    assert OUTPUT_OVERFLOW_RECOVERY in request_text(llm.last_messages)
    assert not any(
        isinstance(m.content, list) and any(b.type == "tool_use" for b in m.content)
        for m in llm.last_messages
    )
    # 残缺回复与反馈均不落会话:仅用户输入 + 最终回答
    assert [m.role for m in out] == ["user", "assistant"]
    assert out[1].content[0].text == "done reply"
    persisted = session.load()
    assert [m.role for m in persisted] == ["user", "assistant"]
    assert persisted[1].content[0].text == "done reply"


async def test_length_pure_text_truncation_no_recovery(tmp_path):
    """§3.2 形态 2:纯文本截断不恢复,截断回复照常落盘并完成(仅记 transition)。"""
    session = Session("s1", tmp_path)
    llm = FakeLLM([lambda i: length_text_event("partial reply"), lambda i: text_event("done reply")])
    loop = _loop(llm, session=session)
    out = await _collect(loop)
    assert llm.calls == 1  # 不重试
    assert [m.role for m in out] == ["user", "assistant"]
    assert out[1].content[0].text == "partial reply"
    persisted = session.load()
    assert persisted[1].content[0].text == "partial reply"


async def test_length_gate_exhausted_falls_through(tmp_path):
    """§3.2 防死循环闸:同一 run 二次 length+tool_use 不再恢复,
    截断回复落回正常循环(不终止本轮,last_stop_reason=completed)。"""
    session = Session("s1", tmp_path)
    llm = FakeLLM(
        [
            lambda i: length_tool_use_event(),
            lambda i: length_tool_use_event(text="second partial", tid="t2"),
        ]
    )
    loop = _loop(llm, session=session)
    out = await _collect(loop)
    assert llm.calls == 2  # 第一次恢复,第二次闸门用尽不恢复
    assert loop.last_stop_reason == "completed"  # is_error 截断消息按普通回复完成
    # 第二次截断回复以正常 assistant 消息落盘(PI-03 已剥 tool_use,仅余文本)
    persisted = session.load()
    assert [m.role for m in persisted] == ["user", "assistant"]
    assert persisted[1].content[0].text == "second partial"
    # 第一次恢复时注入过一次反馈(第二次请求里),未被重复注入
    assert request_text(llm.last_messages).count(OUTPUT_OVERFLOW_RECOVERY) == 1


async def test_length_empty_truncated_no_empty_message_in_session(tmp_path):
    """LOW-3:闸尽后全空截断回复(纯 tool_use 被剥,无文本)→ 不落空消息,
    按原 error 语义终止。"""
    session = Session("s1", tmp_path)
    llm = FakeLLM(
        [
            lambda i: length_tool_use_event(),
            lambda i: length_tool_use_event(text="", tid="t2"),
        ]
    )
    loop = _loop(llm, session=session)
    out = await _collect(loop)
    assert llm.calls == 2
    assert loop.last_stop_reason == "error"
    assert loop.last_transition == "error_terminate"  # §5.3 裁决:全空终止 = 终止非截断落回
    # 无空 assistant 消息落盘(恢复轮与终止轮均未 yield/persist)
    persisted = session.load()
    assert [m.role for m in persisted] == ["user"]
    assert [m.role for m in out] == ["user"]
