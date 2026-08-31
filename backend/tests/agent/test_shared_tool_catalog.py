from app.services.skill_file_service import SkillFileService


def test_build_agent_tool_catalog_for_analysis_includes_runtime_aligned_system_tools(tmp_path):
    from app.services.agent.tools.shared_catalog import build_agent_tool_catalog

    tools = build_agent_tool_catalog(
        project_root=str(tmp_path),
        exclude_patterns=["tests/**"],
        target_files=["src/api.py"],
        user_id="user-1",
        agent_type="analysis",
    )

    assert set(tools) >= {
        "read_file",
        "read_many_files",
        "list_files",
        "search_code",
        "think",
        "reflect",
        "TodoWrite",
        "AskUser",
        "EnterPlanMode",
        "ExitPlanMode",
        "load_skill_body",
        "skill_resource_lookup",
    }
    assert str(SkillFileService.library_root()) in tools["read_file"].allowed_roots


def test_build_agent_tool_catalog_for_orchestrator_keeps_system_interaction_tools_without_file_runtime():
    from app.services.agent.tools.shared_catalog import build_agent_tool_catalog

    tools = build_agent_tool_catalog(
        project_root=None,
        user_id="user-1",
        agent_type="orchestrator",
    )

    assert set(tools) == {
        "think",
        "reflect",
        "TodoWrite",
        "AskUser",
        "EnterPlanMode",
        "ExitPlanMode",
        "load_skill_body",
        "skill_resource_lookup",
    }
