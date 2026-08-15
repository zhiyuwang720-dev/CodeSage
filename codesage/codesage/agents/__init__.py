"""Agent definitions, registry and subagent execution (phase 13).

S1 delivers the definition layer (frontmatter loading, layered priority
merge project > user > builtin, builtin trio); S2 the Agent tool with a
foreground nested run; S3-S7 build forkContext, permission narrowing,
background/Mailbox, task extensions and worktree isolation on top.
"""

from .loader import load_dir
from .registry import BUILTIN_AGENTS, AgentRegistry
from .runner import (
    ASYNC_AGENT_ALLOWED_TOOLS,
    SUBAGENT_DISALLOWED_TOOL_NAMES,
    SubagentRequest,
    SubagentRunner,
    assemble_subagent_tools,
)
from .types import AgentDefinition

__all__ = [
    "AgentDefinition",
    "AgentRegistry",
    "BUILTIN_AGENTS",
    "load_dir",
    "SUBAGENT_DISALLOWED_TOOL_NAMES",
    "ASYNC_AGENT_ALLOWED_TOOLS",
    "SubagentRequest",
    "SubagentRunner",
    "assemble_subagent_tools",
]
