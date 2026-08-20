"""MCP 结果治理测试(spec 12.1:test_result.py)。

覆盖:形状归一(文本/JSON/错误)、25K token 截断、空结果标记、图片占位。
"""

import pytest

from codesage.mcp.result import (
    empty_result_marker,
    get_max_mcp_output_tokens,
    mcp_result_to_content,
    process_mcp_result,
    truncate_mcp_content,
)


def test_text_content_normalized():
    """spec §8.1:文本内容块归一为纯文本。"""
    result = {"content": [{"type": "text", "text": "hello world"}]}
    assert mcp_result_to_content(result) == "hello world"


def test_multi_text_blocks_joined():
    """spec §8.1:多个文本块换行拼接。"""
    result = {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    assert mcp_result_to_content(result) == "a\nb"


def test_structured_content_json():
    """spec §8.1:structuredContent 归一为 JSON。"""
    result = {"structuredContent": {"a": 1}}
    assert mcp_result_to_content(result).strip() == '{\n  "a": 1\n}'


def test_error_result_extracts_first_text():
    """spec §8.1:isError 提取首条文本作为错误信息(模型自愈)。"""
    result = {"isError": True, "content": [{"type": "text", "text": "boom"}]}
    assert mcp_result_to_content(result) == "boom"


def test_image_block_placeholder():
    """spec §8.1:图片块标记占位(截断层压缩在 truncate 阶段)。"""
    result = {"content": [{"type": "image", "data": "xxx"}, {"type": "text", "text": "after"}]}
    assert "[image]" in mcp_result_to_content(result)


def test_empty_result():
    """spec §8.4:空结果。"""
    assert mcp_result_to_content({}) == "(empty result)"
    assert mcp_result_to_content({"content": []}) == "(empty result)"


def test_truncate_below_limit_unchanged():
    """spec §8.1:未超限不截断。"""
    short = "x" * 100
    assert truncate_mcp_content(short) == short


def test_truncate_over_limit_appends_hint():
    """spec §8.1:超限截断并附提示。"""
    big = "x" * (get_max_mcp_output_tokens() * 4 * 2)  # 远超 25K token
    out = truncate_mcp_content(big)
    assert len(out) < len(big)
    assert "[OUTPUT TRUNCATED" in out


def test_empty_result_marker():
    """spec §8.4:空结果标记文案。"""
    assert empty_result_marker("echo") == "(echo completed with no output)"