from .memory_runtime import RuntimeMemoryManager, build_memory_message
from .models import AgentRuntimeState, InvokedSkillState, SessionRuntimeState
from .permission_runtime import RuntimePermissionRuntime, ToolPermissionDecision
# 06-P3: 技能域已收敛至 services/skill/; 此处仅保留 pkg 级再导出以免破坏既有引用。
from app.services.skill.runtime import SkillInvocationRuntime
from app.services.skill.scheduler import SkillDiscoveryScheduler
from .tool_runtime import (
    RuntimeTool,
    StreamingToolExecutor,
    ToolExecutionContext,
    ToolExecutionUpdate,
    ToolOrchestrator,
    ToolRegistry,
    build_runtime_tool,
)


def build_runtime_tool_registry(*args, **kwargs):
    from .runtime_tool_registry import build_runtime_tool_registry as _build_runtime_tool_registry

    return _build_runtime_tool_registry(*args, **kwargs)


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
    "ToolPermissionDecision",
    "ToolRegistry",
    "build_runtime_tool",
]
