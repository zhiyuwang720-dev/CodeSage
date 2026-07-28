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
