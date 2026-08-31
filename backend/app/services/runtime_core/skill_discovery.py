from __future__ import annotations

from typing import Any

from app.services.runtime_core.session_state import SessionRuntimeState

AI_MARKERS = {
    "ai",
    "llm",
    "rag",
    "agent",
    "prompt",
    "tool calling",
    "tool-calling",
    "vector",
    "embedding",
    "mcp",
    "langchain",
    "llamaindex",
    "openai",
    "anthropic",
    "model",
}

REPORT_MARKERS = {
    "report",
    "cve",
    "disclosure",
    "writeup",
    "write-up",
    "draft",
    "markdown",
    "summary",
}

_STAGE_RANK = {
    "catalog": 0,
    "body": 1,
    "references": 2,
    "examples": 3,
    "scripts": 4,
    "full": 5,
}


class SkillDiscoveryScheduler:
    def discover(
        self,
        *,
        agent_type: str,
        runtime_state: SessionRuntimeState,
        available_skills: list[dict[str, Any]],
        matched_skills: list[dict[str, Any]],
        task: str,
        latest_user_message: str,
        recon_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        recon_payload = dict(recon_payload or {})
        matched_refs = {self._skill_ref(item) for item in matched_skills if self._skill_ref(item)}
        context_text = self._build_context_text(task=task, latest_user_message=latest_user_message, recon_payload=recon_payload)
        candidates = [
            self._score_skill(
                skill=item,
                agent_type=agent_type,
                runtime_state=runtime_state,
                matched_refs=matched_refs,
                context_text=context_text,
                latest_user_message=latest_user_message,
                recon_payload=recon_payload,
            )
            for item in available_skills
            if self._skill_ref(item)
        ]
        ranked = sorted(
            candidates,
            key=lambda item: (
                item["score"],
                item["direct_mention"],
                item["freshness"] == "unseen",
                -len(item["trigger_reasons"]),
                item["skill_ref"],
            ),
            reverse=True,
        )
        selected_skill = ranked[0]["skill_ref"] if ranked and ranked[0]["score"] > 0 else None
        for index, item in enumerate(ranked, start=1):
            item["rank"] = index
        return {
            "selected_skill": selected_skill,
            "ranked_candidates": ranked,
            "top_immediate": [item["skill_ref"] for item in ranked if item["invocation_mode"] == "auto_expand"],
        }

    def _score_skill(
        self,
        *,
        skill: dict[str, Any],
        agent_type: str,
        runtime_state: SessionRuntimeState,
        matched_refs: set[str],
        context_text: str,
        latest_user_message: str,
        recon_payload: dict[str, Any],
    ) -> dict[str, Any]:
        skill_ref = self._skill_ref(skill)
        skill_text = self._skill_text(skill)
        score = 0
        trigger_reasons: list[str] = []
        direct_mention = self._has_direct_mention(skill, latest_user_message)
        frontmatter = (skill.get("skill_metadata") or {}).get("frontmatter") or {}
        skill_paths = self._normalize_string_list(frontmatter.get("paths") or skill.get("paths", {}).values())
        invoked_state = runtime_state.agent_states.get(agent_type, None)
        invoked_skill = invoked_state.invoked_skills.get(skill_ref) if invoked_state else None

        if direct_mention:
            score += 18
            trigger_reasons.append("direct_user_mention")

        if skill_ref in matched_refs:
            score += 5
            trigger_reasons.append("binding_match")

        if bool(skill.get("always_include")):
            score += 2
            trigger_reasons.append("always_include")

        keyword_hits = self._count_hits(context_text, self._normalize_string_list(skill.get("match_keywords") or skill.get("tags") or []))
        if keyword_hits:
            score += min(6, keyword_hits * 2)
            trigger_reasons.append("keyword_overlap")

        if self._is_ai_context(context_text) and self._is_ai_skill(skill_text):
            score += 9
            trigger_reasons.append("ai_context_alignment")

        if self._is_report_request(context_text) and self._is_report_skill(skill_text):
            score += 9
            trigger_reasons.append("report_request_alignment")

        path_hits = self._path_overlap_count(runtime_state.touched_paths, skill_paths)
        if path_hits:
            score += min(8, path_hits * 3)
            trigger_reasons.append("path_overlap")

        if str(agent_type).strip().lower() in skill_text:
            score += 2
            trigger_reasons.append("agent_affinity")

        freshness = "unseen"
        current_stage = "catalog"
        if invoked_skill is not None:
            current_stage = invoked_skill.skill_stage
            freshness = f"seen-{current_stage}"
            score += 2
            trigger_reasons.append("invoked_context_continuity")
            if current_stage in {"scripts", "full"} and not direct_mention and not path_hits and not self._is_ai_context(context_text) and not self._is_report_request(context_text):
                score -= 8
                freshness = "saturated"
                trigger_reasons.append("saturation_penalty")
            elif current_stage == "references" and not direct_mention and not path_hits:
                score -= 2
                trigger_reasons.append("repeat_load_penalty")

        suggested_stage = self._suggested_stage(
            current_stage=current_stage,
            direct_mention=direct_mention,
            path_hits=path_hits,
            ai_context=self._is_ai_context(context_text) and self._is_ai_skill(skill_text),
            report_context=self._is_report_request(context_text) and self._is_report_skill(skill_text),
        )
        invocation_mode = self._invocation_mode(score=score, current_stage=current_stage, suggested_stage=suggested_stage)

        return {
            "skill_ref": skill_ref,
            "score": score,
            "trigger_reasons": trigger_reasons,
            "suggested_stage": suggested_stage,
            "freshness": freshness,
            "invocation_mode": invocation_mode,
            "direct_mention": direct_mention,
            "path_overlap_count": path_hits,
            "matched": skill_ref in matched_refs,
            "skill_name": str(skill.get("name") or skill_ref),
        }

    def _suggested_stage(
        self,
        *,
        current_stage: str,
        direct_mention: bool,
        path_hits: int,
        ai_context: bool,
        report_context: bool,
    ) -> str:
        if current_stage == "catalog":
            return "body"
        if current_stage == "body" and (direct_mention or path_hits or ai_context or report_context):
            return "references"
        if current_stage == "references" and (report_context or direct_mention):
            return "examples"
        if current_stage == "examples" and (ai_context or direct_mention):
            return "scripts"
        return current_stage

    def _invocation_mode(self, *, score: int, current_stage: str, suggested_stage: str) -> str:
        if score >= 12 and self._stage_rank(suggested_stage) > self._stage_rank(current_stage):
            return "auto_expand"
        if score >= 12 and current_stage == "catalog":
            return "auto_expand"
        if score >= 6:
            return "suggest"
        return "wait"

    @staticmethod
    def _stage_rank(stage: str) -> int:
        return _STAGE_RANK.get(str(stage or "catalog"), 0)

    @staticmethod
    def _skill_ref(skill: dict[str, Any]) -> str:
        return str(skill.get("slug") or skill.get("id") or skill.get("name") or "").strip()

    def _skill_text(self, skill: dict[str, Any]) -> str:
        frontmatter = (skill.get("skill_metadata") or {}).get("frontmatter") or {}
        parts = [
            str(skill.get("name") or ""),
            str(skill.get("slug") or ""),
            str(skill.get("description") or ""),
            " ".join(self._normalize_string_list(skill.get("tags") or [])),
            " ".join(self._normalize_string_list(skill.get("match_keywords") or [])),
            str(frontmatter.get("when_to_use") or ""),
            str(frontmatter.get("agent") or ""),
        ]
        return "\n".join(part.lower() for part in parts if part)

    def _has_direct_mention(self, skill: dict[str, Any], latest_user_message: str) -> bool:
        message = f" {str(latest_user_message or '').strip().lower()} "
        aliases = {
            str(skill.get("slug") or "").strip().lower(),
            str(skill.get("name") or "").strip().lower(),
        }
        if str(skill.get("name") or "").strip().lower() == "code audit":
            aliases.add("code audit")
        for alias in aliases:
            if alias and f" {alias} " in message:
                return True
        return False

    def _build_context_text(self, *, task: str, latest_user_message: str, recon_payload: dict[str, Any]) -> str:
        parts = [str(task or ""), str(latest_user_message or ""), str(recon_payload.get("summary") or "")]
        project_info = recon_payload.get("project_info") or {}
        for key in ("name", "frameworks", "languages"):
            value = project_info.get(key)
            if value:
                parts.append(str(value))
        return "\n".join(parts).lower()

    @staticmethod
    def _normalize_string_list(values: Any) -> list[str]:
        normalized: list[str] = []
        if isinstance(values, dict):
            values = list(values.values())
        if not isinstance(values, list):
            values = [values] if values else []
        for value in values:
            item = str(value or "").strip()
            if item and item not in normalized:
                normalized.append(item)
        return normalized

    @staticmethod
    def _count_hits(context_text: str, values: list[str]) -> int:
        return sum(1 for value in values if value and value.lower() in context_text)

    @staticmethod
    def _has_any_marker(text: str, markers: set[str]) -> bool:
        return any(marker in text for marker in markers)

    def _is_ai_context(self, context_text: str) -> bool:
        return self._has_any_marker(context_text, AI_MARKERS)

    def _is_ai_skill(self, skill_text: str) -> bool:
        return self._has_any_marker(skill_text, AI_MARKERS)

    def _is_report_request(self, context_text: str) -> bool:
        return self._has_any_marker(context_text, REPORT_MARKERS)

    def _is_report_skill(self, skill_text: str) -> bool:
        return self._has_any_marker(skill_text, REPORT_MARKERS)

    @staticmethod
    def _path_overlap_count(touched_paths: list[str], skill_paths: list[str]) -> int:
        normalized_touched = [str(item or "").replace("\\", "/").lower() for item in touched_paths or []]
        normalized_skill_paths = [str(item or "").replace("\\", "/").lower() for item in skill_paths or []]
        hits = 0
        for touched in normalized_touched:
            if any(skill_path and skill_path in touched for skill_path in normalized_skill_paths):
                hits += 1
        return hits