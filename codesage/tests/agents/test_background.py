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
from codesage.tools import Tool, ToolError, ToolRegistry, ToolResult, ToolUseContext
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


# ---- §6.4 后台完成自动注入父上下文(CC task-notification 同款)----


async def test_background_completion_auto_injected_into_parent(tmp_path):
    """后台完成 → <task-notification> 进父 _notifications:status/result 全量
    进 <result> 段 + session_path 供 Read —— 父模型下一轮自动感知,无需用户
    转述;前台完成 → 结果经 tool_result 回收,队列保持空。"""
    llm = RecordingLLM([lambda i: text_event("child done")])
    parent = _make_parent(llm, tmp_path)
    runner = _runner(parent, SubagentRequest(prompt="bg", run_in_background=True), tmp_path)
    runner.launch()
    await _wait_task(next(iter(parent._subagent_tasks)))
    msgs = []
    while True:
        try:
            msgs.append(parent._notifications.get_nowait())
        except asyncio.QueueEmpty:
            break
    assert len(msgs) == 1
    text = msgs[0]
    assert text.startswith("<task-notification>") and text.endswith("</task-notification>")
    assert "<status>completed</status>" in text
    assert "<result>child done</result>" in text
    assert str(runner._session_path) in text  # 父模型可按此 Read 转录取详情

    # 前台:父阻塞消费结果,不注入
    parent = _make_parent(llm, tmp_path)
    runner = _runner(parent, SubagentRequest(prompt="fg"), tmp_path)
    result = await runner.run()
    assert not result.is_error
    assert parent._notifications.empty()


def _text_content(content) -> str:
    """消息 content(str 原样或 block 列表)→ 拼接文本。"""
    if isinstance(content, str):
        return content
    parts = []
    for c in content:
        if isinstance(c, str):
            parts.append(c)
        elif c.type == "text":
            parts.append(c.text or "")
    return "\n".join(parts)


async def test_notification_injected_into_parent_message_flow(tmp_path):
    """§6.4 注入位:父 loop 每轮迭代前 drain _notifications → user 角色进
    Message 流(模型下一轮看到 + 流式渲染);跨 turn 积压(父 turn 结束时完成
    的子代理,下一轮输入时注入)。"""
    llm = RecordingLLM([
        lambda i: tool_use_event("SendMessage", "s1", '{"to": "ghost", "message": "ping"}'),
        lambda i: text_event("turn two"),
    ])
    parent = _make_parent(llm, tmp_path)
    parent._notifications.put_nowait(
        "<task-notification>\n<status>completed</status>\n<summary>bg done</summary>\n"
        "</task-notification>"
    )
    yielded = []
    async for msg in parent.run("go"):
        yielded.append(msg)
    hits = [_text_content(m.content) for m in yielded]
    assert any("<task-notification>" in t for t in hits), f"yielded={hits!r}"
    assert len(llm.requests) >= 2
    texts = [_text_content(m.content) for m in llm.requests[1]]
    assert any("bg done" in t for t in texts)  # 第二次 LLM 调用时通知已注入


async def test_aborted_background_notifies_parent(tmp_path):
    """§6.4:父中止时后台子代理同样通知父上下文 —— 父模型需要知道子代理消失
    的原因,否则只见 async_launched 后无声无息。两条路径:子代理自检父 abort
    → failed;watcher cancel → cancelled(status 由竞态决定,通知必达)。
    abort 先于 launch:_run_once 入口检查必中,确定性失败路径。"""
    llm = RecordingLLM([lambda i: text_event("never")])
    parent = _make_parent(llm, tmp_path)
    parent.abort.set()
    runner = _runner(parent, SubagentRequest(prompt="bg", run_in_background=True), tmp_path)
    runner.launch()
    task = next(iter(parent._subagent_tasks))
    await _wait_task(task)  # 失败通知路径:run 正常终态,无异常
    text = parent._notifications.get_nowait()
    assert "<task-notification>" in text
    assert "<status>failed</status>" in text or "<status>cancelled</status>" in text


async def test_assembly_failure_notifies_parent(tmp_path):
    """§6.4:装配失败(未知名 agent)后台场景同样注入 failed —— 否则父只见
    async_launched,永远不知道子代理失败(任务异常被 _consume_exception 吞)。"""
    from codesage.agents import AgentRegistry

    llm = RecordingLLM([lambda i: text_event("never")])
    parent = _make_parent(llm, tmp_path)
    runner = SubagentRunner(
        parent,
        SubagentRequest(prompt="p", name="nope", run_in_background=True),
        AgentRegistry(),  # builtin 三类型,无 "nope" → KeyError → ToolError
        session_root=tmp_path / "subagents",
    )
    runner.launch()
    with pytest.raises(ToolError):
        await _wait_task(next(iter(parent._subagent_tasks)))  # 装配失败原样上抛(S2)
    text = parent._notifications.get_nowait()  # 但父上下文已收到 failed 通知
    assert "<status>failed</status>" in text


# ---- 13 S1:唤醒信号 + 空输入启动(REPL 空闲自动继续的引擎入口)----


def _notif_xml(summary):
    return (f"<task-notification>\n<status>completed</status>\n<summary>{summary}</summary>\n"
            "</task-notification>")


async def test_background_done_sets_wake_event(tmp_path):
    """后台完成 → _notifications_event 被 set(§6.4 put 成功后),REPL 空闲
    据此自动继续(S2 消费)。"""
    llm = RecordingLLM([lambda i: text_event("child done")])
    parent = _make_parent(llm, tmp_path)
    assert not parent._notifications_event.is_set()
    runner = _runner(parent, SubagentRequest(prompt="bg", run_in_background=True), tmp_path)
    runner.launch()
    await _wait_task(next(iter(parent._subagent_tasks)))
    assert parent._notifications_event.is_set()
    assert not parent._notifications.empty()


async def test_drain_clears_wake_event(tmp_path):
    """drain 消费后队列空 → event clear(不残留误唤醒)。"""
    llm = RecordingLLM([lambda i: text_event("turn")])
    parent = _make_parent(llm, tmp_path)
    parent._notifications.put_nowait(_notif_xml("bg done"))
    parent._notifications_event.set()
    async for _m in parent.run("go"):
        pass
    assert parent._notifications.empty()
    assert not parent._notifications_event.is_set()


async def test_drain_keeps_event_on_mid_drain_arrival(tmp_path):
    """保活:drain 的 yield 暂停期间新通知到达 → event 保持 set(无条件 clear
    会丢信号)。父 turn 完成后通知留队列 —— 正是 S2 调 run(None) 的场景。"""
    llm = RecordingLLM([lambda i: text_event("answer")])
    parent = _make_parent(llm, tmp_path)
    parent._notifications.put_nowait(_notif_xml("A"))
    parent._notifications_event.set()
    gen = parent.run("go")
    await gen.__anext__()  # 首条 user 消息
    m = await gen.__anext__()  # drain 中 yield 通知 A(生成器暂停于此)
    assert "<task-notification>" in _text_content(m.content)
    # 暂停期间(runner 在父 turn 间隙完成)新通知到达
    parent._notifications.put_nowait(_notif_xml("B"))
    parent._notifications_event.set()
    m2 = await gen.__anext__()  # 推进:drain 收尾 + 保活,回到主循环
    assert m2.role == "assistant"
    # 队列非空(通知 B 未消费)→ 保活后 event 必须仍 set
    assert parent._notifications_event.is_set()
    assert not parent._notifications.empty()
    # 父 turn 就此完成(无工具调用);B 留队列、信号保持 —— S2 据此 run(None)
    rest = [x async for x in gen]
    assert rest == []
    assert parent._notifications_event.is_set()
    # 自动继续轮消费 B,终态队列空 + event clear
    drained = [x async for x in parent.run()]
    assert any("B" in _text_content(x.content) for x in drained)
    assert parent._notifications.empty()
    assert not parent._notifications_event.is_set()


async def test_run_none_injects_pending_notification(tmp_path):
    """run(None) 自动继续轮:预置通知正常注入(模型请求可见),消息流无首条
    空 user 消息。"""
    llm = RecordingLLM([lambda i: text_event("auto answer")])
    parent = _make_parent(llm, tmp_path)
    parent._notifications.put_nowait(_notif_xml("bg done"))
    parent._notifications_event.set()
    yielded = []
    async for msg in parent.run():
        yielded.append(msg)
    # 首条注入消息即通知(user 角色),不是空输入消息
    assert yielded[0].role == "user"
    assert "<task-notification>" in _text_content(yielded[0].content)
    # 模型请求里能看到通知
    texts = [_text_content(m.content) for m in llm.requests[0]]
    assert any("bg done" in t for t in texts)
