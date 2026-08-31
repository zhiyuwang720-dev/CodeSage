from __future__ import annotations

from pathlib import Path
from typing import Any

from .discovery import discover_skill_entries
from .filters import select_skill_entries
from .migration import load_agent_bindings
from .models import SkillEntry, SkillPromptState
from .prompt import build_skill_prompt_state
from .route_plan import build_skill_route_plan


def compose_match_text(context: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("task", "task_context"):
        value = context.get(key)
        if value:
            parts.append(str(value))

    config = context.get("config", {}) or {}
    target_vulns = config.get("target_vulnerabilities") or []
    if isinstance(target_vulns, list):
        parts.extend(str(item) for item in target_vulns)

    recon_data = context.get("recon_data", {}) or {}
    for key in ("summary", "priority_paths", "entry_points", "project_profile", "high_risk_areas"):
        value = recon_data.get(key)
        if value:
            parts.append(str(value))

    project_info = context.get("project_info", {}) or {}
    for key in ("name", "languages", "frameworks"):
        value = project_info.get(key)
        if value:
            parts.append(str(value))

    return "\n".join(parts).lower()


def resolve_agent_skill_state(
    *,
    library_root: Path,
    project_root: Path,
    agent_type: str,
    context: dict[str, Any],
) -> SkillPromptState:
    entries = discover_skill_entries(library_root=library_root, project_root=project_root)
    bindings = load_agent_bindings(library_root=library_root, agent_type=agent_type)
    available, matched = select_skill_entries(entries=entries, bindings=bindings, match_text=compose_match_text(context))
    route_plan = build_skill_route_plan(available=available, matched=matched)
    return build_skill_prompt_state(entries=available, matched=matched, route_plan=route_plan)


def resolve_skill_entry(
    *,
    library_root: Path,
    project_root: Path,
    skill_ref: str,
    agent_type: str | None = None,
) -> SkillEntry:
    entries = discover_skill_entries(library_root=library_root, project_root=project_root)
    if agent_type:
        bindings = load_agent_bindings(library_root=library_root, agent_type=agent_type)
        entries, _ = select_skill_entries(entries=entries, bindings=bindings, match_text="")

    normalized = str(skill_ref or "").strip().lower()
    for entry in entries:
        if normalized in {entry.slug.lower(), entry.name.lower()}:
            return entry

    if agent_type:
        raise ValueError(f"Skill '{skill_ref}' is not enabled for agent '{agent_type}'.")
    raise ValueError("Skill not found.")
