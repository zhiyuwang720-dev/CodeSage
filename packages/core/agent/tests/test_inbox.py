"""Inbox 投影测试:变更面、归一化、通知、重放、校验。"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]  # 仓库根
_PACKAGES = Path(__file__).resolve().parents[3]  # packages/
sys.path.insert(0, str(_PACKAGES))
sys.path.insert(0, str(_ROOT / "cordis-py"))

from core.agent import Inbox  # noqa: E402
from core.session import Session  # noqa: E402


class Notifications:
    """记录通知的测试桩(记录面与回调面同名会冲突,记录加 _log 后缀)。"""

    def __init__(self) -> None:
        self.inserted_log = []
        self.discarded_log = []
        self.claimed_log = []

    def inserted(self, message):
        self.inserted_log.append(message.get("id"))

    def discarded(self, message):
        self.discarded_log.append(message.get("id"))

    def claimed(self, message, turn):
        self.claimed_log.append((message.get("id"), turn))


def _message(id_: str) -> dict:
    return {"id": id_, "role": "user", "content": [], "source": {"kind": "user"}}


def _make_inbox(session, notifications):
    return Inbox(session, notifications)


def test_append_and_prepend():
    session = Session.create("t1")
    notes = Notifications()
    inbox = _make_inbox(session, notes)
    inbox.append("next-turn", _message("a"))
    inbox.prepend("next-turn", _message("b"))
    assert [m["id"] for m in inbox.next_turn] == ["b", "a"]
    assert notes.inserted_log == ["a", "b"]
    # 每个变更一条耐久事件
    assert session.seq == 2
    events = [e for e in session.events if e["type"] == "agent/inbox/spliced"]
    assert [e["data"]["start"] for e in events] == [0, 0]


def test_claim_consumes_both_lists():
    session = Session.create("t2")
    notes = Notifications()
    inbox = _make_inbox(session, notes)
    inbox.append("next-turn", _message("a"))
    inbox.append("next-step", _message("s"))
    claimed = inbox.claim("next-turn", 7)
    assert [m["id"] for m in claimed] == ["s", "a"]  # next-step 在前
    assert not inbox.has_pending
    assert notes.claimed_log == [("s", 7), ("a", 7)]
    # claim 的 splice 是纯删除,不带 outcome
    events = [e for e in session.events if e["type"] == "agent/inbox/spliced"]
    assert all("outcome" not in e["data"] for e in events)


def test_clear_cancels_with_outcome():
    session = Session.create("t3")
    notes = Notifications()
    inbox = _make_inbox(session, notes)
    inbox.append("next-step", _message("s"))
    inbox.append("next-turn", _message("a"))
    inbox.clear()
    assert not inbox.has_pending
    assert notes.discarded_log == ["s", "a"]
    events = [e for e in session.events if e["type"] == "agent/inbox/spliced"]
    # 两次 append 纯插入(无 outcome)+ 两次 clear 删除(带 outcome)
    assert [e["data"].get("outcome") for e in events] == [None, None, "canceled", "canceled"]


def test_replace_and_remove():
    session = Session.create("t4")
    notes = Notifications()
    inbox = _make_inbox(session, notes)
    inbox.append("next-turn", _message("old"))
    # replace:旧消息 discarded、新消息 inserted
    assert inbox.replace("old", _message("new")) is True
    assert [m["id"] for m in inbox.next_turn] == ["new"]
    assert notes.discarded_log == ["old"]
    assert notes.inserted_log == ["old", "new"]
    # 不存在时 false,不写事件
    assert inbox.replace("absent", _message("x")) is False
    assert inbox.remove("absent") is False
    assert inbox.remove("new") is True
    assert not inbox.has_pending


def test_duplicate_pending_rejected():
    session = Session.create("t5")
    inbox = _make_inbox(session, Notifications())
    inbox.append("next-turn", _message("a"))
    with pytest.raises(ValueError, match="already pending"):
        inbox.append("next-step", _message("a"))


def test_out_of_bounds_splice_clamped():
    """活体 splice 越界按 JS 语义钳制,不抛(校验只拒重复 id 与重放损坏)。"""
    session = Session.create("t6")
    inbox = _make_inbox(session, Notifications())
    inbox.append("next-turn", _message("a"))
    # start 越界:钳制到末尾追加
    removed = inbox.splice("next-turn", 5, 0, [_message("x")])
    assert removed == []
    assert [m["id"] for m in inbox.next_turn] == ["a", "x"]
    # 删除越界:钳制到剩余长度
    removed = inbox.splice("next-turn", 0, 99, [])
    assert [m["id"] for m in removed] == ["a", "x"]
    assert not inbox.has_pending
    # 重复 id 跨列表仍拒绝
    inbox.append("next-step", _message("dup"))
    with pytest.raises(ValueError, match="already pending"):
        inbox.append("next-turn", _message("dup"))


def test_negative_start_normalization():
    session = Session.create("t7")
    inbox = _make_inbox(session, Notifications())
    inbox.append("next-turn", _message("a"))
    inbox.append("next-turn", _message("b"))
    removed = inbox.splice("next-turn", -1, 0, [_message("c")])
    assert removed == []
    assert [m["id"] for m in inbox.next_turn] == ["a", "c", "b"]


def test_replay_from_log():
    session = Session.create("t8")
    notes = Notifications()
    inbox = _make_inbox(session, notes)
    inbox.append("next-turn", _message("a"))
    inbox.prepend("next-step", _message("s"))
    inbox.remove("a")
    # 从日志重放的新投影:状态一致,不发布通知
    fresh_notes = Notifications()
    replay = Inbox(session, fresh_notes)
    assert [m["id"] for m in replay.next_step] == ["s"]
    assert not replay.next_turn
    assert fresh_notes.inserted_log == []
    assert fresh_notes.discarded_log == []


def test_seed_boundary_ignored():
    """seedLength 边界内的事件不投影(分叉/重放的种子前缀)。"""
    session = Session.create("t9")
    notes = Notifications()
    inbox = _make_inbox(session, notes)
    inbox.append("next-turn", _message("a"))
    seed = list(session.events)
    # 从种子重建的会话:重放跳过 seedLength
    fork = Session.create("t9f", seed=seed)
    fork_inbox = Inbox(fork, Notifications())
    assert [m["id"] for m in fork_inbox.next_turn] == ["a"]
