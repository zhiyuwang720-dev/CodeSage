"""Write tool tests."""

import pytest

from codesage.tools import ToolUseContext
from codesage.tools.builtin.filesystem.write import WriteTool


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


@pytest.mark.asyncio
async def test_write_creates_directories(tmp_path):
    result = await WriteTool().call({"file_path": "a/b/c.txt", "content": "hello"}, _ctx(tmp_path)).__anext__()
    assert not result.is_error
    assert (tmp_path / "a" / "b" / "c.txt").read_text() == "hello"
