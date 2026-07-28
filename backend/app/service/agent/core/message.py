"""
Agent 间通信机制 (A2A)

提供：
- Agent 消息格式定义（类型、优先级、信封模式）
- 消息队列管理（创建、发送、接收、确认）
- 事件总线（发布/订阅模式）
- 消息持久化和过期清理
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)


# ============================================================================
# 消息类型与优先级
# ============================================================================


class MessageType(str, Enum):
    """Agent 间消息类型"""

    COMMAND = "command"              # 指令：要求接收方执行某个操作
    QUERY = "query"                  # 查询：向接收方请求信息
    RESPONSE = "response"            # 响应：对查询/指令的回复
    NOTIFICATION = "notification"    # 通知：单向信息通告，无需回复
    HANDOFF = "handoff"              # 任务交接：将任务上下文传递给另一个 Agent
    BROADCAST = "broadcast"          # 广播：发送给所有 Agent 的消息
    ERROR = "error"                  # 错误：发生错误时的通知
    DATA = "data"                    # 数据：结构化数据传输
    HEARTBEAT = "heartbeat"          # 心跳：健康检查和存活确认


class MessagePriority(str, Enum):
    """消息优先级"""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class MessageStatus(str, Enum):
    """消息状态"""

    PENDING = "pending"         # 等待投递
    DELIVERED = "delivered"     # 已投递到队列
    READ = "read"               # 已被接收方读取
    ACKNOWLEDGED = "acknowledged"  # 已被接收方确认处理
    EXPIRED = "expired"         # 已过期
    FAILED = "failed"           # 投递失败


# ============================================================================
# 消息数据结构
# ============================================================================


@dataclass
class AgentMessage:
    """
    Agent 间标准消息信封

    设计原则：
    - 不可变核心字段 + 可变状态字段
    - 支持消息关联（correlation_id, reply_to）
    - 内置 TTL 机制防止消息堆积
    - 可扩展的 metadata 字段

    使用示例::

        msg = AgentMessage(
            msg_type=MessageType.COMMAND,
            sender_id="agent_orchestrator",
            receiver_id="agent_auditor",
            subject="开始安全审计",
            content={"target": "backend/api", "rules": ["sql_injection", "xss"]},
            priority=MessagePriority.HIGH,
        )
        message_bus.send_message(msg)
    """

    # ---- 核心标识 ----
    id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:12]}")
    msg_type: MessageType = MessageType.NOTIFICATION

    # ---- 路由信息 ----
    sender_id: str = ""               # 发送方 Agent ID
    receiver_id: str = ""             # 接收方 Agent ID（BROADCAST 时可为空）
    reply_to: Optional[str] = None    # 期望回复的目标队列 ID

    # ---- 内容 ----
    subject: str = ""                 # 消息主题（简短摘要）
    content: Any = None               # 消息正文（字符串、字典、列表等）
    content_type: str = "application/json"  # 内容 MIME 类型

    # ---- 关联与排序 ----
    correlation_id: Optional[str] = None   # 关联 ID（用于请求-响应配对）
    priority: MessagePriority = MessagePriority.NORMAL

    # ---- 时间与生命周期 ----
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ttl_seconds: int = 300            # 消息存活时间（0 表示永不过期）
    expires_at: Optional[datetime] = None

    # ---- 状态 ----
    status: MessageStatus = MessageStatus.PENDING

    # ---- 重试 ----
    max_retries: int = 3
    retry_count: int = 0

    # ---- 扩展 ----
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """计算过期时间"""
        if self.ttl_seconds > 0 and self.expires_at is None:
            self.expires_at = datetime.fromtimestamp(
                self.timestamp.timestamp() + self.ttl_seconds,
                tz=timezone.utc,
            )

    @property
    def is_expired(self) -> bool:
        """检查消息是否已过期"""
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) > self.expires_at

    @property
    def is_valid(self) -> bool:
        """检查消息是否有效（未过期且状态允许投递）"""
        if self.is_expired:
            return False
        if self.status in (MessageStatus.EXPIRED, MessageStatus.FAILED):
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "id": self.id,
            "msg_type": self.msg_type.value,
            "sender_id": self.sender_id,
            "receiver_id": self.receiver_id,
            "reply_to": self.reply_to,
            "subject": self.subject,
            "content": self.content,
            "content_type": self.content_type,
            "correlation_id": self.correlation_id,
            "priority": self.priority.value,
            "timestamp": self.timestamp.isoformat(),
            "ttl_seconds": self.ttl_seconds,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "status": self.status.value,
            "max_retries": self.max_retries,
            "retry_count": self.retry_count,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentMessage":
        """从字典反序列化"""
        return cls(
            id=data.get("id", f"msg_{uuid.uuid4().hex[:12]}"),
            msg_type=MessageType(data.get("msg_type", "notification")),
            sender_id=data.get("sender_id", ""),
            receiver_id=data.get("receiver_id", ""),
            reply_to=data.get("reply_to"),
            subject=data.get("subject", ""),
            content=data.get("content"),
            content_type=data.get("content_type", "application/json"),
            correlation_id=data.get("correlation_id"),
            priority=MessagePriority(data.get("priority", "normal")),
            timestamp=datetime.fromisoformat(data["timestamp"]) if data.get("timestamp") else datetime.now(timezone.utc),
            ttl_seconds=data.get("ttl_seconds", 300),
            status=MessageStatus(data.get("status", "pending")),
            max_retries=data.get("max_retries", 3),
            retry_count=data.get("retry_count", 0),
            metadata=data.get("metadata", {}),
        )

    # ---- 便捷工厂方法 ----

    @classmethod
    def command(
        cls,
        sender_id: str,
        receiver_id: str,
        subject: str,
        content: Any,
        **kwargs,
    ) -> "AgentMessage":
        """创建指令消息"""
        return cls(
            msg_type=MessageType.COMMAND,
            sender_id=sender_id,
            receiver_id=receiver_id,
            subject=subject,
            content=content,
            **kwargs,
        )

    @classmethod
    def query(
        cls,
        sender_id: str,
        receiver_id: str,
        subject: str,
        content: Any,
        **kwargs,
    ) -> "AgentMessage":
        """创建查询消息"""
        return cls(
            msg_type=MessageType.QUERY,
            sender_id=sender_id,
            receiver_id=receiver_id,
            subject=subject,
            content=content,
            **kwargs,
        )

    @classmethod
    def response(
        cls,
        sender_id: str,
        receiver_id: str,
        correlation_id: str,
        content: Any,
        **kwargs,
    ) -> "AgentMessage":
        """创建响应消息"""
        return cls(
            msg_type=MessageType.RESPONSE,
            sender_id=sender_id,
            receiver_id=receiver_id,
            correlation_id=correlation_id,
            subject=f"Re: {correlation_id}",
            content=content,
            **kwargs,
        )

    @classmethod
    def handoff(
        cls,
        sender_id: str,
        receiver_id: str,
        content: Any,
        **kwargs,
    ) -> "AgentMessage":
        """创建任务交接消息"""
        return cls(
            msg_type=MessageType.HANDOFF,
            sender_id=sender_id,
            receiver_id=receiver_id,
            subject=f"Handoff from {sender_id}",
            content=content,
            priority=MessagePriority.HIGH,
            **kwargs,
        )

    @classmethod
    def broadcast(
        cls,
        sender_id: str,
        subject: str,
        content: Any,
        **kwargs,
    ) -> "AgentMessage":
        """创建广播消息"""
        return cls(
            msg_type=MessageType.BROADCAST,
            sender_id=sender_id,
            receiver_id="*",
            subject=subject,
            content=content,
            **kwargs,
        )


# ============================================================================
# 消息队列
# ============================================================================


class MessageQueue:
    """
    单个 Agent 的消息队列

    特性：
    - 按优先级排序（URGENT > HIGH > NORMAL > LOW）
    - TTL 自动过期
    - 消息状态追踪
    - 线程安全
    """

    # 优先级排序权重
    _PRIORITY_ORDER: Dict[MessagePriority, int] = {
        MessagePriority.URGENT: 0,
        MessagePriority.HIGH: 1,
        MessagePriority.NORMAL: 2,
        MessagePriority.LOW: 3,
    }

    def __init__(self, agent_id: str, max_size: int = 1000):
        self.agent_id = agent_id
        self.max_size = max_size
        self._lock = threading.RLock()
        self._queue: List[AgentMessage] = []
        self._message_index: Dict[str, AgentMessage] = {}  # msg_id -> msg (快速查找)
        self._created_at = datetime.now(timezone.utc)
        self._total_enqueued: int = 0
        self._total_dequeued: int = 0
        self._total_expired: int = 0

    @property
    def size(self) -> int:
        """当前队列大小"""
        with self._lock:
            return len(self._queue)

    @property
    def is_empty(self) -> bool:
        """队列是否为空"""
        return self.size == 0

    @property
    def is_full(self) -> bool:
        """队列是否已满"""
        return self.size >= self.max_size

    def enqueue(self, message: AgentMessage) -> bool:
        """
        将消息入队

        Args:
            message: 要入队的消息

        Returns:
            True 表示入队成功，False 表示队列已满
        """
        with self._lock:
            # 清理过期消息
            self._purge_expired()

            if len(self._queue) >= self.max_size:
                logger.warning(
                    f"[MessageQueue] 队列已满 (agent={self.agent_id}, "
                    f"size={len(self._queue)}, max={self.max_size})"
                )
                return False

            # 更新消息状态
            message.status = MessageStatus.DELIVERED

            # 按优先级插入
            insert_idx = self._find_insert_position(message.priority)
            self._queue.insert(insert_idx, message)
            self._message_index[message.id] = message
            self._total_enqueued += 1

            logger.debug(
                f"[MessageQueue] 消息入队: {message.id} "
                f"type={message.msg_type.value} "
                f"from={message.sender_id} → to={self.agent_id}"
            )
            return True

    def dequeue(self, mark_read: bool = True) -> Optional[AgentMessage]:
        """
        从队列取出下一条消息（优先级最高、最先到达的）

        Args:
            mark_read: 是否标记为已读

        Returns:
            消息或 None（队列为空）
        """
        with self._lock:
            self._purge_expired()

            if not self._queue:
                return None

            message = self._queue.pop(0)
            if mark_read:
                message.status = MessageStatus.READ
            self._total_dequeued += 1
            return message

    def peek(self) -> Optional[AgentMessage]:
        """查看队首消息但不取出"""
        with self._lock:
            self._purge_expired()
            return self._queue[0] if self._queue else None

    def get_all(
        self,
        mark_read: bool = True,
        msg_type: Optional[MessageType] = None,
        limit: int = 0,
    ) -> List[AgentMessage]:
        """
        获取队列中所有消息（或按类型过滤）

        Args:
            mark_read: 是否标记为已读
            msg_type: 过滤消息类型，None 表示不过滤
            limit: 最大返回数量，0 表示不限制

        Returns:
            消息列表
        """
        with self._lock:
            self._purge_expired()

            if msg_type:
                filtered = [m for m in self._queue if m.msg_type == msg_type]
            else:
                filtered = list(self._queue)

            if mark_read:
                for m in filtered:
                    m.status = MessageStatus.READ

            if limit > 0:
                filtered = filtered[:limit]

            return filtered

    def acknowledge(self, message_id: str) -> bool:
        """
        确认消息已被处理

        Args:
            message_id: 消息 ID

        Returns:
            True 表示确认成功
        """
        with self._lock:
            if message_id in self._message_index:
                self._message_index[message_id].status = MessageStatus.ACKNOWLEDGED
                return True
            # 消息可能已被取出但仍在索引中
            for msg in self._queue:
                if msg.id == message_id:
                    msg.status = MessageStatus.ACKNOWLEDGED
                    return True
            return False

    def remove(self, message_id: str) -> bool:
        """
        从队列中移除指定消息

        Args:
            message_id: 消息 ID

        Returns:
            True 表示移除成功
        """
        with self._lock:
            self._message_index.pop(message_id, None)
            for i, msg in enumerate(self._queue):
                if msg.id == message_id:
                    self._queue.pop(i)
                    return True
            return False

    def clear(self) -> int:
        """
        清空队列

        Returns:
            清除的消息数量
        """
        with self._lock:
            count = len(self._queue)
            self._queue.clear()
            self._message_index.clear()
            return count

    def get_statistics(self) -> Dict[str, Any]:
        """获取队列统计信息"""
        with self._lock:
            return {
                "agent_id": self.agent_id,
                "current_size": len(self._queue),
                "max_size": self.max_size,
                "total_enqueued": self._total_enqueued,
                "total_dequeued": self._total_dequeued,
                "total_expired": self._total_expired,
                "created_at": self._created_at.isoformat(),
                "pending": sum(1 for m in self._queue if m.status == MessageStatus.PENDING),
                "delivered": sum(1 for m in self._queue if m.status == MessageStatus.DELIVERED),
                "urgent": sum(1 for m in self._queue if m.priority == MessagePriority.URGENT),
                "high": sum(1 for m in self._queue if m.priority == MessagePriority.HIGH),
            }

    def _find_insert_position(self, priority: MessagePriority) -> int:
        """
        按优先级找到插入位置（保持同优先级 FIFO）

        优先级高的排在前面：URGENT(0) < HIGH(1) < NORMAL(2) < LOW(3)
        """
        target_weight = self._PRIORITY_ORDER[priority]
        for i, msg in enumerate(self._queue):
            if self._PRIORITY_ORDER[msg.priority] > target_weight:
                return i
        return len(self._queue)

    def _purge_expired(self) -> int:
        """清理过期消息，返回清理数量"""
        now = datetime.now(timezone.utc)
        expired_ids = [
            msg.id for msg in self._queue
            if msg.expires_at is not None and now > msg.expires_at
        ]
        if expired_ids:
            expired_set = set(expired_ids)
            for msg_id in expired_ids:
                self._message_index.pop(msg_id, None)
            self._queue = [m for m in self._queue if m.id not in expired_set]
            for msg_id in expired_ids:
                # 如果消息还在索引中，更新状态
                if msg_id in self._message_index:
                    self._message_index[msg_id].status = MessageStatus.EXPIRED
            self._total_expired += len(expired_ids)
        return len(expired_ids)


# ============================================================================
# 事件总线
# ============================================================================


# 事件回调类型
EventCallback = Callable[[str, Any], None]
AsyncEventCallback = Callable[[str, Any], Any]  # 返回 awaitable


class EventBus:
    """
    事件总线（发布/订阅模式）

    提供 Agent 间松耦合的事件通信机制。Agent 可以：
    - 订阅特定事件类型
    - 发布事件（同步或异步）
    - 支持通配符订阅

    示例::

        # 订阅
        async def on_agent_completed(event_type, data):
            print(f"Agent {data['agent_id']} completed!")

        event_bus.subscribe("agent.completed", on_agent_completed)

        # 发布
        await event_bus.publish("agent.completed", {"agent_id": "agent_001"})
    """

    # 内置事件类型
    class Events:
        AGENT_CREATED = "agent.created"
        AGENT_STARTED = "agent.started"
        AGENT_COMPLETED = "agent.completed"
        AGENT_FAILED = "agent.failed"
        AGENT_STOPPED = "agent.stopped"
        AGENT_PAUSED = "agent.paused"
        AGENT_RESUMED = "agent.resumed"

        MESSAGE_RECEIVED = "message.received"
        MESSAGE_SENT = "message.sent"
        MESSAGE_EXPIRED = "message.expired"
        MESSAGE_ACKNOWLEDGED = "message.acknowledged"

        HANDOFF_RECEIVED = "handoff.received"
        HANDOFF_COMPLETED = "handoff.completed"

        TASK_ASSIGNED = "task.assigned"
        TASK_COMPLETED = "task.completed"
        TASK_FAILED = "task.failed"

        SYSTEM_ERROR = "system.error"
        SYSTEM_WARNING = "system.warning"

    def __init__(self):
        self._lock = threading.RLock()
        # event_type -> list of (callback, is_async, once)
        self._subscribers: Dict[str, List[Tuple[Any, bool, bool]]] = defaultdict(list)
        self._event_history: List[Dict[str, Any]] = []  # 事件历史（最近 N 条）
        self._max_history: int = 500
        self._total_published: int = 0
        self._total_delivered: int = 0

    def subscribe(
        self,
        event_type: str,
        callback: Union[EventCallback, AsyncEventCallback],
        *,
        once: bool = False,
    ) -> str:
        """
        订阅事件

        Args:
            event_type: 事件类型（支持 * 通配符，如 "agent.*" 匹配 "agent.created"）
            callback: 回调函数（同步或异步）
            once: True 表示触发一次后自动取消订阅

        Returns:
            订阅 ID（用于取消订阅）
        """
        import inspect

        is_async = inspect.iscoroutinefunction(callback)

        with self._lock:
            sub_id = f"sub_{uuid.uuid4().hex[:8]}"
            self._subscribers[event_type].append((callback, is_async, once))
            logger.debug(
                f"[EventBus] 订阅: {sub_id} → {event_type} "
                f"(async={is_async}, once={once})"
            )
            return sub_id

    def unsubscribe(
        self,
        event_type: str,
        callback: Union[EventCallback, AsyncEventCallback],
    ) -> bool:
        """
        取消订阅

        Args:
            event_type: 事件类型
            callback: 之前注册的回调函数

        Returns:
            True 表示取消成功
        """
        with self._lock:
            if event_type not in self._subscribers:
                return False

            before = len(self._subscribers[event_type])
            self._subscribers[event_type] = [
                (cb, is_async, once)
                for cb, is_async, once in self._subscribers[event_type]
                if cb is not callback
            ]
            removed = before - len(self._subscribers[event_type])

            if not self._subscribers[event_type]:
                del self._subscribers[event_type]

            return removed > 0

    def publish(self, event_type: str, data: Any = None) -> int:
        """
        同步发布事件

        所有订阅者（包括异步回调）都会被同步调用。
        对于异步回调，会在当前事件循环中创建任务执行。
        如需等待所有异步回调完成，请使用 `publish_async`。

        Args:
            event_type: 事件类型
            data: 事件数据

        Returns:
            收到事件的订阅者数量
        """
        with self._lock:
            matching = self._find_matching_subscribers(event_type)
            self._record_event(event_type, data)
            self._total_published += 1

        delivered = 0
        for callback, is_async, once in matching:
            try:
                if is_async:
                    # 尝试在现有事件循环中调度
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(callback(event_type, data))
                    except RuntimeError:
                        # 没有运行中的事件循环，同步执行
                        import asyncio as _asyncio
                        _asyncio.run(callback(event_type, data))
                else:
                    callback(event_type, data)
                delivered += 1
            except Exception:
                logger.exception(
                    f"[EventBus] 事件回调异常: event={event_type}, "
                    f"callback={callback.__name__}"
                )

        # 清理一次性订阅
        with self._lock:
            self._cleanup_once_subscribers(event_type)
            self._total_delivered += delivered

        return delivered

    async def publish_async(self, event_type: str, data: Any = None) -> int:
        """
        异步发布事件

        异步回调会被 await，同步回调在线程池中执行。

        Args:
            event_type: 事件类型
            data: 事件数据

        Returns:
            收到事件的订阅者数量
        """
        with self._lock:
            matching = self._find_matching_subscribers(event_type)
            self._record_event(event_type, data)
            self._total_published += 1

        delivered = 0
        for callback, is_async, once in matching:
            try:
                if is_async:
                    await callback(event_type, data)
                else:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, callback, event_type, data)
                delivered += 1
            except Exception:
                logger.exception(
                    f"[EventBus] 异步事件回调异常: event={event_type}"
                )

        with self._lock:
            self._cleanup_once_subscribers(event_type)
            self._total_delivered += delivered

        return delivered

    def get_subscriber_count(self, event_type: Optional[str] = None) -> int:
        """获取订阅者数量"""
        with self._lock:
            if event_type:
                matching = self._find_matching_subscribers(event_type)
                return len(matching)
            return sum(len(subs) for subs in self._subscribers.values())

    def get_event_history(self, event_type: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """获取事件历史"""
        with self._lock:
            if event_type:
                history = [e for e in self._event_history if e["type"] == event_type]
            else:
                history = list(self._event_history)
            return history[-limit:]

    def clear_history(self) -> int:
        """清空事件历史"""
        with self._lock:
            count = len(self._event_history)
            self._event_history.clear()
            return count

    def _find_matching_subscribers(
        self, event_type: str
    ) -> List[Tuple[Any, bool, bool]]:
        """
        查找匹配的订阅者（支持通配符）

        匹配规则：
        - "agent.*" 匹配 "agent.created", "agent.started" 等
        - "agent.**" 匹配 "agent.x.y" 等多层
        - 精确匹配优先级高于通配符
        """
        import fnmatch

        matching: List[Tuple[Any, bool, bool]] = []
        seen: Set[int] = set()

        for pattern, subscribers in self._subscribers.items():
            if fnmatch.fnmatch(event_type, pattern):
                for sub in subscribers:
                    cb_id = id(sub[0])
                    if cb_id not in seen:
                        seen.add(cb_id)
                        matching.append(sub)

        return matching

    def _record_event(self, event_type: str, data: Any) -> None:
        """记录事件到历史"""
        self._event_history.append({
            "type": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        if len(self._event_history) > self._max_history:
            self._event_history = self._event_history[-self._max_history:]

    def _cleanup_once_subscribers(self, event_type: str) -> None:
        """清理一次性订阅"""
        import fnmatch

        for pattern in list(self._subscribers.keys()):
            if fnmatch.fnmatch(event_type, pattern):
                self._subscribers[pattern] = [
                    (cb, is_async, once)
                    for cb, is_async, once in self._subscribers[pattern]
                    if not once
                ]
                if not self._subscribers[pattern]:
                    del self._subscribers[pattern]


# ============================================================================
# 消息总线（核心门面）
# ============================================================================


class MessageBus:
    """
    消息总线 —— Agent 间通信的核心门面

    整合了：
    - 消息队列管理（每个 Agent 一个 MessageQueue）
    - 事件总线（发布/订阅）
    - 消息路由和投递
    - 广播支持
    - 消息生命周期管理

    使用方式::

        # 创建队列
        message_bus.create_queue("agent_001")

        # 发送消息
        msg = AgentMessage.command(
            sender_id="agent_orchestrator",
            receiver_id="agent_001",
            subject="审计 backend/",
            content={"target": "backend/"},
        )
        message_bus.send_message(msg)

        # 接收消息
        messages = message_bus.get_messages("agent_001")
        for msg in messages:
            print(f"收到: {msg.subject}")
            message_bus.acknowledge("agent_001", msg.id)
    """

    def __init__(self, event_bus: Optional[EventBus] = None):
        self._lock = threading.RLock()

        # 消息队列注册表: agent_id → MessageQueue
        self._queues: Dict[str, MessageQueue] = {}

        # 全局消息索引: msg_id → (receiver_id, msg)
        self._global_index: Dict[str, Tuple[str, AgentMessage]] = {}

        # 事件总线
        self._event_bus = event_bus or EventBus()

        # 统计
        self._total_sent: int = 0
        self._total_delivered: int = 0
        self._total_failed: int = 0
        self._total_expired_in_transit: int = 0

    # ---- 属性 ----

    @property
    def event_bus(self) -> EventBus:
        """获取事件总线"""
        return self._event_bus

    # ---- 队列管理 ----

    def create_queue(
        self,
        agent_id: str,
        max_size: int = 1000,
    ) -> MessageQueue:
        """
        为 Agent 创建消息队列

        Args:
            agent_id: Agent ID
            max_size: 队列最大容量

        Returns:
            创建的消息队列

        Raises:
            ValueError: 队列已存在时抛出
        """
        with self._lock:
            if agent_id in self._queues:
                logger.debug(
                    f"[MessageBus] 队列已存在 (agent={agent_id}), 返回已有队列"
                )
                return self._queues[agent_id]

            queue = MessageQueue(agent_id=agent_id, max_size=max_size)
            self._queues[agent_id] = queue

            # 发布事件
            self._event_bus.publish(
                EventBus.Events.AGENT_CREATED,
                {"agent_id": agent_id, "queue_size": max_size},
            )

            logger.info(f"[MessageBus] 创建消息队列: agent={agent_id}, max_size={max_size}")
            return queue

    def delete_queue(self, agent_id: str) -> bool:
        """
        删除 Agent 的消息队列

        Args:
            agent_id: Agent ID

        Returns:
            True 表示删除成功，False 表示队列不存在
        """
        with self._lock:
            if agent_id not in self._queues:
                return False

            queue = self._queues.pop(agent_id)

            # 清理全局索引中属于该队列的消息
            purged = 0
            for msg_id in list(self._global_index.keys()):
                rid, _ = self._global_index[msg_id]
                if rid == agent_id:
                    del self._global_index[msg_id]
                    purged += 1

            logger.info(
                f"[MessageBus] 删除消息队列: agent={agent_id}, "
                f"丢弃消息={queue.size}, 索引清理={purged}"
            )
            return True

    def has_queue(self, agent_id: str) -> bool:
        """检查 Agent 是否有消息队列"""
        with self._lock:
            return agent_id in self._queues

    def get_queue(self, agent_id: str) -> Optional[MessageQueue]:
        """获取 Agent 的消息队列"""
        with self._lock:
            return self._queues.get(agent_id)

    def get_all_agent_ids(self) -> List[str]:
        """获取所有已注册队列的 Agent ID"""
        with self._lock:
            return list(self._queues.keys())

    # ---- 消息发送 ----

    def send_message(self, message: AgentMessage) -> bool:
        """
        发送消息到指定 Agent 的队列

        Args:
            message: 要发送的消息

        Returns:
            True 表示发送成功，False 表示失败
        """
        if message.is_expired:
            logger.warning(f"[MessageBus] 消息已过期，拒绝发送: {message.id}")
            self._total_expired_in_transit += 1
            return False

        receiver_id = message.receiver_id

        # 广播消息特殊处理
        if message.msg_type == MessageType.BROADCAST:
            return self._broadcast_message(message)

        if not receiver_id:
            logger.error(f"[MessageBus] 非广播消息缺少 receiver_id: {message.id}")
            self._total_failed += 1
            return False

        with self._lock:
            queue = self._queues.get(receiver_id)
            if queue is None:
                logger.error(
                    f"[MessageBus] 目标 Agent 队列不存在: "
                    f"receiver={receiver_id}, msg={message.id}"
                )
                self._total_failed += 1
                return False

            success = queue.enqueue(message)
            if success:
                self._global_index[message.id] = (receiver_id, message)
                self._total_sent += 1
                self._total_delivered += 1

                # 发布事件
                self._event_bus.publish(
                    EventBus.Events.MESSAGE_RECEIVED,
                    {
                        "message_id": message.id,
                        "msg_type": message.msg_type.value,
                        "sender_id": message.sender_id,
                        "receiver_id": receiver_id,
                    },
                )

                logger.debug(
                    f"[MessageBus] 消息已投递: {message.id} "
                    f"({message.msg_type.value}) "
                    f"{message.sender_id} → {receiver_id}"
                )
            else:
                self._total_failed += 1

            return success

    def send_and_wait_response(
        self,
        message: AgentMessage,
        timeout_seconds: float = 30.0,
    ) -> Optional[AgentMessage]:
        """
        发送消息并同步等待响应

        Args:
            message: 要发送的消息
            timeout_seconds: 等待超时时间

        Returns:
            响应消息或 None（超时）
        """
        correlation_id = message.id

        # 创建一个临时队列来接收响应
        temp_queue_id = f"_reply_{correlation_id}"
        self.create_queue(temp_queue_id, max_size=10)
        message.reply_to = temp_queue_id

        try:
            if not self.send_message(message):
                return None

            # 轮询等待响应
            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                response = self.get_messages(temp_queue_id, mark_read=True)
                if response:
                    return response[0]
                # 使用短 sleep 而非阻塞等待
                time.sleep(0.1)

            logger.warning(
                f"[MessageBus] 等待响应超时: correlation_id={correlation_id}, "
                f"timeout={timeout_seconds}s"
            )
            return None

        finally:
            self.delete_queue(temp_queue_id)

    def _broadcast_message(self, message: AgentMessage) -> bool:
        """
        广播消息到所有 Agent 队列

        Args:
            message: 广播消息

        Returns:
            True 表示至少投递到一个队列
        """
        exclude_sender = message.metadata.get("exclude_sender", True)
        target_agents = message.metadata.get("target_agents")

        with self._lock:
            agent_ids = list(self._queues.keys())

        # 过滤目标
        if target_agents:
            agent_ids = [a for a in agent_ids if a in target_agents]
        if exclude_sender and message.sender_id:
            agent_ids = [a for a in agent_ids if a != message.sender_id]

        if not agent_ids:
            logger.debug("[MessageBus] 广播无目标 Agent")
            return False

        delivered = 0
        for agent_id in agent_ids:
            copy_msg = AgentMessage.from_dict(message.to_dict())
            copy_msg.receiver_id = agent_id
            copy_msg.id = f"{message.id}_to_{agent_id}"

            with self._lock:
                queue = self._queues.get(agent_id)
                if queue and queue.enqueue(copy_msg):
                    self._global_index[copy_msg.id] = (agent_id, copy_msg)
                    delivered += 1

        self._total_sent += 1
        self._total_delivered += delivered

        logger.debug(
            f"[MessageBus] 广播: {message.id} → {delivered}/{len(agent_ids)} agents"
        )
        return delivered > 0

    # ---- 消息接收 ----

    def get_messages(
        self,
        agent_id: str,
        mark_read: bool = True,
        msg_type: Optional[MessageType] = None,
        limit: int = 0,
    ) -> List[AgentMessage]:
        """
        获取 Agent 的消息

        Args:
            agent_id: Agent ID
            mark_read: 是否标记为已读
            msg_type: 过滤消息类型
            limit: 最大返回数量

        Returns:
            消息列表
        """
        with self._lock:
            queue = self._queues.get(agent_id)
            if queue is None:
                logger.warning(
                    f"[MessageBus] get_messages 失败: 队列不存在 (agent={agent_id})"
                )
                return []

        return queue.get_all(mark_read=mark_read, msg_type=msg_type, limit=limit)

    def get_next_message(
        self,
        agent_id: str,
        mark_read: bool = True,
    ) -> Optional[AgentMessage]:
        """
        获取 Agent 下一条消息（按优先级）

        Args:
            agent_id: Agent ID
            mark_read: 是否标记为已读

        Returns:
            消息或 None
        """
        with self._lock:
            queue = self._queues.get(agent_id)
            if queue is None:
                return None

        return queue.dequeue(mark_read=mark_read)

    def acknowledge(self, agent_id: str, message_id: str) -> bool:
        """
        确认消息已处理

        Args:
            agent_id: Agent ID
            message_id: 消息 ID

        Returns:
            True 表示确认成功
        """
        with self._lock:
            queue = self._queues.get(agent_id)
            if queue is None:
                return False

        result = queue.acknowledge(message_id)
        if result:
            self._event_bus.publish(
                EventBus.Events.MESSAGE_ACKNOWLEDGED,
                {"agent_id": agent_id, "message_id": message_id},
            )
        return result

    # ---- 查询 ----

    def get_queue_size(self, agent_id: str) -> int:
        """获取 Agent 消息队列大小"""
        with self._lock:
            queue = self._queues.get(agent_id)
            return queue.size if queue else 0

    def get_queue_statistics(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """获取 Agent 消息队列统计"""
        with self._lock:
            queue = self._queues.get(agent_id)
            return queue.get_statistics() if queue else None

    def get_global_statistics(self) -> Dict[str, Any]:
        """获取全局消息总线统计"""
        with self._lock:
            total_pending = sum(q.size for q in self._queues.values())
            return {
                "total_queues": len(self._queues),
                "total_sent": self._total_sent,
                "total_delivered": self._total_delivered,
                "total_failed": self._total_failed,
                "total_expired_in_transit": self._total_expired_in_transit,
                "total_pending": total_pending,
                "agent_ids": list(self._queues.keys()),
                "events_published": self._event_bus._total_published,
                "event_subscribers": self._event_bus.get_subscriber_count(),
            }

    # ---- 维护 ----

    def purge_expired(self) -> int:
        """
        清理所有队列中的过期消息

        Returns:
            清理的消息总数
        """
        total = 0
        with self._lock:
            for queue in self._queues.values():
                total += queue._purge_expired()
        if total > 0:
            logger.info(f"[MessageBus] 清理过期消息: {total} 条")
        return total

    def clear_all(self) -> int:
        """
        清空所有队列

        Returns:
            清除的消息总数
        """
        total = 0
        with self._lock:
            for queue in self._queues.values():
                total += queue.clear()
            self._global_index.clear()
            self._total_sent = 0
            self._total_delivered = 0
            self._total_failed = 0
        return total


# ============================================================================
# 全局实例
# ============================================================================

# 全局事件总线
event_bus = EventBus()

# 全局消息总线
message_bus = MessageBus(event_bus=event_bus)
