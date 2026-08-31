from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.runtime_core.runtime_session_checkpoint_store import RuntimeSessionCheckpointStore


@pytest.mark.asyncio
async def test_runtime_session_checkpoint_store_persists_runtime_snapshot_into_agent_checkpoint():
    session = SimpleNamespace(add=MagicMock(), commit=AsyncMock(), refresh=AsyncMock())

    class _SessionFactory:
        def __call__(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    return session

                async def __aexit__(self_inner, exc_type, exc, tb):
                    return False

            return _Ctx()

    store = RuntimeSessionCheckpointStore(session_factory=_SessionFactory())
    agent_state = SimpleNamespace(
        agent_id="agent-1",
        agent_name="Recon",
        agent_type="recon",
        parent_id=None,
        iteration=3,
        status="running",
        total_tokens=42,
        tool_calls=2,
        findings=[{"id": "finding-1"}],
        metadata={
            "runtime_session_ref": {
                "session_key": "legacy:task-1:agent-1",
                "session_id": "agent-1",
                "task_id": "task-1",
                "source": "legacy",
            },
            "runtime_session_state": {
                "session_id": "agent-1",
                "permission_mode": "plan",
                "metadata": {"plan_mode": {"active": True}},
            },
        },
        model_dump=lambda: {
            "agent_id": "agent-1",
            "status": "running",
            "metadata": {"runtime_session_state": {"session_id": "agent-1"}},
        },
    )

    checkpoint = await store.persist_agent_runtime_session_checkpoint(task_id="task-1", agent_state=agent_state)

    assert checkpoint is not None
    session.add.assert_called_once()
    stored = session.add.call_args.args[0]
    assert stored.task_id == "task-1"
    assert stored.agent_id == "agent-1"
    assert stored.checkpoint_name == "runtime_session_state"
    assert stored.checkpoint_metadata["runtime_session_ref"]["session_key"] == "legacy:task-1:agent-1"
    assert stored.checkpoint_metadata["runtime_session_state"]["permission_mode"] == "plan"
    session.commit.assert_awaited_once()
    session.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_session_checkpoint_store_restores_runtime_snapshot_back_into_agent_state():
    checkpoint = SimpleNamespace(
        id="cp-restore-1",
        checkpoint_metadata={
            "runtime_session_ref": {
                "session_key": "legacy:task-1:agent-1",
                "session_id": "agent-1",
                "task_id": "task-1",
                "source": "legacy",
            },
            "runtime_session_state": {
                "session_id": "agent-1",
                "permission_mode": "plan",
                "pending_questions": [{"id": "question-1", "question": "Need approval?"}],
                "metadata": {
                    "plan_mode": {"active": True},
                    "todos": {"todo-1": {"id": "todo-1", "title": "Review auth flow"}},
                    "questions": {"question-1": {"id": "question-1", "question": "Need approval?"}},
                    "permission_rules": {"mutating_probe": {"mode": "ask"}},
                    "session_hooks": {"code-audit-finding": {"PostToolUse": [{"matcher": "*", "hooks": ["log-post"]}]}},
                    "memory_runtime": {
                        "base_system_prompt": "Base prompt",
                        "instructions": [{
                            "memory_kind": "instruction",
                            "title": "Project rule",
                            "source_type": "project_memory",
                            "source_ref": "CLAUDE.md",
                            "content": "Focus auth flows.",
                            "metadata": {"scope": "project"}
                        }],
                        "recalls": []
                    }
                },
                "agent_states": {
                    "recon": {
                        "agent_type": "recon",
                        "pending_todos": [{"id": "todo-1", "title": "Review auth flow"}],
                        "pending_questions": [{"id": "question-1", "question": "Need approval?"}],
                        "metadata": {
                            "tool_runtime": {
                                "records": [{"tool_name": "TodoWrite", "status": "completed"}],
                                "events": [],
                                "hook_records": [],
                                "checkpoints": []
                            }
                        }
                    }
                }
            },
        },
    )

    class _Result:
        def scalars(self):
            return self
        def first(self):
            return checkpoint

    session = SimpleNamespace(execute=AsyncMock(return_value=_Result()))

    class _SessionFactory:
        def __call__(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    return session
                async def __aexit__(self_inner, exc_type, exc, tb):
                    return False
            return _Ctx()

    store = RuntimeSessionCheckpointStore(session_factory=_SessionFactory())
    agent_state = SimpleNamespace(
        agent_id="agent-1",
        agent_name="Recon",
        agent_type="recon",
        parent_id=None,
        iteration=0,
        status="created",
        total_tokens=0,
        tool_calls=0,
        findings=[],
        task_context={},
        metadata={},
    )

    restored = await store.restore_agent_runtime_session_checkpoint(task_id="task-1", agent_state=agent_state)

    assert restored is not None
    assert restored["checkpoint_id"] == "cp-restore-1"
    assert agent_state.metadata["interaction_runtime"]["permission_mode"] == "plan"
    assert agent_state.metadata["interaction_runtime"]["permission_rules"]["mutating_probe"]["mode"] == "ask"
    assert agent_state.metadata["tool_runtime"]["session_hooks"]["code-audit-finding"]["PostToolUse"][0]["hooks"] == ["log-post"]
    assert agent_state.metadata["memory_runtime"]["instructions"][0]["source_ref"] == "CLAUDE.md"
    assert agent_state.metadata["runtime_session_ref"]["session_key"] == "legacy:task-1:agent-1"
