"""MCP 命名与过滤辅助(spec §7.1)。

命名规则 `mcp__server__tool` 同时解决撞名、过滤、权限规则定位与审计归属。
对应 CC `src/services/mcp/mcpStringUtils.ts` + `normalization.ts`。
"""

from __future__ import annotations

from typing import Any


def normalize_name_for_mcp(name: str) -> str:
    """名字归一化:非 [a-zA-Z0-9_-] 全换下划线(满足工具名约束)。"""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    """拼全名:如 `mcp__github__create_issue`(spec §7.1)。"""
    return f"mcp__{normalize_name_for_mcp(server_name)}__{normalize_name_for_mcp(tool_name)}"


def mcp_info_from_string(tool_string: str) -> tuple[str, str] | None:
    """从全名解析回 (server, tool):`mcp__a__b__c` -> ("a", "b__c")。

    已知局限:服务器名含 `__` 时解析不准(取首个 __ 分割),与 CC 同款成文。
    """
    parts = tool_string.split("__")
    if len(parts) < 3 or parts[0] != "mcp":
        return None
    return parts[1], "__".join(parts[2:])


def get_mcp_prefix(server_name: str) -> str:
    """服务器工具名前缀:如 `mcp__github__`(过滤/替换用)。"""
    return f"mcp__{normalize_name_for_mcp(server_name)}__"


def truncate_text(text: str, limit: int = 2048) -> str:
    """截断到 *limit* 字符(超长描述/instructions 防爆上下文;spec §7.1)。"""
    return text if len(text) <= limit else text[:limit] + "… [truncated]"