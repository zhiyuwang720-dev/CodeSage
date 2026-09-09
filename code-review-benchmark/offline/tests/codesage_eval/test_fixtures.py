from __future__ import annotations

import json

import httpx

from codesage_eval.dataset import load_dataset, stable_id
from codesage_eval.fixtures import fetch_current_pr_fixtures


def test_fetch_current_pr_is_hashed_relative_and_idempotent(tmp_path):
    pr_url = "https://github.com/acme/widget/pull/7"
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps({pr_url: {"source_repo": "widget", "golden_comments": []}}))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["accept"])
        if request.headers["accept"] == "application/vnd.github.v3.diff":
            return httpx.Response(200, content=b"diff --git a/a.py b/a.py\n+fixed\n")
        return httpx.Response(
            200,
            json={"base": {"sha": "a" * 40}, "head": {"sha": "b" * 40}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    output = tmp_path / "fixtures" / "current-fixtures.json"
    kwargs = dict(
        dataset_path=dataset,
        output_map=output,
        fixtures_root=tmp_path / "fixtures",
        client=client,
    )
    first = fetch_current_pr_fixtures(**kwargs)
    original = output.read_bytes()
    second = fetch_current_pr_fixtures(**kwargs)

    assert output.read_bytes() == original
    assert first == second
    assert calls.count("application/vnd.github.v3.diff") == 2
    entry = first[pr_url]
    assert entry["source_mode"] == "diff_only"
    assert entry["baseline_eligible"] is False
    assert entry["provenance"]["kind"] == "current_pr"
    assert entry["path"] == f"current/{stable_id('case', pr_url)}/review.diff"
    case = load_dataset(dataset, first, fixture_base=output.parent)[0]
    assert case.source_mode == "diff_only"
    assert case.baseline_eligible is False
    assert case.diff_sha256 == entry["provenance"]["diff_sha256"]
