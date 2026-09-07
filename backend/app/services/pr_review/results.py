"""快速 PR 审查的阶段与最终结果事务。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select

from app.models.agent_task import AgentFinding, AgentTask, AgentTaskPhase, AgentTaskStatus
from app.models.review_execution import ReviewExecutionRun
from app.services.contracts.checkpoint import StageStatus
from app.services.contracts.final_review_contract import ReviewFinding
from app.services.pr_review.artifacts import LocalReviewArtifactStore
from app.services.pr_review.execution_ownership import (
    CancelRequestedError,
    ExecutionLease,
    StaleExecutionOwnerError,
    review_execution_ownership,
)
from app.services.session.stage_store import audit_stage_store


QUICK_REVIEW_STAGES = [
    "intake",
    "review:security",
    "review:architecture",
    "review:quality",
    "report",
]


class IncompleteReviewError(RuntimeError):
    pass


class ReviewResultService:
    async def register(self, db, task_id: str) -> None:
        await audit_stage_store.register(db, task_id, QUICK_REVIEW_STAGES)

    async def mark_running(self, db, task: AgentTask, *, changed_files: int) -> None:
        if changed_files:
            task.total_files = changed_files
        task.status = AgentTaskStatus.RUNNING
        task.current_phase = AgentTaskPhase.ANALYSIS
        await db.commit()

    async def update_runtime_stats(
        self,
        db,
        task: AgentTask,
        *,
        iterations: int,
        tool_calls: int,
        tokens: int,
        token_stats: dict,
    ) -> None:
        task.total_iterations = max(task.total_iterations or 0, iterations)
        task.tool_calls_count = max(task.tool_calls_count or 0, tool_calls)
        task.tokens_used = tokens
        config = dict(task.agent_config or {})
        config["token_stats"] = token_stats
        task.agent_config = config
        await db.commit()

    async def complete_intake(self, db, task_id: str, *, payload: dict) -> None:
        await audit_stage_store.complete(db, task_id, "intake", payload=payload)

    async def touch_session(self, db, task_id: str, perspective: str, session_id: str) -> None:
        await audit_stage_store.touch_session(
            db, task_id, f"review:{perspective}", session_id
        )

    async def complete_perspective(
        self,
        db,
        task_id: str,
        perspective: str,
        *,
        session_id: str | None,
        findings: list[dict],
        stats: dict,
    ) -> None:
        # 关键 Checkpoint 失败必须向上冒泡，不能与 SSE 失败一起吞掉。
        await audit_stage_store.complete(
            db,
            task_id,
            f"review:{perspective}",
            session_id=session_id,
            findings=findings,
            stats=stats,
            payload={"perspective": perspective},
        )

    async def require_completion_gate(self, db, task_id: str, *, empty_reason: str | None) -> None:
        if empty_reason == "no_diff":
            return
        stages = {item.stage_type: item for item in await audit_stage_store.list(db, task_id)}
        unfinished = [
            perspective
            for perspective in ("security", "architecture", "quality")
            if stages.get(f"review:{perspective}") is None
            or stages[f"review:{perspective}"].status != StageStatus.completed
        ]
        if unfinished:
            raise IncompleteReviewError(f"视角未完成: {', '.join(unfinished)}")

    @staticmethod
    def _finding_row(task_id: str, finding: ReviewFinding) -> AgentFinding:
        row = AgentFinding(
            task_id=task_id,
            category=finding.category,
            vulnerability_type=None,
            severity=finding.severity,
            title=finding.title,
            description=finding.description,
            file_path=finding.file_path,
            line_start=finding.line_start,
            line_end=finding.line_end,
            code_snippet=finding.code_snippet,
            suggestion=finding.suggestion,
            ai_confidence=finding.confidence,
            is_verified=finding.verdict == "confirmed",
            finding_metadata={
                "review_payload_version": 1,
                "review_payload": finding.model_dump(mode="json"),
            },
        )
        row.fingerprint = row.generate_fingerprint()
        return row

    async def commit_success(
        self,
        db,
        task: AgentTask,
        lease: ExecutionLease,
        findings: Iterable[ReviewFinding],
        *,
        pr_meta: dict,
        artifact_root: str,
    ) -> int:
        """原子提交 Findings、report StageResult 与 COMPLETED 终态。"""

        normalized = list(findings)
        await review_execution_ownership.assert_current_owner(db, lease)
        execution = await db.get(ReviewExecutionRun, str(task.id))
        if execution is None:
            raise RuntimeError("缺少 ReviewExecutionRun")
        run_id = str((execution.identity_json or {}).get("run_id") or "")
        artifact = LocalReviewArtifactStore(artifact_root).write_bytes(
            run_id=run_id,
            kind="final_result",
            relative_path=f"result/final-{lease.lease_epoch}-{lease.attempt_id}.json",
            content=json.dumps(
                {
                    "review_payload_version": 1,
                    "findings": [item.model_dump(mode="json") for item in normalized],
                    "pr_meta": pr_meta,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8"),
            media_type="application/json",
        )
        try:
            # report complete 使用 caller 事务；任何校验/flush 失败都会回滚 findings 和终态。
            await audit_stage_store.complete(
                db,
                str(task.id),
                "report",
                findings=[item.model_dump(mode="json") for item in normalized],
                payload={
                    "findings_count": len(normalized),
                    "pr_meta": pr_meta,
                    "artifact_refs": [artifact.model_dump(mode="json")],
                },
                commit=False,
            )
            existing = {
                row.fingerprint: row
                for row in (
                    await db.execute(
                        select(AgentFinding).where(AgentFinding.task_id == str(task.id))
                    )
                ).scalars()
                if row.fingerprint
            }
            for finding in normalized:
                row = self._finding_row(str(task.id), finding)
                if row.fingerprint in existing:
                    current = existing[row.fingerprint]
                    current.category = row.category
                    current.severity = row.severity
                    current.title = row.title
                    current.description = row.description
                    current.line_end = row.line_end
                    current.finding_metadata = row.finding_metadata
                else:
                    db.add(row)
                    existing[row.fingerprint] = row
            config = dict(task.agent_config or {})
            config["pr_meta"] = pr_meta
            config.pop("resume_from_checkpoint", None)
            task.agent_config = config
            task.status = AgentTaskStatus.COMPLETED
            task.current_phase = AgentTaskPhase.REPORTING
            task.completed_at = datetime.now(timezone.utc)
            task.findings_count = len(normalized)
            task.error_message = None
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return len(normalized)

    async def mark_failed(
        self,
        db,
        task: AgentTask,
        error: BaseException | str,
        *,
        lease: ExecutionLease | None = None,
    ) -> None:
        await db.rollback()
        if lease is not None:
            try:
                await review_execution_ownership.assert_current_owner(db, lease)
            except (StaleExecutionOwnerError, CancelRequestedError):
                # 旧 owner 和已取消 attempt 均不得写失败收尾。
                await db.rollback()
                return
        task = await db.get(AgentTask, str(task.id))
        if task is None or task.status == AgentTaskStatus.CANCELLED:
            return
        task.status = AgentTaskStatus.FAILED
        task.current_phase = AgentTaskPhase.REPORTING
        task.completed_at = datetime.now(timezone.utc)
        task.error_message = str(error)
        await db.commit()


review_result_service = ReviewResultService()
