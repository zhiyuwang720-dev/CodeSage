"""引擎级影响面约束层:改动最小集,拦截/引导工具调用到最小侵入路径。

核心裁决:最小改动 = 引擎级约束,非提示词要求。对写操作(Edit/Write),先查图谱影响面,
产出「改动最小集」建议并**执行前拦截一次**(spec 20 §4.1:最好的代码是从未写过的代码)。

与权限决策链(05)共存,不替代:deny>ask>allow 零改动,本层是独立叠加关卡。拦截语义
(用户确认):同一目标拦一次,重试放行。`CODESAGE_NO_MINIMAL_CHANGE` 关闭硬拦。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..tools.base import ToolResult

logger = logging.getLogger(__name__)

#: 写操作工具(约束层只拦这些;读操作放行)
WRITE_TOOLS = frozenset({"Write", "Edit"})

#: 拦截结果 error_code(sibling 策略按此豁免株连;不污染 permission_blocked)
MINIMAL_CHANGE_BLOCKED = "minimal_change_blocked"

#: 建议格式常量(重试放行说明 + ponytail 输出契约)
RETRY_HINT = "若该改动确为有意为之,直接重试该操作将放行。"
OUTPUT_CONTRACT = "[code] — skipped: [X], add when [Y].(ponytail 输出契约:先最小代码,再说明跳过什么)"


def minimal_change_enabled() -> bool:
    """约束层开关:CODESAGE_NO_MINIMAL_CHANGE 关闭硬拦(仍留建议)。"""
    return os.environ.get("CODESAGE_NO_MINIMAL_CHANGE", "") != "1"


class MinimalChangeGuard:
    """改动最小集约束器(每会话一个实例,持有拦截记忆)。

    对写目标,查图谱「谁调用/谁依赖」,产出影响集与最小集建议,执行前拦截一次。
    同一目标重复操作放行(拦一次语义,用户确认);任何失败 fail-open 放行。
    """

    def __init__(self, intel, wait_ready_s: float = 10.0) -> None:
        self._intel = intel  # CodeIntelligenceService
        self._wait_ready_s = wait_ready_s
        self._seen: set[str] = set()

    async def guard(self, tool_name: str, tool_input: dict[str, Any]) -> ToolResult | None:
        """约束检查:返回拦截结果(None = 放行)。引擎执行工具前调用。

        *tool_name*:工具名(Write/Edit);*tool_input*:工具输入(含 file_path/path)。
        只约束写操作;拦截以 ToolResult + minimal_change_blocked 呈现,不替代权限决策。
        """
        if tool_name not in WRITE_TOOLS:
            return None
        if not minimal_change_enabled():
            return None
        target = tool_input.get("file_path") or tool_input.get("path")
        if not target:
            return None
        tkey = self._normalize(str(target))
        if tkey in self._seen:
            return None  # 拦一次语义:同一目标重复操作放行
        if self._intel is None or not self._intel.discoverable:
            return None
        try:
            if not await self._intel.wait_ready(timeout_s=self._wait_ready_s):
                return None  # 索引未就绪:fail-open 放行
            impact = await self._intel.impact_of_change(tkey)
        except Exception as exc:  # noqa: BLE001
            logger.warning("minimal-change guard fail-open: %s", exc)
            return None
        advice = self._build_advice(str(target), impact)
        if advice is None:
            return None  # 查询失败/无信号:放行
        self._seen.add(tkey)
        logger.info("minimal-change blocked %s (%s)", tool_name, target)
        return ToolResult(advice, is_error=True, metadata={"error_code": MINIMAL_CHANGE_BLOCKED})

    @staticmethod
    def _normalize(target: str) -> str:
        """目标归一化:Windows 路径分隔符统一 + 大小写不敏感。"""
        return target.replace("\\", "/").lower()

    def _build_advice(self, target: str, impact: dict | None) -> str | None:
        """多场景建议生成(ponytail 阶梯编码 + 输出契约)。返回 None = 放行。"""
        if not impact:
            return None
        status = impact.get("status")
        if status == "error":
            return None  # 查询失败不是 YAGNI 信号,不误导
        if status == "ambiguous":
            sugs = impact.get("suggestions", [])[:5]
            lines = "\n".join(
                f"    {s.get('qualified_name')} ({s.get('file_path')})" for s in sugs
            )
            return (
                f"[minimal-change] 改动 {target} 的符号名在图谱中歧义(多个命中):\n{lines}\n"
                f"建议:用全限定名重试,或先确认要改的确切符号。"
                f"{RETRY_HINT}\n{OUTPUT_CONTRACT}"
            )
        if status == "not_found":
            return (
                f"[minimal-change] 改动 {target} 在图谱中未找到(新文件或未索引符号)。"
                f"ponytail 阶梯 1:确认它是否需要存在(YAGNI);若为新逻辑,先查库内既有 helper 是否可复用。"
                f"{RETRY_HINT}\n{OUTPUT_CONTRACT}"
            )
        # status == ok
        callers_total = int(impact.get("callers_total", 0) or 0)
        if callers_total == 0:
            return (
                f"[minimal-change] 改动 {target}:ponytail 阶梯 1 —— 此目标无入站调用者。"
                f"先确认改动是否真的需要存在(YAGNI);若新增独立逻辑,查库内既有 helper 是否可复用。"
                f"{RETRY_HINT}\n{OUTPUT_CONTRACT}"
            )
        if callers_total == 1:
            return (
                f"[minimal-change] 改动 {target} 有 1 个调用者。ponytail 阶梯 2 —— 根因修复优先:"
                f"改共享函数/根因处(一处)而非逐调用点修补;库内已有模式则复用;一行能解决则一行。"
                f"{RETRY_HINT}\n{OUTPUT_CONTRACT}"
            )
        callers = impact.get("callers", [])[:3]
        caller_line = "、".join(callers) if callers else "影响面大"
        return (
            f"[minimal-change] 改动 {target} 将影响 {callers_total} 个调用者({caller_line})。"
            f"ponytail 阶梯 2/6 —— 优先改共享函数/根因(一处)而非逐调用点;库内已有模式则复用;"
            f"一行能解决则一行。{RETRY_HINT}\n{OUTPUT_CONTRACT}"
        )
