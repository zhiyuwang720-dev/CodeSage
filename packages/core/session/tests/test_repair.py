"""崩溃恢复修复测试:平衡日志无产物、两中断码、seq/时间戳延续、产物可重放。

照 DSH repair.spec.ts 的核心断言面:TOOL_NOT_STARTED 与
TOOL_OUTCOME_UNKNOWN 两码的区分、合成事件追加后仍满足
invariant(可安全写回日志)。
"""

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1]  # 包目录 core/session
sys.path.insert(0, str(_CORE))

from core.session.src.invariant import seed_trace  # noqa: E402
from core.session.src.repair import (  # noqa: E402
    TOOL_NOT_STARTED,
    TOOL_OUTCOME_UNKNOWN,
    interrupted_turn_closers,
)


def _ev(seq, type_, data, time=1000, **extra):
    return {"type": type_, "seq": seq, "time": time, "data": data, **extra}


def _turn_start(seq, turn=1, time=1000):
    return _ev(seq, "turn/start", {"turn": turn}, time)


def _assistant(seq, call_ids=(), step=1, time=1000):
    blocks = [{"type": "tool-call", "id": cid, "name": "read", "arguments": "{}"} for cid in call_ids]
    return _ev(
        seq, "assistant/message",
        {"turn": 1, "step": step, "message": {"role": "assistant", "id": "a1", "source": {"kind": "model"}, "content": blocks}},
        time,
        surfaceOp="append",
    )


def _crash_tail(call_ids, *, call_started=False, turn=1, step=1, time=1000, next_seq=0):
    """一条崩溃尾巴:turn 打开、step 打开、调用挂起(或未开始)。"""
    seq = next_seq
    events = [_turn_start(seq, turn=turn, time=time)]
    seq += 1
    events.append(_step_start(seq, step=step, time=time))
    seq += 1
    events.append(_assistant(seq, call_ids=call_ids, step=step, time=time))
    seq += 1
    if call_started:
        for cid in call_ids:
            events.append(_tool_call(seq, cid, step=step, time=time))
            seq += 1
    return events


def _tool_call(seq, call_id, step=1, time=1000):
    return _ev(seq, "tool/call", {"turn": 1, "step": step, "callId": call_id, "name": "read", "arguments": "{}"}, time)


def _tool_result(seq, call_id, step=1, time=1000):
    return _ev(
        seq, "tool/result",
        {"turn": 1, "step": step,
         "message": {"role": "user", "id": "t1", "source": {"kind": "tool", "callId": call_id},
                     "content": [{"type": "tool-result", "toolCallId": call_id, "content": []}]}},
        time, surfaceOp="append",
    )


def _step_start(seq, step=1, time=1000):
    return _ev(seq, "step/start", {"turn": 1, "step": step}, time)


def test_balanced_log_returns_empty():
    events = [
        _turn_start(0),
        _assistant(1),
        _step_start(2),
        _tool_call(3, "c1"),
        _tool_result(4, "c1"),
        _ev(5, "step/end", {"turn": 1, "step": 1}),
        _ev(6, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
    ]
    assert interrupted_turn_closers(events) == []


def test_empty_log_returns_empty():
    assert interrupted_turn_closers([]) == []


def test_crash_after_call_start_closes_with_unknown_outcome():
    events = _crash_tail(["c1"], call_started=True)
    closers = interrupted_turn_closers(events)
    assert [c["type"] for c in closers] == ["tool/result", "step/end", "turn/end"]
    result = closers[0]
    assert result["data"]["error"]["code"] == TOOL_OUTCOME_UNKNOWN
    assert result["sourceEventSeqs"] == [3]  # 引用 tool/call 的 seq
    assert result["seq"] == 4  # seq 顺延
    assert result["time"] == 1000  # 时间戳复用最后真实事件
    assert result["data"]["message"]["content"][0]["isError"] is True
    assert closers[1]["data"] == {"turn": 1, "step": 1}
    assert closers[2]["data"]["reason"] == {"kind": "interrupted"}
    assert [c["seq"] for c in closers] == [4, 5, 6]


def test_crash_before_call_start_closes_with_not_started():
    events = _crash_tail(["c1"], call_started=False)
    closers = interrupted_turn_closers(events)
    result = closers[0]
    assert result["data"]["error"]["code"] == TOOL_NOT_STARTED
    assert "sourceEventSeqs" not in result
    # 文案区分两种中断
    assert "before the Harness recorded it as started" in result["data"]["message"]["content"][0]["content"][0]["text"]


def test_pending_calls_reset_at_turn_boundary():
    # 前一个 turn 的调用不能漏进尾巴修复
    events = [
        *_crash_tail(["c-old"], call_started=True, turn=1),
        _tool_result(4, "c-old"),
        _ev(5, "step/end", {"turn": 1, "step": 1}),
        _ev(6, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        *_crash_tail(["c1"], call_started=False, turn=2, time=2000, next_seq=7),
    ]
    closers = interrupted_turn_closers(events)
    assert [c["type"] for c in closers] == ["tool/result", "step/end", "turn/end"]
    assert closers[0]["data"]["message"]["source"]["callId"] == "c1"
    assert closers[0]["seq"] == 10


def test_closers_replay_through_invariant():
    # 修复产物必须能被 validate/seed 接受 —— 这是它能安全写回的前提
    events = _crash_tail(["c1"], call_started=False)
    repaired = events + interrupted_turn_closers(events)
    seed_trace(repaired)  # 不抛
