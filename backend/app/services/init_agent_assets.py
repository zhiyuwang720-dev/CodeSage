import logging
from typing import Any, Dict, List

from app.services.report_template_file_service import ReportTemplateFileService
from app.services.skill.file_service import AGENT_TYPES, SkillFileService
from app.services.task_report_service import DEFAULT_REPORT_TEMPLATE

logger = logging.getLogger(__name__)

# 06-P7: 默认绑定收敛到 review:* 视角。code-audit-finding(漏洞审计技能内容)改绑到
# review:security; architecture/quality 暂无配套技能内容, 空绑(预留新 slug)。
# finding/audit_chat 等旧 agent 已退役, 不再为其维护默认绑定。
DEFAULT_AGENT_SKILLS: Dict[str, List[Dict[str, Any]]] = {
    "review:security": [
        {
            "slug": "code-audit-finding",
            "always_include": True,
            "sort_order": 0,
            "match_keywords": [
                "auth",
                "authorization",
                "business-logic",
                "code-audit",
                "idor",
                "security",
                "source-review",
            ],
        }
    ],
    "review:architecture": [],
    "review:quality": [],
}

DEFAULT_REPORT_TEMPLATE_SLUG = "report-template"
DEFAULT_REPORT_TEMPLATE_NAME = "Default PR Audit Report"
DEFAULT_REPORT_TEMPLATE_DESCRIPTION = "CodeSage default PR audit report template."


def _binding_exists(agent_type: str, slug: str) -> bool:
    payload = SkillFileService.get_agent_bindings(agent_type)
    normalized_slug = SkillFileService.slugify(slug)
    return any(
        SkillFileService.slugify(item.get("slug", "")) == normalized_slug
        for item in payload.get("skills", [])
    )


async def init_skill_bindings() -> List[str]:
    slugs: List[str] = []
    for agent_type in AGENT_TYPES:
        SkillFileService.ensure_agent_bindings(agent_type)

    for agent_type, skills in DEFAULT_AGENT_SKILLS.items():
        for skill_spec in skills:
            slug = SkillFileService.slugify(skill_spec["slug"])
            if not SkillFileService.skill_file(slug).exists():
                logger.warning("Bundled skill '%s' is missing from local skill_library; skipping default binding.", slug)
                continue
            if not _binding_exists(agent_type, slug):
                SkillFileService.upsert_binding(
                    agent_type,
                    slug,
                    enabled=True,
                    always_include=bool(skill_spec.get("always_include", False)),
                    sort_order=int(skill_spec.get("sort_order", 0)),
                    match_keywords=list(skill_spec.get("match_keywords", [])),
                    match_config=dict(skill_spec.get("match_config", {})),
                )
            slugs.append(slug)

    SkillFileService.sync_all()
    return slugs


async def init_report_templates() -> str:
    items = ReportTemplateFileService.list_templates()
    if not items:
        ReportTemplateFileService.write_template(
            slug=DEFAULT_REPORT_TEMPLATE_SLUG,
            name=DEFAULT_REPORT_TEMPLATE_NAME,
            description=DEFAULT_REPORT_TEMPLATE_DESCRIPTION,
            content=DEFAULT_REPORT_TEMPLATE,
            report_type="audit_report",
            output_format="markdown",
            variables={
                "summary": "Execution summary",
                "findings": "Findings",
                "remediation": "Remediation",
            },
            metadata_json={"seeded_by": "init_agent_assets"},
            is_default=True,
            is_system=True,
            is_active=True,
            sort_order=0,
        )
        logger.info("Created default filesystem report template")
        return DEFAULT_REPORT_TEMPLATE_SLUG

    default_slug = None
    for item in items:
        if item.get("is_default"):
            default_slug = item["slug"]
            break

    if default_slug is None:
        ReportTemplateFileService.clear_default_flags()
        ReportTemplateFileService.write_template(
            slug=DEFAULT_REPORT_TEMPLATE_SLUG,
            name=DEFAULT_REPORT_TEMPLATE_NAME,
            description=DEFAULT_REPORT_TEMPLATE_DESCRIPTION,
            content=DEFAULT_REPORT_TEMPLATE,
            report_type="audit_report",
            output_format="markdown",
            variables={
                "summary": "Execution summary",
                "findings": "Findings",
                "remediation": "Remediation",
            },
            metadata_json={"seeded_by": "init_agent_assets"},
            is_default=True,
            is_system=True,
            is_active=True,
            sort_order=0,
        )
        logger.info("Refreshed default filesystem report template")
        return DEFAULT_REPORT_TEMPLATE_SLUG

    return default_slug


async def init_agent_assets(db: Any = None) -> None:
    del db
    SkillFileService.ensure_agent_roots()
    await init_report_templates()
    await init_skill_bindings()
