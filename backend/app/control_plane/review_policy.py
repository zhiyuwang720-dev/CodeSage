from __future__ import annotations

from typing import Any

from app.contracts.model_boundary import MODEL_BOUNDARY_VERSION
from app.core.config import settings
from app.domains.pr_review.orchestrator import TOOL_MATRICES
from app.domains.pr_review.prompts import (
    REVIEW_ARCHITECTURE_PROMPT,
    REVIEW_QUALITY_PROMPT,
    REVIEW_SECURITY_PROMPT,
)
from app.models.agent_task import AgentTask


def build_review_compatibility_config(task: AgentTask) -> dict[str, Any]:
    """Build the immutable control-plane policy snapshot used for resume fencing."""
    llm = dict(task.llm_config or {})
    fields = (
        "provider", "model", "base_url", "temperature", "max_tokens", "timeout",
        "endpoint_protocol", "tool_message_format", "disable_streaming",
    )
    safe_llm = {key: llm[key] for key in fields if key in llm}
    return {
        "flow_version": "quick-review-v1",
        # 模型边界实现版本参与恢复指纹：边界实现变化后旧身份必须明确拒绝复用。
        "model_boundary_version": MODEL_BOUNDARY_VERSION,
        "model": safe_llm,
        "runtime": {
            "provider": settings.LLM_PROVIDER,
            "model": settings.LLM_MODEL,
            "temperature": settings.LLM_TEMPERATURE,
            "max_tokens": settings.LLM_MAX_TOKENS,
            "protocol": settings.LLM_ENDPOINT_PROTOCOL,
            "model_boundary_version": MODEL_BOUNDARY_VERSION,
        },
        "prompts": {
            "security": REVIEW_SECURITY_PROMPT,
            "architecture": REVIEW_ARCHITECTURE_PROMPT,
            "quality": REVIEW_QUALITY_PROMPT,
        },
        "tools": {key: sorted(value) for key, value in TOOL_MATRICES.items()},
        "budget": {
            "max_iterations": int(task.max_iterations or 50),
            "timeout_seconds": int(task.timeout_seconds or 1800),
            "token_budget": int(task.token_budget or 100000),
        },
    }
