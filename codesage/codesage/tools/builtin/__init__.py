"""Builtin tool set, organized by category (mirrors Kode's tools/src/tools/)."""

from ..base import Tool
from .agent.agent import AgentTool
from .filesystem.edit import EditTool
from .filesystem.ls import LSTool
from .filesystem.read import ReadTool
from .filesystem.write import WriteTool
from .interaction.task_create import TaskCreateTool
from .interaction.task_get import TaskGetTool
from .interaction.task_list import TaskListTool
from .interaction.task_update import TaskUpdateTool
from .interaction.todo import TodoWriteTool
from .network.webfetch import WebFetchTool
from .search.glob import GlobTool
from .search.grep import GrepTool
from .shell.bash import BashTool
from .system.task import TaskOutputTool, TaskStopTool

#: Canonical builtin tool set; phase 15 (MCP) extends the registry dynamically.
BUILTIN_TOOLS: list[Tool] = [
    LSTool(),
    ReadTool(),
    WriteTool(),
    EditTool(),
    GlobTool(),
    GrepTool(),
    BashTool(),
    TaskOutputTool(),
    TaskStopTool(),
    TodoWriteTool(),
    TaskCreateTool(),
    TaskGetTool(),
    TaskListTool(),
    TaskUpdateTool(),
    WebFetchTool(),
    AgentTool(),
]

__all__ = ["BUILTIN_TOOLS"]
