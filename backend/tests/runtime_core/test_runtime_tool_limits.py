from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.services.agent.tools.base import AgentTool, ToolResult
from app.services.review_runtime.session_store import AuditSessionStore
from app.services.runtime_core.runtime_tool_registry import build_runtime_tool_registry
from app.services.runtime_core.tool_runtime import ToolExecutionContext


class FakeAgentTool(AgentTool):
    def __init__(self, name: str):
        super().__init__()
        self._name = name
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Tool {self._name}"

    async def _execute(self, **kwargs):
        self.calls.append(kwargs)
        return ToolResult(success=True, data={"received": kwargs})


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


def build_registry(*, list_tool: FakeAgentTool | None = None, search_tool: FakeAgentTool | None = None):
    return build_runtime_tool_registry(
        session_store=build_store(),
        agent_tools={
            "read_file": FakeAgentTool("read_file"),
            "read_many_files": FakeAgentTool("read_many_files"),
            "list_files": list_tool or FakeAgentTool("list_files"),
            "search_code": search_tool or FakeAgentTool("search_code"),
        },
        agent_type="finding",
        user_id="user-1",
    )


def test_grep_defaults_to_250_results():
    grep = build_registry().get("Grep")

    parsed = grep.validate_input({"pattern": "dangerous"})

    assert parsed.max_results == 250
    assert parsed.timeout_seconds == 45
    assert grep.execution_timeout_seconds(parsed, build_context()) == 47

    extended = grep.validate_input({"pattern": "dangerous", "timeout_seconds": 120})
    assert grep.execution_timeout_seconds(extended, build_context()) == 122


async def test_grep_caps_requested_results_to_250_and_warns():
    search_tool = FakeAgentTool("search_code")
    grep = build_registry(search_tool=search_tool).get("Grep")

    parsed = grep.validate_input({"pattern": "dangerous", "max_results": 999})
    payload = await grep.execute(parsed, build_context())

    assert search_tool.calls[-1]["max_results"] == 250
    assert payload.metadata["truncated"] is True
    assert "结果被截断，使用更具体的 path 或 pattern" in payload.content


def test_glob_defaults_to_100_files():
    glob = build_registry().get("Glob")

    parsed = glob.validate_input({"pattern": "**/*.py"})

    assert parsed.max_results == 100
    assert parsed.timeout_seconds == 45
    assert glob.execution_timeout_seconds(parsed, build_context()) == 47

    extended = glob.validate_input({"pattern": "**/*.py", "timeout_seconds": 120})
    assert glob.execution_timeout_seconds(extended, build_context()) == 122


async def test_glob_caps_requested_files_to_100_and_warns():
    list_tool = FakeAgentTool("list_files")
    glob = build_registry(list_tool=list_tool).get("Glob")

    parsed = glob.validate_input({"pattern": "**/*.py", "max_results": 999})
    payload = await glob.execute(parsed, build_context())

    assert list_tool.calls[-1]["max_files"] == 100
    assert payload.metadata["truncated"] is True
    assert "结果被截断，使用更具体的 path 或 pattern" in payload.content
