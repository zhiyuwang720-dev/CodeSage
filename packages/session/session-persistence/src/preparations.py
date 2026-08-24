"""未发布 Session 的有界共享与独占预留。

DSH preparations.ts 的 Python 移植。职责:协调器的冷读共享池
(同一 id 的并发 inspect/reserve 共享一次后端加载)、准备好条目的
LRU 淘汰,以及「提交后置」的独占预留 —— 后端持久化修复先提交,
发布候选才进入 reserved 阶段,期间同一身份的 append 被拒绝
(assertWritable)。

阶段机:loading(冷读中)→ ready(可复用)→ committing(预留中,
后端提交中)→ reserved(独占持有,等待 attach/discard/release)。

与 Node 的差异(注释即文档):
- AbortSignal 取消参数在 Python 侧取消(无原生等价物);并发观察者
  共享同一加载,取消仅影响观察者自身的等待 —— 语义由调用方在
  编排层自行处理。
- ``Promise.withResolvers`` → 本包的 Deferred(write_behind 提供,
  惰性 asyncio.Future)。每个条目挂一个 result Deferred;提交路径
  的等待者挂在 reservationSettled 上,由 makeReady/remove 落定。
- 英文错误消息保留逐字(与 DSH 一致)。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Generic, TypeVar

from .write_behind import Deferred

Source = TypeVar("Source")
CommitState = TypeVar("CommitState")

#: 准备条目阶段:loading(冷读中)/ ready(可复用)/ committing(提交中)/ reserved(独占)。
PreparationPhase = str


class _PreparationEntry(Generic[Source, CommitState]):
    """池内一条准备条目的可变状态(DSH 对象字面量的等价物)。"""

    __slots__ = ("id", "result", "phase", "source", "reservation", "reservationSettled", "settleReservation")

    def __init__(self, id: str) -> None:
        self.id = id
        self.result: Any = None  # Deferred.promise,entryFor 时挂上
        self.phase: PreparationPhase = "loading"
        self.source: Source | None = None
        self.reservation: Any = None
        self.reservationSettled: Any = None  # 提交路径等待者共用的 Deferred.promise
        self.settleReservation: Callable[[], None] | None = None  # 落定等待者的解析函数


class SessionPreparationReservation(Generic[Source, CommitState]):
    """一次独占持有的已准备源及其已提交的持久化状态。"""

    __slots__ = ("entry", "source", "state")

    def __init__(self, entry: _PreparationEntry, source: Source, state: CommitState) -> None:
        self.entry = entry
        self.source = source
        self.state = state


class SessionPreparations(Generic[Source, CommitState]):
    """协调器级冷读共享、独占预留与 ready 条目 LRU。

    Source 的契约:必须持有 ``session`` 属性(DSH 的 PreparedSource
    形状);对 PreparedSource 的语义校验由协调器负责。
    """

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.entries: dict[str, _PreparationEntry] = {}

    # --- 观察与共享 ---

    def has(self, id: str) -> bool:
        """池是否知道一个未发布身份。"""
        return id in self.entries

    async def inspect(self, id: str, load: Callable[[], Any]) -> Source:
        """观察一个已准备的源,同一 id 共享一次进行中的冷读。"""
        entry = self.entryFor(id, load)
        loaded = await entry.result
        source = entry.source if entry.source is not None else loaded
        if self.entries.get(id) is entry and entry.phase == "ready":
            self.touch(entry)
        return source

    async def reserve(
        self,
        id: str,
        load: Callable[[], Any],
        commit: Callable[[Source], Any],
    ) -> SessionPreparationReservation | None:
        """提交其挂起的持久化修复后,预留一个 ready 源。

        commit 返回 ``(source, state)`` 表示提交成功并给出新源与游标
        状态;返回 None 表示条目被无效化(持久化日志已变化)。期间同一
        身份被 assertWritable 拒绝写。
        """
        entry = self.entryFor(id, load)
        await entry.result
        while self.entries.get(id) is entry and entry.phase != "ready":
            settled = entry.reservationSettled
            # committing/reserved 转移必然同步安装等待者(见下),此处
            # 是防御性断言:等待者缺失意味着准备机状态损坏。
            if settled is None:
                raise RuntimeError(f'session "{id}" preparation lost its reservation waiter')
            await settled
        if self.entries.get(id) is not entry:
            return None
        assert entry.source is not None  # 已 ready ⇒ 源必然就位(见 entryFor)
        source = entry.source
        reservationSettled = Deferred()
        entry.phase = "committing"
        entry.reservationSettled = reservationSettled.promise
        entry.settleReservation = reservationSettled.resolve
        try:
            committed = await commit(source)
        except BaseException:
            self.remove(entry)
            raise
        if committed is None:
            self.remove(entry)
            return None
        entry.source = committed[0]
        if self.entries.get(id) is not entry:
            return None
        reservation = SessionPreparationReservation(entry, committed[0], committed[1])
        entry.phase = "reserved"
        entry.reservation = reservation
        return reservation

    # --- 预留的消费 ---

    def reservationFor(self, session) -> SessionPreparationReservation | None:
        """取回用于发布的精确预留,拒绝别名。

        session 必须与预留持有的源是同一对象(发布候选的精确性),
        否则说明持久化状态已拥有该身份 —— 抛错而非放行。
        """
        entry = self.entries.get(session.id)
        if entry is None:
            return None
        if (
            entry.phase == "reserved"
            and entry.source is not None
            and entry.source.session is session
            and entry.reservation is not None
        ):
            return entry.reservation
        raise RuntimeError(
            f'cannot publish session "{session.id}": persisted state already owns this identity'
        )

    def attach(self, reservation: SessionPreparationReservation) -> None:
        """在其精确 Session 挂载后消费一个预留。"""
        entry = reservation.entry
        if self.entries.get(entry.id) is not entry or entry.reservation is not reservation:
            raise RuntimeError(f'session "{entry.id}" preparation is no longer reserved')
        self.remove(entry)

    def discard(self, reservation: SessionPreparationReservation) -> None:
        """消费一个调用方只需要已提交检查结果的预留。"""
        entry = reservation.entry
        if self.entries.get(entry.id) is not entry or entry.reservation is not reservation:
            return
        self.remove(entry)

    def release(self, reservation: SessionPreparationReservation, reusable: bool) -> None:
        """把一个可复用的未发布预留还给 ready LRU。"""
        entry = reservation.entry
        if (
            self.entries.get(entry.id) is not entry
            or entry.reservation is not reservation
            or entry.phase != "reserved"
        ):
            return
        if not reusable:
            self.remove(entry)
            return
        entry.reservation = None
        self.makeReady(entry)

    # --- 无效化与写保护 ---

    def invalidate(self, id: str) -> None:
        """持久化日志变化后丢弃一个已准备的视图。"""
        entry = self.entries.get(id)
        if entry is not None:
            self.remove(entry)

    def discardReady(self, id: str, expected: Source) -> str:
        """丢弃一个精确的过期 ready 源,不打扰独占持有者。

        返回 'discarded'(已丢弃)/ 'retained'(被预留持有)/ 'missing'(不存在)。
        """
        entry = self.entries.get(id)
        if entry is None or entry.source is not expected:
            return "missing"
        if entry.phase != "ready":
            return "retained"
        self.remove(entry)
        return "discarded"

    def assertWritable(self, id: str) -> None:
        """未发布 Session 独占预留该身份期间拒绝写入。"""
        phase = self.entries.get(id).phase if id in self.entries else None
        if phase == "committing" or phase == "reserved":
            raise RuntimeError(
                f'cannot append session "{id}" while its persisted preparation is reserved'
            )

    def takeReady(self, id: str) -> Source | None:
        """移除一个已完成条目的 ready 源(供已串行化的 append 采纳)。"""
        entry = self.entries.get(id)
        if entry is None or entry.phase != "ready" or entry.source is None:
            return None
        self.remove(entry)
        return entry.source

    # --- 条目生命周期 ---

    def entryFor(self, id: str, load: Callable[[], Any]) -> _PreparationEntry:
        """取或建条目;同一 id 的并发观察共享一次冷读。

        load 立即启动(同一 tick 的串行化 append 排队在本次读取之后),
        条目进入 ready 后才落定 result Deferred。
        """
        existing = self.entries.get(id)
        if existing is not None:
            return existing
        deferred = Deferred()
        entry = _PreparationEntry(id)
        entry.result = deferred.promise
        self.entries[id] = entry
        try:
            loading = load()
        except BaseException as error:
            self.remove(entry)
            deferred.reject(error)
            return entry

        async def _on_loaded() -> None:
            try:
                source = await loading
            except BaseException as error:
                self.remove(entry)
                deferred.reject(error)
                return
            if self.entries.get(id) is entry:
                entry.source = source
                self.makeReady(entry)
            deferred.resolve(source)

        # entryFor 仅经 inspect/reserve(异步方法)触达,运行中循环必然存在;
        # 装载协程立即挂载,同一 tick 的串行化 append 因此排在本次读取之后。
        asyncio.ensure_future(_on_loaded())
        return entry

    def makeReady(self, entry: _PreparationEntry) -> None:
        """条目进入 ready 并落定所有提交路径等待者。"""
        if self.entries.get(entry.id) is not entry:
            return
        entry.phase = "ready"
        settle = entry.settleReservation
        entry.reservationSettled = None
        entry.settleReservation = None
        if settle is not None:
            settle()
        self.touch(entry)

    def remove(self, entry: _PreparationEntry) -> None:
        """从池中移除条目并落定其提交路径等待者(存在时)。"""
        if self.entries.get(entry.id) is not entry:
            return
        del self.entries[entry.id]
        settle = entry.settleReservation
        entry.reservationSettled = None
        entry.settleReservation = None
        if settle is not None:
            settle()

    def touch(self, entry: _PreparationEntry) -> None:
        """LRU 触达:条目移到最近端,超出容量则淘汰最老的 ready 条目。"""
        del self.entries[entry.id]
        self.entries[entry.id] = entry
        readyCount = sum(1 for candidate in self.entries.values() if candidate.phase == "ready")
        if readyCount <= self.capacity:
            return
        for id, candidate in list(self.entries.items()):
            if candidate.phase != "ready":
                continue
            del self.entries[id]
            return
