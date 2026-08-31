from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

from .models import SkillEntry


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    text = (content or "").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text

    raw_meta = text[4:end].strip()
    body = text[end + 5 :].lstrip()
    try:
        loaded = yaml.safe_load(raw_meta) or {}
    except Exception:  # noqa: BLE001
        return {}, text

    if not isinstance(loaded, dict):
        return {}, body

    metadata = {
        str(key).strip().lower(): value
        for key, value in loaded.items()
        if str(key).strip()
    }
    return metadata, body


def _frontmatter_get(frontmatter: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in frontmatter:
            return frontmatter[key]
    return None


def _coerce_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _coerce_string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if value is None:
        return []
    return [str(value)]


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _build_paths(base_dir: Path, project_root: Path, file_name: str = "SKILL.md") -> Dict[str, str]:
    relative = base_dir.resolve().relative_to(project_root.resolve()).as_posix()
    return {
        "storage_path": str(base_dir),
        "workspace_relative_path": relative,
        "workspace_file_path": f"{relative}/{file_name}",
        "skill_root": str(base_dir),
        "skill_file_path": str(base_dir / file_name),
        "references_root": str(base_dir / "references"),
        "examples_root": str(base_dir / "examples"),
        "scripts_root": str(base_dir / "scripts"),
    }


def _build_extension_manifest(skill_dir: Path) -> List[Dict[str, Any]]:
    manifest: List[Dict[str, Any]] = []
    for folder in ("references", "examples", "scripts"):
        base = skill_dir / folder
        if not base.exists():
            continue
        for file in sorted(base.rglob("*")):
            if file.is_dir():
                continue
            manifest.append(
                {
                    "name": file.relative_to(skill_dir).as_posix(),
                    "description": f"Local skill resource {file.name}",
                    "type": "file",
                }
            )
    return manifest


def discover_skill_entries(library_root: Path, project_root: Path | None = None) -> List[SkillEntry]:
    library_root = Path(library_root).resolve()
    project_root = Path(project_root).resolve() if project_root else library_root.parent.resolve()
    if not library_root.exists():
        return []

    entries: List[SkillEntry] = []
    for child in sorted(library_root.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir():
            continue
        if child.name in {"agents", ".runtime"} or child.name.startswith("."):
            continue

        skill_file = child / "SKILL.md"
        if not skill_file.exists() or not skill_file.is_file():
            continue

        content = skill_file.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(content)
        raw_metadata = _read_json(child / "metadata.json", {})
        if not isinstance(raw_metadata, dict):
            raw_metadata = {}
        nested_metadata = raw_metadata.get("metadata", {}) if isinstance(raw_metadata, dict) else {}
        paths = _build_paths(child, project_root)

        tags = frontmatter.get("tags")
        if not isinstance(tags, list):
            tags = raw_metadata.get("tags") or nested_metadata.get("tags") or []
        if not isinstance(tags, list):
            tags = []

        metadata_json = {
            **(nested_metadata if isinstance(nested_metadata, dict) else {}),
            **paths,
            "workspace_skill_file": paths["workspace_file_path"],
            "folder_path": str(child),
            "bindings_file": str(child / "bindings.json"),
        }

        user_invocable = _frontmatter_get(frontmatter, "user-invocable", "user_invocable")

        entries.append(
            SkillEntry(
                slug=child.name,
                name=str(frontmatter.get("name") or raw_metadata.get("name") or child.name),
                description=str(frontmatter.get("description") or raw_metadata.get("description") or ""),
                skill_file=str(skill_file),
                folder_path=str(child),
                tags=[str(tag) for tag in tags],
                when_to_use=_coerce_string(_frontmatter_get(frontmatter, "when_to_use", "when-to-use")),
                allowed_tools=_coerce_string_list(_frontmatter_get(frontmatter, "allowed-tools", "allowed_tools")),
                argument_hint=_coerce_string(_frontmatter_get(frontmatter, "argument-hint", "argument_hint")),
                argument_names=_coerce_string_list(_frontmatter_get(frontmatter, "arguments", "argument_names")),
                version=_coerce_string(frontmatter.get("version")),
                model=_coerce_string(frontmatter.get("model")),
                disable_model_invocation=bool(
                    _frontmatter_get(frontmatter, "disable-model-invocation", "disable_model_invocation") or False
                ),
                user_invocable=True if user_invocable is None else bool(user_invocable),
                execution_context=_coerce_string(frontmatter.get("context")),
                agent=_coerce_string(frontmatter.get("agent")),
                effort=_coerce_string(frontmatter.get("effort")),
                shell=frontmatter.get("shell") if isinstance(frontmatter.get("shell"), dict) else None,
                hooks=frontmatter.get("hooks") if isinstance(frontmatter.get("hooks"), dict) else {},
                paths=_coerce_string_list(frontmatter.get("paths")),
                frontmatter=frontmatter,
                metadata_json=metadata_json,
                source_type=str(raw_metadata.get("source_type") or nested_metadata.get("source_type") or "manual"),
                source_url=raw_metadata.get("source_url") or nested_metadata.get("source_url"),
                content=content,
                skill_body=body,
                extension_manifest=raw_metadata.get("extension_manifest") or _build_extension_manifest(child),
                is_system=bool(raw_metadata.get("is_system", False)),
                is_active=bool(raw_metadata.get("is_active", True)),
            )
        )

    return entries
