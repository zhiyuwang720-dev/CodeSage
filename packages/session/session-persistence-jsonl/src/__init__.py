"""session-persistence-jsonl/src —— JSONL 落盘后端实现。

导出清单照 DSH index.ts 的公共面:后端类 JsonlSessionPersistence
(同时实现 SessionPersistence 服务契约与 PersistenceBackend 存储
原语)、撕裂尾标记词汇 JsonlTornMarker 与修订号工厂 file_revision。
包根 __init__.py 转发这里。
"""

from . import format, index, win32
from .index import (
    DEFAULT_COMPRESSION,
    DEFAULT_PACK_CHUNKS,
    JsonlSessionPersistence,
    JsonlTornMarker,
    file_revision,
)

__all__ = [
    # 后端
    "JsonlSessionPersistence",
    "JsonlTornMarker",
    "file_revision",
    "DEFAULT_PACK_CHUNKS",
    "DEFAULT_COMPRESSION",
]
