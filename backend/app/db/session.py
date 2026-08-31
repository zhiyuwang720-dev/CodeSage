from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=False, future=True)

AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


def _coerce_sync_database_url(database_url: str) -> str:
    url = make_url(database_url)
    drivername = url.drivername
    driver_map = {
        "sqlite+aiosqlite": "sqlite",
        "postgresql+asyncpg": "postgresql+psycopg",
        "postgresql+psycopg_async": "postgresql+psycopg",
        "mysql+aiomysql": "mysql+pymysql",
        "mysql+asyncmy": "mysql+pymysql",
    }
    sync_driver = driver_map.get(drivername, drivername)
    return url.set(drivername=sync_driver).render_as_string(hide_password=False)


@lru_cache(maxsize=1)
def get_sync_session_factory():
    sync_engine = create_engine(_coerce_sync_database_url(settings.DATABASE_URL), echo=False, future=True)
    return sessionmaker(bind=sync_engine, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


@asynccontextmanager
async def async_session_factory():
    """Async context manager for creating database sessions"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
