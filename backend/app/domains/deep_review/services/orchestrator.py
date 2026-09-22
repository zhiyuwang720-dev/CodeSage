from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ReviewInput
from app.domains.deep_review.schemas.output import PreparationReport
from app.domains.deep_review.services.blast_radius import BlastRadiusError, compute_blast_radius
from app.domains.deep_review.services.diff_engine import build_anatomy
from app.domains.deep_review.services.input_builder import ReviewSnapshot, build_review_snapshot
from app.domains.deep_review.storage.protocol import DeepReviewStore, DeepReviewStoreError


logger = logging.getLogger(__name__)


class PreparationError(RuntimeError):
    """Raised when a non-input analysis stage fails."""


@dataclass(slots=True)
class DeepReviewRunContext:
    run_id: str
    snapshot: ReviewSnapshot
    blast_radius: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


def _safe_error(exc: BaseException) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:500]


class PreparationOrchestrator:
    def __init__(self, config: DeepReviewConfig, store: DeepReviewStore):
        self.config = config
        self.store = store

    async def run_preparation(self, run_id: str, review_input: ReviewInput) -> PreparationReport:
        context = await self._run_input_stage(run_id, review_input)
        await self._run_blast_radius_stage(run_id, context)
        anatomy = await self._run_anatomy_stage(run_id, context)

        decisions = context.snapshot.filter_result.decisions
        report = PreparationReport(
            run_id=run_id,
            mode="preparation",
            pipeline_complete=False,
            completed_stage="anatomy",
            base_commit=context.snapshot.base_commit,
            head_commit=context.snapshot.head_commit,
            merge_base=context.snapshot.merge_base,
            review_paths=context.snapshot.review_paths,
            context_paths=context.snapshot.context_paths,
            excluded_count=sum(item.action.value == "exclude" for item in decisions),
            stats=anatomy.stats,
            clusters=anatomy.clusters,
            related_paths=context.blast_radius,
            diagnostics=context.diagnostics,
        )
        self.store.append(
            run_id,
            "preparation_completed",
            {
                "completed_stage": "anatomy",
                "pipeline_complete": False,
                "review_count": len(report.review_paths),
                "context_count": len(report.context_paths),
                "excluded_count": report.excluded_count,
                "related_count": len(report.related_paths),
                "diagnostics": list(report.diagnostics),
            },
        )
        logger.info(
            "deep_review.preparation_completed run_id=%s review_count=%d related_count=%d",
            run_id,
            len(report.review_paths),
            len(report.related_paths),
        )
        return report

    async def _run_input_stage(self, run_id: str, review_input: ReviewInput) -> DeepReviewRunContext:
        self._stage_started(run_id, "input")
        started = time.monotonic()
        try:
            snapshot = await asyncio.to_thread(build_review_snapshot, review_input, self.config)
        except BaseException as exc:
            self._stage_failed(run_id, "input", exc, started)
            raise

        decisions = snapshot.filter_result.decisions
        self.store.append(
            run_id,
            "input_loaded",
            {
                "base_commit": snapshot.base_commit,
                "head_commit": snapshot.head_commit,
                "merge_base": snapshot.merge_base,
                "changed_count": len(snapshot.changes),
                "review_count": len(snapshot.review_paths),
                "context_count": len(snapshot.context_paths),
                "excluded_count": sum(item.action.value == "exclude" for item in decisions),
                "commit_message_count": len(snapshot.commit_messages),
            },
        )
        self.store.append(
            run_id,
            "filter_completed",
            {
                "completed_by_intake": True,
                "review_count": len(snapshot.review_paths),
                "context_count": len(snapshot.context_paths),
                "excluded_count": sum(item.action.value == "exclude" for item in decisions),
            },
        )
        self._stage_completed(run_id, "input", started, changed_count=len(snapshot.changes))
        return DeepReviewRunContext(run_id=run_id, snapshot=snapshot)

    async def _run_blast_radius_stage(self, run_id: str, context: DeepReviewRunContext) -> None:
        self._stage_started(run_id, "blast_radius")
        started = time.monotonic()
        try:
            related_paths = await compute_blast_radius(
                context.snapshot.review_paths,
                context.snapshot.input.repo_path,
                context.snapshot.head_commit,
                self.config,
            )
        except BlastRadiusError as exc:
            context.diagnostics.append(f"blast_radius_degraded: {_safe_error(exc)}")
            context.blast_radius = []
            self._stage_completed(run_id, "blast_radius", started, degraded=True, related_count=0)
            return
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._stage_failed(run_id, "blast_radius", exc, started)
            raise PreparationError(f"blast radius analysis failed: {_safe_error(exc)}") from exc

        context.blast_radius = related_paths
        self._stage_completed(run_id, "blast_radius", started, related_count=len(related_paths))

    async def _run_anatomy_stage(self, run_id: str, context: DeepReviewRunContext) -> Any:
        self._stage_started(run_id, "anatomy")
        started = time.monotonic()
        allowed_paths = context.snapshot.allowed_paths
        allowed_changes = [
            change for change in context.snapshot.changes if change.path in allowed_paths
        ]
        try:
            anatomy = build_anatomy(allowed_changes, context.blast_radius)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._stage_failed(run_id, "anatomy", exc, started)
            raise PreparationError(f"anatomy analysis failed: {_safe_error(exc)}") from exc
        self._stage_completed(
            run_id,
            "anatomy",
            started,
            files=len(anatomy.files),
            clusters=len(anatomy.clusters),
        )
        return anatomy

    def _stage_started(self, run_id: str, stage: str) -> None:
        self.store.append(run_id, "stage_started", {"stage": stage})
        logger.info("deep_review.stage_started run_id=%s stage=%s", run_id, stage)

    def _stage_completed(self, run_id: str, stage: str, started: float, **details: Any) -> None:
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        self.store.append(
            run_id,
            "stage_completed",
            {"stage": stage, "duration_ms": duration_ms, **details},
        )
        logger.info(
            "deep_review.stage_completed run_id=%s stage=%s duration_ms=%d %s",
            run_id,
            stage,
            duration_ms,
            " ".join(f"{key}={value}" for key, value in details.items()),
        )

    def _stage_failed(self, run_id: str, stage: str, exc: BaseException, started: float) -> None:
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        error = _safe_error(exc)
        try:
            self.store.append(
                run_id,
                "stage_failed",
                {"stage": stage, "duration_ms": duration_ms, "error_type": type(exc).__name__, "error": error},
            )
        finally:
            logger.exception("deep_review.stage_failed run_id=%s stage=%s", run_id, stage)
