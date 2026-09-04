from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.services.agent.tools.base import AgentTool
from app.services.contracts.models import ToolExecutionPayload
from app.services.runtime_core.permission_runtime import ToolPermissionDecision
from app.services.runtime_core.runtime_guardrails import (
    APPROVAL_SCOPE_SINGLE_USE,
    consume_shell_approval,
    has_shell_approval,
    is_guardrails_enabled,
)
from app.services.runtime_core.tool_runtime import RuntimeTool, ToolExecutionContext

DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 600_000

BASH_SEARCH_COMMANDS = {
    "find",
    "grep",
    "rg",
    "ag",
    "ack",
    "locate",
    "which",
    "whereis",
}
BASH_READ_COMMANDS = {
    "cat",
    "head",
    "tail",
    "less",
    "more",
    "wc",
    "stat",
    "file",
    "strings",
    "jq",
    "awk",
    "cut",
    "sort",
    "uniq",
    "tr",
    "md5sum",
    "sha256sum",
    "pwd",
    "env",
    "printenv",
    "id",
    "whoami",
    "uname",
    "hostname",
    "xxd",
    "od",
    "hexdump",
    "test",
}
BASH_LIST_COMMANDS = {"ls", "tree", "du"}
BASH_SEMANTIC_NEUTRAL_COMMANDS = {"echo", "printf", "true", "false", ":"}
BASH_MUTATING_COMMANDS = {
    "rm",
    "mv",
    "cp",
    "mkdir",
    "rmdir",
    "chmod",
    "chown",
    "chgrp",
    "touch",
    "ln",
    "install",
    "tee",
}
GIT_READ_ONLY_SUBCOMMANDS = {
    "status",
    "diff",
    "log",
    "show",
    "rev-parse",
    "ls-files",
    "grep",
    "branch",
    "remote",
    "config",
}
GIT_MUTATING_SUBCOMMANDS = {
    "add",
    "apply",
    "am",
    "bisect",
    "branch",
    "checkout",
    "cherry-pick",
    "clean",
    "clone",
    "commit",
    "fetch",
    "merge",
    "mv",
    "pull",
    "push",
    "rebase",
    "reset",
    "restore",
    "revert",
    "rm",
    "stash",
    "switch",
    "tag",
    "worktree",
}

POWERSHELL_ALIAS_MAP = {
    "ls": "get-childitem",
    "dir": "get-childitem",
    "gci": "get-childitem",
    "cat": "get-content",
    "gc": "get-content",
    "type": "get-content",
    "pwd": "get-location",
    "gl": "get-location",
    "cd": "set-location",
    "sl": "set-location",
    "sls": "select-string",
    "echo": "write-output",
    "write-host": "write-output",
    "rm": "remove-item",
    "del": "remove-item",
    "erase": "remove-item",
    "cp": "copy-item",
    "copy": "copy-item",
    "mv": "move-item",
    "move": "move-item",
    "ren": "rename-item",
    "type": "get-content",
    "ps": "get-process",
}
POWERSHELL_SEARCH_COMMANDS = {"select-string", "get-childitem", "findstr", "where.exe"}
POWERSHELL_READ_COMMANDS = {
    "get-content",
    "get-item",
    "get-itemproperty",
    "test-path",
    "resolve-path",
    "get-process",
    "get-service",
    "get-childitem",
    "get-location",
    "get-filehash",
    "get-acl",
    "format-hex",
    "set-location",
    "push-location",
    "pop-location",
}
POWERSHELL_NEUTRAL_COMMANDS = {"write-output", "write-host"}
POWERSHELL_MUTATING_COMMANDS = {
    "set-content",
    "add-content",
    "new-item",
    "remove-item",
    "move-item",
    "copy-item",
    "rename-item",
    "clear-content",
    "set-item",
    "out-file",
    "export-csv",
    "export-clixml",
    "invoke-expression",
    "iex",
    "start-process",
}


class BashToolInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    command: str = Field(description="The command to execute")
    timeout: int | None = Field(default=None, description=f"Optional timeout in milliseconds (max {MAX_TIMEOUT_MS})")
    description: str | None = Field(default=None, description="Optional concise description of what this command does")
    run_in_background: bool = Field(default=False, description="Set to true to run the command in the background")
    dangerously_disable_sandbox: bool = Field(default=False, alias="dangerouslyDisableSandbox", serialization_alias="dangerouslyDisableSandbox")


class PowerShellToolInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    command: str = Field(description="The PowerShell command to execute")
    timeout: int | None = Field(default=None, description=f"Optional timeout in milliseconds (max {MAX_TIMEOUT_MS})")
    description: str | None = Field(default=None, description="Optional concise description of what this command does")
    run_in_background: bool = Field(default=False, description="Set to true to run the command in the background")
    dangerously_disable_sandbox: bool = Field(default=False, alias="dangerouslyDisableSandbox", serialization_alias="dangerouslyDisableSandbox")


def detect_bash_executable() -> str | None:
    """在 PATH 里找一个可用的 bash 解释器。

    Windows 上必须跳过 `C:\\Windows\\System32\\bash.exe`(WSL 启动器): 命中它时子进程
    输出的是 UTF-16 的 "wsl: ..." 错误(乱码), 而非可用 shell。找不到非 WSL 的 bash
    时返回 None, 让 PowerShell 工具顶上(is_powershell_runtime_tool_enabled 在 nt 为 True)。
    """
    for candidate in ("bash", "bash.exe", "sh", "sh.exe"):
        resolved = shutil.which(candidate)
        if not resolved:
            continue
        resolved = os.path.realpath(resolved)
        if os.name == "nt":
            lower = resolved.lower()
            if lower.endswith(("bash.exe", "sh.exe")) and "\\system32\\" in lower:
                continue  # WSL 启动器, 不是真 bash
        return resolved
    return None


def detect_powershell_executable() -> str | None:
    for candidate in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def is_powershell_runtime_tool_enabled() -> bool:
    return os.name == "nt"


def _split_shell_segments(command: str) -> list[str]:
    return [segment.strip() for segment in re.split(r"\s*(?:\|\||&&|[|;])\s*", str(command or "")) if segment.strip()]


def _has_write_redirection(command: str) -> bool:
    return bool(re.search(r"(^|\s)(?:>>|>|1>|2>)(?![&|])", command))


def _tokenize(command: str) -> list[str]:
    return [token for token in re.split(r"\s+", str(command or "").strip()) if token]


def _bash_segment_is_read_only(segment: str) -> bool:
    tokens = _tokenize(segment)
    if not tokens:
        return False
    base = tokens[0]
    if base in BASH_SEMANTIC_NEUTRAL_COMMANDS:
        return True
    if base == "git":
        subcommand = tokens[1] if len(tokens) > 1 else ""
        if subcommand in GIT_MUTATING_SUBCOMMANDS:
            return False
        return subcommand in GIT_READ_ONLY_SUBCOMMANDS
    return base in BASH_SEARCH_COMMANDS or base in BASH_READ_COMMANDS or base in BASH_LIST_COMMANDS


def _bash_segment_is_destructive(segment: str) -> bool:
    tokens = _tokenize(segment)
    if not tokens:
        return False
    if _has_write_redirection(segment):
        return True
    base = tokens[0]
    if base in BASH_MUTATING_COMMANDS:
        return True
    if base == "sed" and any(token == "-i" or token.startswith("-i") for token in tokens[1:]):
        return True
    if base == "git":
        subcommand = tokens[1] if len(tokens) > 1 else ""
        return subcommand in GIT_MUTATING_SUBCOMMANDS
    return False


def _powershell_canonical(command_name: str) -> str:
    lowered = str(command_name or "").strip().lower()
    return POWERSHELL_ALIAS_MAP.get(lowered, lowered)


def _powershell_segments(command: str) -> list[str]:
    return [segment.strip() for segment in re.split(r"\s*(?:\|\||&&|[;|])\s*", str(command or "")) if segment.strip()]


def _powershell_segment_is_read_only(segment: str) -> bool:
    tokens = _tokenize(segment)
    if not tokens:
        return False
    base = _powershell_canonical(tokens[0])
    if base in POWERSHELL_NEUTRAL_COMMANDS:
        return True
    if base == "git":
        subcommand = tokens[1].lower() if len(tokens) > 1 else ""
        if subcommand in GIT_MUTATING_SUBCOMMANDS:
            return False
        return subcommand in GIT_READ_ONLY_SUBCOMMANDS
    return base in POWERSHELL_SEARCH_COMMANDS or base in POWERSHELL_READ_COMMANDS


def _powershell_segment_is_destructive(segment: str) -> bool:
    tokens = _tokenize(segment)
    if not tokens:
        return False
    if _has_write_redirection(segment):
        return True
    base = _powershell_canonical(tokens[0])
    if base in POWERSHELL_MUTATING_COMMANDS:
        return True
    if base == "git":
        subcommand = tokens[1].lower() if len(tokens) > 1 else ""
        return subcommand in GIT_MUTATING_SUBCOMMANDS
    return False


def _clamp_timeout_ms(raw_timeout: int | None) -> int:
    timeout_ms = raw_timeout or DEFAULT_TIMEOUT_MS
    timeout_ms = max(1_000, timeout_ms)
    return min(timeout_ms, MAX_TIMEOUT_MS)


def _command_targets_outside_project(command: str) -> bool:
    text = str(command or "").strip()
    if not text:
        return False
    return bool(
        re.search(r"(^|[\s'\"=])\.\.[/\\]", text)
        or re.search(r"(^|[\s'\"=])[A-Za-z]:[/\\]", text)
        or re.search(r"(^|[\s'\"=])/(?![/-])", text)
    )


def _shell_guardrail_payload(*, command: str, destructive: bool) -> tuple[str, str]:
    if _command_targets_outside_project(command):
        return (
            "shell_outside_project_root_requires_approval",
            "Running a shell command that references paths outside the project root requires explicit approval while guardrails are enabled.",
        )
    if destructive:
        return (
            "shell_destructive_command_requires_approval",
            "Running a destructive shell command requires explicit approval while guardrails are enabled.",
        )
    return (
        "shell_command_requires_approval",
        "Running a mutating shell command requires explicit approval while guardrails are enabled.",
    )


async def _run_local_command(*, executable: str, args: list[str], cwd: str, timeout_ms: int) -> ToolExecutionPayload:
    process = await asyncio.create_subprocess_exec(
        executable,
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_ms / 1000)
        timed_out = False
    except asyncio.TimeoutError:
        process.kill()
        stdout, stderr = await process.communicate()
        timed_out = True
    stdout_text = stdout.decode("utf-8", errors="ignore")
    stderr_text = stderr.decode("utf-8", errors="ignore")
    exit_code = -1 if timed_out else int(process.returncode or 0)
    content_parts = [f"exit_code: {exit_code}"]
    if stdout_text.strip():
        content_parts.append(f"stdout:\n{stdout_text.strip()}")
    if stderr_text.strip():
        content_parts.append(f"stderr:\n{stderr_text.strip()}")
    if timed_out:
        content_parts.append("error: command timed out")
    return ToolExecutionPayload(
        content="\n\n".join(content_parts),
        output_payload={
            "stdout": stdout_text,
            "stderr": stderr_text,
            "exit_code": exit_code,
            "timed_out": timed_out,
        },
        metadata={"execution_backend": "local_process"},
        is_error=timed_out or exit_code != 0,
    )


class BashRuntimeTool(RuntimeTool):
    name = "Bash"
    description = "Run shell commands from the project workspace for inspection, search, git, and command-line analysis."
    input_model = BashToolInput
    search_hint = "execute shell commands"

    def __init__(
        self,
        *,
        project_root: str,
        backend_tool: AgentTool | None = None,
        executable: str | None = None,
        session_store=None,
    ):
        self._project_root = str(Path(project_root).resolve())
        self._backend_tool = backend_tool
        self._executable = executable or detect_bash_executable()
        self._session_store = session_store

    def is_concurrency_safe(self, parsed_input: BashToolInput) -> bool:
        return self.is_read_only(parsed_input)

    def is_read_only(self, parsed_input: BashToolInput) -> bool:
        command = str(parsed_input.command or "").strip()
        if not command or _has_write_redirection(command):
            return False
        segments = _split_shell_segments(command)
        return bool(segments) and all(_bash_segment_is_read_only(segment) for segment in segments)

    def is_destructive(self, parsed_input: BashToolInput) -> bool:
        command = str(parsed_input.command or "").strip()
        if not command:
            return False
        return any(_bash_segment_is_destructive(segment) for segment in _split_shell_segments(command))

    async def check_permission(self, parsed_input: BashToolInput, context: ToolExecutionContext) -> ToolPermissionDecision:
        if not str(parsed_input.command or "").strip():
            return ToolPermissionDecision(allowed=False, reason="Bash requires a non-empty command")
        if parsed_input.run_in_background:
            return ToolPermissionDecision(allowed=False, reason="Background shell execution is not implemented in AutoCVE runtime yet")
        if parsed_input.dangerously_disable_sandbox:
            return ToolPermissionDecision(allowed=False, reason="Disabling sandbox execution is not supported by the runtime Bash tool")
        if self._executable is None and self._backend_tool is None:
            return ToolPermissionDecision(allowed=False, reason="No Bash execution backend is available for this runtime")
        if not self.is_read_only(parsed_input) and self._session_store is not None:
            runtime_state = self._session_store.load_runtime_state(context.session_id)
            if is_guardrails_enabled(runtime_state):
                guardrail_code, reason = _shell_guardrail_payload(
                    command=parsed_input.command,
                    destructive=self.is_destructive(parsed_input),
                )
                if not has_shell_approval(
                    runtime_state,
                    tool_name=self.name,
                    command=parsed_input.command,
                    guardrail_code=guardrail_code,
                ):
                    return ToolPermissionDecision(
                        allowed=False,
                        source="tool_guardrail",
                        mode="ask",
                        reason=reason,
                        guardrail_code=guardrail_code,
                    )
        return ToolPermissionDecision(allowed=True)

    async def execute(self, parsed_input: BashToolInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        if not self.is_read_only(parsed_input) and self._session_store is not None:
            runtime_state = self._session_store.load_runtime_state(context.session_id)
            if is_guardrails_enabled(runtime_state):
                guardrail_code, _reason = _shell_guardrail_payload(
                    command=parsed_input.command,
                    destructive=self.is_destructive(parsed_input),
                )
                approval = consume_shell_approval(
                    runtime_state,
                    tool_name=self.name,
                    command=parsed_input.command,
                    guardrail_code=guardrail_code,
                )
                if approval and str(approval.get("scope") or "") == APPROVAL_SCOPE_SINGLE_USE:
                    self._session_store.replace_runtime_state(context.session_id, runtime_state)
        timeout_ms = _clamp_timeout_ms(parsed_input.timeout)
        if self._executable is not None:
            payload = await _run_local_command(
                executable=self._executable,
                args=["-lc", parsed_input.command] if Path(self._executable).name.lower().startswith("bash") else ["-c", parsed_input.command],
                cwd=self._project_root,
                timeout_ms=timeout_ms,
            )
            payload.output_payload.update({"shell": "bash", "command": parsed_input.command, "cwd": self._project_root})
            return payload
        if self._backend_tool is None:
            raise ValueError("No Bash execution backend is available")
        timeout_seconds = max(1, math.ceil(timeout_ms / 1000))
        result = await self._backend_tool.execute(command=parsed_input.command, timeout=timeout_seconds)
        output_payload = result.to_dict()
        output_payload.update({"shell": "bash", "command": parsed_input.command, "cwd": self._project_root})
        return ToolExecutionPayload(
            content=result.to_string(),
            output_payload=output_payload,
            metadata={"backend": self._backend_tool.name, **(result.metadata or {})},
            is_error=not result.success,
        )


class PowerShellRuntimeTool(RuntimeTool):
    name = "PowerShell"
    description = "Run Windows PowerShell commands from the project workspace for inspection, search, git, and command-line analysis."
    input_model = PowerShellToolInput
    search_hint = "execute Windows PowerShell commands"

    def __init__(
        self,
        *,
        project_root: str,
        backend_tool: AgentTool | None = None,
        executable: str | None = None,
        session_store=None,
    ):
        self._project_root = str(Path(project_root).resolve())
        self._backend_tool = backend_tool
        self._executable = executable or detect_powershell_executable()
        self._session_store = session_store

    def is_concurrency_safe(self, parsed_input: PowerShellToolInput) -> bool:
        return self.is_read_only(parsed_input)

    def is_read_only(self, parsed_input: PowerShellToolInput) -> bool:
        command = str(parsed_input.command or "").strip()
        if not command or _has_write_redirection(command):
            return False
        segments = _powershell_segments(command)
        return bool(segments) and all(_powershell_segment_is_read_only(segment) for segment in segments)

    def is_destructive(self, parsed_input: PowerShellToolInput) -> bool:
        command = str(parsed_input.command or "").strip()
        if not command:
            return False
        return any(_powershell_segment_is_destructive(segment) for segment in _powershell_segments(command))

    async def check_permission(self, parsed_input: PowerShellToolInput, context: ToolExecutionContext) -> ToolPermissionDecision:
        if not str(parsed_input.command or "").strip():
            return ToolPermissionDecision(allowed=False, reason="PowerShell requires a non-empty command")
        if parsed_input.run_in_background:
            return ToolPermissionDecision(allowed=False, reason="Background shell execution is not implemented in AutoCVE runtime yet")
        if parsed_input.dangerously_disable_sandbox:
            return ToolPermissionDecision(allowed=False, reason="Disabling sandbox execution is not supported by the runtime PowerShell tool")
        if self._executable is None and self._backend_tool is None:
            return ToolPermissionDecision(allowed=False, reason="No PowerShell execution backend is available for this runtime")
        if not self.is_read_only(parsed_input) and self._session_store is not None:
            runtime_state = self._session_store.load_runtime_state(context.session_id)
            if is_guardrails_enabled(runtime_state):
                guardrail_code, reason = _shell_guardrail_payload(
                    command=parsed_input.command,
                    destructive=self.is_destructive(parsed_input),
                )
                if not has_shell_approval(
                    runtime_state,
                    tool_name=self.name,
                    command=parsed_input.command,
                    guardrail_code=guardrail_code,
                ):
                    return ToolPermissionDecision(
                        allowed=False,
                        source="tool_guardrail",
                        mode="ask",
                        reason=reason.replace("shell command", "PowerShell command"),
                        guardrail_code=guardrail_code,
                    )
        return ToolPermissionDecision(allowed=True)

    async def execute(self, parsed_input: PowerShellToolInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        if not self.is_read_only(parsed_input) and self._session_store is not None:
            runtime_state = self._session_store.load_runtime_state(context.session_id)
            if is_guardrails_enabled(runtime_state):
                guardrail_code, _reason = _shell_guardrail_payload(
                    command=parsed_input.command,
                    destructive=self.is_destructive(parsed_input),
                )
                approval = consume_shell_approval(
                    runtime_state,
                    tool_name=self.name,
                    command=parsed_input.command,
                    guardrail_code=guardrail_code,
                )
                if approval and str(approval.get("scope") or "") == APPROVAL_SCOPE_SINGLE_USE:
                    self._session_store.replace_runtime_state(context.session_id, runtime_state)
        timeout_ms = _clamp_timeout_ms(parsed_input.timeout)
        if self._executable is not None:
            payload = await _run_local_command(
                executable=self._executable,
                args=["-NoProfile", "-Command", parsed_input.command],
                cwd=self._project_root,
                timeout_ms=timeout_ms,
            )
            payload.output_payload.update({"shell": "powershell", "command": parsed_input.command, "cwd": self._project_root})
            return payload
        if self._backend_tool is None:
            raise ValueError("No PowerShell execution backend is available")
        timeout_seconds = max(1, math.ceil(timeout_ms / 1000))
        result = await self._backend_tool.execute(command=parsed_input.command, timeout=timeout_seconds)
        output_payload = result.to_dict()
        output_payload.update({"shell": "powershell", "command": parsed_input.command, "cwd": self._project_root})
        return ToolExecutionPayload(
            content=result.to_string(),
            output_payload=output_payload,
            metadata={"backend": self._backend_tool.name, **(result.metadata or {})},
            is_error=not result.success,
        )
