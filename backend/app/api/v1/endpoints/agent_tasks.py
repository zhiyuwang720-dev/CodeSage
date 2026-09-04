"""AutoCVE Agent task API."""

import asyncio
import contextlib
import inspect
import json
import logging
import copy
import os
import re
import zipfile
import shutil
import hashlib
from typing import Any, Callable, List, Optional, Dict, Set
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import case, func
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, Field

from app.api import deps
from app.db.session import get_db, async_session_factory, get_pr_review_sync_session_factory
from app.models.agent_task import (
    AgentTask, AgentEvent, AgentFinding, AgentTreeNode,
    AgentTaskStatus, AgentTaskPhase, AgentEventType,
    VulnerabilitySeverity, FindingStatus,
)
from app.models.audit_session import AuditCheckpoint, AuditSession, AuditSessionMessage, AuditSessionTurn, AuditToolCall
from app.services.runtime.config import RuntimeStack, coerce_runtime_stack
from app.services.contracts.final_finding_contract import has_meaningful_poc, is_placeholder_finding
from app.models.project import Project
from app.models.user import User
from app.models.user_config import UserConfig
from app.services.agent.event_manager import EventManager
from app.services.agent.event_stream import create_agent_event_stream, event_stream_enabled
from app.services.agent.task_queue import enqueue_agent_task, should_use_worker_queue
from app.services.agent.task_executor import (
    _cancelled_tasks,
    _running_asyncio_tasks,
    _running_tasks,
    _watch_task_cancellation,
    clear_task_cancellation,
    execute_agent_task,
    is_task_cancelled,
    request_agent_task_cancellation,
)
from app.services.git_ssh_service import GitSSHOperations
from app.services.skill.file_service import SkillFileService
from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

class AgentTaskCreate(BaseModel):
    """Schema for creating an agent audit task."""

    project_id: str = Field(..., description="Project ID")
    name: Optional[str] = Field(None, description="Task name")
    description: Optional[str] = Field(None, description="Task description")

    audit_scope: Optional[dict] = Field(None, description="Audit scope configuration")
    target_vulnerabilities: Optional[List[str]] = Field(
        default=None,
        description="Optional explicit vulnerability classes to emphasize",
    )
    verification_level: str = Field(
        "sandbox",
        description="Verification mode: analysis_only, sandbox, or generate_poc",
    )

    version_label: Optional[str] = Field(None, description="Human-entered version label")
    version_tag: Optional[str] = Field(None, description="Optional repository tag")
    branch_name: Optional[str] = Field(None, description="Repository branch name")
    exclude_patterns: Optional[List[str]] = Field(
        default=["node_modules", "__pycache__", ".git", "*.min.js"],
        description="Glob patterns to exclude from the audit",
    )
    target_files: Optional[List[str]] = Field(None, description="Explicit file targets for the audit")

    max_iterations: int = Field(50, ge=1, le=200, description="Maximum agent iterations")
    timeout_seconds: int = Field(1800, ge=60, le=7200, description="Task timeout in seconds")
    finding_runtime_stack: Optional[str] = Field(None, description="Finding runtime stack: legacy or runtime")


class AgentTaskResponse(BaseModel):
    """Agent task response schema."""
    id: str
    project_id: str
    name: Optional[str]
    description: Optional[str]
    task_type: str = "agent_audit"
    status: str
    current_phase: Optional[str]
    current_step: Optional[str] = None
    version_label: Optional[str] = None
    version_tag: Optional[str] = None
    branch_name: Optional[str] = None
    commit_sha: Optional[str] = None
    repository_url_snapshot: Optional[str] = None
    
    total_files: int = 0
    indexed_files: int = 0
    analyzed_files: int = 0
    files_with_findings: int = 0
    total_chunks: int = 0
    
    total_iterations: int = 0
    tool_calls_count: int = 0
    tokens_used: int = 0
    
    findings_count: int = 0
    total_findings: int = 0
    verified_count: int = 0
    verified_findings: int = 0
    false_positive_count: int = 0
    
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    
    quality_score: float = 0.0
    security_score: Optional[float] = None
    
    # Progress metrics
    progress_percentage: float = 0.0
    
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    audit_scope: Optional[dict] = None
    target_vulnerabilities: Optional[List[str]] = None
    verification_level: Optional[str] = None
    exclude_patterns: Optional[List[str]] = None
    target_files: Optional[List[str]] = None
    
    error_message: Optional[str] = None
    runtime_session_id: Optional[str] = None
    finding_runtime_stack: str = RuntimeStack.RUNTIME.value
    finding_outcome: str = "none"
    runtime_completion_mode: Optional[str] = None
    finalized_findings_count: int = 0
    recovered_candidates_count: int = 0
    handoff_ready: bool = False
    recovered_candidates: List[Dict[str, Any]] = Field(default_factory=list)
    
    class Config:
        from_attributes = True



class AgentEventResponse(BaseModel):
    """Agent event response schema."""
    id: str
    task_id: Optional[str] = None
    event_type: str
    phase: Optional[str] = None
    message: Optional[str] = None
    sequence: int
    timestamp: Optional[str] = None
    created_at: Optional[str] = None
    tool_name: Optional[str] = None
    tool_input: Optional[Dict[str, Any]] = None
    tool_output: Optional[Dict[str, Any]] = None
    tool_duration_ms: Optional[int] = None
    progress_percent: Optional[float] = None
    finding_id: Optional[str] = None
    tokens_used: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None

    model_config = {
        "from_attributes": True,
        "populate_by_name": True,
    }


class AgentFindingResponse(BaseModel):
    """Agent finding response schema."""
    id: str
    task_id: str
    vulnerability_type: str
    severity: str
    title: str
    description: Optional[str] = None
    file_path: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    code_snippet: Optional[str] = None
    is_verified: bool
    confidence: Optional[float] = Field(default=0.5, validation_alias="ai_confidence")
    ai_confidence: Optional[float] = None
    status: str
    report_status: Optional[str] = None
    verdict: Optional[str] = None
    suggestion: Optional[str] = None
    has_poc: Optional[bool] = None
    poc_code: Optional[str] = None
    fix_code: Optional[str] = None
    ai_explanation: Optional[str] = None
    poc: Optional[dict] = None
    source: Optional[str] = None
    sink: Optional[str] = None
    exploit_chain: List[Dict[str, Any]] = Field(default_factory=list)
    impact: Optional[str] = None
    cve_justification: Optional[str] = None
    verification_notes: Optional[str] = None
    references: List[str] = Field(default_factory=list)
    origin: Optional[str] = None
    evidence_type: Optional[str] = None
    entry_point_refs: List[str] = Field(default_factory=list)
    priority_path_refs: List[str] = Field(default_factory=list)
    business_flow_notes: List[str] = Field(default_factory=list)
    evidence_gaps: List[str] = Field(default_factory=list)
    created_at: datetime

    model_config = {
        "from_attributes": True,
        "populate_by_name": True,
    }


class TaskSummaryResponse(BaseModel):
    """Task summary response schema."""
    task_id: str
    status: str
    security_score: Optional[int] = None
    total_findings: int
    verified_findings: int
    severity_distribution: Dict[str, int]
    vulnerability_types: Dict[str, int]
    duration_seconds: Optional[int] = None
    phases_completed: List[str]


class DebugTaskListItem(BaseModel):
    id: str
    project_id: str
    name: Optional[str]
    status: str
    created_at: datetime
    latest_event_at: Optional[str] = None
    event_count: int = 0
    agent_count: int = 0
    tool_call_count: int = 0


class DebugTraceResponse(BaseModel):
    task: Dict[str, Any]
    summary: Dict[str, Any]
    timeline: List[Dict[str, Any]]
    handoffs: List[Dict[str, Any]]





_running_orchestrators: Dict[str, Any] = {}
_running_event_managers: Dict[str, EventManager] = {}


async def _schedule_agent_task(background_tasks: BackgroundTasks, task_id: str) -> None:
    if should_use_worker_queue():
        await enqueue_agent_task(task_id)
        return
    background_tasks.add_task(_execute_agent_task, task_id)


def _resolve_task_runtime_stack(agent_config: Any) -> str:
    if isinstance(agent_config, dict):
        raw_value = agent_config.get("finding_runtime_stack")
    else:
        raw_value = None
    if raw_value in (None, ""):
        raw_value = getattr(settings, "FINDING_RUNTIME_STACK_DEFAULT", RuntimeStack.LEGACY.value)
    return coerce_runtime_stack(raw_value).value


def _extract_finding_runtime_payload(result_data: Any) -> Dict[str, Any]:
    if not isinstance(result_data, dict):
        return {}
    if any(key in result_data for key in ("runtime_completion_mode", "recovered_candidates", "findings")):
        return result_data
    phases = result_data.get("phases")
    if not isinstance(phases, dict):
        return {}
    finding_phase = phases.get("finding")
    if not isinstance(finding_phase, dict):
        return {}
    data = finding_phase.get("data")
    return data if isinstance(data, dict) else {}


def _normalize_recovered_candidate(candidate: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(candidate, dict):
        return None
    normalized = {
        "title": str(candidate.get("title") or candidate.get("vulnerability_type") or "Recovered candidate"),
        "severity": str(candidate.get("severity") or "medium"),
        "vulnerability_type": str(candidate.get("vulnerability_type") or "other"),
        "description": candidate.get("description"),
        "file_path": candidate.get("file_path"),
        "line_start": candidate.get("line_start"),
        "line_end": candidate.get("line_end"),
        "report_status": candidate.get("report_status") or "recovered_candidate",
        "verdict": candidate.get("verdict"),
        "origin": candidate.get("origin") or "transcript_recovery",
        "evidence_type": candidate.get("evidence_type") or "transcript_recovery",
        "not_finalized": bool(candidate.get("not_finalized", True)),
        "source": candidate.get("source"),
        "sink": candidate.get("sink"),
        "impact": candidate.get("impact"),
        "cve_justification": candidate.get("cve_justification"),
        "verification_notes": candidate.get("verification_notes"),
        "exploit_chain": candidate.get("exploit_chain") or [],
        "references": candidate.get("references") or [],
        "evidence_gaps": candidate.get("evidence_gaps") or [],
    }
    return normalized


def _build_finding_runtime_result_snapshot(
    *,
    persisted_findings_count: int,
    finding_payload: Dict[str, Any],
    handoff: Any = None,
) -> Dict[str, Any]:
    runtime_completion_mode = finding_payload.get("runtime_completion_mode")
    recovered_candidates = [
        normalized
        for normalized in (
            _normalize_recovered_candidate(candidate)
            for candidate in (finding_payload.get("recovered_candidates") or [])
        )
        if normalized is not None
    ]
    finalized_findings_count = int(persisted_findings_count or 0)
    recovered_candidates_count = len(recovered_candidates)
    handoff_ready = bool(handoff) and runtime_completion_mode == "finalize_tool"

    if finalized_findings_count > 0:
        finding_outcome = "finalized"
    elif recovered_candidates_count > 0:
        finding_outcome = "recovered_only"
    elif runtime_completion_mode == "incomplete":
        finding_outcome = "incomplete"
    else:
        finding_outcome = "none"

    return {
        "finding_outcome": finding_outcome,
        "runtime_completion_mode": runtime_completion_mode,
        "finalized_findings_count": finalized_findings_count,
        "recovered_candidates_count": recovered_candidates_count,
        "handoff_ready": handoff_ready,
        "recovered_candidates": recovered_candidates,
    }


def _get_task_finding_runtime_result(task: AgentTask) -> Dict[str, Any]:
    agent_config = dict(task.agent_config or {})
    stored = agent_config.get("finding_runtime_result")
    if isinstance(stored, dict):
        snapshot = {
            "finding_outcome": str(stored.get("finding_outcome") or "none"),
            "runtime_completion_mode": stored.get("runtime_completion_mode"),
            "finalized_findings_count": int(stored.get("finalized_findings_count") or 0),
            "recovered_candidates_count": int(stored.get("recovered_candidates_count") or 0),
            "handoff_ready": bool(stored.get("handoff_ready", False)),
            "recovered_candidates": [
                normalized
                for normalized in (
                    _normalize_recovered_candidate(candidate)
                    for candidate in (stored.get("recovered_candidates") or [])
                )
                if normalized is not None
            ],
        }
        snapshot["recovered_candidates_count"] = len(snapshot["recovered_candidates"]) or snapshot["recovered_candidates_count"]
        return snapshot

    findings_count = int(task.findings_count or 0)
    return {
        "finding_outcome": "finalized" if findings_count > 0 else "none",
        "runtime_completion_mode": None,
        "finalized_findings_count": findings_count,
        "recovered_candidates_count": 0,
        "handoff_ready": False,
        "recovered_candidates": [],
    }


def _prepare_task_for_resume(task: AgentTask) -> AgentTask:
    previous_status = str(task.status or "").strip() or None
    task.status = AgentTaskStatus.PENDING
    task.current_phase = AgentTaskPhase.PLANNING
    task.current_step = "Resuming from latest checkpoint"
    task.error_message = None
    task.started_at = None
    task.completed_at = None
    agent_config = dict(task.agent_config or {})
    agent_config["resume_from_checkpoint"] = True
    agent_config["resume_requested_at"] = datetime.now(timezone.utc).isoformat()
    agent_config["resume_count"] = int(agent_config.get("resume_count") or 0) + 1
    if previous_status:
        agent_config["last_resume_from_status"] = previous_status
    task.agent_config = agent_config
    return task


async def _load_runtime_session_ids(db: AsyncSession, task_ids: List[str]) -> Dict[str, str]:
    if not task_ids:
        return {}

    result = await db.execute(
        select(AuditSession.task_id, AuditSession.id)
        .where(AuditSession.task_id.in_(task_ids))
        .order_by(AuditSession.created_at.desc())
    )
    mapping: Dict[str, str] = {}
    for task_id, session_id in result.all():
        if task_id and task_id not in mapping:
            mapping[str(task_id)] = str(session_id)
    return mapping


async def _load_runtime_task_stats(db: AsyncSession, task_ids: List[str]) -> Dict[str, Dict[str, int]]:
    if not task_ids:
        return {}

    stats: Dict[str, Dict[str, int]] = {
        str(task_id): {"total_iterations": 0, "tool_calls_count": 0, "tokens_used": 0}
        for task_id in task_ids
        if task_id
    }
    turn_rows = await db.execute(
        select(AuditSession.task_id, func.count(AuditSessionTurn.id))
        .join(AuditSessionTurn, AuditSessionTurn.session_id == AuditSession.id)
        .where(AuditSession.task_id.in_(task_ids))
        .group_by(AuditSession.task_id)
    )
    for task_id, count in turn_rows.all():
        if task_id:
            stats.setdefault(str(task_id), {"total_iterations": 0, "tool_calls_count": 0, "tokens_used": 0})["total_iterations"] = int(count or 0)

    tool_rows = await db.execute(
        select(AuditSession.task_id, func.count(AuditToolCall.id))
        .join(AuditToolCall, AuditToolCall.session_id == AuditSession.id)
        .where(AuditSession.task_id.in_(task_ids))
        .group_by(AuditSession.task_id)
    )
    for task_id, count in tool_rows.all():
        if task_id:
            stats.setdefault(str(task_id), {"total_iterations": 0, "tool_calls_count": 0, "tokens_used": 0})["tool_calls_count"] = int(count or 0)

    event_rows = await db.execute(
        select(
            AgentEvent.task_id,
            func.sum(case((AgentEvent.event_type == "llm_action", 1), else_=0)),
            func.sum(case((AgentEvent.event_type == "tool_call", 1), else_=0)),
            func.sum(case((AgentEvent.event_type == "llm_usage", AgentEvent.tokens_used), else_=0)),
            func.max(AgentEvent.tokens_used),
        )
        .where(AgentEvent.task_id.in_(task_ids))
        .group_by(AgentEvent.task_id)
    )
    for task_id, llm_actions, tool_calls, usage_tokens, max_tokens in event_rows.all():
        if not task_id:
            continue
        task_stats = stats.setdefault(str(task_id), {"total_iterations": 0, "tool_calls_count": 0, "tokens_used": 0})
        task_stats["total_iterations"] = max(int(task_stats["total_iterations"] or 0), int(llm_actions or 0))
        task_stats["tool_calls_count"] = max(int(task_stats["tool_calls_count"] or 0), int(tool_calls or 0))
        task_stats["tokens_used"] = max(
            int(task_stats["tokens_used"] or 0),
            int(usage_tokens or 0),
            int(max_tokens or 0),
        )
    return stats


def _count_diff_changed_files(diff_text: str) -> int:
    """diff 中变更文件数(`+++ b/<path>` 行去重); 用于 progress_percentage 的 total_files。"""
    if not diff_text:
        return 0
    files: set[str] = set()
    for line in diff_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("+++ b/"):
            path = stripped[6:].strip()
            if path and path != "/dev/null":
                files.add(path)
    return len(files)


def _build_pr_review_event_sink(task_id: str, event_manager: EventManager, progress_cb=None):
    """PR review 运行时事件 → AgentEvent 流。

    复用 EventManager.add_event: 统一落库 agent_events + 内存队列实时(/stream) + Redis。
    review 事件契约见 app/services/runtime/query_loop.py 与 pr_review/runtime_dispatcher.tag_event_sink
    (事件 dict 带 perspective 字段; tool_call 的 dict 在 event["tool_call"], 名称取 name)。

    事件映射对齐 AutoCVE 契约(base.py:480-490): thinking_start/thinking_token/thinking_end
    生命周期 + token 放 metadata; done → task_complete(触发前端 onComplete 与 /stream 终止);
    message/assistant_* 的 dict 只取 content, 不整 dump。
    """
    sequence = 0
    # per-perspective 思考生命周期跟踪
    _thinking_open: set[str] = set()
    _accumulated: dict[str, list[str]] = {}

    def _extract_text(value) -> str:
        """事件 message 可能是 dict(query_loop.py:1026-1047 把整条消息 dict 放进来):
        取 content/文本字段, 不 str(dict) 整 dump(修复 5)。"""
        if value is None:
            return ""
        if isinstance(value, dict):
            for key in ("content", "text", "message"):
                if isinstance(value.get(key), str) and value[key].strip():
                    return value[key].strip()
            return ""
        return str(value).strip()

    def _perspective(event: dict) -> Optional[str]:
        value = event.get("perspective")
        return str(value) if value is not None else None

    def _message(event: dict, default: str) -> str:
        raw = event.get("message") or event.get("message_text") or event.get("content")
        return _extract_text(raw) or default

    async def _emit_thinking_start(perspective: str, seq: int) -> None:
        await event_manager.add_event(
            task_id, "thinking_start", sequence=seq, phase=perspective,
            message="模型开始推理", metadata=metadata_of(perspective),
        )

    def metadata_of(perspective: Optional[str]) -> dict:
        return {"review_type": "pr_review", "perspective": perspective, "agent_name": perspective}

    async def _close_thinking(perspective: str) -> None:
        """per-perspective 思考收尾: thinking_end(accumulated 进 metadata, 供前端落盘标题)。"""
        if perspective in _thinking_open:
            _thinking_open.discard(perspective)
            accumulated = "".join(_accumulated.pop(perspective, []))
            await event_manager.add_event(
                task_id, "thinking_end", sequence=sequence, phase=perspective,
                message="模型推理完成", metadata={"accumulated": accumulated, **metadata_of(perspective)},
            )

    async def sink(event: dict) -> None:
        nonlocal sequence
        ev_type = str(event.get("type") or "message")
        perspective = _perspective(event)
        metadata = metadata_of(perspective)
        sequence += 1

        if ev_type == "meta":
            repo = event.get("repo") or event.get("project_id") or ""
            pr_number = event.get("pr_number")
            suffix = f" #{pr_number}" if pr_number is not None else ""
            await event_manager.add_event(
                task_id, "review_meta", sequence=sequence, phase=None,
                message=f"PR 审查启动: {repo}{suffix}", metadata=metadata,
            )
        elif ev_type == "perspective_start":
            await event_manager.add_event(
                task_id, "review_perspective_start", sequence=sequence, phase=perspective,
                message=f"开始 {perspective} 视角审查", metadata=metadata,
            )
        elif ev_type == "assistant_start":
            await event_manager.add_event(
                task_id, "assistant_start", sequence=sequence, phase=perspective,
                message=_message(event, f"{perspective} 视角: 模型开始作答"), metadata=metadata,
            )
        elif ev_type in ("token", "reasoning_delta"):
            content = _extract_text(event.get("content"))
            if not content:
                return
            # thinking 生命周期对齐 AutoCVE: 首个 token 前 thinking_start, token 放 metadata
            if perspective not in _thinking_open:
                _thinking_open.add(perspective)
                _accumulated.setdefault(perspective, [])
                await _emit_thinking_start(perspective, sequence)
            _accumulated[perspective].append(content)
            await event_manager.add_event(
                task_id, "thinking_token", sequence=sequence, phase=perspective,
                message=content[:200], metadata={
                    "token": content, "accumulated": "".join(_accumulated[perspective]),
                    **metadata_of(perspective),
                },
            )
        elif ev_type == "tool_call":
            tool = event.get("tool_call") or {}
            tool_name = str(tool.get("name") or event.get("tool_name") or "?")
            tool_input = tool.get("input") or tool.get("arguments") or event.get("tool_input")
            await event_manager.add_event(
                task_id, "tool_call", sequence=sequence, phase=perspective,
                message=f"调用工具 {tool_name}", tool_name=tool_name,
                tool_input=tool_input if isinstance(tool_input, dict) else None,
                metadata=metadata,
            )
        elif ev_type == "done":
            await _close_thinking(perspective)
            if event.get("task_complete"):
                # 审查结束: 触发前端 onComplete 与 /stream 内存路径终止
                await event_manager.add_event(
                    task_id, "task_complete", sequence=sequence, phase=perspective,
                    message=_message(event, "PR 审查完成"), metadata=metadata,
                )
            else:
                # 每轮工具用尽的中转标记(带视角徽标); 非最终 done
                await event_manager.add_event(
                    task_id, "assistant_done", sequence=sequence, phase=perspective,
                    message=_message(event, f"{perspective} 视角: 本轮完成"), metadata=metadata,
                )
        elif ev_type == "perspective_done":
            findings = int(event.get("findings") or 0)
            turns = event.get("turn_count")
            tail = f"({turns} 轮" if turns is not None else "("
            tail += f", {findings} 发现)" if findings else ")"
            await event_manager.add_event(
                task_id, "review_perspective_done", sequence=sequence, phase=perspective,
                message=f"完成 {perspective} 视角审查 {tail}", metadata=metadata,
            )
            if progress_cb is not None and perspective:
                try:
                    await progress_cb(perspective)
                except Exception:
                    logger.debug("pr_review progress callback failed", exc_info=True)
        elif ev_type == "llm_retry":
            attempt = event.get("attempt")
            max_attempts = event.get("max_attempts")
            label = f"LLM 重试 {attempt}/{max_attempts}" if attempt is not None else "LLM 请求重试"
            await event_manager.add_event(
                task_id, "llm_retry", sequence=sequence, phase=perspective,
                message=label, metadata=metadata,
            )
        elif ev_type == "error":
            await event_manager.add_event(
                task_id, "task_error", sequence=sequence, phase=perspective,
                message=_message(event, str(event.get("error") or "审查出错")), metadata=metadata,
            )
        elif ev_type == "assistant_tombstone":
            await _close_thinking(perspective)
            await event_manager.add_event(
                task_id, "assistant_tombstone", sequence=sequence, phase=perspective,
                message=_message(event, "模型输出中断"), metadata=metadata,
            )
        elif ev_type == "message":
            text = _message(event, "")
            if text:
                await event_manager.add_event(
                    task_id, "message", sequence=sequence, phase=perspective,
                    message=text, metadata=metadata,
                )
        # session_start 等低信息量事件跳过

    return sink


async def _execute_pr_review_task_impl(
    db: AsyncSession,
    task: AgentTask,
    project: Project,
    event_manager: EventManager,
) -> None:
    """PR 3-Agent review 任务执行: 三视角运行时 + 事件流 + findings 落库。"""
    from pathlib import Path

    from app.services.pr_review.command_router import run_review_pipeline_async

    scope = (task.audit_scope or {}).get("pr_review") or {}
    pr_url = scope.get("pr_url") or project.repository_url
    diff_file_path = scope.get("diff_file_path")
    diff_text = None
    if diff_file_path and os.path.exists(diff_file_path):
        try:
            diff_text = await asyncio.to_thread(
                Path(diff_file_path).read_text, encoding="utf-8", errors="replace"
            )
        except Exception as exc:
            logger.warning(f"Failed to read diff file {diff_file_path}: {exc}")
    if not pr_url and not diff_text:
        raise ValueError("pr_review task has neither pr_url nor diff content")

    # 纯 diff 审查: 尝试从 PR URL 推导仓库并克隆源码, 让三视角能读/搜被改文件。
    # clone_source 只填 https 仓库基址(非 PR 页面地址), 克隆失败由 importer 降级 diff-only。
    clone_source = None
    if diff_text and pr_url:
        source = pr_url or ""
        if "github.com/" in source:
            parts = source.split("github.com/", 1)[1].split("/")
            if len(parts) >= 2:
                clone_source = f"https://github.com/{parts[0]}/{parts[1]}"
    workspace_root = str(
        Path(settings.MANAGED_PROJECTS_ROOT).resolve()
        / ".auditai_workspaces" / "projects" / str(project.id)
    )

    # 进度推进: diff 变更文件数 → total_files(progress_percentage 计算依据)
    changed_files = _count_diff_changed_files(diff_text or "")
    if changed_files:
        task.total_files = changed_files
    task.current_phase = AgentTaskPhase.ANALYSIS
    task.status = AgentTaskStatus.RUNNING
    await db.commit()

    async def _progress_done(perspective: str) -> None:
        """视角完成回调: 推进 analyzed_files + 发 progress 事件(修复 4)。"""
        task.analyzed_files = (task.analyzed_files or 0) + 1
        await db.commit()
        await event_manager.add_event(
            task.id, "progress", sequence=0, phase=perspective,
            message=f"{perspective} 视角完成",
            metadata={
                "review_type": "pr_review", "perspective": perspective,
                "agent_name": perspective,
                "current": task.analyzed_files or 0,
                "total": task.total_files or 0,
            },
        )

    sink = _build_pr_review_event_sink(task.id, event_manager, progress_cb=_progress_done)
    await sink({"type": "meta", "repo": project.name or pr_url, "pr_number": scope.get("pr_number")})

    result = await run_review_pipeline_async(
        pr_url=pr_url,
        diff_text=diff_text,
        user_context=scope.get("user_context"),
        options={
            "engine": "runtime",
            "task_id": task.id,
            # SEVERITY_RANK 只有 critical/high/medium/low: "info" 不在键里,
            # get 会回落成 3(high)把全部 medium/low 评论滤掉 → 必须用 "low"(全量输出)。
            "min_severity": "low",
            "max_comments": int(scope.get("max_comments") or 10),
            "max_turns": int(task.max_iterations or 50),
            # 三视角运行时审计会话用独立 SQLite 文件: sync 写(运行时)与 async 写
            # (EventManager agent_events)同库并发会在 WAL 下形成写锁循环挂死服务。
            "session_factory": get_pr_review_sync_session_factory(),
            # 工具 root: 克隆源码(source_dir)优先, 否则回退项目工作区(含 review.diff)。
            "workspace_root": workspace_root,
            "clone_source": clone_source,
        },
        event_sink=sink,
    )

    comments = result.comments or []
    findings = [
        {
            "vulnerability_type": c.category or "code_review",
            "severity": c.severity or "medium",
            "title": f"{c.category or 'review'} in {c.path}:{c.line}",
            "description": c.body,
            "file_path": c.path,
            "line_start": c.line,
            "line_end": c.line,
        }
        for c in comments
        if c and getattr(c, "path", None)
    ]
    saved = await _save_findings(db, task.id, findings, project_root=None)

    task.status = AgentTaskStatus.COMPLETED
    task.current_phase = AgentTaskPhase.REPORTING
    task.completed_at = datetime.now(timezone.utc)
    task.findings_count = len(findings)
    task.error_message = None
    await db.commit()
    await sink({"type": "done", "task_complete": True, "message": f"PR 审查完成, 共 {len(comments)} 条评论"})
    logger.info(f"pr_review task {task.id} completed with {saved} findings")


async def _execute_agent_task_impl(task_id: str):
    """Execute an agent audit task in the background."""
    # PR review 任务不依赖 legacy agent 编排, 提前分派: 三视角 review 运行时。
    # 放在 legacy 导入之前, 避免 trimmed 后的 agents 模块缺失阻塞 PR 流程。
    async with async_session_factory() as probe_db:
        probe_task = await probe_db.get(
            AgentTask, task_id, options=[selectinload(AgentTask.project)]
        )
        if probe_task and (
            probe_task.task_type == "pr_review"
            or (probe_task.audit_scope or {}).get("pr_review")
        ):
            if probe_task.status == AgentTaskStatus.CANCELLED or is_task_cancelled(task_id):
                logger.warning(f"pr_review task {task_id} cancelled before execution")
                return
            # 释放本次读取的 SQLite 事务(SHARED 锁): 否则整个 review 期间
            # 运行时的 audit_sessions/agent_events 写入会被阻塞, 报 database is locked。
            await probe_db.commit()
            from app.services.agent.event_manager import EventManager
            event_stream = create_agent_event_stream() if event_stream_enabled() else None
            pr_event_manager = EventManager(
                db_session_factory=async_session_factory,
                event_stream=event_stream,
            )
            pr_event_manager.create_queue(task_id)
            _running_event_managers[task_id] = pr_event_manager
            try:
                await _execute_pr_review_task_impl(
                    probe_db, probe_task, probe_task.project, pr_event_manager
                )
            finally:
                _running_event_managers.pop(task_id, None)
                try:
                    await pr_event_manager.close()
                except Exception:
                    logger.debug("PR event manager cleanup skipped", exc_info=True)
            return

    # 产品收敛: 仅 pr_review 任务存活。建任务接口已闸门拦截非 PR scope,
    # 此处兜底存量 agent_audit 任务: 以明确信息标记失败而非执行已删除的 legacy 编排。
    logger.warning(
        "Unsupported task type for %s: only pr_review tasks are supported "
        "after product convergence (AutoCVE/legacy audit removed)",
        task_id,
    )
    async with async_session_factory() as db:
        task = await db.get(AgentTask, task_id)
        if task is not None and task.status not in (
            AgentTaskStatus.COMPLETED,
            AgentTaskStatus.CANCELLED,
            AgentTaskStatus.FAILED,
        ):
            task.status = AgentTaskStatus.FAILED
            task.completed_at = datetime.now(timezone.utc)
            task.error_message = "该任务类型已下线; 产品仅支持 pr_review 任务"
            await db.commit()


_execute_agent_task = execute_agent_task


async def _get_user_config(db: AsyncSession, user_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Load merged user config for task execution."""
    if not user_id:
        return None

    try:
        from app.api.v1.endpoints.config import _get_user_config_record, _merge_user_config

        record = await _get_user_config_record(db, user_id)
        return _merge_user_config(record)
    except Exception as e:
        logger.warning(f"Failed to get user config: {e}")

    return None


def _severity_rank(value: str | None) -> int:
    return {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}.get(str(value or "").lower(), -1)


def _finding_status_rank(value: str | None) -> int:
    return {
        FindingStatus.NEW: 0,
        FindingStatus.ANALYZING: 1,
        FindingStatus.NEEDS_REVIEW: 1,
        FindingStatus.FALSE_POSITIVE: 2,
        FindingStatus.VERIFIED: 3,
        FindingStatus.FIXED: 4,
    }.get(str(value or "").lower(), -1)


def _merge_string_list(existing_values: Any, new_values: Any) -> list[str] | None:
    merged: list[str] = []
    for value in list(existing_values or []) + list(new_values or []):
        normalized = str(value or "").strip()
        if normalized and normalized not in merged:
            merged.append(normalized)
    return merged or None


def _merge_existing_finding_record(existing: AgentFinding, incoming: AgentFinding, raw_finding: Dict[str, Any]) -> bool:
    changed = False

    if _severity_rank(incoming.severity) > _severity_rank(existing.severity):
        existing.severity = incoming.severity
        changed = True
    if (incoming.ai_confidence or 0) > (existing.ai_confidence or 0):
        existing.ai_confidence = incoming.ai_confidence
        changed = True
    if incoming.is_verified and not existing.is_verified:
        existing.is_verified = True
        existing.status = incoming.status
        changed = True
    elif _finding_status_rank(incoming.status) > _finding_status_rank(existing.status):
        existing.status = incoming.status
        changed = True

    for field_name in (
        "description",
        "suggestion",
        "ai_explanation",
        "verification_method",
        "poc_code",
        "poc_description",
        "source",
        "sink",
    ):
        incoming_value = getattr(incoming, field_name, None)
        existing_value = getattr(existing, field_name, None)
        if incoming_value and (not existing_value or incoming.is_verified):
            if existing_value != incoming_value:
                setattr(existing, field_name, incoming_value)
                changed = True

    if incoming.has_poc and not existing.has_poc:
        existing.has_poc = True
        changed = True
    if incoming.poc_steps and not existing.poc_steps:
        existing.poc_steps = incoming.poc_steps
        changed = True
    if incoming.verification_result and incoming.verification_result != existing.verification_result:
        existing.verification_result = incoming.verification_result
        changed = True
    if incoming.line_end and (existing.line_end or 0) < incoming.line_end:
        existing.line_end = incoming.line_end
        changed = True

    merged_references = _merge_string_list(existing.references, incoming.references)
    if merged_references != (existing.references or None):
        existing.references = merged_references
        changed = True

    metadata = dict(existing.finding_metadata or {})
    latest_report_status = raw_finding.get("report_status") or raw_finding.get("verdict")
    if latest_report_status and metadata.get("report_status") != latest_report_status:
        metadata["report_status"] = latest_report_status
        changed = True
    if raw_finding.get("origin") and metadata.get("origin") != raw_finding.get("origin"):
        metadata["origin"] = raw_finding.get("origin")
        changed = True
    if raw_finding.get("evidence_type") and metadata.get("evidence_type") != raw_finding.get("evidence_type"):
        metadata["evidence_type"] = raw_finding.get("evidence_type")
        changed = True
    if changed:
        metadata["raw_finding"] = raw_finding
        metadata["merge_count"] = int(metadata.get("merge_count") or 0) + 1
        metadata["last_merged_at"] = datetime.now(timezone.utc).isoformat()
        existing.finding_metadata = metadata
    return changed


def _build_finding_fingerprint(record: AgentFinding) -> str:
    record.fingerprint = record.fingerprint or record.generate_fingerprint()
    normalized = str(record.fingerprint or "").strip()
    if normalized:
        return normalized
    components = [
        str(record.vulnerability_type or ""),
        str(record.file_path or ""),
        str(record.line_start or 0),
        str(record.line_end or 0),
        str(record.title or ""),
    ]
    fingerprint = hashlib.sha256("|".join(components).encode("utf-8")).hexdigest()[:16]
    record.fingerprint = fingerprint
    return fingerprint


async def _save_findings(
    db: AsyncSession,
    task_id: str,
    findings: List[Dict],
    project_root: Optional[str] = None,
) -> int:
    """Persist normalized findings for an audit task."""
    from app.models.agent_task import VulnerabilityType

    logger.info(f"[SaveFindings] Starting to save {len(findings)} findings for task {task_id}")
    if not findings:
        logger.warning(f"[SaveFindings] No findings to save for task {task_id}")
        return 0

    existing_result = await db.execute(select(AgentFinding).where(AgentFinding.task_id == task_id))
    existing_scalars = existing_result.scalars()
    if inspect.isawaitable(existing_scalars):
        existing_scalars = await existing_scalars
    existing_findings_result = existing_scalars.all()
    if inspect.isawaitable(existing_findings_result):
        existing_findings_result = await existing_findings_result
    existing_findings = list(existing_findings_result or [])
    existing_by_fingerprint: Dict[str, AgentFinding] = {}
    for existing in existing_findings:
        existing_by_fingerprint[_build_finding_fingerprint(existing)] = existing

    severity_map = {
        "critical": VulnerabilitySeverity.CRITICAL,
        "high": VulnerabilitySeverity.HIGH,
        "medium": VulnerabilitySeverity.MEDIUM,
        "low": VulnerabilitySeverity.LOW,
        "info": VulnerabilitySeverity.INFO,
    }
    type_map = {
        "sql_injection": VulnerabilityType.SQL_INJECTION,
        "nosql_injection": VulnerabilityType.NOSQL_INJECTION,
        "xss": VulnerabilityType.XSS,
        "command_injection": VulnerabilityType.COMMAND_INJECTION,
        "code_injection": VulnerabilityType.CODE_INJECTION,
        "path_traversal": VulnerabilityType.PATH_TRAVERSAL,
        "ssrf": VulnerabilityType.SSRF,
        "xxe": VulnerabilityType.XXE,
        "auth_bypass": VulnerabilityType.AUTH_BYPASS,
        "idor": VulnerabilityType.IDOR,
        "sensitive_data_exposure": VulnerabilityType.SENSITIVE_DATA_EXPOSURE,
        "hardcoded_secret": VulnerabilityType.HARDCODED_SECRET,
        "deserialization": VulnerabilityType.DESERIALIZATION,
        "weak_crypto": VulnerabilityType.WEAK_CRYPTO,
        "file_inclusion": VulnerabilityType.FILE_INCLUSION,
        "race_condition": VulnerabilityType.RACE_CONDITION,
        "business_logic": VulnerabilityType.BUSINESS_LOGIC,
        "memory_corruption": VulnerabilityType.MEMORY_CORRUPTION,
    }

    def normalize_type(raw_type: str):
        mapped = type_map.get(raw_type, VulnerabilityType.OTHER)
        if "sqli" in raw_type or raw_type == "sql" or "sql_" in raw_type:
            return VulnerabilityType.SQL_INJECTION
        if "xss" in raw_type:
            return VulnerabilityType.XSS
        if "rce" in raw_type or "command" in raw_type or "cmd" in raw_type:
            return VulnerabilityType.COMMAND_INJECTION
        if "traversal" in raw_type or "lfi" in raw_type or "rfi" in raw_type:
            return VulnerabilityType.PATH_TRAVERSAL
        if "ssrf" in raw_type:
            return VulnerabilityType.SSRF
        if "xxe" in raw_type:
            return VulnerabilityType.XXE
        if "auth" in raw_type:
            return VulnerabilityType.AUTH_BYPASS
        if "secret" in raw_type or "credential" in raw_type or "password" in raw_type:
            return VulnerabilityType.HARDCODED_SECRET
        if "deserial" in raw_type:
            return VulnerabilityType.DESERIALIZATION
        return mapped

    def normalize_references(raw_references):
        if not raw_references:
            return None
        if isinstance(raw_references, str):
            try:
                parsed = json.loads(raw_references)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                return [raw_references]
            return [raw_references]
        if isinstance(raw_references, list):
            return raw_references
        return [str(raw_references)]

    inserted_count = 0
    merged_count = 0
    for finding in findings:
        if not isinstance(finding, dict):
            logger.debug(f"[SaveFindings] Skipping non-dict finding: {type(finding)}")
            continue
        if is_placeholder_finding(finding):
            logger.warning(
                "[SaveFindings] Skipping placeholder or free-form finding payload: %s",
                sorted(finding.keys()),
            )
            continue

        try:
            raw_severity = str(finding.get("severity") or finding.get("risk") or "medium").lower().strip()
            severity_enum = severity_map.get(raw_severity, VulnerabilitySeverity.MEDIUM)

            raw_type = str(
                finding.get("vulnerability_type")
                or finding.get("type")
                or finding.get("vuln_type")
                or "other"
            ).lower().strip().replace(" ", "_").replace("-", "_")
            type_enum = normalize_type(raw_type)

            location = finding.get("location", "") or ""
            file_path = finding.get("file_path") or finding.get("file")
            if not file_path:
                file_path = location.split(":")[0] if ":" in location else location

            if project_root and file_path:
                clean_path = file_path.split(":")[0].strip()
                full_path = os.path.join(project_root, clean_path)
                if not os.path.isfile(full_path) and not (os.path.isabs(clean_path) and os.path.isfile(clean_path)):
                    logger.warning(
                        f"[SaveFindings] Skipping finding with missing file path '{file_path}' "
                        f"(title: {str(finding.get('title', 'N/A'))[:50]})"
                    )
                    continue

            line_start = finding.get("line_start") or finding.get("line")
            if not line_start and ":" in location:
                try:
                    line_start = int(location.split(":")[1])
                except (ValueError, IndexError):
                    line_start = None
            line_end = finding.get("line_end") or line_start

            code_snippet = finding.get("code_snippet") or finding.get("code") or finding.get("vulnerable_code")
            title = finding.get("title")
            if not title:
                type_display = raw_type.replace("_", " ").title()
                title = f"{type_display} in {os.path.basename(file_path)}" if file_path else f"{type_display} Vulnerability"

            description = (
                finding.get("description")
                or finding.get("details")
                or finding.get("explanation")
                or finding.get("impact")
                or ""
            )
            suggestion = (
                finding.get("suggestion")
                or finding.get("recommendation")
                or finding.get("remediation")
                or finding.get("fix")
            )

            confidence = finding.get("confidence") or finding.get("ai_confidence") or 0.5
            if isinstance(confidence, str):
                try:
                    confidence = float(confidence)
                except ValueError:
                    confidence = 0.5

            verdict = str(finding.get("verdict") or finding.get("report_status") or "candidate").lower()
            is_verified = bool(finding.get("is_verified", False) or verdict == "confirmed")
            poc_data = finding.get("poc") or {}
            has_poc = has_meaningful_poc(poc_data)
            poc_code = poc_data.get("code") if has_poc and isinstance(poc_data, dict) else None
            poc_description = poc_data.get("description") if has_poc and isinstance(poc_data, dict) else None
            poc_steps = poc_data.get("steps") if has_poc and isinstance(poc_data, dict) else None

            verification_method = finding.get("verification_method")
            verification_result = finding.get("verification_result") or {"verdict": verdict}
            references = normalize_references(finding.get("references") or finding.get("reference_links"))
            if verdict == "false_positive":
                status = FindingStatus.FALSE_POSITIVE
            elif is_verified:
                status = FindingStatus.VERIFIED
            else:
                status = FindingStatus.NEW

            record = AgentFinding(
                id=str(uuid4()),
                task_id=task_id,
                title=title,
                description=description,
                vulnerability_type=type_enum,
                severity=severity_enum,
                file_path=file_path,
                line_start=line_start,
                line_end=line_end,
                code_snippet=code_snippet,
                suggestion=suggestion,
                ai_explanation=finding.get("verification_notes") or finding.get("impact"),
                ai_confidence=confidence,
                status=status,
                is_verified=is_verified,
                has_poc=has_poc,
                poc_code=poc_code,
                poc_description=poc_description,
                poc_steps=poc_steps,
                verification_method=verification_method,
                verification_result=verification_result,
                source=finding.get("source"),
                sink=finding.get("sink"),
                references=references,
                finding_metadata={
                    "raw_finding": finding,
                    "report_status": finding.get("report_status") or verdict,
                    "origin": finding.get("origin"),
                    "evidence_type": finding.get("evidence_type"),
                },
            )
            fingerprint = _build_finding_fingerprint(record)
            existing = existing_by_fingerprint.get(fingerprint)
            if existing is not None:
                if _merge_existing_finding_record(existing, record, finding):
                    merged_count += 1
                continue

            db.add(record)
            existing_by_fingerprint[fingerprint] = record
            inserted_count += 1
        except Exception as exc:
            logger.exception(f"[SaveFindings] Failed to save finding: {exc}")

    persisted_count = inserted_count + merged_count
    if persisted_count:
        await db.commit()
        logger.info(
            f"[SaveFindings] Persisted {persisted_count} findings for task {task_id} "
            f"({inserted_count} inserted, {merged_count} merged)"
        )
    else:
        logger.warning(f"[SaveFindings] No findings were saved for task {task_id}")
    return persisted_count


def _serialize_agent_finding_record(finding: AgentFinding) -> Dict[str, Any]:
    from app.services.task_report_service import serialize_finding

    item = serialize_finding(finding)
    item["confidence"] = item.pop("confidence", None)
    return item


async def _load_task_findings(db: AsyncSession, task_id: str) -> List[AgentFinding]:
    result = await db.execute(
        select(AgentFinding)
        .where(AgentFinding.task_id == task_id)
        .order_by(
            case(
                (AgentFinding.is_verified.is_(True), 0),
                (AgentFinding.status == FindingStatus.VERIFIED, 1),
                (AgentFinding.severity == "critical", 2),
                (AgentFinding.severity == "high", 3),
                (AgentFinding.severity == "medium", 4),
                (AgentFinding.severity == "low", 5),
                else_=6,
            ),
            AgentFinding.created_at.desc(),
        )
    )
    return list(result.scalars().all())


def _apply_task_finding_metrics(task: AgentTask, findings: List[AgentFinding | Dict[str, Any]]) -> None:
    severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    files_with_findings: Set[str] = set()
    verified_count = 0
    false_positive_count = 0

    for item in findings:
        finding = item if isinstance(item, dict) else _serialize_agent_finding_record(item)
        severity = str(finding.get("severity", "")).lower()
        if severity in severity_counts:
            severity_counts[severity] += 1
        if finding.get("file_path"):
            files_with_findings.add(str(finding["file_path"]))
        if finding.get("is_verified") or str(finding.get("report_status", "")).lower() == "confirmed":
            verified_count += 1
        if str(finding.get("report_status") or finding.get("status") or "").lower() == "false_positive":
            false_positive_count += 1

    task.findings_count = len(findings)
    task.verified_count = verified_count
    task.false_positive_count = false_positive_count
    task.files_with_findings = len(files_with_findings)
    task.critical_count = severity_counts["critical"]
    task.high_count = severity_counts["high"]
    task.medium_count = severity_counts["medium"]
    task.low_count = severity_counts["low"]


def _calculate_security_score(findings: List[Dict]) -> float:
    """Calculate a simple security score from finding severities."""
    if not findings:
        return 100.0

    deductions = {
        "critical": 25,
        "high": 15,
        "medium": 8,
        "low": 3,
        "info": 1,
    }
    total_deduction = 0
    for finding in findings:
        if isinstance(finding, dict):
            severity = str(finding.get("severity", "low")).lower()
            total_deduction += deductions.get(severity, 3)
    return float(max(0, 100 - total_deduction))


def _debug_event_value(event: Any, key: str, default: Any = None) -> Any:
    if hasattr(event, key):
        value = getattr(event, key)
        if value is not None:
            return value
    if isinstance(event, dict):
        return event.get(key, default)
    return default


def _normalize_debug_event(event: Any) -> Dict[str, Any]:
    metadata = _debug_event_value(event, "event_metadata", {}) or {}
    created_at = _debug_event_value(event, "created_at")
    timestamp = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at) if created_at else None
    payload = metadata.get("payload") if isinstance(metadata, dict) else None
    return {
        "id": _debug_event_value(event, "id"),
        "task_id": _debug_event_value(event, "task_id"),
        "event_type": _debug_event_value(event, "event_type"),
        "sequence": _debug_event_value(event, "sequence", 0),
        "phase": _debug_event_value(event, "phase"),
        "message": _debug_event_value(event, "message"),
        "tool_name": _debug_event_value(event, "tool_name"),
        "tool_input": _debug_event_value(event, "tool_input"),
        "tool_output": _debug_event_value(event, "tool_output"),
        "tool_duration_ms": _debug_event_value(event, "tool_duration_ms"),
        "progress_percent": _event_progress_percent(event),
        "timestamp": timestamp,
        "created_at": timestamp,
        "agent_name": metadata.get("agent_name"),
        "agent_type": metadata.get("agent_type"),
        "provider": metadata.get("provider"),
        "model": metadata.get("model"),
        "iteration": metadata.get("iteration"),
        "payload": payload if payload is not None else metadata,
        "metadata": metadata,
    }


def _event_progress_percent(event: Any) -> Optional[float]:
    direct_value = _debug_event_value(event, "progress_percent")
    if direct_value is not None:
        try:
            return float(direct_value)
        except (TypeError, ValueError):
            return None

    metadata = _debug_event_value(event, "event_metadata", {}) or {}
    candidates: list[Any] = []
    if isinstance(metadata, dict):
        candidates.append(metadata.get("progress_percent"))
        payload = metadata.get("payload")
        if isinstance(payload, dict):
            candidates.append(payload.get("progress_percent"))

    for value in candidates:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def build_debug_task_item(
    *,
    task_id: str,
    task_name: Optional[str],
    project_id: str,
    status: str,
    created_at: datetime,
    events: List[Any],
) -> Dict[str, Any]:
    normalized = [_normalize_debug_event(event) for event in events]
    agent_types = sorted({event["agent_type"] for event in normalized if event.get("agent_type")})
    latest_event_at = normalized[-1]["timestamp"] if normalized else None
    tool_call_count = sum(1 for event in normalized if event["event_type"] == "tool_call")
    return {
        "id": task_id,
        "project_id": project_id,
        "name": task_name,
        "status": status,
        "created_at": created_at,
        "latest_event_at": latest_event_at,
        "event_count": len(normalized),
        "agent_count": len(agent_types),
        "tool_call_count": tool_call_count,
    }


def build_debug_trace_payload(
    *,
    task_id: str,
    task_name: Optional[str],
    task_status: str,
    events: List[Any],
) -> Dict[str, Any]:
    normalized = [_normalize_debug_event(event) for event in events]
    normalized.sort(key=lambda item: item["sequence"])
    handoffs: List[Dict[str, Any]] = []
    phases = sorted({event["phase"] for event in normalized if event.get("phase")})
    agents = sorted({event["agent_type"] for event in normalized if event.get("agent_type")})
    tool_calls = sum(1 for event in normalized if event["event_type"] == "tool_call")
    for event in normalized:
        if event["event_type"] in {"handoff_out", "handoff_in"}:
            payload = event.get("payload") or {}
            if isinstance(payload, dict):
                handoffs.append(
                    {
                        "event_id": event["id"],
                        "event_type": event["event_type"],
                        "sequence": event["sequence"],
                        "timestamp": event["timestamp"],
                        "from_agent": payload.get("from_agent"),
                        "to_agent": payload.get("to_agent"),
                        "summary": payload.get("summary") or payload.get("payload", {}).get("summary"),
                        "payload": payload,
                    }
                )
    return {
        "task": {
            "id": task_id,
            "name": task_name,
            "status": task_status,
        },
        "summary": {
            "event_count": len(normalized),
            "agents": agents,
            "phases": phases,
            "tool_calls": tool_calls,
            "handoff_count": len(handoffs),
        },
        "timeline": normalized,
        "handoffs": handoffs,
    }


@router.post("/", response_model=AgentTaskResponse)
async def create_agent_task(
    request: AgentTaskCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """Create a PR review task (product-converged; agent_audit/仓库源码审计已下线, 不可再创建)."""
    project = await db.get(Project, request.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if not (request.audit_scope or {}).get("pr_review"):
        raise HTTPException(
            status_code=400,
            detail="仅支持创建 PR review 任务(audit_scope.pr_review 必填); 仓库源码审计任务类型已下线",
        )

    runtime_stack = coerce_runtime_stack(
        request.finding_runtime_stack or RuntimeStack.RUNTIME.value
    ).value
    task_name = request.name or f"PR Review - {datetime.now().strftime('%Y%m%d_%H%M%S')}"

    task = AgentTask(
        id=str(uuid4()),
        project_id=project.id,
        task_type="pr_review",
        name=task_name,
        description=request.description,
        status=AgentTaskStatus.PENDING,
        current_phase=AgentTaskPhase.PLANNING,
        target_vulnerabilities=request.target_vulnerabilities,
        version_label=request.version_label or "pr-review",
        version_tag=request.version_tag,
        verification_level=request.verification_level or "sandbox",
        branch_name=request.branch_name,
        repository_url_snapshot=project.repository_url,
        exclude_patterns=request.exclude_patterns,
        target_files=request.target_files,
        max_iterations=request.max_iterations or 50,
        timeout_seconds=request.timeout_seconds or 1800,
        agent_config={"finding_runtime_stack": runtime_stack},
        created_by=current_user.id,
        audit_scope=request.audit_scope,
    )

    db.add(task)
    await db.commit()
    await db.refresh(task)
    # PR review 任务不自动调度; 前端点「启动」后经 POST /agent-tasks/{id}/start 运行
    logger.info(f"Created pr_review task {task.id} for project {project.name} (pending; start via POST /agent-tasks/{task.id}/start)")
    return task


@router.post("/{task_id}/start", response_model=AgentTaskResponse)
async def start_agent_task(
    task_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """Start a pending pr_review task: kicks off the PR 3-Agent review in the background."""
    task = await db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    project = await db.get(Project, task.project_id)
    if not project or project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    if task.task_type != "pr_review" and not (task.audit_scope or {}).get("pr_review"):
        raise HTTPException(status_code=400, detail="Only pr_review tasks can be started")
    if task.status != AgentTaskStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"Task already started (status={task.status})")

    task.status = AgentTaskStatus.RUNNING
    task.started_at = datetime.now(timezone.utc)
    task.current_phase = AgentTaskPhase.PLANNING
    await db.commit()
    await db.refresh(task)
    await _schedule_agent_task(background_tasks, task.id)
    logger.info(f"Started pr_review task {task.id}")
    return task


@router.get("/", response_model=List[AgentTaskResponse])
async def list_agent_tasks(
    project_id: Optional[str] = None,
    status: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """List audit tasks visible to the current user."""
    projects_result = await db.execute(select(Project.id).where(Project.owner_id == current_user.id))
    user_project_ids = [row[0] for row in projects_result.fetchall()]
    if not user_project_ids:
        return []

    query = select(AgentTask).where(AgentTask.project_id.in_(user_project_ids))
    if project_id:
        query = query.where(AgentTask.project_id == project_id)
    if status:
        try:
            query = query.where(AgentTask.status == AgentTaskStatus(status))
        except ValueError:
            pass

    result = await db.execute(query.order_by(AgentTask.created_at.desc()).offset(skip).limit(limit))
    tasks = result.scalars().all()
    runtime_session_ids = await _load_runtime_session_ids(db, [task.id for task in tasks])
    runtime_stats = await _load_runtime_task_stats(db, [task.id for task in tasks])
    for task in tasks:
        runtime_result = _get_task_finding_runtime_result(task)
        task_runtime_stats = runtime_stats.get(str(task.id), {})
        if task_runtime_stats.get("total_iterations"):
            setattr(task, "total_iterations", max(int(task.total_iterations or 0), int(task_runtime_stats["total_iterations"])))
        if task_runtime_stats.get("tool_calls_count"):
            setattr(task, "tool_calls_count", max(int(task.tool_calls_count or 0), int(task_runtime_stats["tool_calls_count"])))
        if task_runtime_stats.get("tokens_used"):
            setattr(task, "tokens_used", max(int(task.tokens_used or 0), int(task_runtime_stats["tokens_used"])))
        setattr(task, "runtime_session_id", runtime_session_ids.get(task.id))
        setattr(task, "finding_runtime_stack", _resolve_task_runtime_stack(task.agent_config))
        setattr(task, "finding_outcome", runtime_result["finding_outcome"])
        setattr(task, "runtime_completion_mode", runtime_result["runtime_completion_mode"])
        setattr(task, "finalized_findings_count", runtime_result["finalized_findings_count"])
        setattr(task, "recovered_candidates_count", runtime_result["recovered_candidates_count"])
        setattr(task, "handoff_ready", runtime_result["handoff_ready"])
        setattr(task, "recovered_candidates", runtime_result["recovered_candidates"])
    return tasks


@router.get("/debug-tasks", response_model=List[DebugTaskListItem])
async def list_debug_tasks(
    project_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    logger.info(
        "debug-tasks:start user=%s project_id=%s status=%s limit=%s",
        getattr(current_user, "id", ""),
        project_id,
        status,
        limit,
    )
    projects_result = await db.execute(select(Project.id).where(Project.owner_id == current_user.id))
    user_project_ids = [row[0] for row in projects_result.fetchall()]
    logger.info("debug-tasks:projects count=%s", len(user_project_ids))
    if not user_project_ids:
        logger.info("debug-tasks:return empty-no-projects")
        return []

    query = select(
        AgentTask.id,
        AgentTask.project_id,
        AgentTask.name,
        AgentTask.status,
        AgentTask.created_at,
        AgentTask.started_at,
        AgentTask.completed_at,
        AgentTask.tool_calls_count,
    ).where(AgentTask.project_id.in_(user_project_ids))
    if project_id:
        query = query.where(AgentTask.project_id == project_id)
    if status:
        query = query.where(AgentTask.status == status)
    logger.info("debug-tasks:before-task-query")
    tasks_result = await db.execute(query.order_by(AgentTask.created_at.desc()).limit(limit))
    tasks = tasks_result.mappings().all()
    logger.info("debug-tasks:tasks count=%s", len(tasks))

    payload = [
        {
            "id": task["id"],
            "project_id": task["project_id"],
            "name": task["name"],
            "status": str(task["status"]),
            "created_at": task["created_at"].isoformat() if task["created_at"] else None,
            "latest_event_at": (
                (task["completed_at"] or task["started_at"] or task["created_at"]).isoformat()
                if (task["completed_at"] or task["started_at"] or task["created_at"])
                else None
            ),
            "event_count": 0,
            "agent_count": 0,
            "tool_call_count": int(task["tool_calls_count"] or 0),
        }
        for task in tasks
    ]
    logger.info("debug-tasks:return count=%s", len(payload))
    return payload


@router.get("/{task_id}", response_model=AgentTaskResponse)
async def get_agent_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """Fetch a single audit task with live stats when available."""
    task = await db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    project = await db.get(Project, task.project_id)
    if not project or project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    progress = 0.0
    if hasattr(task, "progress_percentage") and task.progress_percentage is not None:
        progress = task.progress_percentage
    elif task.status == AgentTaskStatus.COMPLETED:
        progress = 100.0

    total_iterations = task.total_iterations or 0
    tool_calls_count = task.tool_calls_count or 0
    tokens_used = task.tokens_used or 0

    orchestrator = _running_orchestrators.get(task_id)
    if orchestrator and task.status == AgentTaskStatus.RUNNING and hasattr(orchestrator, "get_stats"):
        stats = orchestrator.get_stats()
        total_iterations = stats.get("iterations", total_iterations)
        tool_calls_count = stats.get("tool_calls", tool_calls_count)
        tokens_used = stats.get("tokens_used", tokens_used)
        for agent in getattr(orchestrator, "sub_agents", {}).values():
            if hasattr(agent, "get_stats"):
                sub_stats = agent.get_stats()
                total_iterations += sub_stats.get("iterations", 0)
                tool_calls_count += sub_stats.get("tool_calls", 0)
                tokens_used += sub_stats.get("tokens_used", 0)

    runtime_session_ids = await _load_runtime_session_ids(db, [task.id])
    runtime_stats = await _load_runtime_task_stats(db, [task.id])
    task_runtime_stats = runtime_stats.get(str(task.id), {})
    if task_runtime_stats.get("total_iterations"):
        total_iterations = max(total_iterations, int(task_runtime_stats["total_iterations"]))
    if task_runtime_stats.get("tool_calls_count"):
        tool_calls_count = max(tool_calls_count, int(task_runtime_stats["tool_calls_count"]))
    if task_runtime_stats.get("tokens_used"):
        tokens_used = max(tokens_used, int(task_runtime_stats["tokens_used"]))
    runtime_result = _get_task_finding_runtime_result(task)

    response_data = {
        "id": task.id,
        "project_id": task.project_id,
        "name": task.name,
        "description": task.description,
        "task_type": task.task_type or "agent_audit",
        "status": task.status,
        "current_phase": task.current_phase,
        "current_step": task.current_step,
        "total_files": task.total_files or 0,
        "indexed_files": task.indexed_files or 0,
        "analyzed_files": task.analyzed_files or 0,
        "total_chunks": task.total_chunks or 0,
        "total_iterations": total_iterations,
        "tool_calls_count": tool_calls_count,
        "tokens_used": tokens_used,
        "findings_count": task.findings_count or 0,
        "total_findings": task.findings_count or 0,
        "verified_count": task.verified_count or 0,
        "verified_findings": task.verified_count or 0,
        "false_positive_count": task.false_positive_count or 0,
        "critical_count": task.critical_count or 0,
        "high_count": task.high_count or 0,
        "medium_count": task.medium_count or 0,
        "low_count": task.low_count or 0,
        "quality_score": float(task.quality_score or 0.0),
        "security_score": float(task.security_score) if task.security_score is not None else None,
        "progress_percentage": progress,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "error_message": task.error_message,
        "runtime_session_id": runtime_session_ids.get(task.id),
        "finding_runtime_stack": _resolve_task_runtime_stack(task.agent_config),
        "finding_outcome": runtime_result["finding_outcome"],
        "runtime_completion_mode": runtime_result["runtime_completion_mode"],
        "finalized_findings_count": runtime_result["finalized_findings_count"],
        "recovered_candidates_count": runtime_result["recovered_candidates_count"],
        "handoff_ready": runtime_result["handoff_ready"],
        "recovered_candidates": runtime_result["recovered_candidates"],
        "audit_scope": task.audit_scope,
        "target_vulnerabilities": task.target_vulnerabilities,
        "verification_level": task.verification_level,
        "exclude_patterns": task.exclude_patterns,
        "target_files": task.target_files,
    }
    try:
        return AgentTaskResponse(**response_data)
    except Exception as exc:
        logger.error(f"Error serializing task {task_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to serialize task data: {exc}")


@router.get("/{task_id}/debug-trace", response_model=DebugTraceResponse)
async def get_debug_trace(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    task = await db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    project = await db.get(Project, task.project_id)
    if not project or project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    events_result = await db.execute(
        select(AgentEvent)
        .where(AgentEvent.task_id == task_id)
        .order_by(AgentEvent.sequence)
    )
    events = events_result.scalars().all()
    return build_debug_trace_payload(
        task_id=task.id,
        task_name=task.name,
        task_status=str(task.status),
        events=events,
    )


@router.post("/{task_id}/resume")
async def resume_agent_task(
    task_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """Resume a cancelled, failed, or paused audit task from the latest checkpoint."""
    task = await db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    project = await db.get(Project, task.project_id)
    if not project or project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    if task.status == AgentTaskStatus.RUNNING:
        raise HTTPException(status_code=400, detail="Task is already running")
    if task.status == AgentTaskStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Completed tasks cannot be resumed")
    if task.status not in [AgentTaskStatus.CANCELLED, AgentTaskStatus.FAILED, AgentTaskStatus.PAUSED]:
        raise HTTPException(status_code=400, detail="Task is not resumable")

    _cancelled_tasks.discard(task_id)
    _prepare_task_for_resume(task)
    await db.commit()
    await _schedule_agent_task(background_tasks, task.id)
    logger.info(f"Resumed agent task {task.id}")
    return {
        "message": "Task resumed",
        "task_id": task.id,
        "status": task.status,
        "current_phase": task.current_phase,
    }


@router.post("/{task_id}/cancel")
async def cancel_agent_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """Cancel a running audit task."""
    task = await db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    project = await db.get(Project, task.project_id)
    if not project or project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    if task.status in [AgentTaskStatus.COMPLETED, AgentTaskStatus.FAILED, AgentTaskStatus.CANCELLED]:
        raise HTTPException(status_code=400, detail="Task is already finished")

    request_agent_task_cancellation(task_id)
    task.status = AgentTaskStatus.CANCELLED
    task.completed_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info(f"[Cancel] Task {task_id} cancelled successfully")
    return {"message": "Task cancelled", "task_id": task_id}


@router.get("/{task_id}/events")
async def stream_agent_events(
    task_id: str,
    after_sequence: int = Query(0, ge=0, description="Return events after this sequence number."),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """Stream persisted agent events via SSE."""
    task = await db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    project = await db.get(Project, task.project_id)
    if not project or project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    async def event_generator():
        last_sequence = after_sequence
        poll_interval = 0.5
        max_idle = 300
        idle_time = 0.0

        while True:
            async with async_session_factory() as session:
                result = await session.execute(
                    select(AgentEvent)
                    .where(AgentEvent.task_id == task_id)
                    .where(AgentEvent.sequence > last_sequence)
                    .order_by(AgentEvent.sequence)
                    .limit(50)
                )
                events = result.scalars().all()
                current_task = await session.get(AgentTask, task_id)
                task_status = current_task.status if current_task else None

            if events:
                idle_time = 0.0
                for event in events:
                    last_sequence = event.sequence
                    payload = {
                        "id": event.id,
                        "type": str(event.event_type),
                        "phase": str(event.phase) if event.phase else None,
                        "message": event.message,
                        "sequence": event.sequence,
                        "timestamp": event.created_at.isoformat() if event.created_at else None,
                        "progress_percent": _event_progress_percent(event),
                        "tool_name": event.tool_name,
                    }
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            else:
                idle_time += poll_interval

            status_str = str(task_status) if task_status is not None else None
            if status_str in ["completed", "failed", "cancelled"]:
                yield f"data: {json.dumps({'type': 'task_end', 'status': status_str})}\n\n"
                break
            if idle_time >= max_idle:
                yield f"data: {json.dumps({'type': 'timeout'})}\n\n"
                break
            await asyncio.sleep(poll_interval)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{task_id}/stream")
async def stream_agent_with_thinking(
    task_id: str,
    include_thinking: bool = Query(True, description="Include live LLM thinking events."),
    include_tool_calls: bool = Query(True, description="Include detailed tool call events."),
    after_sequence: int = Query(0, ge=0, description="Return events after this sequence number."),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """Enhanced SSE stream that prefers in-memory events while a task is running."""
    task = await db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    project = await db.get(Project, task.project_id)
    if not project or project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    def format_sse_event(event_data: Dict[str, Any]) -> str:
        event_type = event_data.get("event_type") or event_data.get("type") or "message"
        if "type" not in event_data:
            event_data["type"] = event_type
        return f"event: {event_type}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n"

    async def enhanced_event_generator():
        event_manager = _running_event_managers.get(task_id)
        skip_types = set()
        if not include_thinking:
            skip_types.update({"thinking_start", "thinking_token", "thinking_end"})
        if not include_tool_calls:
            skip_types.update({"tool_call_start", "tool_call_input", "tool_call_output", "tool_call_end"})

        if event_stream_enabled():
            event_stream = create_agent_event_stream()
            try:
                async for event in event_stream.stream_events(task_id, after_sequence=after_sequence):
                    event_type = event.get("event_type") or event.get("type")
                    if event_type in skip_types:
                        continue
                    yield format_sse_event(event)
                    if event_type == "thinking_token":
                        await asyncio.sleep(0.01)
                return
            except Exception as exc:
                logger.error(f"Redis event stream error: {exc}", exc_info=True)
            finally:
                await event_stream.close()

        if event_manager:
            logger.debug(f"Stream {task_id}: Using in-memory event manager")
            try:
                async for event in event_manager.stream_events(task_id, after_sequence=after_sequence):
                    event_type = event.get("event_type") or event.get("type")
                    if event_type in skip_types:
                        continue
                    yield format_sse_event(event)
                    if event_type == "thinking_token":
                        await asyncio.sleep(0.01)
                return
            except Exception as exc:
                logger.error(f"In-memory stream error: {exc}")
                yield format_sse_event({"type": "error", "message": str(exc)})
                return

        logger.debug(f"Stream {task_id}: Falling back to DB polling")
        last_sequence = after_sequence
        poll_interval = 2.0
        heartbeat_interval = 15.0
        max_idle = 60.0
        idle_time = 0.0
        last_heartbeat = 0.0

        while True:
            try:
                async with async_session_factory() as session:
                    result = await session.execute(
                        select(AgentEvent)
                        .where(AgentEvent.task_id == task_id)
                        .where(AgentEvent.sequence > last_sequence)
                        .order_by(AgentEvent.sequence)
                        .limit(100)
                    )
                    events = result.scalars().all()
                    current_task = await session.get(AgentTask, task_id)
                    task_status = current_task.status if current_task else None

                if events:
                    idle_time = 0.0
                    for event in events:
                        last_sequence = event.sequence
                        event_type = str(event.event_type)
                        if event_type in skip_types:
                            continue
                        payload = {
                            "id": event.id,
                            "type": event_type,
                            "phase": str(event.phase) if event.phase else None,
                            "message": event.message,
                            "sequence": event.sequence,
                            "timestamp": event.created_at.isoformat() if event.created_at else None,
                            "progress_percent": _event_progress_percent(event),
                            "tool_name": event.tool_name,
                        }
                        yield format_sse_event(payload)
                else:
                    idle_time += poll_interval
                    last_heartbeat += poll_interval

                status_str = str(task_status) if task_status is not None else None
                if status_str in ["completed", "failed", "cancelled"]:
                    yield format_sse_event({"type": "task_end", "status": status_str})
                    break
                if last_heartbeat >= heartbeat_interval:
                    last_heartbeat = 0.0
                    yield format_sse_event({"type": "heartbeat"})
                if idle_time >= max_idle:
                    yield format_sse_event({"type": "timeout"})
                    break
            except Exception as exc:
                logger.error(f"DB stream error: {exc}", exc_info=True)
                yield format_sse_event({"type": "error", "message": str(exc)})
                break

            await asyncio.sleep(poll_interval)

    return StreamingResponse(
        enhanced_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{task_id}/events/list", response_model=List[AgentEventResponse])
async def list_agent_events(
    task_id: str,
    after_sequence: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """Return persisted events for a task."""
    task = await db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    project = await db.get(Project, task.project_id)
    if not project or project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await db.execute(
        select(AgentEvent)
        .where(AgentEvent.task_id == task_id)
        .where(AgentEvent.sequence > after_sequence)
        .order_by(AgentEvent.sequence)
        .limit(limit)
    )
    return [_normalize_debug_event(event) for event in result.scalars().all()]


@router.get("/{task_id}/findings")
async def list_agent_findings(
    task_id: str,
    severity: Optional[str] = None,
    verified_only: bool = False,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """List findings for a task."""
    task = await db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    project = await db.get(Project, task.project_id)
    if not project or project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    query = select(AgentFinding).where(AgentFinding.task_id == task_id)
    if severity:
        try:
            query = query.where(AgentFinding.severity == VulnerabilitySeverity(severity))
        except ValueError:
            pass
    if verified_only:
        query = query.where(AgentFinding.is_verified.is_(True))

    result = await db.execute(query.order_by(AgentFinding.created_at.desc()).offset(skip).limit(limit))
    return [_serialize_agent_finding_record(item) for item in result.scalars().all()]


@router.get("/{task_id}/findings/{finding_id}")
async def get_agent_finding_detail(
    task_id: str,
    finding_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """Get a single finding with the rich vulnerability report fields."""
    task = await db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    project = await db.get(Project, task.project_id)
    if not project or project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    finding = await db.get(AgentFinding, finding_id)
    if not finding or finding.task_id != task_id:
        raise HTTPException(status_code=404, detail="Finding not found")
    return _serialize_agent_finding_record(finding)


@router.get("/{task_id}/summary", response_model=TaskSummaryResponse)
async def get_task_summary(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """Return a summary view for a task."""
    task = await db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    project = await db.get(Project, task.project_id)
    if not project or project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await db.execute(select(AgentFinding).where(AgentFinding.task_id == task_id))
    findings = result.scalars().all()
    severity_distribution: Dict[str, int] = {}
    vulnerability_types: Dict[str, int] = {}
    verified_count = 0
    for finding in findings:
        severity_key = str(finding.severity)
        type_key = str(finding.vulnerability_type)
        severity_distribution[severity_key] = severity_distribution.get(severity_key, 0) + 1
        vulnerability_types[type_key] = vulnerability_types.get(type_key, 0) + 1
        if finding.is_verified:
            verified_count += 1

    duration = None
    if task.started_at and task.completed_at:
        duration = int((task.completed_at - task.started_at).total_seconds())

    phases_result = await db.execute(
        select(AgentEvent.phase)
        .where(AgentEvent.task_id == task_id)
        .where(AgentEvent.event_type == AgentEventType.PHASE_COMPLETE)
        .distinct()
    )
    phases = [str(row[0]) for row in phases_result.fetchall() if row[0]]

    return TaskSummaryResponse(
        task_id=task_id,
        status=str(task.status),
        security_score=task.security_score,
        total_findings=len(findings),
        verified_findings=verified_count,
        severity_distribution=severity_distribution,
        vulnerability_types=vulnerability_types,
        duration_seconds=duration,
        phases_completed=phases,
    )


@router.patch("/{task_id}/findings/{finding_id}")
async def update_finding_status(
    task_id: str,
    finding_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """Update finding status."""
    status = body.get("status")
    if not status:
        raise HTTPException(status_code=400, detail="Missing status field")

    task = await db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    project = await db.get(Project, task.project_id)
    if not project or project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    finding = await db.get(AgentFinding, finding_id)
    if not finding or finding.task_id != task_id:
        raise HTTPException(status_code=404, detail="Finding not found")

    finding.status = status
    await db.commit()
    return {"message": "Finding status updated", "finding_id": finding_id, "status": status}


# ============ Helper Functions ============


def validate_git_url(url: str) -> bool:
    """Validate a git URL and reject obvious command-injection patterns."""
    if not url:
        return False
    from urllib.parse import urlparse

    parsed = urlparse(url)
    allowed_schemes = {"http", "https", "git", "ssh"}
    if parsed.scheme and parsed.scheme not in allowed_schemes:
        return False

    dangerous_patterns = [";", "|", "&", "$(", "`", "\n", "\r", "\t"]
    return not any(pattern in url for pattern in dangerous_patterns)


def validate_branch_name(branch: str) -> bool:
    """Validate a git branch name."""
    if not branch:
        return False
    if not re.match(r"^[a-zA-Z0-9_\-./]+$", branch):
        return False
    if ".." in branch or branch.startswith("/") or branch.endswith("/"):
        return False
    return len(branch) <= 256


def is_path_safe(base_path: str, target_path: str) -> bool:
    """Ensure a target path stays within a base directory."""
    abs_base = os.path.abspath(base_path)
    abs_target = os.path.abspath(os.path.join(base_path, target_path))
    return abs_target == abs_base or abs_target.startswith(abs_base + os.sep)


def safe_extract_zip(zip_ref: zipfile.ZipFile, extract_dir: str, task_id: str) -> None:
    """Safely extract a ZIP archive while preventing Zip Slip."""
    for index, member in enumerate(zip_ref.infolist()):
        if index % 50 == 0 and is_task_cancelled(task_id):
            raise asyncio.CancelledError("Task cancelled")
        filename = member.filename
        if not filename or filename.endswith("/"):
            continue
        if not is_path_safe(extract_dir, filename):
            logger.warning(f"Skipping unsafe ZIP member: {filename}")
            continue
        target_path = os.path.join(extract_dir, filename)
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with zip_ref.open(member) as src, open(target_path, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _find_managed_project_root_fallback(project: Project) -> Optional[str]:
    managed_root = os.path.abspath(settings.MANAGED_PROJECTS_ROOT)
    if not os.path.isdir(managed_root):
        return None

    entries: list[tuple[str, str]] = []
    for name in os.listdir(managed_root):
        if name == ".auditai_workspaces":
            continue
        path = os.path.join(managed_root, name)
        if os.path.isdir(path):
            entries.append((name, path))

    if not entries:
        return None

    project_name = str(project.name or "").strip().lower()
    if not project_name:
        return None

    exact_matches = [path for name, path in entries if name.lower() == project_name]
    if len(exact_matches) == 1:
        return exact_matches[0]

    normalized_project_name = re.sub(r"[^a-z0-9]+", "", project_name)
    prefix_matches = [
        path
        for name, path in entries
        if re.sub(r"[^a-z0-9]+", "", name.lower()).startswith(normalized_project_name)
    ]
    if len(prefix_matches) == 1:
        return prefix_matches[0]

    return None


async def _get_project_root(
    project: Project,
    task_id: str,
    branch_name: Optional[str] = None,
    github_token: Optional[str] = None,
    gitlab_token: Optional[str] = None,
    gitea_token: Optional[str] = None,
    ssh_private_key: Optional[str] = None,
    event_emitter: Optional[Any] = None,
    workspace_scope: str = "project",
    refresh: bool = False,
) -> str:
    """Prepare a local working copy for the project."""
    import subprocess
    from urllib.parse import urlparse, urlunparse
    from app.services.zip_storage import load_project_zip

    async def emit(message: str, level: str = "info") -> None:
        if not event_emitter:
            return
        if level == "warning":
            await event_emitter.emit_warning(message)
        elif level == "error":
            await event_emitter.emit_error(message)
        else:
            await event_emitter.emit_info(message)

    def check_cancelled() -> None:
        if is_task_cancelled(task_id):
            raise asyncio.CancelledError("Task cancelled")

    safe_task_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(task_id or "task")).strip(".-") or "task"
    safe_project_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(project.id or project.name or "project")).strip(".-") or "project"
    workspace_root = os.path.join(settings.MANAGED_PROJECTS_ROOT, ".auditai_workspaces")
    if workspace_scope == "project":
        base_path = os.path.join(workspace_root, "projects", safe_project_id)
    else:
        base_path = os.path.join(workspace_root, safe_task_id)

    def select_effective_root(root_path: str) -> str:
        items = [item for item in os.listdir(root_path) if not item.startswith("__") and not item.startswith(".")]
        if len(items) == 1:
            single_item_path = os.path.join(root_path, items[0])
            if os.path.isdir(single_item_path):
                return single_item_path
        return root_path

    if project.source_type == "zip":
        persistent_source = str(getattr(project, "local_path", "") or "").strip()
        if persistent_source and os.path.isdir(persistent_source):
            check_cancelled()
            await emit("Using persistent project source directory directly...")
            return select_effective_root(persistent_source)

    if refresh and os.path.exists(base_path):
        shutil.rmtree(base_path, ignore_errors=True)
    if os.path.isdir(base_path) and os.listdir(base_path):
        await emit(f"Reusing project workspace at: {base_path}")
        return select_effective_root(base_path)

    os.makedirs(base_path, exist_ok=True)
    check_cancelled()

    if project.source_type in {"zip", "local_directory"}:
        persistent_source = str(getattr(project, "local_path", "") or "").strip()
        if persistent_source and os.path.isdir(persistent_source):
            await emit("Copying persistent project source directory into the project workspace...")
            shutil.copytree(persistent_source, base_path, dirs_exist_ok=True)
        else:
            if project.source_type == "local_directory":
                raise RuntimeError("Local directory project is missing local_path")
            await emit("Extracting uploaded ZIP project...")
            zip_path = await load_project_zip(project.id)
            if not zip_path or not os.path.exists(zip_path):
                fallback_root = _find_managed_project_root_fallback(project)
                if fallback_root:
                    await emit(f"ZIP source missing; falling back to managed project directory: {fallback_root}", level="warning")
                    target_dir = os.path.join(base_path, os.path.basename(fallback_root))
                    shutil.copytree(fallback_root, target_dir, dirs_exist_ok=True)
                    base_path = target_dir
                else:
                    raise RuntimeError(f"Project ZIP not found: {project.id}")
            else:
                with zipfile.ZipFile(zip_path, "r") as zip_ref:
                    safe_extract_zip(zip_ref, base_path, task_id)
    elif project.source_type == "repository" and project.repository_url:
        repo_url = project.repository_url
        if not validate_git_url(repo_url):
            raise RuntimeError(f"Invalid repository URL: {repo_url}")
        branch = branch_name or project.default_branch or "main"
        if branch and not validate_branch_name(branch):
            raise RuntimeError(f"Invalid branch name: {branch}")

        await emit(f"Cloning repository: {repo_url}")
        target_dir = os.path.join(base_path, "repo")
        os.makedirs(target_dir, exist_ok=True)

        if GitSSHOperations.is_ssh_url(repo_url) and ssh_private_key:
            result = GitSSHOperations.clone_repo_with_ssh(repo_url, ssh_private_key, target_dir, branch)
            if not result.get("success"):
                raise RuntimeError(result.get("error") or result.get("message") or "SSH clone failed")
        else:
            auth_url = repo_url
            parsed = urlparse(repo_url)
            token = github_token or gitlab_token or gitea_token
            if token and parsed.scheme in {"http", "https"} and parsed.hostname:
                auth_url = urlunparse((parsed.scheme, f"oauth2:{token}@{parsed.netloc}", parsed.path, parsed.params, parsed.query, parsed.fragment))
            cmd = ["git", "clone", "--depth", "1"]
            if branch:
                cmd.extend(["--branch", branch])
            cmd.extend([auth_url, target_dir])
            completed = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "git clone failed")
        base_path = target_dir
    else:
        raise RuntimeError("Unsupported project source type")

    base_path = select_effective_root(base_path)

    await emit(f"Project prepared at: {base_path}")
    return base_path


class AgentTreeResponse(BaseModel):
    task_id: str
    total_agents: int
    total_findings: int
    total_iterations: int
    total_tool_calls: int
    total_tokens: int
    root_agent_id: Optional[str] = None
    nodes: Dict[str, Any]
    edges: List[Dict[str, Any]]


class CheckpointResponse(BaseModel):
    id: str
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    agent_type: Optional[str] = None
    iteration: int = 0
    status: Optional[str] = None
    total_tokens: int = 0
    tool_calls: int = 0
    findings_count: int = 0
    checkpoint_type: Optional[str] = None
    checkpoint_name: Optional[str] = None
    created_at: Optional[str] = None


@router.get("/{task_id}/tree", response_model=AgentTreeResponse)
async def get_agent_tree(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """Return the live or persisted agent tree for a task."""
    task = await db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    project = await db.get(Project, task.project_id)
    if not project or project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # 产品收敛: legacy 内存 agent 树(agent_registry)已删除, 恒走 DB 持久树路径。
    from app.models.agent_task import AgentTreeNode
    result = await db.execute(select(AgentTreeNode).where(AgentTreeNode.task_id == task_id))
    db_nodes = result.scalars().all()
    nodes = {
        node.agent_id: {
            "id": node.agent_id,
            "name": node.agent_name,
            "type": node.agent_type,
            "parent_id": node.parent_agent_id,
            "status": node.status,
            "task": node.task_description,
            "result": {"summary": node.result_summary, "findings": [None] * (node.findings_count or 0)},
            "tool_calls": node.tool_calls,
            "iterations": node.iterations,
            "tokens_used": node.tokens_used,
        }
        for node in db_nodes
    }
    edges = []
    root_agent_id = None
    total_iterations = 0
    total_tool_calls = 0
    total_tokens = 0
    total_findings = 0
    for agent_id, node in nodes.items():
        parent_id = node.get("parent_id")
        if parent_id:
            edges.append({"source": parent_id, "target": agent_id})
        else:
            root_agent_id = root_agent_id or agent_id
        total_iterations += int(node.get("iterations", 0) or 0)
        total_tool_calls += int(node.get("tool_calls", 0) or 0)
        total_tokens += int(node.get("tokens_used", 0) or 0)
        result = node.get("result") or {}
        if isinstance(result, dict):
            total_findings += len(result.get("findings", []) or [])

    if total_findings == 0:
        total_findings = int(task.findings_count or 0)

    return AgentTreeResponse(
        task_id=task_id,
        total_agents=len(nodes),
        total_findings=total_findings,
        total_iterations=total_iterations,
        total_tool_calls=total_tool_calls,
        total_tokens=total_tokens,
        root_agent_id=root_agent_id,
        nodes=nodes,
        edges=edges,
    )


@router.get("/{task_id}/checkpoints", response_model=List[CheckpointResponse])
async def list_checkpoints(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """List task checkpoints."""
    task = await db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    project = await db.get(Project, task.project_id)
    if not project or project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    from app.models.agent_task import AgentCheckpoint
    result = await db.execute(
        select(AgentCheckpoint)
        .where(AgentCheckpoint.task_id == task_id)
        .order_by(AgentCheckpoint.created_at.desc())
    )
    checkpoints = result.scalars().all()
    return [
        CheckpointResponse(
            id=cp.id,
            agent_id=cp.agent_id,
            agent_name=cp.agent_name,
            agent_type=cp.agent_type,
            iteration=cp.iteration or 0,
            status=cp.status,
            total_tokens=cp.total_tokens or 0,
            tool_calls=cp.tool_calls or 0,
            findings_count=cp.findings_count or 0,
            checkpoint_type=cp.checkpoint_type,
            checkpoint_name=cp.checkpoint_name,
            created_at=cp.created_at.isoformat() if cp.created_at else None,
        )
        for cp in checkpoints
    ]


@router.get("/{task_id}/checkpoints/{checkpoint_id}")
async def get_checkpoint_detail(
    task_id: str,
    checkpoint_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    """Return a checkpoint detail payload."""
    task = await db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    project = await db.get(Project, task.project_id)
    if not project or project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    from app.models.agent_task import AgentCheckpoint
    checkpoint = await db.get(AgentCheckpoint, checkpoint_id)
    if not checkpoint or checkpoint.task_id != task_id:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    state_data = {}
    if checkpoint.state_data:
        try:
            state_data = json.loads(checkpoint.state_data)
        except json.JSONDecodeError:
            state_data = {}

    return {
        "id": checkpoint.id,
        "task_id": checkpoint.task_id,
        "agent_id": checkpoint.agent_id,
        "agent_name": checkpoint.agent_name,
        "agent_type": checkpoint.agent_type,
        "parent_agent_id": checkpoint.parent_agent_id,
        "iteration": checkpoint.iteration,
        "status": checkpoint.status,
        "total_tokens": checkpoint.total_tokens,
        "tool_calls": checkpoint.tool_calls,
        "findings_count": checkpoint.findings_count,
        "checkpoint_type": checkpoint.checkpoint_type,
        "checkpoint_name": checkpoint.checkpoint_name,
        "state_data": state_data,
        "metadata": checkpoint.checkpoint_metadata,
        "created_at": checkpoint.created_at.isoformat() if checkpoint.created_at else None,
    }


@router.get("/{task_id}/report")
async def generate_audit_report(
    task_id: str,
    format: str = Query("markdown", pattern="^(markdown|json|html)$"),
    template_id: Optional[str] = Query(None, description="闂佺厧顨庢禍婊堟偩閻愵剛鈻曞璺侯儏琚氶梺鍛婄☉閿曘儴鍟梺?ID"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """Generate a final vulnerability report for the task."""
    from fastapi.responses import Response
    from app.models.report_template import AgentTaskReport
    from app.services.task_report_service import generate_task_report

    task = await db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    project = await db.get(Project, task.project_id)
    if not project or project.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await db.execute(
        select(AgentFinding)
        .where(AgentFinding.task_id == task_id)
        .order_by(
            case(
                (AgentFinding.severity == 'critical', 1),
                (AgentFinding.severity == 'high', 2),
                (AgentFinding.severity == 'medium', 3),
                (AgentFinding.severity == 'low', 4),
                else_=5,
            ),
            AgentFinding.created_at.desc(),
        )
    )
    findings = result.scalars().all()
    report = await generate_task_report(db, task, project, findings, template_id=template_id, output_format=format)
    await db.commit()

    if format == "json":
        return report.report_json

    media_type = "text/markdown"
    extension = "md"
    if format == "html":
        media_type = "text/html"
        extension = "html"
    filename = f"audit_report_{task.id[:8]}_{datetime.now().strftime('%Y%m%d')}.{extension}"
    return Response(report.content, media_type=media_type, headers={"Content-Disposition": f"attachment; filename={filename}"})


