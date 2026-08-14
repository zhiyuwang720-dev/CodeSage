"""Subagent runner tests (phase 13 S2): tool-pool assembly, contract
declarations, foreground nested run (mock LLM)."""

import asyncio
from pathlib import Path

import pytest

from codesage.agents import (
    ASYNC_AGENT_ALLOWED_TOOLS,
    AgentRegistry,
    SubagentRequest,
    SubagentRunner,
    assemble_subagent_tools,
)
from codesage.agents.types import AgentDefinition
from codesage.ai import LLMResponse, StreamEvent
from codesage.core import Session
from codesage.engine import AgentLoop, AgentLoopConfig
from codesage.permissions import PermissionEngine, SYSTEM_TOOLS
from codesage.tools import Tool, ToolRegistry, ToolResult, ToolUseContext
from codesage.tools.builtin import BUILTIN_TOOLS


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
        events = self.script[idx](self.calls)
        for ev in events:
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


class EchoTool(Tool):
    name = "Echo"
    description = "Echoes input"
    input_schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return False

    async def _run(self, input, ctx):
        return ToolResult(f"echo:{input['text']}")


def _pool() -> list[Tool]:
    return list(BUILTIN_TOOLS)


def _def(**kw) -> AgentDefinition:
    defaults = dict(name="a", description="d", body="b")
    defaults.update(kw)
    return AgentDefinition(**defaults)


# ---- L1/L2/L3 工具池过滤 ----


def test_assemble_removes_agent_from_pool():
    names = {t.name for t in assemble_subagent_tools(_pool(), _def())}
    assert "Agent" not in names
    assert "Read" in names


def test_assemble_whitelist_keeps_only_declared():
    names = {t.name for t in assemble_subagent_tools(_pool(), _def(tools=frozenset({"Read", "Bash"})))}
    assert names == {"Read", "Bash"}


def test_assemble_blacklist_explore():
    names = {t.name for t in assemble_subagent_tools(
        _pool(), _def(disallowed_tools=frozenset({"Agent", "Write", "Edit"})))}
    assert "Write" not in names and "Edit" not in names
    assert "Read" in names and "Bash" in names


def test_assemble_background_intersects_async_whitelist():
    names = {t.name for t in assemble_subagent_tools(_pool(), _def(), background=True)}
    assert names <= ASYNC_AGENT_ALLOWED_TOOLS
    assert {"Read", "Bash", "Edit", "Write", "TaskCreate"} <= names


def test_assemble_without_definition_is_full_pool_minus_agent():
    names = {t.name for t in assemble_subagent_tools(_pool(), None)}
    assert "Agent" not in names


# ---- 契约声明(§5.5)----


def test_agent_tool_contract_declarations():
    from codesage.tools.builtin.agent.agent import AgentTool

    tool = AgentTool()
    assert tool.needs_permissions({}) is True
    assert tool.is_concurrency_safe is True
    assert "Agent" not in SYSTEM_TOOLS  # 不进白名单:走完整决策链 + 审计


def test_agent_tool_spec_lists_agents():
    from codesage.tools.builtin.agent.agent import AgentTool

    spec = AgentTool().spec()
    assert "general-purpose" in spec.description
    assert "Explore" in spec.description
    assert "Plan" in spec.description
    assert "forkContext" in spec.description


def test_agent_tool_validation():
    from codesage.tools import ToolError
    from codesage.tools.builtin.agent.agent import AgentTool

    tool = AgentTool()
    with pytest.raises(ToolError, match="name is required"):
        tool.validate_input({"prompt": "x"})
    with pytest.raises(ToolError, match="prompt is required"):
        tool.validate_input({"name": "x", "prompt": ""})
    with pytest.raises(ToolError, match="max_turns"):
        tool.validate_input({"name": "x", "prompt": "p", "max_turns": 0})
    tool.validate_input({"name": "x", "prompt": "p", "max_turns": 5})  # ok


# ---- 前台嵌套 run(§5.4)----


def _make_parent(llm, tmp_path) -> AgentLoop:
    from codesage.tools.builtin.agent.agent import AgentTool

    session = Session("parent-1", tmp_path / "sessions")
    return AgentLoop(
        AgentLoopConfig(
            client=llm,
            tools=ToolRegistry([AgentTool(), EchoTool()]),
            permissions=PermissionEngine(),
            system_prompt="parent system",
            cwd=tmp_path,
            session=session,
            max_turns=10,
            # Agent 需显式 allow(needs_permissions=True,不进 SYSTEM_TOOLS)
            session_permissions={"allow": ["Agent"]},
        )
    )


async def test_foreground_nested_run_returns_child_text(tmp_path, monkeypatch):
    """父 turn1 发 Agent 工具调用 → 子 loop 独立跑完 → 结果回父 → 父收尾。"""
    from codesage.agents import SubagentRunner as _OrigRunner

    def patched_runner(parent, req, registry):
        return _OrigRunner(parent, req, registry, session_root=tmp_path / "subagents")

    monkeypatch.setattr("codesage.agents.SubagentRunner", patched_runner)

    llm = FakeLLM(
        [
            lambda i: tool_use_event("Agent", "a1", '{"name": "general-purpose", "prompt": "do it"}'),
            lambda i: text_event("child result text"),  # 子代理的模型回答
            lambda i: text_event("parent final"),
        ]
    )
    parent = _make_parent(llm, tmp_path)
    async for _msg in parent.run("hi"):
        pass

    assert llm.calls == 3  # 父 turn1 + 子 turn1 + 父 turn2
    assert parent.last_stop_reason == "completed"
    # 子代理结果以 tool_result 形态回流入父对话(§10.1 工具边界)
    assert "child result text" in str(llm.last_messages)
    # 子会话独立落盘于 subagents/ 下(typed-entry jsonl)
    subs = list((tmp_path / "subagents").glob("agent-*.jsonl"))
    assert len(subs) == 1
    assert subs[0].read_text(encoding="utf-8")


async def test_unknown_agent_name_is_error_not_crash(tmp_path, monkeypatch):
    """模型幻觉/注入命名不存在的 agent → 错误 tool_result,父 run 存活。"""
    from codesage.agents import SubagentRunner as _OrigRunner

    def patched_runner(parent, req, registry):
        return _OrigRunner(parent, req, registry, session_root=tmp_path / "subagents")

    monkeypatch.setattr("codesage.agents.SubagentRunner", patched_runner)

    llm = FakeLLM(
        [
            lambda i: tool_use_event("Agent", "a1", '{"name": "nope", "prompt": "do it"}'),
            lambda i: text_event("parent survives"),
        ]
    )
    parent = _make_parent(llm, tmp_path)
    async for _msg in parent.run("hi"):
        pass

    assert parent.last_stop_reason == "completed"  # 未崩溃
    assert llm.calls == 2  # 子代理从未启动,父正常进入第二轮
    # 错误信息以 tool_result 形态回流入父(repr 会转义引号,按片段断言)
    assert "unknown agent" in str(llm.last_messages) and "nope" in str(llm.last_messages)


async def test_nested_run_midrun_abort_cascade(tmp_path):
    """父 abort 在子代理执行中置位 → 级联中断 → is_error。"""
    from codesage.tools.builtin.agent.agent import AgentTool as _AgentTool

    class AbortParentTool(Tool):
        name = "AbortParent"
        description = "Sets the parent abort mid-run"

        def needs_permissions(self, input):
            return False

        async def _run(self, input, ctx):
            ctx.parent_loop.abort.set()  # 子代理工具内部触发父 abort
            await asyncio.sleep(0.05)
            return ToolResult("aborted")

    llm = FakeLLM(
        [
            lambda i: tool_use_event("AbortParent", "a1", '{"text": "x"}'),
            lambda i: text_event("never reached"),
        ]
    )
    # 子代理池含 AbortParent:子 turn1 调它 → 父 abort → 子 turn2 中断
    parent = _make_parent(llm, tmp_path)
    parent.tools = ToolRegistry([_AgentTool(), EchoTool(), AbortParentTool()])
    runner = SubagentRunner(
        parent,
        SubagentRequest(prompt="do it", name="general-purpose"),
        AgentRegistry(),
        session_root=tmp_path / "subagents",
    )
    result = await runner.run()
    assert result.is_error is True
    assert "interrupted" in result.content or "中止" in result.content


async def test_nested_run_parent_abort_blocks_spawn(tmp_path):
    """父 abort 已置位 → 子代理不启动,直接报错。"""
    llm = FakeLLM(
        [
            lambda i: tool_use_event("Agent", "a1", '{"name": "general-purpose", "prompt": "do it"}'),
            lambda i: text_event("never reached"),
        ]
    )
    parent = _make_parent(llm, tmp_path)
    parent.abort.set()
    runner = SubagentRunner(
        parent,
        SubagentRequest(prompt="do it", name="general-purpose"),
        AgentRegistry(),
        session_root=tmp_path / "subagents",
    )
    result = await runner.run()
    assert result.is_error is True
    assert "中止" in result.content
    assert llm.calls == 0  # 子 loop 从未启动


async def test_nested_run_child_max_turns_marks_error(tmp_path):
    """子代理 max_turns 超限 → is_error(§5.4 reason ∈ {error, max_turns, interrupted})。"""
    llm = FakeLLM([lambda i: tool_use_event("Echo", "e1", '{"text": "x"}')] * 5)
    parent = _make_parent(llm, tmp_path)
    runner = SubagentRunner(
        parent,
        SubagentRequest(prompt="loop", name="general-purpose", max_turns=2),
        AgentRegistry(),
        session_root=tmp_path / "subagents",
    )
    result = await runner.run()
    assert result.is_error is True
    assert "[子代理无文本输出:max_turns]" in result.content
