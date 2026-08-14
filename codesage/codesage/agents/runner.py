"""Subagent execution (phase 13 S2/S5): tool-pool assembly, foreground nested
run, background launch (S5), forkContext (S3), worktree isolation (S7).

S2 delivers the foreground path: `assemble_subagent_tools` (compiled-time
recursion ban), `build_subagent_system_prompt` (spec §9) and
`SubagentRunner.run()` — a nested AgentLoop run whose final assistant text
becomes the tool_result, with the parent abort cascading down.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from ..config import paths
from ..core import Session
from ..core.messages import SessionMessage
from ..engine import AgentLoop, AgentLoopConfig
from ..permissions import normalize_mode
from ..tools import Tool, ToolError, ToolRegistry, ToolResult

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
ASYNC_AGENT_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {"Read", "Grep", "Glob", "LS", "Bash", "Edit", "Write",
     "TaskCreate", "TaskGet", "TaskList", "TaskUpdate"}
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
    task_list_id 传**子代理自己的**会话 id:引擎按会话注入 ToolUseContext,
    父列表共享要等 S6 的继承机制(届时恢复「同一任务列表」措辞)。
    """
    prompt = f"{base}\n\n# Agent: {name}\n\n{body}" if body else f"{base}\n\n# Agent: {name}"
    prompt += (
        f"\n\n任务与协作:你的任务列表 id={task_list_id}。TaskCreate/TaskGet/"
        "TaskList/TaskUpdate 操作该列表(子代理独立列表,与父会话分离)。"
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
        #: 子会话根;默认 {config_dir}/sessions/subagents(S5 前 list_sessions
        #: 排除面),测试注入 tmp_path 避免污染真实配置目录。
        self._root = session_root or paths.config_dir() / "sessions" / "subagents"

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
        agent_id = _new_agent_id()
        tools = ToolRegistry(
            assemble_subagent_tools(parent.tools.all(), definition, req.run_in_background)
        )
        max_turns = req.max_turns or (definition.max_turns if definition else None) or 50
        model = req.model or (definition.model if definition else None) or parent.model
        cwd = (req.cwd or parent.cwd).resolve()
        body = (definition.body or definition.description) if definition else ""
        system = build_subagent_system_prompt(
            parent.system_prompt, req.name or agent_id, body, agent_id, cwd
        )
        # 注:Agent 工具输入暂不读 permission_mode 参数(§8 参数链只参数化 model/
        # max_turns),request 级仅编程入口可达;当前实际生效面 = 定义级声明。
        declared = req.permission_mode or (definition.permission_mode if definition else None)
        session = Session(agent_id, self._root)
        return AgentLoop(
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

    async def run(self) -> ToolResult:
        """前台:嵌套 run 到终态,回收本轮最后 assistant text(§5.4)。

        操作日志(§11.3):run 开始记 step_attempt,终态记 step_completed/
        step_failed —— 写在父会话文件(审计视角:父发起了子代理步骤),与
        find_open_operations 的 kind 感知配对。子代理的任何崩溃都降级为
        错误 tool_result 交父自愈,绝不向上传播炸掉父 run。
        """
        parent, req = self.parent, self.req
        name = req.name or "forkContext"
        summary = f"{name}:{req.prompt[:100]}"
        if parent.session is not None:  # 与引擎 _record_tool_start 同款守卫
            parent.session.append_operation("step_attempt", tool="Agent", args_summary=summary)
        try:
            result = await self._run_once()
        except Exception:  # noqa: BLE001 - 装配期失败(未知名 agent 等)同样记终态
            if parent.session is not None:
                parent.session.append_operation("step_failed", tool="Agent", args_summary=summary)
            raise  # 原样上抛:引擎转错误 tool_result 交父自愈(S2 行为不变)
        if parent.session is not None:
            parent.session.append_operation(
                "step_failed" if result.is_error else "step_completed",
                tool="Agent", args_summary=summary,
            )
        return result

    async def _run_once(self) -> ToolResult:
        if self.parent.abort.is_set():
            return ToolResult(
                "[子代理未启动:父会话已中止]", is_error=True,
                metadata={"subagent_output": True},
            )
        loop = self._assemble()
        watcher = asyncio.create_task(_propagate_abort(self.parent.abort, loop.abort))
        last_text: str | None = None
        try:
            async for msg in loop.run(self.req.prompt):
                if msg.role == "assistant" and not (msg.is_meta or msg.is_error):
                    text = _assistant_text(msg)
                    if text:
                        last_text = text
        except Exception as exc:  # noqa: BLE001 - 子代理崩溃不炸父 run
            return ToolResult(
                f"[子代理异常:{type(exc).__name__}]", is_error=True,
                metadata={"agent_id": loop.session.session_id, "subagent_output": True},
            )
        finally:
            watcher.cancel()
        reason = loop.last_stop_reason or "completed"
        content = last_text or f"[子代理无文本输出:{reason}]"
        return ToolResult(
            content=content,
            is_error=reason in ("error", "max_turns", "interrupted"),
            metadata={"agent_id": loop.session.session_id, "subagent_output": True},
        )
