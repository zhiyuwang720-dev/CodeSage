from __future__ import annotations

from .models import SkillBinding, SkillEntry


def normalize_keywords(values: list[str]) -> list[str]:
    return [value.strip().lower() for value in values if value and value.strip()]


def _normalize_match_text(match_text: str) -> str:
    return (match_text or "").lower()


def _matches_config(match_text: str, match_config: dict) -> bool:
    if not isinstance(match_config, dict) or not match_config:
        return False

    for key in ("languages", "frameworks", "domains", "path_keywords"):
        values = normalize_keywords([str(value) for value in match_config.get(key, [])])
        if values and any(value in match_text for value in values):
            return True
    return False


def select_skill_entries(
    entries: list[SkillEntry],
    bindings: list[SkillBinding],
    match_text: str,
) -> tuple[list[SkillEntry], list[SkillEntry]]:
    match_text = _normalize_match_text(match_text)
    entries_by_slug = {entry.slug: entry for entry in entries}
    available: list[SkillEntry] = []
    matched: list[SkillEntry] = []

    for binding in sorted(bindings, key=lambda item: (item.sort_order, item.slug)):
        if not binding.enabled:
            continue
        entry = entries_by_slug.get(binding.slug)
        if entry is None:
            continue
        available.append(entry)

        keywords = normalize_keywords(binding.match_keywords) or normalize_keywords(entry.tags)
        keyword_match = any(keyword in match_text for keyword in keywords)
        config_match = _matches_config(match_text, binding.match_config)
        if binding.always_include or keyword_match or config_match:
            matched.append(entry)

    return available, matched
