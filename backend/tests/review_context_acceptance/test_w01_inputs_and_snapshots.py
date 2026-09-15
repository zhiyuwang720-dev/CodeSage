from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.control_plane.review_inputs import (
    PreparedReviewInput,
    ReviewInputError,
    preflight_review_input,
    prepare_review_input,
)
from app.contracts.review_execution import ArtifactRef
from app.infrastructure.repositories.snapshots import (
    GitSnapshotReader,
    SnapshotError,
    create_snapshot_ref,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, encoding="utf-8"
    ).strip()


def _fixture(repo: Path) -> tuple[str, str, bytes]:
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "plan21@example.invalid")
    _git(repo, "config", "user.name", "Plan 21")
    (repo / "src").mkdir()
    (repo / "src" / "service.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "service.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "head")
    head = _git(repo, "rev-parse", "HEAD")
    diff = subprocess.check_output(
        ["git", "-C", str(repo), "diff", "--binary", "--no-ext-diff", base, head]
    )
    return base, head, diff


def _task(scope: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id="task-21a",
        project_id="project-21a",
        audit_scope={"pr_review": scope},
        repository_url_snapshot=None,
        commit_sha=None,
    )


@pytest.mark.asyncio
async def test_diff_only_does_not_require_or_expose_repository(tmp_path: Path):
    patch = tmp_path / "review.diff"
    patch.write_text("diff --git a/a.py b/a.py\n@@ -0,0 +1 @@\n+safe = True\n", encoding="utf-8")
    identity, content, source_dir, pr_url = await prepare_review_input(
        _task({"diff_file_path": str(patch), "pr_url": "https://github.com/o/r/pull/7"}),
        compatibility_config={"flow": "test"},
    )
    prepared = PreparedReviewInput(
        identity,
        ArtifactRef(
            artifact_id="input",
            run_id=identity.run_id,
            kind="input_diff",
            relative_path="input/review.diff",
            sha256=identity.diff_sha256,
            size_bytes=len(content),
            media_type="text/x-diff",
        ),
        content,
        source_dir,
        pr_url,
    )
    capabilities = await preflight_review_input(
        prepared, execution_attempt_id="attempt", worker_id="worker"
    )
    assert capabilities.mode == "diff_only"
    assert capabilities.source_status == "not_requested"
    assert "read_source" not in capabilities.capabilities


@pytest.mark.asyncio
async def test_diff_plus_repo_requires_matching_fixed_range(tmp_path: Path):
    repo = tmp_path / "repo"
    base, head, diff = _fixture(repo)
    patch = tmp_path / "review.diff"
    patch.write_bytes(diff)
    identity, content, source_dir, _ = await prepare_review_input(
        _task(
            {
                "diff_file_path": str(patch),
                "repository_path": str(repo),
                "base_sha": base,
                "head_sha": head,
            }
        ),
        compatibility_config={"flow": "test"},
    )
    assert identity.source_kind == "diff"
    assert identity.base_sha == base
    assert identity.head_sha == head
    assert source_dir == str(repo.resolve())
    assert content == diff

    patch.write_bytes(diff + b"+tampered = True\n")
    with pytest.raises(ReviewInputError, match="不一致") as raised:
        await prepare_review_input(
            _task(
                {
                    "diff_file_path": str(patch),
                    "repository_path": str(repo),
                    "base_sha": base,
                    "head_sha": head,
                }
            ),
            compatibility_config={"flow": "test"},
        )
    assert raised.value.code == "source_diff_mismatch"


@pytest.mark.asyncio
async def test_snapshot_reads_fixed_blob_when_worktree_moves(tmp_path: Path):
    repo = tmp_path / "repo"
    base, head, _ = _fixture(repo)
    snapshot = await create_snapshot_ref(
        repo,
        repository_key="fixture",
        base_revision=base,
        head_revision=head,
        diff_basis="two_dot",
    )
    reader = GitSnapshotReader(repo, snapshot)
    await reader.verify()
    (repo / "src" / "service.py").write_text("value = 999\n", encoding="utf-8")
    assert await reader.read_blob(side="base", path="src/service.py") == b"value = 1\n"
    assert await reader.read_blob(side="head", path="src/service.py") == b"value = 2\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "C:/secret", "//server/share", "a/../b", "a\x00b"])
async def test_snapshot_rejects_escaping_paths(tmp_path: Path, path: str):
    repo = tmp_path / "repo"
    base, head, _ = _fixture(repo)
    snapshot = await create_snapshot_ref(
        repo,
        repository_key="fixture",
        base_revision=base,
        head_revision=head,
        diff_basis="two_dot",
    )
    with pytest.raises(SnapshotError) as raised:
        await GitSnapshotReader(repo, snapshot).read_blob(side="head", path=path)
    assert raised.value.code == "source_invalid_path"
