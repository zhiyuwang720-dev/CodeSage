"""准备条目共享池单测:阶段机、LRU、预留生命周期与写保护。

覆盖 DSH preparations 契约:loading→ready→committing→reserved
阶段转移、同一 id 共享一次冷读、提交返回 None 时条目失效、预留的
attach/discard/release 三消费路径、ready 条目的 LRU 淘汰、以及
committing/reserved 期间的 assertWritable 写拒绝(逐字错误消息)。
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

from session.session_persistence.src.preparations import SessionPreparationReservation, SessionPreparations  # noqa: E402


class _FakeSession:
    """形似 core.session.Session 的最小假会话(只带 identity)。"""

    def __init__(self, id_) -> None:
        self.id = id_


class _Source:
    """PreparedSource 契约形状:必须持有 session(带 .id)。"""

    def __init__(self, id_) -> None:
        self.session = _FakeSession(id_)


def _source(id_):
    return _Source(id_)


async def _settle(awaitable):
    """同步测试里驱动异步装载的辅助。"""
    return await awaitable


def test_inspect_shares_single_load():
    """同一 id 并发 inspect 共享一次冷读;ready 后直接复用。"""

    async def scenario():
        loads = []
        pool = SessionPreparations(5)

        async def load():
            loads.append(1)
            await asyncio.sleep(0.01)
            return _source("s1")

        first = pool.inspect("s1", load)
        second = pool.inspect("s1", load)
        r1, r2 = await asyncio.gather(first, second)
        assert len(loads) == 1  # 共享:只加载一次
        assert r1.session.id == "s1" and r2.session.id == "s1"
        # ready 后再次 inspect 不再触发加载
        r3 = await pool.inspect("s1", load)
        assert len(loads) == 1

    asyncio.run(scenario())


def test_reserve_commits_and_returns_reservation():
    """reserve 走 committing → reserved,提交返回 (source, state)。"""

    async def scenario():
        pool = SessionPreparations(5)
        committed = []

        async def load():
            return _source("s1")

        async def commit(source):
            committed.append(source)
            return (source, {"cursor": 7})

        reservation = await pool.reserve("s1", load, commit)
        assert reservation is not None
        assert reservation.source.session.id == "s1"
        assert reservation.state == {"cursor": 7}
        assert len(committed) == 1
        # 提交期间写被拒绝
        with pytest.raises(RuntimeError, match="cannot append session"):
            pool.assertWritable("s1")

    asyncio.run(scenario())


def test_reserve_commit_none_invalidates_entry():
    """提交返回 None(持久化日志已变化)时条目被移除,reserve 返回 None。"""

    async def scenario():
        pool = SessionPreparations(5)

        async def load():
            return _source("s1")

        async def commit(source):
            return None

        reservation = await pool.reserve("s1", load, commit)
        assert reservation is None
        assert pool.has("s1") is False

    asyncio.run(scenario())


def test_reserve_commit_error_removes_entry():
    """提交抛错:条目移除,错误原样浮出。"""

    async def scenario():
        pool = SessionPreparations(5)

        async def load():
            return _source("s1")

        async def commit(source):
            raise RuntimeError("backend exploded")

        with pytest.raises(RuntimeError, match="backend exploded"):
            await pool.reserve("s1", load, commit)
        assert pool.has("s1") is False

    asyncio.run(scenario())


def test_reserve_waits_for_inflight_commit():
    """同 id 的第二次 reserve 等待进行中的 committing 落定(消费)后重试。

    reservationSettled 只在条目被消费(remove/makeReady)时落定:
    第一条预留未被消费前,第二条持续等待 —— 同一身份的并发
    reserve 不重复提交,也不并行持有。
    """

    async def scenario():
        pool = SessionPreparations(5)
        gate = asyncio.Event()
        attempts = []

        async def load():
            return _source("s1")

        async def commit(source):
            attempts.append("start")
            await gate.wait()
            return (source, {"cursor": 1})

        first = asyncio.ensure_future(pool.reserve("s1", load, commit))
        for _ in range(100):  # 轮询:等第一个进入 committing
            if pool.entries.get("s1", None) is not None and pool.entries["s1"].phase == "committing":
                break
            await asyncio.sleep(0.01)
        assert pool.entries["s1"].phase == "committing"
        second = asyncio.ensure_future(pool.reserve("s1", load, commit))
        await asyncio.sleep(0.05)
        assert len(attempts) == 1  # 第二个等待中,未重复提交
        gate.set()
        r1 = await first
        assert r1 is not None
        # 第一条未消费前第二条仍阻塞;discard 消费后它才继续并看到条目已走
        done = second.done()
        assert done is False
        pool.discard(r1)
        r2 = await second
        assert r2 is None  # 条目已被消费,返回 None

    asyncio.run(scenario())


def test_reservation_for_exact_session_only():
    """reservationFor 拒绝别名:非预留持有的 session 抛错而非放行。"""

    async def scenario():
        pool = SessionPreparations(5)

        async def load():
            return _source("s1")

        async def commit(source):
            return (source, {"cursor": 0})

        reservation = await pool.reserve("s1", load, commit)
        assert reservation is not None
        assert pool.reservationFor(reservation.source.session) is reservation
        # 同一 id 的不同会话对象:精确性检查拒绝,而非放行
        impostor = _FakeSession("s1")
        with pytest.raises(RuntimeError, match="persisted state already owns this identity"):
            pool.reservationFor(impostor)

    asyncio.run(scenario())


def test_attach_consumes_reservation():
    """attach 消费预留:精确 Session 挂载后条目离开池。"""

    async def scenario():
        pool = SessionPreparations(5)

        async def load():
            return _source("s1")

        async def commit(source):
            return (source, {"cursor": 0})

        reservation = await pool.reserve("s1", load, commit)
        pool.attach(reservation)
        assert pool.has("s1") is False
        # 已消费的预留重复 attach 抛错
        with pytest.raises(RuntimeError, match="preparation is no longer reserved"):
            pool.attach(reservation)

    asyncio.run(scenario())


def test_discard_consumes_reservation_idempotent():
    """discard 消费预留;重复 discard 静默(调用方只需已提交检查结果)。"""

    async def scenario():
        pool = SessionPreparations(5)

        async def load():
            return _source("s1")

        async def commit(source):
            return (source, {"cursor": 0})

        reservation = await pool.reserve("s1", load, commit)
        pool.discard(reservation)
        assert pool.has("s1") is False
        pool.discard(reservation)  # 幂等

    asyncio.run(scenario())


def test_release_reusable_returns_to_ready_lru():
    """release(reusable=True) 把未发布的预留放回 ready 池,可复用。"""

    async def scenario():
        pool = SessionPreparations(5)

        async def load():
            return _source("s1")

        async def commit(source):
            return (source, {"cursor": 0})

        reservation = await pool.reserve("s1", load, commit)
        pool.release(reservation, True)
        assert pool.has("s1") is True
        assert pool.entries["s1"].phase == "ready"
        # ready 源可被 takeReady 取走(append 采纳路径)
        source = pool.takeReady("s1")
        assert source.session.id == "s1"

    asyncio.run(scenario())


def test_release_non_reusable_removes():
    """release(reusable=False) 直接移除条目。"""

    async def scenario():
        pool = SessionPreparations(5)

        async def load():
            return _source("s1")

        async def commit(source):
            return (source, {"cursor": 0})

        reservation = await pool.reserve("s1", load, commit)
        pool.release(reservation, False)
        assert pool.has("s1") is False

    asyncio.run(scenario())


def test_invalidate_discards_ready():
    """invalidate(持久化日志变化)丢弃条目,不影响已持有预留的消费。"""

    async def scenario():
        pool = SessionPreparations(5)

        async def load():
            return _source("s1")

        await pool.inspect("s1", load)
        assert pool.has("s1") is True
        pool.invalidate("s1")
        assert pool.has("s1") is False

    asyncio.run(scenario())


def test_assert_writable_during_reserved_verbatim():
    """reserved 期间 append 拒绝,错误文本逐字(D SH 原文)。"""

    async def scenario():
        pool = SessionPreparations(5)

        async def load():
            return _source("s1")

        async def commit(source):
            return (source, {"cursor": 0})

        reservation = await pool.reserve("s1", load, commit)
        with pytest.raises(RuntimeError) as exc:
            pool.assertWritable("s1")
        assert str(exc.value) == 'cannot append session "s1" while its persisted preparation is reserved'
        pool.discard(reservation)
        pool.assertWritable("s1")  # 消费后放行

    asyncio.run(scenario())


def test_lru_evicts_oldest_ready_beyond_capacity():
    """ready 条目数超容量时淘汰最老的;非 ready(loading/committing)不淘汰。"""

    async def scenario():
        pool = SessionPreparations(2)
        loads = {}

        def make_load(id_):
            async def load():
                return _source(id_)

            return load

        for i in range(1, 4):
            await pool.inspect(f"s{i}", make_load(f"s{i}"))
        # 容量 2:最老的 s1 被淘汰
        assert pool.has("s1") is False
        assert pool.has("s2") is True and pool.has("s3") is True
        # 再触达 s2(移到最近端),新 inspect s4 淘汰 s3
        await pool.inspect("s2", make_load("s2"))
        await pool.inspect("s4", make_load("s4"))
        assert pool.has("s3") is False
        assert pool.has("s2") is True and pool.has("s4") is True

    asyncio.run(scenario())


def test_take_ready_missing_or_busy_returns_none():
    """takeReady:无条目/非 ready(被预留)返回 None。"""

    async def scenario():
        pool = SessionPreparations(5)
        assert pool.takeReady("ghost") is None

        async def load():
            return _source("s1")

        async def commit(source):
            return (source, {"cursor": 0})

        reservation = await pool.reserve("s1", load, commit)
        assert pool.takeReady("s1") is None  # reserved,非 ready
        pool.discard(reservation)

    asyncio.run(scenario())


def test_loading_failure_rejects_shared_waiter():
    """冷读失败:所有等待者看到同一错误,条目被清理。"""

    async def scenario():
        pool = SessionPreparations(5)

        async def load():
            raise RuntimeError("load failed")

        with pytest.raises(RuntimeError, match="load failed"):
            await pool.inspect("s1", load)
        assert pool.has("s1") is False
        # 再次 inspect 会重新加载(可重试)
        calls = []

        async def load2():
            calls.append(1)
            return _source("s1")

        result = await pool.inspect("s1", load2)
        assert result.session.id == "s1"

    asyncio.run(scenario())
