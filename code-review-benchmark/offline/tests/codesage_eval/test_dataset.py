from __future__ import annotations

import json
import subprocess

from codesage_eval.dataset import load_dataset, select_suite


def _dataset(path):
    data = {}
    for repo_index in range(5):
        for case_index in range(10):
            url = f"https://example.test/r{repo_index}/pull/{case_index}"
            data[url] = {
                "source_repo": f"repo-{repo_index}",
                "pr_title": f"case {case_index}",
                "golden_comments": [{"comment": f"golden {case_index}"}],
            }
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def test_suite_partition_is_stable_and_has_expected_sizes(tmp_path):
    dataset = tmp_path / "benchmark_data.json"
    _dataset(dataset)
    cases = load_dataset(dataset)
    assert len(cases) == 50
    assert len(select_suite(cases, "smoke")) == 2
    calibration = select_suite(cases, "calibration")
    holdout = select_suite(cases, "holdout")
    assert len(calibration) == 10
    assert len(holdout) == 40
    assert {item.case_id for item in calibration}.isdisjoint(item.case_id for item in holdout)


def test_full_source_resolves_refs_and_exact_diff(tmp_path):
    repository = tmp_path / "fixture"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    source = repository / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.py"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    source.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "head"], cwd=repository, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    dataset = tmp_path / "benchmark_data.json"
    raw = {"https://example.test/repo/pull/1": {"source_repo": "repo", "golden_comments": []}}
    dataset.write_text(json.dumps(raw), encoding="utf-8")

    case = load_dataset(
        dataset,
        {
            next(iter(raw)): {
                "path": str(repository),
                "source_mode": "full_source",
                "base_ref": base,
                "head_ref": head,
            }
        },
    )[0]
    assert case.source_mode == "full_source"
    assert case.base_ref == base
    assert case.head_ref == head
    assert case.merge_base == base
    assert case.diff_sha256
    assert case.changed_lines == 2


def test_claimed_full_source_without_immutable_refs_is_unverified(tmp_path):
    dataset = tmp_path / "benchmark_data.json"
    raw = {"https://example.test/repo/pull/1": {"source_repo": "repo", "golden_comments": []}}
    dataset.write_text(json.dumps(raw), encoding="utf-8")
    case = load_dataset(
        dataset,
        {next(iter(raw)): {"path": str(tmp_path), "source_mode": "full_source"}},
    )[0]
    assert case.source_mode == "fixture_unverified"
