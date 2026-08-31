from __future__ import annotations

from html import escape
from typing import Any, Iterable

from app.models.audit_session import AuditSkillInvocationStatus
from app.services.agent.skill_service import SkillService
from app.services.runtime_core.skill_mentions import ExplicitSkillMention
from app.services.runtime_core.skill_runtime import SkillInvocationRuntime


_BODY_STAGES = {"body", "references", "examples", "scripts", "full"}

EXPLICIT_SKILL_REMINDER = (
    "显式技能加载提醒：以下技能是由用户提示词、系统提示词或路由消息显式提及后自动加载的完整 SKILL.md。"
    "开始执行任务前必须先阅读并遵守其中的启动流程、必读资源和工具调用要求；"
    "如果 SKILL.md 要求继续读取 references、examples 或 scripts，请通过 Skill(action=\"read_resource\") 继续读取并留下审计记录。"
)


async def load_explicit_skill_injections(
    *,
    session_store,
    agent_type: str,
    session_id: str,
    mentions: Iterable[ExplicitSkillMention],
    skill_service: Any = SkillService,
) -> str:
    resolved_mentions = list(mentions or [])
    if not resolved_mentions:
        return ""

    runtime = SkillInvocationRuntime(
        session_store=session_store,
        agent_type=agent_type,
        skill_service=skill_service,
    )
    turn_id: str | None = None
    blocks: list[str] = []
    try:
        for mention in resolved_mentions:
            payload = _find_existing_body_payload(
                session_store=session_store,
                session_id=session_id,
                skill_ref=mention.skill_ref,
            )
            if payload is None:
                if turn_id is None:
                    turn_id = session_store.open_turn(
                        session_id,
                        model_name=f"{agent_type}-explicit-skill-loader",
                    )
                payload = await runtime.invoke(
                    session_id=session_id,
                    turn_id=turn_id,
                    skill_ref=mention.skill_ref,
                    action="body",
                    input_payload={
                        "action": "body",
                        "mention_source": mention.source,
                        "raw_mention": mention.raw_mention,
                        "mention_kind": mention.kind,
                    },
                    invocation_source="explicit_mention",
                )
            blocks.append(_format_explicit_skill_block(mention=mention, payload=payload))
    except Exception:
        if turn_id is not None:
            session_store.close_turn(turn_id, status="failed")
        raise
    if turn_id is not None:
        session_store.close_turn(turn_id, status="completed")

    if not blocks:
        return ""
    return "\n\n".join([EXPLICIT_SKILL_REMINDER, *blocks])


def _find_existing_body_payload(*, session_store, session_id: str, skill_ref: str) -> dict[str, Any] | None:
    if not _runtime_state_has_body(session_store=session_store, session_id=session_id, skill_ref=skill_ref):
        return None
    for invocation in reversed(session_store.list_skill_invocations(session_id)):
        if str(getattr(invocation, "skill_ref", "") or "").strip() != skill_ref:
            continue
        if str(getattr(invocation, "status", "") or "") != AuditSkillInvocationStatus.COMPLETED.value:
            continue
        output_payload = dict(getattr(invocation, "output_payload", None) or {})
        if _extract_skill_content(output_payload).strip():
            return output_payload
    return None


def _runtime_state_has_body(*, session_store, session_id: str, skill_ref: str) -> bool:
    runtime_state = session_store.load_runtime_state(session_id)
    for agent_state in runtime_state.agent_states.values():
        invoked = agent_state.invoked_skills.get(skill_ref)
        if invoked is not None and str(invoked.skill_stage or "") in _BODY_STAGES:
            return True
    return False


def _format_explicit_skill_block(*, mention: ExplicitSkillMention, payload: dict[str, Any]) -> str:
    skill_file = str(payload.get("skill_file") or payload.get("skill_file_path") or "").strip()
    content = _extract_skill_content(payload).strip()
    return "\n".join(
        [
            "<explicit_skill>",
            f"<skill_ref>{escape(mention.skill_ref)}</skill_ref>",
            f"<mention_source>{escape(mention.source)}</mention_source>",
            f"<raw_mention>{escape(mention.raw_mention)}</raw_mention>",
            f"<skill_file>{escape(skill_file)}</skill_file>",
            "<skill_md>",
            content,
            "</skill_md>",
            "</explicit_skill>",
        ]
    )


def _extract_skill_content(payload: dict[str, Any]) -> str:
    content = payload.get("content") or payload.get("body") or payload.get("markdown")
    if content is None:
        return str(payload)
    return str(content)
