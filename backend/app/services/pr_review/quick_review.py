"""快速 PR 审查用例协调器；不拥有 lease，也不直接提交 ORM 事务。"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy.orm import selectinload

from app.models.agent_task import AgentTask, AgentTaskStatus
from app.services.agent.event_manager import EventManager
from app.services.contracts.checkpoint import StageStatus
from app.services.contracts.review_execution import ArtifactRef
from app.services.pr_review.artifacts import LocalReviewArtifactStore
from app.services.pr_review.command_router import run_review_pipeline_async
from app.services.pr_review.dependencies import ReviewUseCaseDependencies
from app.services.pr_review.execution import current_review_llm_service
from app.services.pr_review.execution_events import build_review_event_sink
from app.services.pr_review.execution_ownership import (
    CancelRequestedError,
    current_execution_context,
    current_execution_lease,
)
from app.services.pr_review.results import review_result_service
from app.services.session.stage_store import audit_stage_store

logger = logging.getLogger(__name__)


def _changed_files(diff_text: str) -> int:
    return len({line[6:] for line in diff_text.splitlines() if line.startswith("+++ b/")})


async def _resume_state(db, task: AgentTask) -> tuple[dict, dict]:
    if not (task.agent_config or {}).get("resume_from_checkpoint"):
        return {}, {}
    stages = {item.stage_type: item for item in await audit_stage_store.list(db, str(task.id))}
    prefill: dict = {}
    sessions: dict = {}
    for perspective in ("security", "architecture", "quality"):
        stage = stages.get(f"review:{perspective}")
        if stage is None:
            continue
        if stage.status == StageStatus.running and stage.session_id:
            sessions[perspective] = stage.session_id
            continue
        if stage.status != StageStatus.completed:
            continue
        raw = dict(stage.state_payload or {}).get("stage_result")
        if not raw:
            raise ValueError(f"恢复阶段缺少 StageResult: review:{perspective}")
        from app.services.contracts.review_execution import StageResult

        checkpoint = StageResult.model_validate(raw)
        findings = [item.model_dump(mode="json") for item in checkpoint.findings]
        prefill[perspective] = {
            "from_agent": perspective,
            "to_agent": "orchestrator",
            "summary": f"(resume: {len(findings)} 条发现)",
            "key_findings": findings,
            "priority_areas": sorted({item["file_path"] for item in findings}),
            "context_data": {"resumed": True, "session_id": checkpoint.session_id},
            "confidence": 0.8,
        }
    return prefill, sessions


async def execute_review_use_case(
    task_id: str,
    dependencies: ReviewUseCaseDependencies | None = None,
) -> None:
    deps = dependencies or ReviewUseCaseDependencies()
    lease = current_execution_lease.get()
    context = current_execution_context.get()
    if lease is None or context is None:
        raise RuntimeError("快速审查必须在受管 ExecutionContext 中运行")

    event_manager = EventManager(
        db_session_factory=deps.async_session_factory,
        event_stream=deps.event_stream_factory() if deps.event_stream_factory else None,
    )
    event_manager.create_queue(task_id)
    try:
        async with deps.async_session_factory() as db:
            task = await db.get(AgentTask, task_id, options=[selectinload(AgentTask.project)])
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.status == AgentTaskStatus.CANCELLED:
                return
            config = dict(task.agent_config or {})
            reference = ArtifactRef.model_validate(config["review_execution_input_artifact"])
            artifact_root = str(config["review_execution_artifact_root"])
            diff = LocalReviewArtifactStore(artifact_root).read_verified(reference)
            if reference.sha256 != context.identity.diff_sha256:
                raise ValueError("执行输入与 ReviewRunIdentity 不一致")
            diff_text = diff.decode("utf-8", errors="replace")
            await review_result_service.register(db, task_id)
            await review_result_service.mark_running(
                db, task, changed_files=_changed_files(diff_text)
            )
            await review_result_service.complete_intake(
                db,
                task_id,
                payload={
                    "source_kind": context.identity.source_kind,
                    "base_sha": context.identity.base_sha,
                    "head_sha": context.identity.head_sha,
                    "changed_files": _changed_files(diff_text),
                    "artifact_refs": [reference.model_dump(mode="json")],
                },
            )
            prefill, sessions = await _resume_state(db, task)
            sink = build_review_event_sink(
                task_id,
                event_manager,
                db=db,
                result_service=review_result_service,
            )
            result = await run_review_pipeline_async(
                # 固定输入始终走 diff 适配入口；禁止 pipeline 再取移动 PR/ref。
                pr_url=None,
                diff_text=diff_text,
                user_context=((task.audit_scope or {}).get("pr_review") or {}).get("user_context"),
                options={
                    "engine": "runtime",
                    "task_id": task_id,
                    "repo": context.identity.repository_key,
                    "pr_number": context.identity.pr_number,
                    "min_severity": "low",
                    "max_comments": int(
                        (((task.audit_scope or {}).get("pr_review") or {}).get("max_comments") or 10)
                    ),
                    "max_turns": int(task.max_iterations or 50),
                    "session_factory": deps.sync_session_factory(),
                    "workspace_root": config.get("review_execution_source_dir")
                    or context.workspace_root,
                    "prefill_handoffs": prefill or None,
                    "resume_sessions": sessions or None,
                    "llm_service": deps.llm_service or current_review_llm_service.get(),
                },
                event_sink=sink,
            )
            await review_result_service.require_completion_gate(
                db, task_id, empty_reason=(result.meta or {}).get("empty_reason")
            )
            pr_meta = {
                "pr_url": ((task.audit_scope or {}).get("pr_review") or {}).get("pr_url"),
                "pr_number": context.identity.pr_number,
                "base_sha": context.identity.base_sha,
                "head_sha": context.identity.head_sha,
                "branch": task.branch_name,
            }
            await review_result_service.commit_success(
                db,
                task,
                lease,
                result.findings,
                pr_meta=pr_meta,
                artifact_root=artifact_root,
            )
    except (asyncio.CancelledError, CancelRequestedError):
        raise
    except BaseException as exc:
        logger.exception("quick review failed: %s", task_id)
        async with deps.async_session_factory() as failure_db:
            failure_task = await failure_db.get(AgentTask, task_id)
            if failure_task is not None:
                await review_result_service.mark_failed(
                    failure_db, failure_task, exc, lease=lease
                )
        return
    finally:
        await event_manager.close()
