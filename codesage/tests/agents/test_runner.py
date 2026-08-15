"""Subagent runner tests (phase 13 S2/S3): tool-pool assembly, contract
declarations, foreground nested run, forkContext 三件套, step_attempt 埋点
(mock LLM)."""

import asyncio
import json
from pathlib import Path

import pytest

from codesage.agents import (
    ASYNC_AGENT_ALLOWED_TOOLS,
    AgentRegistry,
    SubagentRequest,
    SubagentRunner,
    assemble_subagent_tools,
)
from codesage.tools.builtin.interaction.task_create import TaskCreateTool
from codesage.agents.types import AgentDefinition
from codesage.ai import ContentBlock, LLMResponse, StreamEvent
from codesage.core import Session, SessionMessage, find_open_operations, user_message
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
    tool.validate_input({"prompt": "x"})  # fork:name 可缺省(§5.2,CC 隐式语义)
    with pytest.raises(ToolError, match="prompt is required"):
        tool.validate_input({"name": "x", "prompt": ""})
    with pytest.raises(ToolError, match="name must be"):
        tool.validate_input({"name": "", "prompt": "x"})  # 给了 name 必须非空
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


# ---- forkContext 三件套(§5.2)----


def _fork_msgs() -> list[SessionMessage]:
    """父历史:user → assistant(文本+tool_use)→ tool_result → assistant 纯文本
    → user 文本 → assistant(tool_use)→ tool_result。"""
    return [
        SessionMessage(role="user", content="hello"),
        SessionMessage(role="assistant", content=[
            ContentBlock(type="text", text="thinking out loud"),
            ContentBlock(type="tool_use", id="tu1", name="Echo", input={"text": "x"}),
        ]),
        SessionMessage(role="user", content=[
            ContentBlock(type="tool_result", tool_use_id="tu1", content="echo:x"),
        ]),
        SessionMessage(role="assistant", content="plain answer"),
        SessionMessage(role="user", content="follow-up"),
        SessionMessage(role="assistant", content=[
            ContentBlock(type="tool_use", id="tu2", name="Echo", input={"text": "y"}),
        ]),
        SessionMessage(role="user", content=[
            ContentBlock(type="tool_result", tool_use_id="tu2", content="echo:y"),
        ]),
    ]


def test_fork_history_three_pieces():
    """三件套字节级断言:assistant 仅 tool_use 块、tool_result → 占位 1:1
    配对、最后 user 消息由 run() 注入(函数不追加 prompt)。"""
    from codesage.agents.runner import FORK_TOOL_RESULT_PLACEHOLDER, build_fork_history

    out = build_fork_history(_fork_msgs())
    asst = [m for m in out if m.role == "assistant"]
    # 件1:assistant 仅 tool_use 块,块级过滤内容不变;纯 text 整条丢弃
    assert len(asst) == 2
    assert all(isinstance(m.content, list) and all(b.type == "tool_use" for b in m.content)
               for m in asst)
    assert [b.id for m in asst for b in m.content] == ["tu1", "tu2"]
    assert "plain answer" not in str(out)
    # 件2:tool_result 消息 → 占位文本,1:1 配对(配对数 == tool_use 数)
    tr = [m for m in out if m.content == FORK_TOOL_RESULT_PLACEHOLDER]
    assert len(tr) == len(asst) == 2
    # 工具输出不注入(§10):原始 tool_result 内容不进 fork 历史
    assert "echo:x" not in str(out) and "echo:y" not in str(out)
    # 普通 user 文本原样保留;件3:最后一条 user 消息 = req.prompt 由 run()
    # 注入 —— 函数产物以 tool_result 占位收尾,无 prompt
    assert [m.content for m in out if m.role == "user"] == [
        "hello", FORK_TOOL_RESULT_PLACEHOLDER, "follow-up", FORK_TOOL_RESULT_PLACEHOLDER,
    ]
    assert out[-1].role == "user"


def test_fork_history_truncation_alignment():
    """截断对齐(R1):63 条 → 最近 60,首条 tool_result 丢弃、末条孤儿
    tool_use 丢弃;截断后配对数 == tool_use 数仍成立。"""
    from codesage.agents.runner import FORK_TOOL_RESULT_PLACEHOLDER, build_fork_history

    msgs = []
    for i in range(31):
        msgs.append(SessionMessage(role="assistant", content=[
            ContentBlock(type="tool_use", id=f"tu{i}", name="Echo", input={"text": str(i)})]))
        msgs.append(SessionMessage(role="user", content=[
            ContentBlock(type="tool_result", tool_use_id=f"tu{i}", content=str(i))]))
    msgs.append(SessionMessage(role="assistant", content=[
        ContentBlock(type="tool_use", id="tu31", name="Echo", input={"text": "orphan"})]))
    out = build_fork_history(msgs)  # 63 → 60:丢首 tr1、丢末 tu31
    assert len(out) == 58
    asst = [m for m in out if m.role == "assistant"]
    tr = [m for m in out if m.content == FORK_TOOL_RESULT_PLACEHOLDER]
    assert len(tr) == len(asst) == 29  # 配对硬断言(含截断后)


def test_fork_history_broken_pairing_rejected():
    """畸形流(段内孤儿 tool_result,不在边界)→ 配对硬断言拒绝,不静默
    放行(R1)。"""
    from codesage.agents.runner import build_fork_history

    with pytest.raises(ValueError, match="pairing broken"):
        build_fork_history([
            SessionMessage(role="user", content="hi"),
            SessionMessage(role="user", content=[
                ContentBlock(type="tool_result", tool_use_id="x", content="r")]),
        ])


def test_fork_history_respects_max_messages_override():
    """max_messages 可覆盖(后备路径/调优用),默认 60。"""
    from codesage.agents.runner import build_fork_history

    out = build_fork_history(_fork_msgs(), max_messages=2)
    assert len(out) == 2
    assert [b.id for m in out if m.role == "assistant" for b in m.content] == ["tu2"]


# ---- fork E2E + step_attempt 埋点(§11.3)----


class RecordingLLM(FakeLLM):
    """FakeLLM + 每轮请求留档(检查子代理收到的 fork 上下文)。"""

    def __init__(self, script):
        super().__init__(script)
        self.requests = []

    def stream(self, request, model="main"):
        self.requests.append(request)
        return super().stream(request, model)


async def test_fork_nested_run_inherits_context(tmp_path, monkeypatch):
    """fork(name 缺省):子代理历史 = 父历史三件套;父工具输出不注入;
    最后 user 消息 = req.prompt;子会话独立落盘;父会话 step 配对 entry。"""
    from codesage.agents import SubagentRunner as _OrigRunner
    from codesage.agents.runner import FORK_TOOL_RESULT_PLACEHOLDER

    def patched_runner(parent, req, registry):
        return _OrigRunner(parent, req, registry, session_root=tmp_path / "subagents")

    monkeypatch.setattr("codesage.agents.SubagentRunner", patched_runner)

    llm = RecordingLLM([
        lambda i: tool_use_event("Echo", "e1", '{"text": "hello"}'),  # 父 turn1:Echo
        lambda i: tool_use_event("Agent", "a1", '{"prompt": "fork me"}'),  # 父 turn2:fork
        lambda i: text_event("child fork ok"),                        # 子 turn1
        lambda i: text_event("parent final"),                         # 父 turn3
    ])
    parent = _make_parent(llm, tmp_path)
    async for _msg in parent.run("hi"):
        pass

    assert llm.calls == 4
    assert parent.last_stop_reason == "completed"
    child_req = llm.requests[2]  # 第 3 次调用 = 子代理 turn1
    text = str(child_req.messages)
    assert "Echo" in text                      # 父 tool_use 块保留(内容不变)
    assert FORK_TOOL_RESULT_PLACEHOLDER in text  # tool_result → 占位
    assert "echo:hello" not in text            # 工具输出不注入(§10 切断传播面)
    assert "fork me" in text                   # 件3:最后 user 消息 = req.prompt
    assert child_req.messages[-1].content.endswith("fork me")
    # 子会话独立落盘 subagents/(sidechain,§5.3)
    subs = list((tmp_path / "subagents").glob("agent-*.jsonl"))
    assert len(subs) == 1
    # 父会话操作日志:step_attempt + step_completed 配对(§11.3;引擎另有
    # 12 §7.1 的 tool_started 埋点,只断言 step 对)
    kinds = [e.data.get("kind") for e in parent.session.entries
             if e.type == "operation" and e.data.get("kind", "").startswith("step_")]
    assert kinds == ["step_attempt", "step_completed"]


async def test_failed_step_records_step_failed(tmp_path, monkeypatch):
    """子代理终态失败(未知名 agent)→ step_failed 配对,find_open_operations
    不误报。"""
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
    kinds = [e.data.get("kind") for e in parent.session.entries
             if e.type == "operation" and e.data.get("kind", "").startswith("step_")]
    assert kinds == ["step_attempt", "step_failed"]


def test_find_open_operations_step_pair_closed(tmp_path):
    """kind 感知升级(13 §11.3):段以 step_completed/step_failed 收尾 →
    相邻配对完整,视为已完成不报(消除 --continue 误报后台子代理中断)。"""
    session = Session("s1", tmp_path)
    session.append_operation("step_attempt", tool="Agent", args_summary="x")
    session.append_operation("step_completed", tool="Agent", args_summary="x")
    assert find_open_operations(session.entries) == []
    session.append_operation("step_attempt", tool="Agent", args_summary="x2")
    session.append_operation("step_failed", tool="Agent", args_summary="x2")
    assert find_open_operations(session.entries) == []
    # 生产形态复合段:引擎 tool_started + step 对(实际父文件形状)
    session.append_operation("tool_started", tool="Agent")
    session.append_operation("step_attempt", tool="Agent", args_summary="x3")
    session.append_operation("step_completed", tool="Agent", args_summary="x3")
    assert find_open_operations(session.entries) == []


def test_named_agent_history_is_empty():
    """具名子代理历史为空(fork 专属路径);构造无需真实 parent。"""
    runner = SubagentRunner(
        None,  # type: ignore[arg-type] - _build_history 具名分支不触 parent
        SubagentRequest(prompt="p", name="general-purpose"),
        AgentRegistry(),
    )
    assert runner._build_history() == []


def test_find_open_operations_orphan_step_attempt_reported(tmp_path):
    """孤 step_attempt(运行中被硬打断,无终态)→ 照旧命中报中断(12 R6 行为不变)。"""
    session = Session("s1", tmp_path)
    session.append_operation("step_attempt", tool="Agent", args_summary="x")
    ops = find_open_operations(session.entries)
    assert len(ops) == 1
    assert ops[0].data["kind"] == "step_attempt"


def test_list_sessions_excludes_subagents(tmp_path):
    """§5.3 R8:list_sessions 排除 subagents/ 侧链转录(与 archive 同款),
    防污染 --continue//sessions。"""
    from codesage.core.session import list_sessions

    root = tmp_path / "sessions"
    Session("s1", root).append(user_message("a"))
    Session("agent-1", root / "subagents").append(user_message("b"))
    Session("agent-2", root / "subagents").append(user_message("c"))
    Session("archived", root / "archive").append(user_message("d"))
    assert [p.stem for p in list_sessions(root)] == ["s1"]


# ---- 13 §11.1:task_list_id 继承 / 自动 owner / 共享同一列表 ----

def test_assemble_inherits_parent_task_list_id(tmp_path):
    """子 loop.task_list_id == 父的(与父共享同一任务列表);_agent_name:
    定义名子代理 = 定义名,fork = 唯一 agent_id(防身份碰撞)。"""
    parent = _make_parent(FakeLLM([lambda i: text_event("x")]), tmp_path)
    parent.task_list_id = "team-a"  # 模拟父已继承/显式设置

    runner = SubagentRunner(parent, SubagentRequest(prompt="p", name="general-purpose"),
                            AgentRegistry(), session_root=tmp_path / "subagents")
    loop = runner._assemble()
    assert loop.task_list_id == "team-a"
    assert loop._agent_name == "general-purpose"

    # forkContext(无 name):owner 身份 = 唯一 agent_id,列表仍共享
    runner = SubagentRunner(parent, SubagentRequest(prompt="p"),
                            AgentRegistry(), session_root=tmp_path / "subagents")
    loop = runner._assemble()
    assert loop.task_list_id == "team-a"
    assert loop._agent_name == runner._agent_id  # 非字面量 "forkContext"
    assert loop._agent_name != "forkContext"


def test_system_prompt_shares_parent_task_list(tmp_path):
    """§9 静态引导:task_list_id 传父的,措辞为「共享同一任务列表」。"""
    from codesage.agents.runner import build_subagent_system_prompt

    prompt = build_subagent_system_prompt(
        "base", "worker-1", "body", "team-a", tmp_path)
    assert "任务列表 id=team-a" in prompt
    assert "共享同一任务列表" in prompt
    assert "独立列表" not in prompt  # 措辞已恢复,无旧分离语义残留


async def test_task_create_auto_owner_shared_list_e2e(tmp_path, monkeypatch):
    """E2E(13 §11.1):子代理 TaskCreate → owner 自动 = agent 名;任务落在父的
    task_list_id 目录 —— 「teammate 协作同一张列表」。"""
    from codesage.core import Session as _Session
    from codesage.core.tasks import TaskStore
    from codesage.tools.builtin.agent.agent import AgentTool as _AgentTool
    import codesage.core.tasks.storage as storage_mod

    monkeypatch.setattr(storage_mod, "_store", TaskStore(tmp_path / "store"))
    session = _Session("parent-1", tmp_path / "sessions")
    seen = []  # 子代理视角的 TaskList 输出收集(父 last_messages 不可见子工具结果)

    class ListTool(Tool):
        name = "TaskList"
        description = "Lists tasks"
        input_schema = {"type": "object", "properties": {}}
        is_concurrency_safe = True

        def needs_permissions(self, input):
            return False

        async def _run(self, input, ctx):
            from codesage.core.tasks import get_task_store

            tasks = get_task_store().list(ctx.task_list_id)
            out = "\n".join(
                f"#{t.id} [{t.status.value}] {t.subject} (owner={t.owner})" for t in tasks)
            seen.append((ctx.task_list_id, out))
            return ToolResult(out)

    llm = FakeLLM([
        lambda i: tool_use_event("Agent", "a1",
                                 '{"name": "general-purpose", "prompt": "make a task"}'),
        lambda i: tool_use_event("TaskCreate", "t1",
                                 '{"subject": "Sub task", "description": "by teammate"}'),
        lambda i: tool_use_event("TaskList", "t2", "{}"),  # 子代理读同一列表
        lambda i: text_event("done"),
        lambda i: text_event("parent final"),
    ])
    parent = AgentLoop(
        AgentLoopConfig(
            client=llm,
            tools=ToolRegistry([_AgentTool(), ListTool(), TaskCreateTool()]),
            permissions=PermissionEngine(),
            system_prompt="parent system",
            cwd=tmp_path,
            session=session,
            max_turns=10,
            session_permissions={"allow": ["Agent"]},
        )
    )
    async for _msg in parent.run("hi"):
        pass

    # 任务落在父会话 id 目录(共享列表),owner = 子代理定义名
    task_file = tmp_path / "store" / "parent-1" / "1.json"
    assert task_file.exists()
    task = json.loads(task_file.read_text(encoding="utf-8"))
    assert task["owner"] == "general-purpose"
    # 子代理经同一 task_list_id 注入读到该任务(共享列表 + 自动 owner 双证)
    assert seen == [("parent-1", "#1 [pending] Sub task (owner=general-purpose)")]
    assert parent.last_stop_reason == "completed"
