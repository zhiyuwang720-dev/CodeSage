"""Hooks (phase 09): 配置驱动的外部事件钩子系统 —— 契约层。

S1 交付:types.py(HookInput/HookJSONOutput/HookSpec/HookAuditEvent + 校验器)
+ base.py(HookExecutor 协议 / HookResult / HookManager 协议)。
匹配解析(S2)、三执行体(S3/S4/S10)、HookManager 实现(S5)及接线在后续步骤。
"""

from .base import HookExecutor, HookManager, HookResult
from .types import (
    DEFAULT_TIMEOUTS,
    EVENTS,
    HOOK_OUTCOMES,
    HOOK_TYPES,
    IF_EVALUABLE_EVENTS,
    MATCHER_IGNORED_EVENTS,
    NOTIFICATION_TYPES,
    SCHEMA_HINT,
    HookAuditEvent,
    HookInput,
    HookJSONOutput,
    HookSpec,
    HookValidationError,
    is_notification_type,
)

__all__ = [
    "DEFAULT_TIMEOUTS",
    "EVENTS",
    "HOOK_OUTCOMES",
    "HOOK_TYPES",
    "IF_EVALUABLE_EVENTS",
    "MATCHER_IGNORED_EVENTS",
    "NOTIFICATION_TYPES",
    "SCHEMA_HINT",
    "HookAuditEvent",
    "HookExecutor",
    "HookInput",
    "HookJSONOutput",
    "HookManager",
    "HookResult",
    "HookSpec",
    "HookValidationError",
    "is_notification_type",
]
