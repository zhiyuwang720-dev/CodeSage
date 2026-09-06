from __future__ import annotations

import pytest_asyncio

from app.db.session import engine


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine_between_test_loops():
    """pytest-asyncio 的 function loop 下不跨用 asyncpg 连接。"""
    yield
    await engine.dispose()

