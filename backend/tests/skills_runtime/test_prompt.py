from app.services.skills_runtime.models import SkillEntry, SkillRoutePlan
from app.services.skills_runtime.prompt import build_skill_prompt_state


def _entry(slug: str) -> SkillEntry:
    return SkillEntry(
        slug=slug,
        name=slug,
        description=f"{slug} description",
        skill_file=f"/tmp/skill_library/{slug}/SKILL.md",
        folder_path=f"/tmp/skill_library/{slug}",
    )


def test_build_skill_prompt_state_renders_deterministic_skill_catalog():
    state = build_skill_prompt_state(entries=[_entry("beta"), _entry("alpha")])

    assert "<available_skills>" in state.prompt
    assert state.prompt.index("<name>alpha</name>") < state.prompt.index("<name>beta</name>")
    rendered = state.prompt.replace("\\", "/")
    assert "<skill_file_path>/tmp/skill_library/alpha/SKILL.md</skill_file_path>" in rendered
    assert "<references_root>/tmp/skill_library/alpha/references</references_root>" in rendered


def test_build_skill_prompt_state_renders_execution_semantics_and_progressive_loading_plan():
    route_plan = SkillRoutePlan(
        primary_skill="alpha",
        secondary_skills=["ai-security"],
        startup_reads=["/tmp/skill_library/alpha/SKILL.md"],
        deferred_skills=["beta"],
        deferred_skill_reads=[
            "/tmp/skill_library/ai-security/SKILL.md",
            "/tmp/skill_library/beta/SKILL.md",
        ],
        selection_reason=["auth signals matched alpha first"],
    )
    state = build_skill_prompt_state(
        entries=[
            SkillEntry(
                slug="alpha",
                name="Alpha",
                description="Auth-heavy audit skill",
                skill_file="/tmp/skill_library/alpha/SKILL.md",
                folder_path="/tmp/skill_library/alpha",
                when_to_use="Use for auth and IDOR reviews",
                allowed_tools=["read_file", "search_code"],
                argument_hint="<target>",
                argument_names=["target"],
                model="claude-3-7-sonnet",
                execution_context="fork",
                agent="finding",
                effort="high",
                paths=["backend/app/auth/**"],
            ),
            _entry("beta"),
        ],
        matched=[],
        route_plan=route_plan,
    )

    rendered = state.prompt.replace("\\", "/")
    assert "<when_to_use>Use for auth and IDOR reviews</when_to_use>" in rendered
    assert "<allowed_tools>read_file, search_code</allowed_tools>" in rendered
    assert "<argument_hint><target></argument_hint>" in rendered
    assert "<model>claude-3-7-sonnet</model>" in rendered
    assert "<execution_context>fork</execution_context>" in rendered
    assert "<agent>finding</agent>" in rendered
    assert "<effort>high</effort>" in rendered
    assert "<paths>backend/app/auth/**</paths>" in rendered
    assert "<progressive_loading>" in rendered
    assert "<primary_skill>alpha</primary_skill>" in rendered
    assert "<startup_reads>/tmp/skill_library/alpha/SKILL.md</startup_reads>" in rendered
    assert "<secondary_skills>ai-security</secondary_skills>" in rendered
    assert "<deferred_skills>beta</deferred_skills>" in rendered
    assert "/tmp/skill_library/ai-security/SKILL.md" in rendered
