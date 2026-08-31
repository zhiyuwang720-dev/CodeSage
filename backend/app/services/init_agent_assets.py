import logging
from typing import Any, Dict, List

from app.services.report_template_file_service import ReportTemplateFileService
from app.services.skill_file_service import AGENT_TYPES, SkillFileService
from app.services.task_report_service import DEFAULT_REPORT_TEMPLATE

logger = logging.getLogger(__name__)

DEFAULT_AGENT_SKILLS: Dict[str, List[Dict[str, Any]]] = {
    "finding": [
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
    ]
}

LEGACY_FINDING_SKILLS: List[Dict[str, Any]] = [
    {
        "slug": "skill-dfyx-code-security-review",
        "match_keywords": ["auth", "idor", "access-control", "business-logic", "ssrf"],
    },
    {
        "slug": "code-security",
        "match_keywords": ["security", "injection", "auth", "idor", "ssrf", "deserialization"],
    },
]

AUDIT_CHAT_AGENT_TYPE = "audit_chat"

DEFAULT_REPORT_TEMPLATE_SLUG = "report-template"
DEFAULT_REPORT_TEMPLATE_NAME = "Default Vulnerability Report"
DEFAULT_REPORT_TEMPLATE_DESCRIPTION = "AutoCVE default final vulnerability report template."


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

    for skill_spec in LEGACY_FINDING_SKILLS:
        slug = SkillFileService.slugify(skill_spec["slug"])
        if SkillFileService.skill_file(slug).exists() and not _binding_exists("finding", slug):
            SkillFileService.upsert_binding(
                "finding",
                slug,
                enabled=True,
                always_include=True,
                sort_order=1,
                match_keywords=list(skill_spec.get("match_keywords", [])),
                match_config={},
            )
            slugs.append(slug)

    for slug in SkillFileService.list_skill_slugs():
        normalized_slug = SkillFileService.slugify(slug)
        if not _binding_exists(AUDIT_CHAT_AGENT_TYPE, normalized_slug):
            SkillFileService.upsert_binding(
                AUDIT_CHAT_AGENT_TYPE,
                normalized_slug,
                enabled=True,
                always_include=False,
                sort_order=10,
                match_keywords=[],
                match_config={},
            )
            slugs.append(normalized_slug)

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
            report_type="final_vulnerability_report",
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
            report_type="final_vulnerability_report",
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
