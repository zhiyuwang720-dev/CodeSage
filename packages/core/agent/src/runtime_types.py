"""公开 agent 类型与活体运行时事件契约(参考实现 agent/runtime-types.ts 实现)。

耐久性转录事实与回合/步骤边界仍是 core/session 的事件;本模块只
声明 agent 层自己的活体运行时面:Agent 协议(注册表与循环交互
的形状)、创建选项、以及全部 agent 主题事件的载荷契约 —— 事件
名常量与载荷校验集中在这里,派发侧(dispatch.py)按名派发。

**Agent 协议**是形状契约(Python 的 Protocol):真实实现
(ReactLoopAgent,agent-loop 包)构造后经注册表发布;消费者按
协议依赖,不绑具体类。
"""

from __future__ import annotations

from typing import Any, Callable, Literal, Protocol, runtime_checkable

__all__ = [
    "AGENT_EVENTS",
    "AGENT_SUBJECT_EVENTS",
    "AGENT_WATERFALL_EVENTS",
    "Agent",
    "AgentOptions",
    "AgentStatus",
    "CancelOptions",
    "PreStepDecision",
    "RequestErrorAction",
    "SessionStartSource",
]

#: 会话生命周期开始的来源:种子创建是 startup,持久化载入是 resume
SessionStartSource = Literal["startup", "resume", "clear", "compact"]

#: 生命周期状态:idle = 无驱动者在跑;running = 驱动者排水中
AgentStatus = Literal["idle", "running"]

#: 是否以及带哪些消息进入提议的步骤
PreStepDecision = dict  # {'kind': 'reject'} 或 {'kind': 'enter', 'messages': [...]}

#: 拥有模型请求恢复的监听者返回的动作
RequestErrorAction = Literal["retry"] | None  # noqa: UP045 -- 与 参考实现 形状一致

#: 合并可扩展的创建选项;persona 属于 system-prompt 分区
AgentOptions = dict  # {'provider': str?, 'model': str?, 'maxTokens': int?}

#: Agent.cancel 选项:keepInbox 保留排队与转向输入而非丢弃
CancelOptions = dict  # {'keepInbox': bool?}

#: 全部 agent 主题事件(载荷恒携带 agent 字段)
AGENT_SUBJECT_EVENTS = (
    # ---- 生命周期(emit)----
    "agent/created",          # 发布:已配置的 agent + 活会话
    "agent/disposed",         # 离开注册表:驱动者静默后、会话脱离前
    "agent/status",           # 状态翻转(idle ⇄ running)
    "agent/inbox/inserted",   # 一条消息进入活收件箱
    "agent/inbox/claimed",    # 一条消息在它所属的回合内被认领
    "agent/inbox/discarded",  # 一条消息被丢弃
    # ---- 会话生命周期(emit)----
    "agent/session-start",    # 会话生命周期开始,首回合之前恰好一次
    # ---- 机器的扩展点 ----
    "agent/pre-step",         # (waterfall)否决或替换进入步骤的消息
    "agent/request",          # (waterfall)替换冻结的调用配置
    "agent/request-error",    # (waterfall)一次失败尝试的恢复动作
    "agent/turn-stopping",    # (serial)回合即将关闭,被等待
    # ---- 错误通知(emit)----
    "agent/error",            # 步骤或回合出错
)

#: waterfall 事件:监听者可调 next() 委托/包装
AGENT_WATERFALL_EVENTS = ("agent/pre-step", "agent/request", "agent/request-error")


@runtime_checkable
class Agent(Protocol):
    """公开活体 agent 句柄(ReactLoopAgent 实现的形状契约)。"""

    #: 与 session 共享的唯一身份
    id: str
    #: 该 agent 的请求所用的提供者路由与模型
    options: AgentOptions
    #: 该 agent 驱动的活会话;其日志是耐久事实源
    session: Any
    #: 耐久待办工作的 agent 属主投影
    inbox: Any
    #: 当前生命周期状态,每次翻转经 agent/status 镜像
    status: AgentStatus
    #: agent 作用域上下文:贡献 agent 本地化,卸载即回卷
    ctx: Any

    def cancel(self, cause: str, options: CancelOptions | None = None) -> None: ...

    def when_idle(self) -> Any: ...

    def run_maintenance(self, task: Callable[[Any], Any]) -> Any: ...

    def send(self, message: dict, target: str, wakeup: bool) -> None: ...

    def followup(self, message: dict) -> None: ...

    def steer(self, message: dict) -> None: ...

    def inject(self, message: dict) -> None: ...


#: agent 主题事件全集(含非 subject 的收尾辅助事件)
AGENT_EVENTS = AGENT_SUBJECT_EVENTS
