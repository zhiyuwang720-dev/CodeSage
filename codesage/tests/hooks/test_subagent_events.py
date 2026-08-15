"""Subagent*/Task* hook events (phase 13 S5, spec §11.2): runner single-point
trigger, SubagentStop additionalContext field-surface extension, matcher match
values, task events dead-config until S6 on_change."""

import asyncio
from pathlib import Path

import pytest

from codesage.agents import SubagentRequest, SubagentRunner
from codesage.ai import LLMResponse, StreamEvent
from codesage.core import Session
from codesage.engine import AgentLoop, AgentLoopConfig
from codesage.hooks import HookDispatchResult, HookInput, HookJSONOutput
from codesage.hooks.registry import _match_value
from codesage.permissions import PermissionEngine
from codesage.tools import Tool, ToolRegistry, ToolResult, ToolUseContext
from codesage.tools.builtin.agent.agent import AgentTool


class FakeLLM:
    def __init__(self, script):
        self.script = script
        self.calls = 0
        self.total_cost = [0.0]

    def stream(self, request, model="main"):
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


class SpyHooks:
    """HookManagerProtocol spy:记录 dispatch 调用,可编程返回。"""

    def __init__(self, **responses):
        self.calls = []  # (event, HookInput)
        self.responses = responses

    def has_hooks_for_event(self, event):
        return True

    async def dispatch(self, event, *, input, abort_event=None):
        self.calls.append((event, input))
        return self.responses.get(event, HookDispatchResult(event=event))

    async def notify(self, *args, **kwargs):
        pass


def _make_parent(llm, tmp_path, *, hooks=None) -> AgentLoop:
    return AgentLoop(
        AgentLoopConfig(
            client=llm,
            tools=ToolRegistry([AgentTool()]),
            permissions=PermissionEngine(),
            system_prompt="parent system",
            cwd=tmp_path,
            session=Session("parent-1", tmp_path / "sessions"),
            max_turns=10,
            hooks=hooks,
        ),
        mode="yolo",
    )


def _runner(parent, req, tmp_path):
    from codesage.agents import AgentRegistry
    return SubagentRunner(parent, req, AgentRegistry(), session_root=tmp_path / "subagents")


# ---- 单点触发(§11.2:runner 唯一触发位)----


async def test_subagent_start_stop_triggered_once(tmp_path):
    """前台一次 run:SubagentStart 恰一次 + SubagentStop 恰一次,agent_name 正确。"""
    hooks = SpyHooks()
    llm = FakeLLM([
        lambda i: tool_use_event("Agent", "a1", '{"name": "general-purpose", "prompt": "do it"}'),
        lambda i: text_event("child result"),
        lambda i: text_event("parent done"),
    ])
    parent = _make_parent(llm, tmp_path, hooks=hooks)
    runner = _runner(parent, SubagentRequest(prompt="do it", name="general-purpose"), tmp_path)
    result = await runner.run()
    assert not result.is_error
    events = [e for e, _ in hooks.calls]
    assert events.count("SubagentStart") == 1
    assert events.count("SubagentStop") == 1
    start_input = next(i for e, i in hooks.calls if e == "SubagentStart")
    assert start_input.extra["agent_name"] == "general-purpose"
    stop_input = next(i for e, i in hooks.calls if e == "SubagentStop")
    assert stop_input.extra["agent_name"] == "general-purpose"


async def test_subagent_stop_additional_context_accumulates(tmp_path):
    """SubagentStop 的 additionalContext → 父 loop 一次性 _hook_reminder(§6.2
    消费路径 2:注入下一次请求)。"""
    hooks = SpyHooks(**{"SubagentStop": HookDispatchResult(
        event="SubagentStop", additional_context="bg done: 42")})
    llm = FakeLLM([lambda i: text_event("child result")])
    parent = _make_parent(llm, tmp_path, hooks=hooks)
    runner = _runner(parent, SubagentRequest(prompt="bg"), tmp_path)
    await runner.run()
    assert parent._hook_reminder == "bg done: 42"


async def test_no_hooks_means_no_dispatch(tmp_path):
    """hooks=None → 零路径,不进管线(Subagent* 不触发)。"""
    llm = FakeLLM([lambda i: text_event("child result")])
    parent = _make_parent(llm, tmp_path, hooks=None)
    runner = _runner(parent, SubagentRequest(prompt="bg"), tmp_path)
    result = await runner.run()
    assert not result.is_error  # 无钩子照常跑


# ---- 字段面扩展(§11.2:仅 SubagentStop 接受 additionalContext)----


def test_subagent_stop_accepts_additional_context():
    out, _ = HookJSONOutput.parse('{"additionalContext": "ctx"}', "SubagentStop")
    assert out.additionalContext == "ctx"


def test_other_subagent_events_reject_additional_context():
    for event in ("SubagentStart", "TaskCreated", "TaskUpdated",
                  "TaskCompleted", "TaskDeleted"):
        with pytest.raises(Exception):
            HookJSONOutput.parse('{"additionalContext": "ctx"}', event)


def test_task_events_accept_common_fields():
    """Task* 四事件是死配置(S6 on_change 才激活):字段面接受通用字段。"""
    for event in ("TaskCreated", "TaskUpdated", "TaskCompleted", "TaskDeleted"):
        out, _ = HookJSONOutput.parse('{"systemMessage": "ok"}', event)
        assert out.systemMessage == "ok"
    # 未知字段仍拒绝(安全位不因事件扩展而松动)
    with pytest.raises(Exception):
        HookJSONOutput.parse('{"bogus": 1}', "TaskCreated")


# ---- matcher 匹配值(§11.2:Subagent* 按 agent_name,Task* 按 task_list_id)----


def test_matcher_match_values():
    inp = HookInput(session_id="s", cwd=".", session_path="p",
                    extra={"agent_name": "explorer", "task_list_id": "t-1"})
    assert _match_value("SubagentStart", inp) == "explorer"
    assert _match_value("SubagentStop", inp) == "explorer"
    assert _match_value("TaskCreated", inp) == "t-1"
    assert _match_value("TaskUpdated", inp) == "t-1"
    assert _match_value("TaskCompleted", inp) == "t-1"
    assert _match_value("TaskDeleted", inp) == "t-1"
    # 缺字段 → None(不匹配)
    bare = HookInput(session_id="s", cwd=".", session_path="p")
    assert _match_value("SubagentStart", bare) is None
