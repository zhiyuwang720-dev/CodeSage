import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.pr_review.inputs import prepare_review_input


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, encoding="utf-8"
    ).strip()


def _task(scope: dict):
    return SimpleNamespace(
        id="task-input",
        project_id="project-input",
        audit_scope={"pr_review": scope},
        repository_url_snapshot=None,
        commit_sha=None,
    )


@pytest.mark.asyncio
async def test_git_input_resolves_commits_and_hashes_real_fixed_diff(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "plan18@example.invalid")
    _git(repo, "config", "user.name", "Plan 18")
    (repo / "a.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "head")
    head = _git(repo, "rev-parse", "HEAD")

    identity, content, source_dir, _ = await prepare_review_input(
        _task({"repository_path": str(repo), "base_sha": base, "head_sha": head}),
        compatibility_config={"flow": "test"},
    )

    assert identity.source_kind == "git"
    assert identity.base_sha == base
    assert identity.head_sha == head
    assert identity.diff_sha256
    assert b"+value = 2" in content
    assert source_dir == str(repo.resolve())


@pytest.mark.asyncio
async def test_explicit_diff_has_priority_over_url_and_git(tmp_path):
    diff = tmp_path / "review.diff"
    diff.write_text("diff --git a/a.py b/a.py\n+safe = True\n", encoding="utf-8")
    identity, content, source_dir, pr_url = await prepare_review_input(
        _task(
            {
                "diff_file_path": str(diff),
                "pr_url": "https://github.com/example/repo/pull/1",
                "repository_path": str(tmp_path / "missing"),
            }
        ),
        compatibility_config={"flow": "test"},
    )
    assert identity.source_kind == "diff"
    assert content == diff.read_bytes()
    assert source_dir is None
    assert pr_url.endswith("/pull/1")
