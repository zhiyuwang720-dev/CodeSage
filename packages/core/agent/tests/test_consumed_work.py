"""foldConsumedWork 记账测试:回合如何为它消费的工作结账。"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]  # 仓库根
_PACKAGES = Path(__file__).resolve().parents[3]  # packages/
sys.path.insert(0, str(_PACKAGES))
sys.path.insert(0, str(_ROOT / "cordis-py"))

from core.agent import fold_consumed_work  # noqa: E402


def _event(type_: str, **data):
    return {"type": type_, "data": data}


def _splice(start: int, removed_count: int, inserted=(), outcome=None):
    data = {"target": "next-turn", "start": start, "inserted": list(inserted)}
    if removed_count:
        data["removedCount"] = removed_count
    if outcome:
        data["outcome"] = outcome
    return _event("agent/inbox/spliced", **data)


def test_no_work():
    result = fold_consumed_work([])
    assert result.end is None and result.dropped_unrun is False


def test_stepped_turn_accounts():
    """进入过步骤的回合:任何结束都记账。"""
    events = [
        _event("turn/start", turn=1),
        _splice(0, 1),
        _event("step/start", turn=1, step=1),
        _event("turn/end", turn=1, reason={"kind": "completed"}),
    ]
    result = fold_consumed_work(events)
    assert result.end is not None and result.dropped_unrun is False


def test_claimed_blocked_turn_accounts():
    """认领过输入后被拒(blocked)的回合:为输入记账。"""
    events = [
        _event("turn/start", turn=1),
        _splice(0, 1),
        _event("turn/end", turn=1, reason={"kind": "blocked"}),
    ]
    result = fold_consumed_work(events)
    assert result.end is not None and result.dropped_unrun is False


def test_claimed_completed_turn_does_not_account():
    """认领被改写掉的 completed 回合:无可跑,不记账。"""
    events = [
        _event("turn/start", turn=1),
        _splice(0, 1),
        _event("turn/end", turn=1, reason={"kind": "completed"}),
    ]
    result = fold_consumed_work(events)
    assert result.end is None and result.dropped_unrun is False


def test_noop_turn_not_accounted():
    """没认领没步骤的空回合:不记账。"""
    events = [
        _event("turn/start", turn=1),
        _event("turn/end", turn=1, reason={"kind": "completed"}),
    ]
    result = fold_consumed_work(events)
    assert result.end is None and result.dropped_unrun is False


def test_canceled_after_closing_turn_marks_dropped():
    """回合关闭后的取消:工作被丢弃但无回合为之记账。"""
    events = [
        _event("turn/start", turn=1),
        _splice(0, 1),
        _event("turn/end", turn=1, reason={"kind": "completed"}),
        _splice(0, 1, outcome="canceled"),
    ]
    result = fold_consumed_work(events)
    assert result.end is None and result.dropped_unrun is True


def test_canceled_within_turn_absorbed():
    """回合内取消:由该回合的结尾(aborted)记账,dropped 复位。"""
    events = [
        _event("turn/start", turn=1),
        _splice(0, 1),
        _splice(0, 1, outcome="canceled"),
        _event("turn/end", turn=1, reason={"kind": "aborted"}),
        _splice(0, 1, outcome="canceled"),  # 回合后的丢弃仍未记账
    ]
    result = fold_consumed_work(events)
    assert result.end is not None and result.dropped_unrun is True


def test_stepped_turn_later_drop_detached():
    """步过回合关闭后丢弃:end 保留(已记账),dropped 归后面。"""
    events = [
        _event("turn/start", turn=1),
        _splice(0, 1),
        _event("step/start", turn=1, step=1),
        _event("turn/end", turn=1, reason={"kind": "completed"}),
    ]
    result = fold_consumed_work(events)
    assert result.end is not None and result.dropped_unrun is False


def test_replacement_keeps_pending():
    """替换(带插入、无 outcome)不算丢弃。"""
    events = [
        _event("turn/start", turn=1),
        _splice(0, 1, inserted=[{"id": "new"}]),  # 认领插入?实为替换
        _event("turn/end", turn=1, reason={"kind": "blocked"}),
    ]
    result = fold_consumed_work(events)
    assert result.end is not None and result.dropped_unrun is False


def test_canceled_replacement_with_insert_not_dropped():
    """取消但留下插入:工作仍待办,不算丢弃。"""
    events = [
        _splice(0, 1, inserted=[{"id": "new"}], outcome="canceled"),
    ]
    result = fold_consumed_work(events)
    assert result.end is None and result.dropped_unrun is False


def test_unknown_turn_end_accounts():
    """未知结尾(词表可扩展):消费过输入不得读作成功。"""
    events = [
        _event("turn/start", turn=1),
        _splice(0, 1),
        _event("turn/end", turn=1, reason={"kind": "mystery"}),
    ]
    result = fold_consumed_work(events)
    assert result.end is not None
