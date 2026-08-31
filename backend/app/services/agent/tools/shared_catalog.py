from __future__ import annotations

from typing import Optional

from app.services.skill_file_service import SkillFileService

from .file_tool import FileReadTool, ReadManyFilesTool, FileSearchTool, ListFilesTool
from .interaction_agent_tools import AskUserTool, EnterPlanModeTool, ExitPlanModeTool, TodoWriteTool
from .skill_tool import SkillBodyTool, SkillResourceTool
from .thinking_tool import ReflectTool, ThinkTool


def shared_skill_library_roots() -> list[str]:
    return [str(SkillFileService.library_root())]


def build_shared_agent_tool_catalog(
    *,
    project_root: str | None,
    exclude_patterns: Optional[list[str]] = None,
    target_files: Optional[list[str]] = None,
) -> dict[str, object]:
    tools: dict[str, object] = {
        "think": ThinkTool(),
        "reflect": ReflectTool(),
        "TodoWrite": TodoWriteTool(),
        "AskUser": AskUserTool(),
        "EnterPlanMode": EnterPlanModeTool(),
        "ExitPlanMode": ExitPlanModeTool(),
    }

    if project_root:
        shared_roots = shared_skill_library_roots()
        tools.update(
            {
                "read_file": FileReadTool(project_root, exclude_patterns, target_files, additional_roots=shared_roots),
                "read_many_files": ReadManyFilesTool(project_root, exclude_patterns, target_files, additional_roots=shared_roots),
                "list_files": ListFilesTool(project_root, exclude_patterns, target_files, additional_roots=shared_roots),
                "search_code": FileSearchTool(project_root, exclude_patterns, target_files, additional_roots=shared_roots),
            }
        )

    return tools


def build_agent_skill_tools(*, user_id: str | None, agent_type: str) -> dict[str, object]:
    return {
        "load_skill_body": SkillBodyTool(user_id, agent_type=agent_type),
        "skill_resource_lookup": SkillResourceTool(user_id, agent_type=agent_type),
    }


def build_agent_tool_catalog(
    *,
    project_root: str | None,
    user_id: str | None,
    agent_type: str,
    exclude_patterns: Optional[list[str]] = None,
    target_files: Optional[list[str]] = None,
) -> dict[str, object]:
    return {
        **build_shared_agent_tool_catalog(
            project_root=project_root,
            exclude_patterns=exclude_patterns,
            target_files=target_files,
        ),
        **build_agent_skill_tools(user_id=user_id, agent_type=agent_type),
    }
