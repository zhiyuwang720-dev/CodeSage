"""core/agent-loop/src —— agent 循环服务内核实现。

导出清单照 参考实现 agent-loop 的公共面:驱动(ReactLoopAgent)、
服务(AgentLoop)、取消原语、并发上限、运行时上下文投影与
启动器身份键。包根 __init__.py 转发这里。
"""

from .abort import AbortController, AbortError, AbortSignal, any_signals
from .agent import ReactLoopAgent
from .constants import DEFAULT_MAX_PARALLEL_TOOL_CALLS
from .index import (
    AGENT_LOOP_SETTINGS_NAMESPACE,
    CONFIGURED_AGENT_IDENTITIES_KEY,
    AgentLoop,
)
from .runtime_context import RuntimeContextProjection
from .tool_calls import execute_tool_calls

__all__ = [
    "AGENT_LOOP_SETTINGS_NAMESPACE",
    "CONFIGURED_AGENT_IDENTITIES_KEY",
    "DEFAULT_MAX_PARALLEL_TOOL_CALLS",
    "AbortController",
    "AbortError",
    "AbortSignal",
    "AgentLoop",
    "ReactLoopAgent",
    "RuntimeContextProjection",
    "any_signals",
    "execute_tool_calls",
]
