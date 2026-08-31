import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agent.event_manager import EventManager


@pytest.mark.asyncio
async def test_thinking_token_metadata_does_not_store_accumulated_text():
    manager = EventManager(queue_max_size=10)
    manager.create_queue("task-1")

    await manager.add_event(
        task_id="task-1",
        event_type="thinking_token",
        sequence=1,
        metadata={"token": "abc", "accumulated": "abc" * 1000},
    )

    event = await manager._event_queues["task-1"].get()

    assert event["metadata"] == {"token": "abc"}


@pytest.mark.asyncio
async def test_queue_full_drops_low_priority_event_to_keep_terminal_event():
    manager = EventManager(queue_max_size=2)
    manager.create_queue("task-1")

    await manager.add_event(
        task_id="task-1",
        event_type="thinking_token",
        sequence=1,
        metadata={"token": "a"},
    )
    await manager.add_event(
        task_id="task-1",
        event_type="thinking_token",
        sequence=2,
        metadata={"token": "b"},
    )
    await manager.add_event(
        task_id="task-1",
        event_type="task_complete",
        sequence=3,
        message="done",
    )

    queued = []
    queue = manager._event_queues["task-1"]
    while not queue.empty():
        queued.append(queue.get_nowait())

    assert [event["event_type"] for event in queued] == ["thinking_token", "task_complete"]
