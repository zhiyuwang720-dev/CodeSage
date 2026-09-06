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
    """async driver → sync driver(asyncpg→psycopg), 供同步引擎复用同一 DATABASE_URL。"""
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
    """同步会话工厂(psycopg): bridge 运行时等 sync 写路径复用主库。"""
    sync_engine = create_engine(
        _coerce_sync_database_url(settings.DATABASE_URL), echo=False, future=True
    )
    return sessionmaker(bind=sync_engine, expire_on_commit=False)


@lru_cache(maxsize=1)
def get_pr_review_sync_session_factory():
    """PR review 运行时同步会话工厂: 与主库同库 + 幂等 create_all bootstrap。

    早期 SQLite 版把审计会话隔到独立 audit_runtime.db, 规避 sync/async 双引擎写同一
    SQLite 文件的 RESERVED↔EXCLUSIVE 锁死; Postgres 无单写者锁(MVCC), 同步/异步引擎
    可同库并发, 故收编为同一 Postgres 库。create_all 幂等, 该工厂 lru_cache 只执行一次,
    保证审计表(audit_sessions 等)在主库齐全。
    """
    from app.db.base import Base

    sync_engine = create_engine(
        _coerce_sync_database_url(settings.DATABASE_URL), echo=False, future=True
    )
    Base.metadata.create_all(bind=sync_engine)
    return sessionmaker(bind=sync_engine, expire_on_commit=False)


def create_all_schema() -> None:
    """启动自愈建表(11-P6): 对主库幂等 create_all, 防未来新表缺表事故复发。

    09 生产事故根因即主库 create_all 管理 + 无启动建表 → audit_stages 缺失。
    create_all 幂等且只在缺表时补建, 每次启动可安全调用; 用 sync psycopg 引擎
    (asyncpg→psycopg 由 _coerce_sync_database_url 承担)。
    """
    from app.db.base import Base

    sync_engine = create_engine(
        _coerce_sync_database_url(settings.DATABASE_URL), echo=False, future=True
    )
    Base.metadata.create_all(bind=sync_engine)
    sync_engine.dispose()


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
