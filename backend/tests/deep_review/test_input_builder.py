import subprocess
from pathlib import Path

import pytest

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ReviewInput
from app.domains.deep_review.services.input_builder import ReviewInputError, build_review_snapshot


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return result.stdout.strip()


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "old.py").write_text("old()\n", encoding="utf-8")
    git(repo, "add", "a.py", "old.py")
    git(repo, "add", "a.py")
    git(repo, "commit", "-m", "base")
    return repo


def test_build_snapshot_parses_added_modified_and_deleted(git_repo: Path) -> None:
    base = git(git_repo, "rev-parse", "HEAD")
    (git_repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    (git_repo / "b.py").write_text("print('new')\n", encoding="utf-8")
    git(git_repo, "add", "a.py", "b.py")
    git(git_repo, "commit", "-m", "update")
    git(git_repo, "rm", "old.py")
    git(git_repo, "commit", "-m", "delete old")
    head = git(git_repo, "rev-parse", "HEAD")

    snapshot = build_review_snapshot(
        ReviewInput(repo_path=str(git_repo), base_ref=base, head_ref=head),
        DeepReviewConfig(),
    )
    paths = {change.path for change in snapshot.changes}
    assert {"a.py", "b.py", "old.py"} <= paths
    assert "a.py" in snapshot.diff_by_path
    assert snapshot.review_paths == ["a.py", "b.py"]
    assert snapshot.allowed_paths >= {"a.py", "b.py", "old.py"}
    assert snapshot.commit_messages == ["delete old", "update"]


def test_diff_limit_fails(git_repo: Path) -> None:
    base = git(git_repo, "rev-parse", "HEAD")
    (git_repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    git(git_repo, "add", "a.py")
    git(git_repo, "commit", "-m", "update")
    head = git(git_repo, "rev-parse", "HEAD")
    with pytest.raises(ReviewInputError, match="max_diff_bytes"):
        build_review_snapshot(
            ReviewInput(repo_path=str(git_repo), base_ref=base, head_ref=head),
            DeepReviewConfig(max_diff_bytes=1),
        )
