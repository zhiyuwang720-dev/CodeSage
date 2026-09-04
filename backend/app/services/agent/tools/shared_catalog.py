from __future__ import annotations

from typing import Optional

from app.services.skill_file_service import SkillFileService

from .file_tool import FileReadTool, ReadManyFilesTool, FileSearchTool, ListFilesTool


def shared_skill_library_roots() -> list[str]:
    return [str(SkillFileService.library_root())]


def build_shared_agent_tool_catalog(
    *,
    project_root: str | None,
    exclude_patterns: Optional[list[str]] = None,
    target_files: Optional[list[str]] = None,
) -> dict[str, object]:
    """06-P1 起只保留文件运行时四件(读取侧由 Canonical* 包装成 RuntimeTool)。

    AskUser/EnterPlanMode/ExitPlanMode/TodoWrite 的活版本是 RuntimeTool 变体,由
    build_runtime_tool_registry 直接注入;legacy AgentTool 副本(interaction/skill/thinking)
    已于 P1 退役,这里不再产出对应死键。
    """
    tools: dict[str, object] = {}

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
