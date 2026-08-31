from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api import deps
from app.models.user import User
from app.schemas.report_template import (
    ReportTemplateCreate,
    ReportTemplateListResponse,
    ReportTemplateResponse,
    ReportTemplateUpdate,
)
from app.services.init_agent_assets import init_report_templates
from app.services.report_template_file_service import ReportTemplateFileService

router = APIRouter()


def _to_response(template: dict[str, Any]) -> ReportTemplateResponse:
    return ReportTemplateResponse(**template)


@router.get("", response_model=ReportTemplateListResponse)
async def list_report_templates(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    del current_user
    items = ReportTemplateFileService.list_templates()
    total = len(items)
    sliced = items[skip : skip + limit]
    return ReportTemplateListResponse(items=[_to_response(item) for item in sliced], total=total)


@router.post("", response_model=ReportTemplateResponse)
async def create_report_template(
    template_in: ReportTemplateCreate,
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    del current_user
    slug = ReportTemplateFileService.slugify(template_in.slug or template_in.name)
    if template_in.is_default:
        ReportTemplateFileService.clear_default_flags()
    template = ReportTemplateFileService.write_template(
        slug=slug,
        name=template_in.name,
        description=template_in.description,
        content=template_in.content,
        report_type=template_in.report_type,
        output_format=template_in.output_format,
        variables=template_in.variables,
        metadata_json=template_in.metadata_json,
        is_default=template_in.is_default,
        is_system=template_in.is_system,
        is_active=template_in.is_active,
        sort_order=template_in.sort_order,
    )
    return _to_response(template)


@router.put("/{template_id}", response_model=ReportTemplateResponse)
async def update_report_template(
    template_id: str,
    template_in: ReportTemplateUpdate,
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    del current_user
    try:
        current = ReportTemplateFileService.read_template(template_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="Report template not found") from exc

    updates = template_in.model_dump(exclude_unset=True)
    new_slug = ReportTemplateFileService.slugify(updates.get("slug") or current["slug"])
    if new_slug != current["slug"]:
        ReportTemplateFileService.rename_template(current["slug"], new_slug)
        current = ReportTemplateFileService.read_template(new_slug)

    if updates.get("is_default"):
        ReportTemplateFileService.clear_default_flags(exclude_slug=new_slug)

    template = ReportTemplateFileService.write_template(
        slug=new_slug,
        name=updates.get("name", current["name"]),
        description=updates.get("description", current.get("description")),
        content=updates.get("content", current["content"]),
        report_type=updates.get("report_type", current["report_type"]),
        output_format=updates.get("output_format", current["output_format"]),
        variables=updates.get("variables", current.get("variables", {})),
        metadata_json={**current.get("metadata_json", {}), **(updates.get("metadata_json") or {})},
        is_default=updates.get("is_default", current.get("is_default", False)),
        is_system=updates.get("is_system", current.get("is_system", False)),
        is_active=updates.get("is_active", current.get("is_active", True)),
        sort_order=updates.get("sort_order", current.get("sort_order", 0)),
    )
    return _to_response(template)


@router.delete("/{template_id}")
async def delete_report_template(
    template_id: str,
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    del current_user
    try:
        current = ReportTemplateFileService.read_template(template_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="Report template not found") from exc
    if current.get("is_system"):
        raise HTTPException(status_code=403, detail="System templates cannot be deleted")
    ReportTemplateFileService.delete_template(template_id)
    return {"ok": True}


@router.post("/resync", response_model=ReportTemplateListResponse)
async def resync_report_templates(
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    del current_user
    await init_report_templates()
    items = ReportTemplateFileService.list_templates()
    return ReportTemplateListResponse(items=[_to_response(item) for item in items], total=len(items))
