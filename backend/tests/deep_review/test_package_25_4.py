from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import types
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.domains.deep_review.agents.planner import (
    ReviewPlanDraft, build_planner_prompt, run_planner_agent,
)
from app.domains.deep_review.agents.semantic import (
    SemanticBriefDraft, build_semantic_prompt, run_semantic_agent,
)
from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ReviewInput
from app.domains.deep_review.services.diff_engine import build_anatomy
from app.domains.deep_review.services.directory_filter import FilterResult
from app.domains.deep_review.services.input_builder import build_review_snapshot
from app.domains.deep_review.services.plan_repair import repair_plan
from app.domains.deep_review.services.prompt_loader import load_prompt, render_prompt
from app.domains.deep_review.services import plan_repair as plan_repair_module
from app.domains.deep_review.services.runtime import RuntimeBridgeDeepReviewRuntimeFactory
from app.domains.deep_review.services.service import DeepReviewService
from app.execution_plane.models.runtime_ai import HarnessIncompleteError


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        encoding="utf-8",
    ).stdout.strip()


@pytest.fixture
def review_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "user.email", "test@example.com")
    for name in ("a.py", "b.py", "c.py", "unchanged.py"):
        (repo / name).write_text("VALUE = 1\n", encoding="utf-8")
    (repo / ".env").write_text("secret=hidden\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    for name in ("a.py", "b.py", "c.py"):
        (repo / name).write_text("VALUE = 2\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "head")
    return repo, base, git(repo, "rev-parse", "HEAD")


def snapshot_for(repo_data: tuple[Path, str, str]):
    repo, base, head = repo_data
    return build_review_snapshot(
        ReviewInput(repo_path=str(repo), base_ref=base, head_ref=head, title="Update values"),
        DeepReviewConfig(),
    )


@dataclass
class MemoryStore:
    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def append(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((run_id, kind, dict(payload)))


def semantic_payload() -> dict[str, Any]:
    return {"narrative": "This PR updates three module values and their callers."}


def dimension(name: str, paths: list[str], contexts: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "review_prompt": "Trace each changed value, compare callers, and report incorrect behavior.",
        "target_files": paths,
        "context_files": contexts or [],
        "priority": 5,
    }


class FakeRuntime:
    def __init__(
        self, *, ai_payload=None, plan_payload=None, review_payload=None,
        error: Exception | None = None,
    ):
        self.ai_payload = ai_payload if ai_payload is not None else semantic_payload()
        self.plan_payload = plan_payload if plan_payload is not None else {
            "dimensions": [dimension("values", ["a.py", "b.py", "c.py"], ["unchanged.py"])]
        }
        self.review_payload = review_payload if review_payload is not None else {
            "findings": [], "summary": "No actionable issue found.",
        }
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def ai(self, prompt: str, **kwargs: Any):
        self.calls.append(("ai", kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(
            parsed=kwargs["schema"].model_validate(self.ai_payload),
            usage={"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
            cost_usd=0.01,
        )

    async def harness(self, prompt: str, **kwargs: Any):
        self.calls.append(("harness", kwargs))
        if self.error:
            raise self.error
        payload = (
            self.plan_payload
            if kwargs["schema"].__name__ == "ReviewPlanDraft"
            else self.review_payload
        )
        return SimpleNamespace(
            parsed=kwargs["schema"].model_validate(payload),
            session_id="session-planner",
            usage={"total_tokens": 20, "usage_complete": False},
            cost_usd=None,
        )


class FakeFactory:
    def __init__(self, semantic: FakeRuntime, planner: FakeRuntime):
        self.semantic = semantic
        self.planner = planner
        self.roles: list[str] = []

    def for_role(self, role: str) -> FakeRuntime:
        self.roles.append(role)
        return self.semantic if role.endswith(":semantic") else self.planner


def test_drafts_are_constrained_and_described() -> None:
    for draft in (SemanticBriefDraft, ReviewPlanDraft):
        schema = draft.model_json_schema()
        for part in [schema, *schema.get("$defs", {}).values()]:
            assert part["additionalProperties"] is False
            for field_schema in part["properties"].values():
                assert field_schema.get("description")
    with pytest.raises(ValidationError):
        SemanticBriefDraft.model_validate({**semantic_payload(), "risk_surfaces": ["x" * 201]})
    with pytest.raises(ValidationError):
        SemanticBriefDraft.model_validate({**semantic_payload(), "confidence": 1.1})
    with pytest.raises(ValidationError):
        ReviewPlanDraft.model_validate({"dimensions": [dimension("bad name", ["a.py"])]})
    accepted_long_but_valid = ReviewPlanDraft.model_validate({
        "summary": "s" * 1_011,
        "cross_reference_hints": [{
            "dimension_names": ["producer", "consumer"],
            "relation": "r" * 345,
            "symbol_or_contract": "Batch.Positions",
        }],
    })
    assert len(accepted_long_but_valid.summary) == 1_011
    assert len(accepted_long_but_valid.cross_reference_hints[0].relation) == 345
    assert "source" not in ReviewPlanDraft.model_fields
    assert "fallback" not in ReviewPlanDraft.model_json_schema().get("$defs", {})["ReviewDimensionDraft"]["properties"]


def test_planner_prompt_defines_investigation_not_finding_quota() -> None:
    prompt = load_prompt("planner")
    for phrase in (
        "assigns review work", "coverage boundaries", "not a list of predicted defects",
        "Semantic Brief is a set of leads", "max_planner_dimensions",
        "complete ownership takes priority", "failure consequence",
        "FinalizeReview", "ReviewPlanDraft",
        "summary` within 2000 characters", "relation` within",
    ):
        assert phrase in prompt
    assert "check code quality" not in prompt


def test_semantic_prompt_matches_draft_and_marks_truncated_evidence(
    review_repo: tuple[Path, str, str],
) -> None:
    prompt = load_prompt("semantic")
    for field_name in SemanticBriefDraft.model_fields:
        assert f"`{field_name}`" in prompt
    assert "no tools" in prompt.lower()
    assert "not a bug claim" in prompt.lower()
    assert "1–200 characters" in prompt

    snapshot = snapshot_for(review_repo)
    anatomy = build_anatomy(snapshot.changes)
    anatomy.files[0].diff = "x" * 2_000
    user_prompt = build_semantic_prompt(snapshot, anatomy, DeepReviewConfig())
    data = json.loads(user_prompt.split("<untrusted_pr_context>\n", 1)[1].split(
        "\n</untrusted_pr_context>", 1,
    )[0])
    assert data["pr_metadata"]["title"] == "Update values"
    assert data["evidence_scope"]["patch_truncated_files"] == [anatomy.files[0].path]
    assert data["diff_summary"]["files"][0]["patch_truncated"] is True
    assert "You cannot read more files" in user_prompt


def test_planner_input_separates_constraints_from_untrusted_evidence(
    review_repo: tuple[Path, str, str],
) -> None:
    snapshot = snapshot_for(review_repo)
    anatomy = build_anatomy(snapshot.changes)
    semantic = SemanticBriefDraft.model_validate(semantic_payload())
    from app.domains.deep_review.schemas.pipeline import SemanticBrief

    prompt = build_planner_prompt(
        snapshot=snapshot, anatomy=anatomy,
        semantic=SemanticBrief(**semantic.model_dump(), source="fallback"),
        config=DeepReviewConfig(max_planner_dimensions=3, max_files_per_work_item=2),
    )
    constraints = json.loads(prompt.split("<run_constraints>\n", 1)[1].split(
        "\n</run_constraints>", 1,
    )[0])
    evidence = json.loads(prompt.split("<untrusted_review_evidence>\n", 1)[1].split(
        "\n</untrusted_review_evidence>", 1,
    )[0])
    assert constraints["review_paths"] == snapshot.review_paths
    assert constraints["head_commit"] == snapshot.head_commit
    assert constraints["max_planner_dimensions"] == 3
    assert constraints["max_turns_planner"] == DeepReviewConfig().max_turns_planner
    assert constraints["max_files_per_work_item"] == 2
    assert evidence["semantic_brief"]["source"] == "fallback"
    assert evidence["pr_metadata"]["title"] == "Update values"
    assert "not code facts" in prompt


def test_prompt_renderer_rejects_missing_or_extra_values() -> None:
    with pytest.raises(ValueError, match="placeholders"):
        render_prompt("semantic_user")
    with pytest.raises(ValueError, match="placeholders"):
        render_prompt("semantic_user", context_json="{}", unused="x")
    assert "{{context_json}}" not in render_prompt("semantic_user", context_json="{}")


def test_semantic_patch_budget_reaches_later_files(review_repo: tuple[Path, str, str]) -> None:
    snapshot = snapshot_for(review_repo)
    anatomy = build_anatomy(snapshot.changes)
    anatomy.files = [
        anatomy.files[0].model_copy(update={"path": f"file_{index:02d}.py", "diff": "x" * 2_000})
        for index in range(20)
    ]
    prompt = build_semantic_prompt(snapshot, anatomy, DeepReviewConfig())
    data = json.loads(prompt.split("<untrusted_pr_context>\n", 1)[1].split(
        "\n</untrusted_pr_context>", 1,
    )[0])
    patches = [item["patch"] for item in data["diff_summary"]["files"]]
    assert all(patches)
    assert sum(len(item.encode("utf-8")) for item in patches) <= 18_000
    assert max(map(len, patches)) - min(map(len, patches)) <= 600


@pytest.mark.asyncio
async def test_planning_run_observations_and_fixed_head_context(
    review_repo: tuple[Path, str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    repo, base, head = review_repo
    semantic = FakeRuntime()
    planner = FakeRuntime()
    factory = FakeFactory(semantic, planner)
    store = MemoryStore()
    with caplog.at_level(logging.INFO):
        report = await DeepReviewService(
            config=DeepReviewConfig(), store=store, runtime_factory=factory
        ).run(
            ReviewInput(repo_path=str(repo), base_ref=base, head_ref=head, title="Update values"),
            through="planning",
        )
    assert report.completed_stage == "planning"
    assert report.pipeline_complete is False
    assert report.semantic.source == "model"
    assert report.plan.coverage_complete is True
    assert report.plan.dimensions[0].context_files == ["unchanged.py"]
    assert factory.roles == ["deep_review:semantic", "deep_review:planner"]
    assert [item.stage for item in report.agent_observations] == ["semantic", "planning"]
    assert report.agent_observations[0].session_id is None
    assert report.agent_observations[0].usage["total_tokens"] == 15
    assert report.agent_observations[0].cost_usd == 0.01
    assert report.agent_observations[1].session_id == "session-planner"
    assert report.agent_observations[1].usage["total_tokens"] == 20
    assert report.agent_observations[1].cost_usd is None
    assert planner.calls[0][1]["schema"] is ReviewPlanDraft
    assert {tool.name for tool in planner.calls[0][1]["tools"]} == {
        "file_read", "file_read_diff", "file_find", "code_search",
    }
    assert [event for _, event, _ in store.events if event.endswith("_completed")][-1] == "preparation_completed"
    assert any("deep_review.agent_completed" in item.message for item in caplog.records)


@pytest.mark.asyncio
async def test_semantic_invalid_response_falls_back_without_losing_usage(
    review_repo: tuple[Path, str, str],
) -> None:
    snapshot = snapshot_for(review_repo)
    runtime = FakeRuntime(ai_payload={**semantic_payload(), "confidence": 2.0})
    # Fake RuntimeBridge returns schema failures; a local validation failure must also be handled.
    runtime.ai_payload = {**semantic_payload(), "confidence": 2.0}
    result = await run_semantic_agent(
        runtime, snapshot=snapshot, anatomy=build_anatomy(snapshot.changes),
        config=DeepReviewConfig(),
    )
    assert result.value.source == "fallback"
    assert result.error == "ValidationError"
    assert result.usage is None
    assert result.session_id is None


@pytest.mark.asyncio
async def test_semantic_mapping_failure_preserves_received_usage(
    review_repo: tuple[Path, str, str],
) -> None:
    snapshot = snapshot_for(review_repo)

    class InvalidParsedRuntime(FakeRuntime):
        async def ai(self, prompt: str, **kwargs: Any):
            return SimpleNamespace(
                parsed={**semantic_payload(), "confidence": 2.0},
                usage={"total_tokens": 7}, cost_usd=0.03,
            )

    result = await run_semantic_agent(
        InvalidParsedRuntime(), snapshot=snapshot, anatomy=build_anatomy(snapshot.changes),
        config=DeepReviewConfig(),
    )
    assert result.value.source == "fallback"
    assert result.usage == {"total_tokens": 7}
    assert result.cost_usd == 0.03


@pytest.mark.asyncio
async def test_semantic_bridge_schema_failure_preserves_metering_without_raw_response(
    review_repo: tuple[Path, str, str],
) -> None:
    snapshot = snapshot_for(review_repo)

    class FailedSchemaRuntime(FakeRuntime):
        async def ai(self, prompt: str, **kwargs: Any):
            class SchemaError(ValueError):
                usage = {"total_tokens": 19}
                cost_usd = 0.04
                raw_text = "SECRET_PROVIDER_RESPONSE"

            raise SchemaError("schema invalid")

    result = await run_semantic_agent(
        FailedSchemaRuntime(), snapshot=snapshot, anatomy=build_anatomy(snapshot.changes),
        config=DeepReviewConfig(),
    )
    assert result.value.source == "fallback"
    assert result.error == "SchemaError"
    assert result.usage == {"total_tokens": 19}
    assert result.cost_usd == 0.04
    assert "SECRET_PROVIDER_RESPONSE" not in str(result)


@pytest.mark.asyncio
async def test_planner_failure_preserves_observation_without_provider_body(
    review_repo: tuple[Path, str, str],
) -> None:
    repo, base, head = review_repo
    secret = "SUPER_SECRET_PROVIDER_BODY"
    store = MemoryStore()
    factory = FakeFactory(FakeRuntime(), FakeRuntime(error=RuntimeError(secret)))
    with pytest.raises(RuntimeError, match="planning failed"):
        await DeepReviewService(
            config=DeepReviewConfig(), store=store, runtime_factory=factory,
        ).run(ReviewInput(repo_path=str(repo), base_ref=base, head_ref=head), through="planning")
    failure = [payload for _, kind, payload in store.events if kind == "plan_failed"]
    assert len(failure) == 1
    assert failure[0]["session_id"] is None
    assert failure[0]["usage"] is None
    assert failure[0]["error"] == "RuntimeError"
    assert secret not in str(store.events)
    assert any(kind == "run_failed" for _, kind, _ in store.events)


@pytest.mark.asyncio
async def test_planner_incomplete_harness_preserves_session_and_usage(
    review_repo: tuple[Path, str, str],
) -> None:
    repo, base, head = review_repo
    store = MemoryStore()
    incomplete = HarnessIncompleteError(
        "terminal action missing", session_id="session-incomplete",
        usage={"total_tokens": 123}, cost_usd=None,
    )
    factory = FakeFactory(FakeRuntime(), FakeRuntime(error=incomplete))
    with pytest.raises(RuntimeError, match="planning failed"):
        await DeepReviewService(
            config=DeepReviewConfig(), store=store, runtime_factory=factory,
        ).run(ReviewInput(repo_path=str(repo), base_ref=base, head_ref=head), through="planning")
    failure = [payload for _, kind, payload in store.events if kind == "plan_failed"]
    assert failure[0]["session_id"] == "session-incomplete"
    assert failure[0]["usage"] == {"total_tokens": 123}
    assert failure[0]["cost_usd"] is None


@pytest.mark.asyncio
async def test_cancelled_semantic_does_not_fallback(review_repo: tuple[Path, str, str]) -> None:
    repo, base, head = review_repo
    store = MemoryStore()
    factory = FakeFactory(FakeRuntime(error=asyncio.CancelledError()), FakeRuntime())
    with pytest.raises(asyncio.CancelledError):
        await DeepReviewService(
            config=DeepReviewConfig(), store=store, runtime_factory=factory,
        ).run(ReviewInput(repo_path=str(repo), base_ref=base, head_ref=head), through="planning")
    assert any(kind == "run_cancelled" for _, kind, _ in store.events)
    assert not any(kind == "semantic_fallback" for _, kind, _ in store.events)


def test_repair_first_owner_fallback_and_ambiguous_hints(
    review_repo: tuple[Path, str, str],
) -> None:
    snapshot = snapshot_for(review_repo)
    draft = {
        "dimensions": [
            dimension("owners", ["a.py", "b.py"]),
            dimension("owners", ["b.py", "../hidden.py", "c.py"]),
        ],
        "cross_reference_hints": [{
            "dimension_names": ["owners", "owners"],
            "relation": "Compare caller and callee behavior",
            "symbol_or_contract": "VALUE",
        }],
    }
    plan = repair_plan(draft, snapshot, DeepReviewConfig())
    assert plan.coverage_complete is True
    assert [path for group in plan.dimensions for path in group.target_files].count("b.py") == 1
    assert len({group.name for group in plan.dimensions}) == len(plan.dimensions)
    assert plan.cross_reference_hints == []
    assert any("removed_invalid_target" in item for item in plan.repair_actions)


def test_repair_merges_earlier_pair_then_defers_without_losing_owner(
    review_repo: tuple[Path, str, str],
) -> None:
    snapshot = snapshot_for(review_repo)
    draft = {"dimensions": [
        dimension("a", ["a.py"]), dimension("b", ["b.py"]), dimension("c", ["c.py"]),
    ]}
    merged = repair_plan(
        draft, snapshot, DeepReviewConfig(max_final_dimensions=2, max_files_per_work_item=2),
    )
    assert len([item for item in merged.dimensions if not item.deferred]) == 2
    assert merged.coverage_complete is True
    assert any(item.source == "merged" for item in merged.dimensions)
    deferred = repair_plan(
        draft, snapshot, DeepReviewConfig(max_final_dimensions=2, max_files_per_work_item=1),
    )
    assert len([item for item in deferred.dimensions if not item.deferred]) == 2
    assert deferred.coverage_complete is False
    assert len([item for item in deferred.dimensions if item.deferred]) == 1
    assert sorted(path for item in deferred.dimensions for path in item.target_files) == snapshot.review_paths
    assert deferred.unresolved_risks


def test_repair_is_stable_for_dict_key_order_and_splits_in_path_order(
    review_repo: tuple[Path, str, str],
) -> None:
    snapshot = snapshot_for(review_repo)
    config = DeepReviewConfig(max_files_per_work_item=2)
    first = {"dimensions": [dimension("values", ["c.py", "a.py", "b.py"])]}
    second = {"dimensions": [{
        "target_files": ["c.py", "a.py", "b.py"],
        "priority": 5,
        "context_files": [],
        "review_prompt": first["dimensions"][0]["review_prompt"],
        "name": "values",
    }]}
    left = repair_plan(first, snapshot, config)
    right = repair_plan(second, snapshot, config)
    assert left.model_dump_json() == right.model_dump_json()
    assert [item.target_files for item in left.dimensions] == [["a.py", "b.py"], ["c.py"]]


def test_repair_25_files_defers_without_discarding_paths(
    review_repo: tuple[Path, str, str],
) -> None:
    snapshot = snapshot_for(review_repo)
    paths = [f"file_{index:02d}.py" for index in range(25)]
    snapshot = replace(
        snapshot,
        filter_result=FilterResult(decisions=[], review_paths=paths, context_paths=[]),
    )
    draft = {"dimensions": [dimension("large", paths)]}
    plan = repair_plan(
        draft, snapshot,
        DeepReviewConfig(max_files_per_work_item=12, max_final_dimensions=2),
    )
    assert len([item for item in plan.dimensions if not item.deferred]) == 2
    assert len([item for item in plan.dimensions if item.deferred]) == 1
    assert sorted(path for item in plan.dimensions for path in item.target_files) == paths
    assert plan.coverage_complete is False
    assert plan.unresolved_risks


@pytest.mark.parametrize(
    ("file_count", "expected_group_sizes"),
    [(13, [13]), (16, [16]), (17, [12, 5])],
)
def test_repair_only_splits_after_one_third_over_nominal_file_limit(
    review_repo: tuple[Path, str, str],
    file_count: int,
    expected_group_sizes: list[int],
) -> None:
    snapshot = snapshot_for(review_repo)
    paths = [f"file_{index:02d}.py" for index in range(file_count)]
    snapshot = replace(
        snapshot,
        filter_result=FilterResult(decisions=[], review_paths=paths, context_paths=[]),
    )

    plan = repair_plan(
        {"dimensions": [dimension("combined", paths)]},
        snapshot,
        DeepReviewConfig(max_files_per_work_item=12),
    )

    assert [len(item.target_files) for item in plan.dimensions] == expected_group_sizes
    assert sorted(path for item in plan.dimensions for path in item.target_files) == paths
    assert plan.coverage_complete is True


def test_repair_empty_draft_fills_all_files(review_repo: tuple[Path, str, str]) -> None:
    snapshot = snapshot_for(review_repo)
    plan = repair_plan(
        {"dimensions": []}, snapshot, DeepReviewConfig(max_files_per_work_item=2),
    )
    assert sorted(path for item in plan.dimensions for path in item.target_files) == snapshot.review_paths
    assert all(item.fallback for item in plan.dimensions)
    assert plan.coverage_complete


def test_repair_remaps_hint_after_merge_but_drops_ambiguous_split(
    review_repo: tuple[Path, str, str],
) -> None:
    snapshot = snapshot_for(review_repo)
    draft = {"dimensions": [
        dimension("a", ["a.py"]), dimension("b", ["b.py"]), dimension("c", ["c.py"]),
    ], "cross_reference_hints": [{
        "dimension_names": ["a", "c"],
        "relation": "Compare producer and consumer behavior",
        "symbol_or_contract": "VALUE",
    }]}
    plan = repair_plan(
        draft, snapshot,
        DeepReviewConfig(max_final_dimensions=2, max_files_per_work_item=2),
    )
    assert len(plan.cross_reference_hints) == 1
    assert set(plan.cross_reference_hints[0].dimension_names) == {
        item.name for item in plan.dimensions
    }

    split = repair_plan({"dimensions": [
        dimension("a", ["a.py", "b.py"]), dimension("c", ["c.py"]),
    ], "cross_reference_hints": draft["cross_reference_hints"]}, snapshot,
        DeepReviewConfig(max_files_per_work_item=1),
    )
    assert split.cross_reference_hints == []
    assert "dropped_invalid_cross_reference_hint" in split.repair_actions


def test_schema_and_repair_do_not_import_agent_modules() -> None:
    root = Path(__file__).resolve().parents[2] / "app" / "domains" / "deep_review"
    for path in [root / "schemas" / "pipeline.py", Path(plan_repair_module.__file__)]:
        assert "from app.domains.deep_review.agents" not in path.read_text(encoding="utf-8")


def test_runtime_factory_checks_role_and_creates_distinct_bridges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge_module = types.ModuleType("app.execution_plane.runtime.bridge")

    class FakeBridge:
        def __init__(self, **kwargs: Any):
            self.options = kwargs

    bridge_module.RuntimeBridge = FakeBridge
    monkeypatch.setitem(sys.modules, "app.execution_plane.runtime.bridge", bridge_module)

    class FakeLLMService:
        def __init__(self):
            self.roles: list[str] = []

        def get_config_for(self, role: str) -> object:
            self.roles.append(role)
            return object()

    llm = FakeLLMService()
    factory = RuntimeBridgeDeepReviewRuntimeFactory(llm_service=llm, session_factory="sessions")
    first = factory.for_role("deep_review:planner")
    second = factory.for_role("deep_review:planner")
    assert first is not second
    assert first.options == {
        "llm_service": llm, "tools": [], "session_factory": "sessions",
        "agent_type": "deep_review:planner",
    }
    assert llm.roles == ["deep_review:planner", "deep_review:planner"]


def test_planning_cli_with_fake_runtime_writes_observations(
    review_repo: tuple[Path, str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.domains.deep_review import __main__ as cli
    from app.domains.deep_review.services import runtime as runtime_module

    repo, base, head = review_repo
    output = tmp_path / "planning.json"
    semantic, planner = FakeRuntime(), FakeRuntime()
    fake_factory = FakeFactory(semantic, planner)

    class FakeLLMService:
        def get_config_for(self, role: str) -> object:
            return object()

    llm_package = types.ModuleType("app.execution_plane.models")
    llm_package.__path__ = []
    llm_module = types.ModuleType("app.execution_plane.models.service")
    llm_module.LLMService = FakeLLMService
    monkeypatch.setitem(sys.modules, "app.execution_plane.models", llm_package)
    monkeypatch.setitem(sys.modules, "app.execution_plane.models.service", llm_module)
    monkeypatch.setattr(
        runtime_module, "RuntimeBridgeDeepReviewRuntimeFactory",
        lambda **kwargs: fake_factory,
    )
    assert cli.main([
        "--repo", str(repo), "--base", base, "--head", head,
        "--through", "planning", "--store-dir", str(tmp_path / "events"),
        "--output", str(output),
    ]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["completed_stage"] == "planning"
    assert payload["plan"]["dimensions"][0]["context_files"] == ["unchanged.py"]
    assert [item["stage"] for item in payload["agent_observations"]] == ["semantic", "planning"]
    assert payload["agent_observations"][0]["session_id"] is None
    assert payload["agent_observations"][1]["session_id"] == "session-planner"


def test_review_cli_runs_preparation_and_reviewer_with_fake_runtime(
    review_repo: tuple[Path, str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.domains.deep_review import __main__ as cli
    from app.domains.deep_review.services import runtime as runtime_module

    repo, base, head = review_repo
    output = tmp_path / "review-report.json"
    semantic = FakeRuntime()
    planner = FakeRuntime(review_payload={
        "findings": [{
            "file_path": "a.py",
            "line_start": 1,
            "line_end": 1,
            "severity": "high",
            "title": "Changed value breaks caller",
            "body": "When this value reaches the caller, its required behavior is no longer preserved.",
            "evidence": "a.py line 1 changes VALUE from 1 to 2.",
            "suggestion": "Preserve the caller contract.",
            "confidence": 0.9,
            "tags": ["behavior"],
        }],
        "summary": "The value change breaks the caller contract.",
    })
    fake_factory = FakeFactory(semantic, planner)

    class FakeLLMService:
        def get_config_for(self, role: str) -> object:
            return object()

    llm_package = types.ModuleType("app.execution_plane.models")
    llm_package.__path__ = []
    llm_module = types.ModuleType("app.execution_plane.models.service")
    llm_module.LLMService = FakeLLMService
    monkeypatch.setitem(sys.modules, "app.execution_plane.models", llm_package)
    monkeypatch.setitem(sys.modules, "app.execution_plane.models.service", llm_module)
    monkeypatch.setattr(
        runtime_module,
        "RuntimeBridgeDeepReviewRuntimeFactory",
        lambda **kwargs: fake_factory,
    )

    assert cli.main([
        "--repo", str(repo), "--base", base, "--head", head,
        "--through", "review", "--store-dir", str(tmp_path / "events"),
        "--output", str(output),
    ]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["completed_stage"] == "review"
    assert payload["pipeline_complete"] is False
    assert payload["reviewer_dimensions_started"] == 1
    assert payload["reviewer_dimensions_succeeded"] == 1
    assert payload["reviewer_dimensions_failed"] == 0
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["dimension_name"] == "values"
    assert payload["candidates"][0]["source"] == "reviewer"
    assert [item["stage"] for item in payload["agent_observations"]] == [
        "semantic", "planning", "reviewer",
    ]
    assert payload["agent_observations"][-1]["session_id"] == "session-planner"
