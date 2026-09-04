# 06-P5: 记忆/状态已独立至 services/{memory,session}/; 此处仅保留 pkg 级再导出以免破坏既有引用。
from app.services.memory.runtime import RuntimeMemoryManager, build_memory_message
from app.services.session.state import AgentRuntimeState, InvokedSkillState, SessionRuntimeState
from .permission_runtime import RuntimePermissionRuntime, ToolPermissionDecision
# 06-P3: 技能域已收敛至 services/skill/; 此处仅保留 pkg 级再导出以免破坏既有引用。
from app.services.skill.runtime import SkillInvocationRuntime
from app.services.skill.scheduler import SkillDiscoveryScheduler
# 06-P4: 工具族已收敛至 services/tooling/; tool_runtime 等旧模块已迁走。
# build_runtime_tool_registry 以惰性包装保持向后兼容(runtime_core 整体删除见 P6)。


def build_runtime_tool_registry(*args, **kwargs):
    from app.services.tooling.registry import build_runtime_tool_registry as _build_runtime_tool_registry

    return _build_runtime_tool_registry(*args, **kwargs)


_TOOLING_REEXPORTS = {
    "RuntimeTool",
    "StreamingToolExecutor",
    "ToolExecutionContext",
    "ToolExecutionUpdate",
    "ToolOrchestrator",
    "ToolRegistry",
    "build_runtime_tool",
    "match_runtime_event_hooks",
}


def __getattr__(name: str):
    # 06-P4 向后兼容再导出: 工具族符号惰性解析自 tooling.runtime(tool_runtime 已迁走)。
    if name in _TOOLING_REEXPORTS:
        from app.services.tooling.runtime import (
            StreamingToolExecutor,
            ToolExecutionContext,
            ToolExecutionUpdate,
            ToolOrchestrator,
            ToolRegistry,
            build_runtime_tool,
            match_runtime_event_hooks,
        )
        from app.services.contracts.tools import RuntimeTool

        _symbols = {
            "RuntimeTool": RuntimeTool,
            "StreamingToolExecutor": StreamingToolExecutor,
            "ToolExecutionContext": ToolExecutionContext,
            "ToolExecutionUpdate": ToolExecutionUpdate,
            "ToolOrchestrator": ToolOrchestrator,
            "ToolRegistry": ToolRegistry,
            "build_runtime_tool": build_runtime_tool,
            "match_runtime_event_hooks": match_runtime_event_hooks,
        }
        return _symbols[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "RuntimeMemoryManager",
    "build_memory_message",
    "AgentRuntimeState",
    "InvokedSkillState",
    "SessionRuntimeState",
    "RuntimePermissionRuntime",
    "ToolPermissionDecision",
    "build_runtime_tool_registry",
    "SkillDiscoveryScheduler",
    "SkillInvocationRuntime",
    "RuntimeTool",
    "StreamingToolExecutor",
    "ToolExecutionContext",
    "ToolExecutionUpdate",
    "ToolOrchestrator",
    "ToolRegistry",
    "build_runtime_tool",
    "match_runtime_event_hooks",
]
