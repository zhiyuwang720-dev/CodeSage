from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.audit_rule import AuditRule, AuditRuleSet
from app.models.audit_session import AuditMemoryKind
from app.services.memory.runtime import RuntimeMemoryManager, build_memory_message


# 技能真内容位于 backend/skill_library/skill_library/{code-audit-finding,...}(嵌套包布局);
# AUDITAI_ASSET_ROOT 须指向 backend/skill_library,使 SkillFileService.library_root()
# 解析到含 code-audit-finding 的 skill_library/skill_library 一层。
WORKTREE_ROOT = Path(__file__).resolve().parents[2] / "skill_library"


def build_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_memory_manager_loads_instruction_and_recalled_memories(monkeypatch):
    monkeypatch.setenv("AUDITAI_ASSET_ROOT", str(WORKTREE_ROOT))
    session_factory = build_session_factory()
    with session_factory() as db:
        rule_set = AuditRuleSet(
            name="OWASP Top 10",
            description="Base security rules for finding agent.",
            language="python",
            rule_type="security",
            is_default=True,
            is_system=True,
            is_active=True,
        )
        db.add(rule_set)
        db.flush()
        db.add(
            AuditRule(
                rule_set_id=rule_set.id,
                rule_code="SEC001",
                name="Authorization bypass checks",
                description="Inspect ownership and tenant isolation.",
                category="security",
                severity="high",
                custom_prompt="Trace authz from controller to data access.",
                fix_suggestion="Bind resources to current principal.",
                enabled=True,
            )
        )
        db.commit()

    manager = RuntimeMemoryManager(session_factory=session_factory)
    bundle = __import__("asyncio").run(
        manager.preload(
            agent_type="review:security",
            system_prompt="Keep this prompt stable.",
            recon_payload={
                "summary": "FastAPI auth endpoints with tenant logic.",
                "project_info": {"languages": ["python"], "frameworks": ["fastapi"]},
                "project_profile": {"languages": ["python"], "frameworks": ["fastapi"]},
                "target_vulnerabilities": ["idor", "authorization"],
                "entry_points": ["/api/admin/users"],
            },
            user_message="Audit the FastAPI authorization flow for IDOR and auth bypass.",
            skill_context={"route_plan": {"primary_skill": "code-audit-finding"}},
        )
    )

    assert len(bundle.instructions) == 1
    assert bundle.instructions[0].memory_kind == AuditMemoryKind.INSTRUCTION.value
    assert "SEC001" in bundle.instructions[0].content
    assert bundle.recalls
    assert any(item.source_ref.endswith("references/languages/python.md") for item in bundle.recalls)
    assert any(item.source_ref.endswith("references/security/authentication_authorization.md") for item in bundle.recalls)
    rendered = build_memory_message(bundle.recalls[0])
    assert bundle.recalls[0].title in rendered
    assert bundle.recalls[0].source_ref in rendered


def test_memory_manager_loads_project_claude_and_claw_memories(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDITAI_ASSET_ROOT", str(WORKTREE_ROOT))
    (tmp_path / "CLAUDE.md").write_text("Project rule: focus auth flows.\n@include.md", encoding="utf-8")
    (tmp_path / "include.md").write_text("Included guidance for exploit proof.", encoding="utf-8")
    (tmp_path / ".claw").mkdir()
    (tmp_path / ".claw" / "CLAW.md").write_text("Claw memory: use canonical tools.", encoding="utf-8")
    (tmp_path / ".claude" / "rules").mkdir(parents=True)
    (tmp_path / ".claude" / "rules" / "java.md").write_text("Check deserialization and SpEL entrypoints.", encoding="utf-8")

    manager = RuntimeMemoryManager(session_factory=build_session_factory())
    bundle = __import__("asyncio").run(
        manager.preload(
            agent_type="review:security",
            system_prompt="Stay aligned.",
            recon_payload={
                "project_info": {"root": str(tmp_path), "languages": ["java"]},
                "summary": "Spring Boot controllers and serializers.",
                "target_vulnerabilities": ["deserialization"],
            },
            user_message="Audit this project for deserialization issues.",
            skill_context={"route_plan": {"primary_skill": "code-audit-finding"}},
        )
    )

    project_memories = [item for item in bundle.instructions if item.source_type == "project_memory"]

    assert project_memories
    assert any(item.source_ref == "CLAUDE.md" for item in project_memories)
    assert any(item.source_ref == ".claw/CLAW.md" for item in project_memories)
    assert any(item.source_ref == ".claude/rules/java.md" for item in project_memories)
    assert any("Included guidance" in item.content for item in project_memories if item.source_ref == "CLAUDE.md")
