"""Session storage (phase 12): typed-entry JSONL with lane pointers.

One session = one .jsonl file under the data root (single file holds every
branch). Appends are the only write path (readers replay the file); a corrupt
trailing line is skipped, never fatal. Each message append is followed by a
same-name lane pointer entry (leaf = the new message uuid) so the active
lane's pointer always points at its latest message (§3.4, written design).
Single-writer assumption unchanged from phase 04.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ..messages import SessionMessage
from .entry import (
    SessionEntry,
    make_bookmark_entry,
    make_branch_summary_entry,
    make_lane_entry,
    make_meta_entry,
    make_message_entry,
    make_model_change_entry,
    make_operation_entry,
    parse_entry,
)
from .tree import lane_names, linear_messages

#: 应用状态 entry 类型(§3.2 PI-10 部分采纳):不进入模型上下文,只被读取器消费
_APP_STATE_TYPES = frozenset({"lane", "bookmark", "branch_summary", "meta", "model_change"})

_SANITIZE_PROJECT = re.compile(r"[^A-Za-z0-9]+")


class Session:
    def __init__(self, session_id: str, root: Path, project_key: str | None = None):
        self.session_id = session_id
        base = root
        if project_key is not None:
            sanitized = _SANITIZE_PROJECT.sub("-", project_key).strip("-")
            if sanitized:
                base = root / sanitized
        self.path = base / f"{session_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lane = "main"  # 活跃 lane 名(load 时重建)
        self._cursor: str | None = None  # parent 游标:上一条消息 uuid(load 时重建)

    def append(self, message: SessionMessage) -> None:
        """Append one message durably (phase 04 API; delegates to append_message)."""
        self.append_message(message)

    def append_message(self, message: SessionMessage) -> SessionEntry:
        """唯一消息写入面(§3.4):写消息 entry(挂 parent 游标)后**顺带追加同名
        校验 lane 指针 entry**(leaf=新 uuid)推进活跃 lane —— 调用方不感知指针。"""
        entry = make_message_entry(message, parent=self._cursor)
        self._append(entry)
        self._append(make_lane_entry(name=self._lane, leaf=entry.uuid))
        self._cursor = entry.uuid
        return entry

    def append_lane(self, name: str, leaf: str) -> SessionEntry:
        """§3.4 fork 用:追加新 lane entry 并重置游标(活跃 lane=name,parent
        游标=leaf) —— 后续 append_message 的新消息挂 leaf 而非旧游标。"""
        self._lane, self._cursor = name, leaf
        return self._append(make_lane_entry(name=name, leaf=leaf))

    def fork(self, entry_id: str, *, name: str | None = None) -> str:
        """§4.2 从 entry_id 分支:追加新 lane entry(leaf = entry_id 本身,分支
        起点)并重置游标(活跃 lane=新名,parent 游标=entry_id) —— 后续
        append_message 的新消息挂 fork 点,绕过原分支后续消息。name 缺省 =
        "main-{n}"(n = 既有分支计数 + 1,默认 main 恒计入)。返回 lane name。"""
        if name is None:
            name = self._next_branch_name()
        self.append_lane(name, entry_id)
        return name

    def _next_branch_name(self) -> str:
        """分支命名:main、main-1、main-2…(n = 文件里不同 lane 名个数,默认
        main 恒计入 —— 空文件/旧文件首分支也是 main-1)。"""
        entries, _ = self._read()
        existing = {e.data.get("name") for e in entries if e.type == "lane"}
        existing.discard(None)
        existing.add("main")
        return f"main-{len(existing)}"

    def append_bookmark(self, entry_id: str, name: str) -> SessionEntry:
        """§6 书签:追加命名 bookmark entry(指向被标记 entry;重名 = 追加,
        读端后者胜 —— 永不删除)。"""
        return self._append(make_bookmark_entry(name, entry_id))

    def append_operation(
        self, kind: str, tool: str | None = None, args_summary: str | None = None
    ) -> SessionEntry:
        """§7.1 操作日志(单向 tool_started):工具调用发起点追加;args_summary
        截断 200 字符 —— 应用状态,不进模型上下文。"""
        if args_summary is not None and len(args_summary) > 200:
            args_summary = args_summary[:200]
        return self._append(make_operation_entry(kind, tool=tool, args_summary=args_summary))

    def append_meta(self, **fields: Any) -> SessionEntry:
        """§8.1/§8.3 meta 追加:首行 = 会话自描述锚点;标题等第二个 meta entry
        追加在后(append-only 无法回改首行),读端合并、后者胜。"""
        return self._append(make_meta_entry(**fields))

    def append_model_change(self, to: str, from_: str | None = None) -> SessionEntry:
        """§8.2 会话内模型指针切换(审计/恢复不用猜当时配置)。"""
        return self._append(make_model_change_entry(to, from_=from_))

    def append_branch_summary(self, text: str, leaf: str) -> SessionEntry:
        """§4.5 分支摘要快照:摘要文本 + leaf(压缩切点后第一条消息 uuid,
        保留清单 #14「summary 挂 leafUuid」);不改消息链、不删被覆盖消息。"""
        return self._append(make_branch_summary_entry(text, leaf))

    @property
    def meta(self) -> dict | None:
        """§8.1/§8.3 读端:合并全部 meta entry(后者胜,含 title);无 meta → None。"""
        merged: dict = {}
        for entry in self._read()[0]:
            if entry.type == "meta":
                merged.update(entry.data)
        return merged or None

    def load(self) -> list[SessionMessage]:
        """Replay the log; corrupt lines are skipped, not fatal.

        返回语义不变(§4.3):沿活跃 lane 的线性消息列表(引擎/REPL/压缩管线
        零改动)。旧格式文件(无 type 键,04 纯消息 JSONL)按 §3.3 惰性推导为
        message 行、parent 线性链接,行为与 04 load() 一致;lane 缺失时默认
        单 lane main,leaf = 最后一条消息。
        """
        entries, last_message_uuid = self._read()
        if not entries:
            self._lane, self._cursor = "main", None
            return []
        lane_name, leaf = self._active_lane(entries, last_message_uuid)
        self._lane = lane_name
        chain = self._chain(entries, leaf, last_message_uuid)
        self._cursor = chain[-1].uuid if chain else None  # 重建游标:续写挂活跃 lane 最新消息
        return [entry.as_message() for entry in chain]

    def load_lane(self, lane: str) -> list[SessionMessage]:
        """§4.4/§5 --continue --lane:指定 lane 的线性视图(linear_messages 的
        便捷封装,load() 的 lane 参数版)—— 同时重建游标/活跃 lane 到该 lane
        的 leaf,后续 append_message 续写挂在命名 lane 上而非活跃 lane(否则
        --lane 选分支后新消息会挂回原分支,语义断裂)。未知 lane 抛 ValueError
        (CLI 捕获报错);旧文件(无 lane entry)只认默认 main。"""
        entries, _ = self._read()
        if lane not in lane_names(entries):
            raise ValueError(f"lane not found: {lane}")
        chain = linear_messages(entries, lane)
        self._lane, self._cursor = lane, chain[-1].uuid if chain else None
        return chain

    @property
    def entries(self) -> list[SessionEntry]:
        """§7.2/§10 读面:全部 entry(CLI 的 find_open_operations/编号渲染、
        审计消费;复用 _read 解析,坏行跳过容错)。"""
        return self._read()[0]

    def _read(self) -> tuple[list[SessionEntry], str | None]:
        entries: list[SessionEntry] = []
        last_message_uuid: str | None = None
        if not self.path.exists():
            return entries, last_message_uuid
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = parse_entry(json.loads(line), last_message_uuid)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue  # torn/corrupt line: skip, keep the rest
                if entry is None:
                    continue
                entries.append(entry)
                if entry.type == "message":
                    last_message_uuid = entry.uuid
        return entries, last_message_uuid

    @staticmethod
    def _active_lane(entries: list[SessionEntry], last_message_uuid: str | None) -> tuple[str, str | None]:
        """活跃 lane = 文件最后一条 lane entry(§3.4 推进保证其 leaf 即最新消息);
        坏行恰为最后一条 lane 时其被跳过,自动退回上一个合法 lane;无 lane(旧文件)
        → 单 lane main 兜底(R4)。"""
        for entry in reversed(entries):
            if entry.type == "lane":
                name, leaf = entry.data.get("name"), entry.data.get("leaf")
                if name is None or leaf is None:
                    continue  # 语义损坏行(缺字段):跳过,退回上一个合法 lane
                return name, leaf
        return "main", last_message_uuid

    @staticmethod
    def _chain(
        entries: list[SessionEntry], leaf: str | None, fallback_leaf: str | None
    ) -> list[SessionEntry]:
        """沿 parent 链从 leaf 走到根(无 parent),逆序为线性消息列表;指针悬空
        (损坏)→ 退回最后一条消息(单 lane main 兜底)。"""
        by_uuid = {e.uuid: e for e in entries if e.type == "message"}
        if leaf not in by_uuid:
            leaf = fallback_leaf
        chain: list[SessionEntry] = []
        seen: set[str] = set()
        cur = leaf
        while cur is not None and cur in by_uuid and cur not in seen:
            seen.add(cur)  # 防手写坏文件成环
            chain.append(by_uuid[cur])
            cur = by_uuid[cur].parent
        chain.reverse()
        return chain

    def _append(self, entry: SessionEntry) -> SessionEntry:
        """与 04 相同的追加写:append-only + flush + fsync(§3.4);返回 entry
        供 append_bookmark 等调用方链式返回。"""
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(entry.to_json() + "\n")
            f.flush()
            os.fsync(f.fileno())
        return entry

    @property
    def exists(self) -> bool:
        return self.path.exists()


def list_sessions(root: Path) -> list[Path]:
    """All active session .jsonl files under *root* (incl. project_key subdirs),
    newest mtime first; any level of archive/ directory is excluded (§9.1/§10.2
    red line — archived sessions are invisible to --continue/--session-id);
    subagents/ 一并排除(13 §5.3 R8 —— 子代理转录不污染 --continue//sessions)。"""
    if not root.exists():
        return []
    files = [
        p
        for p in root.rglob("*.jsonl")
        if "archive" not in p.relative_to(root).parts
        and "subagents" not in p.relative_to(root).parts
    ]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def most_recent_session(root: Path) -> Path | None:
    """Path of the newest session file, or None."""
    sessions = list_sessions(root)
    return sessions[0] if sessions else None


def find_session(root: Path, session_id: str) -> Path | None:
    """Locate a session file by id (root-level or inside any project_key subdir)."""
    return next((p for p in list_sessions(root) if p.stem == session_id), None)


def find_open_operations(entries: list[SessionEntry]) -> list[SessionEntry]:
    """§7.2 活跃 lane 上最后一段 operation(纯函数):从文件末尾往前扫,跳过
    应用状态 entry(lane/bookmark/branch_summary/meta/model_change);遇到
    operation → 收集并继续(同一段内的操作都未完成);遇到 message → 结束
    (该消息即「后继消息」,其前的 operation 视为已完成)。中断检测启发式
    (R6):无配对 end,末尾 operation 即视为未完成 —— 误报只产生提示,不
    自动重放,无副作用。kind 感知(13 §11.3):段以 step_completed/
    step_failed 收尾 → 相邻配对完整,视为已完成不报 —— 消除「正常完成的
    后台子代理停在文件末尾 → --continue 误报中断」;孤 step_attempt 照旧
    命中。"""
    open_ops: list[SessionEntry] = []
    for entry in reversed(entries):
        if entry.type in _APP_STATE_TYPES:
            continue
        if entry.type == "operation":
            open_ops.append(entry)
            continue
        break  # message
    open_ops.reverse()
    if open_ops and open_ops[-1].data.get("kind") in ("step_completed", "step_failed"):
        return []
    return open_ops
