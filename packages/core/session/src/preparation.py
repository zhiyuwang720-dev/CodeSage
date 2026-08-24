"""一个未发布 Session 在注册表公布前的所有权。

Session 的创建与公布之间有一段「准备期」:调用方持有它做设置,
可能放弃(回滚)也可能公布。这份所有权是**唯一的** —— 同一个
Session 不能被两个准备期同时持有。dispose 同步且幂等:provider
决定 release 是把 Session 还给缓存还是丢弃;公布可能先消费了
该状态,使回调成为 no-op。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class SessionPreparation:
    """一个确切未公布的 Session + 维持它可用的 provider 状态。"""

    __slots__ = ("session", "_release", "_released")

    def __init__(self, session, release: "Callable[[], None] | None" = None) -> None:
        self.session = session
        self._release = release
        self._released = False

    @staticmethod
    def create(session, options: "dict | None" = None) -> "SessionPreparation":
        """把一个未公布的 Session 包进一份准备期所有权。

        options 形如 {"release": callback}:公布或回滚后释放
        provider 持有的未公布状态。
        """
        release = options.get("release") if options else None
        return SessionPreparation(session, release)

    def dispose(self) -> None:
        """释放 provider 状态,恰一次(幂等)。"""
        if self._released:
            return
        self._released = True
        if self._release is not None:
            self._release()
