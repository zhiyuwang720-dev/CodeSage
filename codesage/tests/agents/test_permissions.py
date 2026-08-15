"""Subagent permission tests (phase 13 S4, spec §7): effective-mode min
narrowing, ask auto-deny, per-decision audit, fork bubble (inherited parent
request_permission)."""

import asyncio
from pathlib import Path

import pytest

from codesage.agents import AgentRegistry, SubagentRequest, SubagentRunner
from codesage.ai import LLMResponse, StreamEvent
from codesage.core import Session
from codesage.engine import AgentLoop, AgentLoopConfig
from codesage.permissions import PermissionEngine
from codesage.tools import Tool, ToolRegistry, ToolResult, ToolUseContext
from codesage.tools.builtin.agent.agent import AgentTool


class FakeLLM:
    """Scripted stream; serves both parent and nested child loop."""

    def __init__(self, script):
        self.script = script
        self.calls = 0
        self.total_cost = [0.0]
        self.last_messages = None

    def stream(self, request, model="main"):
        self.last_messages = request.messages
        return self._gen()

    async def _gen(self):
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for ev in self.script[idx](self.calls):
            await asyncio.sleep(0)
            yield ev

    async def complete(self, request, model="main"):
        return LLMResponse(content=[])


def tool_use_event(name, tid, input_json):
    return [
        StreamEvent(type="tool_use_start", tool_use_id=tid, tool_name=name),
        StreamEvent(type="tool_use_delta", input_json_delta=input_json),
        StreamEvent(type="done", stop_reason="tool_use"),
    ]


def text_event(text="answer"):
    return [StreamEvent(type="text_delta", text=text), StreamEvent(type="done", stop_reason="end_turn")]


class AskTool(Tool):
    """与 Agent 同契约:needs_permissions=True、不进 SYSTEM_TOOLS → default
    模式下必然 ask。执行返回固定结果(零副作用)。"""

    name = "AskTool"
    description = "Asks permission by contract"
    input_schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return True

    async def _run(self, input, ctx):
        return ToolResult(f"ran:{input['text']}")


class CountingSink:
    """审计收集:每次决策恰一条断言用。"""

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _make_parent(llm, tmp_path, *, mode="default", request_permission=None,
                 sink: CountingSink | None = None) -> AgentLoop:
    session = Session("parent-1", tmp_path / "sessions")
    perms = PermissionEngine(audit_sink=sink)
    return AgentLoop(
        AgentLoopConfig(
            client=llm,
            tools=ToolRegistry([AgentTool(), AskTool()]),
            permissions=perms,
            system_prompt="parent system",
            cwd=tmp_path,
            session=session,
            max_turns=10,
            session_permissions={"allow": ["Agent"]},
            request_permission=request_permission,
        ),
        mode=mode,
    )


def _reg(overrides: dict | None = None) -> AgentRegistry:
    """内置三类型 + 测试注入的覆盖定义。"""
    reg = AgentRegistry()
    if overrides:
        reg._defs.update(overrides)  # 测试注入:跳过文件加载
    return reg


@pytest.fixture
def subagent_runner(tmp_path, monkeypatch):
    """SubagentRunner 的 session_root 注入 tmp_path,避免污染真实配置目录。"""
    from codesage.agents import SubagentRunner as _OrigRunner

    def patched_runner(parent, req, registry):
        return _OrigRunner(parent, req, registry, session_root=tmp_path / "subagents")

    monkeypatch.setattr("codesage.agents.SubagentRunner", patched_runner)
    return patched_runner


def _def(**kw):
    from codesage.agents.types import AgentDefinition

    defaults = dict(name="a", description="d", body="b")
    defaults.update(kw)
    return AgentDefinition(**defaults)


# ---- 生效模式 min 计算(§7)----


def test_min_mode_matrix():
    """plan < default < yolo 全组合:生效模式恒为较窄者。"""
    from codesage.agents.runner import _min_mode

    cases = [
        ("plan", "plan", "plan"), ("plan", "default", "plan"), ("plan", "yolo", "plan"),
        ("default", "plan", "plan"), ("default", "default", "default"), ("default", "yolo", "default"),
        ("yolo", "plan", "plan"), ("yolo", "default", "default"), ("yolo", "yolo", "yolo"),
    ]
    for parent, declared, expected in cases:
        assert _min_mode(parent, declared) == expected, (parent, declared)


def test_min_mode_inherit_and_unknown():
    from codesage.agents.runner import _min_mode

    assert _min_mode("yolo", None) == "yolo"          # 声明缺失 = 继承父
    assert _min_mode("default", "bogus") == "default"  # 未知声明按 default 兜底
    assert _min_mode("plan", "bogus") == "plan"        # 保守:不因声明放宽
    assert _min_mode("yolo", "bogus") == "default"     # 未知 → default,垃圾值不漏进 mode
    assert _min_mode("yolo", " YOLO ") == "yolo"       # 大小写/空白归一后再比较


def test_assemble_effective_mode(tmp_path, subagent_runner):
    """生效模式落点:子 loop.mode = min(父模式, 声明模式)。"""
    llm = FakeLLM([lambda i: text_event("x")])
    parent = _make_parent(llm, tmp_path, mode="default")
    runner = SubagentRunner(parent, SubagentRequest(prompt="p", name="general-purpose"),
                            _reg(), session_root=tmp_path / "subagents")
    assert runner._assemble().mode == "default"  # 无声明 = 继承

    runner = SubagentRunner(parent, SubagentRequest(prompt="p", name="g"),
                            _reg({"g": _def(name="g", permission_mode="yolo")}),
                            session_root=tmp_path / "subagents")
    assert runner._assemble().mode == "default"  # 父 default + 声明 yolo → default

    parent_yolo = _make_parent(llm, tmp_path, mode="yolo")
    runner = SubagentRunner(parent_yolo, SubagentRequest(prompt="p", name="g"),
                            _reg({"g": _def(name="g", permission_mode="plan")}),
                            session_root=tmp_path / "subagents")
    assert runner._assemble().mode == "plan"  # 父 yolo + 声明 plan → plan


# ---- ask 自动 deny(§7.2)+ 审计恰一条 ----


async def test_ask_auto_denied_without_parent_callback(tmp_path, subagent_runner):
    """子代理 request_permission=None → ask 直落 deny → 错误 tool_result 回流
    → 模型自愈继续;每次决策恰一条审计事件。"""
    sink = CountingSink()
    llm = FakeLLM([
        lambda i: tool_use_event("Agent", "a1", '{"name": "general-purpose", "prompt": "do it"}'),
        lambda i: tool_use_event("AskTool", "q1", '{"text": "side effect"}'),  # 子 turn1:ask
        lambda i: text_event("fine, skipped"),                                # 子 turn2:自愈
        lambda i: text_event("parent done"),
    ])
    parent = _make_parent(llm, tmp_path, sink=sink)
    async for _msg in parent.run("hi"):
        pass

    assert parent.last_stop_reason == "completed"
    # 子代理权限链不绕过 + 审计计数(§7.2 不变量):两侧 request_permission 均为
    # None,引擎既有 ask→自动 deny 路径生效(auto-deny 本体是引擎行为,规格红线
    # 零改动;此处只验证子代理接入同一决策链且每次决策恰一条审计)
    assert len(sink.events) == 2  # 父 Agent(allow)1 + 子 AskTool(ask)1
    ask_events = [e for e in sink.events if e.tool_name == "AskTool"]
    assert len(ask_events) == 1
    assert ask_events[0].decision == "ask"  # 引擎决策 ask;无回调 → 执行面自动 deny


async def test_send_message_permission_contract(tmp_path, subagent_runner):
    """SendMessage 与 Agent 同契约(§6.3):needs_permissions=True,子代理调用
    走完整决策链 + 审计(不在 allow 白名单 → ask 自动 deny)。"""
    sink = CountingSink()
    llm = FakeLLM([
        lambda i: tool_use_event("Agent", "a1",
                                 '{"name": "general-purpose", "prompt": "ping teammate", '
                                 '"run_in_background": true}'),
        lambda i: tool_use_event("SendMessage", "s1", '{"to": "bob", "message": "hi"}'),
        lambda i: text_event("denied, fine"),
        lambda i: text_event("parent done"),
    ])
    parent = _make_parent(llm, tmp_path, sink=sink)
    # 父工具池补 SendMessage;子代理是后台 → 工具池 ∩ ASYNC 白名单
    from codesage.tools.builtin.interaction.send_message import SendMessageTool
    parent.tools.register(SendMessageTool())
    async for _msg in parent.run("go"):
        pass
    # 后台子代理是独立 task,父 run 可能先完成 —— 等其终态(done 回调清集合)
    # 再断言审计,否则 SendMessage 决策发生在断言之后(时序竞争)。
    for _ in range(100):
        if not parent._subagent_tasks:
            break
        await asyncio.sleep(0.01)

    assert parent.last_stop_reason == "completed"
    sm_events = [e for e in sink.events if e.tool_name == "SendMessage"]
    assert len(sm_events) == 1
    assert sm_events[0].decision == "ask"  # 引擎决策 ask,与 Agent 同链


# ---- fork bubble(§7.3)----


async def test_fork_bubble_inherits_parent_callback(tmp_path, subagent_runner):
    """fork 子代理继承父 request_permission:ask 走父回调而非 deny。"""
    calls = []

    async def parent_approver(decision, tool, tool_input):
        calls.append(tool.name)
        return True

    llm = FakeLLM([
        lambda i: tool_use_event("Agent", "a1", '{"prompt": "fork me"}'),  # 父 turn1:fork
        lambda i: tool_use_event("AskTool", "q1", '{"text": "need this"}'),  # 子 turn1:ask
        lambda i: text_event("bubbled ok"),                               # 子 turn2
        lambda i: text_event("parent done"),                              # 父 turn2
    ])
    parent = _make_parent(llm, tmp_path, request_permission=parent_approver)
    async for _msg in parent.run("hi"):
        pass

    assert calls == ["AskTool"]                     # 子代理 ask 冒泡到父回调
    assert "bubbled ok" in str(llm.last_messages)   # 放行 → 子代理正常完成,最终文本回流父


async def test_named_agent_keeps_auto_deny(tmp_path, subagent_runner):
    """对照:普通(具名)子代理不继承父回调 —— ask 仍自动 deny。"""
    calls = []
    sink = CountingSink()

    async def parent_approver(decision, tool, tool_input):
        calls.append(tool.name)
        return True

    llm = FakeLLM([
        lambda i: tool_use_event("Agent", "a1", '{"name": "general-purpose", "prompt": "do it"}'),
        lambda i: tool_use_event("AskTool", "q1", '{"text": "side effect"}'),
        lambda i: text_event("fine, skipped"),
        lambda i: text_event("parent done"),
    ])
    parent = _make_parent(llm, tmp_path, request_permission=parent_approver, sink=sink)
    async for _msg in parent.run("hi"):
        pass

    assert calls == []  # 父回调未被子代理使用
    # 证据 = 审计:AskTool 决策 ask 恰一条(无回调 → 自动 deny,§7.2 不变)
    ask_events = [e for e in sink.events if e.tool_name == "AskTool"]
    assert len(ask_events) == 1 and ask_events[0].decision == "ask"
