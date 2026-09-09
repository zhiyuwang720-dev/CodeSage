from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from codesage_eval.contracts import CandidateFinding, DatasetCase, EvalCaseResult
from codesage_eval.dataset import validate_case_fixture
from codesage_eval.storage import read_jsonl, upsert_records

TERMINAL_STATES = {"completed", "failed", "cancelled"}


def finding_id(case_id: str, index: int, payload: dict[str, Any]) -> str:
    stable = "\0".join(
        [
            case_id,
            str(index),
            str(payload.get("title") or ""),
            str(payload.get("file_path") or ""),
            str(payload.get("line_start") or ""),
        ]
    )
    return f"candidate-{hashlib.sha256(stable.encode()).hexdigest()[:16]}"


class ControlPlaneHttpAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: int = 3600,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval: float = 0.5,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.headers = {"Authorization": f"Bearer {token}"}
        self.transport = transport
        self.poll_interval = poll_interval

    async def run_case(
        self,
        case: DatasetCase,
        *,
        eval_run_id: str,
        project_id: str,
        existing: EvalCaseResult | None = None,
        on_registered: Callable[[EvalCaseResult], Awaitable[None]] | None = None,
    ) -> EvalCaseResult:
        if case.source_mode == "fixture_unverified" or not case.fixture_path or not validate_case_fixture(case):
            return EvalCaseResult(
                eval_run_id=eval_run_id,
                case_id=case.case_id,
                status="failed",
                error_kind="fixture_unverified",
            )
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self.headers, timeout=30, transport=self.transport
        ) as client:
            task_id = existing.task_id if existing else None
            if task_id:
                response = await client.get(f"/api/v1/agent-tasks/{task_id}")
                if response.status_code == 404:
                    raise RuntimeError(f"registered task disappeared: {task_id}")
                response.raise_for_status()
                task = response.json()
            else:
                review_scope = {
                    "eval_run_id": eval_run_id,
                    "case_id": case.case_id,
                }
                if case.source_mode == "full_source":
                    review_scope.update(
                        {
                            "repository_path": case.fixture_path,
                            "base_sha": case.merge_base,
                            "head_sha": case.head_ref,
                        }
                    )
                else:
                    review_scope["diff_file_path"] = case.fixture_path
                response = await client.post(
                    "/api/v1/agent-tasks/",
                    json={
                        "project_id": project_id,
                        "name": f"eval:{eval_run_id}:{case.case_id}",
                        "version_label": eval_run_id,
                        "timeout_seconds": self.timeout_seconds,
                        "audit_scope": {"pr_review": review_scope},
                    },
                )
                response.raise_for_status()
                task = response.json()
                task_id = str(task["id"])
                if on_registered:
                    await on_registered(
                        EvalCaseResult(
                            eval_run_id=eval_run_id,
                            case_id=case.case_id,
                            status="running",
                            task_id=task_id,
                        )
                    )
                start = await client.post(f"/api/v1/agent-tasks/{task_id}/start")
                start.raise_for_status()
            deadline = asyncio.get_running_loop().time() + self.timeout_seconds
            while str(task.get("status") or "").lower() not in TERMINAL_STATES:
                if asyncio.get_running_loop().time() >= deadline:
                    return EvalCaseResult(
                        eval_run_id=eval_run_id,
                        case_id=case.case_id,
                        status="failed",
                        task_id=task_id,
                        error_kind="runner_timeout",
                    )
                await asyncio.sleep(self.poll_interval)
                response = await client.get(f"/api/v1/agent-tasks/{task_id}")
                response.raise_for_status()
                task = response.json()
            status = str(task.get("status") or "").lower()
            if status != "completed":
                return EvalCaseResult(
                    eval_run_id=eval_run_id,
                    case_id=case.case_id,
                    status="failed",
                    task_id=task_id,
                    error_kind=status or "business_failure",
                )
            response = await client.get(f"/api/v1/agent-tasks/{task_id}/findings")
            response.raise_for_status()
            findings = response.json()
            if not validate_case_fixture(case):
                return EvalCaseResult(
                    eval_run_id=eval_run_id,
                    case_id=case.case_id,
                    status="failed",
                    task_id=task_id,
                    error_kind="fixture_drift",
                )
            candidates = [
                CandidateFinding(
                    candidate_id=finding_id(case.case_id, index, item),
                    title=str(item.get("title") or ""),
                    description=str(item.get("description") or ""),
                    severity=item.get("severity"),
                    category=item.get("category"),
                    file_path=item.get("file_path"),
                    line_start=item.get("line_start"),
                    line_end=item.get("line_end"),
                    suggestion=item.get("suggestion"),
                    source=item.get("source"),
                )
                for index, item in enumerate(findings)
            ]
            return EvalCaseResult(
                eval_run_id=eval_run_id,
                case_id=case.case_id,
                status="completed",
                execution_complete=True,
                task_id=task_id,
                candidates=candidates,
            )


async def run_cases(
    *,
    cases: list[DatasetCase],
    eval_run_id: str,
    adapter: ControlPlaneHttpAdapter,
    project_ids: dict[str, str],
    output_path: str | Path,
    concurrency: int = 2,
) -> list[EvalCaseResult]:
    if concurrency not in {1, 2}:
        raise ValueError("evaluation concurrency must be 1 or 2")
    existing = {
        item.case_id: item
        for item in read_jsonl(output_path, EvalCaseResult)
    } if Path(output_path).exists() else {}
    semaphore = asyncio.Semaphore(concurrency)
    writer_lock = asyncio.Lock()

    async def run(case: DatasetCase) -> EvalCaseResult:
        previous = existing.get(case.case_id)
        if previous and previous.status == "completed":
            return previous
        project_id = project_ids.get(case.case_id) or project_ids.get(case.repo)
        if not project_id:
            return EvalCaseResult(
                eval_run_id=eval_run_id,
                case_id=case.case_id,
                status="failed",
                error_kind="project_mapping_missing",
            )
        async with semaphore:
            async def register(result: EvalCaseResult) -> None:
                async with writer_lock:
                    upsert_records(output_path, [result], key="case_id")

            result = await adapter.run_case(
                case,
                eval_run_id=eval_run_id,
                project_id=project_id,
                existing=previous,
                on_registered=register,
            )
            async with writer_lock:
                upsert_records(output_path, [result], key="case_id")
            return result

    return await asyncio.gather(*(run(case) for case in cases))
