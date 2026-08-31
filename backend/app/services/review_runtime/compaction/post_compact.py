from __future__ import annotations
from copy import deepcopy
from typing import Any
from app.services.review_runtime.compaction.models import CompactionResult
from app.services.review_runtime.models import RuntimeMessageRole, TranscriptItem
from app.services.review_runtime.query_state import QueryLoopState
POST_COMPACT_MAX_TOKENS_PER_SKILL = 5_000
POST_COMPACT_SKILLS_TOKEN_BUDGET = 25_000
SKILL_TRUNCATION_MARKER = "\n\n[... skill content truncated for compaction; use Read on the skill path if you need the full text]"
def build_post_compact_messages(result: CompactionResult) -> list[TranscriptItem]:
    return [
        result.boundary_marker,
        *result.summary_messages,
        *(result.messages_to_keep or []),
        *result.attachments,
        *result.hook_results,
    ]
def rebuild_post_compact_artifacts(
    *,
    state: QueryLoopState,
    messages_to_keep: list[TranscriptItem] | None,
) -> tuple[list[TranscriptItem], list[TranscriptItem], str | None]:
    config = dict((state.tool_use_context or {}).get("post_compact") or {})
    kept = [deepcopy(item) for item in (messages_to_keep or [])]
    attachments: list[TranscriptItem] = []
    for file_entry in config.get("read_files") or []:
        if not isinstance(file_entry, dict):
            continue
        normalized_path = str(file_entry.get("path") or "").strip()
        if not normalized_path or _has_preserved_file_context(kept, normalized_path):
            continue
        content = str(file_entry.get("content") or "").strip()
        attachments.append(
            TranscriptItem(
                role=RuntimeMessageRole.SYSTEM,
                content=f"Restored file context: {normalized_path}\n{content}".strip(),
                name="post_compact_file_attachment",
                metadata={"synthetic": True, "kind": "post_compact_file_attachment", "path": normalized_path},
            )
        )
    active_skills = [entry for entry in config.get("active_skills") or [] if isinstance(entry, dict)]
    if active_skills:
        skill_attachment = _build_skill_attachment(active_skills, kept)
        if skill_attachment is not None:
            attachments.append(skill_attachment)
    plan_mode = dict(config.get("plan_mode") or {})
    if plan_mode.get("active"):
        attachments.append(
            TranscriptItem(
                role=RuntimeMessageRole.SYSTEM,
                content="Plan mode remains active." + (_format_plan_steps(plan_mode.get("steps") or [])),
                name="post_compact_plan_attachment",
                metadata={"synthetic": True, "kind": "post_compact_plan_attachment", "plan_mode": deepcopy(plan_mode)},
            )
        )
    deferred_tools = [str(tool) for tool in config.get("deferred_tools") or [] if str(tool or "").strip()]
    if deferred_tools and not _has_preserved_tools_context(kept, deferred_tools):
        attachments.append(
            TranscriptItem(
                role=RuntimeMessageRole.SYSTEM,
                content="Available deferred tools: " + ", ".join(deferred_tools),
                name="post_compact_tools_attachment",
                metadata={"synthetic": True, "kind": "post_compact_tools_attachment", "tools": deferred_tools},
            )
        )
    agent_listing = [str(agent) for agent in config.get("agent_listing") or [] if str(agent or "").strip()]
    if agent_listing and not _has_preserved_agent_listing(kept, agent_listing):
        attachments.append(
            TranscriptItem(
                role=RuntimeMessageRole.SYSTEM,
                content="Available agents: " + ", ".join(agent_listing),
                name="post_compact_agents_attachment",
                metadata={"synthetic": True, "kind": "post_compact_agents_attachment", "agents": agent_listing},
            )
        )
    mcp_servers = [str(server) for server in config.get("mcp_servers") or [] if str(server or "").strip()]
    if mcp_servers and not _has_preserved_mcp_servers(kept, mcp_servers):
        attachments.append(
            TranscriptItem(
                role=RuntimeMessageRole.SYSTEM,
                content="Available MCP servers: " + ", ".join(mcp_servers),
                name="post_compact_mcp_attachment",
                metadata={"synthetic": True, "kind": "post_compact_mcp_attachment", "mcp_servers": mcp_servers},
            )
        )
    hook_results: list[TranscriptItem] = []
    session_start_hooks = [entry for entry in config.get("session_start_hooks") or [] if isinstance(entry, dict)]
    if session_start_hooks:
        event_names = [str(entry.get("event") or "compact") for entry in session_start_hooks]
        message_lines = [str(entry.get("message") or "Session start hooks executed after compaction.").strip() for entry in session_start_hooks]
        hook_results.append(
            TranscriptItem(
                role=RuntimeMessageRole.SYSTEM,
                content="\n".join(line for line in message_lines if line),
                name="post_compact_hook_result",
                metadata={"synthetic": True, "kind": "post_compact_hook_result", "events": event_names},
            )
        )
    post_compact_hooks = dict(config.get("post_compact_hooks") or {})
    user_display_message = str(post_compact_hooks.get("user_display_message") or "").strip() or None
    return attachments, hook_results, user_display_message
def _build_skill_attachment(skill_entries: list[dict[str, Any]], messages_to_keep: list[TranscriptItem]) -> TranscriptItem | None:
    preserved_skill_refs = _collect_preserved_skill_refs(messages_to_keep)
    filtered = [entry for entry in skill_entries if str(entry.get("ref") or "").strip() not in preserved_skill_refs]
    if not filtered:
        return None
    used_tokens = 0
    rendered_parts: list[str] = []
    emitted_refs: list[str] = []
    for entry in filtered:
        ref = str(entry.get("ref") or "").strip()
        title = str(entry.get("title") or ref or "skill").strip()
        path = str(entry.get("path") or "").strip()
        content = _truncate_to_tokens(str(entry.get("content") or "").strip(), POST_COMPACT_MAX_TOKENS_PER_SKILL)
        candidate = f"- {title} ({path or ref})\n{content}".strip()
        tokens = _rough_token_count(candidate)
        if emitted_refs and used_tokens + tokens > POST_COMPACT_SKILLS_TOKEN_BUDGET:
            continue
        used_tokens += tokens
        emitted_refs.append(ref)
        rendered_parts.append(candidate)
    if not rendered_parts:
        return None
    return TranscriptItem(
        role=RuntimeMessageRole.SYSTEM,
        content="Invoked skills preserved after compaction:\n" + "\n\n".join(rendered_parts),
        name="post_compact_skill_attachment",
        metadata={"synthetic": True, "kind": "post_compact_skill_attachment", "skills": emitted_refs},
    )
def _has_preserved_file_context(messages: list[TranscriptItem], normalized_path: str) -> bool:
    for item in messages:
        path = str(item.metadata.get("path") or item.payload.get("path") or "").strip()
        if path == normalized_path and str(item.metadata.get("attachment_kind") or "") == "file":
            return True
        if path == normalized_path and str(item.name or "") == "post_compact_file_attachment":
            return True
    return False
def _has_preserved_tools_context(messages: list[TranscriptItem], tools: list[str]) -> bool:
    wanted = sorted(set(tool for tool in tools if tool))
    for item in messages:
        item_tools = item.metadata.get("tools") or item.payload.get("tools") or []
        normalized = sorted(set(str(tool) for tool in item_tools if str(tool or "").strip()))
        if normalized == wanted and str(item.metadata.get("attachment_kind") or "") == "tools_delta":
            return True
        if normalized == wanted and str(item.name or "") == "post_compact_tools_attachment":
            return True
    return False
def _has_preserved_agent_listing(messages: list[TranscriptItem], agents: list[str]) -> bool:
    wanted = sorted(set(agent for agent in agents if agent))
    for item in messages:
        item_agents = item.metadata.get("agents") or item.payload.get("agents") or []
        normalized = sorted(set(str(agent) for agent in item_agents if str(agent or "").strip()))
        if normalized == wanted and str(item.metadata.get("attachment_kind") or "") == "agent_listing":
            return True
        if normalized == wanted and str(item.name or "") == "post_compact_agents_attachment":
            return True
    return False
def _has_preserved_mcp_servers(messages: list[TranscriptItem], mcp_servers: list[str]) -> bool:
    wanted = sorted(set(server for server in mcp_servers if server))
    for item in messages:
        item_servers = item.metadata.get("mcp_servers") or item.payload.get("mcp_servers") or []
        normalized = sorted(set(str(server) for server in item_servers if str(server or "").strip()))
        if normalized == wanted and str(item.metadata.get("attachment_kind") or "") == "mcp_instructions":
            return True
        if normalized == wanted and str(item.name or "") == "post_compact_mcp_attachment":
            return True
    return False
def _collect_preserved_skill_refs(messages: list[TranscriptItem]) -> set[str]:
    refs: set[str] = set()
    for item in messages:
        raw_skills = item.metadata.get("skills") or item.payload.get("skills") or []
        if str(item.metadata.get("attachment_kind") or "") not in {"invoked_skills", "skill_listing"} and str(item.name or "") != "post_compact_skill_attachment":
            continue
        for value in raw_skills:
            normalized = str(value or "").strip()
            if normalized:
                refs.add(normalized)
    return refs
def _truncate_to_tokens(content: str, max_tokens: int) -> str:
    if _rough_token_count(content) <= max_tokens:
        return content
    char_budget = max(0, max_tokens * 4 - len(SKILL_TRUNCATION_MARKER))
    return content[:char_budget] + SKILL_TRUNCATION_MARKER


def _rough_token_count(content: str) -> int:
    return max(1, (len(content) + 3) // 4)


def _format_plan_steps(steps: list[Any]) -> str:
    normalized = [str(step).strip() for step in steps if str(step).strip()]
    if not normalized:
        return ""
    return "\nPlan steps: " + " | ".join(normalized)
