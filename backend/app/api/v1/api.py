"""v1 路由聚合 —— Phase 1 L4 精简移植

保留 12 个 PR 审查链路端点; 删除 8 个 legacy 端点:
tasks(旧审计任务) / scan(即时扫描) / database / agent_direct_audit /
embedding_config(RAG) / vulnerabilities(受管漏洞) / checkmarx / one_click_cve
"""
from fastapi import APIRouter

from app.api.v1.endpoints import (
    agent_tasks,
    audit_sessions,
    auth,
    config,
    members,
    projects,
    prompts,
    report_templates,
    rules,
    skills,
    ssh_keys,
    users,
)

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(projects.router, prefix="/projects", tags=["projects"])
api_router.include_router(members.router, prefix="/projects", tags=["members"])
api_router.include_router(config.router, prefix="/config", tags=["config"])
api_router.include_router(prompts.router, prefix="/prompts", tags=["prompts"])
api_router.include_router(rules.router, prefix="/rules", tags=["rules"])
api_router.include_router(agent_tasks.router, prefix="/agent-tasks", tags=["agent-tasks"])
api_router.include_router(audit_sessions.router, prefix="/audit-sessions", tags=["audit-sessions"])
api_router.include_router(ssh_keys.router, prefix="/ssh-keys", tags=["ssh-keys"])
api_router.include_router(skills.router, prefix="/skills", tags=["skills"])
api_router.include_router(report_templates.router, prefix="/report-templates", tags=["report-templates"])
