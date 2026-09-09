from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from codesage_eval.contracts import DatasetCase, EvalCaseResult
from codesage_eval.runner import ControlPlaneHttpAdapter, run_cases
from codesage_eval.storage import read_jsonl


def _case(fixture_path, case_id="case"):
    fixture_path.write_text("diff --git a/a b/a\n+x\n", encoding="utf-8")
    from codesage_eval.dataset import hash_fixture

    digest = hash_fixture(fixture_path)
    return DatasetCase(
        case_id=case_id, pr_url="url", repo="repo", pr_title="", golden=[], golden_sha256="hash",
        source_mode="diff_only", fixture_path=str(fixture_path), fixture_sha256=digest, diff_sha256=digest,
    )


def test_runner_uses_control_plane_without_leaking_golden_and_registers_before_start(tmp_path):
    requests = []
    states = iter(["running", "completed"])

    def handler(request: httpx.Request):
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.method == "POST" and request.url.path.endswith("/agent-tasks/"):
            return httpx.Response(200, json={"id": "task-1", "status": "pending"})
        if request.method == "POST" and request.url.path.endswith("/start"):
            return httpx.Response(200, json={"id": "task-1", "status": "running"})
        if request.url.path.endswith("/findings"):
            return httpx.Response(200, json=[{"title": "issue", "description": "detail"}])
        return httpx.Response(200, json={"id": "task-1", "status": next(states)})

    adapter = ControlPlaneHttpAdapter(base_url="https://api.test", token="secret", transport=httpx.MockTransport(handler), poll_interval=0)
    output = tmp_path / "cases.jsonl"
    fixture = tmp_path / "input.diff"
    results = asyncio.run(run_cases(cases=[_case(fixture)], eval_run_id="run", adapter=adapter, project_ids={"repo": "project"}, output_path=output))
    assert results[0].status == "completed"
    create_body = next(item[2] for item in requests if item[0] == "POST" and item[1].endswith("/agent-tasks/"))
    serialized = json.dumps(create_body)
    assert "golden" not in serialized
    assert create_body["audit_scope"]["pr_review"] == {
        "diff_file_path": str(fixture), "eval_run_id": "run", "case_id": "case"
    }
    assert read_jsonl(output)[0]["task_id"] == "task-1"


def test_resume_registered_task_does_not_create_or_start_again(tmp_path):
    requests = []

    def handler(request: httpx.Request):
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/findings"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"id": "task-1", "status": "completed"})

    adapter = ControlPlaneHttpAdapter(base_url="https://api.test", token="secret", transport=httpx.MockTransport(handler), poll_interval=0)
    existing = EvalCaseResult(eval_run_id="run", case_id="case", status="running", task_id="task-1")
    result = asyncio.run(adapter.run_case(_case(tmp_path / "input.diff"), eval_run_id="run", project_id="project", existing=existing))
    assert result.status == "completed"
    assert all(method == "GET" for method, _ in requests)


def test_runner_rejects_more_than_two_in_flight(tmp_path):
    adapter = ControlPlaneHttpAdapter(base_url="https://api.test", token="secret")
    with pytest.raises(ValueError, match="concurrency"):
        asyncio.run(
            run_cases(
                cases=[], eval_run_id="run", adapter=adapter, project_ids={},
                output_path=tmp_path / "cases.jsonl", concurrency=3,
            )
        )
