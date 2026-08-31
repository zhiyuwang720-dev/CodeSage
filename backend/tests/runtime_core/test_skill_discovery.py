from __future__ import annotations

from app.services.runtime_core.session_state import SessionRuntimeState
from app.services.runtime_core.skill_discovery import SkillDiscoveryScheduler


CODE_AUDIT_SKILL = {
    "id": "code-audit",
    "slug": "code-audit",
    "name": "Code Audit",
    "description": "General code security audit skill",
    "tags": ["security", "audit"],
    "always_include": False,
    "match_keywords": ["audit", "vulnerability"],
    "skill_metadata": {
        "frontmatter": {
            "when_to_use": "Use when auditing source code for vulnerabilities.",
            "paths": ["src/auth", "src/web"],
        }
    },
    "paths": {
        "skill_file_path": "skill_library/code-audit/SKILL.md",
    },
}

AI_AUDIT_SKILL = {
    "id": "ai-audit",
    "slug": "ai-audit",
    "name": "AI Component Audit",
    "description": "Audit agent, RAG, MCP, prompt orchestration, tool execution, and LLM workflows.",
    "tags": ["ai", "llm", "rag", "agent"],
    "always_include": False,
    "match_keywords": ["llm", "rag", "agent", "mcp"],
    "skill_metadata": {
        "frontmatter": {
            "when_to_use": "Use when the project includes agents, RAG, prompt orchestration, MCP, or tool calling.",
            "paths": ["src/ai", "src/agents"],
        }
    },
    "paths": {
        "skill_file_path": "skill_library/ai-audit/SKILL.md",
    },
}

REPORT_SKILL = {
    "id": "cve-report-writer",
    "slug": "cve-report-writer",
    "name": "CVE Report Writer",
    "description": "Generate vulnerability disclosure and CVE style report drafts.",
    "tags": ["report", "cve", "disclosure"],
    "always_include": False,
    "match_keywords": ["report", "cve", "disclosure"],
    "skill_metadata": {
        "frontmatter": {
            "when_to_use": "Use when the user wants to write or refine a vulnerability report.",
            "paths": ["reports", "findings"],
        }
    },
    "paths": {
        "skill_file_path": "skill_library/cve-report-writer/SKILL.md",
    },
}


def test_discovery_scheduler_prioritizes_directly_named_skill():
    scheduler = SkillDiscoveryScheduler()
    runtime_state = SessionRuntimeState(session_id="session-1")

    snapshot = scheduler.discover(
        agent_type="finding",
        runtime_state=runtime_state,
        available_skills=[CODE_AUDIT_SKILL, REPORT_SKILL],
        matched_skills=[CODE_AUDIT_SKILL],
        task="please use code audit to inspect the auth logic",
        latest_user_message="please use code audit to inspect the auth logic",
        recon_payload={"summary": "auth flow review"},
    )

    assert snapshot["selected_skill"] == "code-audit"
    assert snapshot["ranked_candidates"][0]["skill_ref"] == "code-audit"
    assert "direct_user_mention" in snapshot["ranked_candidates"][0]["trigger_reasons"]
    assert snapshot["ranked_candidates"][0]["suggested_stage"] == "body"
    assert snapshot["ranked_candidates"][0]["invocation_mode"] == "auto_expand"


def test_discovery_scheduler_auto_selects_ai_skill_for_ai_project_context():
    scheduler = SkillDiscoveryScheduler()
    runtime_state = SessionRuntimeState(session_id="session-2")

    snapshot = scheduler.discover(
        agent_type="finding",
        runtime_state=runtime_state,
        available_skills=[CODE_AUDIT_SKILL, AI_AUDIT_SKILL],
        matched_skills=[CODE_AUDIT_SKILL, AI_AUDIT_SKILL],
        task="continue the audit",
        latest_user_message="continue the audit",
        recon_payload={
            "summary": "LangChain agent uses MCP tools and vector retrieval for RAG answers.",
            "project_info": {"frameworks": ["langchain"], "name": "agent-demo"},
        },
    )

    assert snapshot["selected_skill"] == "ai-audit"
    assert snapshot["ranked_candidates"][0]["skill_ref"] == "ai-audit"
    assert "ai_context_alignment" in snapshot["ranked_candidates"][0]["trigger_reasons"]
    assert snapshot["ranked_candidates"][0]["score"] > snapshot["ranked_candidates"][1]["score"]


def test_discovery_scheduler_prioritizes_report_skill_for_report_request():
    scheduler = SkillDiscoveryScheduler()
    runtime_state = SessionRuntimeState(session_id="session-3")
    runtime_state.mark_skill_invoked(agent_type="finding", skill_ref="code-audit", skill_stage="references")

    snapshot = scheduler.discover(
        agent_type="finding",
        runtime_state=runtime_state,
        available_skills=[CODE_AUDIT_SKILL, REPORT_SKILL],
        matched_skills=[CODE_AUDIT_SKILL, REPORT_SKILL],
        task="generate the vulnerability report now",
        latest_user_message="use the report writer skill to draft the CVE report",
        recon_payload={"summary": "finding confirmed"},
    )

    assert snapshot["selected_skill"] == "cve-report-writer"
    assert snapshot["ranked_candidates"][0]["skill_ref"] == "cve-report-writer"
    assert "report_request_alignment" in snapshot["ranked_candidates"][0]["trigger_reasons"]


def test_discovery_scheduler_suppresses_saturated_skills_without_new_signal():
    scheduler = SkillDiscoveryScheduler()
    runtime_state = SessionRuntimeState(session_id="session-4")
    runtime_state.mark_skill_invoked(agent_type="finding", skill_ref="ai-audit", skill_stage="scripts")

    snapshot = scheduler.discover(
        agent_type="finding",
        runtime_state=runtime_state,
        available_skills=[CODE_AUDIT_SKILL, AI_AUDIT_SKILL],
        matched_skills=[CODE_AUDIT_SKILL, AI_AUDIT_SKILL],
        task="audit the authentication code",
        latest_user_message="inspect the auth handlers for bypasses",
        recon_payload={"summary": "authentication service"},
    )

    top = snapshot["ranked_candidates"][0]
    ai_candidate = next(item for item in snapshot["ranked_candidates"] if item["skill_ref"] == "ai-audit")

    assert top["skill_ref"] == "code-audit"
    assert ai_candidate["freshness"] == "saturated"
    assert "saturation_penalty" in ai_candidate["trigger_reasons"]
    assert ai_candidate["score"] < top["score"]