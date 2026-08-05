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
from ..engine import AgentLoop
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
    request_permission=None,
    vcr_mode: str | None = None,
    session_id: str | None = None,
    system_prompt: str | None = None,
) -> AgentLoop:
    settings = load_settings(project_dir=cwd)
    client = LLMClient(project_dir=str(cwd), vcr_mode=vcr_mode)
    audit = JsonlAuditSink(paths.config_dir() / "audit.jsonl")
    permissions = PermissionEngine(audit_sink=audit)
    registry = ToolRegistry(get_builtin_tools())
    session = Session(session_id or _new_session_id(), session_root())

    return AgentLoop(
        client=client,
        tools=registry,
        permissions=permissions,
        request_permission=request_permission,
        system_prompt=system_prompt if system_prompt is not None else get_base_prompt(str(cwd)),
        model=model,
        mode=mode,
        max_turns=max_turns,
        cwd=cwd,
        session=session,
        settings=settings,
    )


def _new_session_id() -> str:
    from datetime import datetime

    # microsecond precision: same-second sessions must not collide
    return datetime.now().strftime("session-%Y%m%d-%H%M%S-%f")
