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

from ..messages import SessionMessage
from .entry import SessionEntry, make_lane_entry, make_message_entry, parse_entry

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

    def _append(self, entry: SessionEntry) -> None:
        """与 04 相同的追加写:append-only + flush + fsync(§3.4)。"""
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(entry.to_json() + "\n")
            f.flush()
            os.fsync(f.fileno())

    @property
    def exists(self) -> bool:
        return self.path.exists()


def list_sessions(root: Path) -> list[Path]:
    """All session .jsonl files under *root* (incl. project_key subdirs), newest mtime first."""
    if not root.exists():
        return []
    files = list(root.glob("*.jsonl"))
    files.extend(p for sub in root.iterdir() if sub.is_dir() for p in sub.glob("*.jsonl"))
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def most_recent_session(root: Path) -> Path | None:
    """Path of the newest session file, or None."""
    sessions = list_sessions(root)
    return sessions[0] if sessions else None


def find_session(root: Path, session_id: str) -> Path | None:
    """Locate a session file by id (root-level or inside any project_key subdir)."""
    return next((p for p in list_sessions(root) if p.stem == session_id), None)
