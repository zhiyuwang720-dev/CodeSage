"""Plan 18 API compatibility wrappers: execution semantics remain in services."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.api.v1.endpoints.agent_tasks as endpoint
from app.execution_plane.task_executor import _running_asyncio_tasks


@pytest.mark.asyncio
async def test_pr_review_compatibility_wrapper_delegates_to_service(monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(
        "app.execution_plane.review.quick_review.execute_review_use_case", execute
    )
    await endpoint._execute_pr_review_task_impl(
        SimpleNamespace(),
        SimpleNamespace(id="task-pr"),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    execute.assert_awaited_once_with("task-pr")


def test_agent_task_compatibility_wrapper_registers_and_cleans_runner(monkeypatch):
    observed = {}

    async def fake_service(task_id: str, delivery_id=None) -> None:
        del delivery_id
        observed["registered"] = _running_asyncio_tasks.get(task_id)

    monkeypatch.setattr(endpoint, "execute_agent_task", fake_service)

    async def run() -> None:
        await asyncio.create_task(endpoint._execute_agent_task_impl("task-reg"))

    asyncio.run(run())
    assert observed["registered"] is not None
    assert "task-reg" not in _running_asyncio_tasks
