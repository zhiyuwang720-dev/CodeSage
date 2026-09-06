from __future__ import annotations

import pytest

from app.services.pr_review.artifacts import ArtifactIntegrityError, LocalReviewArtifactStore


def test_round_trip_and_tamper_detection(tmp_path):
    store = LocalReviewArtifactStore(tmp_path)
    ref = store.write_bytes(
        run_id="run-1",
        kind="input_diff",
        relative_path="input/review.diff",
        content=b"diff --git a/a.py b/a.py\n",
        media_type="text/x-diff",
    )
    assert store.read_verified(ref).startswith(b"diff --git")
    (tmp_path / "run-1" / "input" / "review.diff").write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError, match="不匹配"):
        store.read_verified(ref)


def test_store_rejects_path_escape(tmp_path):
    store = LocalReviewArtifactStore(tmp_path)
    with pytest.raises(Exception):
        store.write_bytes(
            run_id="run-1",
            kind="result",
            relative_path="../outside.json",
            content=b"{}",
            media_type="application/json",
        )

