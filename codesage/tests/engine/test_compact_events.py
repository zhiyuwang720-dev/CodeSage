"""S8 compact 事件接线测试(阶段 09 §6.2/§7.4):PreCompact/PostCompact 钩子。

挂接点 = `_compact` 内部(一处覆盖 auto 主路径 + PTL 反应式路径);
决策语义 = PreCompact exit 2 阻止压缩 / exit 0 stdout 注入 custom instructions /
fail-open(exit 1/超时/无输出);PostCompact 纯观察型;熔断不被钩子误触(R1 §6)。
多钩子 join('\n\n') 与 exit 2 聚合在 manager 层已测(test_manager.py:583-614),
本文件只测 loop 接线。
"""

import asyncio

from codesage.ai import ContentBlock, LLMError, LLMResponse, StreamEvent
from codesage.core import assistant_message, user_message
from codesage.engine import AgentLoop, AgentLoopConfig, CompactionConfig
from codesage.engine.compaction import find_cut_point
from codesage.hooks import HookDispatchResult
from codesage.permissions import PermissionEngine
from codesage.tools import Tool, ToolRegistry, ToolResult, ToolUseContext


class FakeLLM:
    """脚本化 LLM:stream 走脚本;complete 记录摘要请求(压缩路径)。"""

    def __init__(self, script, summary_text="compacted summary", summary_errors=None):
        self.script = script
        self.calls = 0
        self.total_cost = [0.0]
        self.summary_text = summary_text
        self.summary_errors = list(summary_errors) if summary_errors else None
        self.complete_calls = []  # [(model, LLMRequest)] — the compaction path

    def stream(self, request, model="main"):
        return self._gen()

    async def complete(self, request, model="main"):
        self.complete_calls.append((model, request))
        if self.summary_errors:
            raise self.summary_errors.pop(0)
        return LLMResponse(content=[ContentBlock(type="text", text=self.summary_text)])

    async def _gen(self):
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for ev in self.script[idx](self.calls):
            await asyncio.sleep(0)
            yield ev


class CompactFakeHooks:
    """脚本化 HookManager 假件(仅 compact 两事件):结果按列表逐个消费,
    耗尽返回空结果(无决策 → fail-open 收敛面)。raise_dispatch 模拟钩子层 bug(§6.3)。"""

    def __init__(self, pre_results=None, post_results=None, raise_dispatch=False):
        self.pre_results = list(pre_results) if pre_results else []
        self.post_results = list(post_results) if post_results else []
        self.raise_dispatch = raise_dispatch
        self.pre_calls = 0
        self.post_calls = 0
        self.pre_input = None
        self.post_input = None
        self.abort_event = None

    def has_hooks_for_event(self, event):
        return event in ("PreCompact", "PostCompact")

    async def dispatch(self, event, *, input, abort_event=None):
        self.abort_event = abort_event  # §6.3:abort 感知接线(断言用)
        if self.raise_dispatch:
            raise RuntimeError("hook dispatch boom")
        if event == "PreCompact":
            self.pre_calls += 1
            self.pre_input = input
            r = self.pre_results.pop(0) if self.pre_results else HookDispatchResult(event="PreCompact")
            return r
        if event == "PostCompact":
            self.post_calls += 1
            self.post_input = input
            r = self.post_results.pop(0) if self.post_results else HookDispatchResult(event="PostCompact")
            return r
        raise AssertionError(f"unexpected event {event}")


def pre_instructions(text):
    r = HookDispatchResult(event="PreCompact")
    r.compact_instructions = text
    return r


def pre_block():
    r = HookDispatchResult(event="PreCompact")
    r.block_compact = True
    return r


def text_event(text="answer"):
    return [StreamEvent(type="text_delta", text=text), StreamEvent(type="done", stop_reason="end_turn")]


def tool_use_event(name, tid, input_json):
    return [
        StreamEvent(type="tool_use_start", tool_use_id=tid, tool_name=name),
        StreamEvent(type="tool_use_delta", input_json_delta=input_json),
        StreamEvent(type="done", stop_reason="tool_use"),
    ]


def _ptl_stream(i):
    return [StreamEvent(type="error", error="HTTP 400: context_length_exceeded")]


class EchoTool(Tool):
    """一个工具轮次 = 循环进入下一 turn 的方式(纯文本回复会结束循环)。"""

    name = "Echo"
    description = "Echoes input"
    input_schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return False

    async def _run(self, input, ctx):
        return ToolResult(f"echo:{input['text']}")


def _big_history(n=6, size=400):
    return [user_message(f"hist-{i} " + "x" * size) for i in range(n)]


def _tiny_compaction():
    # window/reserve/keep_recent small so ordinary test messages overflow it
    return CompactionConfig(window=100, reserve=10, keep_recent=200)


def _loop(llm, hooks=None, **kw):
    return AgentLoop(
        AgentLoopConfig(
            client=llm,
            tools=ToolRegistry([EchoTool()]),
            permissions=PermissionEngine(),
            hooks=hooks,
            **kw,
        )
    )


async def _collect(loop, user_input="hi"):
    out = []
    async for msg in loop.run(user_input):
        out.append(msg)
    return out


# ---- R1 §6 测试要点 1-6 ----

async def test_precompact_instructions_injected_into_summary_prompt():
    """R1-1:PreCompact exit 0 + stdout → 指令进摘要请求 prompt(# Custom Instructions)。"""
    llm = FakeLLM([lambda i: text_event("answer")], summary_text="compacted")
    hooks = CompactFakeHooks(pre_results=[pre_instructions("Keep file paths exact.")])
    loop = _loop(llm, hooks=hooks, history=_big_history(), compaction=_tiny_compaction())
    messages = await _collect(loop)
    assert len(llm.complete_calls) == 1  # 压缩照常发生
    prompt = llm.complete_calls[0][1].messages[0].content
    assert "# Custom Instructions\nKeep file paths exact." in prompt
    assert any(m.is_compaction_summary for m in messages)
    # HookInput 字段(§6.2):trigger/context_tokens/window/reserve/keep_recent
    extra = hooks.pre_input.extra
    assert extra["trigger"] == "auto"
    assert extra["context_tokens"] > 0
    assert extra["window"] == 100 and extra["reserve"] == 10 and extra["keep_recent"] == 200
    assert hooks.abort_event is loop.abort  # §6.3:abort 感知接线


async def test_precompact_exit2_blocks_compaction_and_debounce_recovers():
    """R1-2:exit 2 → 本轮不压缩(generate_summary 不调);防抖已占位但
    下轮(turn+1)正常恢复,可再次触发(与 CC「阻止本次压缩」语义一致)。"""
    llm = FakeLLM(
        [lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'), lambda i: text_event("b")],
        summary_text="compacted",
    )
    hooks = CompactFakeHooks(pre_results=[pre_block(), pre_block()])
    loop = _loop(llm, hooks=hooks, history=_big_history(), compaction=_tiny_compaction())
    messages = await _collect(loop)
    assert llm.complete_calls == []  # 摘要请求从未发出
    assert hooks.pre_calls == 2  # turn1 被阻;turn2(工具轮之后)再次触发(防抖只挡同轮)
    assert not any(m.is_compaction_summary for m in messages)
    assert loop.last_stop_reason == "completed"  # 主循环不受阻


async def test_precompact_fail_open_no_instructions():
    """R1-3:exit 1/超时/无输出(空结果聚合面)→ 压缩照常、无指令(fail-open)。"""
    llm = FakeLLM([lambda i: text_event("answer")], summary_text="compacted")
    hooks = CompactFakeHooks()  # 空结果 = 无输出;exit 1/超时在 manager 层同收敛
    loop = _loop(llm, hooks=hooks, history=_big_history(), compaction=_tiny_compaction())
    messages = await _collect(loop)
    assert len(llm.complete_calls) == 1
    prompt = llm.complete_calls[0][1].messages[0].content
    assert "# Custom Instructions" not in prompt
    assert any(m.is_compaction_summary for m in messages)


async def test_ptl_path_triggers_compact_hooks():
    """R1-4:PTL 反应式路径(loop.py:246)同样触发钩子;context_tokens 无检查点
    估算 → _compact 内回退估算(原始 span)。

    本脚本下 _compact 共调用 3 次:turn1 auto 检查点、PTL 反应式、turn2 auto
    检查点(压缩后 keep_recent 保留段仍超阈值)。三次 pre_results 都有指令,
    断言第 2 次摘要请求(== 反应式压缩)的 prompt 含指令 —— 若钩子没挂在
    反应式路径上,该 prompt 无 # Custom Instructions。
    """
    llm = FakeLLM([_ptl_stream, lambda i: text_event("answer")], summary_text="compacted")
    hooks = CompactFakeHooks(
        pre_results=[pre_instructions("recover carefully")] * 3
    )
    loop = _loop(llm, hooks=hooks, history=_big_history(), compaction=_tiny_compaction())
    messages = await _collect(loop)
    assert hooks.pre_calls == 3 and hooks.post_calls == 3  # 每次 _compact 都派发
    assert len(llm.complete_calls) == 3
    # complete_calls[1] = PTL 反应式压缩的摘要请求(turn1 auto 在前、turn2 auto 在后)
    reactive_prompt = llm.complete_calls[1][1].messages[0].content
    assert "# Custom Instructions\nrecover carefully" in reactive_prompt
    for _, req in llm.complete_calls:
        assert "# Custom Instructions\nrecover carefully" in req.messages[0].content
    assert hooks.pre_input.extra["trigger"] == "auto"
    assert hooks.pre_input.extra["context_tokens"] > 0
    assert messages[-1].content[0].text == "answer"
    assert loop.last_stop_reason == "completed"


async def test_postcompact_observational_with_fields():
    """R1-5:PostCompact 成功后触发,compact_summary 含摘要文本、cut_index 正确;
    exit 2 同款结果被忽略(观察型,§4.3 PostToolUse 同款)。"""
    llm = FakeLLM([lambda i: text_event("answer")], summary_text="compacted")
    blocked = HookDispatchResult(event="PostCompact")
    blocked.block_compact = True  # exit 2 同款:观察事件无阻塞通道
    hooks = CompactFakeHooks(pre_results=[pre_instructions("keep")], post_results=[blocked])
    loop = _loop(llm, hooks=hooks, history=_big_history(), compaction=_tiny_compaction())
    messages = await _collect(loop)
    assert hooks.post_calls == 1
    extra = hooks.post_input.extra
    assert extra["trigger"] == "auto"
    assert extra["compact_summary"] == "compacted"  # 无 fileOps → 无尾段
    expected_cut = find_cut_point([*_big_history(), user_message("hi")], keep_recent=200)
    assert extra["cut_index"] == expected_cut.index
    assert extra["keep_recent"] == 200
    assert any(m.is_compaction_summary for m in messages)  # exit 2 结果无效果,压缩成功


async def test_precompact_hook_failure_does_not_trip_breaker():
    """R1-6:PreCompact 钩子失败(fail-open)→ 压缩照常,不计入 _compact_failures
    (熔断只由 generate_summary 的 LLMError 驱动)。"""
    llm = FakeLLM(
        [lambda i: tool_use_event("Echo", "t1", '{"text": "x"}'), lambda i: text_event("b")],
        summary_text="compacted",
    )
    hooks = CompactFakeHooks(raise_dispatch=True)  # 每次派发都炸(§6.3 钩子 bug)
    loop = _loop(llm, hooks=hooks, history=_big_history(), compaction=_tiny_compaction())
    await _collect(loop)
    assert len(llm.complete_calls) == 2  # 两个 turn 都压缩成功(fail-open)
    assert loop._compact_failures == 0  # 钩子失败不计入熔断
    assert loop.compaction.enabled is True  # 熔断未被误触


async def test_hooks_none_compaction_unaffected():
    """无 hooks(默认)→ 压缩行为与 S6 既有完全一致(零路径,§4.10.1)。"""
    llm = FakeLLM([lambda i: text_event("answer")], summary_text="compacted")
    loop = _loop(llm, history=_big_history(), compaction=_tiny_compaction())
    messages = await _collect(loop)
    assert len(llm.complete_calls) == 1
    prompt = llm.complete_calls[0][1].messages[0].content
    assert "# Custom Instructions" not in prompt
    assert any(m.is_compaction_summary for m in messages)
