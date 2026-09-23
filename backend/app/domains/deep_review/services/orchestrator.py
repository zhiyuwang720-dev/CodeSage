from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ReviewInput
from app.domains.deep_review.schemas.output import AgentObservation, PreparationReport
from app.domains.deep_review.schemas.pipeline import Anatomy, ReviewPlan, SemanticBrief
from app.domains.deep_review.agents.planner import run_planner_agent
from app.domains.deep_review.agents.semantic import run_semantic_agent
from app.domains.deep_review.services.blast_radius import BlastRadiusError, compute_blast_radius
from app.domains.deep_review.services.diff_engine import build_anatomy
from app.domains.deep_review.services.input_builder import ReviewSnapshot, build_review_snapshot
from app.domains.deep_review.services.runtime import AgentCallResult, DeepReviewRuntimeFactory
from app.domains.deep_review.storage.protocol import DeepReviewStore, DeepReviewStoreError
from app.domains.deep_review.tools.catalog import build_review_tools


logger = logging.getLogger(__name__)


class PreparationError(RuntimeError):
    """Raised when a non-input analysis stage fails."""


@dataclass(slots=True)
class DeepReviewRunContext:
    run_id: str
    snapshot: ReviewSnapshot
    blast_radius: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    anatomy: Anatomy | None = None


def _safe_error(exc: BaseException) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:500]


class PreparationOrchestrator:
    def __init__(
        self,
        *,
        config: DeepReviewConfig,
        store: DeepReviewStore,
        runtime_factory: DeepReviewRuntimeFactory | None = None,
    ):
        self.config = config
        self.store = store
        self.runtime_factory = runtime_factory

    async def run_preparation(
        self,
        run_id: str,
        review_input: ReviewInput,
        *,
        through: str,
    ) -> PreparationReport:
        context = await self._run_input_stage(run_id, review_input)
        await self._run_blast_radius_stage(run_id, context)
        anatomy = await self._run_anatomy_stage(run_id, context)
        context.anatomy = anatomy
        semantic = None
        plan = None
        observations: list[AgentObservation] = []
        if through == "planning":
            semantic, semantic_observation = await self._run_semantic_stage(run_id, context)
            plan, plan_observation = await self._run_planning_stage(run_id, context, semantic)
            observations = [semantic_observation, plan_observation]

        decisions = context.snapshot.filter_result.decisions
        report = PreparationReport(
            run_id=run_id,
            mode="preparation",
            pipeline_complete=False,
            completed_stage=through,
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
            semantic=semantic,
            plan=plan,
            agent_observations=observations,
        )
        self.store.append(
            run_id,
            "preparation_completed",
            {
                "completed_stage": through,
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

    async def _run_semantic_stage(
        self, run_id: str, context: DeepReviewRunContext
    ) -> tuple[SemanticBrief, AgentObservation]:
        assert context.anatomy is not None
        self._stage_started(run_id, "semantic")
        started = time.monotonic()
        try:
            assert self.runtime_factory is not None
            runtime = self.runtime_factory.for_role(self.config.semantic_role)
            call = await run_semantic_agent(
                runtime,
                snapshot=context.snapshot,
                anatomy=context.anatomy,
                config=self.config,
            )
            if call.value is None:
                raise PreparationError("semantic produced no business result")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._stage_failed(run_id, "semantic", exc, started)
            raise
        observation = _agent_observation("semantic", call)
        record_type = (
            "semantic_fallback" if call.value.source == "fallback" else "semantic_completed"
        )
        self._append_agent_event(run_id, record_type, observation)
        self._log_agent_observation(observation)
        self._stage_completed(
            run_id,
            "semantic",
            started,
            source=call.value.source,
            confidence=call.value.confidence,
        )
        return call.value, observation

    async def _run_planning_stage(
        self,
        run_id: str,
        context: DeepReviewRunContext,
        semantic: SemanticBrief,
    ) -> tuple[ReviewPlan, AgentObservation]:
        assert context.anatomy is not None
        self._stage_started(run_id, "planning")
        started = time.monotonic()
        try:
            assert self.runtime_factory is not None
            runtime = self.runtime_factory.for_role(self.config.planner_role)
            call = await run_planner_agent(
                runtime,
                snapshot=context.snapshot,
                anatomy=context.anatomy,
                semantic=semantic,
                config=self.config,
                tools=build_review_tools(
                    repo_path=context.snapshot.input.repo_path,
                    head_commit=context.snapshot.head_commit,
                    snapshot=context.snapshot,
                    config=self.config,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._stage_failed(run_id, "planning", exc, started)
            raise
        observation = _agent_observation("planning", call)
        if call.value is None:
            self._append_agent_event(run_id, "plan_failed", observation)
            self._log_agent_observation(observation)
            self._stage_failed(run_id, "planning", RuntimeError(call.error or "unknown"), started)
            raise PreparationError(f"planning failed: {call.error or 'unknown'}")
        self._append_agent_event(run_id, "plan_completed", observation)
        self._log_agent_observation(observation)
        self._stage_completed(
            run_id,
            "planning",
            started,
            dimensions=len(call.value.dimensions),
            coverage_complete=call.value.coverage_complete,
            deferred=sum(item.deferred for item in call.value.dimensions),
        )
        return call.value, observation

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
                diagnostics=context.diagnostics,
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
            anatomy = build_anatomy(
                allowed_changes,
                context.blast_radius,
                directory_depth=self.config.cluster_directory_depth,
                max_files_per_cluster=self.config.max_cluster_files,
            )
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

    def _append_agent_event(
        self, run_id: str, record_type: str, observation: AgentObservation
    ) -> None:
        self.store.append(run_id, record_type, observation.model_dump(mode="json"))

    @staticmethod
    def _log_agent_observation(observation: AgentObservation) -> None:
        usage = observation.usage or {}
        logger.info(
            "deep_review.agent_completed stage=%s session_id=%s input_tokens=%s "
            "output_tokens=%s total_tokens=%s cost_available=%s error=%s",
            observation.stage,
            observation.session_id or "none",
            usage.get("input_tokens", usage.get("prompt_tokens")),
            usage.get("output_tokens", usage.get("completion_tokens")),
            usage.get("total_tokens"),
            observation.cost_usd is not None,
            observation.error or "none",
        )


def _agent_observation(stage: str, call: AgentCallResult) -> AgentObservation:
    return AgentObservation(
        stage=stage,
        session_id=call.session_id,
        usage=call.usage,
        cost_usd=call.cost_usd,
        error=call.error,
    )
