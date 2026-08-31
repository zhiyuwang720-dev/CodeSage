from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import SkillBinding


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def runtime_state_root(library_root: Path) -> Path:
    return Path(library_root) / ".runtime"


def load_agent_bindings(library_root: Path, agent_type: str) -> list[SkillBinding]:
    library_root = Path(library_root)
    agent_root = library_root / "agents" / agent_type
    payload = _read_json(agent_root / "bindings.json", {"skills": []})
    bindings_by_slug: dict[str, SkillBinding] = {}

    for item in payload.get("skills", []) if isinstance(payload, dict) else []:
        slug = str(item.get("slug", "")).strip()
        if not slug:
            continue
        bindings_by_slug[slug] = SkillBinding(
            agent_type=agent_type,
            slug=slug,
            enabled=bool(item.get("enabled", True)),
            always_include=bool(item.get("always_include", False)),
            sort_order=int(item.get("sort_order", 0)),
            match_keywords=[str(value) for value in item.get("match_keywords", []) if str(value).strip()],
            match_config=item.get("match_config", {}) or {},
        )

    if agent_root.exists():
        for mirror in sorted(agent_root.iterdir(), key=lambda item: item.name.lower()):
            if not mirror.is_dir():
                continue
            binding_file = mirror / "binding.json"
            binding_payload = _read_json(binding_file, {})
            slug = str(
                binding_payload.get("slug")
                or binding_payload.get("skill_id")
                or mirror.name
            ).strip()
            if not slug or slug in bindings_by_slug:
                continue
            bindings_by_slug[slug] = SkillBinding(
                agent_type=agent_type,
                slug=slug,
                enabled=bool(binding_payload.get("enabled", True)),
                always_include=bool(binding_payload.get("always_include", False)),
                sort_order=int(binding_payload.get("sort_order", 0)),
                match_keywords=[
                    str(value) for value in binding_payload.get("match_keywords", []) if str(value).strip()
                ],
                match_config=binding_payload.get("match_config", {}) or {},
            )

    return sorted(bindings_by_slug.values(), key=lambda binding: (binding.sort_order, binding.slug))
