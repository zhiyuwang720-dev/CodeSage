from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional


class ReportTemplateFileService:
    """Filesystem-first report template library."""

    @classmethod
    def project_root(cls) -> Path:
        env_root = os.getenv("AUDITAI_ASSET_ROOT", "").strip() or os.getenv("DEEPAUDIT_ASSET_ROOT", "").strip()
        if env_root:
            return Path(env_root)

        current = Path(__file__).resolve()
        for candidate in current.parents:
            if (candidate / "docker-compose.yml").exists():
                return candidate
            if (candidate / "skill_library").exists() or (candidate / "report_template_library").exists():
                return candidate
            if (candidate / "alembic.ini").exists() and (candidate / "app").exists():
                return candidate
        return current.parents[2]

    @classmethod
    def library_root(cls) -> Path:
        root = cls.project_root() / "report_template_library"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @classmethod
    def display_root(cls) -> str:
        return "report_template_library"

    @classmethod
    def _host_project_root(cls) -> str:
        return os.getenv("HOST_PROJECT_ROOT", "").strip().rstrip("/\\")

    @classmethod
    def _to_host_path(cls, relative_path: str) -> str:
        host_root = cls._host_project_root()
        if not host_root:
            return ""
        normalized = relative_path.replace("/", "\\")
        return f"{host_root}\\{normalized}" if normalized else host_root

    @classmethod
    def slugify(cls, value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip().lower()).strip("-")
        return slug or "report-template"

    @classmethod
    def template_dir(cls, slug: str) -> Path:
        path = cls.library_root() / cls.slugify(slug)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def template_file(cls, slug: str) -> Path:
        return cls.template_dir(slug) / "template.md"

    @classmethod
    def metadata_file(cls, slug: str) -> Path:
        return cls.template_dir(slug) / "metadata.json"

    @classmethod
    def _read_json(cls, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    @classmethod
    def _write_json(cls, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def _paths(cls, slug: str) -> Dict[str, str]:
        directory = cls.template_dir(slug)
        relative = directory.relative_to(cls.project_root()).as_posix()
        return {
            "storage_path": str(directory),
            "template_file": str(directory / "template.md"),
            "workspace_relative_path": relative,
            "workspace_file_path": f"{relative}/template.md",
            "host_storage_path": cls._to_host_path(relative),
            "host_template_file": cls._to_host_path(f"{relative}/template.md"),
            "display_root": cls.display_root(),
        }

    @classmethod
    def read_template(cls, slug: str) -> Dict[str, Any]:
        slug = cls.slugify(slug)
        metadata = cls._read_json(cls.metadata_file(slug), {})
        content = cls.template_file(slug).read_text(encoding="utf-8") if cls.template_file(slug).exists() else ""
        paths = cls._paths(slug)
        return {
            "id": slug,
            "slug": slug,
            "name": metadata.get("name") or slug,
            "description": metadata.get("description"),
            "report_type": metadata.get("report_type", "final_vulnerability_report"),
            "output_format": metadata.get("output_format", "markdown"),
            "content": content,
            "variables": metadata.get("variables", {}) or {},
            "metadata_json": {**(metadata.get("metadata", {}) or {}), **paths},
            "is_default": bool(metadata.get("is_default", False)),
            "is_system": bool(metadata.get("is_system", False)),
            "is_active": bool(metadata.get("is_active", True)),
            "sort_order": int(metadata.get("sort_order", 0)),
            "folder_path": str(cls.template_dir(slug)),
            "template_file": str(cls.template_file(slug)),
            "created_by": None,
            "created_at": None,
            "updated_at": None,
        }

    @classmethod
    def list_templates(cls) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for entry in sorted(cls.library_root().iterdir(), key=lambda item: item.name.lower()):
            if entry.is_dir():
                items.append(cls.read_template(entry.name))
        return items

    @classmethod
    def write_template(
        cls,
        *,
        slug: str,
        name: str,
        description: Optional[str],
        content: str,
        report_type: str,
        output_format: str,
        variables: Dict[str, Any],
        metadata_json: Optional[Dict[str, Any]],
        is_default: bool,
        is_system: bool,
        is_active: bool,
        sort_order: int,
    ) -> Dict[str, Any]:
        slug = cls.slugify(slug)
        cls.template_file(slug).write_text(content or "", encoding="utf-8")
        cls._write_json(
            cls.metadata_file(slug),
            {
                "name": name,
                "description": description,
                "report_type": report_type,
                "output_format": output_format,
                "variables": variables or {},
                "metadata": metadata_json or {},
                "is_default": bool(is_default),
                "is_system": bool(is_system),
                "is_active": bool(is_active),
                "sort_order": int(sort_order),
            },
        )
        return cls.read_template(slug)

    @classmethod
    def rename_template(cls, current_slug: str, new_slug: str) -> Dict[str, Any]:
        current_slug = cls.slugify(current_slug)
        new_slug = cls.slugify(new_slug)
        if current_slug == new_slug:
            return cls.read_template(new_slug)
        current_dir = cls.library_root() / current_slug
        target_dir = cls.library_root() / new_slug
        if not current_dir.exists():
            raise FileNotFoundError(f"Template '{current_slug}' not found")
        if target_dir.exists():
            raise FileExistsError(f"Template '{new_slug}' already exists")
        current_dir.rename(target_dir)
        return cls.read_template(new_slug)

    @classmethod
    def delete_template(cls, slug: str) -> None:
        directory = cls.library_root() / cls.slugify(slug)
        if directory.exists():
            shutil.rmtree(directory)

    @classmethod
    def clear_default_flags(cls, exclude_slug: Optional[str] = None) -> None:
        exclude_slug = cls.slugify(exclude_slug) if exclude_slug else None
        for entry in cls.list_templates():
            if exclude_slug and entry["slug"] == exclude_slug:
                continue
            metadata = cls._read_json(cls.metadata_file(entry["slug"]), {})
            metadata["is_default"] = False
            cls._write_json(cls.metadata_file(entry["slug"]), metadata)
