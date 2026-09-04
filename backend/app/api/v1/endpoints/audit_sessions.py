from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api import deps
from app.core.config import settings
from app.core.encryption import decrypt_sensitive_data
from app.db.session import async_session_factory, get_db
from app.models.agent_task import AgentFinding, AgentTask, FindingStatus
from app.models.audit_session import (
    AuditHandoff,
    AuditCheckpoint,
    AuditMemory,
    AuditModelStreamAttempt,
    AuditSession,
    AuditSessionMessage,
    AuditSessionTurn,
    AuditSkill,
    AuditSkillInvocation,
    AuditToolCall,
)
from app.models.project import Project
from app.models.user import User
from app.services.review_runtime.bridge import FindingRuntimeBridge
from app.services.llm.service import LLMService
from app.services.runtime_core.runtime_guardrails import is_guardrails_enabled

logger = logging.getLogger(__name__)
router = APIRouter()


class AuditSessionResponse(BaseModel):
    id: str
    project_id: str
    task_id: Optional[str] = None
    runtime_stack: str
    state: str
    system_prompt: Optional[str] = None
    recon_payload: Optional[dict[str, Any]] = None
    guardrails_enabled: bool = False
    created_at: datetime
    updated_at: datetime
    can_resume: bool = False
    last_error_kind: Optional[str] = None
    resume_status: Optional[str] = None

    model_config = {"from_attributes": True}


class AuditSessionMessageResponse(BaseModel):
    id: str
    session_id: str
    sequence: int
    role: str
    content: str
    name: Optional[str] = None
    metadata: dict[str, Any]
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditSessionMessageMutationResponse(AuditSessionMessageResponse):
    mode: str = "chat"


class AuditSessionToolCallResponse(BaseModel):
    id: str
    session_id: str
    turn_id: str
    sequence: int
    tool_use_id: str
    tool_name: str
    status: str
    is_concurrency_safe: bool
    input_payload: dict[str, Any]
    output_payload: dict[str, Any]
    error_message: Optional[str] = None
    duration_ms: Optional[int] = None
    started_at: datetime
    completed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class AuditModelStreamAttemptResponse(BaseModel):
    id: str
    session_id: str
    turn_id: str
    attempt_number: int
    status: str
    error_kind: Optional[str] = None
    error_message: Optional[str] = None
    provider_request_count: int
    started_at: datetime
    completed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class AuditSessionSkillResponse(BaseModel):
    id: str
    session_id: str
    skill_ref: str
    name: str
    description: Optional[str] = None
    source_type: Optional[str] = None
    enabled: bool
    matched: bool
    skill_metadata: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditSessionSkillInvocationResponse(BaseModel):
    id: str
    session_id: str
    turn_id: str
    sequence: int
    skill_ref: str
    status: str
    input_payload: dict[str, Any]
    output_payload: dict[str, Any]
    error_message: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditSessionMemoryResponse(BaseModel):
    id: str
    session_id: str
    sequence: int
    memory_kind: str
    title: str
    source_type: str
    source_ref: str
    content: str
    relevance_score: Optional[int] = None
    metadata_json: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditSessionHandoffResponse(BaseModel):
    id: str
    session_id: str
    target: str
    status: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditSessionMessageCreate(BaseModel):
    content: str
    mode: str = "chat"
    selected_skill_refs: list[str] = []


class AuditSessionResumeResponse(BaseModel):
    session_id: str
    status: str
    message: str


def _format_sse_event(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _to_message_response(message: AuditSessionMessage) -> AuditSessionMessageResponse:
    return AuditSessionMessageResponse(
        id=message.id,
        session_id=message.session_id,
        sequence=message.sequence,
        role=message.role,
        content=message.content,
        name=message.name,
        metadata=dict(message.message_metadata or {}),
        payload=dict(message.payload or {}),
        created_at=message.created_at,
    )


def _to_message_mutation_response(
    message: AuditSessionMessage,
    *,
    mode: str,
) -> AuditSessionMessageMutationResponse:
    payload = _to_message_response(message).model_dump(mode="python")
    payload["mode"] = mode
    return AuditSessionMessageMutationResponse.model_validate(payload)


def _to_session_response(session: AuditSession) -> AuditSessionResponse:
    metadata = dict((session.runtime_state_json or {}).get("metadata") or {})
    runtime_state = type("RuntimeStateRef", (), {"metadata": metadata})()
    return AuditSessionResponse.model_validate(
        {
            "id": session.id,
            "project_id": session.project_id,
            "task_id": session.task_id,
            "runtime_stack": session.runtime_stack,
            "state": session.state,
            "system_prompt": session.system_prompt,
            "recon_payload": session.recon_payload,
            "guardrails_enabled": is_guardrails_enabled(runtime_state),
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }
    )


def _build_agent_user_config(user_config: dict[str, Any] | None, agent_name: str | None) -> dict[str, Any]:
    merged = copy.deepcopy(user_config or {})
    llm_payload = copy.deepcopy((merged or {}).get("llmConfig", {}) or {})
    agent_configs = llm_payload.get("agentConfigs") or {}
    override = agent_configs.get(agent_name or "") if agent_name else None
    if isinstance(override, dict) and override.get("enabled"):
        for key in (
            "llmProvider",
            "llmApiKey",
            "llmModel",
            "llmBaseUrl",
            "llmTimeout",
            "llmTemperature",
            "llmTopP",
            "llmMaxTokens",
            "alwaysThinkingEnabled",
            "llmCustomHeaders",
            "llmFirstTokenTimeout",
            "llmStreamTimeout",
            "agentTimeout",
            "subAgentTimeout",
            "toolTimeout",
        ):
            value = override.get(key)
            if value not in (None, ""):
                llm_payload[key] = value
        override_env = override.get("env")
        if isinstance(override_env, dict) and override_env:
            base_env = llm_payload.get("env") if isinstance(llm_payload.get("env"), dict) else {}
            llm_payload["env"] = {**base_env, **override_env}
    merged["llmConfig"] = llm_payload
    return merged


def _resolve_runtime_turn_limit(user_config: dict[str, Any] | None, agent_name: str) -> int | None:
    llm_payload = copy.deepcopy((user_config or {}).get("llmConfig", {}) or {})
    agent_configs = llm_payload.get("agentConfigs") or {}
    override = agent_configs.get(agent_name) or {}
    raw_value = override.get("maxIterations") if isinstance(override, dict) else None
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


async def _build_runtime_follow_up_context(
    *,
    session: AuditSession,
    db: AsyncSession,
) -> tuple[FindingRuntimeBridge, str, int | None]:
    from app.api.v1.endpoints.agent_tasks import _get_project_root, _get_user_config
    from app.services.agent.tools.shared_catalog import build_shared_agent_tool_catalog

    task = await db.get(AgentTask, session.task_id) if session.task_id else None
    project = await db.get(Project, session.project_id)
    if task is None or project is None:
        raise HTTPException(status_code=409, detail="Audit session is missing task or project context")

    user_config = await _get_user_config(db, task.created_by)
    other_config = (user_config or {}).get("otherConfig", {})
    github_token = other_config.get("githubToken") or settings.GITHUB_TOKEN
    gitlab_token = other_config.get("gitlabToken") or settings.GITLAB_TOKEN
    gitea_token = other_config.get("giteaToken") or settings.GITEA_TOKEN
    ssh_private_key = None
    if other_config.get("sshPrivateKey"):
        try:
            ssh_private_key = decrypt_sensitive_data(other_config["sshPrivateKey"])
        except Exception:
            ssh_private_key = None

    project_root = await _get_project_root(
        project,
        task.id,
        task.branch_name,
        github_token=github_token,
        gitlab_token=gitlab_token,
        gitea_token=gitea_token,
        ssh_private_key=ssh_private_key,
        event_emitter=None,
    )

    target_files = task.target_files
    if target_files:
        valid_target_files = [file_path for file_path in target_files if os.path.exists(os.path.join(project_root, file_path))]
        target_files = valid_target_files or None

    llm_service = LLMService(user_config=_build_agent_user_config(user_config, "finding"))
    # 06-P1: finding legacy `_initialize_tools` 整链退役 —— 运行时续聊与 pr_review 分发器
    # 同源, 直接用文件运行时工具四件作为 bridge 工具集(注册表内部挂 Canonical*/RuntimeTool)。
    tools = build_shared_agent_tool_catalog(
        project_root=project_root,
        exclude_patterns=task.exclude_patterns,
        target_files=target_files,
    )
    bridge = FindingRuntimeBridge(
        llm_service=llm_service,
        tools=tools,
        user_id=task.created_by,
    )
    model_name = None
    latest_turn_model = await db.scalar(
        select(AuditSessionTurn.model_name)
        .where(AuditSessionTurn.session_id == session.id)
        .order_by(AuditSessionTurn.sequence.desc())
        .limit(1)
    )
    model_name = str(latest_turn_model or "finding")
    max_turns = _resolve_runtime_turn_limit(user_config, "finding")
    return bridge, model_name, max_turns


async def continue_runtime_session(*, session_id: str, content: str, db: AsyncSession) -> dict[str, Any] | None:
    del content
    session = await db.get(AuditSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Audit session not found")
    try:
        bridge, model_name, max_turns = await _build_runtime_follow_up_context(session=session, db=db)
    except HTTPException as exc:
        if exc.status_code == 409:
            return None
        raise
    return await bridge.continue_dialogue_session(session_id=session_id, model_name=model_name, max_turns=max_turns)


_resume_tasks: set[asyncio.Task] = set()


def _schedule_inline_session_resume(session_id: str, resume_token: str) -> None:
    """arq resume 消费链已剪断: resume 改走进程内内联, runner 自身会把 session.state 收敛。

    continue_dialogue_session 会先追加 runtime_resume 指令再 run_once;
    runner 结束把 session.state 写为 completed/failed(见 review_runtime/runner.py)。
    """

    async def _run() -> None:
        async with async_session_factory() as db:
            try:
                await continue_runtime_session(session_id=session_id, content="", db=db)
            except Exception as exc:
                logger.error(f"Audit session inline resume failed for {session_id}: {exc}")
                try:
                    session = await db.get(AuditSession, session_id)
                    if session is not None and session.state == "running":
                        session.state = "failed"
                        runtime_state = dict(session.runtime_state_json or {})
                        metadata = dict(runtime_state.get("metadata") or {})
                        metadata["resume_job"] = {
                            **dict(metadata.get("resume_job") or {}),
                            "status": "failed",
                            "error_kind": "resume_error",
                            "error": str(exc),
                        }
                        runtime_state["metadata"] = metadata
                        session.runtime_state_json = runtime_state
                        await db.commit()
                except Exception:
                    pass

    task = asyncio.create_task(_run())
    _resume_tasks.add(task)
    task.add_done_callback(_resume_tasks.discard)


async def queue_runtime_session_resume(
    *,
    session_id: str,
    current_user_id: str,
    db: AsyncSession,
) -> tuple[AuditSession, bool]:
    """Atomically claim a failed session and schedule an in-process inline resume."""
    session = await db.scalar(
        select(AuditSession).where(AuditSession.id == session_id).with_for_update()
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Audit session not found")
    task = await db.get(AgentTask, session.task_id) if session.task_id else None
    if task is not None and task.created_by != current_user_id:
        raise HTTPException(status_code=403, detail="No permission to resume this audit session")
    if session.runtime_stack != "runtime":
        raise HTTPException(status_code=400, detail="Only runtime audit sessions can be resumed")
    if session.state == "running":
        return session, False
    if session.state == "completed":
        raise HTTPException(status_code=400, detail="Completed audit sessions cannot be resumed")

    resume_token = str(uuid.uuid4())
    runtime_state = dict(session.runtime_state_json or {})
    metadata = dict(runtime_state.get("metadata") or {})
    metadata["resume_job"] = {
        "token": resume_token,
        "status": "queued",
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "can_resume": False,
        "error_kind": None,
    }
    runtime_state["metadata"] = metadata
    session.runtime_state_json = runtime_state
    session.state = "running"
    if task is not None:
        task.status = "running"
        task.error_message = None
        task.completed_at = None
    await db.commit()

    _schedule_inline_session_resume(session.id, resume_token)
    return session, True


@router.get("/{session_id}", response_model=AuditSessionResponse)
async def get_audit_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> AuditSessionResponse:
    session = await db.get(AuditSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Audit session not found")
    if session.task_id:
        task = await db.get(AgentTask, session.task_id)
        if task is not None and task.created_by != current_user.id:
            raise HTTPException(status_code=403, detail="No permission to access this audit session")
    checkpoint = await db.scalar(
        select(AuditCheckpoint)
        .where(AuditCheckpoint.session_id == session_id)
        .order_by(AuditCheckpoint.created_at.desc())
        .limit(1)
    )
    response = _to_session_response(session)
    checkpoint_payload = dict(checkpoint.state_payload or {}) if checkpoint is not None else {}
    runtime_metadata = dict((session.runtime_state_json or {}).get("metadata") or {})
    resume_job = dict(runtime_metadata.get("resume_job") or {})
    response.can_resume = session.runtime_stack == "runtime" and session.state == "failed" and bool(
        resume_job.get("can_resume")
        or checkpoint_payload.get("resumable")
        or checkpoint_payload.get("checkpoint_kind") == "resumable_failed"
    )
    response.last_error_kind = str(resume_job.get("error_kind") or checkpoint_payload.get("error_kind") or "") or None
    response.resume_status = str(resume_job.get("status") or "") or None
    return response


@router.post("/{session_id}/resume", response_model=AuditSessionResumeResponse, status_code=202)
async def resume_audit_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> AuditSessionResumeResponse:
    session, queued = await queue_runtime_session_resume(
        session_id=session_id,
        current_user_id=current_user.id,
        db=db,
    )
    if not queued:
        return AuditSessionResumeResponse(session_id=session_id, status="running", message="Audit session is already running")
    return AuditSessionResumeResponse(session_id=session_id, status="running", message="Audit session resume scheduled (in-process)")


@router.get("/{session_id}/messages", response_model=list[AuditSessionMessageResponse])
async def list_audit_session_messages(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(deps.get_current_user),
) -> list[AuditSessionMessageResponse]:
    session = await db.get(AuditSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Audit session not found")

    result = await db.execute(
        select(AuditSessionMessage)
        .where(AuditSessionMessage.session_id == session_id)
        .order_by(AuditSessionMessage.sequence)
    )
    return [_to_message_response(message) for message in result.scalars().all()]


@router.get("/{session_id}/tool-calls", response_model=list[AuditSessionToolCallResponse])
async def list_audit_session_tool_calls(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(deps.get_current_user),
) -> list[AuditSessionToolCallResponse]:
    session = await db.get(AuditSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Audit session not found")

    result = await db.execute(
        select(AuditToolCall)
        .where(AuditToolCall.session_id == session_id)
        .order_by(AuditToolCall.sequence)
    )
    return [AuditSessionToolCallResponse.model_validate(tool_call) for tool_call in result.scalars().all()]


@router.get("/{session_id}/model-attempts", response_model=list[AuditModelStreamAttemptResponse])
async def list_audit_session_model_attempts(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(deps.get_current_user),
) -> list[AuditModelStreamAttemptResponse]:
    session = await db.get(AuditSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Audit session not found")
    result = await db.execute(
        select(AuditModelStreamAttempt)
        .where(AuditModelStreamAttempt.session_id == session_id)
        .order_by(AuditModelStreamAttempt.started_at)
    )
    return [AuditModelStreamAttemptResponse.model_validate(item) for item in result.scalars().all()]


@router.get("/{session_id}/skills", response_model=list[AuditSessionSkillResponse])
async def list_audit_session_skills(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(deps.get_current_user),
) -> list[AuditSessionSkillResponse]:
    session = await db.get(AuditSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Audit session not found")

    result = await db.execute(
        select(AuditSkill)
        .where(AuditSkill.session_id == session_id)
        .order_by(AuditSkill.created_at)
    )
    return [AuditSessionSkillResponse.model_validate(skill) for skill in result.scalars().all()]


@router.get("/{session_id}/skill-invocations", response_model=list[AuditSessionSkillInvocationResponse])
async def list_audit_session_skill_invocations(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(deps.get_current_user),
) -> list[AuditSessionSkillInvocationResponse]:
    session = await db.get(AuditSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Audit session not found")

    result = await db.execute(
        select(AuditSkillInvocation)
        .where(AuditSkillInvocation.session_id == session_id)
        .order_by(AuditSkillInvocation.sequence)
    )
    return [AuditSessionSkillInvocationResponse.model_validate(invocation) for invocation in result.scalars().all()]


@router.get("/{session_id}/memories", response_model=list[AuditSessionMemoryResponse])
async def list_audit_session_memories(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(deps.get_current_user),
) -> list[AuditSessionMemoryResponse]:
    session = await db.get(AuditSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Audit session not found")

    result = await db.execute(
        select(AuditMemory)
        .where(AuditMemory.session_id == session_id)
        .order_by(AuditMemory.sequence)
    )
    return [AuditSessionMemoryResponse.model_validate(memory) for memory in result.scalars().all()]


@router.get("/{session_id}/handoffs", response_model=list[AuditSessionHandoffResponse])
async def list_audit_session_handoffs(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(deps.get_current_user),
) -> list[AuditSessionHandoffResponse]:
    session = await db.get(AuditSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Audit session not found")

    result = await db.execute(
        select(AuditHandoff)
        .where(AuditHandoff.session_id == session_id)
        .order_by(AuditHandoff.created_at)
    )
    return [AuditSessionHandoffResponse.model_validate(handoff) for handoff in result.scalars().all()]


@router.post("/{session_id}/messages", response_model=AuditSessionMessageMutationResponse)
async def create_audit_session_message(
    session_id: str,
    payload: AuditSessionMessageCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(deps.get_current_user),
) -> AuditSessionMessageMutationResponse:
    session = await db.get(AuditSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Audit session not found")
    # 06-P1: finding 旧线路整链退役 —— 非 runtime 旧版审计会话仅可读, 不再续聊
    if session.runtime_stack != "runtime":
        raise HTTPException(status_code=410, detail="旧版审计会话仅可读,不再续聊")
    mode = str(payload.mode or "chat").strip() or "chat"
    if mode != "chat":
        raise HTTPException(status_code=400, detail="Only chat mode is supported for audit session messages")

    next_sequence = await db.scalar(
        select(func.max(AuditSessionMessage.sequence)).where(AuditSessionMessage.session_id == session_id)
    )
    message = AuditSessionMessage(
        session_id=session_id,
        sequence=(next_sequence or 0) + 1,
        role="user",
        content=payload.content,
        message_metadata=(
            {"kind": "follow_up_user_message", "mode": mode, "selected_skill_refs": list(payload.selected_skill_refs or [])}
            if session.runtime_stack == "runtime"
            else {"mode": mode, "selected_skill_refs": list(payload.selected_skill_refs or [])}
        ),
        payload=(
            {"continued": session.runtime_stack == "runtime", "mode": mode, "selected_skill_refs": list(payload.selected_skill_refs or [])}
            if session.runtime_stack == "runtime"
            else {"mode": mode, "selected_skill_refs": list(payload.selected_skill_refs or [])}
        ),
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)

    if session.runtime_stack == "runtime":
        await continue_runtime_session(session_id=session_id, content=payload.content, db=db)

    return _to_message_mutation_response(
        message,
        mode=mode,
    )


@router.post("/{session_id}/messages/stream")
async def stream_audit_session_message(
    session_id: str,
    payload: AuditSessionMessageCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(deps.get_current_user),
) -> StreamingResponse:
    session = await db.get(AuditSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Audit session not found")
    # 06-P1: finding 旧线路整链退役 —— 非 runtime 旧版审计会话仅可读, 不再续聊
    if session.runtime_stack != "runtime":
        raise HTTPException(status_code=410, detail="旧版审计会话仅可读,不再续聊")
    mode = str(payload.mode or "chat").strip() or "chat"
    if mode != "chat":
        raise HTTPException(status_code=400, detail="Only chat mode is supported for audit session messages")

    next_sequence = await db.scalar(
        select(func.max(AuditSessionMessage.sequence)).where(AuditSessionMessage.session_id == session_id)
    )
    user_message = AuditSessionMessage(
        session_id=session_id,
        sequence=(next_sequence or 0) + 1,
        role="user",
        content=payload.content,
        message_metadata={
            "kind": "follow_up_user_message",
            "streaming": True,
            "mode": mode,
            "selected_skill_refs": list(payload.selected_skill_refs or []),
        },
        payload={
            "continued": True,
            "streaming": True,
            "mode": mode,
            "selected_skill_refs": list(payload.selected_skill_refs or []),
        },
    )
    db.add(user_message)
    await db.commit()
    await db.refresh(user_message)

    if session.runtime_stack == "runtime":
        async def runtime_event_generator():
            yield _format_sse_event({
                "type": "user_message",
                "message": _to_message_response(user_message).model_dump(mode="json"),
            })
            try:
                result = await continue_runtime_session(session_id=session_id, content=payload.content, db=db)
                yield _format_sse_event({
                    "type": "done",
                    "usage": {},
                    "mode": mode,
                    "result": result,
                })
            except Exception as exc:
                await db.rollback()
                error_message = str(exc)
                yield _format_sse_event({"type": "error", "message": error_message, "message_text": error_message})

        return StreamingResponse(
            runtime_event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

