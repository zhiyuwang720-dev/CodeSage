from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, Set

from app.models.agent_task import AgentTask, AgentTaskStatus

logger = logging.getLogger(__name__)

_running_tasks: Dict[str, Any] = {}
_running_asyncio_tasks: Dict[str, asyncio.Task] = {}
_cancelled_tasks: Set[str] = set()


def is_task_cancelled(task_id: str) -> bool:
    return task_id in _cancelled_tasks


def clear_task_cancellation(task_id: str) -> None:
    _cancelled_tasks.discard(task_id)


def request_agent_task_cancellation(task_id: str) -> None:
    """Signal cancellation in the current process and stop known running work."""
    _cancelled_tasks.add(task_id)
    runner = _running_tasks.get(task_id)
    if runner and hasattr(runner, "cancel"):
        runner.cancel()

    from app.services.agent.core.graph_controller import stop_all_agents

    try:
        stop_result = stop_all_agents(exclude_root=False)
        logger.info("[Cancel] Stopped all agents: %s", stop_result)
    except Exception as exc:
        logger.warning("[Cancel] Failed to stop agents via registry: %s", exc)

    asyncio_task = _running_asyncio_tasks.get(task_id)
    if asyncio_task and not asyncio_task.done():
        asyncio_task.cancel()


async def _watch_task_cancellation(
    *,
    task_id: str,
    run_task: asyncio.Task,
    session_factory: Callable[[], Any],
    poll_interval: float = 1.0,
) -> None:
    """Cancel local execution when another process marks the DB task cancelled."""
    while not run_task.done():
        await asyncio.sleep(poll_interval)
        async with session_factory() as db:
            task = await db.get(AgentTask, task_id)
        if task and task.status == AgentTaskStatus.CANCELLED:
            request_agent_task_cancellation(task_id)
            if not run_task.done():
                run_task.cancel()
            return


async def execute_agent_task(task_id: str) -> None:
    """Service entrypoint for executing an agent audit task."""
    from app.api.v1.endpoints.agent_tasks import _execute_agent_task_impl

    await _execute_agent_task_impl(task_id)
