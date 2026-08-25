"""agent 会话事件词表扩展(参考实现 agent/types.ts 实现)。

agent 拥有的两条有序待办消息列表(next-turn / next-step)的每次
归一化变更都以 ``agent/inbox/spliced`` 事件写入会话日志 —— 它
是 agent 层对 session 事件词表的唯一贡献,经 session 包的
``extend_event_types`` 注册(TS 用声明合并,Python 用注册表,
见 core/session 的 known_event_types)。

**投递序不变量**:活体派发(agent/inbox/inserted 等通知)先于
投影变更发生,所以同步监听者能读到 splice 前的列表,恢复被移除
的消息 —— Inbox 的实现依赖这一点。
"""

from __future__ import annotations

from typing import Literal

from core.session import extend_event_types

__all__ = ["INBOX_TARGETS", "InboxTarget"]

#: 两条待办列表:next-turn 等待个体回合,next-step 等待下一个步骤边界
INBOX_TARGETS = ("next-turn", "next-step")

#: agent 拥有的两条有序待办消息列表之一
InboxTarget = Literal["next-turn", "next-step"]

#: 把 agent/inbox/spliced 登记进 session 事件词表(声明合并的落点)。
#: 模块导入即注册,幂等。
extend_event_types("agent/inbox/spliced")
