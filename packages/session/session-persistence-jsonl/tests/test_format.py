"""format 层单测:路径净化、header 行(反)序列化、扫描边界。

DSH jsonl.spec.ts 中纯格式断言面的等价覆盖:encodeSegment 的单射
性与遍历中和、projectKey 规范化与长度有界、header 行类型守卫与
未来格式版本拒绝、scan_log 的撕裂尾/空洞/损坏行边界语义
(已提交前缀永不静默当完整历史读)。
"""

import sys
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[1]  # 本包目录(session-persistence-jsonl)
_PACKAGES = Path(__file__).resolve().parents[3]  # packages/
_ROOT = Path(__file__).resolve().parents[4]  # 仓库根
sys.path.insert(0, str(_PKG))
sys.path.insert(0, str(_PACKAGES))
sys.path.insert(0, str(_ROOT / "cordis-py"))

from core.session import SESSION_FORMAT_VERSION  # noqa: E402

from session.session_persistence_jsonl.src.format import (  # noqa: E402
    SessionLogScanner,
    encode_segment,
    from_header_line,
    is_header_line,
    parse_header_meta,
    project_key,
    scan_log,
    to_header_line,
)
from session.session_persistence_jsonl.src.index import JsonlSessionPersistence  # noqa: E402
from session.session_persistence import (  # noqa: E402
    SessionFormatUnsupportedError,
    sessionFormatVersionRefusal,
)


def _meta(id_, cwd="C:/work"):
    return {"id": id_, "cwd": cwd, "createdAt": 1, "version": SESSION_FORMAT_VERSION}


def _ev(seq, type_, **data):
    return {"type": type_, "seq": seq, "time": 1, "data": data}


def _header_line(meta):
    return to_header_line(meta)


def _log_bytes(meta, events):
    lines = [json_dumps(_header_line(meta))]
    for event in events:
        lines.append(json_dumps(event))
    return ("\n".join(lines) + "\n").encode("utf-8")


def json_dumps(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


# ---- encodeSegment / projectKey ----


def test_encode_segment_neutralizes_traversal_and_separators():
    # 分隔符一律转义为 ~XXXX:编码结果永不包含原始分隔符。
    # '.' 是安全字符(整段 "." / ".." 有特判),遍历由分隔符中和保证。
    for raw in ("../evil", "/abs/path", "a/b", "a\\b", "a:b", "a\x00b", "~"):
        encoded = encode_segment(raw)
        assert "/" not in encoded
        assert "\\" not in encoded
        assert ":" not in encoded
        assert "\x00" not in encoded
    assert "/" not in encode_segment("..")


def test_encode_segment_is_injective():
    # 单射:一批含 lone surrogate 的字符串两两编码不同;且同串幂等
    samples = [
        "hello",
        "a~b",
        "a-b_c.d",
        "\ud800\udc00",  # surrogate 对
        "\ud800",  # lone high surrogate
        "\udc00",  # lone low surrogate
        "a\ud800b",
        "~002E",
        "s" * 256,
        ".",
        "..",
        "C:/x\\y",
    ]
    encoded = [encode_segment(s) for s in samples]
    assert len(set(encoded)) == len(samples)
    for s, e in zip(samples, encoded):
        assert encode_segment(s) == e  # 幂等


def test_encode_segment_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        encode_segment("")


def test_encode_segment_dot_special_cases():
    assert encode_segment(".") == "~002E"
    assert encode_segment("..") == "~002E~002E"


def test_project_key_normalizes_paths():
    # 分隔符折叠为单个 '-' 并保持可读(保留大小写);盘符冒号同样折叠
    assert project_key("C:/Users/me/project") == "--C-Users-me-project--"
    assert project_key("C:\\Users\\me\\project") == "--C-Users-me-project--"
    # 分隔符连续 run 折叠成一个 '-'
    assert project_key("a//b///c") == "--a-b-c--"
    # 前导分隔符剥掉;全分隔符回退 'root'
    assert project_key("/abs/path") == "--abs-path--"
    assert project_key("///") == "--root--"
    # 不安全字符走 ~XXXX
    assert "~" in project_key("a b")


def test_project_key_bounded_length():
    key = project_key("C:/" + "x" * 1000)
    assert len(key) == 255  # -- + slug[:251] + --


# ---- header 行守卫 ----


def test_header_line_round_trips_agent_preset():
    meta = dict(_meta("s"), agentPreset="expert")
    line = to_header_line(meta)
    assert is_header_line(line)
    parsed = from_header_line(line)
    assert parsed["agentPreset"] == "expert"


def test_header_rejects_negative_zero_created_at():
    line = _header_line(_meta("s"))
    line["createdAt"] = -0.0
    assert not is_header_line(line)


def test_header_rejects_negative_zero_delegation_depth():
    line = _header_line(_meta("s"))
    line["delegationDepth"] = -0.0
    assert not is_header_line(line)


def test_header_rejects_non_string_agent_preset():
    line = _header_line(_meta("s"))
    line["agentPreset"] = 42
    assert not is_header_line(line)


def test_header_rejects_non_object_line():
    # 非对象首行是损坏(ValueError),不是格式拒绝(SessionFormatUnsupportedError)
    with pytest.raises(ValueError, match="corrupt session log: first line is not a session header"):
        scan_log(b"[]\n")


def test_foreign_format_version_is_refusal_not_corruption():
    import json

    line = _header_line(_meta("s"))
    line["version"] = SESSION_FORMAT_VERSION + 100
    buffer = (json.dumps(line) + "\n").encode("utf-8")
    with pytest.raises(SessionFormatUnsupportedError) as info:
        scan_log(buffer)
    assert sessionFormatVersionRefusal("s", SESSION_FORMAT_VERSION + 100) in str(info.value)


def test_foreign_version_names_stringified_non_string_id():
    import json

    line = _header_line(_meta("s"))
    line["version"] = SESSION_FORMAT_VERSION + 100
    line["id"] = 123  # 非字符串 id
    buffer = (json.dumps(line) + "\n").encode("utf-8")
    with pytest.raises(SessionFormatUnsupportedError) as info:
        scan_log(buffer)
    # 拒绝信息点名 stringify 后的 id
    assert "123" in str(info.value)


# ---- scan_log 边界 ----


def test_scan_header_only_log():
    result = scan_log(_log_bytes(_meta("s"), []))
    assert result["meta"]["id"] == "s"
    assert result["events"] == []
    assert result["committedBytes"] == len(
        (json_dumps(_header_line(_meta("s"))) + "\n").encode("utf-8")
    )


def test_scan_rejects_headerless_log():
    with pytest.raises(ValueError, match="empty or header-less session log"):
        scan_log(b"")
    with pytest.raises(ValueError, match="empty or header-less session log"):
        scan_log(b'{"type":"turn/start"}')  # 无换行 → 无完整 header 记录


def test_scan_rejects_corrupt_header_line():
    with pytest.raises(ValueError, match="corrupt session log: header line is not valid JSON"):
        scan_log(b"{not json}\n")


def test_scan_rejects_non_session_first_line():
    with pytest.raises(ValueError, match="corrupt session log: first line is not a session header"):
        scan_log(b'{"version": 0, "id": "s", "createdAt": 1}\n')


def test_scan_requires_exactly_one_newline_terminated_header():
    meta = _meta("s")
    # 事件行之后不能把 header 记录吞进 header 区:首个换行后的内容
    # 全按事件行扫描
    events = [_ev(0, "turn/start", turn=1), _ev(1, "turn/end", turn=1, reason={"kind": "completed"})]
    result = scan_log(_log_bytes(meta, events))
    assert [e["seq"] for e in result["events"]] == [0, 1]


def test_scan_tolerates_seq_gap_after_last_turn_end():
    # 撕裂尾:最后一个 turn/end 之后、gap 行不含 turn/end → 保留前缀
    events = [_ev(0, "turn/start", turn=1), _ev(1, "turn/end", turn=1, reason={"kind": "completed"}), _ev(2, "turn/start", turn=2), _ev(4, "step/start", turn=2, step=1)]
    result = scan_log(_log_bytes(_meta("s"), events))
    # gap 行(seq4)本身被丢弃;其前的已解码行保留(seq2 turn/start 是
    # 完整行,已提交前缀的一部分)
    assert [e["seq"] for e in result["events"]] == [0, 1, 2]


def test_scan_rejects_seq_gap_before_committed_turn_end():
    # 已提交前缀损坏:turn/end 之前出现空洞必须拒绝
    events = [_ev(0, "turn/start", turn=1), _ev(2, "turn/end", turn=1, reason={"kind": "completed"})]
    with pytest.raises(ValueError, match="seq gap in committed region"):
        scan_log(_log_bytes(_meta("s"), events))


def test_scan_rejects_corrupt_line_before_committed_turn_end():
    import json

    lines = [json_dumps(_header_line(_meta("s"))), json_dumps(_ev(0, "turn/start", turn=1)), "{corrupt", json_dumps(_ev(2, "turn/end", turn=1, reason={"kind": "completed"}))]
    buffer = ("\n".join(lines) + "\n").encode("utf-8")
    with pytest.raises(ValueError, match="unparsable committed event at line 2"):
        scan_log(buffer)


def test_scan_tolerates_corrupt_line_after_last_turn_end():
    import json

    lines = [json_dumps(_header_line(_meta("s"))), json_dumps(_ev(0, "turn/start", turn=1)), json_dumps(_ev(1, "turn/end", turn=1, reason={"kind": "completed"})), "{corrupt"]
    buffer = ("\n".join(lines) + "\n").encode("utf-8")
    result = scan_log(buffer)
    assert [e["seq"] for e in result["events"]] == [0, 1]


def test_scanner_incremental_across_fragment_boundaries():
    """增量扫描:完整记录跨多个 write 块,解码器结果不受碎片切割影响。"""
    events = [_ev(0, "turn/start", turn=1), _ev(1, "turn/end", turn=1, reason={"kind": "completed"})]
    buffer = _log_bytes(_meta("s"), events)
    header_end = buffer.find(b"\n") + 1
    scanner = SessionLogScanner(buffer[:header_end])
    # 一字节一字节喂:每条完整记录跨碎片
    for i in range(header_end, len(buffer)):
        scanner.write(buffer[i : i + 1])
    result = scanner.finish()
    assert [e["seq"] for e in result["events"]] == [0, 1]
    assert result["committedBytes"] == len(buffer)


def test_scan_packed_row_advances_seq_cursor_by_whole_run():
    from session.session_persistence_jsonl.src.format import event_lines

    # assistant/chunk 增量段连续(text-delta 白名单)→ 一条 text-chunks 行承载整个 run
    events = [
        _ev(i, "assistant/chunk", turn=1, step=1, chunk={"type": "text-delta", "index": 0, "text": "abc"[i]})
        for i in range(3)
    ]
    packed = event_lines(events, True)
    assert packed.count("\n") == 0  # 一条行
    import json

    lines = [json_dumps(_header_line(_meta("s"))), packed]
    buffer = ("\n".join(lines) + "\n").encode("utf-8")
    result = scan_log(buffer)
    # 一条打包行承载整个 run:解码回 3 个事件,游标推进整 run
    assert [e["seq"] for e in result["events"]] == [0, 1, 2]


def test_parse_header_meta_returns_none_for_non_header():
    assert parse_header_meta("not json") is None
    assert parse_header_meta('{"type":"turn/start"}') is None
    assert parse_header_meta(json_dumps(_header_line(_meta("s"))))["id"] == "s"


def test_event_lines_unpacked_is_byte_identical_layout():
    from session.session_persistence_jsonl.src.format import event_lines

    events = [_ev(0, "turn/start", turn=1)]
    unpacked = event_lines(events, False)
    assert unpacked == json_dumps(events[0])
