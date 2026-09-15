from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.bootstrap.handler_registry import RunHandlerRegistry, UnsupportedRunHandler
from app.contracts.platform import InputArtifactRef, RunCommand


def _command(**updates) -> RunCommand:
    payload = {
        "task_id": str(uuid4()),
        "agent_type": "pr_review",
        "entrypoint": "quick_review",
        "agent_version": "1",
        "delivery_id": str(uuid4()),
        "input_ref": {
            "artifact_id": "input-manifest",
            "sha256": "a" * 64,
            "size_bytes": 42,
            "media_type": "application/json",
        },
        "deadline_at": datetime.now(timezone.utc),
        "grant_ref": "grant-pr-review",
        "trace_carrier": {"traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01"},
    }
    payload.update(updates)
    return RunCommand.model_validate(payload)


def test_22a_t03_run_command_is_strict_and_has_no_local_locator():
    command = _command()
    assert RunCommand.model_validate_json(command.model_dump_json()) == command
    assert "relative_path" not in command.input_ref.model_dump()
    with pytest.raises(ValidationError):
        _command(secret="leak")
    with pytest.raises(ValidationError):
        _command(trace_carrier={"authorization": "secret"})
    with pytest.raises(ValidationError):
        _command(entrypoint="app.module:arbitrary_handler")
    with pytest.raises(ValidationError):
        InputArtifactRef.model_validate({
            "artifact_id": "x", "sha256": "a" * 64, "size_bytes": 1,
            "media_type": "text/plain", "relative_path": "C:/secret",
        })


@pytest.mark.asyncio
async def test_22a_t03_registry_rejects_unknown_agent_entrypoint_and_version():
    registry = RunHandlerRegistry()

    async def handler(command: RunCommand) -> str:
        return command.task_id

    registry.register(agent_type="pr_review", entrypoint="quick_review", version="1", handler=handler)
    command = _command()
    assert await registry.resolve(command)(command) == command.task_id
    for update in (
        {"agent_type": "unknown"},
        {"entrypoint": "other"},
        {"agent_version": "2"},
    ):
        with pytest.raises(UnsupportedRunHandler):
            registry.resolve(_command(**update))
