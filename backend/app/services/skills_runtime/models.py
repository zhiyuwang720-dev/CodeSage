from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(slots=True)
class SkillEntry:
    slug: str
    name: str
    description: str
    skill_file: str
    folder_path: str
    tags: List[str] = field(default_factory=list)
    when_to_use: Optional[str] = None
    allowed_tools: List[str] = field(default_factory=list)
    argument_hint: Optional[str] = None
    argument_names: List[str] = field(default_factory=list)
    version: Optional[str] = None
    model: Optional[str] = None
    disable_model_invocation: bool = False
    user_invocable: bool = True
    execution_context: Optional[str] = None
    agent: Optional[str] = None
    effort: Optional[str] = None
    shell: Optional[Dict[str, Any]] = None
    hooks: Dict[str, Any] = field(default_factory=dict)
    paths: List[str] = field(default_factory=list)
    frontmatter: Dict[str, Any] = field(default_factory=dict)
    metadata_json: Dict[str, Any] = field(default_factory=dict)
    source_type: str = "manual"
    source_url: Optional[str] = None
    content: str = ""
    skill_body: str = ""
    extension_manifest: List[Dict[str, Any]] = field(default_factory=list)
    is_system: bool = False
    is_active: bool = True


@dataclass(slots=True)
class SkillBinding:
    agent_type: str
    slug: str
    enabled: bool = True
    always_include: bool = False
    sort_order: int = 0
    match_keywords: List[str] = field(default_factory=list)
    match_config: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SkillRoutePlan:
    primary_skill: Optional[str] = None
    secondary_skills: List[str] = field(default_factory=list)
    mandatory_reads: List[str] = field(default_factory=list)
    recommended_reads: List[str] = field(default_factory=list)
    selection_reason: List[str] = field(default_factory=list)
    startup_reads: List[str] = field(default_factory=list)
    deferred_skills: List[str] = field(default_factory=list)
    deferred_skill_reads: List[str] = field(default_factory=list)


@dataclass(slots=True)
class SkillPromptState:
    entries: List[SkillEntry] = field(default_factory=list)
    matched: List[SkillEntry] = field(default_factory=list)
    prompt: str = ""
    route_plan: SkillRoutePlan = field(default_factory=SkillRoutePlan)


@dataclass(slots=True)
class SkillSnapshot:
    prompt: str = ""
    skills: List[str] = field(default_factory=list)
