"""Plugin kernel (phase 21): Python port of Cordis.

九文件照译 vendor/cordis/src,五概念:
- plugin-as-Service(Service 基类 + RegistryService)
- context-as-service-repository(Context + ReflectService)
- inject(Fiber 依赖声明)
- typed events(EventsService 五派发模式)
- reversible effects(Fiber.effect,卸载逆序清理)

映射差异见 docs/modules/21-plugin-kernel.md 映射表。
"""

from .context import Context
from .events import EventsService, Hook, is_bailed
from .fiber import CordisError, Fiber, FiberState, ValidationError, resolve_config
from .loader import Loader, _interpolate
from .logger import Logger, LoggerService, Message
from .reflect import Impl, ReflectService
from .registry import RegistryService, Runtime, resolve_inject
from .service import Service
from .utils import DisposableList

__all__ = [
    "Context",
    "CordisError",
    "DisposableList",
    "EventsService",
    "Fiber",
    "FiberState",
    "Hook",
    "Impl",
    "Loader",
    "Logger",
    "LoggerService",
    "Message",
    "ReflectService",
    "RegistryService",
    "Runtime",
    "Service",
    "ValidationError",
    "is_bailed",
    "resolve_config",
    "resolve_inject",
]
