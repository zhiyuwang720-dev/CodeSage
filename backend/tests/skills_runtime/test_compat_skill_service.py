import json

import pytest

from app.services.agent.skill_service import SkillService
from app.services.skill_file_service import SkillFileService


def _write_skill(library_root, slug: str, *, tags=None, description="Demo skill"):
    skill_dir = library_root / slug
    (skill_dir / "references" / "core").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {slug}\n"
        f"description: {description}\n"
        f"tags: [{', '.join(tags or [])}]\n"
        "---\n\n"
        f"# {slug}\n",
        encoding="utf-8",
    )
    (skill_dir / "references" / "core" / "guide.md").write_text("guide", encoding="utf-8")


@pytest.fixture
def runtime_skill_library(tmp_path, monkeypatch):
    project_root = tmp_path
    library_root = project_root / "skill_library"
    (library_root / "agents" / "finding").mkdir(parents=True)

    _write_skill(library_root, "alpha", tags=["auth"])
    _write_skill(library_root, "ai-security", tags=["llm", "prompt"])
    _write_skill(library_root, "beta", tags=["files"])

    (library_root / "agents" / "finding" / "bindings.json").write_text(
        json.dumps(
            {
                "agent_type": "finding",
                "skills": [
                    {
                        "slug": "alpha",
                        "enabled": True,
                        "always_include": True,
                        "sort_order": 0,
                        "match_keywords": ["idor"],
                    },
                    {
                        "slug": "ai-security",
                        "enabled": True,
                        "always_include": False,
                        "sort_order": 1,
                        "match_keywords": ["llm"],
                        "match_config": {
                            "frameworks": ["langchain"],
                            "path_keywords": ["rag"],
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(SkillFileService, "library_root", classmethod(lambda cls: library_root))
    monkeypatch.setattr(SkillFileService, "project_root", classmethod(lambda cls: project_root))
    return library_root


@pytest.mark.asyncio
async def test_skill_service_resolve_agent_skills_uses_runtime(runtime_skill_library):
    skill_context = await SkillService.resolve_agent_skills(
        None,
        "finding",
        {
            "task": "audit tenant idor flow",
            "task_context": "focus on authz",
            "config": {},
            "project_info": {"languages": ["Python"], "frameworks": ["FastAPI"]},
        },
    )

    assert [item["slug"] for item in skill_context["metadata"]] == ["alpha", "ai-security"]
    assert [item["slug"] for item in skill_context["matched"]] == ["alpha"]
    assert "<available_skills>" in skill_context["prompt"]
    assert "<name>alpha</name>" in skill_context["prompt"]
    assert "<skill_file_path>" in skill_context["prompt"]
    assert "<references_root>" in skill_context["prompt"]
    assert "<progressive_loading>" in skill_context["prompt"]
    assert skill_context["route_plan"]["primary_skill"] == "alpha"
    assert skill_context["route_plan"]["secondary_skills"] == []
    assert skill_context["route_plan"]["startup_reads"][0].replace("\\", "/").endswith("skill_library/alpha/SKILL.md")
    assert skill_context["route_plan"]["deferred_skills"] == ["ai-security"]


@pytest.mark.asyncio
async def test_skill_service_metadata_preserves_binding_fields(runtime_skill_library):
    metadata = await SkillService.list_agent_skill_metadata(None, "finding")

    assert len(metadata) == 2
    assert metadata[0]["slug"] == "alpha"
    assert metadata[0]["always_include"] is True
    assert metadata[0]["match_keywords"] == ["idor"]
    assert metadata[0]["binding_id"] == "finding:alpha"
    assert metadata[0]["paths"]["skill_file_path"].replace("\\", "/").endswith("skill_library/alpha/SKILL.md")
    assert metadata[0]["paths"]["references_root"].replace("\\", "/").endswith("skill_library/alpha/references")


@pytest.mark.asyncio
async def test_skill_service_route_plan_matches_ai_skill_only_for_ai_context(runtime_skill_library):
    skill_context = await SkillService.resolve_agent_skills(
        None,
        "finding",
        {
            "task": "audit llm prompt injection and rag data leakage",
            "task_context": "langchain based ai assistant",
            "config": {},
            "project_info": {"languages": ["Python"], "frameworks": ["LangChain"]},
            "recon_data": {
                "priority_paths": ["app/rag/pipeline.py"],
                "summary": "llm rag service with vector retrieval",
            },
        },
    )

    assert [item["slug"] for item in skill_context["matched"]] == ["alpha", "ai-security"]
    assert skill_context["route_plan"]["primary_skill"] == "alpha"
    assert skill_context["route_plan"]["secondary_skills"] == ["ai-security"]
    assert skill_context["route_plan"]["deferred_skills"] == ["ai-security"]


@pytest.mark.asyncio
async def test_skill_service_body_and_resource_reads_are_binding_aware(runtime_skill_library):
    body = await SkillService.get_skill_body(None, "alpha", agent_type="finding")
    resource = await SkillService.get_skill_resource(None, "alpha", "references/core/guide.md", agent_type="finding")

    assert body["slug"] == "alpha"
    assert resource["content"] == "guide"

    with pytest.raises(ValueError, match="not enabled"):
        await SkillService.get_skill_body(None, "beta", agent_type="finding")
