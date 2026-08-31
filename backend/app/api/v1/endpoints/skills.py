import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from app.api import deps
from app.models.user import User
from app.schemas.skill import (
    AgentSkillBindingCreate,
    AgentSkillBindingResponse,
    AgentSkillBindingUpdate,
    SkillCreate,
    SkillImportRequest,
    SkillListResponse,
    SkillMetadataResponse,
    SkillResponse,
    SkillUpdate,
)
from app.services.agent.skill_service import SkillService
from app.services.init_agent_assets import init_agent_assets
from app.services.skill_file_service import SkillFileService

router = APIRouter()


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9\-_]+", "-", value)
    return value.strip("-") or "skill"


def _binding_response(binding: dict[str, Any]) -> AgentSkillBindingResponse:
    return AgentSkillBindingResponse(**binding)


def _skill_metadata_response(skill: dict[str, Any]) -> SkillMetadataResponse:
    return SkillMetadataResponse(
        id=skill["id"],
        name=skill["name"],
        slug=skill["slug"],
        description=skill["description"],
        tags=skill.get("tags", []),
        source_type=skill.get("source_type", "manual"),
        source_url=skill.get("source_url"),
        metadata_json=skill.get("metadata_json", {}),
        is_system=skill.get("is_system", False),
        is_active=skill.get("is_active", True),
        bindings=[_binding_response(item) for item in skill.get("bindings", [])],
        folder_path=skill.get("folder_path"),
        skill_file=skill.get("skill_file"),
        bindings_file=skill.get("bindings_file"),
        created_by=None,
        created_at=None,
        updated_at=None,
    )


def _skill_response(skill: dict[str, Any]) -> SkillResponse:
    payload = _skill_metadata_response(skill).model_dump()
    return SkillResponse(
        **payload,
        content=skill.get("content"),
        extension_manifest=skill.get("extension_manifest", []),
        extension_payload=skill.get("extension_payload", {}),
    )


def _binding_from_id(skill_slug: str, binding_id: str) -> dict[str, Any]:
    skill = SkillFileService.read_skill(skill_slug)
    binding = next((item for item in skill.get("bindings", []) if item.get("id") == binding_id), None)
    if binding is None:
        raise HTTPException(status_code=404, detail="Binding not found")
    return binding


@router.get("", response_model=SkillListResponse)
async def list_skills(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    agent_type: Optional[str] = None,
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    del current_user
    items = SkillFileService.list_skills()
    if agent_type:
        items = [item for item in items if any(binding["agent_type"] == agent_type for binding in item.get("bindings", []))]
    total = len(items)
    sliced = items[skip : skip + limit]
    return SkillListResponse(items=[_skill_metadata_response(item) for item in sliced], total=total)


@router.get("/{skill_id}", response_model=SkillResponse)
async def get_skill(
    skill_id: str,
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    del current_user
    try:
        return _skill_response(SkillFileService.read_skill(skill_id))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("", response_model=SkillResponse)
async def create_skill(
    skill_in: SkillCreate,
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    del current_user
    slug = _slugify(skill_in.slug or skill_in.name)
    if slug in SkillFileService.list_skill_slugs():
        raise HTTPException(status_code=400, detail="Skill slug already exists")

    skill = SkillFileService.write_skill(
        slug=slug,
        name=skill_in.name,
        description=skill_in.description,
        content=skill_in.content,
        tags=skill_in.tags,
        source_type=skill_in.source_type,
        source_url=skill_in.source_url,
        metadata_json=skill_in.metadata_json,
        is_system=skill_in.is_system,
        is_active=skill_in.is_active,
        extension_payload=skill_in.extension_payload if isinstance(skill_in.extension_payload, dict) else {},
    )
    for binding in skill_in.bindings:
        SkillFileService.upsert_binding(
            binding.agent_type,
            slug,
            enabled=binding.enabled,
            always_include=binding.always_include,
            sort_order=binding.sort_order,
            match_keywords=binding.match_keywords,
            match_config=binding.match_config,
        )
    return _skill_response(SkillFileService.read_skill(slug))


@router.post("/import-github", response_model=SkillResponse)
async def import_github_skill(
    request: SkillImportRequest,
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    del current_user
    try:
        skill = await SkillService.import_github_skill(request.repo_url)
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if request.bind_to_agent and request.agent_type:
        SkillFileService.upsert_binding(
            request.agent_type,
            skill["slug"],
            enabled=request.enabled,
            always_include=request.always_include,
            match_keywords=request.match_keywords,
            sort_order=0,
            match_config={},
        )
    return _skill_response(SkillFileService.read_skill(skill["slug"]))


@router.post("/upload-zip", response_model=SkillResponse)
async def upload_skill_zip(
    file: UploadFile = File(...),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    del current_user
    if not str(file.filename or "").lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Please upload a ZIP file")

    temp_file_handle = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    temp_file_path = temp_file_handle.name
    temp_file_handle.close()

    try:
        with open(temp_file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        file_size = os.path.getsize(temp_file_path)
        if file_size > 100 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="ZIP file size cannot exceed 100MB")

        skill = SkillFileService.import_skill_zip(Path(temp_file_path), str(file.filename or "skill.zip"))
        return _skill_response(skill)
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        try:
            os.unlink(temp_file_path)
        except OSError:
            pass


@router.put("/{skill_id}", response_model=SkillResponse)
async def update_skill(
    skill_id: str,
    skill_in: SkillUpdate,
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    del current_user
    try:
        current = SkillFileService.read_skill(skill_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    updates = skill_in.model_dump(exclude_unset=True)
    new_slug = _slugify(updates.get("slug") or current["slug"])
    if new_slug != current["slug"]:
        SkillFileService.rename_skill(current["slug"], new_slug)
        current = SkillFileService.read_skill(new_slug)

    updated = SkillFileService.write_skill(
        slug=new_slug,
        name=updates.get("name", current["name"]),
        description=updates.get("description", current["description"]),
        content=updates.get("content", current["skill_body"]),
        tags=updates.get("tags", current.get("tags", [])),
        source_type=updates.get("source_type", current.get("source_type", "manual")),
        source_url=updates.get("source_url", current.get("source_url")),
        metadata_json={**current.get("metadata_json", {}), **(updates.get("metadata_json") or {})},
        is_system=updates.get("is_system", current.get("is_system", False)),
        is_active=updates.get("is_active", current.get("is_active", True)),
        extension_payload=updates.get("extension_payload", current.get("extension_payload", {})),
    )
    return _skill_response(updated)


@router.delete("/{skill_id}")
async def delete_skill(
    skill_id: str,
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    del current_user
    try:
        SkillFileService.delete_skill(skill_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/{skill_id}/bindings", response_model=AgentSkillBindingResponse)
async def create_skill_binding(
    skill_id: str,
    binding_in: AgentSkillBindingCreate,
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    del current_user
    if skill_id not in SkillFileService.list_skill_slugs():
        raise HTTPException(status_code=404, detail="Skill not found")
    binding = SkillFileService.upsert_binding(
        binding_in.agent_type,
        skill_id,
        enabled=binding_in.enabled,
        always_include=binding_in.always_include,
        sort_order=binding_in.sort_order,
        match_keywords=binding_in.match_keywords,
        match_config=binding_in.match_config,
    )
    return _binding_response(binding)


@router.put("/{skill_id}/bindings/{binding_id}", response_model=AgentSkillBindingResponse)
async def update_skill_binding(
    skill_id: str,
    binding_id: str,
    binding_in: AgentSkillBindingUpdate,
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    del current_user
    binding = _binding_from_id(skill_id, binding_id)
    updated = SkillFileService.update_binding(binding["agent_type"], skill_id, binding_in.model_dump(exclude_unset=True))
    return _binding_response(updated)


@router.delete("/{skill_id}/bindings/{binding_id}")
async def delete_skill_binding(
    skill_id: str,
    binding_id: str,
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    del current_user
    binding = _binding_from_id(skill_id, binding_id)
    SkillFileService.delete_binding(binding["agent_type"], skill_id)
    return {"ok": True}


@router.post("/resync", response_model=SkillListResponse)
async def resync_skills(
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    del current_user
    await init_agent_assets()
    SkillFileService.sync_all()
    items = SkillFileService.list_skills()
    return SkillListResponse(items=[_skill_metadata_response(item) for item in items], total=len(items))
