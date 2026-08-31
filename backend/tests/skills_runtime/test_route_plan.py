from app.services.skills_runtime.models import SkillEntry
from app.services.skills_runtime.route_plan import build_skill_route_plan


def _entry(slug: str) -> SkillEntry:
    return SkillEntry(
        slug=slug,
        name=slug,
        description=f"{slug} description",
        skill_file=f"/tmp/skill_library/{slug}/SKILL.md",
        folder_path=f"/tmp/skill_library/{slug}",
    )


def test_build_skill_route_plan_includes_progressive_loading_metadata():
    alpha = _entry("alpha")
    ai_security = _entry("ai-security")
    beta = _entry("beta")

    plan = build_skill_route_plan(
        available=[alpha, ai_security, beta],
        matched=[alpha, ai_security],
    )

    assert plan.primary_skill == "alpha"
    assert plan.secondary_skills == ["ai-security"]
    assert plan.startup_reads == ["/tmp/skill_library/alpha/SKILL.md"]
    assert plan.deferred_skills == ["ai-security", "beta"]
    assert plan.deferred_skill_reads == [
        "/tmp/skill_library/ai-security/SKILL.md",
        "/tmp/skill_library/beta/SKILL.md",
    ]
    assert "progressively" in plan.selection_reason[-1].lower()
