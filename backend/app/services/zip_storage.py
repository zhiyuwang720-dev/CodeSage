"""
ZIP artifact storage helpers.

This module manages two separate assets for ZIP-based projects:
1. the optional original ZIP archive used for traceability
2. the persistent extracted source directory used by audits
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core.config import settings


def get_zip_storage_path() -> Path:
    path = Path(settings.ZIP_STORAGE_PATH).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_project_source_storage_path() -> Path:
    path = Path(settings.PROJECT_SOURCE_STORAGE_PATH).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_project_zip_path(project_id: str) -> Path:
    return get_zip_storage_path() / f"{project_id}.zip"


def get_project_zip_meta_path(project_id: str) -> Path:
    return get_zip_storage_path() / f"{project_id}.meta"


def get_managed_projects_root() -> Path:
    path = Path(settings.MANAGED_PROJECTS_ROOT).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_project_persistent_source_path(project_id: str) -> Path:
    return get_project_source_storage_path() / project_id


def _is_within_directory(base_dir: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(base_dir.resolve())
        return True
    except ValueError:
        return False


def _extract_zip_to_directory(zip_path: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            member_name = str(member.filename or "").strip()
            if not member_name or member_name.endswith("/"):
                continue
            destination = (target_dir / member_name).resolve()
            if not _is_within_directory(target_dir, destination):
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as src, open(destination, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _collapse_single_root_directory(target_dir: Path) -> None:
    entries = [entry for entry in target_dir.iterdir() if entry.name not in {".DS_Store", "__MACOSX"}]
    if len(entries) != 1 or not entries[0].is_dir():
        return

    root_dir = entries[0]
    temp_dir = target_dir.parent / f"{target_dir.name}.normalized"
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    for child in root_dir.iterdir():
        shutil.move(str(child), str(temp_dir / child.name))
    shutil.rmtree(target_dir, ignore_errors=True)
    temp_dir.rename(target_dir)


def _save_project_zip_sync(
    project_id: str,
    file_path: str,
    original_filename: str,
    *,
    import_status: str = "ready",
    keep_archive: bool = True,
) -> dict:
    target_path = get_project_zip_path(project_id)
    meta_path = get_project_zip_meta_path(project_id)

    shutil.copy2(file_path, target_path)
    file_size = os.path.getsize(target_path)
    meta = {
        "original_filename": original_filename,
        "file_size": file_size,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "project_id": project_id,
        "import_status": import_status,
        "import_error": None,
        "keep_archive": keep_archive,
    }
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump(meta, handle)
    return meta


async def save_project_zip(
    project_id: str,
    file_path: str,
    original_filename: str,
    *,
    import_status: str = "ready",
    keep_archive: bool = True,
) -> dict:
    return await asyncio.to_thread(
        _save_project_zip_sync,
        project_id,
        file_path,
        original_filename,
        import_status=import_status,
        keep_archive=keep_archive,
    )


def _materialize_project_source_from_zip_sync(project_id: str, file_path: str) -> dict[str, str]:
    source_root = get_project_persistent_source_path(project_id)
    if source_root.exists():
        shutil.rmtree(source_root, ignore_errors=True)
    source_root.mkdir(parents=True, exist_ok=True)
    _extract_zip_to_directory(Path(file_path).resolve(), source_root)
    _collapse_single_root_directory(source_root)
    return {
        "path": str(source_root.resolve()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


async def materialize_project_source_from_zip(project_id: str, file_path: str) -> dict[str, str]:
    return await asyncio.to_thread(_materialize_project_source_from_zip_sync, project_id, file_path)


async def load_project_zip(project_id: str) -> Optional[str]:
    zip_path = get_project_zip_path(project_id)
    if zip_path.exists():
        return str(zip_path)
    return None


async def get_project_zip_meta(project_id: str) -> Optional[dict]:
    meta_path = get_project_zip_meta_path(project_id)
    if not meta_path.exists():
        return None
    with open(meta_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _update_project_zip_meta_sync(project_id: str, **updates) -> dict:
    meta_path = get_project_zip_meta_path(project_id)
    if meta_path.exists():
        with open(meta_path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
    else:
        meta = {"project_id": project_id}
    meta.update(updates)
    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump(meta, handle)
    return meta


async def update_project_zip_meta(project_id: str, **updates) -> dict:
    return await asyncio.to_thread(_update_project_zip_meta_sync, project_id, **updates)


async def get_project_persistent_source_meta(project_id: str) -> Optional[dict[str, str]]:
    source_root = get_project_persistent_source_path(project_id)
    if not source_root.exists() or not source_root.is_dir():
        return None
    return {
        "path": str(source_root.resolve()),
        "updated_at": datetime.fromtimestamp(source_root.stat().st_mtime, tz=timezone.utc).isoformat(),
    }


def _delete_project_zip_sync(project_id: str) -> bool:
    zip_path = get_project_zip_path(project_id)
    meta_path = get_project_zip_meta_path(project_id)

    deleted = False
    if zip_path.exists():
        os.remove(zip_path)
        deleted = True
    if meta_path.exists():
        os.remove(meta_path)
    return deleted


async def delete_project_zip(project_id: str) -> bool:
    return await asyncio.to_thread(_delete_project_zip_sync, project_id)


def _delete_project_persistent_source_sync(project_id: str) -> bool:
    source_root = get_project_persistent_source_path(project_id)
    if not source_root.exists() or not source_root.is_dir():
        return False
    shutil.rmtree(source_root, ignore_errors=True)
    return True


async def delete_project_persistent_source(project_id: str) -> bool:
    return await asyncio.to_thread(_delete_project_persistent_source_sync, project_id)


async def has_project_zip(project_id: str) -> bool:
    return get_project_zip_path(project_id).exists()


async def has_project_persistent_source(project_id: str) -> bool:
    source_root = get_project_persistent_source_path(project_id)
    return source_root.exists() and source_root.is_dir()
