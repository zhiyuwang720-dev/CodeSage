"""Fork tests (phase 12 S2): branch lane entry, cursor reset, naming, no copy."""

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


def _messages(session: Session) -> list[SessionEntry]:
    return [e for e in _entries(session) if e.type == "message"]


def test_fork_appends_lane_entry_with_leaf(tmp_path):
    """fork 追加一条 lane entry:leaf = entry_id 本身(分支起点)。"""
    session = Session("s1", tmp_path)
    session.append(user_message("a"))
    session.append(user_message("b"))
    mid = session.load()[1].uuid
    lane = session.fork(mid)
    assert lane == "main-1"
    lines = [json.loads(line) for line in open(session.path, encoding="utf-8")]
    assert lines[-1]["type"] == "lane"
    assert lines[-1]["name"] == "main-1"
    assert lines[-1]["leaf"] == mid


def test_fork_then_append_parents_to_fork_point(tmp_path):
    """fork 后写消息:parent = fork 点,绕过原分支后续消息;main lane 不受影响。"""
    session = Session("s1", tmp_path)
    session.append(user_message("a"))
    session.append(user_message("b"))
    session.append(user_message("c"))
    u2 = session.load()[1].uuid
    session.fork(u2)  # 分支点 = 中间消息
    session.append(user_message("d"))
    entries = _entries(session)
    # 活跃 lane(新分支)线性视图 = 共享前缀 + fork 点 + 新消息
    assert [m.content for m in linear_messages(entries)] == ["a", "b", "d"]
    assert [m.content for m in linear_messages(entries, "main")] == ["a", "b", "c"]
    assert [m.content for m in session.load()] == ["a", "b", "d"]
    # 新消息 parent 链:挂 fork 点
    assert _messages(session)[-1].parent == u2
    # 树视图:两条分支都从 fork 点续写(u2 的子树 = 原分支 c + 新分支 d)
    view = build_tree(entries)
    assert [c.entry.uuid for c in view.nodes[u2].children] == [
        m.uuid for m in _messages(session)[-2:]
    ]


def test_branch_name_counting(tmp_path):
    """name 缺省 = main-{n},n = 既有分支计数 + 1(默认 main 恒计入)。"""
    session = Session("s1", tmp_path)
    session.append(user_message("a"))
    u1 = session.load()[0].uuid
    assert session.fork(u1) == "main-1"
    assert session.fork(u1) == "main-2"


def test_named_fork(tmp_path):
    session = Session("s1", tmp_path)
    session.append(user_message("a"))
    u1 = session.load()[0].uuid
    assert session.fork(u1, name="fix") == "fix"
    assert session.fork(u1, name="fix") == "fix"  # 显式同名允许(追加式)


def test_fork_copies_no_messages(tmp_path):
    """fork 不复制消息:文件行数只增一条 lane entry;续写只增消息 + lane 指针。"""
    session = Session("s1", tmp_path)
    session.append(user_message("a"))
    session.append(user_message("b"))
    before = sum(1 for _ in open(session.path, encoding="utf-8"))
    u1 = session.load()[0].uuid
    session.fork(u1)
    assert sum(1 for _ in open(session.path, encoding="utf-8")) == before + 1
    session.append(user_message("c"))
    assert sum(1 for _ in open(session.path, encoding="utf-8")) == before + 3


def test_fork_survives_reload(tmp_path):
    """重开后 fork 状态正确:活跃 lane = 新分支,续写挂 fork 点。"""
    session = Session("s1", tmp_path)
    session.append(user_message("a"))
    u1 = session.load()[0].uuid
    session.fork(u1, name="alt")
    reloaded = Session("s1", tmp_path)
    reloaded.load()  # --continue 路径:load 重建活跃 lane 与游标
    reloaded.append(user_message("b"))
    assert _messages(reloaded)[-1].parent == u1
    assert [m.content for m in reloaded.load()] == ["a", "b"]
    lines = [json.loads(line) for line in open(session.path, encoding="utf-8")]
    assert lines[-1]["name"] == "alt"  # 续写推进的 lane 指针用新分支名


def test_append_bookmark(tmp_path):
    """append_bookmark 追加 bookmark entry,读端挂到目标节点。"""
    session = Session("s1", tmp_path)
    session.append(user_message("a"))
    u1 = session.load()[0].uuid
    entry = session.append_bookmark(u1, "auth-fix")
    assert isinstance(entry, SessionEntry)
    assert entry.type == "bookmark"
    assert entry.data == {"name": "auth-fix", "entry": u1}
    assert json.loads(open(session.path, encoding="utf-8").read().splitlines()[-1])[
        "type"
    ] == "bookmark"
    view = build_tree(_entries(session))
    assert [b.data["name"] for b in view.nodes[u1].bookmarks] == ["auth-fix"]
