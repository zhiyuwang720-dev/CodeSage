"""Subagent execution (phase 13 S2/S5): tool-pool assembly, foreground nested
run, background launch (S5), forkContext (S3), worktree isolation (S7).

S2 delivers the foreground path: `assemble_subagent_tools` (compiled-time
recursion ban), `build_subagent_system_prompt` (spec §9) and
`SubagentRunner.run()` — a nested AgentLoop run whose final assistant text
becomes the tool_result, with the parent abort cascading down.
S5 adds `launch()` (background + Mailbox notify), SendMessage inbox plumbing
and the SubagentStart/SubagentStop hook events.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from ..config import paths
from ..core import Session
from ..core.messages import SessionMessage
from ..core.tasks import MailMessage, SUBAGENT_DONE, get_mailbox, get_task_store
from ..engine import AgentLoop, AgentLoopConfig
from ..hooks import HookInput
from ..permissions import normalize_mode
from ..tools import Tool, ToolError, ToolRegistry, ToolResult
from .worktree import (  # S7 §5.1/§5.4
    WorktreeError,
    cleanup_worktree,
    create_worktree,
    worktree_branch,
)

#: 权限模式等级(§7 只收窄不放宽):plan < default < yolo
_MODE_RANK = {"plan": 0, "default": 1, "yolo": 2}


def _min_mode(parent: str, declared: str | None) -> str:
    """生效模式 = min(父模式, 声明模式);声明缺失 = 继承父(§7)。

    声明值先经 normalize_mode 归一化(未知/大小写/空白 → default),只收窄
    精神:识别不了就保守,垃圾值绝不漏进 loop.mode。
    """
    if declared is None:
        return parent
    d = normalize_mode(declared).value  # 未知 → default(权限链边界同款兜底)
    return parent if _MODE_RANK.get(parent, 1) <= _MODE_RANK[d] else d

#: 编译期禁递归:Agent 工具从子代理工具池剔除(L1 元工具层,spec §4)。
SUBAGENT_DISALLOWED_TOOL_NAMES: frozenset[str] = frozenset({"Agent"})
#: 后台子代理白名单(L3,spec §4):能干活(read/search/bash/edit/write/task 协作),
#: 排除需交互弹窗的元工具;权限仍走 min 收窄 + 审计,ask→deny 无 UI 阻塞。
#: SendMessage 为队友协作工具,与 Task×4 同族(§6.3,CC IN_PROCESS_TEAMMATE_
#: ALLOWED_TOOLS 实测 = Task×4 + SendMessage)。
ASYNC_AGENT_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {"Read", "Grep", "Glob", "LS", "Bash", "Edit", "Write",
     "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "SendMessage"}
)

#: forkContext(§5.2):历史截断上限 —— 只继承最近 60 条消息,防父历史过长
#: 撑爆子代理上下文(R1 对齐配对:截断后仍须 tool_result 数 == tool_use 数)。
FORK_MAX_MESSAGES = 60
#: fork 占位文本:父工具输出不注入子代理(§10 切断父子双向传播面)。
FORK_TOOL_RESULT_PLACEHOLDER = "[tool_result omitted (fork context)]"


def assemble_subagent_tools(
    parent_pool: list[Tool],
    definition: "AgentDefinition | None",
    background: bool = False,
) -> list[Tool]:
    """三层过滤流水线(对齐 CC filterToolsForAgent,spec §4):

    父池全集 → L1 减 SUBAGENT_DISALLOWED_TOOL_NAMES → L2 按定义
    tools(白)/disallowed_tools(黑) → L3 后台时 ∩ ASYNC_AGENT_ALLOWED_TOOLS。
    """
    pool = [t for t in parent_pool if t.name not in SUBAGENT_DISALLOWED_TOOL_NAMES]
    if definition is not None:
        if definition.tools is not None:
            pool = [t for t in pool if t.name in definition.tools]
        if definition.disallowed_tools:
            pool = [t for t in pool if t.name not in definition.disallowed_tools]
    if background:
        pool = [t for t in pool if t.name in ASYNC_AGENT_ALLOWED_TOOLS]
    return pool


def build_subagent_system_prompt(
    base: str, name: str, body: str, task_list_id: str, cwd: Path
) -> str:
    """spec §9:父系统提示 + agent 头/正文 + 任务引导(静态段落)+ 环境细节。

    forkContext 子代理用同一函数(base 原样复用即父前缀一致)。
    task_list_id(13 §11.1)传**父的** task_list_id:子代理与父共享同一
    任务列表 —— 「teammate 协作同一张列表」;引擎侧 loop.task_list_id
    同步继承,两者一致(措辞与注入点不脱节)。
    """
    prompt = f"{base}\n\n# Agent: {name}\n\n{body}" if body else f"{base}\n\n# Agent: {name}"
    prompt += (
        f"\n\n任务与协作:你的任务列表 id={task_list_id}。TaskCreate/TaskGet/"
        "TaskList/TaskUpdate 操作该列表(与主会话共享同一任务列表)。"
    )
    prompt += f"\n\n环境:工作目录 {cwd} (绝对路径)。平台:Windows。"
    return prompt


def _new_agent_id() -> str:
    return datetime.now().strftime("agent-%Y%m%d-%H%M%S-%f")


def build_fork_history(
    messages: list[SessionMessage], max_messages: int = FORK_MAX_MESSAGES
) -> list[SessionMessage]:
    """spec §5.2 fork 三件套(纯函数,单测硬断言面):

    1. assistant 消息仅保留 tool_use 块(块级过滤,内容不变;纯 text 整条丢弃)
    2. tool_result 消息 → 占位文本,与 tool_use 1:1 顺序配对
    3. 最后 user 消息 = req.prompt,由 run(user_input) 注入,不在此构造

    截断(R1):取最近 *max_messages* 条后,首条 tool_result 丢弃、末条
    tool_use 丢弃;返回前硬断言 tool_result 数 == tool_use 数 —— 防孤儿
    tool_result 打爆 API。配对被打破(畸形流)→ ValueError 防御性拒绝。
    """
    out: list[SessionMessage] = []
    for msg in messages:
        if msg.role == "assistant":
            if not isinstance(msg.content, list):
                continue  # 纯文本 assistant:整条丢弃
            blocks = [b for b in msg.content if b.type == "tool_use"]
            if blocks:
                out.append(SessionMessage(role="assistant", content=blocks))
        elif isinstance(msg.content, list) and any(
            b.type == "tool_result" for b in msg.content
        ):
            out.append(SessionMessage(role="user", content=FORK_TOOL_RESULT_PLACEHOLDER))
        elif msg.content:  # 普通用户文本原样保留(空消息丢弃)
            out.append(msg)
    if len(out) > max_messages:
        out = out[-max_messages:]
    # 边界对齐(R1 硬性,无条件):首条 tool_result → 其配对 tool_use 在窗口外
    # 丢弃;末条 tool_use → 其 tool_result 未写入 —— 截断窗口外,或即 fork
    # 调用自身(执行时尚未回填 tool_result,必须剔除防自引用)。
    while out and out[0].content == FORK_TOOL_RESULT_PLACEHOLDER:
        out.pop(0)
    while out and isinstance(out[-1].content, list) and any(
        b.type == "tool_use" for b in out[-1].content
    ):
        out.pop()
    tool_uses = sum(
        len([b for b in m.content if b.type == "tool_use"])
        for m in out
        if isinstance(m.content, list)
    )
    tool_results = sum(m.content == FORK_TOOL_RESULT_PLACEHOLDER for m in out)
    if tool_results != tool_uses:
        raise ValueError(
            f"fork history pairing broken: {tool_results} tool_result vs "
            f"{tool_uses} tool_use"
        )
    return out


def _assistant_text(msg: SessionMessage) -> str | None:
    if isinstance(msg.content, str):
        return msg.content or None
    parts = [b.text for b in msg.content if b.type == "text" and b.text]
    return "\n".join(parts) if parts else None


async def _propagate_abort(src: asyncio.Event, dst: asyncio.Event) -> None:
    """父 abort → 级联子 abort(单向传播,spec §6.1;前台路径同样适用)。"""
    await src.wait()
    dst.set()


def _consume_exception(task: asyncio.Task) -> None:
    """后台任务 done 回调:消费未检索异常,防 "never retrieved" 警告。run() 已
    记 step_failed 终态,此处只需排掉 Task 层的异常引用。"""
    if not task.cancelled():
        task.exception()


@dataclass(slots=True)
class SubagentRequest:
    prompt: str  # 必填,自包含(CC §8.2 三理由)
    name: str | None = None  # 定义名;None → forkContext(S3,继承父上下文)
    model: str | None = None  # 工具参数覆盖(§8 链:参数 > 定义 > 父)
    max_turns: int | None = None
    run_in_background: bool = False
    permission_mode: str | None = None  # 只收窄(S4)
    cwd: Path | None = None  # 默认父 cwd;与 isolation 互斥(S7)
    isolation: Literal["worktree"] | None = None  # S7
    address_name: str | None = None  # SendMessage 寻址名(S5)


class SubagentRunner:
    """Spawn one subagent from a parent loop; foreground run (S2)."""

    def __init__(
        self,
        parent: AgentLoop,
        req: SubagentRequest,
        registry: "AgentRegistry",
        session_root: Path | None = None,
    ) -> None:
        self.parent = parent
        self.req = req
        self.registry = registry
        #: 会话 id 生成于构造期:launch() 立即返回 async_launched 需要它,_assemble
        #: 复用(会话文件/Subagent* 事件/Mailbox 寻址的同一 id)。
        self._agent_id = _new_agent_id()
        #: 子会话根;默认 {config_dir}/sessions/subagents(S5 前 list_sessions
        #: 排除面),测试注入 tmp_path 避免污染真实配置目录。
        self._root = session_root or paths.config_dir() / "sessions" / "subagents"
        self._session_path: Path | None = None  # 后台完成通知的 payload(S5)
        self._mailbox: "Mailbox | None" = None  # 终态注销 inbox 用(S5)
        self._worktree: Path | None = None  # S7:isolation=worktree 时创建的 worktree
        self._worktree_branch: str = ""  # S7:worktree 分支名(保留场景回填 metadata)
        self._base_cwd: Path | None = None  # S7:创建 worktree 的父目录(清理锚点)

    def _build_history(self) -> list[SessionMessage]:
        """spec §5.2 fork 历史:定义名子代理 = 空历史;fork(name=None)= 父
        历史三件套。主路径从父 loop._active_messages(12 线性投影)构造;
        文件定位(session_id + lane 经 load_lane)为后备 —— 跨进程/重开
        场景(§5.3,resume 工具入口留 19)。"""
        if self.req.name is not None:
            return []
        parent = self.parent
        messages = parent._active_messages
        if messages is None:
            lane = getattr(parent.session, "_lane", None) or "main"
            messages = parent.session.load_lane(lane)
        return build_fork_history(messages)

    def _resolve_definition(self) -> "AgentDefinition | None":
        if self.req.name is None:
            return None  # forkContext(S3):无定义,继承父上下文
        try:
            return self.registry.get(self.req.name)
        except KeyError as exc:
            # 模型幻觉/注入命名不存在的 agent:转 ToolError(工具队列只捕
            # ToolError → 错误 tool_result 交父自愈),绝不炸掉父 run
            raise ToolError(str(exc)) from None

    def _assemble(self) -> AgentLoop:
        parent, req = self.parent, self.req
        definition = self._resolve_definition()
        agent_id = self._agent_id
        tools = ToolRegistry(
            assemble_subagent_tools(parent.tools.all(), definition, req.run_in_background)
        )
        max_turns = req.max_turns or (definition.max_turns if definition else None) or 50
        model = req.model or (definition.model if definition else None) or parent.model
        cwd = (req.cwd or parent.cwd).resolve()
        # §5.4 worktree 隔离(S7):effectiveIsolation = 工具参数 > 定义。
        # 与 cwd 参数互斥(重定向语义重叠,双指歧义 → 明确报错);创建失败
        # → ToolError 交父自愈,绝不降级到父工作区执行(隔离承诺不可静默放弃)。
        isolation = req.isolation or (definition.isolation if definition else None)
        if isolation == "worktree":
            if req.cwd is not None:
                raise ToolError("cwd is mutually exclusive with isolation=worktree")
            self._base_cwd = cwd
            try:
                self._worktree = create_worktree(cwd, agent_id)
            except WorktreeError as exc:
                raise ToolError(str(exc)) from None
            self._worktree_branch = worktree_branch(agent_id)
            cwd = self._worktree
        body = (definition.body or definition.description) if definition else ""
        system = build_subagent_system_prompt(
            parent.system_prompt, req.name or agent_id, body, parent.task_list_id, cwd
        )
        # 注:Agent 工具输入暂不读 permission_mode 参数(§8 参数链只参数化 model/
        # max_turns),request 级仅编程入口可达;当前实际生效面 = 定义级声明。
        declared = req.permission_mode or (definition.permission_mode if definition else None)
        session = Session(agent_id, self._root)
        loop = AgentLoop(
            AgentLoopConfig(
                client=parent.client,
                tools=tools,
                permissions=parent.permissions,
                # §7.2 ask 自动 deny;§7.3 fork bubble:fork(name=None)继承父
                # 回调 —— 权限请求冒泡到父终端,普通子代理保持 None
                request_permission=parent.request_permission if req.name is None else None,
                system_prompt=system,
                compaction=parent.compaction,  # R2:子代理长任务同样需要压缩
                model=model,
                max_turns=max_turns,
                max_budget_usd=parent.max_budget_usd,
                cwd=cwd,
                session=session,
                settings=parent.settings,
                session_permissions=parent.session_permissions,
                history=self._build_history(),  # fork 时注入父历史三件套(§5.2)
                hooks=parent.hooks,  # 透传:子代理内部事件照常(R12 翻倍已知)
            ),
            # §7:生效模式 = min(父模式, 声明模式),只收窄不放宽
            mode=_min_mode(normalize_mode(parent.mode).value, declared),
        )
        # 13 §11.1:任务列表继承 —— 子代理与父共享同一列表(引擎按此注入
        # ToolUseContext.task_list_id,agent_name 自动 owner 分配来源)。
        # owner 身份:定义名子代理用定义名;fork 用唯一 agent_id —— 防并发
        # fork 身份碰撞(claim busy check 把两个 fork 当成同一 agent;事件侧
        # agent_name 仍按 spec 显示 "forkContext",此处只管 owner 身份)。
        loop.task_list_id = parent.task_list_id
        loop._agent_name = req.name or agent_id
        # 13 §11.2(单例 last-writer-wins):子代理 loop 构造时若覆盖了父对
        # 存储单例 on_change 的接线(子代理必然后构造),恢复父的 —— 否则
        # 父会话自己的 Task* 事件将以子代理的 session_id 派发。
        # 注:bound method 的 is 比较恒 False,须经 __self__ 判属主。
        current = get_task_store().on_change
        if current is not None and current.__self__ is loop:
            get_task_store().on_change = parent._dispatch_task_event
        # §6.3:队友消息 inbox —— 注册进 Mailbox(agent_id + address_name 双寻址
        # 名),目标 loop 每轮迭代前 drain 注入 Message 流(引擎 _inbox 字段)。
        inbox = asyncio.Queue()
        loop._inbox = inbox
        self._mailbox = get_mailbox()
        self._mailbox.register(agent_id, inbox)
        if req.address_name:
            self._mailbox.register(req.address_name, inbox)
        return loop

    async def run(self) -> ToolResult:
        """嵌套 run 到终态,回收本轮最后 assistant text(§5.4);前后台共用。

        操作日志(§11.3):run 开始记 step_attempt,终态记 step_completed/
        step_failed —— 写在父会话文件(审计视角:父发起了子代理步骤),与
        find_open_operations 的 kind 感知配对。子代理的任何崩溃都降级为
        错误 tool_result 交父自愈,绝不向上传播炸掉父 run。后台终态额外
        Mailbox 通知(§6.2,前台父阻塞消费结果不通知)。
        """
        parent, req = self.parent, self.req
        name = req.name or "forkContext"
        summary = f"{name}:{req.prompt[:100]}"
        if parent.session is not None:  # 与引擎 _record_tool_start 同款守卫
            parent.session.append_operation("step_attempt", tool="Agent", args_summary=summary)
        result: ToolResult | None = None
        try:
            result = await self._run_once()
        except asyncio.CancelledError:
            # 父 abort 级联取消:转录已 fsync,只需记失败终态(R10 影响低)。
            if parent.session is not None:
                parent.session.append_operation("step_failed", tool="Agent", args_summary=summary)
            # §6.4:取消同样自动注入父上下文(父模型需要知道子代理消失原因)
            await self._notify_done("cancelled", f"{name}: 已取消")
            raise
        except Exception:  # noqa: BLE001 - 装配期失败(未知名 agent 等)同样记终态
            if parent.session is not None:
                parent.session.append_operation("step_failed", tool="Agent", args_summary=summary)
            await self._notify_done("failed", "[子代理装配失败]")
            raise  # 原样上抛:引擎转错误 tool_result 交父自愈(S2 行为不变)
        finally:
            # §5.4 终态单点清理:成功/失败/取消三路径一致 —— worktree 无变更
            # 自动删除;有变更保留并回填 metadata。异常路径 result 为 None:
            # 照常清理(失败/取消不留半成品 worktree 垃圾),metadata 无从回填。
            self._cleanup_worktree(result)
        status = "failed" if result.is_error else "completed"
        if parent.session is not None:
            parent.session.append_operation(
                "step_failed" if result.is_error else "step_completed",
                tool="Agent", args_summary=summary,
            )
        await self._notify_done(status, str(result.content), content=str(result.content))
        return result

    def launch(self) -> ToolResult:
        """后台(§6.1):create_task 注册进父 _subagent_tasks,立即返回
        async_launched;父 abort → 级联 cancel(R10,转录已 fsync 保留部分成果)。
        """
        task = asyncio.create_task(self.run())
        self.parent._subagent_tasks.add(task)
        task.add_done_callback(self.parent._subagent_tasks.discard)
        task.add_done_callback(_consume_exception)

        async def _cancel_on_abort() -> None:
            await self.parent.abort.wait()
            task.cancel()

        watcher = asyncio.create_task(_cancel_on_abort())
        self._abort_watcher = watcher  # 泄漏回归断言锚点(完成即 done)
        # 子代理先完成 → watcher 一并取消,防常驻 pending 任务泄漏(R10)。
        task.add_done_callback(lambda _t: watcher.cancel())
        return ToolResult(
            content=json.dumps({"agent_id": self._agent_id, "status": "async_launched"}),
            metadata={"subagent_output": True},
        )

    def _cleanup_worktree(self, result: ToolResult | None) -> None:
        """§5.4 收尾:worktree 内无未提交变更 → git worktree remove 自动清理
        (连带删分支);有变更 → 保留。保留的路径必须送达父模型 —— 引擎
        tool_result 块只带 content(metadata 不进父消息流),因此追加进 content
        兑现「供宿主导入」契约(M1);成功/异常/取消三路径一致:取消/失败
        (result=None)时路径记入父会话操作日志,不留无声孤儿(L1)。"""
        if self._worktree is None or self._base_cwd is None:
            return
        if cleanup_worktree(self._base_cwd, self._agent_id):
            return
        tail = (f"\n\n[worktree 已保留:{self._worktree}"
                f"(分支 {self._worktree_branch});改动待合并回主工作区]")
        if result is not None:
            result.content += tail
            result.metadata.update({
                "worktree_path": str(self._worktree),
                "worktree_branch": self._worktree_branch,
            })
        elif self.parent.session is not None:
            self.parent.session.append_operation(
                "step_failed", tool="Agent",
                args_summary=(f"{self.req.name or 'forkContext'}: 取消/失败,"
                              f"保留 worktree {self._worktree}"),
            )

    async def _notify_done(self, status: str, summary: str, content: str | None = None) -> None:
        """后台完成通知(§6.2/§6.4)三通道:Mailbox 广播(订阅者自取)+ 父引擎
        _notify 通道(状态栏/UI 消费)+ 父上下文自动注入(CC task-notification
        同款:user 角色消息,父模型下一轮自然看到,长时间自动化无需用户转
        述)。摘要截断 200,完整结果进 <result> 段 + session_path 供 Read。"""
        if not self.req.run_in_background:
            return
        get_mailbox().notify(MailMessage(
            kind=SUBAGENT_DONE,
            agent_id=self._agent_id,
            payload={
                "status": status,
                "summary": summary[:200],
                "session_path": str(self._session_path or ""),
            },
        ))
        # §6.4:父上下文自动注入 —— 失败/取消同样通知(父模型需要知道子代理
        # 为什么消失);fail-open:父 loop 通道缺失/异常仅日志,不拖慢收尾。
        try:
            self.parent._notifications.put_nowait(
                f"<task-notification>\n"
                f"<agent_id>{self._agent_id}</agent_id>\n"
                f"<status>{status}</status>\n"
                f"<summary>{summary[:200]}</summary>"
                + (f"\n<result>{content}</result>" if content else "")
                + f"\n<session_path>{self._session_path or ''}</session_path>\n"
                f"</task-notification>"
            )
        except Exception:  # noqa: BLE001 - fail-open,§2.5 通知同款语义
            logger.exception("parent notification injection failed (fail-open)")
        await self.parent._notify(  # 阶段 09 §2.5:通知 UI 消费路径
            "subagent_done", f"后台子代理 {self.req.name or 'forkContext'} {status}",
            status=status, agent_id=self._agent_id,
        )

    async def _run_once(self) -> ToolResult:
        if self.parent.abort.is_set():
            return ToolResult(
                "[子代理未启动:父会话已中止]", is_error=True,
                metadata={"subagent_output": True},
            )
        loop = self._assemble()
        self._session_path = loop.session.path
        watcher = asyncio.create_task(_propagate_abort(self.parent.abort, loop.abort))
        last_text: str | None = None
        try:
            await self._emit_event("SubagentStart", loop)
            async for msg in loop.run(self.req.prompt):
                if msg.role == "assistant" and not (msg.is_meta or msg.is_error):
                    text = _assistant_text(msg)
                    if text:
                        last_text = text
        except Exception as exc:  # noqa: BLE001 - 子代理崩溃不炸父 run
            await self._emit_event("SubagentStop", loop, status="failed",
                                   summary=str(exc)[:200])
            return ToolResult(
                f"[子代理异常:{type(exc).__name__}]", is_error=True,
                metadata={"agent_id": loop.session.session_id, "subagent_output": True},
            )
        finally:
            watcher.cancel()
            self._unregister_inbox()
        reason = loop.last_stop_reason or "completed"
        content = last_text or f"[子代理无文本输出:{reason}]"
        await self._emit_event("SubagentStop", loop, status="failed" if reason in (
            "error", "max_turns", "interrupted") else "completed", summary=content)
        return ToolResult(
            content=content,
            is_error=reason in ("error", "max_turns", "interrupted"),
            metadata={"agent_id": loop.session.session_id, "subagent_output": True},
        )

    async def _emit_event(self, event: str, loop: AgentLoop, **extra: str) -> None:
        """Subagent* 事件单点触发(§11.2):agent_name = 定义名或 forkContext;
        SubagentStop 的 additionalContext 累积进父 loop 一次性 _hook_reminder
        (§6.2 消费路径 2)。无钩子订阅 → 零路径,不进管线。"""
        hooks = self.parent.hooks
        if hooks is None or not hooks.has_hooks_for_event(event):
            return
        result = await hooks.dispatch(event, input=HookInput(
            session_id=loop.session.session_id,
            cwd=str(loop.cwd),
            session_path=str(loop.session.path),
            extra={"agent_name": self.req.name or "forkContext",
                   "agent_id": self._agent_id, **extra},
        ), abort_event=loop.abort)  # §6.3:钩子批次 abort 感知(与引擎各派发点一致)
        if result.additional_context:
            self.parent._accumulate_hook_reminder(result.additional_context)

    def _unregister_inbox(self) -> None:
        """终态注销(§6.3):目标消失后 SendMessage 投递 → 明确报错(R16)。"""
        if self._mailbox is None:
            return
        self._mailbox.unregister(self._agent_id)
        if self.req.address_name:
            self._mailbox.unregister(self.req.address_name)
