"""错误分类器测试(specs/10 §2 三分表):413/PTL → CONTEXT_OVERFLOW,
stop_reason=="length" → OUTPUT_OVERFLOW,其余 → None。"""

import pytest

from codesage.ai import LLMError
from codesage.engine.errors import RecoveryClass, classify_recoverable


def _llm_error(status_code=None, message="boom", **kw):
    return LLMError(message, status_code=status_code, **kw)


# --- CONTEXT_OVERFLOW:HTTP 413 / 400 PTL 文本 ---


def test_413_is_context_overflow():
    assert classify_recoverable(_llm_error(413), None, False) == RecoveryClass.CONTEXT_OVERFLOW


def test_400_with_context_length_exceeded_is_context_overflow():
    exc = _llm_error(400, "This model's maximum context length is 128000 tokens")
    assert classify_recoverable(exc, None, False) == RecoveryClass.CONTEXT_OVERFLOW


def test_400_with_prompt_too_long_is_context_overflow():
    exc = _llm_error(400, "prompt_too_long: your request is too large")
    assert classify_recoverable(exc, None, False) == RecoveryClass.CONTEXT_OVERFLOW


def test_400_generic_error_is_not_recoverable():
    exc = _llm_error(400, "invalid request: bad parameter")
    assert classify_recoverable(exc, None, False) is None


# --- OUTPUT_OVERFLOW:stop_reason == "length" ---


def test_length_with_truncated_tool_use_is_output_overflow():
    assert classify_recoverable(None, "length", True) == RecoveryClass.OUTPUT_OVERFLOW


def test_length_without_truncated_tool_use_is_output_overflow():
    """形态 2(纯文本截断)同样归类,恢复与否由 S3 恢复策略决定(§3.1)。"""
    assert classify_recoverable(None, "length", False) == RecoveryClass.OUTPUT_OVERFLOW


def test_ptl_wins_over_stop_reason():
    """413 异常同时带 length 停止原因 → 上下文溢出优先(输入侧先判)。"""
    exc = _llm_error(413)
    assert classify_recoverable(exc, "length", False) == RecoveryClass.CONTEXT_OVERFLOW


def test_cancelled_error_is_not_recoverable():
    """cancelled LLMError 永不重试/回退(retry.py 语义),不可恢复。"""
    assert classify_recoverable(_llm_error(413, cancelled=True), None, False) is None


# --- 其他 → None ---


def test_429_is_not_recoverable():
    assert classify_recoverable(_llm_error(429), None, False) is None


def test_5xx_is_not_recoverable():
    assert classify_recoverable(_llm_error(500), None, False) is None
    assert classify_recoverable(_llm_error(503), None, False) is None


def test_plain_exception_is_not_recoverable():
    assert classify_recoverable(ValueError("boom"), None, False) is None


def test_none_input_is_not_recoverable():
    assert classify_recoverable(None, None, False) is None
    assert classify_recoverable(None, "end_turn", False) is None
