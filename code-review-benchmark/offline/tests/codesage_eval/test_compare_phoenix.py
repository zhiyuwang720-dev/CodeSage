from __future__ import annotations

from types import SimpleNamespace

from codesage_eval.compare import paired_bootstrap, validate_comparable
from codesage_eval.contracts import DatasetCase
from codesage_eval.phoenix_adapter import PhoenixAdapter


def test_comparison_rejects_scoring_and_fixture_mismatch_and_is_deterministic():
    base = {
        "case_ids": ["a"], "dataset_sha256": "d", "judge_fingerprint": "j",
        "scoring_version": "s", "serializer_version": "z", "coverage_version": "c",
        "fixture_fingerprints": {"a": "f"},
    }
    candidate = dict(base, scoring_version="other")
    assert validate_comparable(base, candidate) == ["scoring_version"]
    first = paired_bootstrap({"a": .2, "b": .4}, {"a": .4, "b": .5})
    second = paired_bootstrap({"a": .2, "b": .4}, {"a": .4, "b": .5})
    assert first == second
    assert first["iterations"] == 1000


class FakePhoenixClient:
    def __init__(self):
        self.retries = None
        self.datasets = SimpleNamespace(create_dataset=lambda **kwargs: kwargs)
        self.experiments = SimpleNamespace(run_experiment=self._run)
        self.spans = SimpleNamespace(get_spans=lambda **kwargs: [kwargs])

    def _run(self, **kwargs):
        self.retries = kwargs["retries"]
        return kwargs


def test_phoenix_boundary_disables_task_retries_and_links_attributes():
    client = FakePhoenixClient()
    adapter = PhoenixAdapter(client=client)
    case = DatasetCase(case_id="case", pr_url="url", repo="repo", pr_title="", golden=[], golden_sha256="hash")
    dataset = adapter.create_dataset(name="dataset", cases=[case])
    assert dataset["inputs"] == [{"case_id": "case", "pr_url": "url"}]
    adapter.run_experiment(dataset=dataset, task=lambda _: None, eval_run_id="run")
    assert client.retries == 0
    spans = adapter.wait_for_trace(project_identifier="project", eval_run_id="run", case_id="case", timeout_seconds=.01)
    assert spans[0]["attributes"] == {"codesage.eval_run_id": "run", "codesage.case_id": "case"}
