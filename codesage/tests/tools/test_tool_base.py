"""Tool contract basics."""

import pytest

from codesage.tools import Tool, ToolError, ToolResult, ToolUseContext


def test_spec_generation():
    class Dummy(Tool):
        name = "Dummy"
        description = "A test tool"
        input_schema = {"type": "object", "properties": {"x": {"type": "string"}}}

    spec = Dummy().spec()
    assert spec.name == "Dummy"
    assert spec.description == "A test tool"
    assert spec.input_schema["properties"]["x"]["type"] == "string"


async def test_base_call_wraps_run():
    class Dummy(Tool):
        name = "Dummy"

        async def _run(self, input, ctx):
            return ToolResult("done")

    async for result in Dummy().call({}, ToolUseContext(cwd=__import__("pathlib").Path("."))):
        assert result.content == "done"


def test_undeclared_tool_defaults_to_not_concurrency_safe():
    # fail-closed: only read-only tools may opt into parallel execution
    class Dummy(Tool):
        name = "Dummy"

    assert Dummy().is_concurrency_safe is False


def test_readonly_builtins_declare_concurrency_safe():
    from codesage.tools.builtin.filesystem.ls import LSTool
    from codesage.tools.builtin.filesystem.read import ReadTool
    from codesage.tools.builtin.network.webfetch import WebFetchTool
    from codesage.tools.builtin.search.glob import GlobTool
    from codesage.tools.builtin.search.grep import GrepTool
    from codesage.tools.builtin.system.task import TaskOutputTool

    for tool in (LSTool(), ReadTool(), GlobTool(), GrepTool(), WebFetchTool(), TaskOutputTool()):
        assert tool.is_concurrency_safe, tool.name


def test_validate_input_raises_tool_error():
    from codesage.tools.builtin.shell.bash import BashTool

    tool = BashTool()
    with pytest.raises(ToolError):
        tool.validate_input({"command": "ls", "timeout_ms": -1})
    with pytest.raises(ToolError):
        tool.validate_input({"command": "   "})
    with pytest.raises(ToolError):
        tool.validate_input({"command": "ls", "timeout_ms": 999_999_999})
