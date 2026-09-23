from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ReviewInput
from app.domains.deep_review.schemas.output import PreparationReport
from app.domains.deep_review.agents.reviewer import ReviewerAgentOutcome
from app.domains.deep_review.schemas.pipeline import (
    Anatomy,
    ReviewDimension,
    ReviewFinding,
    ReviewPlan,
    ReviewerResult,
    SemanticBrief,
)
from app.domains.deep_review.services import orchestrator as orchestrator_module
from app.domains.deep_review.services.diff_engine import build_anatomy
from app.domains.deep_review.services.input_builder import ReviewSnapshot, build_review_snapshot
from app.domains.deep_review.services.runtime import AgentCallResult
from app.domains.deep_review.services.orchestrator import (
    DeepReviewRunContext,
    PreparationError,
    PreparationOrchestrator,
    _sort_candidates,
)
from app.domains.deep_review.services.service import DeepReviewService
from app.domains.deep_review.storage.protocol import DeepReviewStoreError


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


@pytest.fixture
def snapshot(tmp_path: Path) -> ReviewSnapshot:
    repo = tmp_path / "parallel-review-repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.name", "Parallel Test")
    git(repo, "config", "user.email", "parallel@example.test")
    for index in range(6):
        (repo / f"file{index}.py").write_text("value = 1\nsecond = 2\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    for index in range(6):
        (repo / f"file{index}.py").write_text("value = 2\nsecond = 2\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "head")
    head = git(repo, "rev-parse", "HEAD")
    return build_review_snapshot(
        ReviewInput(repo_path=str(repo), base_ref=base, head_ref=head),
        DeepReviewConfig(),
    )


@dataclass
class MemoryStore:
    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    fail_after: int | None = None

    def append(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        if self.fail_after is not None and len(self.events) >= self.fail_after:
            raise DeepReviewStoreError("injected storage failure")
        self.events.append((run_id, kind, dict(payload)))


class NoRuntimeFactory:
    def for_role(self, role: str) -> object:
        raise AssertionError("preparation pipeline must not initialize a model runtime")


@pytest.fixture()
def preparation_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "preparation-repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "old.py").write_text("old()\n", encoding="utf-8")
    (repo / ".env").write_text("secret = base\n", encoding="utf-8")
    (repo / "normal.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "caller.py").write_text("from normal import VALUE\n\nprint(VALUE)\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    return repo


@pytest.fixture()
def prepared_head(preparation_repo: Path) -> tuple[str, str]:
    (preparation_repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    (preparation_repo / "new.py").write_text("print('new')\n", encoding="utf-8")
    (preparation_repo / ".env").write_text("secret = head\n", encoding="utf-8")
    (preparation_repo / "normal.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(preparation_repo, "add", "a.py", "new.py", ".env", "normal.py")
    git(preparation_repo, "rm", "old.py")
    git(preparation_repo, "commit", "-m", "head")
    return git(preparation_repo, "rev-parse", "HEAD^"), git(preparation_repo, "rev-parse", "HEAD")


def make_service(store: MemoryStore, max_concurrency: int = 4) -> DeepReviewService:
    return DeepReviewService(
        config=DeepReviewConfig(max_concurrent_reviewers=max_concurrency),
        store=store,
        runtime_factory=NoRuntimeFactory(),
    )


@dataclass
class RuntimeCoordinator:
    delay_by_name: dict[str, float] = field(default_factory=dict)
    fail_names: set[str] = field(default_factory=set)
    empty_names: set[str] = field(default_factory=set)
    gate: asyncio.Event | None = None
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    active: int = 0
    peak: int = 0
    calls: list[str] = field(default_factory=list)


class FakeRuntime:
    def __init__(self, coordinator: RuntimeCoordinator):
        self.coordinator = coordinator

    async def harness(self, prompt: str, **kwargs: Any):
        dimension_text = prompt.split("<dimension_json>\n", 1)[1].split("\n</dimension_json>", 1)[0]
        scope_text = prompt.split("<scope_json>\n", 1)[1].split("\n</scope_json>", 1)[0]
        import json

        name = json.loads(dimension_text)["name"]
        target = json.loads(scope_text)["target_files"][0]
        state = self.coordinator
        state.calls.append(name)
        state.active += 1
        state.peak = max(state.peak, state.active)
        state.entered.set()
        try:
            await asyncio.sleep(state.delay_by_name.get(name, 0))
            if state.gate is not None:
                await state.gate.wait()
            if name in state.fail_names:
                raise RuntimeError("fake provider failure")
            findings = [] if name in state.empty_names else [
                {
                    "file_path": target,
                    "line_start": 1,
                    "line_end": 1,
                    "severity": "high",
                    "title": f"Issue for {name}",
                    "body": f"When {name} is used, this changed behavior violates its contract.",
                    "evidence": f"{target}:1 sets value = 2.",
                    "suggestion": "Restore the required behavior.",
                    "confidence": 0.9,
                    "tags": [],
                }
            ]
            parsed = kwargs["schema"].model_validate(
                {"findings": findings, "summary": f"Completed {name}."}
            )
            return SimpleNamespace(
                parsed=parsed,
                session_id=f"session-{name}",
                usage={"total_tokens": 10},
                cost_usd=None,
            )
        finally:
            state.active -= 1


class FakeFactory:
    def __init__(self, coordinator: RuntimeCoordinator):
        self.coordinator = coordinator

    def for_role(self, role: str):
        assert role == "deep_review:reviewer"
        return FakeRuntime(self.coordinator)


def build_dimension(name: str, path: str, *, deferred: bool = False) -> ReviewDimension:
    return ReviewDimension(
        name=name,
        review_prompt="Trace the changed value and report only a verified behavior defect.",
        target_files=[path],
        deferred=deferred,
    )


def setup_orchestrator(
    snapshot: ReviewSnapshot,
    *,
    dimensions: list[ReviewDimension],
    concurrency: int = 2,
    coordinator: RuntimeCoordinator | None = None,
):
    store = MemoryStore()
    coordinator = coordinator or RuntimeCoordinator()
    config = DeepReviewConfig(max_concurrent_reviewers=concurrency)
    anatomy = build_anatomy(snapshot.changes)
    orchestrator = PreparationOrchestrator(
        config=config,
        store=store,
        runtime_factory=FakeFactory(coordinator),
        reviewer_semaphore=asyncio.Semaphore(concurrency),
    )
    context = DeepReviewRunContext(
        run_id="run-review-test",
        snapshot=snapshot,
        anatomy=anatomy,
    )
    plan = ReviewPlan(dimensions=dimensions, coverage_complete=True)
    return orchestrator, store, coordinator, context, plan, SemanticBrief(narrative="Semantic lead.")


@pytest.mark.asyncio
async def test_parallel_limit_and_candidate_order_follow_plan_not_completion(snapshot: ReviewSnapshot) -> None:
    dimensions = [
        build_dimension("first", "file5.py"),
        build_dimension("second", "file0.py"),
        build_dimension("third", "file3.py"),
        build_dimension("fourth", "file1.py"),
        build_dimension("fifth", "file4.py"),
        build_dimension("sixth", "file2.py"),
    ]
    coordinator = RuntimeCoordinator(
        delay_by_name={"first": 0.05, "second": 0.001, "third": 0.02},
    )
    orchestrator, store, coordinator, context, plan, semantic = setup_orchestrator(
        snapshot, dimensions=dimensions, concurrency=2, coordinator=coordinator,
    )

    reports, candidates, observations, counts = await orchestrator._run_reviewer_stage(
        "run-review-test", context, plan, semantic,
    )

    assert 1 < coordinator.peak <= 2
    assert coordinator.calls[0] == "first"
    assert [item.dimension_name for item in reports] == [item.name for item in dimensions]
    assert [item.dimension_name for item in candidates] == [item.name for item in dimensions]
    assert [item.dimension_name for item in observations] == [item.name for item in dimensions]
    assert counts["reviewer_dimensions_started"] == 6
    assert counts["reviewer_dimensions_succeeded"] == 6
    completed = [payload["dimension_name"] for _, kind, payload in store.events if kind == "reviewer_completed"]
    assert completed == [item.name for item in dimensions]


@pytest.mark.asyncio
async def test_partial_failure_keeps_successful_results_and_empty_success_counts(snapshot: ReviewSnapshot) -> None:
    dimensions = [
        build_dimension("failed", "file0.py"),
        build_dimension("empty", "file1.py"),
        build_dimension("found", "file2.py"),
    ]
    coordinator = RuntimeCoordinator(fail_names={"failed"}, empty_names={"empty"})
    orchestrator, store, coordinator, context, plan, semantic = setup_orchestrator(
        snapshot, dimensions=dimensions, coordinator=coordinator,
    )
    reports, candidates, observations, counts = await orchestrator._run_reviewer_stage(
        "run-review-test", context, plan, semantic,
    )
    assert [item.status for item in reports] == ["failed", "succeeded", "succeeded"]
    assert reports[1].finding_count == 0
    assert [item.dimension_name for item in candidates] == ["found"]
    assert counts["reviewer_dimensions_failed"] == 1
    assert counts["reviewer_dimensions_succeeded"] == 2
    assert len(observations) == 3
    assert any(kind == "reviewer_failed" for _, kind, _ in store.events)


@pytest.mark.asyncio
async def test_all_reviewer_failures_stop_stage_and_are_recorded(snapshot: ReviewSnapshot) -> None:
    dimensions = [build_dimension("bad-a", "file0.py"), build_dimension("bad-b", "file1.py")]
    coordinator = RuntimeCoordinator(fail_names={"bad-a", "bad-b"})
    orchestrator, store, _, context, plan, semantic = setup_orchestrator(
        snapshot, dimensions=dimensions, coordinator=coordinator,
    )
    with pytest.raises(PreparationError, match="all reviewer dimensions"):
        await orchestrator._run_reviewer_stage("run-review-test", context, plan, semantic)
    assert not any(kind == "candidate_aggregation_completed" for _, kind, _ in store.events)
    assert any(kind == "stage_failed" and payload["stage"] == "reviewer" for _, kind, payload in store.events)


@pytest.mark.asyncio
async def test_all_degraded_reviewer_results_complete_stage_with_diagnostics(
    snapshot: ReviewSnapshot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dimensions = [build_dimension("degraded-a", "file0.py"), build_dimension("degraded-b", "file1.py")]
    orchestrator, store, _, context, plan, semantic = setup_orchestrator(
        snapshot, dimensions=dimensions,
    )

    async def degraded_reviewer(*args: Any, **kwargs: Any) -> ReviewerAgentOutcome:
        return ReviewerAgentOutcome(
            call=AgentCallResult(value=ReviewerResult(summary="Model output was rejected by the mapper.")),
            diagnostics=["all_findings_rejected"],
            rejected_count=1,
        )

    monkeypatch.setattr(orchestrator_module, "run_reviewer_agent", degraded_reviewer)
    reports, candidates, observations, counts = await orchestrator._run_reviewer_stage(
        "run-review-test", context, plan, semantic,
    )

    assert [report.status for report in reports] == ["degraded", "degraded"]
    assert candidates == []
    assert [(item.dimension_name, item.error) for item in observations] == [
        ("degraded-a", None),
        ("degraded-b", None),
    ]
    assert counts["reviewer_dimensions_succeeded"] == 0
    assert counts["reviewer_dimensions_failed"] == 0
    assert counts["reviewer_dimensions_degraded"] == 2
    assert any(kind == "stage_completed" and payload["stage"] == "reviewer" for _, kind, payload in store.events)
    assert not any(kind == "stage_failed" and payload["stage"] == "reviewer" for _, kind, payload in store.events)


@pytest.mark.asyncio
async def test_unrepaired_seventeen_file_dimension_is_rejected_before_any_call(
    snapshot: ReviewSnapshot,
) -> None:
    wide = build_dimension("too-wide", "file0.py")
    wide.target_files = [f"wide{index}.py" for index in range(17)]
    orchestrator, store, coordinator, context, plan, semantic = setup_orchestrator(
        snapshot,
        dimensions=[build_dimension("valid", "file0.py"), wide],
    )
    with pytest.raises(ValueError, match="maximum is 16"):
        await orchestrator._run_reviewer_stage("run-review-test", context, plan, semantic)
    assert coordinator.calls == []
    assert not any(kind == "reviewer_started" for _, kind, _ in store.events)


def test_equal_candidate_sort_keys_use_canonical_business_json_tiebreak() -> None:
    plan = ReviewPlan(dimensions=[build_dimension("same", "file0.py")])
    first = ReviewFinding(
        file_path="file0.py", line_start=1, severity="high", title="Same title",
        body="Zebra claim is the same issue.", dimension_name="same",
    )
    second = first.model_copy(update={"body": "Alpha claim is the same issue."})
    forward = _sort_candidates([first, second], plan)
    reverse = _sort_candidates([second, first], plan)
    assert [item.model_dump_json() for item in forward] == [item.model_dump_json() for item in reverse]
    assert forward[0].body == "Alpha claim is the same issue."


@pytest.mark.asyncio
async def test_deferred_dimension_is_not_called_and_remains_visible(snapshot: ReviewSnapshot) -> None:
    dimensions = [
        build_dimension("active", "file0.py"),
        build_dimension("deferred", "file1.py", deferred=True),
    ]
    orchestrator, _, coordinator, context, plan, semantic = setup_orchestrator(
        snapshot, dimensions=dimensions,
    )
    reports, _, _, counts = await orchestrator._run_reviewer_stage(
        "run-review-test", context, plan, semantic,
    )
    assert coordinator.calls == ["active"]
    assert [item.status for item in reports] == ["succeeded", "deferred"]
    assert counts["reviewer_dimensions_deferred"] == 1


@pytest.mark.asyncio
async def test_empty_plan_skips_reviewer_calls_and_returns_zero_candidates(snapshot: ReviewSnapshot) -> None:
    orchestrator, store, coordinator, context, plan, semantic = setup_orchestrator(
        snapshot, dimensions=[],
    )
    reports, candidates, observations, counts = await orchestrator._run_reviewer_stage(
        "run-review-test", context, plan, semantic,
    )
    assert reports == []
    assert candidates == []
    assert observations == []
    assert coordinator.calls == []
    assert counts["reviewer_dimensions_started"] == 0
    assert any(kind == "candidate_aggregation_completed" for _, kind, _ in store.events)


@pytest.mark.asyncio
async def test_cancelling_stage_reclaims_all_tasks_and_semaphore(snapshot: ReviewSnapshot) -> None:
    gate = asyncio.Event()
    coordinator = RuntimeCoordinator(gate=gate)
    dimensions = [build_dimension(f"dimension-{index}", f"file{index}.py") for index in range(6)]
    orchestrator, _, coordinator, context, plan, semantic = setup_orchestrator(
        snapshot, dimensions=dimensions, concurrency=2, coordinator=coordinator,
    )
    task = asyncio.create_task(
        orchestrator._run_reviewer_stage("run-review-test", context, plan, semantic)
    )
    await asyncio.wait_for(coordinator.entered.wait(), timeout=1)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coordinator.active == 0
    assert len(coordinator.calls) <= 2
    assert sum(kind == "reviewer_started" for _, kind, _ in orchestrator.store.events) <= 2
    acquired = 0
    for _ in range(2):
        await asyncio.wait_for(orchestrator.reviewer_semaphore.acquire(), timeout=0.1)
        acquired += 1
    for _ in range(acquired):
        orchestrator.reviewer_semaphore.release()


@pytest.mark.asyncio
async def test_preparation_reaches_anatomy_without_model_runtime(
    preparation_repo: Path, prepared_head: tuple[str, str]
) -> None:
    base, head = prepared_head
    store = MemoryStore()
    report = await make_service(store).run(
        ReviewInput(repo_path=str(preparation_repo), base_ref=base, head_ref=head),
        through="anatomy",
    )

    assert isinstance(report, PreparationReport)
    assert report.pipeline_complete is False
    assert report.completed_stage == "anatomy"
    assert report.review_paths == ["a.py", "new.py", "normal.py"]
    assert report.context_paths == ["old.py"]
    assert report.excluded_count == 1
    assert report.related_paths == ["caller.py"]
    assert ".env" not in report.model_dump_json()
    assert [record_type for _run, record_type, _payload in store.events][0] == "run_started"


@pytest.mark.asyncio
async def test_preparation_uses_fixed_head_not_working_tree(
    preparation_repo: Path, prepared_head: tuple[str, str]
) -> None:
    base, head = prepared_head
    target = preparation_repo / "a.py"
    original = target.read_bytes()
    target.write_text("uncommitted mutation\n", encoding="utf-8")
    try:
        store = MemoryStore()
        report = await make_service(store).run(
            ReviewInput(repo_path=str(preparation_repo), base_ref=base, head_ref=head)
        )
        assert report.stats.total_additions == 3
        assert "uncommitted mutation" not in report.model_dump_json()
    finally:
        target.write_bytes(original)
    assert target.read_bytes() == original


@pytest.mark.asyncio
async def test_intake_failure_returns_input_error_and_no_report(
    preparation_repo: Path, prepared_head: tuple[str, str]
) -> None:
    _base, head = prepared_head
    store = MemoryStore()
    with pytest.raises(ValueError, match="ref not found"):
        await make_service(store).run(
            ReviewInput(repo_path=str(preparation_repo), base_ref="missing-ref", head_ref=head)
        )
    assert store.events[-1][1] == "run_failed"


@pytest.mark.asyncio
async def test_unsupported_through_stage_rejected_before_run(
    preparation_repo: Path, prepared_head: tuple[str, str]
) -> None:
    base, head = prepared_head
    store = MemoryStore()
    with pytest.raises(ValueError, match="unsupported --through stage"):
        await make_service(store).run(
            ReviewInput(repo_path=str(preparation_repo), base_ref=base, head_ref=head),
            through="reviewer",
        )
    assert store.events == []


@pytest.mark.asyncio
async def test_secret_is_excluded_and_deleted_file_is_context(
    preparation_repo: Path, prepared_head: tuple[str, str]
) -> None:
    base, head = prepared_head
    report = await make_service(MemoryStore()).run(
        ReviewInput(repo_path=str(preparation_repo), base_ref=base, head_ref=head)
    )
    assert "old.py" in report.context_paths
    assert ".env" not in set(report.review_paths) | set(report.context_paths) | set(report.related_paths)
    assert report.excluded_count == 1


@pytest.mark.asyncio
async def test_intake_filter_runs_once(
    preparation_repo: Path,
    prepared_head: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, head = prepared_head
    calls = 0
    original = orchestrator_module.build_review_snapshot

    def counted_snapshot(review_input: object, config: object) -> object:
        nonlocal calls
        calls += 1
        return original(review_input, config)

    monkeypatch.setattr(orchestrator_module, "build_review_snapshot", counted_snapshot)
    await make_service(MemoryStore()).run(
        ReviewInput(repo_path=str(preparation_repo), base_ref=base, head_ref=head)
    )
    assert calls == 1


@pytest.mark.asyncio
async def test_known_blast_radius_error_degrades_to_empty_related_paths(
    preparation_repo: Path,
    prepared_head: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, head = prepared_head

    async def known_failure(*args: object, **kwargs: object) -> list[str]:
        raise orchestrator_module.BlastRadiusError("injected known failure")

    monkeypatch.setattr(orchestrator_module, "compute_blast_radius", known_failure)
    report = await make_service(MemoryStore()).run(
        ReviewInput(repo_path=str(preparation_repo), base_ref=base, head_ref=head)
    )
    assert report.related_paths == []
    assert report.diagnostics == ["blast_radius_degraded: injected known failure"]


@pytest.mark.asyncio
async def test_storage_failure_after_first_event_fails_without_preparation(
    preparation_repo: Path, prepared_head: tuple[str, str]
) -> None:
    base, head = prepared_head
    store = MemoryStore(fail_after=1)
    with pytest.raises(DeepReviewStoreError, match="injected storage failure"):
        await make_service(store).run(
            ReviewInput(repo_path=str(preparation_repo), base_ref=base, head_ref=head)
        )
    assert len(store.events) == 1
    assert store.events[0][1] == "run_started"


@pytest.mark.asyncio
async def test_same_input_creates_new_runs_with_stable_business_fields(
    preparation_repo: Path, prepared_head: tuple[str, str]
) -> None:
    base, head = prepared_head
    input_model = ReviewInput(repo_path=str(preparation_repo), base_ref=base, head_ref=head)
    first = await make_service(MemoryStore()).run(input_model)
    second = await make_service(MemoryStore()).run(input_model)

    assert first.run_id != second.run_id
    assert first.model_dump(exclude={"run_id"}) == second.model_dump(exclude={"run_id"})


@pytest.mark.asyncio
async def test_empty_diff_is_valid_preparation(preparation_repo: Path) -> None:
    head = git(preparation_repo, "rev-parse", "HEAD")
    report = await make_service(MemoryStore()).run(
        ReviewInput(repo_path=str(preparation_repo), base_ref=head, head_ref=head)
    )
    assert report.review_paths == []
    assert report.context_paths == []
    assert report.completed_stage == "anatomy"
    assert report.pipeline_complete is False
