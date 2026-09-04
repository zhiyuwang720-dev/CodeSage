"""运行时工具注册表(P4: 原 runtime_tool_registry → tooling/registry.py)。

build_runtime_tool_registry 接收已实例化的文件工具(file_tools: Read/Glob/Grep),
再补入 Write/Shell/Skill/交互/终点工具。Finalizer 只保留 review:*→FinalizeReview。
"""
from __future__ import annotations

from typing import Any

from app.services.skill.tool import RuntimeSkillTool
from app.services.tooling.finalize_review import FinalizeReviewTool
from app.services.tooling.interactive.ask_user import AskUserRuntimeTool
from app.services.tooling.interactive.plan_mode import EnterPlanModeRuntimeTool, ExitPlanModeRuntimeTool
from app.services.tooling.interactive.todo import TodoWriteRuntimeTool
from app.services.tooling.runtime import RuntimeTool, ToolRegistry
from app.services.tooling.search import ToolSearchRuntimeTool
from app.services.tooling.shell import (
    BashRuntimeTool,
    PowerShellRuntimeTool,
    detect_bash_executable,
    detect_powershell_executable,
    is_powershell_runtime_tool_enabled,
)
from app.services.tooling.write import WriteRuntimeTool


def _infer_project_root(file_tools: list[RuntimeTool]) -> str | None:
    for tool in file_tools or []:
        project_root = getattr(tool, "project_root", None)
        if isinstance(project_root, str) and project_root.strip():
            return project_root
    return None


def build_runtime_tool_registry(
    *,
    session_store,
    file_tools: list[RuntimeTool],
    agent_type: str,
    user_id: str | None = None,
    tool_allowlist: set[str] | None = None,
) -> ToolRegistry:
    tools: list[RuntimeTool] = []
    tools.extend(file_tools or [])

    project_root = _infer_project_root(file_tools)
    tools.append(WriteRuntimeTool(session_store=session_store, project_root=project_root))
    if project_root:
        bash_executable = detect_bash_executable()
        if bash_executable:
            tools.append(
                BashRuntimeTool(
                    project_root=project_root,
                    executable=bash_executable,
                    session_store=session_store,
                )
            )
        if is_powershell_runtime_tool_enabled():
            powershell_executable = detect_powershell_executable()
            if powershell_executable:
                tools.append(
                    PowerShellRuntimeTool(
                        project_root=project_root,
                        executable=powershell_executable,
                        session_store=session_store,
                    )
                )

    tools.append(
        RuntimeSkillTool(
            session_store=session_store,
            agent_type=agent_type,
            user_id=user_id,
        )
    )
    if str(agent_type or "").strip().startswith("review:"):
        # 阶段 02: PR 审查三视角(review:security/architecture/quality)共用 FinalizeReview 终点
        tools.append(FinalizeReviewTool())
    tools.extend(
        [
            TodoWriteRuntimeTool(session_store),
            AskUserRuntimeTool(session_store),
            EnterPlanModeRuntimeTool(session_store),
            ExitPlanModeRuntimeTool(session_store),
        ]
    )
    if tool_allowlist is not None:
        # 阶段 02 权限矩阵(§3.2.2): 视角只能调用矩阵内工具; 终点工具始终保留
        allow = set(tool_allowlist) | {"FinalizeReview"}
        tools = [tool for tool in tools if tool.name in allow]

    registry = ToolRegistry(tools)
    if registry.has_deferred_tools():
        registry.register(ToolSearchRuntimeTool(session_store=session_store, registry_getter=lambda: registry))
    return registry
