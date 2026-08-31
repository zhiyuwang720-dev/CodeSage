from __future__ import annotations

import asyncio
import json
from time import perf_counter
from typing import Any


class SubprocessHookCommandRunner:
    async def run(
        self,
        *,
        hook_id: str,
        hook_name: str,
        hook_event: str,
        command: str,
        prompt_text: str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, object]:
        del hook_id, hook_name, hook_event, prompt_text
        started = perf_counter()
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        progress_updates: list[dict[str, str]] = []

        async def _pump(stream, target: list[str], stream_name: str) -> None:
            while True:
                chunk = await stream.read(1024)
                if not chunk:
                    return
                text = chunk.decode("utf-8", errors="replace")
                target.append(text)
                progress_updates.append(
                    {
                        "stdout": "".join(stdout_chunks),
                        "stderr": "".join(stderr_chunks),
                        "output": "".join(stdout_chunks) + "".join(stderr_chunks),
                        "stream": stream_name,
                    }
                )

        stdout_task = asyncio.create_task(_pump(process.stdout, stdout_chunks, "stdout"))
        stderr_task = asyncio.create_task(_pump(process.stderr, stderr_chunks, "stderr"))
        timed_out = False
        try:
            if timeout_ms is None:
                await process.wait()
            else:
                await asyncio.wait_for(process.wait(), timeout_ms / 1000)
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            await process.wait()
        finally:
            await asyncio.gather(stdout_task, stderr_task)

        duration_ms = max(0, int((perf_counter() - started) * 1000))
        exit_code = int(process.returncode if process.returncode is not None else (124 if timed_out else 1))
        outcome = "cancelled" if timed_out else ("success" if exit_code == 0 else "error")
        return {
            "stdout": "".join(stdout_chunks),
            "stderr": "".join(stderr_chunks),
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "outcome": outcome,
            "progress_updates": progress_updates,
        }


class HookExecutorRuntime:
    def __init__(self, *, command_runner: Any | None = None):
        self._command_runner = command_runner or SubprocessHookCommandRunner()

    async def execute_event_hooks(self, event: dict[str, Any]) -> dict[str, Any]:
        executed = dict(event or {})
        matched_hooks = [dict(item) for item in (executed.get("matched_hooks") or []) if isinstance(item, dict)]
        hook_runs: list[dict[str, Any]] = []
        execution_events: list[dict[str, Any]] = []
        blocking_errors: list[str] = []
        additional_contexts: list[str] = []
        prevent_continuation = False
        stop_reason = str(executed.get("stop_reason") or "").strip() or None
        hook_event = str(executed.get("event") or "Stop").strip() or "Stop"

        for index, hook in enumerate(matched_hooks, start=1):
            hook_id = str(hook.get("id") or f"{hook_event.lower()}-{index}").strip()
            hook_name = str(hook.get("hook_name") or hook.get("name") or hook_event).strip() or hook_event
            command = str(hook.get("command") or hook.get("cmd") or "").strip() or None
            prompt_text = str(hook.get("prompt_text") or hook.get("promptText") or "").strip() or None
            timeout_ms = hook.get("timeout_ms") or hook.get("timeout")
            run: dict[str, Any]
            precomputed_output = any(
                hook.get(key) is not None
                for key in ("stdout", "stderr", "exit_code", "exitCode", "duration_ms", "durationMs", "content")
            )
            if command and not precomputed_output:
                execution_events.append(
                    {
                        "type": "started",
                        "hookId": hook_id,
                        "hookName": hook_name,
                        "hookEvent": hook_event,
                        "command": command,
                        "promptText": prompt_text,
                    }
                )
                result = await self._command_runner.run(
                    hook_id=hook_id,
                    hook_name=hook_name,
                    hook_event=hook_event,
                    command=command,
                    prompt_text=prompt_text,
                    timeout_ms=int(timeout_ms) if timeout_ms is not None else None,
                )
                stdout = str(result.get("stdout") or "")
                stderr = str(result.get("stderr") or "")
                for progress in result.get("progress_updates") or []:
                    execution_events.append(
                        {
                            "type": "progress",
                            "hookId": hook_id,
                            "hookName": hook_name,
                            "hookEvent": hook_event,
                            "stdout": str(progress.get("stdout") or ""),
                            "stderr": str(progress.get("stderr") or ""),
                            "output": str(progress.get("output") or ""),
                            "command": command,
                            "promptText": prompt_text,
                        }
                    )
                outcome = str(result.get("outcome") or ("success" if int(result.get("exit_code") or 0) == 0 else "error"))
                execution_events.append(
                    {
                        "type": "response",
                        "hookId": hook_id,
                        "hookName": hook_name,
                        "hookEvent": hook_event,
                        "stdout": stdout,
                        "stderr": stderr,
                        "output": stdout + stderr,
                        "exitCode": int(result.get("exit_code") or 0),
                        "outcome": outcome,
                        "command": command,
                        "promptText": prompt_text,
                    }
                )
                parsed_response = _parse_sync_hook_response(stdout)
                run = {
                    "index": index,
                    "hookId": hook_id,
                    "hookName": hook_name,
                    "command": command,
                    "promptText": prompt_text,
                    "stdout": stdout,
                    "stderr": stderr,
                    "exitCode": int(result.get("exit_code") or 0),
                    "durationMs": int(result.get("duration_ms") or 0),
                    "content": (stdout or stderr).strip() or None,
                    "outcome": outcome,
                    "response": parsed_response,
                }
            elif command and precomputed_output:
                stdout = str(hook.get("stdout") or "")
                stderr = str(hook.get("stderr") or "")
                exit_code = hook.get("exit_code") if hook.get("exit_code") is not None else hook.get("exitCode")
                duration_ms = hook.get("duration_ms") if hook.get("duration_ms") is not None else hook.get("durationMs")
                content = str(hook.get("content") or "").strip() or None
                if content is None and (stdout or stderr):
                    content = (stdout or stderr).strip() or None
                execution_events.append(
                    {
                        "type": "started",
                        "hookId": hook_id,
                        "hookName": hook_name,
                        "hookEvent": hook_event,
                        "command": command,
                        "promptText": prompt_text,
                    }
                )
                if stdout or stderr:
                    execution_events.append(
                        {
                            "type": "progress",
                            "hookId": hook_id,
                            "hookName": hook_name,
                            "hookEvent": hook_event,
                            "stdout": stdout,
                            "stderr": stderr,
                            "output": stdout + stderr,
                            "command": command,
                            "promptText": prompt_text,
                        }
                    )
                outcome = str(hook.get("outcome") or ("success" if exit_code in (None, 0) else "error"))
                execution_events.append(
                    {
                        "type": "response",
                        "hookId": hook_id,
                        "hookName": hook_name,
                        "hookEvent": hook_event,
                        "stdout": stdout,
                        "stderr": stderr,
                        "output": stdout + stderr,
                        "exitCode": int(exit_code) if exit_code is not None else None,
                        "outcome": outcome,
                        "command": command,
                        "promptText": prompt_text,
                    }
                )
                parsed_response = _parse_sync_hook_response(stdout)
                run = {
                    "index": index,
                    "hookId": hook_id,
                    "hookName": hook_name,
                    "command": command,
                    "promptText": prompt_text,
                    "stdout": stdout,
                    "stderr": stderr,
                    "exitCode": int(exit_code) if exit_code is not None else None,
                    "durationMs": int(duration_ms) if duration_ms is not None else None,
                    "content": content,
                    "outcome": outcome,
                    "response": parsed_response,
                }
            else:
                parsed_response = None
                run = {
                    "index": index,
                    "hookId": hook_id,
                    "hookName": hook_name,
                    "command": None,
                    "promptText": prompt_text,
                    "stdout": "",
                    "stderr": "",
                    "exitCode": None,
                    "durationMs": None,
                    "content": None,
                    "outcome": "success",
                    "response": None,
                }

            hook_runs.append(run)
            hook_effects = _derive_hook_effects(hook=hook, run=run)
            for error in hook_effects["blocking_errors"]:
                if error not in blocking_errors:
                    blocking_errors.append(error)
            for context in hook_effects["additional_contexts"]:
                if context not in additional_contexts:
                    additional_contexts.append(context)
            prevent_continuation = prevent_continuation or bool(hook_effects["prevent_continuation"])
            stop_reason = stop_reason or hook_effects["stop_reason"]

        executed["hook_runs"] = hook_runs
        executed["hook_execution_events"] = execution_events
        executed["blocking_errors"] = blocking_errors
        executed["additional_contexts"] = additional_contexts
        executed["prevent_continuation"] = prevent_continuation
        executed["stop_reason"] = stop_reason
        return executed


def _parse_sync_hook_response(stdout: str) -> dict[str, Any] | None:
    candidates = [line.strip() for line in str(stdout or "").splitlines() if line.strip().startswith("{")]
    if str(stdout or "").strip().startswith("{"):
        candidates.append(str(stdout or "").strip())
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict) and parsed.get("async") is not True:
            return parsed
    return None


def _derive_hook_effects(*, hook: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    parsed_response = run.get("response") if isinstance(run.get("response"), dict) else None
    blocking_errors: list[str] = []
    additional_contexts: list[str] = []
    prevent_continuation = False
    stop_reason: str | None = None

    if parsed_response:
        decision = str(parsed_response.get("decision") or "").strip().lower()
        reason = str(parsed_response.get("reason") or parsed_response.get("systemMessage") or "").strip()
        if decision == "block" and reason:
            blocking_errors.append(reason)
        if parsed_response.get("continue") is False:
            prevent_continuation = True
            stop_reason = str(parsed_response.get("stopReason") or reason or "").strip() or None
        hook_specific = parsed_response.get("hookSpecificOutput")
        if isinstance(hook_specific, dict):
            additional_context = str(hook_specific.get("additionalContext") or "").strip()
            if additional_context:
                additional_contexts.append(additional_context)

    static_blocking = str(hook.get("blocking_error") or hook.get("blockingError") or "").strip()
    if static_blocking and static_blocking not in blocking_errors:
        blocking_errors.append(static_blocking)
    if bool(hook.get("prevent_continuation") or hook.get("preventContinuation")):
        prevent_continuation = True
    static_stop_reason = str(hook.get("stop_reason") or hook.get("stopReason") or hook.get("message") or "").strip() or None
    stop_reason = stop_reason or static_stop_reason

    if not blocking_errors and run.get("exitCode") == 2:
        fallback_error = str(run.get("stderr") or run.get("stdout") or "").strip()
        if fallback_error:
            blocking_errors.append(fallback_error)

    return {
        "blocking_errors": blocking_errors,
        "additional_contexts": additional_contexts,
        "prevent_continuation": prevent_continuation,
        "stop_reason": stop_reason,
    }
