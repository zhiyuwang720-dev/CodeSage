from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ReportTemplateBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = None
    report_type: str = Field("final_vulnerability_report")
    output_format: str = Field("markdown")
    content: str = Field(..., min_length=1)
    variables: Dict[str, Any] = Field(default_factory=dict)
    metadata_json: Dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    sort_order: int = 0


class ReportTemplateCreate(ReportTemplateBase):
    slug: Optional[str] = None
    is_default: bool = False
    is_system: bool = False


class ReportTemplateUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    report_type: Optional[str] = None
    output_format: Optional[str] = None
    content: Optional[str] = None
    variables: Optional[Dict[str, Any]] = None
    metadata_json: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None
    is_default: Optional[bool] = None
    is_system: Optional[bool] = None


class ReportTemplateResponse(ReportTemplateBase):
    id: str
    slug: str
    is_default: bool = False
    is_system: bool = False
    folder_path: Optional[str] = None
    template_file: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ReportTemplateListResponse(BaseModel):
    items: List[ReportTemplateResponse]
    total: int


class AgentTaskReportResponse(BaseModel):
    task_id: str
    template_id: Optional[str] = None
    output_format: str
    title: Optional[str] = None
    content: str
    report_json: Optional[Dict[str, Any]] = None
    report_metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
