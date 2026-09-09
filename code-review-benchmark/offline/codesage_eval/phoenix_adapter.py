from __future__ import annotations

import time
from collections.abc import Callable

from codesage_eval.contracts import DatasetCase


class PhoenixAdapter:
    """The only Phoenix client boundary used by benchmark orchestration."""

    def __init__(self, *, base_url: str = "http://127.0.0.1:6006", client=None):
        if client is None:
            from phoenix.client import Client

            client = Client(base_url=base_url)
        self.client = client

    def create_dataset(self, *, name: str, cases: list[DatasetCase]):
        return self.client.datasets.create_dataset(
            name=name,
            inputs=[{"case_id": item.case_id, "pr_url": item.pr_url} for item in cases],
            outputs=[{"golden_ids": [gold.golden_id for gold in item.golden]} for item in cases],
            metadata=[{"repo": item.repo, "source_mode": item.source_mode} for item in cases],
        )

    def run_experiment(self, *, dataset, task: Callable, eval_run_id: str):
        return self.client.experiments.run_experiment(
            dataset=dataset,
            task=task,
            evaluators=None,
            experiment_name=eval_run_id,
            experiment_metadata={"eval_run_id": eval_run_id},
            retries=0,
            repetitions=1,
        )

    def wait_for_trace(
        self,
        *,
        project_identifier: str,
        eval_run_id: str,
        case_id: str,
        timeout_seconds: float = 30,
        poll_seconds: float = 0.5,
    ):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            spans = self.client.spans.get_spans(
                project_identifier=project_identifier,
                attributes={"codesage.eval_run_id": eval_run_id, "codesage.case_id": case_id},
                limit=10,
            )
            if spans:
                return spans
            time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
        return []

    @staticmethod
    def trace_id(spans) -> str | None:
        if not spans:
            return None
        span = spans[0]
        values = [
            getattr(span, "trace_id", None),
            getattr(getattr(span, "context", None), "trace_id", None),
        ]
        if isinstance(span, dict):
            values.extend([span.get("trace_id"), (span.get("context") or {}).get("trace_id")])
        for value in values:
            if isinstance(value, int):
                return f"{value:032x}"
            if value:
                return str(value)
        return None
