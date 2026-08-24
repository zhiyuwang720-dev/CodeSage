"""块行打包测试:跑识别/打包/展开往返、白名单保真、畸形拒绝。

照 DSH chunk-rows.spec.ts 的核心断言面:MIN_RUN 门槛、同块同种类
连续才打包、工具调用 name 均匀性、解码后与原始事件逐字相等、
畸形行抛错而非静默丢跑。
"""

import copy
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1]  # 包目录 core/session
sys.path.insert(0, str(_CORE))

from core.session.src.chunk_rows import (  # noqa: E402
    MIN_RUN,
    decode_storage_record,
    pack_chunk_runs,
)


def _chunk(seq, kind, index=0, text=None, id_=None, name=None, args=None, turn=1, step=1, time=None):
    chunk = {"type": kind, "index": index}
    if text is not None:
        chunk["text"] = text
    if id_ is not None:
        chunk["id"] = id_
    if name is not None:
        chunk["name"] = name
    if args is not None:
        chunk["argumentsDelta"] = args
    return {"type": "assistant/chunk", "seq": seq, "time": time if time is not None else seq, "data": {"turn": turn, "step": step, "chunk": chunk}}


def test_below_min_run_stays_verbatim():
    run = [_chunk(0, "text-delta", text="a"), _chunk(1, "text-delta", text="b")]
    records = pack_chunk_runs(run)
    assert records == run  # 2 < MIN_RUN:原样


def test_pack_round_trip_text():
    run = [_chunk(i, "text-delta", text=chr(97 + i)) for i in range(MIN_RUN + 2)]
    records = pack_chunk_runs(run)
    assert len(records) == 1
    row = records[0]
    assert row["type"] == "text-chunks"
    assert row["seq0"] == run[0]["seq"]
    assert row["data"]["texts"] == ["a", "b", "c", "d", "e"]
    assert row["data"]["dt"] == [1, 1, 1, 1]
    decoded = decode_storage_record(row)
    assert decoded == run  # 逐字往返


def test_pack_round_trip_reasoning():
    run = [_chunk(i, "reasoning-delta", text=f"r{i}") for i in range(MIN_RUN)]
    row = pack_chunk_runs(run)[0]
    assert row["type"] == "reasoning-chunks"
    assert decode_storage_record(row) == run


def test_pack_round_trip_tool_call():
    run = [_chunk(i, "tool-call-delta", id_="c1", name="read", args=f"{{}}") for i in range(MIN_RUN)]
    row = pack_chunk_runs(run)[0]
    assert row["type"] == "tool-call-chunks"
    assert row["data"]["id"] == "c1"
    assert row["data"]["name"] == "read"
    assert decode_storage_record(row) == run


def test_mixed_kind_splits_runs():
    events = [
        _chunk(0, "text-delta", text="a"),
        _chunk(1, "text-delta", text="b"),
        _chunk(2, "text-delta", text="c"),
        _chunk(3, "reasoning-delta", text="r"),  # 种类切换
        _chunk(4, "reasoning-delta", text="r2"),
        _chunk(5, "reasoning-delta", text="r3"),
    ]
    records = pack_chunk_runs(events)
    assert [r["type"] for r in records] == ["text-chunks", "reasoning-chunks"]
    assert decode_storage_record(records[0]) + decode_storage_record(records[1]) == events


def test_block_index_breaks_run():
    events = [
        _chunk(0, "text-delta", text="a", index=0),
        _chunk(1, "text-delta", text="b", index=0),
        _chunk(2, "text-delta", text="c", index=0),
        _chunk(3, "text-delta", text="d", index=1),  # 块切换
        _chunk(4, "text-delta", text="e", index=1),
        _chunk(5, "text-delta", text="f", index=1),
    ]
    records = pack_chunk_runs(events)
    assert len(records) == 2
    assert decode_storage_record(records[0]) + decode_storage_record(records[1]) == events


def test_seq_gap_breaks_run():
    events = [_chunk(0, "text-delta", text="a"), _chunk(1, "text-delta", text="b"), _chunk(3, "text-delta", text="c")]  # seq 跳跃
    records = pack_chunk_runs(events)
    assert len(records) == 3  # 跑被切碎,且没有跑到 MIN_RUN → 全部原样
    assert decode_storage_record(records[0]) + decode_storage_record(records[1]) + decode_storage_record(records[2]) == events


def test_tool_call_name_uniformity():
    events = [
        _chunk(0, "tool-call-delta", id_="c1", args="a"),
        _chunk(1, "tool-call-delta", id_="c1", args="b"),
        _chunk(2, "tool-call-delta", id_="c1", args="c"),
        _chunk(3, "tool-call-delta", id_="c1", args="d"),
    ]
    # 有 name 的跑与无 name 的跑不能混
    with_name = copy.deepcopy(events)
    for e in with_name:
        e["data"]["chunk"]["name"] = "read"
    records = pack_chunk_runs(with_name)
    assert records[0]["data"].get("name") == "read"
    assert len(records) == 1
    mixed = [with_name[0], events[1], events[2], events[3]]
    records = pack_chunk_runs(mixed)
    assert len(records) == 2  # 名存在性不同 → 拆跑(深拷贝避免共享嵌套 chunk)


def test_non_packable_passes_through():
    events = [
        _chunk(0, "text-delta", text="a"),
        {"type": "turn/start", "seq": 1, "time": 1, "data": {"turn": 1}},  # 非 chunk
        _chunk(2, "finish", index=0),  # 未知块种类
    ]
    records = pack_chunk_runs(events)
    assert records == events  # 全部原样


def test_malformed_row_throws():
    # dt 元数不匹配
    bad_dt = {"type": "text-chunks", "seq0": 0, "time0": 0, "data": {"turn": 1, "step": 1, "index": 0, "dt": [1], "texts": ["a", "b", "c"]}}
    try:
        decode_storage_record(bad_dt)
        raise AssertionError("dt arity mismatch accepted")
    except ValueError:
        pass
    # 信封缺键
    bad_env = {"type": "text-chunks", "seq0": 0, "data": {"turn": 1, "step": 1, "index": 0, "dt": [1, 2], "texts": ["a", "b", "c"]}}
    try:
        decode_storage_record(bad_env)
        raise AssertionError("bad envelope accepted")
    except ValueError:
        pass
    # 空载荷
    bad_payload = {"type": "text-chunks", "seq0": 0, "time0": 0, "data": {"turn": 1, "step": 1, "index": 0, "dt": [], "texts": []}}
    try:
        decode_storage_record(bad_payload)
        raise AssertionError("empty payload accepted")
    except ValueError:
        pass


def test_non_row_values_pass_unvalidated():
    event = {"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}}
    assert decode_storage_record(event) == [event]
    assert decode_storage_record(None) == [None]


def test_negative_dt_gap_reconstructs():
    # 时间回拨:dt 为负也能精确重建
    run = [
        _chunk(0, "text-delta", text="a", time=100),
        _chunk(1, "text-delta", text="b", time=95),
        _chunk(2, "text-delta", text="c", time=90),
    ]
    decoded = decode_storage_record(pack_chunk_runs(run)[0])
    assert decoded == run
