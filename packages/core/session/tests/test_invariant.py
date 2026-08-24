"""关系不变式测试:seq 连续、turn/step 括号、调用配对、end-seed 豁免。

覆盖 DSH invariant.spec.ts 的核心断言面:合法日志流通过、每个
违规点精确拒绝。
"""

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1]  # 包目录 core/session
sys.path.insert(0, str(_CORE))

from core.session.src.invariant import apply_transition, fresh_trace, seed_trace, validate_event  # noqa: E402


def _event(seq: int, type_: str, data: dict, **extra):
    return {"type": type_, "seq": seq, "time": 1, "data": data, **extra}


def _apply(trace, event):
    apply_transition(trace, validate_event(trace, event))


def _valid_turn(turn: int, step: int = 1):
    """一个完整 step 的合法事件序列:turn 包 step 包调用配对。"""
    return [
        _event(0, "turn/start", {"turn": turn}),
        _event(1, "user/message", {"role": "user", "id": "u1", "source": {"kind": "human"}, "content": []}),
        _event(2, "step/start", {"turn": turn, "step": step}),
        _event(3, "assistant/message", {
            "turn": turn, "step": step,
            "message": {"role": "assistant", "id": "a1", "source": {"kind": "model", "provider": "p", "model": "m"}, "content": []},
        }, surfaceOp="append"),
        _event(4, "tool/call", {"turn": turn, "step": step, "callId": "c1", "name": "read", "arguments": "{}"}),
        _event(5, "tool/result", {
            "turn": turn, "step": step,
            "message": {"role": "user", "id": "t1", "source": {"kind": "tool", "callId": "c1"},
                        "content": [{"type": "tool-result", "toolCallId": "c1", "content": [{"type": "text", "text": "ok"}]}]},
        }, surfaceOp="append"),
        _event(6, "step/end", {"turn": turn, "step": step}),
        _event(7, "turn/end", {"turn": turn, "reason": {"kind": "completed"}}),
    ]


def test_valid_log_passes():
    trace = fresh_trace()
    for event in _valid_turn(1):
        _apply(trace, event)
    assert trace.last_seq == 7
    assert trace.open_turn is None
    assert trace.next_turn == 2
    assert trace.pending_calls == set()


def test_seq_must_strictly_increase():
    # 不变式只要求单调递增;严格连续(seq = log.length)由
    # Session.append 的接纳边界强制 —— 见 DSH invariant.ts:60
    trace = fresh_trace()
    _apply(trace, _event(0, "turn/start", {"turn": 1}))
    try:
        _apply(trace, _event(0, "user/message", {"id": "x"}))
        raise AssertionError("duplicate seq accepted")
    except ValueError:
        pass
    _apply(trace, _event(5, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}))  # 跳号合法


def test_turn_start_requires_closed_turn():
    trace = fresh_trace()
    _apply(trace, _event(0, "turn/start", {"turn": 1}))
    try:
        _apply(trace, _event(1, "turn/start", {"turn": 2}))
        raise AssertionError("nested turn accepted")
    except ValueError:
        pass
    # 编号必须接续
    trace2 = fresh_trace()
    try:
        _apply(trace2, _event(0, "turn/start", {"turn": 5}))
        raise AssertionError("non-contiguous turn number accepted")
    except ValueError:
        pass


def test_turn_end_must_match_and_close_steps():
    trace = fresh_trace()
    events = _valid_turn(1)
    for event in events[:3]:  # 到 step/start 为止
        _apply(trace, event)
    try:
        _apply(trace, _event(3, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}))
        raise AssertionError("turn/end with open step accepted")
    except ValueError:
        pass
    # 编号不符
    trace2 = fresh_trace()
    _apply(trace2, _event(0, "turn/start", {"turn": 1}))
    try:
        _apply(trace2, _event(1, "turn/end", {"turn": 2, "reason": {"kind": "completed"}}))
        raise AssertionError("mismatched turn/end accepted")
    except ValueError:
        pass


def test_step_scoped_events_require_open_step():
    trace = fresh_trace()
    _apply(trace, _event(0, "turn/start", {"turn": 1}))
    for type_, data in (
        ("assistant/chunk", {"turn": 1, "step": 1, "chunk": {"type": "text-delta", "index": 0, "text": "x"}}),
        ("assistant/message", {"turn": 1, "step": 1, "message": {"id": "a"}}),
        ("tool/call", {"turn": 1, "step": 1, "callId": "c1", "name": "n", "arguments": "{}"}),
    ):
        try:
            _apply(trace, _event(1, type_, data))
            raise AssertionError(f"{type_} without open step accepted")
        except ValueError:
            pass


def test_step_number_contiguity():
    trace = fresh_trace()
    for event in _valid_turn(1)[:3]:
        _apply(trace, event)
    try:
        _apply(trace, _event(3, "step/start", {"turn": 1, "step": 2}))
        raise AssertionError("non-contiguous step accepted")
    except ValueError:
        pass


def test_tool_result_requires_prior_call():
    trace = fresh_trace()
    for event in _valid_turn(1)[:4]:  # 到 assistant/message,无 tool/call
        _apply(trace, event)
    try:
        _apply(trace, _event(4, "tool/result", {
            "turn": 1, "step": 1,
            "message": {"role": "user", "id": "t1", "source": {"kind": "tool", "callId": "ghost"},
                        "content": [{"type": "tool-result", "toolCallId": "ghost", "content": []}]},
        }, surfaceOp="append"))
        raise AssertionError("unpaired tool/result accepted")
    except ValueError:
        pass


def test_tool_result_call_id_mismatch():
    trace = fresh_trace()
    for event in _valid_turn(1)[:5]:  # 到 tool/call c1
        _apply(trace, event)
    try:
        _apply(trace, _event(5, "tool/result", {
            "turn": 1, "step": 1,
            "message": {"role": "user", "id": "t1", "source": {"kind": "tool", "callId": "c2"},
                        "content": [{"type": "tool-result", "toolCallId": "c2", "content": []}]},
        }, surfaceOp="append"))
        raise AssertionError("mismatched tool/result accepted")
    except ValueError:
        pass


def test_synthetic_tool_not_started_is_exempt():
    # 崩溃恢复合成的事件:无对应 tool/call,但带 TOOL_NOT_STARTED 码 —— 豁免配对
    trace = fresh_trace()
    for event in _valid_turn(1)[:4]:
        _apply(trace, event)
    result = _event(4, "tool/result", {
        "turn": 1, "step": 1,
        "message": {"role": "user", "id": "t1", "source": {"kind": "tool", "callId": "ghost"},
                    "content": [{"type": "tool-result", "toolCallId": "ghost", "isError": True, "content": []}]},
        "error": {"name": "ToolNotStartedError", "code": "TOOL_NOT_STARTED"},
    }, surfaceOp="append")
    _apply(trace, result)  # 不抛


def test_surface_replacement_tool_result_needs_only_open_turn():
    # 表面替换(compact 重写)不是对原调用的执行 —— 只要求 turn 打开
    trace = fresh_trace()
    _apply(trace, _event(0, "turn/start", {"turn": 1}))
    _apply(trace, _event(1, "tool/result", {
        "turn": 1, "step": 1,
        "message": {"role": "user", "id": "t1", "source": {"kind": "tool", "callId": "c1"},
                    "content": [{"type": "tool-result", "toolCallId": "c1", "content": []}]},
    }, surfaceOp={"op": "replace", "start": 0, "end": 0}))
    # 但完全在 turn 外替换也不行
    trace2 = fresh_trace()
    try:
        _apply(trace2, _event(0, "tool/result", {
            "turn": 1, "step": 1,
            "message": {"role": "user", "id": "t1", "source": {"kind": "tool", "callId": "c1"},
                        "content": [{"type": "tool-result", "toolCallId": "c1", "content": []}]},
        }, surfaceOp={"op": "replace", "start": 0, "end": 0}))
        raise AssertionError("replacement outside any turn accepted")
    except ValueError:
        pass


def test_turn_enclosed_log_only_events():
    for type_ in ("todo/write", "request/header", "request/context"):
        trace = fresh_trace()
        try:
            _apply(trace, _event(0, type_, {"x": 1}))
            raise AssertionError(f"{type_} outside turn accepted")
        except ValueError:
            pass
        # turn 内合法
        trace = fresh_trace()
        _apply(trace, _event(0, "turn/start", {"turn": 1}))
        _apply(trace, _event(1, type_, {"x": 1}))


def test_end_seed_unconstrained():
    # end-seed 非平衡地在打开的 turn 内也合法
    trace = fresh_trace()
    _apply(trace, _event(0, "turn/start", {"turn": 1}))
    _apply(trace, _event(1, "session/end-seed", {}))


def test_user_message_outside_turn_ok():
    # 合成注入的用户消息可出现在 turn 外
    trace = fresh_trace()
    _apply(trace, _event(0, "user/message", {"id": "u", "content": []}))


def test_seed_trace_matches_incremental():
    events = _valid_turn(1)
    incremental = fresh_trace()
    for event in events:
        _apply(incremental, event)
    replayed = seed_trace(events)
    assert replayed.last_seq == incremental.last_seq
    assert replayed.open_turn == incremental.open_turn
    assert replayed.next_turn == incremental.next_turn
    assert replayed.next_step == incremental.next_step
    assert replayed.pending_calls == incremental.pending_calls
