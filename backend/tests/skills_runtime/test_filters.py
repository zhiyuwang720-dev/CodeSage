from app.services.skills_runtime.filters import select_skill_entries
from app.services.skills_runtime.models import SkillBinding, SkillEntry


def _entry(slug: str, tags: list[str] | None = None) -> SkillEntry:
    return SkillEntry(
        slug=slug,
        name=slug,
        description=f"{slug} description",
        skill_file=f"/tmp/skill_library/{slug}/SKILL.md",
        folder_path=f"/tmp/skill_library/{slug}",
        tags=tags or [],
    )


def test_select_skill_entries_excludes_unbound_skills():
    available, matched = select_skill_entries(
        entries=[_entry("alpha"), _entry("beta")],
        bindings=[SkillBinding(agent_type="finding", slug="alpha")],
        match_text="",
    )

    assert [entry.slug for entry in available] == ["alpha"]
    assert matched == []


def test_select_skill_entries_honors_enabled_and_always_include():
    available, matched = select_skill_entries(
        entries=[_entry("alpha"), _entry("beta")],
        bindings=[
            SkillBinding(agent_type="finding", slug="alpha", enabled=True, always_include=True),
            SkillBinding(agent_type="finding", slug="beta", enabled=False),
        ],
        match_text="",
    )

    assert [entry.slug for entry in available] == ["alpha"]
    assert [entry.slug for entry in matched] == ["alpha"]


def test_select_skill_entries_matches_by_binding_keyword_or_tag_fallback():
    available, matched = select_skill_entries(
        entries=[_entry("alpha", ["auth"]), _entry("beta", ["files"])],
        bindings=[
            SkillBinding(agent_type="finding", slug="alpha", match_keywords=["idor"]),
            SkillBinding(agent_type="finding", slug="beta"),
        ],
        match_text="tenant idor auth flow",
    )

    assert [entry.slug for entry in available] == ["alpha", "beta"]
    assert [entry.slug for entry in matched] == ["alpha"]


def test_select_skill_entries_matches_by_match_config_signals():
    available, matched = select_skill_entries(
        entries=[_entry("ai-security", ["llm"]), _entry("code-audit", ["audit"])],
        bindings=[
            SkillBinding(
                agent_type="finding",
                slug="ai-security",
                match_config={
                    "languages": ["python"],
                    "frameworks": ["langchain"],
                    "domains": ["llm"],
                    "path_keywords": ["rag"],
                },
            ),
            SkillBinding(agent_type="finding", slug="code-audit", always_include=True),
        ],
        match_text=(
            "project languages: python\n"
            "frameworks: langchain\n"
            "summary: llm rag agent with vector store\n"
            "priority_paths: app/rag/pipeline.py"
        ),
    )

    assert [entry.slug for entry in available] == ["ai-security", "code-audit"]
    assert [entry.slug for entry in matched] == ["ai-security", "code-audit"]
