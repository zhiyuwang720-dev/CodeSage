"""单活会话的有界写合并控制器。

DSH write-behind.ts 的 Python 移植。职责:一个活会话的挂起事件、
固定的批处理窗口、活动的耐久写、失败保留(保序重试)与显式
排空屏障。

与 Node 的语义差异(注释即文档):
- 定时器走 asyncio task(``asyncio.sleep`` 窗口);Node 的 setTimeout
  恒存在,而 Python 的定时器必须挂在运行中的事件循环上 —— 在
  无循环的同步上下文里 enqueue 不启动自动窗口(事件保留在队列),
  由显式 ``flush()`` 驱动落盘(协调器的耐久屏障也是显式调用方)。
- ``structuredClone(event)`` → 事件入队列时在顶层拷贝:事件本体
  在会话侧接纳时已深度冻结,嵌套值不可变,顶层拷贝足以让队列
  持有独立引用(消费方改不动它,队列之间也不共享可变子节点)。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any


class Deferred:
    """``Promise.withResolvers`` 等价物:延迟创建的 asyncio.Future。

    Python 的 asyncio.Future 必须挂在事件循环上;部分 deferred
    (preparations 的加载结果、本文件的排空屏障)可能在同步上下
    文里构造,所以 Future 惰性创建 —— 首次触达时才取运行中的
    循环。同一 helper 供本包三个模块复用。
    """

    __slots__ = ("_future",)

    def __init__(self) -> None:
        self._future: asyncio.Future | None = None

    def _get(self) -> asyncio.Future:
        if self._future is None:
            self._future = asyncio.get_running_loop().create_future()
        return self._future

    @property
    def promise(self) -> asyncio.Future:
        """等待侧挂的 Promise 视图(惰性创建)。"""
        return self._get()

    def resolve(self, value: Any = None) -> None:
        self._get().set_result(value)

    def reject(self, error: BaseException) -> None:
        self._get().set_exception(error)


async def _maybe_await(value: Any) -> Any:
    """await 一个可能是协程/可等待对象的结果(DSH 的 Promise|T 语义)。"""
    if inspect.isawaitable(value):
        return await value
    return value


class SessionWriteBehind:
    """一个活会话的挂起事件、固定批处理期限、活动写与排空屏障。"""

    __slots__ = (
        "options",
        "pending",
        "timer",
        "active",
        "barrier",
        "deadline_expired",
        "automatic_paused",
    )

    def __init__(self, options: dict) -> None:
        """options:max_delay_ms/write/report_background_failure 三键策略。"""
        self.options = options
        self.pending: list = []
        self.timer: asyncio.Task | None = None
        self.active: asyncio.Future | None = None
        self.barrier: Deferred | None = None
        self.deadline_expired = False
        self.automatic_paused = False

    @property
    def has_work(self) -> bool:
        """是否拥有排队事件或进行中的耐久写。"""
        return len(self.pending) > 0 or self.active is not None

    # --- 入队与自动窗口 ---

    def enqueue(self, event: dict) -> None:
        """拷贝一个事件进持久化自有的队列,空闲时启动固定期限。

        事件已深度冻结(会话侧接纳时),这里只做顶层拷贝让队列
        持有独立引用。
        """
        was_empty = len(self.pending) == 0
        self.pending.append(dict(event))
        if self.barrier is not None:
            return
        if self.automatic_paused:
            self.automatic_paused = False
            self.deadline_expired = False
            self._arm_timer()
        elif was_empty:
            self._arm_timer()

    def flush(self) -> asyncio.Future:
        """取消批处理等待,耐久排空到静默点;并发调用共享同一屏障。"""
        if self.barrier is not None:
            return self.barrier.promise
        self._cancel_timer()
        self.deadline_expired = False
        self.automatic_paused = False
        barrier = Deferred()
        self.barrier = barrier
        asyncio.ensure_future(self._drain_barrier(barrier))
        return barrier.promise

    def cancel_automatic_wait(self) -> None:
        """取消当前自动期限但不排空已保留的工作。"""
        self._cancel_timer()
        self.deadline_expired = False

    # --- 内部:窗口与后台写 ---

    def _arm_timer(self) -> None:
        """启动当前挂起前缀的一个固定窗口。

        无运行中的事件循环时不启动(见模块 docstring 的差异说明):
        事件保留,由显式 flush 驱动。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.timer = None
            return

        async def _on_timer() -> None:
            await asyncio.sleep(self.options["max_delay_ms"] / 1000)
            self._on_deadline()

        self.timer = loop.create_task(_on_timer())

    def _cancel_timer(self) -> None:
        if self.timer is None:
            return
        self.timer.cancel()
        self.timer = None

    def _on_deadline(self) -> None:
        """窗口到期:立即后台写,或记住活动写用掉了本窗口预算。"""
        self.timer = None
        if self.active is not None:
            self.deadline_expired = True
            return
        self._start_background()

    def _start_background(self) -> None:
        """启动一个分离的写:失败被报告并保留(保序重试)。"""
        active = self._start_write(True)
        active.add_done_callback(lambda t: self._continue_automatic())

    def _continue_automatic(self) -> None:
        """活动写结束后立即续写(预算已超),否则保留其窗口。"""
        if self.barrier is not None or len(self.pending) == 0:
            return
        if self.deadline_expired:
            self.deadline_expired = False
            self._start_background()

    async def _drain_barrier(self, barrier: Deferred) -> None:
        """等待重叠工作、排空到静默、落定共享屏障。"""
        try:
            overlapping = self.active
            if overlapping is not None:
                await asyncio.gather(overlapping, return_exceptions=True)
                self.automatic_paused = False
            while len(self.pending) > 0:
                await self._start_write(False)
        except BaseException as error:
            self.barrier = None
            barrier.reject(error)
            return
        # 在同一观察到空队列的任务里关闭对屏障的准入,再落定调用方:
        # 之后的 enqueue 启动自己的自动窗口,而不是滞留在一个已落定
        # 的屏障后面。
        self.barrier = None
        barrier.resolve()

    def _start_write(self, background: bool) -> asyncio.Future:
        """启动一个稳定挂起前缀的写,耐久失败时按序保留整批。"""
        batch = self.pending
        self.pending = []
        self._cancel_timer()
        self.deadline_expired = False

        async def _op() -> None:
            try:
                await _maybe_await(self.options["write"](batch))
            except BaseException as error:
                self.pending = batch + self.pending
                self._cancel_timer()
                self.deadline_expired = False
                self.automatic_paused = True
                if background:
                    self.options["report_background_failure"](error)
                raise
            finally:
                self.active = None

        active = asyncio.ensure_future(_op())
        self.active = active
        return active
