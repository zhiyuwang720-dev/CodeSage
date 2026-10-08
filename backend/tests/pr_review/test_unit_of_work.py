import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork


@pytest.fixture
async def database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE records (id INTEGER PRIMARY KEY)"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def count(factory):
    async with factory() as session:
        return await session.scalar(text("SELECT COUNT(*) FROM records"))


@pytest.mark.asyncio
async def test_explicit_commit_and_single_use(database):
    uow = SqlAlchemyUnitOfWork(database)
    with pytest.raises(RuntimeError):
        _ = uow.session
    async with uow:
        await uow.session.execute(text("INSERT INTO records VALUES (1)"))
        await uow.commit()
        with pytest.raises(RuntimeError):
            await uow.commit()
    assert await count(database) == 1
    with pytest.raises(RuntimeError):
        async with uow:
            pass


@pytest.mark.asyncio
async def test_no_commit_rolls_back_even_after_flush(database):
    async with SqlAlchemyUnitOfWork(database) as uow:
        await uow.session.execute(text("INSERT INTO records VALUES (1)"))
        await uow.session.flush()
    assert await count(database) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("original"), asyncio.CancelledError()])
async def test_exception_and_cancellation_roll_back(database, error):
    with pytest.raises(type(error)):
        async with SqlAlchemyUnitOfWork(database) as uow:
            await uow.session.execute(text("INSERT INTO records VALUES (1)"))
            raise error
    assert await count(database) == 0


@pytest.mark.asyncio
async def test_constraint_failure_rolls_back_all_writes(database):
    with pytest.raises(Exception, match="UNIQUE"):
        async with SqlAlchemyUnitOfWork(database) as uow:
            await uow.session.execute(text("INSERT INTO records VALUES (1)"))
            await uow.session.execute(text("INSERT INTO records VALUES (1)"))
            await uow.commit()
    assert await count(database) == 0


@pytest.mark.asyncio
async def test_task_cancellation_rolls_back_before_completion(database):
    started = asyncio.Event()

    async def write_and_wait():
        async with SqlAlchemyUnitOfWork(database) as uow:
            await uow.session.execute(text("INSERT INTO records VALUES (1)"))
            started.set()
            await asyncio.Event().wait()
            await uow.commit()

    task = asyncio.create_task(write_and_wait())
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await count(database) == 0


@pytest.mark.asyncio
async def test_borrowed_session_remains_usable(database):
    async with database() as session:
        async with SqlAlchemyUnitOfWork.from_session(session) as uow:
            await uow.session.execute(text("INSERT INTO records VALUES (1)"))
            await uow.commit()
        assert await session.scalar(text("SELECT COUNT(*) FROM records")) == 1


@pytest.mark.asyncio
async def test_failed_commit_rolls_back_and_closes_scope():
    events = []

    class Session:
        async def commit(self):
            events.append("commit")
            raise RuntimeError("commit failed")

        async def rollback(self):
            events.append("rollback")

    @asynccontextmanager
    async def factory():
        try:
            yield Session()
        finally:
            events.append("close")

    with pytest.raises(RuntimeError, match="commit failed"):
        async with SqlAlchemyUnitOfWork(factory) as uow:
            await uow.commit()
    assert events == ["commit", "rollback", "close"]
