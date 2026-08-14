"""Archiving (phase 12 S5, spec §9.1): move session files in/out of archive/.

Archive = a file move (same-disk os.replace is atomic on POSIX and Windows;
config/atomic.py only covers rewrites, a move needs no atomic write).
Root-level sessions go to `root/archive/`, project-key-scoped sessions to
`root/{project}/archive/` — the archive dir always sits next to the session
file it contains. list_sessions excludes any level of archive/ (§10.2 red
line: --continue/--session-id never see archived sessions); active_sessions /
archived_sessions are the two enumerators behind /sessions. Archive never
deletes: restore_session is the one-line reverse move.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .entry import parse_entry
from .session import find_session


@dataclass(slots=True)
class SessionMeta:
    """会话列表条目(§9.2 渲染面):公开字段按列表需要,path 是内部字段。"""

    session_id: str
    title: str | None  # meta.title 合并值;无 → None(渲染面显示 (untitled))
    messages: int  # message entry 数
    branches: int  # 不同 lane 名数(旧文件缺 lane → 1)
    mtime: float  # 文件修改时间(排序键)
    path: Path = field(repr=False)  # 内部:归档/恢复定位用


def _session_files(root: Path, *, archived: bool) -> list[Path]:
    """枚举 root 下全部会话文件,按是否处于任一 archive/ 层级分组 —— 递归
    搜索里带 archive 路径分量即归档(§9.1「排除任何层级的 archive/ 目录」)。"""
    if not root.exists():
        return []
    return [
        p
        for p in root.rglob("*.jsonl")
        if ("archive" in p.relative_to(root).parts) == archived
    ]


def _read_meta(path: Path) -> SessionMeta:
    """解析一个会话文件的列表元信息(复用 parse_entry,坏行跳过容错);
    标题 = 全部 meta entry 合并后的 title(§8.3,读端合并、后者胜)。"""
    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = parse_entry(json.loads(line), None)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                if entry is not None:
                    entries.append(entry)
    except OSError:
        pass  # 枚举后文件被删(并发)→ 跳过,不致命
    merged: dict = {}
    for e in entries:
        if e.type == "meta":
            merged.update(e.data)
    return SessionMeta(
        session_id=path.stem,
        title=merged.get("title"),
        messages=sum(1 for e in entries if e.type == "message"),
        branches=len({e.data.get("name") for e in entries if e.type == "lane"}) or 1,
        mtime=path.stat().st_mtime if path.exists() else 0.0,
        path=path,
    )


def active_sessions(root: Path) -> list[SessionMeta]:
    """活跃会话(排除任何层级 archive/),按 mtime 倒序(§9.2 选择器数据面)。"""
    return sorted(
        (_read_meta(p) for p in _session_files(root, archived=False)),
        key=lambda m: m.mtime,
        reverse=True,
    )


def archived_sessions(root: Path) -> list[SessionMeta]:
    """仅归档会话(archive/ 内),同样按 mtime 倒序。"""
    return sorted(
        (_read_meta(p) for p in _session_files(root, archived=True)),
        key=lambda m: m.mtime,
        reverse=True,
    )


def archive_session(root: Path, session_id: str) -> Path:
    """归档 = 移动文件(§9.1):root 级会话 → root/archive/,project 级 →
    root/{project}/archive/。不存在/已在 archive → ValueError —— find_session
    经 list_sessions(排除 archive)查找,「已归档」自然落入 not found。"""
    path = find_session(root, session_id)
    if path is None:
        raise ValueError(f"session not found: {session_id}")
    archive_dir = path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / path.name
    if dest.exists():
        raise ValueError(f"archive target already exists: {dest}")  # 不覆盖(永不删除)
    os.replace(path, dest)
    return dest


def restore_session(root: Path, session_id: str) -> Path:
    """一行恢复(§9.1):从任意层级 archive/ 移回原位置(archive/ 的上级目录)。
    不在 archive → ValueError。"""
    target = next(
        (m for m in archived_sessions(root) if m.session_id == session_id), None
    )
    if target is None:
        raise ValueError(f"archived session not found: {session_id}")
    dest = target.path.parent.parent / target.path.name
    if dest.exists():
        raise ValueError(f"restore target already exists: {dest}")  # 不覆盖
    os.replace(target.path, dest)
    return dest
