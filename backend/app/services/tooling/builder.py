"""运行时文件工具目录(P4: 原 shared_catalog → builder.py)。

build_runtime_tool_catalog 直接产出文件工具的 RuntimeTool 实例(Read/Glob/Grep),
不再经旧工具 dict; registry 只负责把 Skill/Shell/交互/终点工具补入注册表。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from app.services.skill.file_service import SkillFileService

from app.services.tooling.read import GlobRuntimeTool, GrepRuntimeTool, ReadRuntimeTool

if TYPE_CHECKING:
    from app.services.contracts.tools import RuntimeTool


def shared_skill_library_roots() -> list[str]:
    return [str(SkillFileService.library_root())]


def build_runtime_tool_catalog(
    *,
    project_root: str | None,
    exclude_patterns: Optional[list[str]] = None,
    target_files: Optional[list[str]] = None,
    additional_roots: Optional[list[str]] = None,
) -> list[RuntimeTool]:
    """构建文件工具的 RuntimeTool 实例列表(读/枚举/搜索)。

    06-P4 起只保留文件运行时三件(Read 兼单文件与批量); 写工具 WriteRuntimeTool 需要
    session_store, 由 build_runtime_tool_registry 内部挂载, 不在此目录内。
    AskUser/EnterPlanMode/ExitPlanMode/TodoWrite 的活版本是 RuntimeTool 变体, 同样由
    registry 注入; legacy 工具副本已于 P1/P4 退役。
    """
    if not project_root:
        return []
    shared_roots = shared_skill_library_roots()
    extra_roots = [str(root) for root in (additional_roots or []) if str(root or "").strip()]
    roots = extra_roots + shared_roots
    return [
        ReadRuntimeTool(
            project_root=project_root,
            exclude_patterns=exclude_patterns,
            target_files=target_files,
            additional_roots=roots,
        ),
        GlobRuntimeTool(
            project_root=project_root,
            exclude_patterns=exclude_patterns,
            target_files=target_files,
            additional_roots=roots,
        ),
        GrepRuntimeTool(
            project_root=project_root,
            exclude_patterns=exclude_patterns,
            target_files=target_files,
            additional_roots=roots,
        ),
    ]
