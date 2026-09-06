from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.services.contracts.review_execution import (
    ArtifactRef,
    ExecutionContext,
    ReviewRunIdentity,
    StageResult,
    build_config_fingerprint,
    sha256_bytes,
)


def identity(**overrides):
    values = {
        "run_id": "run-1",
        "task_id": "task-1",
        "source_kind": "git",
        "repository_key": "github:owner/repo",
        "pr_number": 7,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "diff_sha256": sha256_bytes(b"diff"),
        "config_fingerprint": build_config_fingerprint({"model": "fixture", "temperature": 0}),
    }
    values.update(overrides)
    return ReviewRunIdentity(**values)


def test_git_identity_requires_fixed_range_and_diff_may_omit_it():
    with pytest.raises(ValidationError, match="base_sha"):
        identity(base_sha=None)
    diff_identity = identity(source_kind="diff", base_sha=None, head_sha=None)
    assert diff_identity.source_kind == "diff"


def test_execution_context_requires_absolute_trusted_paths(tmp_path):
    context = ExecutionContext(
        identity=identity(),
        attempt_id="attempt-1",
        worker_id="worker-1",
        lease_epoch=1,
        workspace_root=str(tmp_path.resolve()),
        artifact_root=str((tmp_path / "artifacts").resolve()),
        deadline_at=datetime.now(timezone.utc),
    )
    assert context.lease_epoch == 1
    with pytest.raises(ValidationError, match="绝对路径"):
        context.model_copy(update={"workspace_root": "relative"}).model_validate(
            {**context.model_dump(), "workspace_root": "relative"}
        )


@pytest.mark.parametrize("path", ["../secret", "/absolute", "C:/absolute", "a/../b"])
def test_artifact_ref_rejects_escape(path):
    with pytest.raises(ValidationError):
        ArtifactRef(
            artifact_id="a",
            run_id="run-1",
            kind="result",
            relative_path=path,
            sha256="0" * 64,
            size_bytes=0,
            media_type="application/json",
        )


def test_stage_result_rejects_cross_run_artifact_and_ambiguous_incomplete():
    ref = ArtifactRef(
        artifact_id="a",
        run_id="other",
        kind="result",
        relative_path="result.json",
        sha256="0" * 64,
        size_bytes=0,
        media_type="application/json",
    )
    with pytest.raises(ValidationError, match="其他 run"):
        StageResult(run_id="run-1", stage_type="report", status="completed", artifact_refs=[ref])
    with pytest.raises(ValidationError, match="错误原因"):
        StageResult(run_id="run-1", stage_type="review:security", status="incomplete")


def test_config_fingerprint_is_stable_and_rejects_secrets():
    assert build_config_fingerprint({"b": 2, "a": 1}) == build_config_fingerprint({"a": 1, "b": 2})
    with pytest.raises(ValueError, match="凭据"):
        build_config_fingerprint({"nested": {"api_key": "do-not-hash"}})

