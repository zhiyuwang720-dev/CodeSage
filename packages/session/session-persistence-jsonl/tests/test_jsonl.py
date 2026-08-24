"""JSONL 后端集成单测:真文件系统 + 真协调器编排。

覆盖 DSH jsonl.spec.ts 后端断言面:惰性物化、逐字节 round-trip、
撕裂尾修复(截断 + 合成关闭器、已提交事件永不重写)、appendLines
部分写回滚、修订号稳定、list 元数据级只读、跨项目/碰撞/编码冲突/
legacy 布局拒绝、路径穿越中和、磁盘日志采纳。
"""

import json
import os
import sys
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[1]  # 本包目录(session-persistence-jsonl)
_PACKAGES = Path(__file__).resolve().parents[3]  # packages/
_ROOT = Path(__file__).resolve().parents[4]  # 仓库根
sys.path.insert(0, str(_PKG))
sys.path.insert(0, str(_PACKAGES))
sys.path.insert(0, str(_ROOT / "cordis-py"))

from cordis import Context  # noqa: E402

from core.session import SESSION_FORMAT_VERSION, SessionStore  # noqa: E402

from session.session_persistence import SessionFormatUnsupportedError  # noqa: E402
from session.session_persistence_jsonl.src.format import encode_segment, event_lines, log_path, to_header_line  # noqa: E402
from session.session_persistence_jsonl.src.index import JsonlSessionPersistence  # noqa: E402


def _meta(id_, cwd="C:/work"):
    return {"id": id_, "cwd": cwd, "createdAt": 1, "version": SESSION_FORMAT_VERSION}


def _ev(seq, type_, **data):
    return {"type": type_, "seq": seq, "time": 1, "data": data}


def _closed_events():
    """一个已提交回合(协调器视为完整历史)。"""
    return [
        _ev(0, "turn/start", turn=1),
        _ev(1, "step/start", turn=1, step=1),
        _ev(2, "turn/end", turn=1, reason={"kind": "completed"}),
    ]


def _write_log(path: str, meta: dict, events: list, pack_chunks=True) -> bytes:
    """直接写一个物理日志文件(模拟任意磁盘状态),返回完整字节。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 物化布局:header 行 + (事件行 + 尾换行);空事件时只有 header 行
    content = json.dumps(to_header_line(meta), ensure_ascii=False) + "\n"
    if events:
        content += event_lines(events, pack_chunks) + "\n"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return content.encode("utf-8")


@pytest.fixture
async def fixture(tmp_path):
    """真实 cordis ctx + SessionStore + JSONL 后端(临时 root)。

    协调器要求运行中事件循环内构造:pytest-asyncio auto 模式下
    async fixture 在循环内执行。
    """
    ctx = Context()
    store = SessionStore(ctx)
    root = tmp_path / "root"
    backend = JsonlSessionPersistence(ctx, {"root": str(root)})
    yield {"ctx": ctx, "store": store, "backend": backend, "root": root}


def _b(fixture):
    return fixture["backend"]


def _path(fixture, id_, cwd="C:/work", compression="none"):
    return log_path(str(fixture["root"]), cwd, id_, compression)


# ---- 惰性物化与 round-trip ----


async def test_lazy_materialization_writes_no_file_until_first_append(fixture):
    backend = _b(fixture)
    await backend.create(_meta("s-lazy"))
    assert not os.path.exists(_path(fixture, "s-lazy"))
    await backend.append("s-lazy", [_ev(0, "turn/start", turn=1)])
    assert os.path.exists(_path(fixture, "s-lazy"))


async def test_round_trip_byte_identical(fixture):
    backend = _b(fixture)
    events = _closed_events()
    await backend.create(_meta("s-rt"))
    await backend.append("s-rt", events)
    raw = await backend.readRaw("s-rt")
    assert raw is not None
    assert raw.filename == "session.jsonl"
    # readRaw 内容与磁盘字节逐字一致(打包行、键序、换行)
    with open(_path(fixture, "s-rt"), encoding="utf-8", newline="") as handle:
        assert raw.content == handle.read()
    loaded = await backend.load("s-rt")
    assert [e["seq"] for e in loaded.events] == [0, 1, 2]


async def test_locate_stable_on_resume_fork_gets_own_location(fixture):
    backend = _b(fixture)
    first = backend.locate(_meta("s"))
    again = backend.locate(_meta("s"))
    assert first.path == again.path
    fork = backend.locate(_meta("s-fork"))
    assert fork.path != first.path


async def test_read_raw_absent_session(fixture):
    assert await _b(fixture).readRaw("nope") is None


async def test_pack_chunks_false_writes_one_event_per_line(fixture):
    # 独立 ctx:同一 ctx 上重复注册 sessionPersistence 服务会被 cordis 拒绝
    ctx2 = Context()
    SessionStore(ctx2)
    backend = JsonlSessionPersistence(ctx2, {"root": str(fixture["root"]), "packChunks": False})
    await backend.create(_meta("s-u"))
    await backend.append("s-u", _closed_events())
    with open(_path(fixture, "s-u"), encoding="utf-8", newline="") as handle:
        lines = handle.read().splitlines()
    assert len(lines) == 1 + 3  # header + 每个事件一行
    loaded = await backend.load("s-u")
    assert [e["seq"] for e in loaded.events] == [0, 1, 2]


# ---- 撕裂尾修复 ----


async def test_crash_tail_repair_truncates_and_appends_closers(fixture):
    backend = _b(fixture)
    meta = _meta("s-torn")
    path = _path(fixture, "s-torn")
    committed = _closed_events()
    tail = b'{"type": "turn/start", "seq": 3, "time": 1, "data": {"turn": 2}}'  # 撕裂:无换行
    full = _write_log(path, meta, committed)
    with open(path, "ab") as handle:
        handle.write(tail)
    full += tail
    stored = await backend.loadStored("s-torn")
    assert stored is not None
    assert [e["seq"] for e in stored.events] == [0, 1, 2]
    # tornMarker 指向已提交前缀的字节边界
    committed_bytes = len(full) - len(tail)
    assert stored.tornMarker == {"truncateTo": committed_bytes, "recoveredEvents": []}
    # 修复:截断撕裂尾 + 追加合成关闭器
    closers = [_ev(3, "turn/end", turn=2, reason={"kind": "interrupted"})]
    await backend.commitRepair(meta, stored.tornMarker, closers)
    with open(path, encoding="utf-8", newline="") as handle:
        repaired = handle.read()
    assert repaired.endswith('"interrupted"}}}' + "\n")  # data + reason + 信封闭合
    assert repaired.encode("utf-8").startswith(full[:committed_bytes])  # 已提交字节不重写
    again = await backend.loadStored("s-torn")
    assert again.tornMarker is None
    assert [e["seq"] for e in again.events] == [0, 1, 2, 3]


async def test_header_only_log_preserves_open_turn_on_load(fixture):
    backend = _b(fixture)
    meta = _meta("s-header-only")
    path = _path(fixture, "s-header-only")
    _write_log(path, meta, [])
    stored = await backend.loadStored("s-header-only")
    assert stored.events == []
    assert stored.tornMarker is None  # header 后的空余量不构成撕裂尾


async def test_append_lines_rolls_back_partial_write(fixture, monkeypatch):
    """部分写失败:字节回滚到写前大小,重试不产生 seq gap。"""
    backend = _b(fixture)
    meta = _meta("s-rb")
    await backend.create(meta)
    await backend.append(meta["id"], [_ev(0, "turn/start", turn=1)])
    before = os.path.getsize(_path(fixture, "s-rb"))
    real_write = os.write

    def flaky_write(fd, data):
        if isinstance(data, bytes) and data.startswith(b'{"type": "turn/end"'):
            real_write(fd, data[: len(data) // 2])  # 写一半后失败
            raise OSError(28, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", flaky_write)
    with pytest.raises(OSError, match="No space"):
        await backend.append(
            "s-rb", [_ev(1, "turn/end", turn=1, reason={"kind": "completed"})]
        )
    monkeypatch.undo()
    assert os.path.getsize(_path(fixture, "s-rb")) == before  # 回滚成功
    # 重试成功:无 seq gap
    await backend.append(
        "s-rb", [_ev(1, "turn/end", turn=1, reason={"kind": "completed"})]
    )
    loaded = await backend.load("s-rb")
    assert [e["seq"] for e in loaded.events] == [0, 1]


# ---- 修订号 ----


async def test_revision_stable_and_changes(fixture):
    backend = _b(fixture)
    meta = _meta("s-rev")
    await backend.create(meta)
    await backend.append(meta["id"], [_ev(0, "turn/start", turn=1)])
    light = await backend.readStoredRevision("s-rev")
    stored = await backend.loadStored("s-rev")
    assert light is not None
    assert light == stored.revision  # 轻量与全量读同一修订号
    await backend.append(meta["id"], [_ev(1, "turn/end", turn=1, reason={"kind": "completed"})])
    changed = await backend.readStoredRevision("s-rev")
    assert changed != light  # 追加后令牌变化


# ---- list / 发现 ----


async def test_list_discovers_across_project_directories(fixture):
    backend = _b(fixture)
    await backend.create(_meta("s-a", cwd="C:/work"))
    await backend.append("s-a", [_ev(0, "turn/start", turn=1)])
    await backend.create(_meta("s-b", cwd="D:/other"))
    await backend.append("s-b", [_ev(0, "turn/start", turn=1)])
    listed = await backend.list()
    ids = {entry["id"] for entry in listed}
    assert ids == {"s-a", "s-b"}
    snapshots = await backend.listSnapshots()
    assert {s.header["id"] for s in snapshots} == {"s-a", "s-b"}


async def test_list_empty_root_and_absent_root(fixture):
    assert await _b(fixture).list() == []
    # root 目录本身不存在 → 无会话,不报错(独立 ctx 防服务重复注册)
    ctx2 = Context()
    SessionStore(ctx2)
    missing = JsonlSessionPersistence(ctx2, {"root": str(fixture["root"] / "nope")})
    assert await missing.list() == []


async def test_list_skips_empty_and_non_header_logs(fixture):
    backend = _b(fixture)
    # 空文件与垃圾文件:元数据级读取跳过
    garbage = _path(fixture, "garbage")
    os.makedirs(os.path.dirname(garbage), exist_ok=True)
    with open(garbage, "w", encoding="utf-8") as handle:
        handle.write("this is not json\n")
    empty = _path(fixture, "empty")
    os.makedirs(os.path.dirname(empty), exist_ok=True)
    open(empty, "w").close()
    assert await backend.list() == []


async def test_list_reads_header_longer_than_8kb(fixture):
    backend = _b(fixture)
    # agentPreset 撑长 header 行而不影响物理路径(cwd 决定路径)
    meta = dict(_meta("s-long"), agentPreset="x" * 9000)
    _write_log(_path(fixture, "s-long", cwd=meta["cwd"]), meta, _closed_events())
    listed = await backend.list()
    assert len(listed) == 1
    assert listed[0]["id"] == "s-long"
    assert len(listed[0]["agentPreset"]) == 9000


async def test_list_rejects_header_whose_cwd_misidentifies_log(fixture):
    backend = _b(fixture)
    # 物理路径在 C:/work 项目下,header 却自称 D:/other
    meta = _meta("s-lying", cwd="D:/other")
    _write_log(_path(fixture, "s-lying", cwd="C:/work"), meta, _closed_events())
    with pytest.raises(ValueError, match="identify"):
        await backend.list()


async def test_duplicate_id_across_projects_rejected(fixture):
    backend = _b(fixture)
    for cwd in ("C:/work", "D:/other"):
        _write_log(_path(fixture, "s-dup", cwd=cwd), _meta("s-dup", cwd=cwd), _closed_events())
    with pytest.raises(ValueError, match="duplicate JSONL session id"):
        await backend.loadStored("s-dup")
    with pytest.raises(ValueError, match="duplicate JSONL session id"):
        await backend.list()


async def test_opposite_compression_artifact_rejected(fixture):
    backend = _b(fixture)
    meta = _meta("s-opp")
    _write_log(_path(fixture, "s-opp", compression="zstd"), meta, _closed_events())
    with pytest.raises(ValueError, match="compression"):
        await backend.loadStored("s-opp")
    with pytest.raises(ValueError, match="compression"):
        await backend.list()


async def test_flat_file_layout_rejected(fixture):
    backend = _b(fixture)
    # 平铺:项目目录下直接一个 .jsonl 文件(旧布局)
    project = fixture["root"] / "--C-work--"
    project.mkdir(parents=True, exist_ok=True)
    (project / "s.jsonl").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported flat-file layout"):
        await backend.list()
    with pytest.raises(ValueError, match="unsupported flat-file layout"):
        await backend.loadStored("s")


async def test_root_is_file_rejected(fixture):
    parent = fixture["root"].parent
    target = parent / "file"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError, match="not a directory"):
        JsonlSessionPersistence(Context(), {"root": str(target)})


async def test_path_traversal_id_stays_inside_root(fixture):
    backend = _b(fixture)
    meta = _meta("../evil")
    await backend.create(meta)
    await backend.append(meta["id"], [_ev(0, "turn/start", turn=1)])
    assert not (fixture["root"].parent / "--C-work--").exists()
    encoded = encode_segment("../evil")
    assert encoded not in ("../evil",)  # 分隔符被中和
    # 物理日志位于 root 内,id 段为编码形态
    project = fixture["root"] / "--C-work--"
    assert project.exists()


# ---- 格式拒绝 ----


async def test_future_version_refusal_points_at_raw_log(fixture):
    backend = _b(fixture)
    meta = dict(_meta("s-future"), version=SESSION_FORMAT_VERSION + 1)
    _write_log(_path(fixture, "s-future"), meta, [])
    with pytest.raises(SessionFormatUnsupportedError) as info:
        await backend.loadStored("s-future")
    assert info.value.location is not None
    assert info.value.location.path == _path(fixture, "s-future")


async def test_read_raw_rejects_corrupt_header(fixture):
    backend = _b(fixture)
    path = _path(fixture, "s-bad")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{not json}\n")
    with pytest.raises(ValueError, match="invalid header line"):
        await backend.readRaw("s-bad")


# ---- 磁盘日志采纳 ----


async def test_append_adopts_disk_only_session_and_repairs_crash_tail(fixture):
    backend = _b(fixture)
    meta = _meta("s-disk")
    path = _path(fixture, "s-disk")
    committed = _closed_events()
    tail = b'{"type": "turn/start", "seq": 3, "time": 1, "data": {"turn": 2}}'
    full = _write_log(path, meta, committed)
    with open(path, "ab") as handle:
        handle.write(tail)
    full += tail
    committed_bytes = len(full) - len(tail)
    # 不 create:磁盘已有日志,append 直接采纳(协调器 createCore 会拒绝)
    await backend.append("s-disk", [_ev(3, "turn/end", turn=2, reason={"kind": "interrupted"})])
    loaded = await backend.load("s-disk")
    # 采纳前缀 + 修复关闭:撕裂尾被截断,合成关闭器持久化
    assert [e["seq"] for e in loaded.events] == [0, 1, 2, 3]
    with open(path, encoding="utf-8", newline="") as handle:
        content = handle.read()
    assert content.encode("utf-8").startswith(full[:committed_bytes])


async def test_no_cwd_live_session_cannot_adopt_log_from_other_cwd(fixture):
    backend = _b(fixture)
    store = fixture["store"]
    _write_log(
        _path(fixture, "s-cwd", cwd="C:/work"),
        _meta("s-cwd", cwd="C:/work"),
        _closed_events(),
    )
    # 活会话:announce 触发异步 init 链,磁盘日志 cwd 不符在链中浮出
    session = store.prepare("s-cwd", {"meta": {"cwd": None, "createdAt": 1}})
    detach = store.enter(session)
    store.announce(session)
    with pytest.raises(RuntimeError, match="cwd"):
        await store.flush(session)
    detach()
