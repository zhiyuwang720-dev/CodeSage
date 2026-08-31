from __future__ import annotations

import asyncio

from app.services.runtime_core.hook_runtime import HookExecutorRuntime


class FakeHookRunner:
    def __init__(self, *, stdout: str = "", stderr: str = "", exit_code: int = 0, duration_ms: int = 25, progress_updates: list[dict[str, str]] | None = None):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.duration_ms = duration_ms
        self.progress_updates = list(progress_updates or [])
        self.calls: list[dict[str, object]] = []

    async def run(self, *, hook_id: str, hook_name: str, hook_event: str, command: str, prompt_text: str | None = None, timeout_ms: int | None = None) -> dict[str, object]:
        self.calls.append(
            {
                "hook_id": hook_id,
                "hook_name": hook_name,
                "hook_event": hook_event,
                "command": command,
                "prompt_text": prompt_text,
                "timeout_ms": timeout_ms,
            }
        )
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "progress_updates": list(self.progress_updates),
        }


def test_hook_executor_runtime_captures_started_progress_and_response_events():
    runner = FakeHookRunner(
        stdout='{"decision":"block","reason":"Need more evidence.","continue":true}\n',
        progress_updates=[{"stdout": "checking hook\n", "stderr": ""}],
        duration_ms=42,
    )
    runtime = HookExecutorRuntime(command_runner=runner)

    result = asyncio.run(
        runtime.execute_event_hooks(
            {
                "event": "TaskCompleted",
                "agent_name": "alice",
                "matched_hooks": [
                    {
                        "command": "python hooks/check.py",
                        "prompt_text": "Decide whether alice can conclude the task.",
                        "timeout_ms": 5000,
                    }
                ],
            }
        )
    )

    assert runner.calls[0]["command"] == "python hooks/check.py"
    assert result["blocking_errors"] == ["Need more evidence."]
    assert result["prevent_continuation"] is False
    assert [item["type"] for item in result["hook_execution_events"]] == ["started", "progress", "response"]
    assert result["hook_runs"][0]["durationMs"] == 42
    assert result["hook_runs"][0]["response"]["decision"] == "block"


def test_hook_executor_runtime_uses_response_payload_to_prevent_continuation():
    runner = FakeHookRunner(
        stdout='{"continue":false,"stopReason":"Hook requested handoff.","systemMessage":"handoff"}\n',
        stderr="",
        exit_code=0,
        duration_ms=17,
    )
    runtime = HookExecutorRuntime(command_runner=runner)

    result = asyncio.run(
        runtime.execute_event_hooks(
            {
                "event": "TeammateIdle",
                "agent_name": "alice",
                "matched_hooks": [
                    {
                        "command": "python hooks/idle.py",
                    }
                ],
            }
        )
    )

    assert result["blocking_errors"] == []
    assert result["prevent_continuation"] is True
    assert result["stop_reason"] == "Hook requested handoff."
    assert result["hook_runs"][0]["outcome"] == "success"
