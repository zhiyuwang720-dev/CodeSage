from __future__ import annotations

import asyncio
import uuid

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ReviewInput
from app.domains.deep_review.schemas.output import DeepReviewResult, PreparationReport
from app.domains.deep_review.services.orchestrator import PreparationOrchestrator
from app.domains.deep_review.services.runtime import DeepReviewRuntimeFactory
from app.domains.deep_review.storage.protocol import DeepReviewStore, DeepReviewStoreError


SUPPORTED_THROUGH_STAGES = {"anatomy", "planning"}


class DeepReviewService:
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

    async def run(
        self,
        review_input: ReviewInput,
        *,
        through: str | None = None,
    ) -> PreparationReport | DeepReviewResult:
        requested_stage = through or "anatomy"
        if requested_stage not in SUPPORTED_THROUGH_STAGES:
            supported = ", ".join(sorted(SUPPORTED_THROUGH_STAGES))
            raise ValueError(
                f"unsupported --through stage: {requested_stage!r}; supported stages: {supported}"
            )
        if requested_stage == "planning" and self.runtime_factory is None:
            raise ValueError("planning requires a deep review runtime factory")

        run_id = f"{uuid.uuid4().hex[:12]}"
        try:
            self.store.append(
                run_id,
                "run_started",
                {"mode": "preparation", "through": requested_stage},
            )
        except DeepReviewStoreError:
            raise

        orchestrator = PreparationOrchestrator(
            config=self.config,
            store=self.store,
            runtime_factory=self.runtime_factory,
        )
        try:
            return await orchestrator.run_preparation(run_id, review_input, through=requested_stage)
        except asyncio.CancelledError:
            self._append_terminal(run_id, "run_cancelled", {"stage": "preparation"})
            raise
        except Exception as exc:
            self._append_terminal(
                run_id,
                "run_failed",
                {"stage": "preparation", "error_type": type(exc).__name__, "error": str(exc)[:500]},
            )
            raise

    def _append_terminal(self, run_id: str, record_type: str, payload: dict) -> None:
        try:
            self.store.append(run_id, record_type, payload)
        except DeepReviewStoreError:
            return
