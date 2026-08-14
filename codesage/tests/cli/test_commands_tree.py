"""Slash command tests (phase 12 S5, spec §6/§9/§10.1 L407).

/tree 渲染(分支头/书签/翻页/上下文窗口)、--type 筛选、--bookmarks;/fork
/bookmark 输出;/sessions 表头 + (untitled)(§9.2);/archive 归档/恢复输出。
符号即语义断言(§1.4.3):→/✓/! 是语义本体,渲染无 ANSI 色码 —— 符号独立于
颜色;80 字符截断断言(§1.4.2):显示层截断、数据层全量保留。

直接调 handler(args, state)(state 带 fake loop,有 session 属性即可)——
不经过 REPL 分发,保持最轻。
"""

import os

from codesage.ai import ContentBlock
from codesage.cli.commands import find_command
from codesage.config import paths
from codesage.core import Session, assistant_message, user_message


class _FakeLoop:
    """Minimal loop: only session matters for these handlers."""

    mode = "default"

    def __init__(self, session=None):
        self.session = session


def _state(session=None):
    return {"loop": _FakeLoop(session)}


def _run(cmd, args, session=None, capsys=None):
    find_command(cmd).handler(list(args), _state(session))
    return capsys.readouterr().out if capsys is not None else None


def _tree_out(session, *args, capsys):
    return _run("tree", args, session, capsys)


def _patch_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / ".codesage")


# ---- 固定夹具 ----

def _branch_session(root, sid="s1") -> Session:
    """main 链 5 消息(③ 是带书签的 tool_use)+ 中部 operation + fork @ ③ → main-1
    续写 1 条。文件序:1 user, 2 assistant, 3 tool_use(★), 4 user, 5 assistant,
    6 operation, 7 user(fork 分支)。"""
    s = Session(sid, root)
    s.append_message(user_message("修复 auth 登录 bug"))
    s.append_message(assistant_message("尝试了方案 A"))
    e3 = s.append_message(
        assistant_message([ContentBlock(type="tool_use", id="t1", name="Bash", input={"cmd": "npm test"})])
    )
    s.append_bookmark(e3.uuid, "auth-fix")
    s.append_message(user_message("再试一次,加日志"))
    s.append_message(assistant_message("方案 B 通过"))
    s.append_operation("tool_started", tool="Bash", args_summary="npm run deploy")
    s.fork(e3.uuid)  # → lane main-1,fork @ ③
    s.append_message(user_message("从 ③ 继续,换方案 C"))
    return s


def _open_op_session(root, sid="s2") -> Session:
    """末尾 operation = 未完成(§7.2 启发式)→ /tree 该行 ! 前缀。"""
    s = Session(sid, root)
    s.append_message(user_message("q1"))
    s.append_message(assistant_message("a1"))
    s.append_operation("tool_started", tool="Bash", args_summary="npm run deploy")
    return s


# ---- /tree 渲染(§6)----

def test_tree_renders_branches_and_symbols(tmp_path, capsys):
    s = _branch_session(tmp_path)
    out = _tree_out(s, capsys=capsys)

    # 会话头:消息数 + 分支数
    assert "session s1" in out and "6 messages, 2 branches" in out
    # 分支头:main 装饰线 + 活跃 lane → + fork 装饰(§6 示例形态)
    assert "main ─" in out
    assert "→ main-1" in out
    assert "fork @ ③" in out
    # 书签符号:✓ 前缀 + (★ 名) 后缀
    assert "✓ ③" in out and "(★ auth-fix)" in out
    # 行内容:编号/类型/内容
    assert "① user" in out and '"修复 auth 登录 bug"' in out
    assert "③ tool_use" in out and 'Bash({"cmd": "npm test"})' in out
    # 符号即语义(§1.4.3):渲染无 ANSI 色码,符号独立于颜色
    assert "\x1b[" not in out


def test_tree_open_operation_bang(tmp_path, capsys):
    """未完成操作(§7.2 find_open_operations 命中)行前缀 ! —— 符号断言。"""
    s = _open_op_session(tmp_path)
    out = _tree_out(s, capsys=capsys)
    assert "! ③ operation" in out
    assert "tool_started Bash(npm run deploy)" in out


def test_tree_row_truncated_at_80_data_untouched(tmp_path, capsys):
    """§1.4.2 信息密度分层:显示层截断(行 ≤80),数据层全量保留(文件内完整)。"""
    long_text = "很长" * 200  # 400 字符
    s = Session("s1", tmp_path)
    s.append_message(user_message(long_text))
    out = _tree_out(s, capsys=capsys)

    for line in out.splitlines():
        assert len(line) <= 80
    assert "…" in out  # 截断标记
    assert len(s.load()[0].content) == len(long_text)  # 数据层未动
    assert s.load()[0].content == long_text


def test_tree_type_filter(tmp_path, capsys):
    s = Session("s1", tmp_path)
    s.append_message(user_message("q1"))
    s.append_message(assistant_message("a1"))
    s.append_message(assistant_message([ContentBlock(type="tool_use", id="t1", name="Bash", input={"cmd": "ls"})]))
    s.append_message(user_message([ContentBlock(type="tool_result", tool_use_id="t1", content="ok")]))

    out = _tree_out(s, "--type", "user", capsys=capsys)
    assert "① user" in out
    assert "assistant" not in out and "tool_use" not in out and "tool_result" not in out

    out = _tree_out(s, "--type", "tool_use", capsys=capsys)
    assert "③ tool_use" in out and "① user" not in out

    out = _tree_out(s, "--type", "tool_result", capsys=capsys)
    assert "④ tool_result" in out  # 工具结果载体(user 角色)按块类型渲染

    out = _tree_out(s, "--type", "bogus", capsys=capsys)
    assert "unknown type" in out


def test_tree_bookmarks_only(tmp_path, capsys):
    s = Session("s1", tmp_path)
    e1 = s.append_message(user_message("q1"))
    s.append_message(assistant_message("a1"))
    s.append_bookmark(e1.uuid, "start")

    out = _tree_out(s, "--bookmarks", capsys=capsys)
    assert "① user" in out and "(★ start)" in out and "✓" in out
    assert "assistant" not in out  # 未标记行过滤


def test_tree_pagination(tmp_path, capsys):
    s = Session("s1", tmp_path)
    for i in range(1, 26):
        s.append_message(user_message(f"q{i}"))

    page1 = _tree_out(s, capsys=capsys)
    assert "① user" in page1 and "⑳ user" in page1
    assert "q21" not in page1 and "q25" not in page1

    page2 = _tree_out(s, "2", capsys=capsys)  # 数字 ≤ 总页数(2)= 页码
    assert "21 user" in page2 and "25 user" in page2
    assert "q20" not in page2


def test_tree_context_window(tmp_path, capsys):
    """§6 /tree <entryId>:所在分支上下文窗口(前 5 后 3)+ parent 链标注
    (↑ 祖先 / ▼ 目标)。数字引用与 uuid 引用等价。"""
    s = Session("s1", tmp_path)
    msgs = [s.append_message(user_message(f"q{i}")) for i in range(10)]
    target = msgs[7]  # 0-based 7 = 1-based ⑧;窗口 = q2..q9(前 5 后 3)

    out = _tree_out(s, target.uuid, capsys=capsys)
    assert "context of entry ⑧" in out and "lane main" in out
    assert "▼" in out and "↑" in out  # 目标 + parent 链标注(符号即语义,§1.4.3)
    target_line = next(l for l in out.splitlines() if "▼" in l)
    assert "⑧ user" in target_line and '"q7"' in target_line  # 目标行
    assert '"q2"' in out and '"q9"' in out
    assert "q0" not in out and "q1" not in out  # 窗口外(前 5 后 3)

    out_num = _tree_out(s, "8", capsys=capsys)  # 8 > 总页数(1)→ entry 编号
    assert "context of entry ⑧" in out_num and "▼" in out_num
    assert '"q7"' in out_num


def test_tree_requires_session(capsys):
    out = _run("tree", [], None, capsys)
    assert "no active session" in out


def test_tree_no_entries(tmp_path, capsys):
    out = _tree_out(Session("s1", tmp_path), capsys=capsys)  # 空文件
    assert "no entries" in out


# ---- /fork(§4.2/§6)----

def test_fork_output_and_lane(tmp_path, capsys):
    s = Session("s1", tmp_path)
    s.append_message(user_message("q1"))
    e2 = s.append_message(user_message("q2"))
    s.append_message(assistant_message("a1"))

    out = _run("fork", ["1"], s, capsys)
    assert out == "forked at 1 → lane main-1\n"
    # fork 后线性视图 = 分支点链(共享前缀历史,§4.2)
    assert [m.content for m in s.load_lane("main-1")] == ["q1"]
    # 指定名字
    out = _run("fork", ["2", "mylane"], s, capsys)
    assert out == "forked at 2 → lane mylane\n"
    assert [m.content for m in s.load_lane("mylane")] == ["q1", "q2"]
    assert s.load_lane("mylane")[-1].uuid == e2.uuid


def test_fork_unknown_entry_reports(tmp_path, capsys):
    s = _branch_session(tmp_path)
    out = _run("fork", ["999"], s, capsys)
    assert "entry not found: 999" in out


def test_fork_requires_message_entry(tmp_path, capsys):
    s = _open_op_session(tmp_path)
    out = _run("fork", ["3"], s, capsys)  # ③ = operation,不可作分支点
    assert "不是消息" in out


def test_fork_usage(tmp_path, capsys):
    out = _run("fork", [], Session("s1", tmp_path), capsys)
    assert "usage: /fork" in out


# ---- /bookmark(§6)----

def test_bookmark_output_and_persistence(tmp_path, capsys):
    s = Session("s1", tmp_path)
    e1 = s.append_message(user_message("q1"))

    out = _run("bookmark", ["1", "start"], s, capsys)
    assert out == "bookmarked 1 as start\n"
    # 持久化在会话文件内(重开 Session 仍可读,§6)
    s2 = Session("s1", tmp_path)
    bms = [e for e in s2.entries if e.type == "bookmark"]
    assert len(bms) == 1
    assert bms[0].data["name"] == "start" and bms[0].data["entry"] == e1.uuid
    # /tree --bookmarks 可见(★ 后缀)
    out = _tree_out(s2, "--bookmarks", capsys=capsys)
    assert "(★ start)" in out


def test_bookmark_unknown_entry_reports(tmp_path, capsys):
    out = _run("bookmark", ["9", "x"], Session("s1", tmp_path), capsys)
    assert "entry not found: 9" in out


# ---- /sessions(§9.2)----

def _two_sessions(tmp_path, monkeypatch) -> tuple[Session, Session]:
    _patch_config_dir(monkeypatch, tmp_path)
    root = paths.config_dir() / "sessions"
    s1 = Session("session-20260814-103000-123456", root)
    s1.append_message(user_message("q1"))
    s2 = Session("session-20260814-103100-654321", root)
    s2.append_meta(title="我的会话")
    s2.append_message(user_message("q1"))
    s2.append_message(user_message("q2"))
    os.utime(s1.path, (0, 1000))
    os.utime(s2.path, (0, 2000))
    return s1, s2


def test_sessions_header_and_untitled(tmp_path, monkeypatch, capsys):
    _two_sessions(tmp_path, monkeypatch)
    out = _run("sessions", [], None, capsys)

    for word in ("id", "title", "messages", "branches", "time"):
        assert word in out  # §9.2 表头
    assert "(untitled)" in out  # 无标题会话
    assert '"我的会话"' in out
    # 按 mtime 倒序:s2 更新 → 在前
    lines = out.splitlines()
    assert "我的会话" in lines[1] and "(untitled)" in lines[2]
    # id 显示层截断(§1.4.2):前缀形态无区分度 → 尾部时间戳段
    assert "…" in out and "123456" in out


def test_sessions_archive_and_all(tmp_path, monkeypatch, capsys):
    s1, s2 = _two_sessions(tmp_path, monkeypatch)
    find_command("archive").handler(["session-20260814-103000-123456"], _state())

    out = _run("sessions", [], None, capsys)
    assert "(untitled)" not in out  # 归档后活跃列表不可见(§9.1)
    out = _run("sessions", ["--archive"], None, capsys)
    assert "(untitled)" in out  # 仅归档
    out = _run("sessions", ["--all"], None, capsys)
    assert "我的会话" in out and "(untitled)" in out  # 两者


# ---- /archive(§9.1)----

def test_archive_command_move_and_restore(tmp_path, monkeypatch, capsys):
    _patch_config_dir(monkeypatch, tmp_path)
    root = paths.config_dir() / "sessions"
    s = Session("s1", root)
    s.append_message(user_message("q1"))

    out = _run("archive", ["s1"], None, capsys)
    assert "archived s1" in out
    assert not s.path.exists() and (root / "archive" / "s1.jsonl").exists()

    out = _run("archive", ["s1", "--restore"], None, capsys)
    assert "restored s1" in out
    assert s.path.exists() and not (root / "archive" / "s1.jsonl").exists()

    out = _run("archive", ["ghost"], None, capsys)
    assert "error:" in out and "not found" in out


def test_archive_usage(capsys):
    out = _run("archive", [], None, capsys)
    assert "usage: /archive" in out


# ---- 注册表(HELP_TEXT 自动生成,§10.2 红线)----

def test_new_commands_registered():
    for name in ("tree", "fork", "bookmark", "sessions", "archive"):
        assert find_command(name) is not None
