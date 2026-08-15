"""Mailbox (phase 13 §6.2/§6.3): 进程内消息总线 + 子代理寻址注册表。

后台子代理完成 → ``notify(MailMessage(kind="subagent_done"))``;SendMessage
按 address_name/agent_id 寻址 → 目标 inbox(asyncio.Queue,随 runner 生命周期)
—— 目标 loop 每轮迭代前 drain 注入 Message 流(引擎既有入口,零新通道)。

进程内单例(get_mailbox),模式对齐 get_task_store;测试用 reset_mailbox 隔离。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger("codesage.tasks.mailbox")

#: MailMessage.kind 取值:后台子代理完成通知(§6.2;"task_notify" 为 11 §12 预留)
SUBAGENT_DONE = "subagent_done"


@dataclass(slots=True)
class MailMessage:
    """一条总线通知:kind + 目标 agent_id + 载荷。"""

    kind: str
    agent_id: str
    payload: dict = field(default_factory=dict)  # {status, summary, session_path}


class Mailbox:
    """通知总线(notify/subscribe)+ 寻址注册表(register/send)二合一。"""

    def __init__(self) -> None:
        self._inboxes: dict[str, asyncio.Queue[str]] = {}
        self._subs: dict[str, list[Callable[[MailMessage], None]]] = {}

    # ---- 寻址(§6.3)----

    def register(self, name: str, inbox: asyncio.Queue[str]) -> None:
        """注册 名 → inbox(agent_id 恒注册;address_name 存在则一并注册)。

        同名重复注册 = 覆盖(幂等:address_name 撞名时后者胜,不报错)。
        """
        self._inboxes[name] = inbox

    def unregister(self, name: str) -> None:
        """注销(子代理终态)。目标消失后投递 → 明确报错(R16)。

        §6.3 投递确认不含送达保证:竞态窗口内(in-flight)已入队消息随注销
        丢弃 —— 显式 drain 计数记 warning,发送方不至于完全无声(R17 透明)。
        """
        inbox = self._inboxes.pop(name, None)
        if inbox is None:
            return
        dropped = 0
        while True:
            try:
                inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped += 1
        if dropped:
            logger.warning("mailbox unregister %s: %d in-flight message(s) dropped", name, dropped)

    def send(self, to: str, message: str) -> tuple[bool, str]:
        """投递一条消息到目标 inbox;目标不存在/已终止 → (False, 错误)(幂等)。"""
        inbox = self._inboxes.get(to)
        if inbox is None:
            return False, f"no such subagent inbox: {to}"
        inbox.put_nowait(message)
        return True, "delivered"

    # ---- 通知(§6.2)----

    def notify(self, msg: MailMessage) -> None:
        """广播一条通知给所有订阅方(订阅方抛异常仅吞日志,不炸调用方)。"""
        for handler in list(self._subs.get(msg.kind, [])):
            try:
                handler(msg)
            except Exception:  # noqa: BLE001 - 订阅方故障不影响通知方
                logger.exception("mailbox handler failed for kind %s", msg.kind)

    def subscribe(self, kind: str, handler: Callable[[MailMessage], None]) -> Callable[[], None]:
        """订阅某种通知,返回取消函数。"""
        self._subs.setdefault(kind, []).append(handler)
        box = self

        def _cancel() -> None:
            handlers = box._subs.get(kind, [])
            if handler in handlers:
                handlers.remove(handler)

        return _cancel


_mailbox: Mailbox | None = None


def get_mailbox() -> Mailbox:
    """进程内单例(工具装配点之外直取用;测试直接构造或 reset_mailbox)。"""
    global _mailbox
    if _mailbox is None:
        _mailbox = Mailbox()
    return _mailbox


def reset_mailbox() -> None:
    """清空单例(测试隔离;生产不调用)。"""
    global _mailbox
    _mailbox = None
