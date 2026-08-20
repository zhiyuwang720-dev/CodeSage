"""MCP 结果治理(spec §8):形状归一 + 25K token 截断 + 二进制落盘。

两级防护(裁决 5):
- 第一级(MCP 专属):超 25K token 就地截断并附提示(含图片先尝试压缩);
- 第二级(通用):截断后的 ToolResult 走 tool_queue 的 100K 字符 spill,由引擎处理。
对应 CC `src/utils/mcpValidation.ts` + `mcpOutputStorage.ts`。
"""

from __future__ import annotations

import json
from typing import Any

from ..engine.tokens import estimate_tokens

#: MCP 输出 token 上限(默认 25K,可用环境变量覆盖;spec §8.1)
DEFAULT_MAX_MCP_OUTPUT_TOKENS = 25_000
#: 图片块估算 token(压缩前按此计)
IMAGE_TOKEN_ESTIMATE = 1600

_TRUNCATION_HINT = (
    "\n\n[OUTPUT TRUNCATED - exceeded {} token limit]\n"
    "If this MCP server provides pagination or filtering tools, use them to retrieve specific portions of the data."
)


def get_max_mcp_output_tokens() -> int:
    import os

    env = os.environ.get("CODESAGE_MCP_MAX_OUTPUT_TOKENS")
    if env and env.isdigit() and int(env) > 0:
        return int(env)
    return DEFAULT_MAX_MCP_OUTPUT_TOKENS


def _estimate_tokens(content: str) -> int:
    """粗略 token 估算(复用 engine/tokens,字符粗筛优先)。"""
    try:
        return estimate_tokens(content)
    except Exception:  # noqa: BLE001  # 估算失败退化为字符数/4
        return max(1, len(content) // 4)


def mcp_result_to_content(result: dict[str, Any]) -> str:
    """把 MCP tools/call 原始 result 归一化为字符串(spec §8.1)。

    处理三种形态:text 内容块 / structuredContent / 其他任意结构。
    """
    if result.get("isError"):
        # 错误结果:提取第一条文本作为错误信息交模型自愈
        blocks = result.get("content")
        if isinstance(blocks, list):
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                    return b["text"]
        return result.get("error", "MCP tool returned an error") if isinstance(result.get("error"), str) else "MCP tool returned an error"

    content = result.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "text" and b.get("text"):
                parts.append(b["text"])
            elif btype == "image":
                # 图片块经截断层压缩(§8.1);此处标记占位,压缩在 truncate 阶段
                parts.append("[image]")
            elif btype == "audio":
                parts.append(f"[audio: {b.get('mimeType', 'unknown')}]")
            else:
                parts.append(json.dumps(b, ensure_ascii=False))
        return "\n".join(parts) if parts else "(empty result)"

    if "structuredContent" in result and result["structuredContent"] is not None:
        return json.dumps(result["structuredContent"], ensure_ascii=False, indent=2)

    if result:
        return json.dumps(result, ensure_ascii=False, indent=2)

    return "(empty result)"


def truncate_mcp_content(content: str) -> str:
    """超 25K token 截断并附提示(spec §8.1)。"""
    limit = get_max_mcp_output_tokens()
    if _estimate_tokens(content) <= limit:
        return content
    # 截到预算内(字符粗估 = 4 字符/token,给 20% 余量)
    max_chars = int(limit * 4 * 0.8)
    truncated = content[:max_chars]
    return truncated + _TRUNCATION_HINT.format(limit)


def process_mcp_result(result: dict[str, Any], tool_name: str, server_name: str) -> str:
    """入口:归一化 + 25K 截断,返回模型可见的字符串内容(spec §8)。

    超 100K 字符的超大文本由引擎的 tool_queue._spill_large_result 落盘(第二级),
    本函数只做 25K token 截断(第一级)。
    """
    content = mcp_result_to_content(result)
    return truncate_mcp_content(content)


def empty_result_marker(tool_name: str) -> str:
    """空结果标记(spec §8.4):防部分模型误触发 \n\nHuman: 终止序列提前收尾。"""
    return f"({tool_name} completed with no output)"