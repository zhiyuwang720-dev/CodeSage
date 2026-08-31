from __future__ import annotations

import asyncio

from app.services.agent.tools.base import AgentTool, ToolResult
from app.services.review_runtime.session_store import AuditSessionStore
from app.db.base import Base
from app.services.runtime_core.runtime_tool_registry import build_runtime_tool_registry
from app.services.runtime_core.runtime_guardrails import register_shell_approval
from app.services.runtime_core.shell_runtime_tools import (
    BashRuntimeTool,
    BashToolInput,
    PowerShellRuntimeTool,
    PowerShellToolInput,
)
from app.services.runtime_core.tool_runtime import ToolExecutionContext
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class FakeExecAgentTool(AgentTool):
    def __init__(self, name: str = "sandbox_exec"):
        super().__init__()
        self._name = name
        self.calls: list[dict] = []
        self.project_root = "D:/repo"

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Tool {self._name}"

    async def _execute(self, **kwargs):
        self.calls.append(dict(kwargs))
        return ToolResult(success=True, data={"stdout": "ok", "stderr": "", "exit_code": 0}, metadata={"backend": self._name})


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(session_id="session-1", turn_id="turn-1", tool_use_id="tool-use-1", tool_call_id="tool-call-1")


def _store() -> AuditSessionStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return AuditSessionStore(session_factory=sessionmaker(bind=engine))


def test_bash_runtime_tool_matches_restored_style_metadata_and_safety_flags():
    tool = BashRuntimeTool(project_root="D:/repo")

    assert tool.name == "Bash"
    assert tool.search_hint == "execute shell commands"
    assert tool.user_facing_name({}) == "Bash"
    assert tool.is_read_only(BashToolInput(command="ls -la")) is True
    assert tool.is_concurrency_safe(BashToolInput(command="ls -la")) is True
    assert tool.is_read_only(BashToolInput(command="grep token app.py")) is True
    assert tool.is_read_only(BashToolInput(command="python -c 'print(1)'")) is False
    assert tool.is_read_only(BashToolInput(command="echo hi > out.txt")) is False
    assert tool.is_destructive(BashToolInput(command="rm -rf build")) is True
    assert tool.is_destructive(BashToolInput(command="sed -i 's/a/b/' app.py")) is True
    assert tool.interrupt_behavior() == "block"

    denied_background = asyncio.run(tool.check_permission(BashToolInput(command="ls", run_in_background=True), _context()))
    denied_sandbox = asyncio.run(tool.check_permission(BashToolInput(command="ls", dangerouslyDisableSandbox=True), _context()))
    allowed_destructive = asyncio.run(tool.check_permission(BashToolInput(command="rm -rf build"), _context()))

    assert denied_background.allowed is False
    assert "background" in str(denied_background.reason).lower()
    assert denied_sandbox.allowed is False
    assert "sandbox" in str(denied_sandbox.reason).lower()
    assert allowed_destructive.allowed is True


def test_bash_runtime_tool_executes_with_backend_tool_when_present(monkeypatch):
    monkeypatch.setattr("app.services.runtime_core.shell_runtime_tools.detect_bash_executable", lambda: None)
    backend = FakeExecAgentTool()
    tool = BashRuntimeTool(project_root="D:/repo", backend_tool=backend, executable=None)

    payload = asyncio.run(tool.execute(BashToolInput(command="ls -la", timeout=9_000), _context()))

    assert backend.calls == [{"command": "ls -la", "timeout": 9}]
    assert payload.is_error is False
    assert payload.output_payload["shell"] == "bash"
    assert payload.output_payload["command"] == "ls -la"
    assert payload.metadata["backend"] == "sandbox_exec"


def test_powershell_runtime_tool_matches_restored_style_metadata_and_safety_flags():
    tool = PowerShellRuntimeTool(project_root="D:/repo")

    assert tool.name == "PowerShell"
    assert tool.search_hint == "execute Windows PowerShell commands"
    assert tool.user_facing_name({}) == "PowerShell"
    assert tool.is_read_only(PowerShellToolInput(command="Get-ChildItem -Force")) is True
    assert tool.is_concurrency_safe(PowerShellToolInput(command="Get-ChildItem -Force")) is True
    assert tool.is_read_only(PowerShellToolInput(command="Get-Content README.md")) is True
    assert tool.is_read_only(PowerShellToolInput(command="Set-Content README.md hello")) is False
    assert tool.is_destructive(PowerShellToolInput(command="Remove-Item README.md -Force")) is True
    assert tool.is_destructive(PowerShellToolInput(command="Get-Content README.md > out.txt")) is True

    denied_background = asyncio.run(tool.check_permission(PowerShellToolInput(command="Get-ChildItem", run_in_background=True), _context()))
    denied_sandbox = asyncio.run(tool.check_permission(PowerShellToolInput(command="Get-ChildItem", dangerouslyDisableSandbox=True), _context()))
    allowed_destructive = asyncio.run(tool.check_permission(PowerShellToolInput(command="Remove-Item README.md -Force"), _context()))

    assert denied_background.allowed is False
    assert denied_sandbox.allowed is False
    assert allowed_destructive.allowed is True


def test_runtime_tool_registry_adds_shell_tools_when_shell_backends_are_available(monkeypatch):
    sandbox_exec = FakeExecAgentTool("sandbox_exec")
    read_tool = FakeExecAgentTool("read_file")
    list_tool = FakeExecAgentTool("list_files")
    search_tool = FakeExecAgentTool("search_code")

    monkeypatch.setattr("app.services.runtime_core.runtime_tool_registry.detect_bash_executable", lambda: None)
    monkeypatch.setattr("app.services.runtime_core.runtime_tool_registry.detect_powershell_executable", lambda: "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    monkeypatch.setattr("app.services.runtime_core.runtime_tool_registry.is_powershell_runtime_tool_enabled", lambda: True)

    registry = build_runtime_tool_registry(
        session_store=object(),
        agent_tools={
            "read_file": read_tool,
            "list_files": list_tool,
            "search_code": search_tool,
            "sandbox_exec": sandbox_exec,
        },
        agent_type="finding",
        user_id=None,
    )

    tool_names = [item["name"] for item in registry.describe_tools()]

    assert "Bash" in tool_names
    assert "PowerShell" in tool_names


def test_bash_runtime_tool_requires_approval_for_mutating_commands_when_guardrails_are_enabled(monkeypatch):
    monkeypatch.setattr("app.services.runtime_core.shell_runtime_tools.detect_bash_executable", lambda: None)
    store = _store()
    session_id = store.create_session(project_id="project-1")
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.metadata["guardrails"] = {"enabled": True}
    store.replace_runtime_state(session_id, runtime_state)
    tool = BashRuntimeTool(project_root="D:/repo", backend_tool=FakeExecAgentTool(), executable=None, session_store=store)

    decision = asyncio.run(
        tool.check_permission(
            BashToolInput(command="python -c \"open('out.txt','w').write('x')\""),
            ToolExecutionContext(session_id=session_id, turn_id="turn-1", tool_use_id="tool-use-1", tool_call_id="tool-call-1"),
        )
    )

    assert decision.allowed is False
    assert decision.mode == "ask"
    assert decision.guardrail_code == "shell_command_requires_approval"


def test_bash_runtime_tool_allows_session_approved_mutating_command(monkeypatch):
    monkeypatch.setattr("app.services.runtime_core.shell_runtime_tools.detect_bash_executable", lambda: None)
    store = _store()
    session_id = store.create_session(project_id="project-1")
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.metadata["guardrails"] = {"enabled": True}
    register_shell_approval(
        runtime_state,
        tool_name="Bash",
        command="python -c \"open('out.txt','w').write('x')\"",
        guardrail_code="shell_command_requires_approval",
    )
    store.replace_runtime_state(session_id, runtime_state)
    tool = BashRuntimeTool(project_root="D:/repo", backend_tool=FakeExecAgentTool(), executable=None, session_store=store)

    decision = asyncio.run(
        tool.check_permission(
            BashToolInput(command="python -c \"open('out.txt','w').write('x')\""),
            ToolExecutionContext(session_id=session_id, turn_id="turn-1", tool_use_id="tool-use-1", tool_call_id="tool-call-1"),
        )
    )

    assert decision.allowed is True


def test_bash_runtime_tool_requires_approval_for_destructive_commands_when_guardrails_are_enabled(monkeypatch):
    monkeypatch.setattr("app.services.runtime_core.shell_runtime_tools.detect_bash_executable", lambda: None)
    store = _store()
    session_id = store.create_session(project_id="project-1")
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.metadata["guardrails"] = {"enabled": True}
    store.replace_runtime_state(session_id, runtime_state)
    tool = BashRuntimeTool(project_root="D:/repo", backend_tool=FakeExecAgentTool(), executable=None, session_store=store)

    decision = asyncio.run(
        tool.check_permission(
            BashToolInput(command="rm -rf build"),
            ToolExecutionContext(session_id=session_id, turn_id="turn-1", tool_use_id="tool-use-1", tool_call_id="tool-call-1"),
        )
    )

    assert decision.allowed is False
    assert decision.mode == "ask"
    assert decision.guardrail_code == "shell_destructive_command_requires_approval"


def test_bash_runtime_tool_allows_destructive_commands_when_guardrails_are_disabled(monkeypatch):
    monkeypatch.setattr("app.services.runtime_core.shell_runtime_tools.detect_bash_executable", lambda: None)
    store = _store()
    session_id = store.create_session(project_id="project-1")
    tool = BashRuntimeTool(project_root="D:/repo", backend_tool=FakeExecAgentTool(), executable=None, session_store=store)

    decision = asyncio.run(
        tool.check_permission(
            BashToolInput(command="rm -rf build"),
            ToolExecutionContext(session_id=session_id, turn_id="turn-1", tool_use_id="tool-use-1", tool_call_id="tool-call-1"),
        )
    )

    assert decision.allowed is True


def test_bash_runtime_tool_requires_approval_for_commands_targeting_paths_outside_project_root(monkeypatch):
    monkeypatch.setattr("app.services.runtime_core.shell_runtime_tools.detect_bash_executable", lambda: None)
    store = _store()
    session_id = store.create_session(project_id="project-1")
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.metadata["guardrails"] = {"enabled": True}
    store.replace_runtime_state(session_id, runtime_state)
    tool = BashRuntimeTool(project_root="D:/repo", backend_tool=FakeExecAgentTool(), executable=None, session_store=store)

    decision = asyncio.run(
        tool.check_permission(
            BashToolInput(command="python audit.py ../other-project/output.json"),
            ToolExecutionContext(session_id=session_id, turn_id="turn-1", tool_use_id="tool-use-1", tool_call_id="tool-call-1"),
        )
    )

    assert decision.allowed is False
    assert decision.mode == "ask"
    assert decision.guardrail_code == "shell_outside_project_root_requires_approval"


def test_bash_runtime_tool_consumes_single_use_shell_approval_after_first_execution(monkeypatch):
    monkeypatch.setattr("app.services.runtime_core.shell_runtime_tools.detect_bash_executable", lambda: None)
    store = _store()
    session_id = store.create_session(project_id="project-1")
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.metadata["guardrails"] = {"enabled": True}
    register_shell_approval(
        runtime_state,
        tool_name="Bash",
        command="python -c \"open('out.txt','w').write('x')\"",
        guardrail_code="shell_command_requires_approval",
        scope="single_use",
    )
    store.replace_runtime_state(session_id, runtime_state)
    backend = FakeExecAgentTool()
    tool = BashRuntimeTool(project_root="D:/repo", backend_tool=backend, executable=None, session_store=store)
    context = ToolExecutionContext(session_id=session_id, turn_id="turn-1", tool_use_id="tool-use-1", tool_call_id="tool-call-1")

    first_decision = asyncio.run(
        tool.check_permission(
            BashToolInput(command="python -c \"open('out.txt','w').write('x')\""),
            context,
        )
    )
    first_payload = asyncio.run(
        tool.execute(
            BashToolInput(command="python -c \"open('out.txt','w').write('x')\""),
            context,
        )
    )
    persisted_state = store.load_runtime_state(session_id)
    second_decision = asyncio.run(
        tool.check_permission(
            BashToolInput(command="python -c \"open('out.txt','w').write('x')\""),
            ToolExecutionContext(session_id=session_id, turn_id="turn-2", tool_use_id="tool-use-2", tool_call_id="tool-call-2"),
        )
    )

    assert first_decision.allowed is True
    assert first_payload.output_payload["command"] == "python -c \"open('out.txt','w').write('x')\""
    assert persisted_state.metadata["shell_approvals"][0]["scope"] == "single_use"
    assert persisted_state.metadata["shell_approvals"][0].get("consumed_at")
    assert second_decision.allowed is False
    assert second_decision.mode == "ask"
    assert second_decision.guardrail_code == "shell_command_requires_approval"


def test_bash_runtime_tool_keeps_session_scope_shell_approval_reusable(monkeypatch):
    monkeypatch.setattr("app.services.runtime_core.shell_runtime_tools.detect_bash_executable", lambda: None)
    store = _store()
    session_id = store.create_session(project_id="project-1")
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.metadata["guardrails"] = {"enabled": True}
    register_shell_approval(
        runtime_state,
        tool_name="Bash",
        command="python -c \"open('out.txt','w').write('x')\"",
        guardrail_code="shell_command_requires_approval",
        scope="session",
    )
    store.replace_runtime_state(session_id, runtime_state)
    backend = FakeExecAgentTool()
    tool = BashRuntimeTool(project_root="D:/repo", backend_tool=backend, executable=None, session_store=store)

    first_decision = asyncio.run(
        tool.check_permission(
            BashToolInput(command="python -c \"open('out.txt','w').write('x')\""),
            ToolExecutionContext(session_id=session_id, turn_id="turn-1", tool_use_id="tool-use-1", tool_call_id="tool-call-1"),
        )
    )
    asyncio.run(
        tool.execute(
            BashToolInput(command="python -c \"open('out.txt','w').write('x')\""),
            ToolExecutionContext(session_id=session_id, turn_id="turn-1", tool_use_id="tool-use-1", tool_call_id="tool-call-1"),
        )
    )
    second_decision = asyncio.run(
        tool.check_permission(
            BashToolInput(command="python -c \"open('out.txt','w').write('x')\""),
            ToolExecutionContext(session_id=session_id, turn_id="turn-2", tool_use_id="tool-use-2", tool_call_id="tool-call-2"),
        )
    )
    persisted_state = store.load_runtime_state(session_id)

    assert first_decision.allowed is True
    assert second_decision.allowed is True
    assert persisted_state.metadata["shell_approvals"][0]["scope"] == "session"
    assert persisted_state.metadata["shell_approvals"][0].get("consumed_at") is None
