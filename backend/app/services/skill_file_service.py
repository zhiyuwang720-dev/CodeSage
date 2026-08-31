from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from app.services.skills_runtime.discovery import parse_frontmatter

AGENT_TYPES = ["orchestrator", "recon", "scan", "triage", "finding", "verification", "audit_chat"]


def _safe_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part not in {"", ".", ".."}]
    return "/".join(parts)


class SkillFileService:
    """Filesystem-first skill catalog and binding management."""

    @classmethod
    def project_root(cls) -> Path:
        env_root = os.getenv("AUDITAI_ASSET_ROOT", "").strip() or os.getenv("DEEPAUDIT_ASSET_ROOT", "").strip()
        if env_root:
            return Path(env_root)

        current = Path(__file__).resolve()
        best_candidate: Optional[Path] = None
        best_score = -1
        for candidate in current.parents:
            if (candidate / "docker-compose.yml").exists():
                score = 3
            elif (candidate / "skill_library").exists() or (candidate / "report_template_library").exists():
                score = 2
            elif (candidate / "alembic.ini").exists() and (candidate / "app").exists():
                score = 1
            else:
                score = -1
            if score > best_score:
                best_candidate = candidate
                best_score = score
                if score == 3:
                    break
        return best_candidate or current.parents[2]

    @classmethod
    def library_root(cls) -> Path:
        root = cls.project_root() / "skill_library"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @classmethod
    def runtime_root(cls) -> Path:
        root = cls.library_root() / ".runtime"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @classmethod
    def installed_skills_index_file(cls) -> Path:
        return cls.runtime_root() / "installed_skills.json"

    @classmethod
    def agents_root(cls) -> Path:
        root = cls.library_root() / "agents"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @classmethod
    def display_root(cls) -> str:
        return "skill_library"

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
    def _timestamp(cls) -> str:
        return datetime.now(timezone.utc).isoformat()

    @classmethod
    def slugify(cls, value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip().lower()).strip("-")
        return slug or "skill"

    @classmethod
    def skill_dir(cls, slug: str) -> Path:
        path = cls.library_root() / cls.slugify(slug)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def skill_file(cls, slug: str) -> Path:
        return cls.skill_dir(slug) / "SKILL.md"

    @classmethod
    def metadata_file(cls, slug: str) -> Path:
        return cls.skill_dir(slug) / "metadata.json"

    @classmethod
    def aggregated_bindings_file(cls, slug: str) -> Path:
        return cls.skill_dir(slug) / "bindings.json"

    @classmethod
    def agent_root(cls, agent_type: str) -> Path:
        path = cls.agents_root() / agent_type
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def bindings_file(cls, agent_type: str) -> Path:
        return cls.agent_root(agent_type) / "bindings.json"

    @classmethod
    def agent_skill_dir(cls, agent_type: str, slug: str) -> Path:
        return cls.agent_root(agent_type) / cls.slugify(slug)

    @classmethod
    def _build_paths(cls, base_dir: Path, file_name: str = "SKILL.md") -> Dict[str, str]:
        relative = base_dir.relative_to(cls.project_root()).as_posix()
        return {
            "storage_path": str(base_dir),
            "workspace_relative_path": relative,
            "workspace_file_path": f"{relative}/{file_name}",
            "workspace_skill_file": f"{relative}/{file_name}",
            "host_storage_path": cls._to_host_path(relative),
            "host_file_path": cls._to_host_path(f"{relative}/{file_name}"),
            "skill_root": str(base_dir),
            "skill_file_path": str(base_dir / file_name),
            "references_root": str(base_dir / "references"),
            "examples_root": str(base_dir / "examples"),
            "scripts_root": str(base_dir / "scripts"),
            "display_root": cls.display_root(),
        }

    @classmethod
    def _build_extension_manifest(cls, skill_dir: Path) -> List[Dict[str, Any]]:
        manifest: List[Dict[str, Any]] = []
        for folder in ("references", "examples", "scripts"):
            base = skill_dir / folder
            if not base.exists():
                continue
            for file in sorted(base.rglob("*")):
                if file.is_dir():
                    continue
                logical_name = file.relative_to(skill_dir).as_posix()
                manifest.append({"name": logical_name, "description": f"Imported from {logical_name}", "type": "file"})
        return manifest

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
    def _merge_json(cls, path: Path, updates: Dict[str, Any]) -> Dict[str, Any]:
        payload = cls._read_json(path, {})
        if not isinstance(payload, dict):
            payload = {}
        payload.update(updates)
        cls._write_json(path, payload)
        return payload

    @classmethod
    def _frontmatter_block(cls, metadata: Dict[str, Any]) -> str:
        lines = ["---"]
        for key in ("name", "description", "tags"):
            value = metadata.get(key)
            if value is None:
                continue
            if isinstance(value, list):
                serialized = ", ".join(json.dumps(item, ensure_ascii=False) for item in value)
                lines.append(f"{key}: [{serialized}]")
            else:
                lines.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")
        lines.append("---")
        return "\n".join(lines)

    @classmethod
    def _normalize_binding(
        cls,
        agent_type: str,
        slug: str,
        *,
        enabled: bool = True,
        always_include: bool = False,
        sort_order: int = 0,
        match_keywords: Optional[List[str]] = None,
        match_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized_slug = cls.slugify(slug)
        paths = cls._build_paths(cls.skill_dir(normalized_slug))
        return {
            "id": f"{agent_type}:{normalized_slug}",
            "skill_id": normalized_slug,
            "slug": normalized_slug,
            "agent_type": agent_type,
            "enabled": bool(enabled),
            "always_include": bool(always_include),
            "sort_order": int(sort_order),
            "match_keywords": [str(item) for item in (match_keywords or []) if str(item).strip()],
            "match_config": match_config or {},
            "bindings_file": str(cls.bindings_file(agent_type)),
            "skill_file": str(cls.skill_file(normalized_slug)),
            "workspace_relative_path": paths["workspace_relative_path"],
            "skill_root": paths["skill_root"],
        }

    @classmethod
    def _write_agent_binding_mirror(cls, binding: Dict[str, Any]) -> None:
        del binding
        return None

    @classmethod
    def _remove_agent_binding_mirror(cls, agent_type: str, slug: str) -> None:
        mirror_dir = cls.agent_root(agent_type) / cls.slugify(slug)
        if mirror_dir.exists():
            shutil.rmtree(mirror_dir)

    @classmethod
    def _collect_bindings_for_slug(cls, slug: str) -> List[Dict[str, Any]]:
        normalized_slug = cls.slugify(slug)
        items: List[Dict[str, Any]] = []
        for agent_type in AGENT_TYPES:
            payload = cls.get_agent_bindings(agent_type)
            for binding in payload.get("skills", []):
                if cls.slugify(binding.get("slug", "")) == normalized_slug:
                    items.append(binding)
        return sorted(items, key=lambda item: (item["agent_type"], item["sort_order"], item["slug"]))

    @classmethod
    def _read_skill_payload(cls, slug: str) -> Dict[str, Any]:
        normalized_slug = cls.slugify(slug)
        skill_dir = cls.library_root() / normalized_slug
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            raise FileNotFoundError(f"Skill '{normalized_slug}' not found")

        content = skill_file.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(content)
        raw_metadata = cls._read_json(cls.metadata_file(normalized_slug), {})
        if not isinstance(raw_metadata, dict):
            raw_metadata = {}
        nested_metadata = raw_metadata.get("metadata", {}) if isinstance(raw_metadata.get("metadata"), dict) else {}
        paths = cls._build_paths(skill_dir)
        extension_manifest = raw_metadata.get("extension_manifest") or cls._build_extension_manifest(skill_dir)
        metadata_json = {
            **nested_metadata,
            **paths,
            "folder_path": str(skill_dir),
            "bindings_file": str(cls.aggregated_bindings_file(normalized_slug)),
        }
        name = str(frontmatter.get("name") or raw_metadata.get("name") or normalized_slug)
        description = str(frontmatter.get("description") or raw_metadata.get("description") or "")
        tags = frontmatter.get("tags")
        if not isinstance(tags, list):
            tags = raw_metadata.get("tags", [])
        if not isinstance(tags, list):
            tags = []

        return {
            "id": normalized_slug,
            "name": name,
            "slug": normalized_slug,
            "description": description,
            "tags": [str(tag) for tag in tags],
            "source_type": str(raw_metadata.get("source_type", "manual")),
            "source_url": raw_metadata.get("source_url"),
            "metadata_json": metadata_json,
            "is_system": bool(raw_metadata.get("is_system", False)),
            "is_active": bool(raw_metadata.get("is_active", True)),
            "bindings": cls._collect_bindings_for_slug(normalized_slug),
            "folder_path": str(skill_dir),
            "skill_file": str(skill_file),
            "bindings_file": str(cls.aggregated_bindings_file(normalized_slug)),
            "content": content,
            "skill_body": body,
            "extension_manifest": extension_manifest,
            "extension_payload": raw_metadata.get("extension_payload", {}) or {},
            "installed_at": raw_metadata.get("installed_at"),
            "updated_at": raw_metadata.get("updated_at"),
        }

    @classmethod
    def _build_installed_record(cls, slug: str, existing: Dict[str, Any] | None = None) -> Dict[str, Any]:
        skill = cls._read_skill_payload(slug)
        existing = existing or {}
        bindings = skill.get("bindings", []) or []
        return {
            "slug": skill["slug"],
            "name": skill["name"],
            "source_type": skill.get("source_type", "manual"),
            "source_url": skill.get("source_url"),
            "skill_file": skill["skill_file"],
            "workspace_relative_path": skill["metadata_json"].get("workspace_relative_path"),
            "is_system": skill.get("is_system", False),
            "is_active": skill.get("is_active", True),
            "bound_agents": sorted({str(binding.get("agent_type")) for binding in bindings if binding.get("agent_type")}),
            "bindings": bindings,
            "installed_at": existing.get("installed_at") or skill.get("installed_at") or cls._timestamp(),
            "updated_at": cls._timestamp(),
        }

    @classmethod
    def _refresh_installed_index(cls) -> None:
        existing_payload = cls._read_json(cls.installed_skills_index_file(), {"skills": []})
        existing_by_slug = {
            str(item.get("slug")): item
            for item in existing_payload.get("skills", [])
            if isinstance(item, dict) and str(item.get("slug", "")).strip()
        }
        records = [cls._build_installed_record(slug, existing_by_slug.get(slug)) for slug in cls.list_skill_slugs()]
        cls._write_json(cls.installed_skills_index_file(), {"skills": sorted(records, key=lambda item: item["slug"])})

    @classmethod
    def _refresh_installed_index_record(cls, slug: str) -> None:
        normalized_slug = cls.slugify(slug)
        existing_payload = cls._read_json(cls.installed_skills_index_file(), {"skills": []})
        existing_items = existing_payload.get("skills", []) if isinstance(existing_payload, dict) else []
        existing_records = [item for item in existing_items if isinstance(item, dict)]
        existing_by_slug = {
            str(item.get("slug")): item
            for item in existing_records
            if str(item.get("slug", "")).strip()
        }
        records = [item for item in existing_records if item.get("slug") != normalized_slug]
        if (cls.library_root() / normalized_slug / "SKILL.md").exists():
            records.append(cls._build_installed_record(normalized_slug, existing_by_slug.get(normalized_slug)))
        cls._write_json(cls.installed_skills_index_file(), {"skills": sorted(records, key=lambda item: str(item.get("slug", "")))})

    @classmethod
    def sync_skill_runtime(cls, slug: str) -> None:
        normalized_slug = cls.slugify(slug)
        cls.ensure_agent_roots()
        cls._write_json(cls.aggregated_bindings_file(normalized_slug), {"skills": cls._collect_bindings_for_slug(normalized_slug)})
        cls._refresh_installed_index_record(normalized_slug)

    @classmethod
    def ensure_agent_roots(cls) -> None:
        cls.runtime_root()
        for agent in AGENT_TYPES:
            cls.ensure_agent_bindings(agent)

    @classmethod
    def ensure_agent_bindings(cls, agent_type: str) -> None:
        payload = cls._read_json(cls.bindings_file(agent_type), None)
        if not isinstance(payload, dict):
            payload = None
        if payload is None:
            cls._write_json(cls.bindings_file(agent_type), {"agent_type": agent_type, "skills": []})

    @classmethod
    def list_skill_slugs(cls) -> List[str]:
        slugs: List[str] = []
        for entry in sorted(cls.library_root().iterdir(), key=lambda item: item.name.lower()):
            if entry.is_dir() and entry.name not in {"agents", ".runtime"} and not entry.name.startswith(".") and (entry / "SKILL.md").exists():
                slugs.append(entry.name)
        return slugs

    @classmethod
    def list_skills(cls) -> List[Dict[str, Any]]:
        return [cls._read_skill_payload(slug) for slug in cls.list_skill_slugs()]

    @classmethod
    def read_skill(cls, slug: str) -> Dict[str, Any]:
        return cls._read_skill_payload(slug)

    @classmethod
    def write_skill(
        cls,
        *,
        slug: str,
        name: str,
        description: Optional[str],
        content: Optional[str],
        tags: Optional[List[str]],
        source_type: str = "manual",
        source_url: Optional[str] = None,
        metadata_json: Optional[Dict[str, Any]] = None,
        is_system: bool = False,
        is_active: bool = True,
        extension_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized_slug = cls.slugify(slug)
        skill_dir = cls.skill_dir(normalized_slug)
        frontmatter = {
            "name": name,
            "description": description or "",
            "tags": list(tags or []),
        }
        body = (content or "").strip()
        skill_text = f"{cls._frontmatter_block(frontmatter)}\n\n{body}\n"
        cls.skill_file(normalized_slug).write_text(skill_text, encoding="utf-8")

        existing_metadata = cls._read_json(cls.metadata_file(normalized_slug), {})
        installed_at = existing_metadata.get("installed_at") if isinstance(existing_metadata, dict) else None
        payload = {
            "name": name,
            "slug": normalized_slug,
            "description": description or "",
            "tags": list(tags or []),
            "source_type": source_type or "manual",
            "source_url": source_url,
            "metadata": metadata_json or {},
            "extension_manifest": cls._build_extension_manifest(skill_dir),
            "extension_payload": extension_payload or {},
            "is_system": bool(is_system),
            "is_active": bool(is_active),
            "installed_at": installed_at or cls._timestamp(),
            "updated_at": cls._timestamp(),
        }
        cls._write_json(cls.metadata_file(normalized_slug), payload)
        cls.sync_all()
        return cls.read_skill(normalized_slug)

    @classmethod
    def rename_skill(cls, current_slug: str, new_slug: str) -> Dict[str, Any]:
        current_slug = cls.slugify(current_slug)
        new_slug = cls.slugify(new_slug)
        if current_slug == new_slug:
            return cls.read_skill(new_slug)
        current_dir = cls.library_root() / current_slug
        if not current_dir.exists():
            raise FileNotFoundError(f"Skill '{current_slug}' not found")
        target_dir = cls.library_root() / new_slug
        if target_dir.exists():
            raise FileExistsError(f"Skill '{new_slug}' already exists")

        current_dir.rename(target_dir)
        metadata = cls._read_json(target_dir / "metadata.json", {})
        if isinstance(metadata, dict):
            metadata["slug"] = new_slug
            metadata["updated_at"] = cls._timestamp()
            cls._write_json(target_dir / "metadata.json", metadata)

        for agent_type in AGENT_TYPES:
            payload = cls.get_agent_bindings(agent_type)
            changed = False
            for item in payload.get("skills", []):
                if cls.slugify(item.get("slug", "")) == current_slug:
                    item.update(cls._normalize_binding(
                        agent_type,
                        new_slug,
                        enabled=bool(item.get("enabled", True)),
                        always_include=bool(item.get("always_include", False)),
                        sort_order=int(item.get("sort_order", 0)),
                        match_keywords=item.get("match_keywords", []),
                        match_config=item.get("match_config", {}),
                    ))
                    changed = True
            if changed:
                cls._write_json(cls.bindings_file(agent_type), payload)
            cls._remove_agent_binding_mirror(agent_type, current_slug)
            cls._remove_agent_binding_mirror(agent_type, new_slug)

        cls.sync_all()
        return cls.read_skill(new_slug)

    @classmethod
    def delete_skill(cls, slug: str) -> None:
        normalized_slug = cls.slugify(slug)
        skill_dir = cls.library_root() / normalized_slug
        if not skill_dir.exists() or not (skill_dir / "SKILL.md").exists():
            raise FileNotFoundError(f"Skill '{normalized_slug}' not found")

        for agent_type in AGENT_TYPES:
            payload = cls.get_agent_bindings(agent_type)
            skills = [
                item
                for item in payload.get("skills", [])
                if cls.slugify(item.get("slug", "")) != normalized_slug
            ]
            if len(skills) != len(payload.get("skills", [])):
                cls._write_json(cls.bindings_file(agent_type), {"agent_type": agent_type, "skills": skills})
            cls._remove_agent_binding_mirror(agent_type, normalized_slug)

        shutil.rmtree(skill_dir)
        cls.sync_all()

    @classmethod
    def get_agent_bindings(cls, agent_type: str) -> Dict[str, Any]:
        cls.ensure_agent_bindings(agent_type)
        payload = cls._read_json(cls.bindings_file(agent_type), {"agent_type": agent_type, "skills": []})
        skills: List[Dict[str, Any]] = []
        for item in payload.get("skills", []) if isinstance(payload, dict) else []:
            skills.append(
                cls._normalize_binding(
                    agent_type,
                    item.get("slug", ""),
                    enabled=bool(item.get("enabled", True)),
                    always_include=bool(item.get("always_include", False)),
                    sort_order=int(item.get("sort_order", 0)),
                    match_keywords=item.get("match_keywords", []),
                    match_config=item.get("match_config", {}),
                )
            )
        return {"agent_type": agent_type, "skills": sorted(skills, key=lambda item: (item["sort_order"], item["slug"]))}

    @classmethod
    def upsert_binding(
        cls,
        agent_type: str,
        slug: str,
        *,
        enabled: bool = True,
        always_include: bool = False,
        sort_order: int = 0,
        match_keywords: Optional[List[str]] = None,
        match_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized_slug = cls.slugify(slug)
        payload = cls.get_agent_bindings(agent_type)
        binding = cls._normalize_binding(
            agent_type,
            normalized_slug,
            enabled=enabled,
            always_include=always_include,
            sort_order=sort_order,
            match_keywords=match_keywords,
            match_config=match_config,
        )
        replaced = False
        for index, item in enumerate(payload["skills"]):
            if item["slug"] == normalized_slug:
                payload["skills"][index] = binding
                replaced = True
                break
        if not replaced:
            payload["skills"].append(binding)
        payload["skills"] = sorted(payload["skills"], key=lambda item: (item["sort_order"], item["slug"]))
        cls._write_json(cls.bindings_file(agent_type), payload)
        cls._remove_agent_binding_mirror(agent_type, normalized_slug)
        cls.sync_skill_runtime(normalized_slug)
        return binding

    @classmethod
    def update_binding(cls, agent_type: str, slug: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        current = next((item for item in cls.get_agent_bindings(agent_type)["skills"] if item["slug"] == cls.slugify(slug)), None)
        if current is None:
            raise FileNotFoundError(f"Binding '{agent_type}:{slug}' not found")
        return cls.upsert_binding(
            agent_type,
            slug,
            enabled=bool(updates.get("enabled", current["enabled"])),
            always_include=bool(updates.get("always_include", current["always_include"])),
            sort_order=int(updates.get("sort_order", current["sort_order"])),
            match_keywords=updates.get("match_keywords", current.get("match_keywords", [])),
            match_config=updates.get("match_config", current.get("match_config", {})),
        )

    @classmethod
    def delete_binding(cls, agent_type: str, slug: str, *, ignore_missing: bool = False) -> None:
        normalized_slug = cls.slugify(slug)
        payload = cls.get_agent_bindings(agent_type)
        new_items = [item for item in payload["skills"] if item["slug"] != normalized_slug]
        if len(new_items) == len(payload["skills"]) and not ignore_missing:
            raise FileNotFoundError(f"Binding '{agent_type}:{normalized_slug}' not found")
        payload["skills"] = new_items
        cls._write_json(cls.bindings_file(agent_type), payload)
        cls._remove_agent_binding_mirror(agent_type, normalized_slug)
        cls.sync_skill_runtime(normalized_slug)

    @classmethod
    def sync_all(cls) -> None:
        cls.ensure_agent_roots()
        for slug in cls.list_skill_slugs():
            cls._write_json(cls.aggregated_bindings_file(slug), {"skills": cls._collect_bindings_for_slug(slug)})
        cls._refresh_installed_index()

    @classmethod
    def _parse_github_skill_source(cls, repo_url: str) -> Dict[str, str]:
        parsed = urlparse(repo_url)
        if parsed.netloc not in {"github.com", "www.github.com", "raw.githubusercontent.com"}:
            raise ValueError("Only GitHub URLs are supported")

        if parsed.netloc == "raw.githubusercontent.com":
            parts = [part for part in parsed.path.strip("/").split("/") if part]
            if len(parts) < 4:
                raise ValueError("Invalid raw GitHub skill URL")
            owner, repo, ref = parts[:3]
            subpath = "/".join(parts[3:])
            if subpath.endswith("SKILL.md"):
                subpath = str(Path(subpath).parent).replace("\\", "/")
                if subpath == ".":
                    subpath = ""
            return {"owner": owner, "repo": repo, "ref": ref, "subpath": subpath}

        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) == 2:
            owner, repo = parts
            return {
                "owner": owner,
                "repo": repo.removesuffix(".git"),
                "ref": "",
                "subpath": "",
            }
        if len(parts) < 5 or parts[2] not in {"tree", "blob"}:
            raise ValueError("GitHub skill URL must point to a tree/blob path")
        owner, repo, _, ref = parts[:4]
        subpath = "/".join(parts[4:])
        if subpath.endswith("SKILL.md"):
            subpath = str(Path(subpath).parent).replace("\\", "/")
            if subpath == ".":
                subpath = ""
        return {"owner": owner, "repo": repo, "ref": ref, "subpath": subpath}

    @classmethod
    async def _resolve_github_default_branch(cls, owner: str, repo: str) -> str:
        api_url = f"https://api.github.com/repos/{owner}/{repo}"
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(api_url)
            response.raise_for_status()
            payload = response.json()
        default_branch = str(payload.get("default_branch") or "").strip()
        return default_branch or "main"

    @classmethod
    def _safe_extract_zip(cls, archive: zipfile.ZipFile, dest_dir: Path) -> Path:
        dest_root = dest_dir.resolve()
        top_levels = set()
        for info in archive.infolist():
            extracted_path = (dest_dir / info.filename).resolve()
            if extracted_path != dest_root and dest_root not in extracted_path.parents:
                raise ValueError("Downloaded archive contains files outside the destination")
            parts = Path(info.filename).parts
            if parts:
                top_levels.add(parts[0])
        archive.extractall(dest_dir)
        if len(top_levels) != 1:
            raise ValueError("Unexpected GitHub archive layout")
        return dest_dir / next(iter(top_levels))

    @classmethod
    def _safe_extract_skill_zip(cls, archive: zipfile.ZipFile, dest_dir: Path) -> Path:
        dest_root = dest_dir.resolve()
        dest_root.mkdir(parents=True, exist_ok=True)
        for info in archive.infolist():
            member_name = str(info.filename or "").replace("\\", "/").strip()
            if not member_name or member_name.endswith("/"):
                continue
            destination = (dest_root / member_name).resolve()
            if destination != dest_root and dest_root not in destination.parents:
                raise ValueError("Archive contains files outside the destination")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as src, open(destination, "wb") as dst:
                shutil.copyfileobj(src, dst)

        if (dest_root / "SKILL.md").is_file():
            return dest_root

        candidates = [
            path
            for path in dest_root.rglob("SKILL.md")
            if "__MACOSX" not in path.parts and path.is_file()
        ]
        if len(candidates) != 1:
            raise ValueError("Archive must contain exactly one SKILL.md file")
        return candidates[0].parent

    @classmethod
    async def _download_github_repo_zip(cls, owner: str, repo: str, ref: str) -> bytes:
        zip_url = f"https://codeload.github.com/{owner}/{repo}/zip/{ref}"
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(zip_url)
            response.raise_for_status()
            return response.content

    @classmethod
    def _install_skill_directory(
        cls,
        *,
        repo_root: Path,
        subpath: str,
        source_url: str,
        owner: str,
        repo: str,
        ref: str,
    ) -> Dict[str, Any]:
        safe_subpath = _safe_relative_path(subpath)
        source_dir = repo_root / safe_subpath if safe_subpath else repo_root
        if not source_dir.exists() or not source_dir.is_dir():
            raise FileNotFoundError(f"Skill path not found: {safe_subpath or '.'}")
        skill_file = source_dir / "SKILL.md"
        if not skill_file.exists() or not skill_file.is_file():
            raise ValueError("SKILL.md not found in selected skill directory")

        content = skill_file.read_text(encoding="utf-8")
        frontmatter, _ = parse_frontmatter(content)
        slug = cls.slugify(frontmatter.get("name") or source_dir.name or "imported-skill")
        destination = cls.library_root() / slug
        if destination.exists():
            raise FileExistsError(f"Skill '{slug}' already exists")

        shutil.copytree(source_dir, destination)
        tags = frontmatter.get("tags") if isinstance(frontmatter.get("tags"), list) else []
        existing_metadata = cls._read_json(destination / "metadata.json", {})
        installed_at = existing_metadata.get("installed_at") if isinstance(existing_metadata, dict) else None
        nested_metadata = existing_metadata.get("metadata", {}) if isinstance(existing_metadata, dict) else {}
        if not isinstance(nested_metadata, dict):
            nested_metadata = {}
        cls._merge_json(
            destination / "metadata.json",
            {
                "name": str(frontmatter.get("name") or slug),
                "slug": slug,
                "description": str(frontmatter.get("description") or ""),
                "tags": [str(tag) for tag in tags],
                "source_type": "github",
                "source_url": source_url,
                "metadata": {
                    **nested_metadata,
                    "imported_from": source_url,
                    "repo_slug": f"{owner}/{repo}",
                    "branch": ref,
                    "subdir": safe_subpath,
                },
                "extension_manifest": cls._build_extension_manifest(destination),
                "is_system": False,
                "is_active": True,
                "installed_at": installed_at or cls._timestamp(),
                "updated_at": cls._timestamp(),
            },
        )
        cls.sync_all()
        return cls.read_skill(slug)

    @classmethod
    async def import_github_skill(cls, repo_url: str) -> Dict[str, Any]:
        source = cls._parse_github_skill_source(repo_url)
        if not source["ref"]:
            source["ref"] = await cls._resolve_github_default_branch(source["owner"], source["repo"])
        with tempfile.TemporaryDirectory(prefix="auditai-skill-import-") as tmp_dir:
            archive_bytes = await cls._download_github_repo_zip(source["owner"], source["repo"], source["ref"])
            with zipfile.ZipFile(BytesIO(archive_bytes), "r") as archive:
                repo_root = cls._safe_extract_zip(archive, Path(tmp_dir))
            return cls._install_skill_directory(
                repo_root=repo_root,
                subpath=source["subpath"],
                source_url=repo_url,
                owner=source["owner"],
                repo=source["repo"],
                ref=source["ref"],
            )

    @classmethod
    def import_skill_zip(cls, zip_path: Path, original_filename: str) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="auditai-skill-upload-") as tmp_dir:
            extract_root = Path(tmp_dir) / "archive"
            with zipfile.ZipFile(zip_path, "r") as archive:
                source_dir = cls._safe_extract_skill_zip(archive, extract_root)

            skill_file = source_dir / "SKILL.md"
            content = skill_file.read_text(encoding="utf-8")
            frontmatter, _ = parse_frontmatter(content)
            slug = cls.slugify(frontmatter.get("name") or source_dir.name or Path(original_filename).stem or "uploaded-skill")
            destination = cls.library_root() / slug
            if destination.exists():
                raise FileExistsError(f"Skill '{slug}' already exists")

            shutil.copytree(source_dir, destination)
            tags = frontmatter.get("tags") if isinstance(frontmatter.get("tags"), list) else []
            existing_metadata = cls._read_json(destination / "metadata.json", {})
            installed_at = existing_metadata.get("installed_at") if isinstance(existing_metadata, dict) else None
            nested_metadata = existing_metadata.get("metadata", {}) if isinstance(existing_metadata, dict) else {}
            if not isinstance(nested_metadata, dict):
                nested_metadata = {}
            cls._merge_json(
                destination / "metadata.json",
                {
                    "name": str(frontmatter.get("name") or slug),
                    "slug": slug,
                    "description": str(frontmatter.get("description") or ""),
                    "tags": [str(tag) for tag in tags],
                    "source_type": "local_zip",
                    "source_url": original_filename,
                    "metadata": {
                        **nested_metadata,
                        "imported_from": original_filename,
                    },
                    "extension_manifest": cls._build_extension_manifest(destination),
                    "is_system": False,
                    "is_active": True,
                    "installed_at": installed_at or cls._timestamp(),
                    "updated_at": cls._timestamp(),
                },
            )
            cls.sync_all()
            return cls.read_skill(slug)
