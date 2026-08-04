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


def test_validate_input_raises_tool_error():
    from codesage.tools.builtin.shell.bash import BashTool

    tool = BashTool()
    with pytest.raises(ToolError):
        tool.validate_input({"command": "ls", "timeout_ms": -1})
    with pytest.raises(ToolError):
        tool.validate_input({"command": "   "})
    with pytest.raises(ToolError):
        tool.validate_input({"command": "ls", "timeout_ms": 999_999_999})
