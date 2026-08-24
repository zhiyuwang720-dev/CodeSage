"""表面折叠测试:三类事件的投影、替换/溯源/工具重写规则、增量管理。

照 DSH surface.spec.ts 的核心断言面:折叠顺序、替换遮蔽、
sourceEventSeqs 溯源完整性、tool/result 只改 content、增量路径
与全量重放一致性。
"""

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1]  # 包目录 core/session
sys.path.insert(0, str(_CORE))

from core.session.src.surface import (  # noqa: E402
    SurfaceManager,
    derive_event_message,
    fold_surface,
    is_append_surface_event,
    is_surface_event,
)


_SURFACE_TYPES = {"user/message", "assistant/message", "tool/result"}


def _ev(seq, type_, data, surface_op="append", source=None):
    e = {"type": type_, "seq": seq, "time": 1, "data": data}
    # 只有表面类型携带标记;非表面事件带标记是数据错误
    if surface_op is not None and type_ in _SURFACE_TYPES:
        e["surfaceOp"] = surface_op
    if source is not None:
        e["sourceEventSeqs"] = source
    return e


def _user(seq, id_="u1"):
    return _ev(seq, "user/message", {"role": "user", "id": id_, "source": {"kind": "human"}, "content": []})


def _assistant(seq, id_="a1"):
    return _ev(
        seq, "assistant/message",
        {"message": {"role": "assistant", "id": id_, "source": {"kind": "model", "provider": "p", "model": "m"}, "content": []}},
    )


def _tool_result(seq, call_id="c1", text="ok", surface_op="append", source=None, **overrides):
    data = {
        "message": {
            # id 由调用方派生而非 seq:替换副本必须保持消息身份不变
            "role": "user", "id": f"t{call_id}", "source": {"kind": "tool", "callId": call_id},
            "content": [{"type": "tool-result", "toolCallId": call_id, "content": [{"type": "text", "text": text}]}],
        }
    }
    data.update(overrides)
    return _ev(seq, "tool/result", data, surface_op=surface_op, source=source)


def test_projection_rules():
    # 三类事件各投影一条消息;空内容 assistant 不投影;非表面不投影
    assert derive_event_message(_user(0)) == _user(0)["data"]
    non_empty = _ev(1, "assistant/message", {"message": {
        "role": "assistant", "id": "a1", "source": {"kind": "model"}, "content": [{"type": "text", "text": "hi"}],
    }})
    assert derive_event_message(non_empty) == non_empty["data"]["message"]
    assert derive_event_message(_tool_result(2))["content"][0]["toolCallId"] == "c1"
    empty = _ev(1, "assistant/message", {"message": {"role": "assistant", "id": "a", "content": []}})
    assert derive_event_message(empty) is None
    assert derive_event_message(_ev(0, "turn/start", {"turn": 1})) is None
    assert derive_event_message(_ev(0, "assistant/chunk", {"chunk": {"type": "text-delta", "index": 0, "text": "x"}})) is None


def test_surface_eligibility_markers():
    assert is_surface_event(_user(0))
    assert is_append_surface_event(_user(0))
    # 非表面事件带标记是数据错误
    bad = _ev(0, "turn/start", {"turn": 1})
    bad["surfaceOp"] = "append"
    try:
        fold_surface([bad])
        raise AssertionError("non-eligible type with surfaceOp accepted")
    except ValueError:
        pass
    # 表面类型缺标记也是数据错误
    missing = _user(0)
    del missing["surfaceOp"]
    try:
        fold_surface([missing])
        raise AssertionError("surface-eligible type without marker accepted")
    except ValueError:
        pass


def test_fold_basic_order():
    events = [turn_start := _ev(0, "turn/start", {"turn": 1}), _user(1), _assistant(2), _tool_result(3)]
    result = fold_surface(events)
    assert result.nodes == [1, 2, 3]
    assert result.replacements == []


def test_fold_skips_non_surface():
    events = [
        _ev(0, "turn/start", {"turn": 1}),
        _user(1),
        _ev(2, "assistant/chunk", {"turn": 1, "step": 1, "chunk": {"type": "text-delta", "index": 0, "text": "x"}}),
        _assistant(3),
    ]
    result = fold_surface(events)
    assert result.nodes == [1, 3]


def test_replace_shadows_range():
    events = [
        _ev(0, "turn/start", {"turn": 1}),
        _user(1),
        _user(2, id_="u2"),
        _ev(3, "user/message", {"role": "user", "id": "u-new", "source": {"kind": "human"}, "content": []},
            surface_op={"op": "replace", "start": 1, "end": 2}, source=[1, 2]),
    ]
    result = fold_surface(events)
    assert result.nodes == [3]
    assert len(result.replacements) == 1
    assert result.replacements[0].shadowed_seqs == [1, 2]


def test_provenance_required_and_complete():
    # 替换必须完整声明被遮蔽节点
    events = [
        _ev(0, "turn/start", {"turn": 1}),
        _user(1),
        _user(2, id_="u2"),
    ]
    incomplete = _ev(3, "user/message", {"role": "user", "id": "u-new", "content": []},
                     surface_op={"op": "replace", "start": 1, "end": 2}, source=[1])
    try:
        fold_surface([*events, incomplete])
        raise AssertionError("incomplete provenance accepted")
    except ValueError:
        pass
    # 源事件必须早于替换事件
    future = _ev(3, "user/message", {"role": "user", "id": "u-new", "content": []},
                 surface_op={"op": "replace", "start": 1, "end": 2}, source=[1, 5])
    try:
        fold_surface([*events, future])
        raise AssertionError("future source seq accepted")
    except ValueError:
        pass
    # 重复源拒绝
    dup = _ev(3, "user/message", {"role": "user", "id": "u-new", "content": []},
              surface_op={"op": "replace", "start": 1, "end": 2}, source=[1, 1])
    try:
        fold_surface([*events, dup])
        raise AssertionError("duplicate source seq accepted")
    except ValueError:
        pass


def test_replacement_range_rules():
    base = [_ev(0, "turn/start", {"turn": 1}), _user(1)]
    # start 不在表面
    ghost = _ev(2, "user/message", {"role": "user", "id": "x", "content": []},
                surface_op={"op": "replace", "start": 9, "end": 1}, source=[9, 1])
    try:
        fold_surface([*base, ghost])
        raise AssertionError("ghost start accepted")
    except ValueError:
        pass
    # start 在 end 之后
    reversed_ = _ev(2, "user/message", {"role": "user", "id": "x", "content": []},
                    surface_op={"op": "replace", "start": 1, "end": 0}, source=[1, 0])
    try:
        fold_surface([_ev(0, "turn/start", {"turn": 1}), _user(0), _user(1), reversed_])
        raise AssertionError("reversed range accepted")
    except ValueError:
        pass


def test_tool_result_rewrite_only_content():
    base = [_ev(0, "turn/start", {"turn": 1}), _tool_result(1, text="old")]
    # 只改 content 合法
    rewrite = _tool_result(2, text="new", surface_op=None)
    rewrite["surfaceOp"] = {"op": "replace", "start": 1, "end": 1}
    rewrite["sourceEventSeqs"] = [1]
    result = fold_surface([*base, rewrite])
    assert result.nodes == [2]
    # 改消息 id 不行
    changed_id = _tool_result(2, text="new", surface_op=None)
    changed_id["data"]["message"]["id"] = "t-changed"
    changed_id["surfaceOp"] = {"op": "replace", "start": 1, "end": 1}
    changed_id["sourceEventSeqs"] = [1]
    try:
        fold_surface([*base, changed_id])
        raise AssertionError("id change accepted")
    except ValueError:
        pass
    # 改 source(callId)不行
    changed_source = _tool_result(2, text="new", surface_op=None)
    changed_source["data"]["message"]["source"] = {"kind": "tool", "callId": "c-other"}
    changed_source["data"]["message"]["content"][0]["toolCallId"] = "c-other"
    changed_source["surfaceOp"] = {"op": "replace", "start": 1, "end": 1}
    changed_source["sourceEventSeqs"] = [1]
    try:
        fold_surface([*base, changed_source])
        raise AssertionError("source change accepted")
    except ValueError:
        pass
    # 遮蔽两个节点不行
    two = [_ev(0, "turn/start", {"turn": 1}), _tool_result(1), _user(2)]
    wide = _ev(3, "tool/result", {
        "message": {"role": "user", "id": "t3", "source": {"kind": "tool", "callId": "c1"},
                    "content": [{"type": "tool-result", "toolCallId": "c1", "content": []}]},
    }, surface_op={"op": "replace", "start": 1, "end": 2}, source=[1, 2])
    try:
        fold_surface([*two, wide])
        raise AssertionError("multi-node tool rewrite accepted")
    except ValueError:
        pass
    # tool/result 替换必须命中当前的 tool/result:目标是 user/message 时拒绝
    # (注意:反向 —— user/message 替换 tool/result —— 合法,规则只约束
    # 替换方是 tool/result 的情况,见 DSH assertToolResultRewrite)
    not_result_target = _tool_result(2, text="new", surface_op=None)
    not_result_target["surfaceOp"] = {"op": "replace", "start": 1, "end": 1}
    not_result_target["sourceEventSeqs"] = [1]
    try:
        fold_surface([_ev(0, "turn/start", {"turn": 1}), _user(1), not_result_target])
        raise AssertionError("non-result target accepted")
    except ValueError:
        pass


def test_seq_contiguity_required():
    events = [_ev(0, "turn/start", {"turn": 1}), _user(5)]
    try:
        fold_surface(events)
        raise AssertionError("non-contiguous seq accepted")
    except ValueError:
        pass


def test_surface_manager_incremental_matches_full_fold():
    log: list[dict] = []
    manager = SurfaceManager(log)
    events = [
        _ev(0, "turn/start", {"turn": 1}),
        _user(1),
        _assistant(2),
        _ev(3, "user/message", {"role": "user", "id": "u-new", "content": []},
            surface_op={"op": "replace", "start": 1, "end": 1}, source=[1]),
    ]
    for event in events:
        manager.validate_next(event)  # 先校验
        log.append(event)  # 再接纳
        manager.nodes  # 触碰即推进增量
    full = fold_surface(events)
    assert manager.nodes == full.nodes
    assert manager.replace_generation == 1


def test_surface_manager_rejects_bad_candidate():
    log: list[dict] = [_ev(0, "turn/start", {"turn": 1}), _user(1)]
    manager = SurfaceManager(log)
    bad = _ev(2, "user/message", {"role": "user", "id": "x", "content": []},
              surface_op={"op": "replace", "start": 9, "end": 9}, source=[9])
    try:
        manager.validate_next(bad)
        raise AssertionError("invalid candidate accepted by validate_next")
    except ValueError:
        pass
