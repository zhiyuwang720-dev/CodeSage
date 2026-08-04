"""Builtin tool set, organized by category (mirrors Kode's tools/src/tools/)."""

from ..base import Tool
from .filesystem.edit import EditTool
from .filesystem.ls import LSTool
from .filesystem.read import ReadTool
from .filesystem.write import WriteTool
from .search.glob import GlobTool
from .search.grep import GrepTool
from .shell.bash import BashTool

#: Canonical builtin tool set; phase 15 (MCP) extends the registry dynamically.
BUILTIN_TOOLS: list[Tool] = [
    LSTool(),
    ReadTool(),
    WriteTool(),
    EditTool(),
    GlobTool(),
    GrepTool(),
    BashTool(),
]

__all__ = ["BUILTIN_TOOLS"]
