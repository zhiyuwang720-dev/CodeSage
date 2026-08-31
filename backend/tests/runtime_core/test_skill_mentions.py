from __future__ import annotations

from app.services.runtime_core.skill_mentions import collect_explicit_skill_mentions


SKILLS = [
    {
        "slug": "code-audit-finding",
        "name": "Code Audit Finding",
        "paths": {
            "skill_file_path": "/app/skill_library/code-audit-finding/SKILL.md",
        },
    },
    {
        "slug": "cve-report-writer",
        "name": "CVE Report Writer",
        "paths": {
            "skill_file_path": "/app/skill_library/cve-report-writer/SKILL.md",
        },
    },
]


def test_collect_explicit_skill_mentions_from_configured_sources():
    mentions = collect_explicit_skill_mentions(
        mention_sources=[
            ("user", "Please load $code-audit-finding before auditing."),
            ("system", 'Use Skill("cve-report-writer") for reports.'),
            ("route", "See [$code-audit-finding](/app/skill_library/code-audit-finding/SKILL.md)."),
        ],
        available_skills=SKILLS,
    )

    assert [(item.skill_ref, item.source) for item in mentions] == [
        ("code-audit-finding", "user"),
        ("cve-report-writer", "system"),
    ]


def test_collect_explicit_skill_mentions_supports_case_insensitive_exact_names():
    mentions = collect_explicit_skill_mentions(
        mention_sources=[("user", "Use $Code-Audit-Finding and skill://CVE-REPORT-WRITER.")],
        available_skills=SKILLS,
    )

    assert [item.skill_ref for item in mentions] == ["code-audit-finding", "cve-report-writer"]


def test_collect_explicit_skill_mentions_does_not_fuzzy_match_natural_language():
    mentions = collect_explicit_skill_mentions(
        mention_sources=[("user", "Please use the code audit skill for this review.")],
        available_skills=SKILLS,
    )

    assert mentions == []
