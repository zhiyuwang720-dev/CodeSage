"""A short, explicit-commit database unit of work.

Every participant uses the same session and must not commit independently.
This covers database writes only, not files, queues, or model calls.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncContextManager, Callable

from sqlalchemy.ext.asyncio import AsyncSession


SessionFactory = Callable[[], AsyncContextManager[AsyncSession]]


class SqlAlchemyUnitOfWork:
    def __init__(self, session_factory: SessionFactory):
        self._factory = session_factory
        self._scope = None
        self._session: AsyncSession | None = None
        self._entered = False
        self._committed = False

    @classmethod
    def from_session(cls, session: AsyncSession) -> "SqlAlchemyUnitOfWork":
        """Adopt the caller's transaction, but do not close its session.

        Compatibility entry point: the caller must give this operation exclusive
        ownership of the transaction, including any pending writes.
        """
        @asynccontextmanager
        async def borrowed():
            yield session

        return cls(borrowed)

    @property
    def session(self) -> AsyncSession:
        if self._session is None or self._committed:
            raise RuntimeError("UnitOfWork is not active")
        return self._session

    async def __aenter__(self) -> "SqlAlchemyUnitOfWork":
        if self._entered:
            raise RuntimeError("UnitOfWork cannot be reused or nested")
        self._entered = True
        self._scope = self._factory()
        self._session = await self._scope.__aenter__()
        return self

    async def commit(self) -> None:
        await self.session.commit()
        self._committed = True

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            if not self._committed:
                await self._session.rollback()
        finally:
            try:
                await self._scope.__aexit__(exc_type, exc, traceback)
            finally:
                self._session = None
