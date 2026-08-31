from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import unquote, urlparse


@dataclass(frozen=True, slots=True)
class ExplicitSkillMention:
    skill_ref: str
    source: str
    raw_mention: str
    kind: str


_DOLLAR_RE = re.compile(r"(?<![\w$])\$([A-Za-z0-9][A-Za-z0-9_.:-]*)")
_SKILL_URI_RE = re.compile(r"\bskill://([A-Za-z0-9][A-Za-z0-9_.:-]*)", re.IGNORECASE)
_SKILL_CALL_RE = re.compile(r"\bSkill\s*\(\s*(?:skill_ref\s*=\s*)?([\"']?)([^\"')\n,]+)\1", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def collect_explicit_skill_mentions(
    *,
    mention_sources: Iterable[tuple[str, str | None]],
    available_skills: Iterable[dict[str, Any]],
) -> list[ExplicitSkillMention]:
    aliases, paths = _build_skill_lookup(available_skills)
    seen: set[str] = set()
    mentions: list[ExplicitSkillMention] = []
    for source, raw_text in mention_sources:
        text = str(raw_text or "")
        if not text.strip():
            continue
        for candidate in sorted(_iter_candidates(text), key=lambda item: item.start):
            skill_ref = _resolve_candidate(candidate, aliases=aliases, paths=paths)
            if not skill_ref or skill_ref in seen:
                continue
            seen.add(skill_ref)
            mentions.append(
                ExplicitSkillMention(
                    skill_ref=skill_ref,
                    source=str(source or "").strip() or "unknown",
                    raw_mention=candidate.raw_value,
                    kind=candidate.kind,
                )
            )
    return mentions


@dataclass(frozen=True, slots=True)
class _MentionCandidate:
    value: str
    raw_value: str
    kind: str
    start: int


def _iter_candidates(text: str) -> Iterable[_MentionCandidate]:
    for match in _MARKDOWN_LINK_RE.finditer(text):
        label, target = match.groups()
        yield _MentionCandidate(value=target, raw_value=match.group(0), kind="markdown_link", start=match.start())
        yield _MentionCandidate(value=label.strip().lstrip("$"), raw_value=match.group(0), kind="markdown_link_label", start=match.start())
    for match in _SKILL_URI_RE.finditer(text):
        yield _MentionCandidate(value=match.group(1), raw_value=match.group(0), kind="skill_uri", start=match.start())
    for match in _DOLLAR_RE.finditer(text):
        yield _MentionCandidate(value=match.group(1), raw_value=match.group(0), kind="dollar", start=match.start())
    for match in _SKILL_CALL_RE.finditer(text):
        yield _MentionCandidate(value=match.group(2).strip(), raw_value=match.group(0), kind="skill_call", start=match.start())


def _build_skill_lookup(available_skills: Iterable[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    aliases: dict[str, str] = {}
    paths: dict[str, str] = {}
    for item in available_skills or []:
        skill_ref = _skill_ref(item)
        if not skill_ref:
            continue
        for value in (skill_ref, item.get("slug"), item.get("id"), item.get("name")):
            normalized = _normalize_alias(value)
            if normalized:
                aliases.setdefault(normalized, skill_ref)
        for path in _iter_skill_paths(item):
            normalized_path = _normalize_path(path)
            if normalized_path:
                paths.setdefault(normalized_path, skill_ref)
    return aliases, paths


def _resolve_candidate(candidate: _MentionCandidate, *, aliases: dict[str, str], paths: dict[str, str]) -> str | None:
    value = str(candidate.value or "").strip()
    if not value:
        return None
    normalized_path = _normalize_path(value)
    if normalized_path in paths:
        return paths[normalized_path]
    normalized_alias = _normalize_alias(value)
    return aliases.get(normalized_alias)


def _skill_ref(item: dict[str, Any]) -> str:
    return str(item.get("slug") or item.get("id") or item.get("name") or "").strip()


def _iter_skill_paths(item: dict[str, Any]) -> Iterable[str]:
    for key in ("skill_file_path", "skill_file", "file", "path"):
        value = item.get(key)
        if isinstance(value, str):
            yield value
    paths = item.get("paths")
    if isinstance(paths, dict):
        for value in paths.values():
            if isinstance(value, str):
                yield value
    elif isinstance(paths, list):
        for value in paths:
            if isinstance(value, str):
                yield value


def _normalize_alias(value: Any) -> str:
    return str(value or "").strip().strip(".,;:!?)]}").lower()


def _normalize_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme and parsed.scheme != "file":
        return raw.lower()
    path = unquote(parsed.path if parsed.scheme == "file" else raw)
    return path.replace("\\", "/").rstrip("/").lower()
