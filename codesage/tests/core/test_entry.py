"""Typed session entry tests (phase 12 S1): seven entry types, parent chain,
lane pointer advancement, legacy compat, corruption tolerance."""

import json

from codesage.ai import ContentBlock, Usage
from codesage.core import (
    Session,
    SessionEntry,
    assistant_message,
    make_bookmark_entry,
    make_branch_summary_entry,
    make_lane_entry,
    make_meta_entry,
    make_model_change_entry,
    make_operation_entry,
    parse_entry,
    user_message,
)

_NEW_FORMAT_KEYS = ("type", "uuid", "timestamp", "parent")


def _roundtrip(entry: SessionEntry, prev: str | None = None) -> SessionEntry:
    parsed = parse_entry(json.loads(entry.to_json()), prev)
    assert parsed is not None
    return parsed


def test_message_entry_roundtrip(tmp_path):
    msg = user_message("你好")
    entry = _roundtrip(SessionEntry(type="message", uuid=msg.uuid, timestamp=msg.timestamp,
                                    parent="prev-uuid", data=msg.to_dict()))
    assert entry.type == "message"
    assert entry.uuid == msg.uuid
    assert entry.parent == "prev-uuid"
    restored = entry.as_message()
    assert restored.content == "你好"
    assert restored.uuid == msg.uuid


def test_message_entry_with_blocks_roundtrip(tmp_path):
    msg = assistant_message(
        [ContentBlock(type="tool_use", id="t1", name="Read", input={"path": "/x"})],
        usage=Usage(input_tokens=1, output_tokens=2, total_tokens=3),
    )
    restored = _roundtrip(SessionEntry(type="message", uuid=msg.uuid, timestamp=msg.timestamp,
                                       parent=None, data=msg.to_dict())).as_message()
    assert restored.content[0].name == "Read"
    assert restored.content[0].input == {"path": "/x"}
    assert restored.usage.total_tokens == 3


def test_all_entry_types_roundtrip(tmp_path):
    cases = [
        make_lane_entry("main", "m1"),
        make_bookmark_entry("auth-fix", "m3"),
        make_branch_summary_entry("摘要文本", "m3"),
        make_operation_entry("tool_started", tool="Bash", args_summary="npm test"),
        make_model_change_entry("sonnet", from_="main"),
        make_meta_entry(model="main", show_thinking=False, cwd="E:/Mac/CodeSage"),
    ]
    for entry in cases:
        parsed = _roundtrip(entry)
        assert parsed.type == entry.type
        assert parsed.uuid == entry.uuid
        assert parsed.timestamp == entry.timestamp
        assert parsed.parent is None  # 应用状态 entry 无 parent 链
        assert parsed.data == entry.data
        assert parsed.as_message() is None


def test_lane_entry_carries_name_and_leaf(tmp_path):
    lane = _roundtrip(make_lane_entry("main-1", "m4"))
    assert lane.data == {"name": "main-1", "leaf": "m4"}


def test_legacy_line_parsed_as_message(tmp_path):
    # 无 type 键的 04 纯消息行 → 推导为 message;parent = 上一行消息 uuid
    first = parse_entry({"role": "user", "content": "hi", "uuid": "u1"}, None)
    assert first.type == "message"
    assert first.parent is None
    second = parse_entry({"role": "user", "content": "again", "uuid": "u2"}, first.uuid)
    assert second.parent == "u1"
    assert second.as_message().content == "again"


def test_legacy_file_loads_like_04(tmp_path):
    session = Session("s1", tmp_path)
    with open(session.path, "w", encoding="utf-8") as f:  # 手写 04 纯消息文件
        f.write(json.dumps({"role": "user", "content": "a", "uuid": "u1"}) + "\n")
        f.write(json.dumps({"role": "assistant", "content": "b", "uuid": "u2"}) + "\n")
    assert [m.content for m in session.load()] == ["a", "b"]


def test_legacy_file_then_new_append_chains(tmp_path):
    # --continue 路径:load() 重建游标后再 append,新消息挂旧链末尾
    session = Session("s1", tmp_path)
    with open(session.path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "a", "uuid": "u1"}) + "\n")
    assert session.load()[0].content == "a"
    session.append(user_message("b"))
    assert [m.content for m in session.load()] == ["a", "b"]


def test_lane_pointer_advancement_per_append(tmp_path):
    """§3.4 写死设计:每条消息后跟一条同名校验 lane 指针(leaf=新 uuid)。"""
    session = Session("s1", tmp_path)
    session.append(user_message("a"))
    session.append(user_message("b"))
    lines = [json.loads(line) for line in open(session.path, encoding="utf-8")]
    assert [line["type"] for line in lines] == ["message", "lane", "message", "lane"]
    lane_entries = [line for line in lines if line["type"] == "lane"]
    assert all(line["name"] == "main" for line in lane_entries)
    # 最后一条 lane entry 的 leaf = 最新消息 uuid(活跃 lane 恒指向最新消息)
    assert lane_entries[-1]["leaf"] == lines[-2]["uuid"]
    # 消息 parent 链连续,根为 None
    assert [line["parent"] for line in lines if line["type"] == "message"] == [None, lines[0]["uuid"]]


def test_append_returns_entry_and_04_api_stays(tmp_path):
    session = Session("s1", tmp_path)
    entry = session.append_message(user_message("x"))
    assert isinstance(entry, SessionEntry)
    assert entry.type == "message"
    assert session.append(user_message("y")) is None  # 04 append 语义:无返回值


def test_parent_missing_derived_from_previous(tmp_path):
    # 新格式 message 缺 parent → 推导为上一行消息 uuid(§3.3 缺省推导)
    prev = parse_entry({"type": "message", "role": "user", "content": "a", "uuid": "u1",
                        "timestamp": "t", "parent": None}, None)
    entry = parse_entry({"type": "message", "role": "user", "content": "b", "uuid": "u2",
                         "timestamp": "t"}, prev.uuid)
    assert entry.parent == "u1"


def test_unknown_type_and_unknown_role_skipped(tmp_path):
    assert parse_entry({"type": "bogus", "uuid": "x"}, None) is None
    assert parse_entry({"role": "system", "content": "bogus"}, None) is None
    session = Session("s1", tmp_path)
    session.append(user_message("good"))
    with open(session.path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "bogus", "uuid": "x"}) + "\n")
        f.write(json.dumps({"role": "system", "content": "bogus"}) + "\n")
        f.write("{not json}\n")
    session.append(user_message("still good"))
    assert [m.content for m in session.load()] == ["good", "still good"]


def test_dangling_lane_pointer_falls_back(tmp_path):
    """R4 兜底:最后一条 lane 指针悬空(损坏)→ 退回最后一条消息。"""
    session = Session("s1", tmp_path)
    session.append(user_message("a"))
    with open(session.path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "lane", "uuid": "l2", "timestamp": "t",
                            "name": "main", "leaf": "ghost"}) + "\n")
    assert [m.content for m in session.load()] == ["a"]


def test_malformed_lane_line_falls_back_to_previous_lane(tmp_path):
    """缺字段 lane 行(语义损坏)与合法 lane 行混合:load 不炸,退回上一个合法 lane。"""
    session = Session("s1", tmp_path)
    session.append(user_message("a"))
    with open(session.path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "lane", "uuid": "bad", "timestamp": "t",
                            "name": "main"}) + "\n")  # 缺 leaf
    assert [m.content for m in session.load()] == ["a"]
    session.append(user_message("b"))
    lines = [json.loads(line) for line in open(session.path, encoding="utf-8")]
    messages = [line for line in lines if line["type"] == "message"]
    assert messages[-1]["parent"] == messages[-2]["uuid"]


def test_cycle_in_parent_chain_terminates(tmp_path):
    """手写坏文件成环 → load 有界返回,不死循环。"""
    session = Session("s1", tmp_path)
    with open(session.path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "message", "role": "user", "content": "loop",
                            "uuid": "u1", "timestamp": "t", "parent": "u1"}) + "\n")
    assert [m.content for m in session.load()] == ["loop"]


def test_cursor_rebuilt_on_load(tmp_path):
    """load 重建游标:重开会话后 append 挂活跃 lane 最新消息。"""
    session = Session("s1", tmp_path)
    session.append(user_message("a"))
    session.append(user_message("b"))
    reloaded = Session("s1", tmp_path)
    reloaded.load()
    reloaded.append(user_message("c"))
    lines = [json.loads(line) for line in open(session.path, encoding="utf-8")]
    messages = [line for line in lines if line["type"] == "message"]
    assert messages[-1]["parent"] == messages[-2]["uuid"]
