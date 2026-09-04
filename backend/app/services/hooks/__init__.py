"""Hook 策略/执行层(06-P6): 独立 services/hooks/, 引擎 hook 语义收口于此。

布局:
- policy.py  Hook 策略裁决(collect_turn_hook_events / evaluate_stop_hook_policy /
              evaluate_post_tool_hook_policy), 面向 runtime loop/query_stop_hooks
- runtime.py SubprocessHookCommandRunner + HookExecutorRuntime(hook 事件执行载体)
"""
from app.services.hooks.policy import (
    collect_turn_hook_events,
    evaluate_post_tool_hook_policy,
    evaluate_stop_hook_policy,
)
from app.services.hooks.runtime import HookExecutorRuntime, SubprocessHookCommandRunner

__all__ = [
    "HookExecutorRuntime",
    "SubprocessHookCommandRunner",
    "collect_turn_hook_events",
    "evaluate_post_tool_hook_policy",
    "evaluate_stop_hook_policy",
]
