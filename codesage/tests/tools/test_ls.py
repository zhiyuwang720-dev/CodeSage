"""LS tool tests."""

import pytest

from codesage.tools import ToolUseContext
from codesage.tools.builtin.filesystem.ls import LSTool


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


@pytest.mark.asyncio
async def test_ls_lists_sorted_with_dir_suffix(tmp_path):
    (tmp_path / "b.txt").write_text("x")
    (tmp_path / "adir").mkdir()
    result = await LSTool().call({"path": "."}, _ctx(tmp_path)).__anext__()
    assert result.content == "adir/\nb.txt"


@pytest.mark.asyncio
async def test_ls_missing_dir_errors(tmp_path):
    result = await LSTool().call({"path": "nope"}, _ctx(tmp_path)).__anext__()
    assert result.is_error
