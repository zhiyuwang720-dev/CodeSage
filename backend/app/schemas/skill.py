from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AgentSkillBindingBase(BaseModel):
    agent_type: str = Field(..., description="Target agent type")
    enabled: bool = Field(True, description="Whether this binding is enabled")
    always_include: bool = Field(False, description="Always inject this skill metadata at agent startup")
    sort_order: int = Field(0, description="Binding order inside the agent")
    match_keywords: List[str] = Field(default_factory=list, description="Keywords used to decide whether the skill body should be loaded later")
    match_config: Dict[str, Any] = Field(default_factory=dict, description="Reserved binding config")


class AgentSkillBindingCreate(AgentSkillBindingBase):
    pass


class AgentSkillBindingUpdate(BaseModel):
    enabled: Optional[bool] = None
    always_include: Optional[bool] = None
    sort_order: Optional[int] = None
    match_keywords: Optional[List[str]] = None
    match_config: Optional[Dict[str, Any]] = None


class AgentSkillBindingResponse(AgentSkillBindingBase):
    id: str
    skill_id: str
    bindings_file: Optional[str] = None
    skill_file: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class SkillBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    slug: str = Field(..., min_length=1, max_length=160)
    description: str = Field(..., min_length=1)
    source_type: str = Field("manual", description="manual/local/github")
    source_url: Optional[str] = None
    content: Optional[str] = None
    metadata_json: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)
    extension_manifest: List[Dict[str, Any]] = Field(default_factory=list)
    extension_payload: Dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    is_system: bool = False


class SkillCreate(SkillBase):
    bindings: List[AgentSkillBindingCreate] = Field(default_factory=list)


class SkillUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    source_type: Optional[str] = None
    source_url: Optional[str] = None
    content: Optional[str] = None
    metadata_json: Optional[Dict[str, Any]] = None
    tags: Optional[List[str]] = None
    extension_manifest: Optional[List[Dict[str, Any]]] = None
    extension_payload: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    is_system: Optional[bool] = None


class SkillMetadataResponse(BaseModel):
    id: str
    name: str
    slug: str
    description: str
    tags: List[str] = Field(default_factory=list)
    source_type: str
    source_url: Optional[str] = None
    metadata_json: Dict[str, Any] = Field(default_factory=dict)
    is_system: bool = False
    is_active: bool = True
    bindings: List[AgentSkillBindingResponse] = Field(default_factory=list)
    folder_path: Optional[str] = None
    skill_file: Optional[str] = None
    bindings_file: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class SkillResponse(SkillMetadataResponse):
    content: Optional[str] = None
    extension_manifest: List[Dict[str, Any]] = Field(default_factory=list)
    extension_payload: Dict[str, Any] = Field(default_factory=dict)


class SkillListResponse(BaseModel):
    items: List[SkillMetadataResponse]
    total: int


class SkillImportRequest(BaseModel):
    repo_url: str
    agent_type: Optional[str] = None
    bind_to_agent: bool = True
    enabled: bool = True
    always_include: bool = False
    match_keywords: List[str] = Field(default_factory=list)
