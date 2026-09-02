from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings


def _sqlite_enable_wal(dbapi_connection, connection_record):
    """SQLite 并发写容忍度: WAL + busy_timeout。

    PR review 运行时与 EventManager 通过各自 session 并发写库(agent_events/
    audit_sessions), 默认 rollback journal 下 writer 会被其他 session 的读事务
    阻塞报 database is locked。WAL 允许读-写并发, busy_timeout 让写锁等待而非立即失败。
    """
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()
    except Exception:
        # 非 SQLite 后端(Postgres 等)无此 PRAGMA, 忽略
        pass


_engine_kwargs: dict = {}
if settings.DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"timeout": 30}

engine = create_async_engine(settings.DATABASE_URL, echo=False, future=True, **_engine_kwargs)
if settings.DATABASE_URL.startswith("sqlite"):
    event.listen(engine.sync_engine, "connect", _sqlite_enable_wal)

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
    sync_kwargs: dict = {}
    if settings.DATABASE_URL.startswith("sqlite"):
        sync_kwargs["connect_args"] = {"timeout": 30}
    sync_engine = create_engine(
        _coerce_sync_database_url(settings.DATABASE_URL), echo=False, future=True, **sync_kwargs
    )
    if settings.DATABASE_URL.startswith("sqlite"):
        event.listen(sync_engine, "connect", _sqlite_enable_wal)
    return sessionmaker(bind=sync_engine, expire_on_commit=False)


@lru_cache(maxsize=1)
def get_pr_review_sync_session_factory():
    """PR review 运行时专用同步会话工厂: 审计会话落到独立 SQLite 文件。

    三视角运行时经 sync 引擎写 audit_sessions, 而 EventManager/FastAPI 经 async
    引擎(aiosqlite 单工作线程)写 agent_events。两者若写同一主库文件, 并发写事务在
    WAL 下会形成 RESERVED↔EXCLUSIVE 写锁循环: 事件循环线程被同步 sqlite 调用阻塞,
    aiosqlite 工作线程在锁上排队, 整个服务挂死(health 无响应, 实测复现)。
    隔到独立文件后双引擎各自单写, busy_timeout 即可串行化, 服务保持响应。
    """
    from pathlib import Path

    from app.db.base import Base

    main_path = make_url(settings.DATABASE_URL).database or "eval_runtime.db"
    audit_path = str(Path(main_path).parent / "audit_runtime.db")
    sync_engine = create_engine(
        f"sqlite:///{audit_path}", echo=False, future=True, connect_args={"timeout": 30}
    )
    event.listen(sync_engine, "connect", _sqlite_enable_wal)
    # 独立文件的审计表不会随主库 create_all 生成, 这里就地建全量表(审计用表齐全,
    # 多余表空置无害)。create_all 幂等, 该工厂 lru_cache 只会执行一次。
    Base.metadata.create_all(bind=sync_engine)
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
