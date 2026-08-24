"""session-persistence/src —— 持久化编排实现。

导出清单照 DSH index.ts 的公共面:契约(SessionPersistence 抽象服务
+ 词汇类型)、协调器(PersistenceCoordinator + PersistenceBackend)、
准备共享池、写合并控制器与修订号工厂。包根 __init__.py 转发这里。
"""

from . import coordinator, invariant, preparations, revision, write_behind
from .coordinator import (
    DEFAULT_PREPARED_SESSION_CACHE_SIZE,
    DEFAULT_WRITE_BATCH_MAX_DELAY_MS,
    MAX_WRITE_BATCH_DELAY_MS,
    LiveSessionState,
    PersistenceBackend,
    PersistenceCoordinator,
    PreparedSessionSource,
    SessionFormatUnsupportedError,
    SessionPersistenceCorruptionError,
    SessionState,
    StoredPrefix,
    StoredSuffix,
    sessionFormatVersionRefusal,
)
from .index import (
    SessionInspection,
    SessionLocation,
    SessionPersistence,
    SessionPersistenceSnapshot,
    SessionRawArtifact,
)
from .preparations import SessionPreparationReservation, SessionPreparations
from .revision import SessionPersistenceRevision
from .write_behind import Deferred, SessionWriteBehind

__all__ = [
    # 契约
    "SessionPersistence",
    "SessionPersistenceSnapshot",
    "SessionInspection",
    "SessionRawArtifact",
    "SessionLocation",
    # 协调器
    "PersistenceCoordinator",
    "PersistenceBackend",
    "StoredPrefix",
    "StoredSuffix",
    "PreparedSessionSource",
    "SessionState",
    "LiveSessionState",
    "SessionPersistenceCorruptionError",
    "SessionFormatUnsupportedError",
    "sessionFormatVersionRefusal",
    "DEFAULT_PREPARED_SESSION_CACHE_SIZE",
    "DEFAULT_WRITE_BATCH_MAX_DELAY_MS",
    "MAX_WRITE_BATCH_DELAY_MS",
    # 准备与写路径
    "SessionPreparations",
    "SessionPreparationReservation",
    "SessionWriteBehind",
    "Deferred",
    # 修订号
    "SessionPersistenceRevision",
]
