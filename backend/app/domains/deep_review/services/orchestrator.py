from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Any, Literal

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ReviewInput
from app.domains.deep_review.schemas.output import (
    AgentObservation,
    BlastObservation,
    DeepReviewResult,
    PreparationReport,
    ProcessReport,
    ReviewMetrics,
    ReviewerDimensionReport,
)
from app.domains.deep_review.schemas.pipeline import (
    Anatomy,
    CrossAnalysisResult,
    EvidencePackage,
    ReviewDimension,
    ReviewFinding,
    ReviewPlan,
    SemanticBrief,
)
from app.domains.deep_review.agents.cross_analysis import run_cross_agent
from app.domains.deep_review.agents.planner import run_planner_agent
from app.domains.deep_review.agents.reviewer import ReviewerAgentOutcome, run_reviewer_agent
from app.domains.deep_review.agents.semantic import run_semantic_agent
from app.domains.deep_review.services.blast_radius import BlastResult, analyze_blast_radius
from app.domains.deep_review.services.head_tree import HeadTreeIndex
from app.domains.deep_review.services.diff_engine import build_anatomy
from app.domains.deep_review.services.evidence import build_evidence_packages
from app.domains.deep_review.services.input_builder import ReviewSnapshot, build_review_snapshot
from app.domains.deep_review.services.cross_repair import keep_all_result
from app.domains.deep_review.services.merge_gate import merge_findings, stable_business_hash
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
    blast_result: BlastResult = field(default_factory=BlastResult)
    head_tree: HeadTreeIndex | None = None
    diagnostics: list[str] = field(default_factory=list)
    anatomy: Anatomy | None = None


@dataclass(slots=True)
class ReviewerExecution:
    dimension_order: int
    outcome: ReviewerAgentOutcome
    duration_ms: int


def _safe_error(exc: BaseException) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    for key, secret in os.environ.items():
        sensitive = any(marker in key.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
        credential_url = key.upper().endswith(("_URL", "_DSN")) and "://" in secret and "@" in secret
        if (sensitive or credential_url) and len(secret) >= 8:
            message = message.replace(secret, "[REDACTED]")
            if credential_url:
                try:
                    password = urlsplit(secret).password
                except ValueError:
                    password = None
                if password and len(password) >= 4:
                    message = message.replace(password, "[REDACTED]")
    return message[:500]


def _blast_observation(result: BlastResult) -> BlastObservation:
    return BlastObservation(
        coverage_by_language=result.coverage_by_language,
        observed_hint_count=result.observed_hint_count,
        displayed_path_count=len(result.displayed_paths),
        scanned_file_count=result.scanned_file_count,
        scanned_bytes=result.scanned_bytes,
        truncated=result.truncated,
        diagnostics=list(result.diagnostics),
    )


class PreparationOrchestrator:
    def __init__(
        self,
        *,
        config: DeepReviewConfig,
        store: DeepReviewStore,
        runtime_factory: DeepReviewRuntimeFactory | None = None,
        reviewer_semaphore: asyncio.Semaphore | None = None,
    ):
        self.config = config
        self.store = store
        self.runtime_factory = runtime_factory
        self.reviewer_semaphore = reviewer_semaphore or asyncio.Semaphore(
            config.max_concurrent_reviewers
        )
        self._stage_durations_ms: dict[str, int] = {}

    async def run_preparation(
        self,
        run_id: str,
        review_input: ReviewInput,
        *,
        through: str,
    ) -> PreparationReport | DeepReviewResult:
        run_started = time.monotonic()
        context = await self._run_input_stage(run_id, review_input)
        await self._run_blast_radius_stage(run_id, context)
        anatomy = await self._run_anatomy_stage(run_id, context)
        context.anatomy = anatomy
        semantic = None
        plan = None
        observations: list[AgentObservation] = []
        reviewer_reports: list[ReviewerDimensionReport] = []
        candidates = []
        reviewer_counts: dict[str, int] = {}
        if through in {"planning", "review", "cross", "final"}:
            semantic, semantic_observation = await self._run_semantic_stage(run_id, context)
            plan, plan_observation = await self._run_planning_stage(run_id, context, semantic)
            observations = [semantic_observation, plan_observation]
        if through in {"review", "cross", "final"}:
            assert semantic is not None and plan is not None and context.anatomy is not None
            reviewer_reports, candidates, reviewer_observations, reviewer_counts = (
                await self._run_reviewer_stage(run_id, context, plan, semantic)
            )
            observations.extend(reviewer_observations)

        cross_result: CrossAnalysisResult | None = None
        cross_status: Literal["completed", "partial", "skipped"] | None = None
        cross_diagnostics: list[str] = []
        evidence: dict[int, EvidencePackage] = {}
        if through in {"cross", "final"}:
            assert semantic is not None and plan is not None
            evidence, evidence_diagnostics = await self._run_evidence_stage(run_id, context, candidates)
            context.diagnostics.extend(evidence_diagnostics)
            cross_result, cross_status, cross_diagnostics, cross_observation = (
                await self._run_cross_stage(
                    run_id, context, semantic, plan, reviewer_reports, candidates, evidence,
                )
            )
            if cross_observation is not None:
                observations.append(cross_observation)
        if through == "final":
            assert plan is not None and cross_result is not None and cross_status is not None
            return self._run_final_stage(
                run_id=run_id, context=context, plan=plan, reviewers=reviewer_reports,
                semantic=semantic,
                candidates=candidates, evidence=evidence, cross=cross_result,
                cross_status=cross_status, cross_diagnostics=cross_diagnostics,
                observations=observations, run_started=run_started,
            )

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
            blast_radius=_blast_observation(context.blast_result),
            diagnostics=context.diagnostics,
            semantic=semantic,
            plan=plan,
            reviewers=reviewer_reports,
            candidates=candidates,
            candidate_count=len(candidates),
            cross=cross_result,
            cross_status=cross_status,
            cross_diagnostics=cross_diagnostics,
            **reviewer_counts,
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

    async def _run_reviewer_stage(
        self,
        run_id: str,
        context: DeepReviewRunContext,
        plan: ReviewPlan,
        semantic: SemanticBrief,
    ) -> tuple[
        list[ReviewerDimensionReport],
        list[ReviewFinding],
        list[AgentObservation],
        dict[str, int],
    ]:
        assert context.anatomy is not None
        self._stage_started(run_id, "reviewer")
        started = time.monotonic()
        try:
            # Validate every cap before launching any model call, so a malformed
            # repaired Plan cannot partially spend on an over-wide dimension.
            active = [
                (order, dimension)
                for order, dimension in enumerate(plan.dimensions)
                if not dimension.deferred
            ]
            turn_limits = {
                order: self.config.reviewer_turn_limit(len(dimension.target_files))
                for order, dimension in active
            }
            executions = await self._execute_reviewers(
                run_id,
                context,
                semantic,
                active,
                turn_limits,
            )

            execution_by_order = {item.dimension_order: item for item in executions}
            reviewer_reports: list[ReviewerDimensionReport] = []
            observations: list[AgentObservation] = []
            findings: list[ReviewFinding] = []
            for order, dimension in enumerate(plan.dimensions):
                if dimension.deferred:
                    reviewer_reports.append(
                        ReviewerDimensionReport(
                            dimension_name=dimension.name,
                            dimension_order=order,
                            status="deferred",
                            target_files=list(dimension.target_files),
                            context_files=list(dimension.context_files),
                            error="deferred_by_plan_repair",
                        )
                    )
                    continue

                execution = execution_by_order[order]
                call = execution.outcome.call
                observation = _agent_observation("reviewer", call, dimension.name)
                observations.append(observation)
                if call.value is None:
                    report = ReviewerDimensionReport(
                        dimension_name=dimension.name,
                        dimension_order=order,
                        status="failed",
                        target_files=list(dimension.target_files),
                        context_files=list(dimension.context_files),
                        error=call.error or "unknown",
                    )
                    self._append_agent_event(
                        run_id,
                        "reviewer_failed",
                        observation,
                        dimension_order=order,
                        target_files=list(dimension.target_files),
                        duration_ms=execution.duration_ms,
                    )
                else:
                    status = "degraded" if execution.outcome.degraded else "succeeded"
                    report = ReviewerDimensionReport(
                        dimension_name=dimension.name,
                        dimension_order=order,
                        status=status,
                        target_files=list(dimension.target_files),
                        context_files=list(dimension.context_files),
                        summary=call.value.summary,
                        finding_count=len(call.value.findings),
                        diagnostics=list(execution.outcome.diagnostics),
                    )
                    findings.extend(call.value.findings)
                    self._append_agent_event(
                        run_id,
                        "reviewer_completed",
                        observation,
                        dimension_order=order,
                        status=status,
                        target_files=list(dimension.target_files),
                        duration_ms=execution.duration_ms,
                        summary=call.value.summary,
                        findings=[item.model_dump(mode="json") for item in call.value.findings],
                        diagnostics=list(execution.outcome.diagnostics),
                    )
                reviewer_reports.append(report)
                self._log_agent_observation(observation)

            started_count = len(active)
            succeeded_count = sum(item.status == "succeeded" for item in reviewer_reports)
            failed_count = sum(item.status == "failed" for item in reviewer_reports)
            deferred_count = sum(item.status == "deferred" for item in reviewer_reports)
            degraded_count = sum(item.status == "degraded" for item in reviewer_reports)
            if started_count and failed_count == started_count:
                raise PreparationError(
                    "all reviewer dimensions failed; refusing to continue"
                )

            candidates = _sort_candidates(findings, plan)
            self.store.append(
                run_id,
                "candidate_aggregation_completed",
                {
                    "candidate_count": len(candidates),
                    "candidates": [item.model_dump(mode="json") for item in candidates],
                },
            )

            counts = {
                "reviewer_dimensions_started": started_count,
                "reviewer_dimensions_succeeded": succeeded_count,
                "reviewer_dimensions_failed": failed_count,
                "reviewer_dimensions_deferred": deferred_count,
                "reviewer_dimensions_degraded": degraded_count,
            }
            self._stage_completed(
                run_id,
                "reviewer",
                started,
                **counts,
                candidate_count=len(candidates),
            )
            return reviewer_reports, candidates, observations, counts
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._stage_failed(run_id, "reviewer", exc, started)
            raise

    async def _run_evidence_stage(
        self, run_id: str, context: DeepReviewRunContext, candidates: list[ReviewFinding],
    ) -> tuple[dict[int, EvidencePackage], list[str]]:
        self._stage_started(run_id, "evidence")
        started = time.monotonic()
        try:
            packages = await build_evidence_packages(
                candidates, context.snapshot, context.blast_radius, self.config,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._stage_failed(run_id, "evidence", exc, started)
            raise
        empty = [
            index for index in range(len(candidates))
            if index not in packages or not any((
                packages[index].primary_code, packages[index].diff_hunk,
                packages[index].caller_snippets, packages[index].cross_ref_snippets,
                packages[index].related_code,
            ))
        ]
        diagnostics = [f"evidence_empty:{index}" for index in empty]
        truncated = [index for index, package in packages.items() if package.truncated]
        self.store.append(run_id, "evidence_completed", {
            "candidate_count": len(candidates), "evidence_count": len(packages),
            "empty_indices": empty, "truncated_indices": truncated,
        })
        self._stage_completed(
            run_id, "evidence", started, candidate_count=len(candidates),
            evidence_empty=len(empty), evidence_truncated=len(truncated),
        )
        return packages, diagnostics

    async def _run_cross_stage(
        self,
        run_id: str,
        context: DeepReviewRunContext,
        semantic: SemanticBrief,
        plan: ReviewPlan,
        reviewers: list[ReviewerDimensionReport],
        candidates: list[ReviewFinding],
        evidence: dict[int, EvidencePackage],
    ) -> tuple[CrossAnalysisResult, Literal["completed", "partial", "skipped"], list[str], AgentObservation | None]:
        self._stage_started(run_id, "cross")
        started = time.monotonic()
        if not candidates and not plan.cross_reference_hints:
            result = CrossAnalysisResult(summary="No candidates or outstanding cross-dimension hints.")
            self.store.append(run_id, "cross_skipped", {"reason": "no_candidates_or_hints"})
            self._stage_completed(run_id, "cross", started, status="skipped")
            return result, "skipped", [], None
        assert context.anatomy is not None and self.runtime_factory is not None
        try:
            async with asyncio.timeout(self.config.max_duration_seconds):
                outcome = await run_cross_agent(
                    self.runtime_factory,
                    snapshot=context.snapshot,
                    anatomy=context.anatomy,
                    semantic=semantic,
                    plan=plan,
                    reviewers=reviewers,
                    candidates=candidates,
                    evidence=evidence,
                    config=self.config,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Transcript persistence failures are not a trustworthy completed run.
            from app.domains.deep_review.agents.reviewer import _is_persistence_failure

            if _is_persistence_failure(exc):
                self._stage_failed(run_id, "cross", exc, started)
                raise
            fallback_reason = f"cross_failed:{type(exc).__name__}"
            result = keep_all_result(candidates, fallback_reason)
            diagnostics = [fallback_reason]
            observation = AgentObservation(stage="cross", error=_safe_error(exc))
            self._append_agent_event(run_id, "cross_failed", observation,
                                     candidate_count=len(candidates), diagnostics=diagnostics)
            self._stage_completed(run_id, "cross", started, status="partial")
            return result, "partial", diagnostics, observation

        call = outcome.call
        observation = _agent_observation("cross", call)
        self._log_agent_observation(observation)
        if call.value is None:
            reason = call.error or "cross_harness_incomplete"
            result = keep_all_result(candidates, reason)
            diagnostics = [f"cross_failed:{reason}"]
            self._append_agent_event(
                run_id, "cross_failed", observation,
                candidate_count=len(candidates), diagnostics=diagnostics,
            )
            self._stage_completed(run_id, "cross", started, status="partial")
            return result, "partial", diagnostics, observation

        status: Literal["completed", "partial"] = "partial" if outcome.partial else "completed"
        self._append_agent_event(
            run_id, "cross_completed", observation,
            status=status, candidate_count=len(candidates),
            result=call.value.model_dump(mode="json"), diagnostics=outcome.diagnostics,
        )
        self._stage_completed(
            run_id, "cross", started, status=status,
            kept=sum(item.result == "keep" for item in call.value.decisions),
            dropped=sum(item.result == "drop" for item in call.value.decisions),
            new_findings=len(call.value.new_findings),
        )
        return call.value, status, outcome.diagnostics, observation

    def _run_final_stage(
        self, *, run_id: str, context: DeepReviewRunContext,
        semantic: SemanticBrief, plan: ReviewPlan, reviewers: list[ReviewerDimensionReport],
        candidates: list[ReviewFinding], evidence: dict[int, EvidencePackage],
        cross: CrossAnalysisResult, cross_status: Literal["completed", "partial", "skipped"],
        cross_diagnostics: list[str], observations: list[AgentObservation],
        run_started: float,
    ) -> DeepReviewResult:
        self._stage_started(run_id, "final")
        started = time.monotonic()
        try:
            merged = merge_findings(candidates, cross, evidence, context.snapshot, self.config)
            coverage_gaps = [
                f"Reviewer dimension {item.dimension_name}: {item.status}"
                for item in reviewers if item.status != "succeeded"
            ]
            risks = list(dict.fromkeys(
                [*plan.unresolved_risks, *cross.unresolved_risks, *coverage_gaps]
            ))
            partial = (
                cross_status == "partial" or not plan.coverage_complete
                or bool(coverage_gaps) or bool(cross_diagnostics) or bool(merged.diagnostics)
            )
            status: Literal["completed", "partial"] = "partial" if partial else "completed"
            diagnostics = [*context.diagnostics, *cross_diagnostics, *merged.diagnostics]
            metrics = _review_metrics(
                context=context, plan=plan, observations=observations,
                elapsed_ms=max(0, int((time.monotonic() - run_started) * 1000)),
            )
            result = DeepReviewResult(
                run_id=run_id, status=status, findings=merged.findings,
                summary=cross.summary, unresolved_risks=risks,
                metrics=metrics, candidate_count=len(candidates),
                cross_status=cross_status, diagnostics=diagnostics,
                content_hash=stable_business_hash(merged.findings, cross.summary, risks, status),
            )
            final_event = {
                "status": status, "candidate_count": len(candidates),
                "finding_count": len(result.findings),
                "dropped_by_cross": merged.dropped_by_cross,
                "exact_duplicates": merged.exact_duplicates,
                "filtered": merged.filtered,
                "content_hash": result.content_hash,
                "findings": [item.model_dump(mode="json") for item in result.findings],
                "scores": [
                    {"score": item.score, "evidence_completeness": item.evidence_completeness,
                     "diff_proximity": item.diff_proximity}
                    for item in merged.scores
                ],
                "result": result.model_dump(mode="json"),
            }
            self.store.append(run_id, "final_result", final_event)
            self._stage_completed(
                run_id, "final", started, status=status,
                candidate_count=len(candidates), finding_count=len(result.findings),
            )
            decisions = context.snapshot.filter_result.decisions
            process_report = ProcessReport(
                run_id=run_id,
                base_commit=context.snapshot.base_commit,
                head_commit=context.snapshot.head_commit,
                merge_base=context.snapshot.merge_base,
                review_paths=context.snapshot.review_paths,
                context_paths=context.snapshot.context_paths,
                excluded_count=sum(item.action.value == "exclude" for item in decisions),
                stats=context.anatomy.stats,
                clusters=context.anatomy.clusters,
                related_paths=context.blast_radius,
                blast_radius=_blast_observation(context.blast_result),
                diagnostics=diagnostics,
                semantic=semantic,
                plan=plan,
                reviewers=reviewers,
                candidates=candidates,
                candidate_count=len(candidates),
                cross=cross,
                cross_status=cross_status,
                cross_diagnostics=cross_diagnostics,
                reviewer_dimensions_started=sum(item.status != "deferred" for item in reviewers),
                reviewer_dimensions_succeeded=sum(item.status == "succeeded" for item in reviewers),
                reviewer_dimensions_failed=sum(item.status == "failed" for item in reviewers),
                reviewer_dimensions_deferred=sum(item.status == "deferred" for item in reviewers),
                reviewer_dimensions_degraded=sum(item.status == "degraded" for item in reviewers),
                agent_observations=observations,
                stage_durations_ms=dict(self._stage_durations_ms),
                final_status=status,
                final_content_hash=result.content_hash,
                final_metrics=result.metrics,
            )
            self.store.append(run_id, "process_report", {"report": process_report.model_dump(mode="json")})
            self.store.append(run_id, "run_completed", {
                "status": status, "content_hash": result.content_hash,
            })
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._stage_failed(run_id, "final", exc, started)
            raise

    async def _execute_reviewers(
        self,
        run_id: str,
        context: DeepReviewRunContext,
        semantic: SemanticBrief,
        active: list[tuple[int, ReviewDimension]],
        turn_limits: dict[int, int],
    ) -> list[ReviewerExecution]:
        if not active:
            return []
        if self.runtime_factory is None:
            raise PreparationError("reviewer stage requires a deep review runtime factory")

        async def execute(order: int, dimension: ReviewDimension) -> ReviewerExecution:
            async with self.reviewer_semaphore:
                dimension_started = time.monotonic()
                self.store.append(
                    run_id,
                    "reviewer_started",
                    {
                        "dimension_order": order,
                        "dimension_name": dimension.name,
                        "target_files": list(dimension.target_files),
                        "max_turns": turn_limits[order],
                    },
                )
                outcome = await run_reviewer_agent(
                    self.runtime_factory,
                    snapshot=context.snapshot,
                    semantic=semantic,
                    dimension=dimension,
                    config=self.config,
                )
            return ReviewerExecution(
                dimension_order=order,
                outcome=outcome,
                duration_ms=max(0, int((time.monotonic() - dimension_started) * 1000)),
            )

        self.store.append(run_id, "reviewer_batch_started", {
            "dimensions": [
                {"dimension_order": order, "dimension_name": dimension.name}
                for order, dimension in active
            ],
        })
        tasks = [asyncio.create_task(execute(order, dimension)) for order, dimension in active]
        try:
            async with asyncio.timeout(self.config.max_duration_seconds):
                # gather preserves Plan order even when calls complete out of order.
                return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

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
                blast_result=context.blast_result,
                head_tree=context.head_tree,
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
            result, head_tree = await analyze_blast_radius(
                context.snapshot.review_paths,
                context.snapshot.input.repo_path,
                context.snapshot.head_commit,
                self.config,
                changes=context.snapshot.changes,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            context.diagnostics.append(f"blast_radius_degraded: {_safe_error(exc)}")
            result = BlastResult(coverage_by_language={"internal": "degraded"}, truncated=True,
                                 diagnostics=(f"dispatcher_failed:{type(exc).__name__}",))
            head_tree = None
        context.blast_result = result
        context.head_tree = head_tree
        context.blast_radius = result.displayed_paths
        self._stage_completed(
            run_id, "blast_radius", started,
            related_count=len(context.blast_radius), observed_hint_count=result.observed_hint_count,
            coverage_by_language=result.coverage_by_language, truncated=result.truncated,
            scanned_file_count=result.scanned_file_count, scanned_bytes=result.scanned_bytes,
        )

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
        self._stage_durations_ms[stage] = duration_ms
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
            logger.error("deep_review.stage_failed run_id=%s stage=%s error_type=%s error=%s",
                         run_id, stage, type(exc).__name__, error)

    def _append_agent_event(
        self,
        run_id: str,
        record_type: str,
        observation: AgentObservation,
        **details: Any,
    ) -> None:
        payload = observation.model_dump(mode="json")
        payload.update(details)
        self.store.append(run_id, record_type, payload)

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


def _agent_observation(
    stage: Literal["semantic", "planning", "reviewer", "cross"],
    call: AgentCallResult,
    dimension_name: str | None = None,
) -> AgentObservation:
    return AgentObservation(
        stage=stage,
        dimension_name=dimension_name,
        session_id=call.session_id,
        usage=call.usage,
        cost_usd=call.cost_usd,
        error=_safe_error(RuntimeError(call.error)) if call.error else None,
    )


def _review_metrics(
    *, context: DeepReviewRunContext, plan: ReviewPlan,
    observations: list[AgentObservation], elapsed_ms: int,
) -> ReviewMetrics:
    def token(item: AgentObservation, name: str, alternate: str | None = None) -> int | None:
        usage = item.usage or {}
        value = usage.get(name)
        if value is None and alternate is not None:
            value = usage.get(alternate)
        return value if isinstance(value, int) and value >= 0 else None

    totals = [token(item, "total_tokens") for item in observations]
    inputs = [token(item, "input_tokens", "prompt_tokens") for item in observations]
    outputs = [token(item, "output_tokens", "completion_tokens") for item in observations]
    costs = [item.cost_usd for item in observations]
    return ReviewMetrics(
        reviewed_files=len(context.snapshot.review_paths),
        excluded_files=sum(
            item.action.value == "exclude"
            for item in context.snapshot.filter_result.decisions
        ),
        dimensions=len(plan.dimensions),
        model_calls=len(observations),
        total_tokens=sum(value for value in totals if value is not None),
        input_tokens=sum(inputs) if inputs and all(value is not None for value in inputs) else None,
        output_tokens=sum(outputs) if outputs and all(value is not None for value in outputs) else None,
        cost_usd=sum(costs) if costs and all(value is not None for value in costs) else None,
        usage_complete=bool(totals) and all(
            value is not None for series in (totals, inputs, outputs) for value in series
        ) and all((item.usage or {}).get("usage_complete") is not False for item in observations),
        cost_available=bool(costs) and all(value is not None for value in costs),
        duration_ms=elapsed_ms,
    )


def _sort_candidates(
    findings: list[ReviewFinding], plan: ReviewPlan
) -> list[ReviewFinding]:
    dimension_order = {
        dimension.name: index for index, dimension in enumerate(plan.dimensions)
    }
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}

    def key(finding):
        canonical = json.dumps(
            finding.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            dimension_order.get(finding.dimension_name, len(dimension_order)),
            finding.file_path,
            finding.line_start or 0,
            severity_rank[finding.severity],
            finding.title,
            canonical,
        )

    return sorted(findings, key=key)
