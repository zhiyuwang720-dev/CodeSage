import subprocess
from pathlib import Path

import pytest

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ChangeType, FileChange, ReviewInput
from app.domains.deep_review.schemas.pipeline import ReviewFinding
from app.domains.deep_review.services import evidence
from app.domains.deep_review.services.evidence import EvidenceError, build_evidence_packages
from app.domains.deep_review.services.input_builder import ReviewSnapshot, build_review_snapshot


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


async def test_evidence_extracts_primary_diff_and_caller(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "changed.py").write_text("def changed():\n    return 1\n", encoding="utf-8")
    (repo / "caller.py").write_text("from changed import changed\n\nchanged()\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "changed.py").write_text("def changed():\n    return 2\n", encoding="utf-8")
    git(repo, "add", "changed.py")
    git(repo, "commit", "-m", "head")
    head = git(repo, "rev-parse", "HEAD")
    snapshot = build_review_snapshot(
        ReviewInput(repo_path=str(repo), base_ref=base, head_ref=head), DeepReviewConfig()
    )
    finding = ReviewFinding(
        file_path="changed.py",
        line_start=2,
        severity="high",
        title="changed behavior",
        body="`changed` now returns 2",
    )
    packages = await build_evidence_packages([finding], snapshot, [], DeepReviewConfig())

    package = packages[0]
    assert "return 2" in package.primary_code
    assert "+    return 2" in package.diff_hunk
    assert any("caller.py" in snippet for snippet in package.caller_snippets)


async def test_evidence_denies_excluded_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "excluded.py").write_text("value = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "excluded.py").write_text("value = 2\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "head")
    head = git(repo, "rev-parse", "HEAD")
    config = DeepReviewConfig(exclude_paths=["excluded.py"])
    snapshot = build_review_snapshot(
        ReviewInput(repo_path=str(repo), base_ref=base, head_ref=head), config
    )
    finding = ReviewFinding(
        file_path="excluded.py", line_start=1, severity="low", title="x", body="y"
    )
    packages = await build_evidence_packages([finding], snapshot, [], config)
    assert packages[0].primary_code == ""
    assert packages[0].diff_hunk == ""


async def test_evidence_budget_is_hard_limit(tmp_path: Path) -> None:
    repo = tmp_path / "budget-repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "large.py").write_text("value = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "large.py").write_text("value = '" + ("x" * 20_000) + "'\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "head")
    head = git(repo, "rev-parse", "HEAD")
    snapshot = build_review_snapshot(
        ReviewInput(repo_path=str(repo), base_ref=base, head_ref=head),
        DeepReviewConfig(max_evidence_bytes_per_finding=512),
    )
    finding = ReviewFinding(
        file_path="large.py", line_start=1, severity="low", title="large", body="large value"
    )
    packages = await build_evidence_packages([finding], snapshot, [], DeepReviewConfig(max_evidence_bytes_per_finding=512))

    encoded = packages[0].model_dump_json().encode("utf-8")
    assert len(encoded) <= 512


async def test_evidence_does_not_search_secret_paths(tmp_path: Path) -> None:
    repo = tmp_path / "secret-repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "changed.py").write_text("value = 1\n", encoding="utf-8")
    (repo / ".env").write_text("secret_value = hidden\n", encoding="utf-8")
    git(repo, "add", "changed.py", ".env")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "changed.py").write_text("value = 2\n", encoding="utf-8")
    git(repo, "add", "changed.py")
    git(repo, "commit", "-m", "head")
    head = git(repo, "rev-parse", "HEAD")
    snapshot = build_review_snapshot(
        ReviewInput(repo_path=str(repo), base_ref=base, head_ref=head), DeepReviewConfig()
    )
    finding = ReviewFinding(
        file_path="changed.py", line_start=1, severity="low", title="secret", body="`secret_value` usage"
    )
    packages = await build_evidence_packages([finding], snapshot, [], DeepReviewConfig())

    assert ".env" not in packages[0].model_dump_json()


async def test_evidence_is_fixed_to_head(tmp_path: Path) -> None:
    repo = tmp_path / "fixed-repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "changed.py").write_text("value = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "changed.py").write_text("value = 2\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "head")
    head = git(repo, "rev-parse", "HEAD")
    snapshot = build_review_snapshot(
        ReviewInput(repo_path=str(repo), base_ref=base, head_ref=head), DeepReviewConfig()
    )
    (repo / "changed.py").write_text("working tree mutation\n", encoding="utf-8")
    finding = ReviewFinding(
        file_path="changed.py", line_start=1, severity="low", title="x", body="y"
    )
    packages = await build_evidence_packages([finding], snapshot, [], DeepReviewConfig())
    assert "value = 2" in packages[0].primary_code
    assert "working tree mutation" not in packages[0].model_dump_json()


async def test_one_evidence_failure_degrades_only_that_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "failure-repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "one.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "two.py").write_text("value = 2\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "one.py").write_text("value = 11\n", encoding="utf-8")
    (repo / "two.py").write_text("value = 22\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "head")
    head = git(repo, "rev-parse", "HEAD")
    snapshot = build_review_snapshot(
        ReviewInput(repo_path=str(repo), base_ref=base, head_ref=head), DeepReviewConfig()
    )
    original = evidence._read_head

    async def fail_first(snapshot: object, path: str, config: object) -> str:
        if path == "one.py":
            raise EvidenceError("injected failure")
        return await original(snapshot, path, config)

    monkeypatch.setattr(evidence, "_read_head", fail_first)
    findings = [
        ReviewFinding(file_path="one.py", line_start=1, severity="low", title="one", body="body"),
        ReviewFinding(file_path="two.py", line_start=1, severity="low", title="two", body="body"),
    ]
    packages = await build_evidence_packages(findings, snapshot, [], DeepReviewConfig())

    assert packages[0].primary_code == ""
    assert "value = 22" in packages[1].primary_code
