from __future__ import annotations

from .models import SkillEntry, SkillRoutePlan


def build_skill_route_plan(
    available: list[SkillEntry],
    matched: list[SkillEntry],
) -> SkillRoutePlan:
    if matched:
        primary_entry = matched[0]
        secondary = [entry.slug for entry in matched[1:]]
        deferred_entries = [entry for entry in available if entry.slug != primary_entry.slug]
        reasons = [f"Primary skill selected from matched runtime skills: {primary_entry.slug}"]
        if secondary:
            reasons.append("Additional matched skills available for targeted follow-up reads.")
        if deferred_entries:
            reasons.append("Load the primary skill first and progressively read secondary or deferred skills only when needed.")
        return SkillRoutePlan(
            primary_skill=primary_entry.slug,
            secondary_skills=secondary,
            startup_reads=[primary_entry.skill_file],
            deferred_skills=[entry.slug for entry in deferred_entries],
            deferred_skill_reads=[entry.skill_file for entry in deferred_entries],
            selection_reason=reasons,
        )

    if available:
        primary_entry = available[0]
        deferred_entries = available[1:]
        reasons = [f"Fallback to first enabled skill: {primary_entry.slug}"]
        if deferred_entries:
            reasons.append("Load the fallback primary skill first and keep the rest deferred for progressive follow-up.")
        return SkillRoutePlan(
            primary_skill=primary_entry.slug,
            secondary_skills=[],
            startup_reads=[primary_entry.skill_file],
            deferred_skills=[entry.slug for entry in deferred_entries],
            deferred_skill_reads=[entry.skill_file for entry in deferred_entries],
            selection_reason=reasons,
        )

    return SkillRoutePlan()
