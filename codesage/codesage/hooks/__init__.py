"""Hooks (phase 09): 配置驱动的外部事件钩子系统。

S1 契约层(types.py/base.py)、S2 匹配解析(_common.py)、S3 命令执行体(command.py)、
S4 HTTP 执行体(http.py)、S5 HookManager 执行引擎(registry.py)已交付;S10
prompt 执行体(prompt.py)与装配(assemble.py)在后续步骤。
"""

from .base import HookExecutor, HookManager, HookResult
from .registry import HookDispatchResult, HookManager, load_hook_manager
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
    "HookDispatchResult",
    "HookExecutor",
    "HookInput",
    "HookJSONOutput",
    "HookManager",
    "HookResult",
    "HookSpec",
    "HookValidationError",
    "is_notification_type",
    "load_hook_manager",
]
