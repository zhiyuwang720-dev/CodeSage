"""会话持久化/状态层(06-P5): 独立 services/session/, 不再依赖运行时编排。

布局:
- store.py        AuditSessionStore(消息/轮次/记忆/技能/工具/检查点等落库)
- state.py        SessionRuntimeState/AgentRuntimeState 等运行时状态模型
- interaction.py  InteractionRuntime(todo/提问/计划模式的纯状态操作)
"""
from app.services.session.state import (
    AgentRuntimeState,
    InvokedSkillState,
    SessionRuntimeState,
    build_legacy_agent_runtime_state,
    sync_legacy_agent_metadata_from_runtime_state,
)
from app.services.session.store import AuditSessionPersistenceError, AuditSessionStore
from app.services.session.interaction import InteractionRuntime

__all__ = [
    "AgentRuntimeState",
    "AuditSessionPersistenceError",
    "AuditSessionStore",
    "InteractionRuntime",
    "InvokedSkillState",
    "SessionRuntimeState",
    "build_legacy_agent_runtime_state",
    "sync_legacy_agent_metadata_from_runtime_state",
]
