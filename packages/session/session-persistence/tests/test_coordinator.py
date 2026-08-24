"""持久化协调器集成单测:MemoryBackend + 真实 SessionStore。

覆盖 DSH coordinator 契约的等价断言面:构造校验、惰性物化、
append 连续 seq 契约、load 的崩溃修复(interrupted 关闭器 +
撕裂尾截断)、inspect 不提交、prepare/resume 全生命周期
(预留 → enter → announce → attach → 事件落盘 → dispose 退休)、
id 冲突(同 id 不同 cwd)、legacy 事件迁移、未知类型/版本拒绝、
HMR 种子前缀采纳、readFrom 后缀读、dispose 排干与后端关闭。
"""

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]  # 仓库根
_PACKAGES = Path(__file__).resolve().parents[3]  # packages/
_PKG = Path(__file__).resolve().parents[1]  # 本包目录(session-persistence)
sys.path.insert(0, str(_PKG))
sys.path.insert(0, str(_PACKAGES))
sys.path.insert(0, str(_ROOT / "cordis-py"))

from cordis import Context  # noqa: E402

from core.session import (  # noqa: E402
    SESSION_FORMAT_VERSION,
    SessionStore,
    adopt_session_event,
    install,
)
from session.session_persistence.src.coordinator import (  # noqa: E402
    MAX_WRITE_BATCH_DELAY_MS,
    PersistenceCoordinator,
    SessionFormatUnsupportedError,
    SessionPersistenceCorruptionError,
    StoredPrefix,
    sessionFormatVersionRefusal,
)
from session.session_persistence.src.preparations import SessionPreparations  # noqa: E402


# ---- 内存后端(PersistenceBackend 契约的最小实现)----


class MemoryBackend:
    """真实性的内存后端:每 id 一个日志 + 修订号 + 撕裂标记。"""

    name = "memory"

    def __init__(self) -> None:
        self.stored = {}  # id -> {"meta", "events", "revision", "torn"}
        self.closed = False

    async def loadStored(self, id: str) -> StoredPrefix | None:
        entry = self.stored.get(id)
        if entry is None:
            return None
        # 撕裂尾由后端在读时永久丢弃(JSONL 后端:文件尾未完成行
        # 从未成为日志的一部分,读取即删除),只暴露完整前缀 +
        # tornMarker;协调器看不到撕裂事件本身。
        torn = entry["torn"]
        if torn is not None:
            entry["events"] = entry["events"][:-1]
        return StoredPrefix(
            dict(entry["meta"]), [dict(e) for e in entry["events"]], entry["revision"], torn
        )

    async def readStoredRevision(self, id: str):
        entry = self.stored.get(id)
        return entry["revision"] if entry is not None else None

    async def appendBatch(self, meta: dict, events: list, materialized: bool) -> None:
        id = meta["id"]
        if id not in self.stored:
            self.stored[id] = {"meta": dict(meta), "events": [], "revision": 0, "torn": None}
        entry = self.stored[id]
        entry["events"].extend(dict(e) for e in events)
        entry["revision"] += 1

    async def commitRepair(self, meta: dict, tornMarker, closers: list) -> None:
        id = meta["id"]
        entry = self.stored.setdefault(id, {"meta": dict(meta), "events": [], "revision": 0, "torn": None})
        if tornMarker is not None:
            assert entry["torn"] == tornMarker  # 后端只接受匹配其标记的修复
            entry["torn"] = None
        entry["events"].extend(dict(e) for e in closers)
        entry["revision"] += 1

    async def list(self) -> list:
        return [dict(e["meta"]) for e in self.stored.values()]

    async def close(self) -> None:
        self.closed = True


# ---- 仪式辅助(与 test_session.py 同款风格)----


def _meta(id_, cwd="C:/work"):
    """完整 session header 形状(DSH 契约测试 meta() 同款:version 必带)。"""
    return {"id": id_, "cwd": cwd, "createdAt": 1, "version": SESSION_FORMAT_VERSION}


def _turn_start(s, turn=1):
    return s.append("turn/start", {"turn": turn})


def _step_start(s, step=1):
    return s.append("step/start", {"turn": 1, "step": step})


def _user(s, text="hello", id_="u1"):
    return s.append(
        "user/message",
        {"role": "user", "id": id_, "source": {"kind": "human"},
         "content": [{"type": "text", "text": text}]},
        surface_op="append",
    )


def _step_end(s):
    return s.append("step/end", {"turn": 1, "step": 1})


def _turn_end(s):
    return s.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})


def _closed_turn(s, turn=1):
    """一段 invariant 平衡的回合仪式(无 assistant/tool,5 事件)。"""
    _turn_start(s, turn)
    _step_start(s)
    _user(s, "hello", f"u{turn}")
    _step_end(s)
    _turn_end(s)


def _ev(seq, type_, **data):
    """存储形状事件:信封 + data;surface 类型必须由调用方补 surfaceOp。"""
    return {"type": type_, "seq": seq, "time": 1, "data": data}


# ---- fixture ----


@pytest.fixture
async def harness():
    """真实 cordis ctx + SessionStore + MemoryBackend + 协调器。"""
    ctx = Context()
    store = SessionStore(ctx)
    backend = MemoryBackend()
    persistence = PersistenceCoordinator(
        ctx, backend, {"preparedSessionCacheSize": 5, "writeBatchMaxDelayMs": 200}
    )
    yield {
        "ctx": ctx,
        "store": store,
        "backend": backend,
        "persistence": persistence,
    }


def _h(harness):
    return harness["ctx"], harness["store"], harness["backend"], harness["persistence"]


# ---- 构造校验 ----


def test_constructor_validates_options():
    async def scenario():
        ctx = Context()
        store = SessionStore(ctx)
        backend = MemoryBackend()
        with pytest.raises(TypeError, match="preparedSessionCacheSize"):
            PersistenceCoordinator(ctx, backend, {"preparedSessionCacheSize": 0})
        with pytest.raises(TypeError, match="preparedSessionCacheSize"):
            PersistenceCoordinator(ctx, backend, {"preparedSessionCacheSize": 1.5})
        # 部分选项缺省 cache 尺寸:TS 默认参数语义下整包未提供默认,
        # 先报缺省键(镜像 DSH 行为)。
        with pytest.raises(TypeError, match="preparedSessionCacheSize"):
            PersistenceCoordinator(ctx, backend, {"writeBatchMaxDelayMs": 0})
        with pytest.raises(TypeError, match="writeBatchMaxDelayMs"):
            PersistenceCoordinator(ctx, backend, {"preparedSessionCacheSize": 5, "writeBatchMaxDelayMs": 0})
        with pytest.raises(TypeError, match="writeBatchMaxDelayMs"):
            PersistenceCoordinator(ctx, backend, {"preparedSessionCacheSize": 5, "writeBatchMaxDelayMs": MAX_WRITE_BATCH_DELAY_MS + 1})
        # 全缺省:默认值生效
        ok = PersistenceCoordinator(ctx, backend)
        assert ok.writeBatchMaxDelayMs == 200
        ok2 = PersistenceCoordinator(ctx, backend, {"preparedSessionCacheSize": 5, "writeBatchMaxDelayMs": MAX_WRITE_BATCH_DELAY_MS})
        assert ok2.writeBatchMaxDelayMs == MAX_WRITE_BATCH_DELAY_MS

    asyncio.run(scenario())


def test_constructor_requires_event_loop():
    """同步构造(无运行循环)给出明确诊断。"""
    ctx = Context()
    backend = MemoryBackend()
    with pytest.raises(RuntimeError, match="running event loop"):
        PersistenceCoordinator(ctx, backend)


# ---- 惰性物化与 append 契约 ----


def test_create_is_lazy_until_first_append(harness):
    """create 只记意图;首个 append 才物化工件。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        await persistence.create(_meta("s-lazy"))
        assert await backend.list() == []  # 未 append:无工件
        await persistence.append("s-lazy", [_ev(0, "turn/start", turn=1)])
        headers = await backend.list()
        assert [h["id"] for h in headers] == ["s-lazy"]

    asyncio.run(scenario())


def test_append_seq_mismatch_rejected(harness):
    """连续性契约:首事件 seq 必须续上存储日志。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        await persistence.create(_meta("s-seq"))
        await persistence.append("s-seq", [_ev(0, "turn/start", turn=1)])
        with pytest.raises(RuntimeError, match="seq mismatch"):
            await persistence.append("s-seq", [_ev(1, "user/message"), _ev(3, "step/end")])
        with pytest.raises(RuntimeError, match="seq mismatch"):
            await persistence.append("s-seq", [_ev(2, "user/message")])  # 断裂

    asyncio.run(scenario())


def test_create_collision_rejected(harness):
    """重复 create 拒绝;已物化的 id 拒绝新建(提示 load/resume)。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        await persistence.create(_meta("s-dup"))
        with pytest.raises(RuntimeError, match="already exists"):
            await persistence.create(_meta("s-dup"))
        await persistence.append("s-dup", [_ev(0, "turn/start", turn=1)])
        # 物化但协调器不在内存:新实例同 id 创建被挡(指向 load/resume)
        ctx2 = Context()
        SessionStore(ctx2)  # 协调器要求 ctx.sessions 存在(DSH 类型强制)
        fresh = PersistenceCoordinator(ctx2, backend, {"preparedSessionCacheSize": 5, "writeBatchMaxDelayMs": 200})
        with pytest.raises(RuntimeError, match="load/resume it instead of creating"):
            await fresh.create(_meta("s-dup"))

    asyncio.run(scenario())


def test_append_non_json_data_rejected(harness):
    """拒绝非 JSON 可序列化的事件负载。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        await persistence.create(_meta("s-json"))
        with pytest.raises(TypeError, match="not losslessly JSON-serializable"):
            await persistence.append("s-json", [{"type": "x", "seq": 0, "time": 1, "data": {"bad": 1j}}])

    asyncio.run(scenario())


# ---- load:崩溃修复与读取 ----


def test_load_returns_balanced_view_with_closers(harness):
    """load 修复崩溃回合:打开回合收到 interrupted 关闭器并落盘。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        await persistence.create(_meta("s-crash"))
        await persistence.append("s-crash", [_ev(0, "turn/start", turn=1), _ev(1, "step/start", turn=1, step=1)])
        inspection = await persistence.load("s-crash")
        types = [e["type"] for e in inspection.events]
        assert types == ["turn/start", "step/start", "step/end", "turn/end"]
        assert inspection.events[-1]["data"]["reason"]["kind"] == "interrupted"
        # 修复已提交:存储现在平衡
        types2 = [e["type"] for e in backend.stored["s-crash"]["events"]]
        assert types2 == ["turn/start", "step/start", "step/end", "turn/end"]

    asyncio.run(scenario())


def test_load_truncates_torn_tail(harness):
    """撕裂尾:load 截断撕裂事件,同时仍合成关闭器(commitRepair
    同时收 tornMarker 与 closers —— DSH 契约测试同款断言)。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        await persistence.create(_meta("s-torn"))
        await persistence.append("s-torn", [_ev(0, "turn/start", turn=1)])
        # 模拟撕裂:后端日志末尾追加一个从未提交的撕裂事件 + 打标记
        entry = backend.stored["s-torn"]
        entry["events"].append({"type": "turn/start", "seq": 99, "time": 1, "data": {}})
        entry["torn"] = "torn-token-1"
        inspection = await persistence.load("s-torn")
        # 截断后的真实事件 + 合成关闭器(打开回合被 interrupted 关闭)
        types = [e["type"] for e in inspection.events]
        assert types == ["turn/start", "turn/end"]
        assert inspection.events[-1]["data"]["reason"]["kind"] == "interrupted"
        # 修复耐久:撕裂标记消费,关闭器落盘,修订号 +1
        assert backend.stored["s-torn"]["torn"] is None
        assert [e["type"] for e in backend.stored["s-torn"]["events"]] == ["turn/start", "turn/end"]
        assert backend.stored["s-torn"]["revision"] == 2

    asyncio.run(scenario())


def test_load_missing_session(harness):
    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        with pytest.raises(RuntimeError, match='session "ghost" not found'):
            await persistence.load("ghost")

    asyncio.run(scenario())


def test_inspect_does_not_commit(harness):
    """inspect 不提交修复:冷打开回合只内存合成关闭器,后端不变。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        await persistence.create(_meta("s-insp"))
        await persistence.append("s-insp", [_ev(0, "turn/start", turn=1)])
        inspection = await persistence.inspect("s-insp")
        assert [e["type"] for e in inspection.events] == ["turn/start", "turn/end"]
        assert [e["type"] for e in backend.stored["s-insp"]["events"]] == ["turn/start"]
        assert backend.stored["s-insp"]["revision"] == 1  # 未修复

    asyncio.run(scenario())


# ---- prepare / resume 生命周期 ----


def test_prepare_reserve_enter_announce_attach(harness):
    """完整 resume:prepare 预留 → store.enter → announce 触发 attach
    → 追加事件落盘 → dispose 退休释放状态。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        # 先持久化一个会话
        s = store.prepare("s-resume", {"meta": {"cwd": "C:/work", "createdAt": 1}})
        detach = store.enter(s)
        store.announce(s)
        _closed_turn(s)
        await store.flush(s)
        detach()
        retirement = persistence.retirements.get("s-resume")
        if retirement is not None:
            await retirement
        assert len(backend.stored["s-resume"]["events"]) == 5  # 完整回合
        # prepare:预留精确的未发布会话
        prep = await persistence.prepare("s-resume")
        session = prep.session
        assert session.id == "s-resume"
        assert store.get("s-resume") is None
        # 发布:enter + announce → _initFor 找到预留 → _attachPrepared
        detach2 = store.enter(session)
        store.announce(session)
        # 追加一个完整新回合(turn 2)并 flush。注意:end-seed 分界
        # 事件也随未发布后缀落盘(DSH 同款:attach 只持久化
        # state.cursor 之后的 suffix),所以落盘事件 = 5 + end-seed + 4。
        _turn_start(session, 2)
        session.append("step/start", {"turn": 2, "step": 1})
        session.append("step/end", {"turn": 2, "step": 1})
        session.append("turn/end", {"turn": 2, "reason": {"kind": "completed"}})
        await store.flush(session)
        assert len(backend.stored["s-resume"]["events"]) == 10
        # 回合已关,活会话可 load(打开回合时 live load 会被拒绝)
        inspection = await persistence.load("s-resume")
        assert inspection.events[-1]["type"] == "turn/end"
        assert inspection.events[-1]["seq"] == 9
        prep.dispose()  # 已 attach,release 为移除(幂等)
        detach2()
        retirement2 = persistence.retirements.get("s-resume")
        if retirement2 is not None:
            await retirement2
        # 退休后状态释放:再次 load 走冷路径仍一致
        inspection2 = await persistence.load("s-resume")
        assert len(inspection2.events) == 10

    asyncio.run(scenario())


def test_prepare_unattached_release_reusable(harness):
    """prepare 未发布直接 dispose:预留回到 ready 池,可复用。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        s = store.prepare("s-rel", {"meta": {"cwd": "C:/work", "createdAt": 1}})
        detach = store.enter(s)
        store.announce(s)
        _closed_turn(s)
        await store.flush(s)
        detach()
        prep = await persistence.prepare("s-rel")
        prep.dispose()
        # 释放后同 id 再次 prepare 仍可用(ready 复用,无重新加载冲突)
        prep2 = await persistence.prepare("s-rel")
        assert prep2.session.id == "s-rel"
        prep2.dispose()

    asyncio.run(scenario())


# ---- 活会话写路径 ----


def test_live_append_flush_and_reload(harness):
    """活会话 append → flush 落盘 → load 一致。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        s = store.prepare("s-live", {"meta": {"cwd": "C:/work", "createdAt": 1}})
        detach = store.enter(s)
        store.announce(s)
        _closed_turn(s)
        await store.flush(s)
        assert len(backend.stored["s-live"]["events"]) == 5
        inspection = await persistence.load("s-live")
        assert [e["type"] for e in inspection.events] == [
            "turn/start", "step/start", "user/message", "step/end", "turn/end",
        ]
        detach()
        # 退休是异步排干:等它落定,状态释放后 load 走冷读
        retirement = persistence.retirements.get("s-live")
        if retirement is not None:
            await retirement
        assert "s-live" not in persistence.states
        inspection2 = await persistence.load("s-live")
        assert len(inspection2.events) == 5

    asyncio.run(scenario())


def test_live_load_snapshot(harness):
    """活会话的 load:返回耐久 flush 后的快照(活日志,无打开回合)。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        s = store.prepare("s-live2", {"meta": {"cwd": "C:/work", "createdAt": 1}})
        detach = store.enter(s)
        store.announce(s)
        _closed_turn(s)
        inspection = await persistence.load("s-live2")  # 活会话:flush + 快照
        assert [e["type"] for e in inspection.events] == [
            "turn/start", "step/start", "user/message", "step/end", "turn/end",
        ]
        assert inspection.meta["id"] == "s-live2"
        detach()

    asyncio.run(scenario())


def test_id_collision_different_cwd(harness):
    """同 id 不同 cwd 的活会话:冲突拒绝而非静默采纳。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        s = store.prepare("s-cwd", {"meta": {"cwd": "C:/work", "createdAt": 1}})
        detach = store.enter(s)
        store.announce(s)
        _closed_turn(s)
        await store.flush(s)
        detach()
        retirement = persistence.retirements.get("s-cwd")
        if retirement is not None:
            await retirement
        # 同 id、不同 cwd 的新活会话:碰撞错误。冲突在 announce 触发的
        # 异步初始化链(initFor → onCreated)里抛出,flush 浮出。
        s2 = store.prepare("s-cwd", {"meta": {"cwd": "D:/other", "createdAt": 2}})
        detach2 = store.enter(s2)
        store.announce(s2)
        with pytest.raises(RuntimeError, match="different cwd"):
            await store.flush(s2)
        detach2()

    asyncio.run(scenario())


# ---- legacy 迁移与格式拒绝 ----


def test_legacy_steering_message_migrated(harness):
    """legacy steering/message 迁移为 user/message 并铸身份。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        legacy = {
            "type": "steering/message", "seq": 0, "time": 1, "surfaceOp": "append",
            "data": {"turn": 1, "content": [{"type": "text", "text": "hi"}], "source": {"kind": "human"}},
        }
        backend.stored["s-leg"] = {"meta": {"id": "s-leg", "cwd": "C:/work", "createdAt": 1, "version": SESSION_FORMAT_VERSION}, "events": [legacy], "revision": 0, "torn": None}
        inspection = await persistence.load("s-leg")
        event = inspection.events[0]
        assert event["type"] == "user/message"
        assert event["data"]["id"] == "legacy-message:s-leg:0"
        assert event["data"]["role"] == "user"

    asyncio.run(scenario())


def test_legacy_unsupported_shape_rejected_as_corruption(harness):
    """本构建不可重放的 legacy 形状:包装为损坏错误。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        legacy = {"type": "request/header-delta", "seq": 0, "time": 1, "data": {}}
        backend.stored["s-bad"] = {"meta": {"id": "s-bad", "cwd": "C:/work", "createdAt": 1, "version": SESSION_FORMAT_VERSION}, "events": [legacy], "revision": 0, "torn": None}
        with pytest.raises(SessionPersistenceCorruptionError, match="unsupported legacy request/header-delta"):
            await persistence.load("s-bad")

    asyncio.run(scenario())


def test_unknown_event_type_refused(harness):
    """未标记 ignorable 的未知类型:格式拒绝而非静默跳过。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        unknown = {"type": "future/event", "seq": 0, "time": 1, "data": {}}
        backend.stored["s-fut"] = {"meta": {"id": "s-fut", "cwd": "C:/work", "createdAt": 1, "version": SESSION_FORMAT_VERSION}, "events": [unknown], "revision": 0, "torn": None}
        with pytest.raises(SessionFormatUnsupportedError, match="future/event"):
            await persistence.load("s-fut")
        # ignorable 标记放行
        backend.stored["s-fut"]["events"][0]["ignorable"] = True
        inspection = await persistence.load("s-fut")
        assert inspection.events[0]["type"] == "future/event"

    asyncio.run(scenario())


def test_version_refusal_verbatim(harness):
    """未来格式版本:方向性拒绝文本(逐字),不是损坏。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        future = SESSION_FORMAT_VERSION + 1
        backend.stored["s-ver"] = {"meta": {"id": "s-ver", "cwd": "C:/work", "createdAt": 1, "version": future}, "events": [], "revision": 0, "torn": None}
        with pytest.raises(SessionFormatUnsupportedError) as exc:
            await persistence.load("s-ver")
        assert str(exc.value) == sessionFormatVersionRefusal("s-ver", future)

    asyncio.run(scenario())


# ---- readFrom / list / HMR / dispose ----


def test_read_from_suffix(harness):
    """readFrom:从 seq 起读存储事件(无合成关闭器、无修复)。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        await persistence.create(_meta("s-read"))
        await persistence.append("s-read", [_ev(0, "turn/start", turn=1), _ev(1, "step/start", turn=1, step=1)])
        suffix = await persistence.readFrom("s-read", 1)
        assert [e["seq"] for e in suffix["events"]] == [1]
        assert suffix["meta"]["id"] == "s-read"
        # fromSeq 超出前缀:空列表(非错误)
        suffix2 = await persistence.readFrom("s-read", 5)
        assert suffix2["events"] == []

    asyncio.run(scenario())


def test_list_and_list_snapshots(harness):
    """list 只列已物化会话(list 是后端原始操作,协调器不中转)。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        await persistence.create(_meta("s-a"))
        assert await backend.list() == []  # 惰性:未 append 不出现
        await persistence.append("s-a", [_ev(0, "turn/start", turn=1)])
        headers = await backend.list()
        assert [h["id"] for h in headers] == ["s-a"]

    asyncio.run(scenario())


def test_hmr_adopts_live_seed_prefix(harness):
    """HMR:构造时已存在的活会话,seed 前缀被采纳为持久化历史。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        # 先建活会话但不用任何持久化(模拟 HMR 前挂着的会话)
        s = store.prepare("s-hmr", {"meta": {"cwd": "C:/work", "createdAt": 1}})
        detach = store.enter(s)
        store.announce(s)
        _turn_start(s)
        _step_start(s)
        _user(s)
        # 此时再构造协调器:HMR 循环 _initFor 已活会话
        backend2 = MemoryBackend()
        persistence2 = PersistenceCoordinator(ctx, backend2, {"preparedSessionCacheSize": 5, "writeBatchMaxDelayMs": 200})
        await store.flush(s)
        assert len(backend2.stored["s-hmr"]["events"]) == 3
        assert backend2.stored["s-hmr"]["meta"]["cwd"] == "C:/work"
        detach()

    asyncio.run(scenario())


def test_write_path_dispose_drains_and_closes(harness):
    """ctx fiber 的 write-path disposer:排干 + 关闭后端。"""

    async def scenario():
        ctx, store, backend, persistence = _h(harness)
        s = store.prepare("s-dis", {"meta": {"cwd": "C:/work", "createdAt": 1}})
        detach = store.enter(s)
        store.announce(s)
        _closed_turn(s)
        detach()  # 会话退休,但后端未关闭
        assert backend.closed is False
        # 根 fiber 的 disposer:注册的 effect 返回的 dispose(带 label)
        disposers = list(ctx.fiber._disposables)
        write_path = [d for d in disposers if getattr(d, "__cordis_effect__", {}).get("label") == "memory write path"]
        assert len(write_path) == 1
        await write_path[0]()
        assert backend.closed is True

    asyncio.run(scenario())
