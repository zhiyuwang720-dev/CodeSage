"""Assemble a fully-wired AgentLoop from config (the composition root).

Phase 06's AgentLoop is dependency-injected; this is where the CLI wires
everything together: settings → LLM client + registry + permission engine +
audit sink + session.
"""

from __future__ import annotations

from pathlib import Path

from ..ai import LLMClient
from ..config import GlobalConfig, load_settings, paths
from ..core import Session
from ..engine import AgentLoop, build_context_bundle
from ..permissions import JsonlAuditSink, PermissionEngine
from ..permissions.store import load_permission_rules
from ..tools import ToolRegistry, get_builtin_tools
from .base_prompt import get_base_prompt


def session_root() -> Path:
    from ..config import paths

    return paths.config_dir() / "sessions"


def build_loop(
    *,
    cwd: Path,
    mode: str = "default",
    model: str = "main",
    max_turns: int = 100,
    max_budget_usd: float | None = None,
    request_permission=None,
    vcr_mode: str | None = None,
    session_id: str | None = None,
    system_prompt: str | None = None,
    project_key: str | None = None,
    session: Session | None = None,  # existing session (--continue); else new
    history: list | None = None,  # prior turns as context (--continue)
) -> AgentLoop:
    settings = load_settings(project_dir=cwd)
    client = LLMClient(project_dir=str(cwd), vcr_mode=vcr_mode)
    audit = JsonlAuditSink(paths.config_dir() / "audit.jsonl")
    permissions = PermissionEngine(audit_sink=audit)
    registry = ToolRegistry(get_builtin_tools())
    if session is None:
        session = Session(session_id or _new_session_id(), session_root(), project_key=project_key)

    return AgentLoop(
        client=client,
        tools=registry,
        permissions=permissions,
        request_permission=request_permission,
        system_prompt=system_prompt if system_prompt is not None else get_base_prompt(str(cwd)),
        context_bundle=build_context_bundle(cwd),  # memoize: once per session (S4)
        model=model,
        mode=mode,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        cwd=cwd,
        session=session,
        settings=settings,
        history=history,
    )


def _new_session_id() -> str:
    from datetime import datetime

    # microsecond precision: same-second sessions must not collide
    return datetime.now().strftime("session-%Y%m%d-%H%M%S-%f")


def apply_tool_filter(loop: AgentLoop, allowed: str | None, disallowed: str | None) -> None:
    """Restrict the loop's registry: keep only --allowedTools, drop --disallowedTools (comma-separated).

    The model sees only the surviving specs, so a removed tool reads as
    "Unknown tool" — the precise surface-control path for unattended runs.
    """
    if not allowed and not disallowed:
        return
    tools = loop.tools.all()
    if allowed:
        keep = {name.strip() for name in allowed.split(",") if name.strip()}
        tools = [t for t in tools if t.name in keep]
    if disallowed:
        drop = {name.strip() for name in disallowed.split(",") if name.strip()}
        tools = [t for t in tools if t.name not in drop]
    loop.tools = ToolRegistry(tools)
