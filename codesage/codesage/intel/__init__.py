"""CodeIntelligenceService + 影响面约束层(阶段 20 战略转向)。

把 codebase-memory-mcp 从「可选 MCP 服务器」提升为「核心服务」:启动自动索引当前
代码库,暴露影响面查询接口,供引擎约束层与 agent 上下文消费。最小改动 = 引擎级约束,
非提示词要求(spec 20 §2 裁决 2)。
"""

from .service import CodeIntelligenceService, discover_cbm_cli
from .minimal_change import MINIMAL_CHANGE_BLOCKED, WRITE_TOOLS, MinimalChangeGuard
from .ponytail import PONYTAIL_FULL_BODY, register_ponytail

__all__ = [
    "CodeIntelligenceService",
    "MinimalChangeGuard",
    "MINIMAL_CHANGE_BLOCKED",
    "PONYTAIL_FULL_BODY",
    "WRITE_TOOLS",
    "discover_cbm_cli",
    "register_ponytail",
]