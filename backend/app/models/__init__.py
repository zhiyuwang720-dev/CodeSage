"""精简版 models 包 —— Phase 1 L0 移植(CodeSage 二次开发)

自 AutoCVE 移植,仅保留 PR 审查链路所需模型:
  user / user_config / project / prompt_template / audit_rule
  agent_task(AgentTask/AgentEvent/AgentFinding) / audit_session(runtime 会话全量落库)
被删冗余: audit(旧审计) / analysis(InstantAnalysis) / managed_vulnerability /
checkmarx_scan / one_click_cve(仅旧 worker 集成使用)
"""
from .user import User
from .user_config import UserConfig
from .project import Project, ProjectMember
from .prompt_template import PromptTemplate
from .audit_rule import AuditRule, AuditRuleSet
from .agent_task import (
    AgentEvent,
    AgentEventType,
    AgentFinding,
    AgentTask,
    AgentTaskPhase,
    AgentTaskStatus,
    FindingStatus,
    VulnerabilitySeverity,
    VulnerabilityType,
)
from .audit_session import (
    AuditArtifact,
    AuditCheckpoint,
    AuditCheckpointType,
    AuditHandoff,
    AuditMemory,
    AuditMemoryKind,
    AuditSession,
    AuditSessionMessage,
    AuditSessionTurn,
    AuditSkill,
    AuditSkillInvocation,
    AuditSkillInvocationStatus,
    AuditToolCall,
    AuditToolCallStatus,
)

__all__ = [
    "User",
    "UserConfig",
    "Project",
    "ProjectMember",
    "PromptTemplate",
    "AuditRule",
    "AuditRuleSet",
    "AgentTask",
    "AgentEvent",
    "AgentFinding",
    "AgentTaskStatus",
    "AgentTaskPhase",
    "AgentEventType",
    "VulnerabilitySeverity",
    "VulnerabilityType",
    "FindingStatus",
    "AuditArtifact",
    "AuditCheckpoint",
    "AuditCheckpointType",
    "AuditHandoff",
    "AuditMemory",
    "AuditMemoryKind",
    "AuditSession",
    "AuditSessionMessage",
    "AuditSessionTurn",
    "AuditSkill",
    "AuditSkillInvocation",
    "AuditSkillInvocationStatus",
    "AuditToolCall",
    "AuditToolCallStatus",
]
