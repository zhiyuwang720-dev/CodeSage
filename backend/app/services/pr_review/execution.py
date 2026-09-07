"""快速 PR 审查应用服务入口。API 与 ARQ worker 共用此入口。"""
from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import dataclass
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from app.core.config import settings
from app.db.session import async_session_factory
from app.db.session import get_pr_review_sync_session_factory
from app.models.agent_task import AgentTask, AgentTaskStatus
from app.services.agent.prompts.review_prompts import (
    REVIEW_ARCHITECTURE_PROMPT,
    REVIEW_QUALITY_PROMPT,
    REVIEW_SECURITY_PROMPT,
)
from app.services.contracts.review_execution import ExecutionContext
from app.services.pr_review.execution_ownership import (
    ActiveLeaseError,
    CancelRequestedError,
    ExecutionLease,
    StaleExecutionOwnerError,
    review_execution_ownership,
    current_execution_lease,
    current_execution_context,
)
from app.services.pr_review.orchestrator import TOOL_MATRICES
from app.services.pr_review.inputs import initialize_or_resume_review_input


QuickRunner = Callable[[str], Awaitable[None]]


@dataclass
class QuickReviewDependencies:
    session_factory: Callable[[], Any] = async_session_factory
    sync_session_factory: Callable[[], Any] = get_pr_review_sync_session_factory
    runner: QuickRunner | None = None
    worker_id: str | None = None
    artifact_root: str | None = None
    observer: Callable[[dict[str, Any]], Any] | None = None
    llm_service: Any | None = None
    event_stream_factory: Callable[[], Any] | None = None
    heartbeat_interval: float = 5.0


current_review_llm_service: ContextVar[Any | None] = ContextVar(
    "current_review_llm_service", default=None
)


def _worker_id(explicit: str | None) -> str:
    return explicit or f"{socket.gethostname()}:{os.getpid()}"


def build_review_compatibility_config(task: AgentTask) -> dict[str, Any]:
    llm = dict(task.llm_config or {})
    compatibility_fields = (
        "provider",
        "model",
        "base_url",
        "temperature",
        "max_tokens",
        "timeout",
        "endpoint_protocol",
        "tool_message_format",
        "disable_streaming",
    )
    safe_llm = {key: llm[key] for key in compatibility_fields if key in llm}
    return {
        "flow_version": "quick-review-v1",
        "model": safe_llm,
        "runtime": {
            "provider": settings.LLM_PROVIDER,
            "model": settings.LLM_MODEL,
            "temperature": settings.LLM_TEMPERATURE,
            "max_tokens": settings.LLM_MAX_TOKENS,
            "protocol": settings.LLM_ENDPOINT_PROTOCOL,
        },
        "prompts": {
            "security": REVIEW_SECURITY_PROMPT,
            "architecture": REVIEW_ARCHITECTURE_PROMPT,
            "quality": REVIEW_QUALITY_PROMPT,
        },
        "tools": {key: sorted(value) for key, value in TOOL_MATRICES.items()},
        "budget": {
            "max_iterations": int(task.max_iterations or 50),
            "timeout_seconds": int(task.timeout_seconds or 1800),
            "token_budget": int(task.token_budget or 100000),
        },
    }


async def _heartbeat(
    lease: ExecutionLease,
    dependencies: QuickReviewDependencies,
    run_task: asyncio.Task,
) -> None:
    while not run_task.done():
        await asyncio.sleep(dependencies.heartbeat_interval)
        try:
            async with dependencies.session_factory() as db:
                await review_execution_ownership.renew(db, lease)
        except (StaleExecutionOwnerError, CancelRequestedError):
            run_task.cancel()
            raise


async def execute_quick_review(
    task_id: str,
    dependencies: QuickReviewDependencies | None = None,
    *,
    delivery_id: str | None = None,
) -> str:
    """领取并执行一个完整快速审查；重复有效投递返回 ``already_owned``。"""

    deps = dependencies or QuickReviewDependencies()
    delivery = delivery_id or str(uuid4())
    async with deps.session_factory() as db:
        task = await db.get(AgentTask, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        if task.status == AgentTaskStatus.COMPLETED:
            return AgentTaskStatus.COMPLETED
        prepared = await initialize_or_resume_review_input(
            db,
            task,
            compatibility_config=build_review_compatibility_config(task),
            artifact_root=(
                deps.artifact_root
                or str(Path(settings.MANAGED_PROJECTS_ROOT) / ".review_artifacts")
            ),
            delivery_id=delivery,
        )
        try:
            lease = await review_execution_ownership.claim(
                db,
                task_id,
                worker_id=_worker_id(deps.worker_id),
                delivery_id=delivery,
            )
        except ActiveLeaseError:
            return "already_owned"
        if lease.lease_epoch > 1:
            # 过期 lease 接管与显式 resume 统一消费阶段检查点；claim 已完成 fencing，
            # 此处只在新 owner 下标记恢复意图，不修改稳定运行身份。
            await review_execution_ownership.assert_current_owner(db, lease)
            task = await db.get(AgentTask, task_id)
            agent_config = dict(task.agent_config or {})
            agent_config["resume_from_checkpoint"] = True
            task.agent_config = agent_config
            await db.commit()

    if deps.observer is not None:
        observed = deps.observer(
            {
                "type": "attempt_start",
                "task_id": task_id,
                "attempt_id": lease.attempt_id,
                "worker_id": lease.worker_id,
                "lease_epoch": lease.lease_epoch,
            }
        )
        if asyncio.iscoroutine(observed):
            await observed

    artifact_root = str(
        Path(deps.artifact_root or settings.MANAGED_PROJECTS_ROOT) / ".review_artifacts"
    ) if deps.artifact_root is None else str(Path(deps.artifact_root).resolve())
    execution_context = ExecutionContext(
            identity=prepared.identity,
        attempt_id=lease.attempt_id,
        worker_id=lease.worker_id,
        lease_epoch=lease.lease_epoch,
        workspace_root=str(
            (Path(settings.MANAGED_PROJECTS_ROOT) / ".auditai_workspaces" / "projects" / task_id).resolve()
        ),
        artifact_root=str(Path(artifact_root).resolve()),
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=int(task.timeout_seconds or 1800)),
    )
    lease_token = current_execution_lease.set(lease)
    context_token = current_execution_context.set(execution_context)
    llm_token = current_review_llm_service.set(deps.llm_service)
    if deps.runner is not None:
        review_coro = deps.runner(task_id)
    else:
        from app.services.pr_review.dependencies import ReviewUseCaseDependencies
        from app.services.pr_review.quick_review import execute_review_use_case

        review_coro = execute_review_use_case(
            task_id,
            ReviewUseCaseDependencies(
                async_session_factory=deps.session_factory,
                sync_session_factory=deps.sync_session_factory,
                llm_service=deps.llm_service,
                event_stream_factory=deps.event_stream_factory,
            ),
        )
    run_task = asyncio.create_task(review_coro)
    heartbeat = asyncio.create_task(_heartbeat(lease, deps, run_task))
    try:
        done, _ = await asyncio.wait(
            {run_task, heartbeat}, return_when=asyncio.FIRST_COMPLETED
        )
        if heartbeat in done:
            heartbeat_error = heartbeat.exception()
            if heartbeat_error is not None:
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
                if isinstance(heartbeat_error, CancelRequestedError):
                    return AgentTaskStatus.CANCELLED
                raise heartbeat_error
        await run_task
        async with deps.session_factory() as db:
            try:
                await review_execution_ownership.assert_current_owner(db, lease)
            except CancelRequestedError:
                return AgentTaskStatus.CANCELLED
            task = await db.get(AgentTask, task_id)
            result_status = str(task.status or "failed")
            if result_status not in {
                AgentTaskStatus.COMPLETED,
                AgentTaskStatus.CANCELLED,
                AgentTaskStatus.FAILED,
            }:
                raise RuntimeError(
                    f"quick review runner returned without terminal business status: {result_status}"
                )
            await review_execution_ownership.release(db, lease)
        return result_status
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        current_execution_context.reset(context_token)
        current_execution_lease.reset(lease_token)
        current_review_llm_service.reset(llm_token)
