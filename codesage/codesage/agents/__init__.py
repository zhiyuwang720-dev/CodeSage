"""Agent definitions and registry (phase 13): frontmatter loading, layered
priority merge (project > user > builtin), builtin trio.

S1 delivers the definition layer; the Agent tool (S2), forkContext (S3),
permission narrowing (S4) and background/Mailbox (S5) build on it.
"""

from .loader import load_dir
from .registry import BUILTIN_AGENTS, AgentRegistry
from .types import AgentDefinition

__all__ = ["AgentDefinition", "AgentRegistry", "BUILTIN_AGENTS", "load_dir"]
