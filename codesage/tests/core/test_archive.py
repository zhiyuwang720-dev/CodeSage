"""Archive tests (phase 12 S5, spec §9.1/§10.1): move in/out of archive/,
active/archived enumeration, list_sessions exclusion, restore, error paths.

Archive = same-disk os.replace move: root-level → root/archive/, project-key
scoped → root/{project}/archive/. list_sessions excludes any level of
archive/ (§10.2 red line); archive_session on a missing or already-archived
id raises ValueError (find_session already excludes archive).
"""

import pytest

from codesage.core import (
    Session,
    active_sessions,
    archive_session,
    archived_sessions,
    list_sessions,
    restore_session,
    user_message,
)


def _session(root, sid="s1", project=None) -> Session:
    s = Session(sid, root, project_key=project)
    s.append_meta(model="main")
    s.append_message(user_message("你好"))
    return s


# ---- 归档移动(§9.1)----

def test_archive_moves_root_level(tmp_path):
    root = tmp_path / "sessions"
    s = _session(root)
    dest = archive_session(root, "s1")

    assert dest == root / "archive" / "s1.jsonl"
    assert dest.exists() and not s.path.exists()  # 移动而非复制
    # 数据无损:归档文件仍可读
    assert [m.content for m in Session("s1", root / "archive").load()] == ["你好"]


def test_archive_moves_project_scoped(tmp_path):
    root = tmp_path / "sessions"
    s = _session(root, "p1", project="my-proj")
    dest = archive_session(root, "p1")

    assert dest == root / "my-proj" / "archive" / "p1.jsonl"
    assert dest.exists() and not s.path.exists()


def test_archive_already_archived_raises(tmp_path):
    root = tmp_path / "sessions"
    _session(root)
    archive_session(root, "s1")
    with pytest.raises(ValueError):
        archive_session(root, "s1")  # 已归档 = list_sessions 不可见 → not found


def test_archive_missing_id_raises(tmp_path):
    root = tmp_path / "sessions"
    with pytest.raises(ValueError):
        archive_session(root, "ghost")


# ---- active/archived 枚举(§9.1)----

def test_active_archived_enumeration(tmp_path):
    root = tmp_path / "sessions"
    _session(root, "a1")
    _session(root, "a2", project="proj")
    archive_session(root, "a1")

    active = active_sessions(root)
    archived = archived_sessions(root)
    assert {m.session_id for m in active} == {"a2"}  # 排除任何层级 archive/
    assert {m.session_id for m in archived} == {"a1"}
    assert active[0].title is None  # 无标题 meta → None(渲染面 (untitled))


def test_meta_shape_and_title(tmp_path):
    root = tmp_path / "sessions"
    s = _session(root, "a1")
    s.append_meta(title="我的会话")
    meta = active_sessions(root)[0]
    assert meta.session_id == "a1"
    assert meta.title == "我的会话"
    assert meta.messages == 1
    assert meta.branches == 1
    assert meta.path == s.path


def test_meta_branches_count_lanes(tmp_path):
    root = tmp_path / "sessions"
    s = Session("a1", root)
    s.append_message(user_message("q1"))
    e2 = s.append_message(user_message("q2"))
    s.fork(e2.uuid)
    s.append_message(user_message("q3"))
    meta = active_sessions(root)[0]
    assert meta.messages == 3
    assert meta.branches == 2


def test_empty_and_missing_root(tmp_path):
    assert active_sessions(tmp_path / "nope") == []
    assert archived_sessions(tmp_path / "nope") == []
    root = tmp_path / "sessions"
    root.mkdir()
    assert active_sessions(root) == []


# ---- restore(§9.1 一行恢复)----

def test_restore_moves_back(tmp_path):
    root = tmp_path / "sessions"
    _session(root, "s1")
    archive_session(root, "s1")
    dest = restore_session(root, "s1")

    assert dest == root / "s1.jsonl"
    assert dest.exists() and not (root / "archive" / "s1.jsonl").exists()
    assert {m.session_id for m in active_sessions(root)} == {"s1"}
    assert archived_sessions(root) == []


def test_restore_missing_raises(tmp_path):
    root = tmp_path / "sessions"
    _session(root)
    with pytest.raises(ValueError):
        restore_session(root, "ghost")


def test_restore_after_restore_raises(tmp_path):
    root = tmp_path / "sessions"
    _session(root, "s1")
    archive_session(root, "s1")
    restore_session(root, "s1")
    with pytest.raises(ValueError):
        restore_session(root, "s1")  # 已恢复 → 不在 archive → not found


# ---- list_sessions 排除 archive(§9.1/§10.2 红线)----

def test_list_sessions_excludes_archive(tmp_path):
    root = tmp_path / "sessions"
    _session(root, "active")
    _session(root, "gone", project="proj")
    archive_session(root, "gone")

    assert {p.stem for p in list_sessions(root)} == {"active"}


def test_list_sessions_excludes_deep_archive(tmp_path):
    # 任何层级 archive/:root/archive/ 与 root/{project}/archive/ 都排除
    root = tmp_path / "sessions"
    _session(root, "s1")
    _session(root, "s2", project="proj")
    _session(root, "s3")
    archive_session(root, "s1")
    archive_session(root, "s2")

    assert {p.stem for p in list_sessions(root)} == {"s3"}
