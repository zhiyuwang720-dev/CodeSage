"""cordis — Cordis 插件内核的 Python 移植版。

核心八文件照译 vendor/cordis/src,五概念:
- plugin-as-Service(Service 基类 + RegistryService)
- context-as-service-repository(Context + ReflectService)
- inject(Fiber 依赖声明,拓扑激活)
- typed events(EventsService 五派发模式)
- reversible effects(Fiber.effect,卸载逆序清理)

完整 Loader + Include 见 cordis.loader / cordis.include。
映射差异见 docs/architecture-walkthrough.md。
"""

from .context import Context
from .events import EventsService, Hook, is_bailed
from .fiber import CordisError, Fiber, FiberState, ValidationError, resolve_config
from .include import ConfigFileError, Include, apply_entry_patches, entry_list_schema
from .loader import (
    Entry,
    EntryGroup,
    EntryTree,
    GlobalRealm,
    Group,
    Loader,
    LoaderEntryError,
    LocalRealm,
    Realm,
    evaluate,
    interpolate,
    is_js_expr,
)
from .logger import Logger, LoggerService, Message
from .reflect import Impl, ReflectService
from .registry import RegistryService, Runtime, resolve_inject
from .service import Service
from .utils import AggregateError, DisposableList

__all__ = [
    "AggregateError",
    "ConfigFileError",
    "Context",
    "CordisError",
    "DisposableList",
    "Entry",
    "EntryGroup",
    "EntryTree",
    "EventsService",
    "Fiber",
    "FiberState",
    "GlobalRealm",
    "Group",
    "Hook",
    "Impl",
    "Include",
    "Loader",
    "LoaderEntryError",
    "LocalRealm",
    "Logger",
    "LoggerService",
    "Message",
    "Realm",
    "ReflectService",
    "RegistryService",
    "Runtime",
    "Service",
    "ValidationError",
    "apply_entry_patches",
    "entry_list_schema",
    "evaluate",
    "interpolate",
    "is_bailed",
    "is_js_expr",
    "resolve_config",
    "resolve_inject",
]
