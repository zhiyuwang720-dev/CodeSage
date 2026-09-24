from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.domains.deep_review.agents.cross_analysis import (
    CrossAgentOutcome, CrossAnalysisResultDraft, run_cross_agent,
)
from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ReviewInput
from app.domains.deep_review.schemas.output import DeepReviewResult, ReviewMetrics, ReviewerDimensionReport
from app.domains.deep_review.schemas.pipeline import (
    Anatomy, CrossAnalysisResult, EvidencePackage, FindingDecision, ReviewDimension,
    ReviewFinding, ReviewInvestigation, ReviewPlan, ReviewerResult, SemanticBrief,
)
from app.domains.deep_review.services import orchestrator as orchestrator_module
from app.domains.deep_review.services.cross_repair import repair_cross_result
from app.domains.deep_review.services.evidence import _fit_evidence_budget
from app.domains.deep_review.services.input_builder import build_review_snapshot
from app.domains.deep_review.services.merge_gate import merge_findings, stable_business_hash
from app.domains.deep_review.services.orchestrator import PreparationOrchestrator, DeepReviewRunContext
from app.domains.deep_review.services.output_formatter import format_cross_inputs
from app.domains.deep_review.services.prompt_loader import load_prompt, render_prompt
from app.domains.deep_review.services.runtime import AgentCallResult
from app.domains.deep_review.services.service import DeepReviewService


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, encoding="utf-8",
    ).stdout.strip()


@pytest.fixture
def snapshot(tmp_path: Path):
    repo = tmp_path / "cross-repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Cross Test")
    _git(repo, "config", "user.email", "cross@example.test")
    (repo / "a.py").write_text("value = 1\nother = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("value = 1\nother = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("value = 2\nother = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("value = 2\nother = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")
    return build_review_snapshot(
        ReviewInput(repo_path=str(repo), base_ref=base, head_ref=head), DeepReviewConfig(),
    )


def finding(path: str = "a.py", title: str = "Changed behavior") -> ReviewFinding:
    return ReviewFinding(
        file_path=path, line_start=1, line_end=1, severity="high", title=title,
        body="The changed value causes an observable failure for this caller.",
        evidence=f"{path}:1 changed value", confidence=0.75,
        dimension_name="shared-target", source="reviewer",
    )


def plan(*, with_hint: bool = False) -> ReviewPlan:
    from app.domains.deep_review.schemas.pipeline import CrossReferenceHint

    return ReviewPlan(
        dimensions=[ReviewDimension(
            name="shared-target", target_files=["a.py", "b.py"],
            investigations=[ReviewInvestigation(
                source_dimension="shared-target", priority=1,
                review_prompt="Check both files against the changed contract.",
            )],
        )],
        cross_reference_hints=[CrossReferenceHint(
            dimension_names=["first", "second"], relation="A produces data consumed by B",
            symbol_or_contract="value",
        )] if with_hint else [],
        dimension_name_map={"old-name": ["shared-target"]},
        coverage_complete=True,
    )


def test_cross_draft_schema_rejects_invalid_enum_without_decision_cap() -> None:
    decisions = [{"finding_index": index, "result": "keep", "reason": "checked"} for index in range(257)]
    assert len(CrossAnalysisResultDraft.model_validate({"decisions": decisions}).decisions) == 257
    with pytest.raises(ValidationError):
        CrossAnalysisResultDraft.model_validate({
            "decisions": [{"finding_index": 0, "result": "keep", "reason": "x", "revised_severity": "blocker"}]
        })


@pytest.mark.parametrize(
    ("candidate_count", "expected"), [(0, 12), (10, 12), (11, 24), (30, 24), (31, 36)],
)
def test_cross_turn_cap_follows_candidate_count(candidate_count: int, expected: int) -> None:
    assert DeepReviewConfig.cross_turn_limit(candidate_count) == expected


def test_cross_repair_first_valid_decision_and_missing_keep(snapshot) -> None:
    candidates = [finding(title=f"Problem {i}") for i in range(3)]
    result = repair_cross_result(
        {"decisions": [
            {"finding_index": 0, "result": "keep", "reason": "verified"},
            {"finding_index": 0, "result": "drop", "reason": "duplicate"},
            {"finding_index": 99, "result": "drop", "reason": "wrong index"},
        ]},
        candidates=candidates, snapshot=snapshot, head_line_counts={},
    )
    assert [(item.finding_index, item.result) for item in result.result.decisions] == [
        (0, "keep"), (1, "keep"), (2, "keep"),
    ]
    assert "cross_decision_duplicate:1:0" in result.diagnostics
    assert "cross_decision_missing:1" in result.diagnostics
    assert result.partial


def test_cross_repair_empty_drop_and_new_finding_bounds(snapshot) -> None:
    raw_new = {
        "file_path": "b.py", "line_start": 1, "line_end": 1,
        "severity": "medium", "title": "Cross file contract failure",
        "body": "Producer A changes value and consumer B uses the old invariant.",
        "evidence": "a.py:1 and b.py:1", "confidence": 0.8,
    }
    repaired = repair_cross_result(
        {"decisions": [{"finding_index": 0, "result": "drop", "reason": "  "}],
         "new_findings": [raw_new, {**raw_new, "file_path": "other.py"},
                          {**raw_new, "line_start": 99, "line_end": 99}]},
        candidates=[finding()], snapshot=snapshot,
        head_line_counts={"b.py": 2},
    )
    assert repaired.result.decisions[0].result == "keep"
    assert len(repaired.result.new_findings) == 1
    assert repaired.result.new_findings[0].source == "cross"
    assert repaired.result.new_findings[0].dimension_name == "cross-analysis"
    assert len(repaired.diagnostics) == 3


def test_cross_input_includes_all_indices_and_repaired_relation_semantics(snapshot) -> None:
    candidates = [finding(title=f"Problem {i}") for i in range(257)]
    evidence = {1: EvidencePackage(finding_index=1, truncated=True)}
    values = format_cross_inputs(
        snapshot=snapshot, anatomy=Anatomy(), semantic=SemanticBrief(),
        plan=plan(with_hint=True), reviewers=[ReviewerDimensionReport(
            dimension_name="shared-target", dimension_order=0, status="failed",
            target_files=["a.py", "b.py"],
        )], candidates=candidates, evidence=evidence, max_turns=36,
    )
    assert len(json.loads(values["candidate_summaries_json"])) == 257
    packages = json.loads(values["evidence_packages_json"])
    assert [item["index"] for item in packages] == list(range(257))
    assert packages[1]["evidence_empty"] and packages[1]["truncated"]
    relation = json.loads(values["plan_relations_json"])
    assert relation["dimension_name_map"] == {"old-name": ["shared-target"]}
    coverage = json.loads(values["reviewer_coverage_json"])
    assert coverage["paths_without_successful_reviewer"] == ["a.py", "b.py"]
    prompt = render_prompt("cross_analysis_user", **values)
    system = load_prompt("cross_analysis")
    for word in ("benign explanation", "compound", "duplicate", "FinalizeReview"):
        assert word in system or word in prompt
    assert "Reviewer-authored evidence" in prompt
    assert "Read/Grep/Glob/PowerShell" not in system + prompt
    assert "transcript" not in values["candidate_summaries_json"].lower()


@pytest.mark.asyncio
async def test_cross_agent_one_harness_and_capacity_fallback(snapshot) -> None:
    class Runtime:
        calls = 0

        async def harness(self, prompt: str, **kwargs):
            self.calls += 1
            assert kwargs["max_turns"] == 12
            assert kwargs["schema"] is CrossAnalysisResultDraft
            assert kwargs["tool_allowlist"] == {"file_read", "file_read_diff", "file_find", "code_search"}
            return SimpleNamespace(
                parsed=kwargs["schema"].model_validate({"decisions": [
                    {"finding_index": 0, "result": "drop", "reason": "Guard at a.py:1 prevents this state."}
                ]}),
                session_id="cross-session", usage={"total_tokens": 11}, cost_usd=None,
            )

    class Factory:
        runtime = Runtime()

        def for_role(self, role):
            assert role == "deep_review:cross_analysis"
            return self.runtime

    factory = Factory()
    kwargs = dict(
        snapshot=snapshot, anatomy=Anatomy(), semantic=SemanticBrief(),
        plan=plan(), reviewers=[], candidates=[finding()], evidence={},
    )
    completed = await run_cross_agent(factory, config=DeepReviewConfig(), **kwargs)
    assert completed.call.value.decisions[0].result == "drop"
    assert completed.call.session_id == "cross-session"
    assert factory.runtime.calls == 1
    over_capacity = await run_cross_agent(
        factory, config=DeepReviewConfig(max_cross_context_bytes=100), **kwargs,
    )
    assert over_capacity.call.value is None
    assert "cross_context_capacity_exceeded" in over_capacity.call.error
    assert factory.runtime.calls == 1


def test_scoring_merge_exact_dedup_and_hash_ignore_run_observation(snapshot) -> None:
    first = finding(title="same issue")
    second = first.model_copy(update={"severity": "medium", "confidence": 0.9})
    third = finding(title="a different claim on the same line")
    candidates = [first, second, third]
    cross = CrossAnalysisResult(decisions=[
        FindingDecision(finding_index=0, result="keep", reason="present"),
        FindingDecision(finding_index=1, result="keep", reason="present"),
        FindingDecision(finding_index=2, result="keep", reason="independent"),
    ])
    evidence = {0: EvidencePackage(finding_index=0, primary_code="1|value = 2", diff_hunk="+value = 2")}
    merged = merge_findings(candidates, cross, evidence, snapshot, DeepReviewConfig())
    assert len(merged.findings) == 2
    assert merged.exact_duplicates == 1
    assert next(item for item in merged.findings if item.title == "same issue").severity == "high"
    assert second.severity == "medium"  # original candidate not mutated
    scored_by_title = dict(zip((item.title for item in merged.findings), merged.scores))
    assert scored_by_title["same issue"].score == 0.6
    assert scored_by_title["same issue"].evidence_completeness == 2
    assert stable_business_hash(merged.findings, "done", [], "completed") == stable_business_hash(
        merged.findings, "done", [], "completed"
    )


def test_evidence_utf8_budget_marks_truncation() -> None:
    package = EvidencePackage(
        finding_index=0, primary_code="证据" * 1000,
        diff_hunk="+证据" * 1000,
    )
    bounded = _fit_evidence_budget(package, finding_index=0, max_bytes=1000)
    assert len(bounded.model_dump_json().encode("utf-8")) <= 1000
    assert bounded.truncated


@pytest.mark.asyncio
async def test_cross_skip_zero_candidates_without_hint(snapshot) -> None:
    class Store:
        def __init__(self):
            self.events = []

        def append(self, run_id, kind, payload):
            self.events.append((kind, payload))

    store = Store()
    orchestrator = PreparationOrchestrator(
        config=DeepReviewConfig(), store=store, runtime_factory=None,
    )
    context = DeepReviewRunContext(run_id="r", snapshot=snapshot, anatomy=Anatomy())
    result, status, diagnostics, observation = await orchestrator._run_cross_stage(
        "r", context, SemanticBrief(), plan(), [], [], {},
    )
    assert status == "skipped" and observation is None and not diagnostics
    assert result.decisions == []
    assert any(kind == "cross_skipped" for kind, _ in store.events)


@pytest.mark.asyncio
async def test_cross_failure_keeps_every_candidate_and_marks_partial(snapshot) -> None:
    class Store:
        def __init__(self):
            self.events = []

        def append(self, run_id, kind, payload):
            self.events.append((kind, payload))

    class FailingFactory:
        def for_role(self, role):
            class FailingRuntime:
                async def harness(self, prompt, **kwargs):
                    raise RuntimeError("provider unavailable")

            return FailingRuntime()

    store = Store()
    orchestrator = PreparationOrchestrator(
        config=DeepReviewConfig(), store=store, runtime_factory=FailingFactory(),
    )
    context = DeepReviewRunContext(run_id="r", snapshot=snapshot, anatomy=Anatomy())
    candidates = [finding(title="First issue"), finding(title="Second issue")]
    result, status, diagnostics, observation = await orchestrator._run_cross_stage(
        "r", context, SemanticBrief(), plan(), [], candidates, {},
    )
    assert status == "partial"
    assert [item.result for item in result.decisions] == ["keep", "keep"]
    assert result.new_findings == []
    assert diagnostics and observation.error
    assert any(kind == "cross_failed" for kind, _ in store.events)


@pytest.mark.asyncio
async def test_full_fake_final_run_applies_drop_and_new_finding(snapshot, monkeypatch) -> None:
    class Store:
        def __init__(self):
            self.events = []

        def append(self, run_id, kind, payload):
            self.events.append((run_id, kind, payload))

    class Factory:
        def for_role(self, role):
            return object()

    async def semantic(*args, **kwargs):
        return AgentCallResult(value=SemanticBrief(narrative="changed values"), usage={"total_tokens": 1})

    async def planner(*args, **kwargs):
        return AgentCallResult(value=plan(), usage={"total_tokens": 2})

    async def reviewer(*args, **kwargs):
        return orchestrator_module.ReviewerAgentOutcome(call=AgentCallResult(
            value=ReviewerResult(findings=[finding("a.py", "Keep this"), finding("b.py", "Drop this")]),
            usage={"total_tokens": 3},
        ))

    async def cross(*args, **kwargs):
        assert len(kwargs["candidates"]) == 2
        assert set(kwargs["evidence"]) == {0, 1}
        return CrossAgentOutcome(call=AgentCallResult(
            value=CrossAnalysisResult(
                decisions=[FindingDecision(finding_index=0, result="keep", reason="verified"),
                           FindingDecision(finding_index=1, result="drop", reason="upstream guard")],
                new_findings=[finding("b.py", "New cross contract failure").model_copy(
                    update={"source": "cross", "dimension_name": "cross-analysis"}
                )],
                summary="One kept and one new issue.",
            ),
            usage={"total_tokens": 4},
        ))

    monkeypatch.setattr(orchestrator_module, "run_semantic_agent", semantic)
    monkeypatch.setattr(orchestrator_module, "run_planner_agent", planner)
    monkeypatch.setattr(orchestrator_module, "run_reviewer_agent", reviewer)
    monkeypatch.setattr(orchestrator_module, "run_cross_agent", cross)
    store = Store()
    service = DeepReviewService(config=DeepReviewConfig(), store=store, runtime_factory=Factory())
    first = await service.run(snapshot.input, through="final")
    second = await service.run(snapshot.input, through="final")
    assert isinstance(first, DeepReviewResult)
    assert first.status == "completed" and first.cross_status == "completed"
    assert first.candidate_count == 2 and len(first.findings) == 2
    assert {item.title for item in first.findings} == {"Keep this", "New cross contract failure"}
    assert first.metrics.model_calls == 4 and first.metrics.total_tokens == 10
    assert first.content_hash == second.content_hash
    events = [payload for _, kind, payload in store.events if kind == "final_result"]
    assert events[0]["dropped_by_cross"] == 1
    assert events[0]["candidate_count"] == 2 and events[0]["finding_count"] == 2


@pytest.mark.asyncio
async def test_final_cli_writes_result_and_summary(tmp_path: Path, monkeypatch) -> None:
    from app.domains.deep_review import __main__ as entry

    class Engine:
        def dispose(self):
            pass

    class Factory:
        pass

    monkeypatch.setattr(entry, "_create_temporary_sqlite_runtime", lambda *args, **kwargs: (Factory(), Engine()))

    async def fake_run(self, review_input, *, through):
        assert through == "final"
        return DeepReviewResult(
            run_id="fake-run", status="completed", findings=[finding()],
            summary="verified", metrics=ReviewMetrics(model_calls=4, total_tokens=10),
            content_hash="a" * 64, candidate_count=2, cross_status="completed",
        )

    monkeypatch.setattr(entry.DeepReviewService, "run", fake_run)
    report_path = tmp_path / "report.json"
    args = entry.build_parser().parse_args([
        "--repo", str(tmp_path), "--base", "base000", "--head", "head000",
        "--through", "final", "--output", str(report_path),
        "--store-dir", str(tmp_path / "events"),
    ])
    assert await entry._run(args) == 0
    assert json.loads(report_path.read_text(encoding="utf-8"))["content_hash"] == "a" * 64
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["completed_stage"] == "final"
    assert summary["pipeline_complete"] is True
    assert summary["candidate_count"] == 2 and summary["finding_count"] == 1
