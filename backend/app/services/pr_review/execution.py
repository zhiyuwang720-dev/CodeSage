"""快速 PR 审查应用服务入口。API 与 ARQ worker 共用此入口。"""
from __future__ import annotations

import asyncio
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.agent_task import AgentTask
from app.services.agent.prompts.review_prompts import (
    REVIEW_ARCHITECTURE_PROMPT,
    REVIEW_QUALITY_PROMPT,
    REVIEW_SECURITY_PROMPT,
)
from app.services.contracts.review_execution import (
    ReviewRunIdentity,
    build_config_fingerprint,
    sha256_bytes,
)
from app.services.pr_review.artifacts import LocalReviewArtifactStore
from app.services.pr_review.execution_ownership import (
    ActiveLeaseError,
    CancelRequestedError,
    ExecutionLease,
    StaleExecutionOwnerError,
    review_execution_ownership,
    current_execution_lease,
)
from app.services.pr_review.orchestrator import TOOL_MATRICES


QuickRunner = Callable[[str], Awaitable[None]]


@dataclass
class QuickReviewDependencies:
    session_factory: Callable[[], Any] = async_session_factory
    runner: QuickRunner | None = None
    worker_id: str | None = None
    artifact_root: str | None = None
    observer: Callable[[dict[str, Any]], Any] | None = None


def _worker_id(explicit: str | None) -> str:
    return explicit or f"{socket.gethostname()}:{os.getpid()}"


def _compatibility_config(task: AgentTask) -> dict[str, Any]:
    llm = dict(task.llm_config or {})
    safe_llm = {
        key: value
        for key, value in llm.items()
        if not any(marker in key.lower() for marker in ("key", "token", "secret", "password"))
    }
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
    }


async def build_review_identity(task: AgentTask) -> tuple[ReviewRunIdentity, bytes | None]:
    scope = (task.audit_scope or {}).get("pr_review") or {}
    diff_path = scope.get("diff_file_path")
    diff_bytes: bytes | None = None
    if diff_path:
        diff_bytes = await asyncio.to_thread(Path(diff_path).read_bytes)
    source_kind = "diff" if diff_bytes is not None else "git"
    base_sha = scope.get("base_sha") or task.commit_sha
    head_sha = scope.get("head_sha")
    repository_key = str(
        scope.get("repository_key")
        or task.repository_url_snapshot
        or task.project_id
    )
    identity = ReviewRunIdentity(
        run_id=str(uuid4()),
        task_id=str(task.id),
        source_kind=source_kind,
        repository_key=repository_key,
        pr_number=scope.get("pr_number"),
        base_sha=base_sha,
        head_sha=head_sha,
        diff_sha256=sha256_bytes(diff_bytes or b""),
        config_fingerprint=build_config_fingerprint(_compatibility_config(task)),
    )
    return identity, diff_bytes


async def _default_runner(task_id: str) -> None:
    # 兼容包装只保留在此处；worker 不再直接反向导入 API 私有实现。
    from app.api.v1.endpoints.agent_tasks import _execute_agent_task_impl

    await _execute_agent_task_impl(task_id)


async def _heartbeat(
    lease: ExecutionLease,
    dependencies: QuickReviewDependencies,
    run_task: asyncio.Task,
) -> None:
    while not run_task.done():
        await asyncio.sleep(5)
        try:
            async with dependencies.session_factory() as db:
                await review_execution_ownership.renew(db, lease)
        except (StaleExecutionOwnerError, CancelRequestedError):
            run_task.cancel()
            return


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
        candidate, diff_bytes = await build_review_identity(task)
        persisted = await review_execution_ownership.load_identity(db, task_id)
        if persisted is None:
            identity = candidate
            await review_execution_ownership.initialize(db, identity, delivery_id=delivery)
            artifact_store = LocalReviewArtifactStore(
                deps.artifact_root
                or str(Path(settings.MANAGED_PROJECTS_ROOT) / ".review_artifacts")
            )
            if diff_bytes is not None:
                input_ref = artifact_store.write_bytes(
                    run_id=identity.run_id,
                    kind="input_diff",
                    relative_path="input/review.diff",
                    content=diff_bytes,
                    media_type="text/x-diff",
                )
                agent_config = dict(task.agent_config or {})
                agent_config["review_execution_artifact_root"] = str(artifact_store.root)
                agent_config["review_execution_input_artifact"] = input_ref.model_dump(mode="json")
                task.agent_config = agent_config
                await db.commit()
        else:
            await review_execution_ownership.validate_resume_identity(db, candidate)
        try:
            lease = await review_execution_ownership.claim(
                db,
                task_id,
                worker_id=_worker_id(deps.worker_id),
                delivery_id=delivery,
            )
        except ActiveLeaseError:
            return "already_owned"

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

    token = current_execution_lease.set(lease)
    run_task = asyncio.create_task((deps.runner or _default_runner)(task_id))
    heartbeat = asyncio.create_task(_heartbeat(lease, deps, run_task))
    try:
        await run_task
        async with deps.session_factory() as db:
            await review_execution_ownership.assert_current_owner(db, lease)
            await db.commit()
        return "completed"
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        current_execution_lease.reset(token)
