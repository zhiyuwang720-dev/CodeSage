"""阶段 02 测试驱动装置: 从 spike_pr_review.py 抽取的 fake 模型 + 运行时组装。

离线驱动真实 QueryLoop/Runner/注册表(不触网、不依赖外部 LLM)。
"""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.services.agent.tools.shared_catalog import build_shared_agent_tool_catalog
from app.services.contracts.models import (
    RuntimeMessageRole,
    RuntimeModelResponse,
    TranscriptItem,
)
from app.services.review_runtime.runner import FindingRuntimeRunner
from app.services.review_runtime.session_store import AuditSessionStore
from app.services.runtime_core import build_runtime_tool_registry
from app.services.runtime_core.tool_message_codec import build_runtime_model_messages


def make_session_factory(tmp_path):
    db_path = tmp_path / f"rt-{uuid4().hex[:8]}.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class ScriptedLLMService:
    """按剧本回放 chat_completion 响应(无网络)。"""

    def __init__(self, turns: list[dict]):
        self.turns = turns
        self.calls = 0

    async def chat_completion(
        self,
        messages,
        temperature=None,
        max_tokens=None,
        agent_type=None,
        tools=None,
        parallel_tool_calls=None,
    ):
        del temperature, max_tokens, agent_type, parallel_tool_calls, messages, tools
        index = min(self.calls, len(self.turns) - 1)
        self.calls += 1
        turn = dict(self.turns[index])
        turn.setdefault("finish_reason", "tool_calls" if turn.get("tool_calls") else "stop")
        turn.setdefault("usage", {})
        turn.setdefault("content", "")
        turn.setdefault("reasoning_content", "")
        return turn


class ScriptedModelClient:
    """spike SpikeModelClient 的测试版: 包装 ScriptedLLMService → RuntimeModelResponse。"""

    FINALIZER_TOOL_NAMES = {"FinalizeFinding", "FinalizeReview", "FinalizeVulnerabilityReports"}

    def __init__(self, llm_service):
        self._llm_service = llm_service

    async def complete(
        self,
        *,
        system_prompt: str | None,
        recon_payload: dict[str, Any],
        transcript: list[Any],
        model_name: str,
        tool_definitions: list[dict[str, Any]],
        max_output_tokens_override: int | None = None,
    ) -> RuntimeModelResponse:
        del model_name, max_output_tokens_override
        messages = build_runtime_model_messages(
            system_prompt=system_prompt or "",
            recon_payload=recon_payload,
            transcript=transcript,
            tool_definitions=tool_definitions,
            tool_message_format="openai_tools",
        )
        llm = getattr(self, "_llm_service")
        response = await llm.chat_completion(messages=messages)
        tool_calls = [self._normalize(item) for item in (response.get("tool_calls") or [])]
        return RuntimeModelResponse(
            content=str(response.get("content") or ""),
            reasoning_content=str(response.get("reasoning_content") or ""),
            tool_calls=tool_calls,
            stop_reason=str(response.get("finish_reason") or "stop"),
            usage=dict(response.get("usage") or {}),
            native_tool_call_count=len(tool_calls),
            has_terminal_tool_call=any(
                str(item.get("name") or "") in self.FINALIZER_TOOL_NAMES for item in tool_calls
            ),
        )

    async def stream_complete(self, **kwargs):
        response = await self.complete(**kwargs)
        if response.content:
            yield {"type": "content_delta", "content": response.content, "accumulated": response.content}
        for tool_call in response.tool_calls:
            yield {"type": "tool_call", "tool_call": tool_call}
        yield {
            "type": "done",
            "content": response.content,
            "stop_reason": response.stop_reason,
            "recoverable_error_kind": response.recoverable_error_kind,
            "recoverable_error_message": response.recoverable_error_message,
            "tool_calls": [],
        }

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
        item = dict(raw or {})
        item.setdefault("id", f"tool-use-{uuid4().hex[:8]}")
        name = item.get("name")
        if not name and isinstance(item.get("function"), dict):
            name = item["function"].get("name")
        item["name"] = str(name or "unknown")
        raw_input = item.get("input") or item.get("arguments")
        if isinstance(raw_input, str):
            try:
                raw_input = json.loads(raw_input)
            except json.JSONDecodeError:
                raw_input = {"raw": raw_input}
        item["input"] = raw_input if isinstance(raw_input, dict) else {}
        return item


def finalize_call(findings: list[dict], summary: str) -> dict:
    return {"name": "FinalizeReview", "input": {"findings": findings, "summary": summary}}


def build_review_runner(
    session_factory,
    model_client,
    *,
    project_root,
    agent_type: str = "review:security",
    max_turns: int = 8,
    nudge_limit: int = 2,
    require_terminal_action: bool = True,
):
    """组装(会话存储 + 权限矩阵注册表 + 终点 nudge)与生产一致的 runner。"""
    session_store = AuditSessionStore(session_factory=session_factory)
    registry = build_runtime_tool_registry(
        session_store=session_store,
        agent_tools=build_shared_agent_tool_catalog(project_root=str(project_root)),
        agent_type=agent_type,
        include_finding_finalizer=False,
    )
    from app.services.runtime_core.tool_runtime import ToolOrchestrator

    orchestrator = ToolOrchestrator(session_store=session_store, tool_registry=registry)
    runner = FindingRuntimeRunner(
        session_store=session_store,
        model_client=model_client,
        tool_registry=registry,
        tool_orchestrator=orchestrator,
        max_turns=max_turns,
        require_terminal_action=require_terminal_action,
        terminal_action_nudge_limit=nudge_limit,
        terminal_action_nudge_message="你必须调用 FinalizeReview 工具提交结构化审查评论，禁止只用自然语言结束。",
    )
    return session_store, runner, registry


def create_review_session(session_store: AuditSessionStore, diff_text: str, prompt: str) -> str:
    session_id = session_store.create_session(
        project_id="pr-review-test",
        task_id=None,
        runtime_stack="runtime",
        system_prompt=prompt,
        recon_payload={"diff_text": diff_text, "repo": "fixture", "pr_number": 1},
    )
    session_store.append_message(
        session_id,
        TranscriptItem(role=RuntimeMessageRole.USER, content="请审查本次 PR diff 并用 FinalizeReview 提交评论集。"),
    )
    return session_id
