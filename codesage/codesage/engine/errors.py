"""错误分类器(specs/10 §2):把 LLMError / 截断响应归入可恢复类,供恢复阶梯裁决。

None = 不可恢复,走原 error 路径。分类器只分类,恢复策略在 loop 的恢复闸门决定。
"""

from __future__ import annotations

from enum import Enum

from ..ai import LLMError
from ..ai.retry import is_ptl_error


class RecoveryClass(Enum):
    CONTEXT_OVERFLOW = "context_overflow"  # 413 / PTL 文本(已有路径,并入统一闸门)
    OUTPUT_OVERFLOW = "output_overflow"  # max_output_tokens / stop_reason=="length"(新增)


def classify_recoverable(
    exc: BaseException | None,
    stop_reason: str | None,
    last_block_is_truncated_tool_use: bool,
) -> RecoveryClass | None:
    """None = 不可恢复,走原错误路径。每类错误只在恢复闸门允许时执行一次恢复动作。

    - `is_ptl_error(exc)`(413 / 400 PTL)→ CONTEXT_OVERFLOW
    - `stop_reason == "length"` → OUTPUT_OVERFLOW(残缺 tool_use 与纯文本截断同归类,
      恢复与否由 §3.1 形态判定,即 `last_block_is_truncated_tool_use`,属 S3 恢复策略)
    - 其他 → None
    """
    if isinstance(exc, LLMError) and is_ptl_error(exc):
        return RecoveryClass.CONTEXT_OVERFLOW
    if stop_reason == "length":
        return RecoveryClass.OUTPUT_OVERFLOW
    return None
