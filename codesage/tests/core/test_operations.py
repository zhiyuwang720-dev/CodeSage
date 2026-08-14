"""Operation log + meta/model_change + branch_summary tests (phase 12 S3, spec §7-8)."""

import json

from codesage.core import (
    Session,
    SessionEntry,
    find_open_operations,
    parse_entry,
    user_message,
)


def _entries(session: Session) -> list[SessionEntry]:
    """镜像 Session._read:解析文件行 → entry 列表(测试读端)。"""
    entries, last = [], None
    with open(session.path, encoding="utf-8") as f:
        for line in f:
            entry = parse_entry(json.loads(line), last)
            if entry is not None:
                entries.append(entry)
                if entry.type == "message":
                    last = entry.uuid
    return entries


def test_append_operation_entry_shape(tmp_path):
    """append_operation 落盘 operation entry:kind/tool/args_summary 字段齐备。"""
    session = Session("s1", tmp_path)
    entry = session.append_operation("tool_started", tool="Bash", args_summary='{"cmd": "npm test"}')
    assert isinstance(entry, SessionEntry)
    assert entry.type == "operation"
    assert entry.data == {
        "kind": "tool_started",
        "tool": "Bash",
        "args_summary": '{"cmd": "npm test"}',
    }
    assert json.loads(open(session.path, encoding="utf-8").read().splitlines()[-1])[
        "type"
    ] == "operation"


def test_append_operation_truncates_args_summary(tmp_path):
    """args_summary 截断 200 字符(§7.1,不进模型上下文)。"""
    session = Session("s1", tmp_path)
    entry = session.append_operation("tool_started", tool="Bash", args_summary="x" * 500)
    assert len(entry.data["args_summary"]) == 200
    assert entry.data["args_summary"] == "x" * 200
    # 未超限不截断
    entry2 = session.append_operation("step_attempt", tool="Bash", args_summary="short")
    assert entry2.data["args_summary"] == "short"


def test_find_open_operations_hit_when_trailing_operation(tmp_path):
    """末尾是 operation(后跟 lane 指针) → 该操作未完成。"""
    session = Session("s1", tmp_path)
    session.append(user_message("a"))
    op = session.append_operation("tool_started", tool="Bash", args_summary="npm run deploy")
    assert [e.uuid for e in find_open_operations(_entries(session))] == [op.uuid]


def test_find_open_operations_miss_when_trailing_message(tmp_path):
    """末尾是消息 → 无未完成操作(该消息即后继消息)。"""
    session = Session("s1", tmp_path)
    session.append_operation("tool_started", tool="Bash")
    session.append(user_message("a"))
    assert find_open_operations(_entries(session)) == []


def test_find_open_operations_hit_behind_app_state_entries(tmp_path):
    """operation 后只有应用状态 entry(lane/bookmark/branch_summary/meta/
    model_change)而无可继续的消息 → 仍视为未完成。"""
    session = Session("s1", tmp_path)
    op = session.append_operation("tool_started", tool="Read")
    session.append_bookmark("some-entry", "b1")
    session.append_branch_summary("sum", "leaf-1")
    session.append_meta(model="main", cwd=".")
    session.append_model_change(to="sonnet", from_="main")
    assert [e.uuid for e in find_open_operations(_entries(session))] == [op.uuid]


def test_find_open_operations_multiple_trailing_ops(tmp_path):
    """同一段内多个 operation(并发批)都未完成;中间有消息则闭合。"""
    session = Session("s1", tmp_path)
    session.append_operation("tool_started", tool="Read")
    session.append_operation("tool_started", tool="Grep")
    session.append(user_message("a"))
    session.append_operation("tool_started", tool="Bash")
    ops = find_open_operations(_entries(session))
    assert [e.data["tool"] for e in ops] == ["Bash"]  # 只有最后一段


def test_append_meta_and_merge_latter_wins(tmp_path):
    """meta 追加 + Session.meta 合并多个 meta entry、后者胜(含 title,§8.3)。"""
    session = Session("s1", tmp_path)
    session.append_meta(model="main", show_thinking=False, cwd="/tmp", session_id="s1")
    session.append_meta(title="修复 auth 登录 bug")
    meta = session.meta
    assert meta is not None
    assert meta["model"] == "main"
    assert meta["show_thinking"] is False
    assert meta["cwd"] == "/tmp"
    assert meta["session_id"] == "s1"
    assert meta["title"] == "修复 auth 登录 bug"  # 第二个 entry 追加,读端后者胜
    assert json.loads(open(session.path, encoding="utf-8").read().splitlines()[0])["type"] == "meta"


def test_meta_none_on_empty_or_meta_less_session(tmp_path):
    assert Session("s1", tmp_path).meta is None
    session = Session("s2", tmp_path)
    session.append(user_message("a"))
    assert session.meta is None


def test_append_model_change(tmp_path):
    """model_change 追加:to/from 指针名落盘(§8.2)。"""
    session = Session("s1", tmp_path)
    entry = session.append_model_change(to="sonnet", from_="main")
    assert entry.type == "model_change"
    assert entry.data == {"to": "sonnet", "from": "main"}
    assert json.loads(open(session.path, encoding="utf-8").read().splitlines()[-1])[
        "type"
    ] == "model_change"


def test_append_branch_summary(tmp_path):
    """branch_summary 追加:摘要文本 + leaf(压缩切点锚点消息 uuid,§4.5)。"""
    session = Session("s1", tmp_path)
    session.append(user_message("a"))
    leaf = session.load()[0].uuid
    entry = session.append_branch_summary("compacted summary", leaf)
    assert entry.type == "branch_summary"
    assert entry.data == {"content": "compacted summary", "leaf": leaf}
    assert json.loads(open(session.path, encoding="utf-8").read().splitlines()[-1])[
        "type"
    ] == "branch_summary"
    # 应用状态 entry 不出现在 load() 线性视图(§3.2 PI-10)
    assert [m.content for m in session.load()] == ["a"]
