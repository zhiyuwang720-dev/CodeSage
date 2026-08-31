from __future__ import annotations

from pathlib import Path, PurePosixPath

from .models import SkillEntry


def _is_within(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_target(skill_root: Path, relative_name: str) -> Path:
    normalized = str(relative_name or "").replace("\\", "/").strip()
    if normalized in {"", "."}:
        return skill_root.resolve()
    if ".." in PurePosixPath(normalized).parts:
        raise ValueError("Skill resource resolves outside skill root.")
    target = (skill_root / normalized).resolve(strict=False)
    if not _is_within(skill_root.resolve(), target):
        raise ValueError("Skill resource resolves outside skill root.")
    return target


def read_skill_body(entry: SkillEntry) -> dict:
    return {
        "skill": entry.name,
        "slug": entry.slug,
        "description": entry.description,
        "skill_file": entry.skill_file,
        "workspace_relative_path": entry.metadata_json.get("workspace_relative_path"),
        "content": entry.content,
        "metadata": {
            "name": entry.name,
            "description": entry.description,
            "tags": entry.tags,
            "frontmatter": entry.frontmatter,
        },
    }


def list_skill_resources(entry: SkillEntry, resource_name: str = "") -> dict:
    skill_root = Path(entry.folder_path).resolve()
    target = _resolve_target(skill_root, resource_name)
    if not target.exists() or not target.is_dir():
        raise ValueError("Skill resource directory not found.")

    items = []
    for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        resolved = child.resolve(strict=False)
        if not _is_within(skill_root, resolved):
            continue
        items.append(
            {
                "path": child.relative_to(skill_root).as_posix(),
                "name": child.name,
                "type": "directory" if child.is_dir() else "file",
            }
        )

    return {
        "skill": entry.name,
        "slug": entry.slug,
        "resource_root": resource_name.replace("\\", "/").strip("/") or ".",
        "items": items,
    }


def read_skill_resource(entry: SkillEntry, resource_name: str) -> dict:
    skill_root = Path(entry.folder_path).resolve()
    target = _resolve_target(skill_root, resource_name)
    if not target.exists() or not target.is_file():
        raise ValueError("Skill resource not found.")
    resolved = target.resolve()
    if not _is_within(skill_root, resolved):
        raise ValueError("Skill resource resolves outside skill root.")

    return {
        "skill": entry.name,
        "slug": entry.slug,
        "resource": Path(resource_name).as_posix().replace("\\", "/"),
        "content": resolved.read_text(encoding="utf-8"),
        "resource_file": str(resolved),
    }
