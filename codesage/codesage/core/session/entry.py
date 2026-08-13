"""Typed session entries: the row model of the session JSONL (phase 12).

Each line of a session file is one entry:
`{"type", "uuid", "timestamp", "parent", **type-specific data}` (spec §3.2).

Message entries reuse SessionMessage serialization unchanged (phase 04
contract, zero changes); the other six types are application state — read by
tree views / resume / audit, never fed to the LLM (§3.2 PI-10 partial).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from ..messages import SessionMessage

#: 七类 entry(§3.2):message 是唯一进入模型上下文的 entry,其余为应用状态
EntryType = Literal[
    "message", "lane", "bookmark", "branch_summary", "operation", "model_change", "meta"
]
ENTRY_TYPES: tuple[str, ...] = EntryType.__args__


@dataclass(slots=True)
class SessionEntry:
    """One typed row of the session JSONL."""

    type: EntryType
    uuid: str  # 唯一;消息 entry 的 uuid 即其身份(与 04 的 uuid 同源)
    timestamp: str
    parent: str | None  # 消息链:沿 parent 走;lane/bookmark/meta 等应用状态无
    data: dict[str, Any]  # 类型特有字段(message = SessionMessage.to_dict())

    def to_json(self) -> str:
        return json.dumps(
            {
                "type": self.type,
                "uuid": self.uuid,
                "timestamp": self.timestamp,
                "parent": self.parent,
                **self.data,  # 消息 entry 的 data 自带同源 uuid/timestamp,值与上层一致
            },
            ensure_ascii=False,
        )

    def as_message(self) -> SessionMessage | None:
        """message entry → SessionMessage(§3.2 线性视图投影);应用状态 entry 返回 None。"""
        if self.type != "message":
            return None
        return SessionMessage.from_dict(self.data)


def parse_entry(raw: dict[str, Any], prev_message_uuid: str | None) -> SessionEntry | None:
    """一行 JSON 对象 → SessionEntry;损坏/未知类型返回 None(load 跳过,不致命)。

    旧格式兼容(§3.3 惰性推导):无 `type` 键的行 = 04 纯消息行 → 视为 message,
    parent 推导为上一行消息的 uuid(线性链);新格式 message 缺 parent 时同样推导。
    """
    etype = raw.get("type")
    if etype is None or etype == "message":
        message = SessionMessage.from_dict(raw)
        if message is None:
            return None  # 未知 role / 缺 content 等:按 04 语义跳过
        parent = raw.get("parent") if raw.get("parent") is not None else prev_message_uuid
        return SessionEntry(
            type="message",
            uuid=message.uuid,
            timestamp=message.timestamp,
            parent=parent,
            data=message.to_dict(),
        )
    if etype not in ENTRY_TYPES:
        return None  # 未知类型:视为损坏,跳过
    return SessionEntry(
        type=etype,
        uuid=raw.get("uuid") or uuid.uuid4().hex,
        timestamp=raw.get("timestamp", ""),
        parent=raw.get("parent"),
        data={k: v for k, v in raw.items() if k not in ("type", "uuid", "timestamp", "parent")},
    )


def _new(type_: EntryType, data: dict[str, Any]) -> SessionEntry:
    return SessionEntry(
        type=type_,
        uuid=uuid.uuid4().hex,
        timestamp=datetime.now(timezone.utc).isoformat(),
        parent=None,
        data=data,
    )


def make_message_entry(message: SessionMessage, parent: str | None) -> SessionEntry:
    """消息 entry:复用 SessionMessage 序列化(uuid/timestamp 同源),parent 挂链。"""
    return SessionEntry(
        type="message",
        uuid=message.uuid,
        timestamp=message.timestamp,
        parent=parent,
        data=message.to_dict(),
    )


def make_lane_entry(name: str, leaf: str) -> SessionEntry:
    """lane 指针 entry(§3.4):活跃 lane 名 + 指向最新消息;fork 时 leaf=分支起点。"""
    return _new("lane", {"name": name, "leaf": leaf})


def make_bookmark_entry(name: str, entry: str) -> SessionEntry:
    """书签(§6):命名标记某个 entry;追加式,重名覆盖 = 读端后者胜。"""
    return _new("bookmark", {"name": name, "entry": entry})


def make_branch_summary_entry(content: str, leaf: str) -> SessionEntry:
    """分支摘要快照(§4.5):摘要文本 + 挂分支 leaf(保留清单 #14「summary 挂 leafUuid」)。"""
    return _new("branch_summary", {"content": content, "leaf": leaf})


def make_operation_entry(
    kind: str, tool: str | None = None, args_summary: str | None = None
) -> SessionEntry:
    """操作日志(§7,单向 tool_started;配对 end 归后续强化)。"""
    return _new("operation", {"kind": kind, "tool": tool, "args_summary": args_summary})


def make_model_change_entry(to: str, from_: str | None = None) -> SessionEntry:
    """会话内模型指针切换(§8.2),审计/恢复不用猜当时配置。"""
    return _new("model_change", {"to": to, "from": from_})


def make_meta_entry(**kw: Any) -> SessionEntry:
    """会话自描述锚点(§8.1,文件首行);标题等后续 meta 追加,读端合并、后者胜。"""
    return _new("meta", dict(kw))
