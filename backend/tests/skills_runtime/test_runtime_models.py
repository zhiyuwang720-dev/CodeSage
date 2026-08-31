from app.services.skills_runtime.models import SkillBinding, SkillEntry, SkillPromptState, SkillRoutePlan


def test_skill_entry_defaults():
    entry = SkillEntry(
        slug="demo-skill",
        name="demo-skill",
        description="Demo description",
        skill_file="/tmp/project/skill_library/demo-skill/SKILL.md",
        folder_path="/tmp/project/skill_library/demo-skill",
    )

    assert entry.slug == "demo-skill"
    assert entry.tags == []
    assert entry.frontmatter == {}
    assert entry.metadata_json == {}
    assert entry.extension_manifest == []
    assert entry.is_active is True
    assert entry.when_to_use is None
    assert entry.allowed_tools == []
    assert entry.argument_hint is None
    assert entry.argument_names == []
    assert entry.version is None
    assert entry.model is None
    assert entry.disable_model_invocation is False
    assert entry.user_invocable is True
    assert entry.execution_context is None
    assert entry.agent is None
    assert entry.effort is None
    assert entry.shell is None
    assert entry.hooks == {}
    assert entry.paths == []


def test_skill_binding_defaults():
    binding = SkillBinding(agent_type="finding", slug="demo-skill")

    assert binding.agent_type == "finding"
    assert binding.slug == "demo-skill"
    assert binding.enabled is True
    assert binding.always_include is False
    assert binding.sort_order == 0
    assert binding.match_keywords == []
    assert binding.match_config == {}


def test_skill_prompt_state_defaults():
    state = SkillPromptState()

    assert state.entries == []
    assert state.matched == []
    assert state.prompt == ""
    assert state.route_plan.primary_skill is None


def test_skill_route_plan_defaults():
    plan = SkillRoutePlan()

    assert plan.primary_skill is None
    assert plan.secondary_skills == []
    assert plan.mandatory_reads == []
    assert plan.recommended_reads == []
    assert plan.selection_reason == []
    assert plan.startup_reads == []
    assert plan.deferred_skills == []
    assert plan.deferred_skill_reads == []
