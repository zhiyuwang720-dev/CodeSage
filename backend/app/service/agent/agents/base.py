from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from ..core.message import message_bus
from ..core.register import agent_registry
from ..core.state import AgentState


logger = logging.getLogger(__name__)

class AgentType(Enum):
    GENERAL = "general"


class AgentPattern(Enum):
    REACT = "react"
    PLAN_AND_EXECUTE = "plan_execute"
    REFLECTION = "reflection"


@dataclass
class AgentConfig:
    name: str
    agent_type: AgentType
    pattern: AgentPattern = AgentPattern.REACT
    model: Optional[str] = None
    temperature: float = 0.1
    max_tokens: int = 8192
    max_iterations: int = 20
    timeout_seconds: int = 600
    tools: List[str] = field(default_factory=list)
    system_prompt: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


""" Agent 任务交接数据结构 """
@dataclass
class TaskHandoff:
    from_agent: str
    to_agent: str
    summary: str
    work_completed: List[str] = field(default_factory=list)
    key_findings: List[Dict[str, Any]] = field(default_factory=list)
    insights: List[str] = field(default_factory=list)
    suggested_actions: List[Dict[str, Any]] = field(default_factory=list)
    attention_points: List[str] = field(default_factory=list)
    priority_areas: List[str] = field(default_factory=list)
    context_data: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.8
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "summary": self.summary,
            "work_completed": self.work_completed,
            "key_findings": self.key_findings,
            "insights": self.insights,
            "suggested_actions": self.suggested_actions,
            "attention_points": self.attention_points,
            "priority_areas": self.priority_areas,
            "context_data": self.context_data,
            "confidence": self.confidence,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskHandoff":
        return cls(
            from_agent=data.get("from_agent", ""),
            to_agent=data.get("to_agent", ""),
            summary=data.get("summary", ""),
            work_completed=data.get("work_completed", []),
            key_findings=data.get("key_findings", []),
            insights=data.get("insights", []),
            suggested_actions=data.get("suggested_actions", []),
            attention_points=data.get("attention_points", []),
            priority_areas=data.get("priority_areas", []),
            context_data=data.get("context_data", {}),
            confidence=data.get("confidence", 0.8),
        )

    def to_prompt_context(self) -> str:
        lines = [
            f"## Handoff From {self.from_agent} Agent",
            "",
            "### Summary",
            self.summary,
            "",
        ]
        if self.work_completed:
            lines.append("### Completed Work")
            lines.extend(f"- {work}" for work in self.work_completed)
            lines.append("")
        if self.key_findings:
            lines.append("### Key Findings")
            for index, finding in enumerate(self.key_findings[:15], 1):
                severity = finding.get("severity", "medium")
                title = finding.get("title", "Unknown")
                file_path = finding.get("file_path", "")
                lines.append(f"{index}. [{severity.upper()}] {title}")
                if file_path:
                    lines.append(f"   Location: {file_path}:{finding.get('line_start', '')}")
                if finding.get("description"):
                    lines.append(f"   Description: {finding['description'][:100]}")
            lines.append("")
        if self.insights:
            lines.append("### Insights")
            lines.extend(f"- {item}" for item in self.insights)
            lines.append("")
        if self.suggested_actions:
            lines.append("### Suggested Actions")
            for action in self.suggested_actions:
                action_type = action.get("type", action.get("action", "general"))
                description = action.get("description", action.get("reason", ""))
                priority = action.get("priority", "medium")
                lines.append(f"- [{priority.upper()}] {action_type}: {description}")
            lines.append("")
        if self.attention_points:
            lines.append("### Attention Points")
            lines.extend(f"- {item}" for item in self.attention_points)
            lines.append("")
        if self.priority_areas:
            lines.append("### Priority Areas")
            lines.extend(f"- {item}" for item in self.priority_areas)
        return "\n".join(lines)


@dataclass
class AgentResult:
    success: bool
    data: Any = None
    error: Optional[str] = None
    iterations: int = 0
    tool_calls: int = 0
    tokens_used: int = 0
    duration_ms: int = 0
    intermediate_steps: List[Dict[str, Any]] = field(default_factory=list)    # 记录中间执行步骤
    metadata: Dict[str, Any] = field(default_factory=dict)
    handoff: Optional[TaskHandoff] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "tokens_used": self.tokens_used,
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
            "handoff": self.handoff.to_dict() if self.handoff else None,
        }


class BaseAgent(ABC):

    def __init__(
            self,
            config: AgentConfig,
            llm_service,
            tools: Dict[str, Any],
            event_emitter=None,
            parent_id: Optional[str] = None,
            knowledge_modules: Optional[List[str]] = None,
    ):
        self.config = config
        self.llm_service = llm_service
        self.tools = tools
        self.event_emitter = event_emitter
        self.parent_id = parent_id
        self.knowledge_modules = knowledge_modules or []
        self._agent_id = f"agent_{uuid.uuid4().hex[:8]}"
        self._state = AgentState(
            agent_id=self._agent_id,
            agent_name=config.name,
            agent_type=config.agent_type.value,
            parent_id=parent_id,
            max_iterations=config.max_iterations,
            knowledge_modules=self.knowledge_modules,
        )
        self._iteration = 0
        self._total_tokens = 0
        self._tool_calls = 0
        self._cancelled = False
        self._cancel_callback = None
        self._registered = False
        self._runtime_session_checkpoint_store = None
        self._incoming_handoff: Optional[TaskHandoff] = None
        self._insights: List[str] = []
        self._work_completed: List[str] = []
        self._timeout_config = self._get_timeout_config()

    @abstractmethod
    async def run(self, input_data: Dict[str, Any]) -> AgentResult:
        # 子类必须实现具体的执行逻辑
        raise NotImplementedError


    @property
    def name(self) -> str:
        return self.config.name

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def agent_type(self) -> AgentType:
        return self.config.agent_type

    def _get_timeout_config(self) -> Dict[str, int]:
        from core.config import settings
        timeout_getter = getattr(self.llm_service, "get_agent_timeout_config", None)
        if callable(timeout_getter):
            resolved = timeout_getter()
            if isinstance(resolved, dict):
                return resolved
        return {
            "llm_first_token_timeout": getattr(settings, "LLM_FIRST_TOKEN_TIMEOUT", 30),
            "llm_stream_timeout": getattr(settings, "LLM_STREAM_TIMEOUT", 60),
            "agent_timeout": getattr(settings, "AGENT_TIMEOUT_SECONDS", 1800),
            "sub_agent_timeout": getattr(settings, "SUB_AGENT_TIMEOUT_SECONDS", 600),
            "tool_timeout": getattr(settings, "TOOL_TIMEOUT_SECONDS", 60),
        }

    """ Agent 注册与消息队列 """
    def _register_to_registry(self, task: Optional[str] = None) -> None:
        if self._registered:
            return
        agent_registry.register_agent(
            agent_id=self._agent_id,
            agent_name=self.config.name,
            agent_type=self.config.agent_type.value,
            task=task or self._state.task or "Initializing",
            parent_id=self.parent_id,
            agent_instance=self,
            state=self._state,
            knowledge_modules=self.knowledge_modules,
        )
        try:
            message_bus.create_queue(self._agent_id)
        except Exception:
            pass
        self._registered = True


    def compress_messages_if_needed(self, messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        max_messages = 40
        if len(messages) <= max_messages:
            return messages
        return [messages[0]] + messages[-(max_messages - 1):]


    async def stream_llm_call(
            self,
            messages: List[Dict[str, str]],
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            auto_compress: bool = True,
    ) -> Tuple[str, int]:
        if auto_compress:
            messages = self.compress_messages_if_needed(messages)
        if self.is_cancelled:
            return "", 0

        accumulated = ""
        total_tokens = 0
        token_buffer: List[str] = []
        token_buffer_count = 0
        from core.config import settings
        token_chunk_size = max(1, int(getattr(settings, "AGENT_TOKEN_EVENT_CHUNK_SIZE", 20)))
        flush_interval = max(0.0, float(getattr(settings, "AGENT_TOKEN_EVENT_FLUSH_INTERVAL_MS", 100)) / 1000.0)
        last_flush_at = time.monotonic()

        async def flush_token_buffer(force: bool = False) -> None:
            nonlocal token_buffer, token_buffer_count, last_flush_at
            if not token_buffer:
                return
            now = time.monotonic()
            if (
                    force
                    or token_buffer_count >= token_chunk_size
                    or (flush_interval > 0 and now - last_flush_at >= flush_interval)
            ):
                token_text = "".join(token_buffer)
                token_buffer = []
                token_buffer_count = 0
                last_flush_at = now
                await self.emit_thinking_token(token_text)

        await self.emit_thinking_start()
        try:
            stream = self.llm_service.chat_completion_stream(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            iterator = stream.__aiter__()
            first_timeout = float(self._timeout_config.get("llm_first_token_timeout", 30))
            stream_timeout = float(self._timeout_config.get("llm_stream_timeout", 60))
            first_token = False
            while True:
                if self.is_cancelled:
                    break
                try:
                    timeout = first_timeout if not first_token else stream_timeout
                    chunk = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    accumulated = accumulated or "[LLM timeout]"
                    break

                if chunk.get("type") == "token":
                    first_token = True
                    token = chunk.get("content", "")
                    accumulated = chunk.get("accumulated", accumulated + token)
                    if token:
                        token_buffer.append(token)
                        token_buffer_count += 1
                        await flush_token_buffer()
                    await asyncio.sleep(0)
                elif chunk.get("type") == "done":
                    await flush_token_buffer(force=True)
                    accumulated = chunk.get("content", accumulated)
                    usage = chunk.get("usage") or {}
                    total_tokens = usage.get("total_tokens", 0)
                    if total_tokens:
                        await self.emit_event(
                            "llm_usage",
                            "LLM token usage recorded",
                            metadata={"tokens_used": total_tokens, "usage": usage},
                        )
                    break
                elif chunk.get("type") == "error":
                    await flush_token_buffer(force=True)
                    accumulated = chunk.get("accumulated", accumulated)
                    error_message = chunk.get("user_message") or chunk.get("error") or "Unknown error"
                    accumulated = accumulated or f"[API_ERROR:{chunk.get('error_type', 'unknown')}] {error_message}"
                    usage = chunk.get("usage") or {}
                    total_tokens = usage.get("total_tokens", 0)
                    if total_tokens:
                        await self.emit_event(
                            "llm_usage",
                            "LLM token usage recorded",
                            metadata={"tokens_used": total_tokens, "usage": usage},
                        )
                    break
        except Exception as exc:
            logger.error("[%s] Unexpected error in stream_llm_call: %s", self.name, exc, exc_info=True)
            await self.emit_event("error", f"LLM call error: {exc}")
            accumulated = f"[LLM调用错误: {str(exc)}] 请重试。"
        finally:
            await flush_token_buffer(force=True)
            await self.emit_thinking_end(accumulated)
        return accumulated, total_tokens