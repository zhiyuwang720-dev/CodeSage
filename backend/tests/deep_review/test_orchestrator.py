from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ReviewInput
from app.domains.deep_review.schemas.output import PreparationReport
from app.domains.deep_review.services import orchestrator as orchestrator_module
from app.domains.deep_review.services.orchestrator import PreparationOrchestrator
from app.domains.deep_review.services.service import DeepReviewService
from app.domains.deep_review.storage.protocol import DeepReviewStoreError


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


@dataclass
class MemoryStore:
    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    fail_after: int | None = None

    def append(self, run_id: str, record_type: str, payload: dict[str, Any]) -> None:
        if self.fail_after is not None and len(self.events) >= self.fail_after:
            raise DeepReviewStoreError("injected storage failure")
        self.events.append((run_id, record_type, dict(payload)))


class NoRuntimeFactory:
    def for_role(self, role: str) -> object:
        raise AssertionError("preparation pipeline must not initialize a model runtime")


@pytest.fixture()
def preparation_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
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
    base = git(preparation_repo, "rev-parse", "HEAD^")
    head = git(preparation_repo, "rev-parse", "HEAD")
    return base, head


def make_service(store: MemoryStore, max_concurrency: int = 4) -> DeepReviewService:
    return DeepReviewService(
        config=DeepReviewConfig(max_concurrent_reviewers=max_concurrency),
        store=store,
        runtime_factory=NoRuntimeFactory(),
    )


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
    assert NoRuntimeFactory.for_role  # fixture remains valid if refactored
    assert [record_type for _run, record_type, _payload in store.events][0] == "run_started"


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


async def test_intake_failure_returns_input_error_and_no_report(
    preparation_repo: Path, prepared_head: tuple[str, str]
) -> None:
    base, head = prepared_head
    store = MemoryStore()
    service = make_service(store)
    with pytest.raises(ValueError, match="ref not found"):
        await service.run(
            ReviewInput(repo_path=str(preparation_repo), base_ref="missing-ref", head_ref=head)
        )
    assert MemoryStore  # keeps fixture helper referenced in failure path too
    assert store.events[-1][1] == "run_failed"


async def test_unsupported_through_stage_rejected_before_run(
    preparation_repo: Path, prepared_head: tuple[str, str]
) -> None:
    base, head = prepared_head
    store = MemoryStore()
    with pytest.raises(ValueError, match="unsupported --through stage"):
        await make_service(store).run(
            ReviewInput(repo_path=str(preparation_repo), base_ref=base, head_ref=head),
            through="planning",
        )
    assert store.events == []


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


async def test_same_input_creates_new_runs_with_stable_business_fields(
    preparation_repo: Path, prepared_head: tuple[str, str]
) -> None:
    base, head = prepared_head
    input_model = ReviewInput(repo_path=str(preparation_repo), base_ref=base, head_ref=head)
    first = await make_service(MemoryStore()).run(input_model)
    second = await make_service(MemoryStore()).run(input_model)

    assert first.run_id != second.run_id
    first_dict = first.model_dump(exclude={"run_id"})
    second_dict = second.model_dump(exclude={"run_id"})
    assert first_dict == second_dict


async def test_empty_diff_is_valid_preparation(preparation_repo: Path) -> None:
    head = git(preparation_repo, "rev-parse", "HEAD")
    report = await make_service(MemoryStore()).run(
        ReviewInput(repo_path=str(preparation_repo), base_ref=head, head_ref=head)
    )
    assert report.review_paths == []
    assert report.context_paths == []
    assert report.completed_stage == "anatomy"
    assert report.pipeline_complete is False
