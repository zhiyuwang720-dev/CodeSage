from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.services.session.store import AuditSessionStore
from app.services.tooling.read import GlobRuntimeTool, GrepRuntimeTool
from app.services.tooling.runtime import ToolExecutionContext


def build_store() -> AuditSessionStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return AuditSessionStore(session_factory=session_factory)


def build_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id="session-1",
        turn_id="turn-1",
        tool_use_id="tool-use-1",
        tool_call_id="tool-call-1",
    )


def make_workspace(tmp_path) -> str:
    (tmp_path / "a.py").write_text("def dangerous():\n    return 1\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("hello dangerous world\n", encoding="utf-8")
    return str(tmp_path)


def build_grep(project_root: str) -> GrepRuntimeTool:
    return GrepRuntimeTool(project_root=project_root)


def build_glob(project_root: str) -> GlobRuntimeTool:
    return GlobRuntimeTool(project_root=project_root)


def test_grep_defaults_to_250_results(tmp_path):
    grep = build_grep(make_workspace(tmp_path))

    parsed = grep.validate_input({"pattern": "dangerous"})

    assert parsed.max_results == 250
    assert parsed.timeout_seconds == 45
    assert grep.execution_timeout_seconds(parsed, build_context()) == 47

    extended = grep.validate_input({"pattern": "dangerous", "timeout_seconds": 120})
    assert grep.execution_timeout_seconds(extended, build_context()) == 122


async def test_grep_caps_requested_results_to_250_and_warns(tmp_path):
    grep = build_grep(make_workspace(tmp_path))

    parsed = grep.validate_input({"pattern": "dangerous", "max_results": 999})
    payload = await grep.execute(parsed, build_context())

    assert payload.metadata["truncated"] is True
    assert payload.output_payload["applied_limit"] == 250
    assert "结果被截断，使用更具体的 path 或 pattern" in payload.content


def test_glob_defaults_to_100_files(tmp_path):
    glob = build_glob(make_workspace(tmp_path))

    parsed = glob.validate_input({})

    assert parsed.max_results == 100
    assert parsed.timeout_seconds == 45
    assert glob.execution_timeout_seconds(parsed, build_context()) == 47

    extended = glob.validate_input({"timeout_seconds": 120})
    assert glob.execution_timeout_seconds(extended, build_context()) == 122


async def test_glob_caps_requested_files_to_100_and_warns(tmp_path):
    glob = build_glob(make_workspace(tmp_path))

    parsed = glob.validate_input({"max_results": 999})
    payload = await glob.execute(parsed, build_context())

    assert payload.metadata["truncated"] is True
    assert payload.output_payload["applied_limit"] == 100
    assert "结果被截断，使用更具体的 path 或 pattern" in payload.content
