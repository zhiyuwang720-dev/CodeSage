"""Tree view tests (phase 12 S2): parent-chain tree, lane parsing, linear
projection, bookmark/summary mounting, dangling fallbacks."""

import json

from codesage.core import (
    Session,
    SessionEntry,
    build_tree,
    linear_messages,
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


def _entries_from_lines(tmp_path, lines) -> list[SessionEntry]:
    """手写文件行(固定 uuid,便于构造分支/坏文件)→ entry 列表。"""
    session = Session("s1", tmp_path)
    with open(session.path, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(line) for line in lines) + "\n")
    return _entries(session)


def _m(uuid_: str, parent: str | None) -> dict:
    """手写 message 行(内容 = uuid,断言时自解释)。"""
    return {"role": "user", "content": uuid_, "uuid": uuid_, "parent": parent}


def test_single_chain_degrades_to_linear(tmp_path):
    """单链树退化:单根单链,linear_messages 与 04 load 一致。"""
    session = Session("s1", tmp_path)
    session.append(user_message("a"))
    session.append(user_message("b"))
    session.append(user_message("c"))
    entries = _entries(session)
    view = build_tree(entries)
    assert len(view.roots) == 1
    chain, cur = [], view.roots[0]
    while cur:
        chain.append(cur)
        cur = cur.children[0] if cur.children else None
    assert [n.entry.data["content"] for n in chain] == ["a", "b", "c"]
    assert list(view.nodes) == [e.uuid for e in entries if e.type == "message"]
    # lane 解析:活跃 lane main 的 leaf = 最新消息
    assert view.lanes == {"main": chain[-1].entry.uuid}
    assert view.active_lane == "main"
    assert [m.content for m in linear_messages(entries)] == ["a", "b", "c"]
    assert [m.content for m in linear_messages(entries)] == [
        m.content for m in session.load()
    ]


def test_legacy_file_linear_messages_matches_04_load(tmp_path):
    """旧格式(无 type 键)文件:单根单链;linear_messages 与 04 load() 逐条一致。"""
    session = Session("s1", tmp_path)
    with open(session.path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "a", "uuid": "u1"}) + "\n")
        f.write(json.dumps({"role": "assistant", "content": "b", "uuid": "u2"}) + "\n")
    entries = _entries(session)
    view = build_tree(entries)
    assert len(view.roots) == 1
    assert view.lanes == {}  # 无 lane entry → 无映射;活跃 lane 兜底 main
    assert view.active_lane == "main"
    assert view.active_leaf is None
    assert [m.content for m in linear_messages(entries)] == ["a", "b"]
    assert [m.content for m in linear_messages(entries)] == [
        m.content for m in session.load()
    ]


def test_multiple_roots_and_lanes(tmp_path):
    """多根(分支起点)+ lane 解析:活跃 lane = 最后一条 lane entry。

    §3.3 推导规则下 parent=None 只属于文件首行,第二根 = parent 指向不存在的
    消息(悬空链,树视图不丢节点,归入根)。
    """
    lines = [
        _m("u1", None),
        _m("u2", "u1"),
        {"type": "lane", "uuid": "l1", "timestamp": "t", "name": "main", "leaf": "u2"},
        _m("u5", "ghost"),  # 第二根:parent 悬空 → 独立链
        {"type": "lane", "uuid": "l2", "timestamp": "t", "name": "side", "leaf": "u5"},
        _m("u3", "u2"),
    ]
    entries = _entries_from_lines(tmp_path, lines)
    view = build_tree(entries)
    assert [n.entry.uuid for n in view.roots] == ["u1", "u5"]
    assert view.lanes == {"main": "u2", "side": "u5"}
    assert view.active_lane == "side"  # 最后一条 lane entry 胜
    assert view.active_leaf == "u5"
    assert [c.entry.uuid for c in view.roots[0].children] == ["u2"]
    assert [c.entry.uuid for c in view.nodes["u2"].children] == ["u3"]
    assert view.nodes["u5"].children == []
    # 线性视图随 lane 变化:活跃 lane 只含自己的链;main 的链止于其 leaf
    assert [m.content for m in linear_messages(entries)] == ["u5"]
    assert [m.content for m in linear_messages(entries, "main")] == ["u1", "u2"]


def test_same_name_lane_later_wins(tmp_path):
    """§3.4 指针推进:同名 lane 重复出现,后者胜(映射与活跃 lane 都是)。"""
    lines = [
        _m("u1", None),
        {"type": "lane", "uuid": "l1", "timestamp": "t", "name": "main", "leaf": "u1"},
        _m("u2", "u1"),
        {"type": "lane", "uuid": "l2", "timestamp": "t", "name": "main", "leaf": "u2"},
    ]
    entries = _entries_from_lines(tmp_path, lines)
    view = build_tree(entries)
    assert view.lanes == {"main": "u2"}
    assert view.active_lane == "main"
    assert [m.content for m in linear_messages(entries)] == ["u1", "u2"]


def test_only_message_entries_become_nodes(tmp_path):
    """类型筛选(树层):应用状态 entry 不进节点集,树视图只含 message。"""
    lines = [
        _m("u1", None),
        {"type": "lane", "uuid": "l1", "timestamp": "t", "name": "main", "leaf": "u1"},
        {"type": "bookmark", "uuid": "b1", "timestamp": "t", "name": "star", "entry": "u1"},
        {"type": "operation", "uuid": "o1", "timestamp": "t", "kind": "tool_started"},
    ]
    view = build_tree(_entries_from_lines(tmp_path, lines))
    assert list(view.nodes) == ["u1"]
    assert len(view.roots) == 1


def test_bookmark_mounts_to_target_node(tmp_path):
    lines = [
        _m("u1", None),
        _m("u2", "u1"),
        {"type": "bookmark", "uuid": "b1", "timestamp": "t", "name": "auth-fix", "entry": "u2"},
        {"type": "bookmark", "uuid": "b2", "timestamp": "t", "name": "ghost", "entry": "u9"},
    ]
    view = build_tree(_entries_from_lines(tmp_path, lines))
    assert [b.data["name"] for b in view.nodes["u2"].bookmarks] == ["auth-fix"]
    assert view.nodes["u1"].bookmarks == []
    # 悬空书签不挂载也不报错
    assert all(len(r.bookmarks) == 0 for r in view.roots)
    # 读端映射:消息 entry 本身未被修改
    assert "bookmarks" not in view.nodes["u2"].entry.data


def test_summary_mounts_by_leaf(tmp_path):
    lines = [
        _m("u1", None),
        _m("u2", "u1"),
        {"type": "branch_summary", "uuid": "s1", "timestamp": "t",
         "content": "摘要", "leaf": "u2"},
    ]
    view = build_tree(_entries_from_lines(tmp_path, lines))
    assert [s.data["content"] for s in view.nodes["u2"].summaries] == ["摘要"]
    assert view.nodes["u1"].summaries == []


def test_malformed_trailing_lane_skipped(tmp_path):
    """R4:缺字段 lane 行跳过,活跃 lane 退回上一个合法 lane。"""
    lines = [
        _m("u1", None),
        _m("u2", "u1"),
        {"type": "lane", "uuid": "l1", "timestamp": "t", "name": "main", "leaf": "u2"},
        {"type": "lane", "uuid": "l2", "timestamp": "t", "name": "main"},  # 缺 leaf
    ]
    entries = _entries_from_lines(tmp_path, lines)
    view = build_tree(entries)
    assert view.lanes == {"main": "u2"}
    assert view.active_lane == "main"
    assert [m.content for m in linear_messages(entries)] == ["u1", "u2"]


def test_dangling_lane_leaf_falls_back(tmp_path):
    """悬空 lane 指针(leaf 不在消息 uuid 集)→ 退回最后一条消息。"""
    session = Session("s1", tmp_path)
    session.append(user_message("a"))
    session.append(user_message("b"))
    with open(session.path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "lane", "uuid": "l3", "timestamp": "t",
                            "name": "ghost", "leaf": "nope"}) + "\n")
    entries = _entries(session)
    view = build_tree(entries)
    assert view.lanes["ghost"] == "nope"
    assert view.active_lane == "ghost"
    assert [m.content for m in linear_messages(entries)] == ["a", "b"]
    assert [m.content for m in linear_messages(entries, "ghost")] == ["a", "b"]


def test_unknown_lane_falls_back(tmp_path):
    """未知 lane 名 → 同样退回最后一条消息(单 lane 04 语义兜底)。"""
    session = Session("s1", tmp_path)
    session.append(user_message("a"))
    entries = _entries(session)
    assert [m.content for m in linear_messages(entries, "no-such-lane")] == ["a"]


def test_cycle_in_parent_chain_terminates(tmp_path):
    """手写坏文件成环 → linear_messages 有界返回,不死循环。"""
    lines = [
        {"role": "user", "content": "loop", "uuid": "u1", "parent": "u1"},
        {"type": "lane", "uuid": "l1", "timestamp": "t", "name": "main", "leaf": "u1"},
    ]
    assert [m.content for m in linear_messages(_entries_from_lines(tmp_path, lines))] == ["loop"]


def test_empty_entries(tmp_path):
    assert build_tree([]).roots == []
    assert build_tree([]).active_lane == "main"
    assert linear_messages([]) == []
