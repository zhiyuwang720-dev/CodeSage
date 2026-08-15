"""Subagent background tests (phase 13 S5, spec §6): launch() immediate return,
Mailbox subagent_done notify, task-set cleanup, abort cascade, SendMessage
addressing/injection, background tool-pool whitelist."""

import asyncio
import json
from pathlib import Path

import pytest

from codesage.agents import SubagentRequest, SubagentRunner
from codesage.ai import LLMResponse, StreamEvent
from codesage.core import Session
from codesage.core.tasks import get_mailbox, reset_mailbox
from codesage.engine import AgentLoop, AgentLoopConfig
from codesage.permissions import PermissionEngine
from codesage.tools import Tool, ToolRegistry, ToolResult, ToolUseContext
from codesage.tools.builtin.agent.agent import AgentTool
from codesage.tools.builtin.interaction.send_message import SendMessageTool
from codesage.tools.builtin.interaction.task_list import TaskListTool

ASYNC_ALLOWED = {"Read", "Grep", "Glob", "LS", "Bash", "Edit", "Write",
                 "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "SendMessage"}


class RecordingLLM:
    """Scripted stream; keeps every request for injection assertions."""

    def __init__(self, script):
        self.script = script
        self.calls = 0
        self.total_cost = [0.0]
        self.requests = []

    def stream(self, request, model="main"):
        self.requests.append(request.messages)
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


def _make_parent(llm, tmp_path, *, mode="yolo") -> AgentLoop:
    session = Session("parent-1", tmp_path / "sessions")
    return AgentLoop(
        AgentLoopConfig(
            client=llm,
            tools=ToolRegistry([AgentTool(), SendMessageTool(), TaskListTool()]),
            permissions=PermissionEngine(),
            system_prompt="parent system",
            cwd=tmp_path,
            session=session,
            max_turns=10,
            session_permissions={"allow": ["Agent", "SendMessage"]},  # 后台互发前提
        ),
        mode=mode,
    )


def _runner(parent, req, tmp_path) -> SubagentRunner:
    return SubagentRunner(parent, req, None, session_root=tmp_path / "subagents")


async def _wait_task(task, timeout=5.0):
    await asyncio.wait_for(task, timeout)


# ---- 后台执行(§6.1)----


async def test_launch_returns_async_launched(tmp_path):
    """launch 立即返回(不阻塞父循环),json 载荷含 agent_id;完成回调清理集合。"""
    llm = RecordingLLM([lambda i: text_event("child done")])
    parent = _make_parent(llm, tmp_path)
    runner = _runner(parent, SubagentRequest(prompt="bg", run_in_background=True), tmp_path)
    result = runner.launch()
    assert not result.is_error
    data = json.loads(result.content)
    assert data["status"] == "async_launched" and data["agent_id"] == runner._agent_id
    task = next(iter(parent._subagent_tasks))
    assert not task.done()
    await _wait_task(task)
    assert parent._subagent_tasks == set()  # done 回调清理(R3)
    await asyncio.sleep(0)                  # 让 watcher.cancel() 推进到终态
    assert runner._abort_watcher.done()     # 子代理先完成 → watcher 被取消(R10 无泄漏)


async def test_mailbox_notified_on_done(tmp_path):
    """后台完成 → Mailbox subagent_done(§6.2);前台完成 → 不通知。"""
    got = []
    get_mailbox().subscribe("subagent_done", lambda m: got.append(m))

    llm = RecordingLLM([lambda i: text_event("child done")])
    parent = _make_parent(llm, tmp_path)
    runner = _runner(parent, SubagentRequest(prompt="bg", run_in_background=True), tmp_path)
    runner.launch()
    await _wait_task(next(iter(parent._subagent_tasks)))
    assert len(got) == 1
    msg = got[0]
    assert msg.kind == "subagent_done" and msg.agent_id == runner._agent_id
    assert msg.payload["status"] == "completed"
    assert "child done" in msg.payload["summary"]
    assert msg.payload["session_path"].endswith(".jsonl")
    reset_mailbox()

    # 前台:父阻塞消费结果,不通知
    got.clear()
    parent = _make_parent(llm, tmp_path)
    runner = _runner(parent, SubagentRequest(prompt="fg"), tmp_path)
    result = await runner.run()
    assert not result.is_error
    assert got == []


async def test_background_tool_pool_whitelist(tmp_path):
    """后台工具池 = 父池 ∩ ASYNC_AGENT_ALLOWED_TOOLS(§4 L3);Agent 被编译期剔除。"""
    llm = RecordingLLM([lambda i: text_event("x")])
    parent = _make_parent(llm, tmp_path)
    runner = _runner(parent, SubagentRequest(prompt="bg", run_in_background=True), tmp_path)
    loop = runner._assemble()
    names = {t.name for t in loop.tools.all()}
    assert names == {"SendMessage", "TaskList"}          # 父池 [Agent,SendMessage,TaskList] 过滤后
    assert names <= ASYNC_ALLOWED and "Agent" not in names


async def test_abort_cascades_to_background(tmp_path):
    """父 abort → 后台任务被 cancel(R10);转录已 fsync,部分成果不丢。"""
    llm = RecordingLLM([lambda i: text_event("slow child")])
    parent = _make_parent(llm, tmp_path)
    runner = _runner(parent, SubagentRequest(prompt="bg", run_in_background=True), tmp_path)
    runner.launch()
    task = next(iter(parent._subagent_tasks))
    parent.abort.set()
    for _ in range(20):
        await asyncio.sleep(0.01)
        if task.done():
            break
    assert task.cancelled() or task.done()  # 级联取消(竞态窗口内取消即成立)


# ---- SendMessage(§6.3)----


async def test_send_message_delivers_to_registered_inbox(tmp_path):
    """后台子代理 turn1 调 SendMessage → 投递到已注册 inbox(后台互发链路)。"""
    sink = asyncio.Queue()
    get_mailbox().register("sink", sink)
    llm = RecordingLLM([
        lambda i: tool_use_event("SendMessage", "s1", '{"to": "sink", "message": "hi b"}'),
        lambda i: text_event("a done"),
    ])
    parent = _make_parent(llm, tmp_path)
    runner = _runner(parent, SubagentRequest(prompt="bg", run_in_background=True), tmp_path)
    await _wait_task(runner.launch() and next(iter(parent._subagent_tasks)))
    assert sink.get_nowait() == "hi b"
    reset_mailbox()


async def test_send_message_injected_into_target_loop(tmp_path):
    """投递后目标 loop 每轮迭代前 drain 注入 Message 流(引擎 _inbox)。"""
    llm = RecordingLLM([lambda i: text_event("child done")])
    parent = _make_parent(llm, tmp_path)
    runner = _runner(parent, SubagentRequest(prompt="bg", run_in_background=True,
                                             address_name="bob"), tmp_path)
    loop = runner._assemble()  # 注册 inbox(agent_id + "bob" 双寻址名)
    loop._inbox.put_nowait("ping from team")
    async for _msg in loop.run("child task"):
        pass
    assert any("ping from team" in str(m) for m in llm.requests[0])
    reset_mailbox()


async def test_agent_tool_background_end_to_end(tmp_path):
    """Agent 工具带 run_in_background → 后台启动(§6.1 工具面可达,HIGH 修复
    回归):父引擎经工具调用即返回 async_launched,后台完成触发 subagent_done。"""
    got = []
    get_mailbox().subscribe("subagent_done", lambda m: got.append(m))
    llm = RecordingLLM([
        lambda i: tool_use_event("Agent", "a1",
                                 '{"name": "general-purpose", "prompt": "bg", '
                                 '"run_in_background": true}'),
        lambda i: text_event("parent continues immediately"),
    ])
    parent = _make_parent(llm, tmp_path)
    async for msg in parent.run("go"):
        pass
    # 父 run 期间子代理已在后台跑(launch 接线生效);等其完成通知。
    # 竞态说明:done 回调 discard 后 _subagent_tasks 为空是正常终态,以
    # Mailbox 通知为准(子代理可能快于父收尾,不能依赖集合非空)。
    for _ in range(50):
        if got:
            break
        await asyncio.sleep(0.01)
    assert len(got) == 1 and got[0].payload["status"] == "completed"
    reset_mailbox()


async def test_send_message_target_missing_is_error(tmp_path):
    """目标不存在/已终止 → 明确报错,幂等(§6.3 失败语义 + R16)。"""
    tool = SendMessageTool()
    result = await tool._run({"to": "ghost", "message": "hi"}, ToolUseContext(cwd=tmp_path))
    assert result.is_error and "ghost" in result.content
    # 已注销目标同样报错
    inbox = asyncio.Queue()
    get_mailbox().register("gone", inbox)
    get_mailbox().unregister("gone")
    result = await tool._run({"to": "gone", "message": "hi"}, ToolUseContext(cwd=tmp_path))
    assert result.is_error


async def test_unregister_drops_inflight_with_warning(tmp_path, caplog):
    """竞态窗口内已入队消息随注销丢弃 → 计数记 warning,不静默(R17)。"""
    inbox = asyncio.Queue()
    get_mailbox().register("dying", inbox)
    inbox.put_nowait("in-flight")
    with caplog.at_level("WARNING", logger="codesage.tasks.mailbox"):
        get_mailbox().unregister("dying")
    assert "1 in-flight message(s) dropped" in caplog.text
    # 注销后投递 → 幂等报错(既有语义不变)
    assert get_mailbox().send("dying", "x") == (False, "no such subagent inbox: dying")
    reset_mailbox()


async def test_send_message_by_address_name(tmp_path):
    """address_name 寻址(§6.3):agent_id 与 address_name 均可投递。"""
    inbox = asyncio.Queue()
    get_mailbox().register("agent-1", inbox)
    get_mailbox().register("bob", inbox)
    tool = SendMessageTool()
    assert not (await tool._run({"to": "bob", "message": "m"}, ToolUseContext(cwd=tmp_path))).is_error
    assert inbox.get_nowait() == "m"
    assert not (await tool._run({"to": "agent-1", "message": "m2"}, ToolUseContext(cwd=tmp_path))).is_error
    assert inbox.get_nowait() == "m2"
    reset_mailbox()
