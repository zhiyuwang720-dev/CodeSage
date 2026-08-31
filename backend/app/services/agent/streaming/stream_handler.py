
"""Utilities for normalizing agent and LangGraph stream events."""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class StreamEventType(str, Enum):
    LLM_START = "llm_start"
    LLM_THOUGHT = "llm_thought"
    LLM_DECISION = "llm_decision"
    LLM_ACTION = "llm_action"
    LLM_OBSERVATION = "llm_observation"
    LLM_COMPLETE = "llm_complete"
    THINKING_START = "thinking_start"
    THINKING_TOKEN = "thinking_token"
    THINKING_END = "thinking_end"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_INPUT = "tool_call_input"
    TOOL_CALL_OUTPUT = "tool_call_output"
    TOOL_CALL_END = "tool_call_end"
    TOOL_CALL_ERROR = "tool_call_error"
    NODE_START = "node_start"
    NODE_END = "node_end"
    PHASE_START = "phase_start"
    PHASE_END = "phase_end"
    FINDING_NEW = "finding_new"
    FINDING_VERIFIED = "finding_verified"
    PROGRESS = "progress"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    TASK_START = "task_start"
    TASK_COMPLETE = "task_complete"
    TASK_ERROR = "task_error"
    TASK_CANCEL = "task_cancel"
    HEARTBEAT = "heartbeat"


@dataclass
class StreamEvent:
    event_type: StreamEventType
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    sequence: int = 0
    node_name: Optional[str] = None
    phase: Optional[str] = None
    tool_name: Optional[str] = None

    def to_sse(self) -> str:
        event_data = {
            "type": self.event_type.value,
            "data": self.data,
            "timestamp": self.timestamp,
            "sequence": self.sequence,
        }
        if self.node_name:
            event_data["node"] = self.node_name
        if self.phase:
            event_data["phase"] = self.phase
        if self.tool_name:
            event_data["tool"] = self.tool_name
        return f"event: {self.event_type.value}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "data": self.data,
            "timestamp": self.timestamp,
            "sequence": self.sequence,
            "node_name": self.node_name,
            "phase": self.phase,
            "tool_name": self.tool_name,
        }


class StreamHandler:
    """Convert raw execution events into a consistent stream payload."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self._sequence = 0
        self._current_phase: Optional[str] = None
        self._current_node: Optional[str] = None
        self._thinking_buffer: list[str] = []
        self._tool_states: Dict[str, Dict[str, Any]] = {}

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    async def process_langgraph_event(self, event: Dict[str, Any]) -> Optional[StreamEvent]:
        event_kind = event.get("event", "")
        event_name = event.get("name", "")
        event_data = event.get("data", {})

        if event_kind == "on_chat_model_stream":
            return await self._handle_llm_stream(event_data, event_name)
        if event_kind == "on_chat_model_start":
            return await self._handle_llm_start(event_data, event_name)
        if event_kind == "on_chat_model_end":
            return await self._handle_llm_end(event_data, event_name)
        if event_kind == "on_tool_start":
            return await self._handle_tool_start(event_name, event_data)
        if event_kind == "on_tool_end":
            return await self._handle_tool_end(event_name, event_data)
        if event_kind == "on_chain_start" and self._is_node_event(event_name):
            return await self._handle_node_start(event_name, event_data)
        if event_kind == "on_chain_end" and self._is_node_event(event_name):
            return await self._handle_node_end(event_name, event_data)
        if event_kind == "on_custom_event":
            return await self._handle_custom_event(event_name, event_data)
        return None

    def _is_node_event(self, name: str) -> bool:
        node_names = [
            "recon",
            "scan",
            "triage",
            "finding",
            "analysis",
            "verification",
            "report",
            "orchestrator",
            "ReconNode",
            "ScanNode",
            "TriageNode",
            "FindingNode",
            "AnalysisNode",
            "VerificationNode",
            "ReportNode",
        ]
        return any(token.lower() in name.lower() for token in node_names)

    async def _handle_llm_start(self, data: Dict[str, Any], name: str) -> StreamEvent:
        self._thinking_buffer = []
        return StreamEvent(
            event_type=StreamEventType.THINKING_START,
            sequence=self._next_sequence(),
            node_name=self._current_node,
            phase=self._current_phase,
            data={"model": name, "message": "LLM thinking started"},
        )

    async def _handle_llm_stream(self, data: Dict[str, Any], name: str) -> Optional[StreamEvent]:
        chunk = data.get("chunk")
        if not chunk:
            return None
        content = getattr(chunk, "content", None)
        if content is None and isinstance(chunk, dict):
            content = chunk.get("content", "")
        if not content:
            return None
        self._thinking_buffer.append(content)
        return StreamEvent(
            event_type=StreamEventType.THINKING_TOKEN,
            sequence=self._next_sequence(),
            node_name=self._current_node,
            phase=self._current_phase,
            data={"token": content, "accumulated": "".join(self._thinking_buffer)},
        )

    async def _handle_llm_end(self, data: Dict[str, Any], name: str) -> StreamEvent:
        full_response = "".join(self._thinking_buffer)
        self._thinking_buffer = []
        usage = {}
        output = data.get("output")
        usage_metadata = getattr(output, "usage_metadata", None)
        if usage_metadata:
            usage = {
                "input_tokens": getattr(usage_metadata, "input_tokens", 0),
                "output_tokens": getattr(usage_metadata, "output_tokens", 0),
            }
        return StreamEvent(
            event_type=StreamEventType.THINKING_END,
            sequence=self._next_sequence(),
            node_name=self._current_node,
            phase=self._current_phase,
            data={"response": full_response[:2000], "usage": usage, "message": "LLM thinking finished"},
        )

    async def _handle_tool_start(self, tool_name: str, data: Dict[str, Any]) -> StreamEvent:
        tool_input = data.get("input", {})
        self._tool_states[tool_name] = {"start_time": time.time(), "input": tool_input}
        return StreamEvent(
            event_type=StreamEventType.TOOL_CALL_START,
            sequence=self._next_sequence(),
            node_name=self._current_node,
            phase=self._current_phase,
            tool_name=tool_name,
            data={"tool_name": tool_name, "input": self._truncate_data(tool_input), "message": f"Calling tool: {tool_name}"},
        )

    async def _handle_tool_end(self, tool_name: str, data: Dict[str, Any]) -> StreamEvent:
        state = self._tool_states.pop(tool_name, {})
        duration_ms = None
        if state.get("start_time"):
            duration_ms = int((time.time() - state["start_time"]) * 1000)
        output = data.get("output", data)
        return StreamEvent(
            event_type=StreamEventType.TOOL_CALL_END,
            sequence=self._next_sequence(),
            node_name=self._current_node,
            phase=self._current_phase,
            tool_name=tool_name,
            data={
                "tool_name": tool_name,
                "input": self._truncate_data(state.get("input")),
                "output": self._truncate_data(output),
                "duration_ms": duration_ms,
                "message": f"Tool finished: {tool_name}",
            },
        )

    async def _handle_node_start(self, node_name: str, data: Dict[str, Any]) -> StreamEvent:
        self._current_node = node_name
        lowered = node_name.lower()
        phase_map = {
            "recon": "recon",
            "scan": "analysis",
            "triage": "analysis",
            "finding": "analysis",
            "analysis": "analysis",
            "verification": "verification",
            "report": "reporting",
            "orchestrator": "orchestration",
        }
        for token, phase in phase_map.items():
            if token in lowered:
                self._current_phase = phase
                break
        return StreamEvent(
            event_type=StreamEventType.NODE_START,
            sequence=self._next_sequence(),
            node_name=node_name,
            phase=self._current_phase,
            data={"node_name": node_name, "input": self._truncate_data(data.get("input", data)), "message": f"Node started: {node_name}"},
        )

    async def _handle_node_end(self, node_name: str, data: Dict[str, Any]) -> StreamEvent:
        event = StreamEvent(
            event_type=StreamEventType.NODE_END,
            sequence=self._next_sequence(),
            node_name=node_name,
            phase=self._current_phase,
            data={"node_name": node_name, "output": self._truncate_data(data.get("output", data)), "message": f"Node finished: {node_name}"},
        )
        self._current_node = None
        return event

    async def _handle_custom_event(self, event_name: str, data: Dict[str, Any]) -> StreamEvent:
        event_type_map = {
            "finding": StreamEventType.FINDING_NEW,
            "finding_verified": StreamEventType.FINDING_VERIFIED,
            "progress": StreamEventType.PROGRESS,
            "warning": StreamEventType.WARNING,
            "error": StreamEventType.ERROR,
            "phase_start": StreamEventType.PHASE_START,
            "phase_end": StreamEventType.PHASE_END,
        }
        event_type = event_type_map.get(event_name, StreamEventType.INFO)
        if event_type == StreamEventType.PHASE_START:
            self._current_phase = data.get("phase") or data.get("name") or self._current_phase
        return StreamEvent(
            event_type=event_type,
            sequence=self._next_sequence(),
            node_name=self._current_node,
            phase=self._current_phase,
            data=self._truncate_data(data),
        )

    def _truncate_data(self, data: Any, max_length: int = 1000) -> Any:
        if data is None:
            return None
        if isinstance(data, (str, bytes)):
            text = data.decode(errors="ignore") if isinstance(data, bytes) else data
            return text if len(text) <= max_length else text[:max_length] + "..."
        if isinstance(data, dict):
            return {key: self._truncate_data(value, max_length) for key, value in data.items()}
        if isinstance(data, list):
            return [self._truncate_data(item, max_length) for item in data[:20]]
        return data

    def create_progress_event(self, progress_percent: float, message: str, phase: Optional[str] = None, node_name: Optional[str] = None) -> StreamEvent:
        return StreamEvent(
            event_type=StreamEventType.PROGRESS,
            sequence=self._next_sequence(),
            node_name=node_name or self._current_node,
            phase=phase or self._current_phase,
            data={"progress_percent": progress_percent, "message": message},
        )

    def create_finding_event(self, finding: Dict[str, Any], is_verified: bool = False, node_name: Optional[str] = None) -> StreamEvent:
        return StreamEvent(
            event_type=StreamEventType.FINDING_VERIFIED if is_verified else StreamEventType.FINDING_NEW,
            sequence=self._next_sequence(),
            node_name=node_name or self._current_node,
            phase=self._current_phase,
            data=self._truncate_data(finding),
        )

    def create_heartbeat(self) -> StreamEvent:
        return StreamEvent(
            event_type=StreamEventType.HEARTBEAT,
            sequence=self._next_sequence(),
            node_name=self._current_node,
            phase=self._current_phase,
            data={"task_id": self.task_id},
        )
