"""Load persisted PR analysis prompts and rules for an injected LLM policy port."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.nodes.pr_review.persistence.prompt_models import PromptTemplate
from app.nodes.pr_review.persistence.rule_models import AuditRuleSet


async def load_analysis_policy(
    db_session,
    *,
    rule_set_id: str | None,
    prompt_template_id: str | None,
    use_default_template: bool,
    output_language: str,
) -> tuple[str | None, list[dict] | None]:
    custom_prompt = None
    rules = None
    is_chinese = output_language.lower().startswith("zh")

    if prompt_template_id:
        result = await db_session.execute(
            select(PromptTemplate).where(PromptTemplate.id == prompt_template_id)
        )
        template = result.scalar_one_or_none()
        if template:
            custom_prompt = template.content_zh if is_chinese else template.content_en
    elif use_default_template:
        result = await db_session.execute(
            select(PromptTemplate).where(
                PromptTemplate.is_default == True,  # noqa: E712
                PromptTemplate.is_active == True,  # noqa: E712
                PromptTemplate.template_type == "system",
            )
        )
        template = result.scalar_one_or_none()
        if template:
            custom_prompt = template.content_zh if is_chinese else template.content_en

    if rule_set_id:
        result = await db_session.execute(
            select(AuditRuleSet)
            .options(selectinload(AuditRuleSet.rules))
            .where(AuditRuleSet.id == rule_set_id)
        )
        rule_set = result.scalar_one_or_none()
        if rule_set and getattr(rule_set, "rules", None):
            rules = [
                {
                    "rule_code": rule.rule_code,
                    "name": rule.name,
                    "description": rule.description,
                    "category": rule.category,
                    "severity": rule.severity,
                    "custom_prompt": rule.custom_prompt,
                    "enabled": rule.enabled,
                }
                for rule in rule_set.rules
                if getattr(rule, "enabled", True)
            ]
    return custom_prompt, rules
