"""引擎级影响面约束层:改动最小集,拦截/引导工具调用到最小侵入路径。

核心裁决:最小改动 = 引擎级约束,非提示词要求。对写操作(Edit/Write),先查图谱影响面,给「改动最小集」建议。不改变权限决策链,
是独立叠加关卡;默认只「建议引导」,可 CODESAGE_NO_MINIMAL_CHANGE 关闭硬拦。
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: 写操作工具(约束层只引导这些;读操作放行)
WRITE_TOOLS = frozenset({"Write", "Edit"})


def minimal_change_enabled() -> bool:
    """约束层开关:CODESAGE_NO_MINIMAL_CHANGE 关闭硬拦(仍留建议)。"""
    return os.environ.get("CODESAGE_NO_MINIMAL_CHANGE", "") != "1"


class MinimalChangeGuard:
    """改动最小集约束器

    对写目标,查图谱「谁调用/谁依赖」,产出影响集与最小集建议。
    ponytail 阶梯编码进建议生成:删除优先/复用既有/根因修复/一行优先。
    """

    def __init__(self, intel) -> None:
        self._intel = intel  # CodeIntelligenceService

    async def guard(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """约束检查:返回改动建议(None = 放行,无需引导)。

        *tool_name*:工具名(Write/Edit);*tool_input*:工具输入(含 file_path/path)。
        只约束写操作;建议是「引导」,不替代权限决策。
        """
        if tool_name not in WRITE_TOOLS:
            return None
        if not minimal_change_enabled():
            return None
        target = tool_input.get("file_path") or tool_input.get("path")
        if not target:
            return None
        if self._intel is None or not self._intel.available:
            return None
        # 影响面:查目标符号/文件的入站调用(谁依赖它)
        impact = await self._intel.impact_of_change(str(target))
        if not impact:
            return None
        return self._build_suggestion(target, impact)

    def _build_suggestion(self, target: str, impact: dict) -> str:
        """按 ponytail 阶梯生成改动建议"""
        from .ponytail import ladder_suggestion

        callers = int(impact.get("callers_total", 0) or 0)
        ladder = ladder_suggestion(callers)
        if callers == 0:
            return f"[minimal-change] 目标 {target}: {ladder}"
        return (
            f"[minimal-change] 改动 {target} 将影响 {callers} 个调用者。"
            f"{ladder}"
        )


async def minimal_change_guard(item, intel, state) -> Any:
    """引擎接线辅助(loop 调用):对写操作叠加约束层,返回建议文本或 None。"""
    guard = MinimalChangeGuard(intel)
    return await guard.guard(item.tool.name, item.input)