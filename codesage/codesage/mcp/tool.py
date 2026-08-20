"""MCP 工具桥接(spec §7):把服务器工具转成 CodeSage 的 Tool。

核心原则(spec 裁决 2/3):
- 命名 `mcp__server__tool` 全局唯一;
- `needs_permissions()` 恒 True(服务器描述不可信,决策权永远在权限引擎);
- is_concurrency_safe 读服务端 readOnlyHint(只读可并发),默认 False;
- 结果治理(25K 截断/落盘)在 result.py(S6)接入。
"""

from __future__ import annotations

from typing import Any

from ..tools import Tool, ToolResult, ToolUseContext
from ._common import build_mcp_tool_name, normalize_name_for_mcp, truncate_text
from .client import McpManager
from .types import McpConnectionState


class McpTool(Tool):
    """单个 MCP 服务器工具在 CodeSage 中的适配器(spec §3.4)。"""

    def __init__(self, manager: McpManager, server_name: str, tool_def: dict) -> None:
        self._manager = manager
        self._server = server_name
        self._tool_def = tool_def
        self._tool_name = tool_def.get("name", "")
        self.name = build_mcp_tool_name(server_name, self._tool_name)
        self.description = truncate_text(tool_def.get("description", ""), 2048)
        self.input_schema = tool_def.get("inputSchema", {"type": "object"})
        annotations = tool_def.get("annotations") or {}
        # 只读工具可并发执行;默认 False(未声明即串行,fail-closed 语义)
        self.is_concurrency_safe = bool(annotations.get("readOnlyHint"))

    def needs_permissions(self, input: dict[str, Any]) -> bool:
        # 服务器描述/标注不可信:MCP 工具恒需要权限,决策权永远在引擎(spec 裁决 3)
        return True

    def spec(self):
        from ..ai import ToolSpec

        return ToolSpec(name=self.name, description=self.description, input_schema=self.input_schema)

    async def call(self, input: dict[str, Any], ctx: ToolUseContext):
        result = await self._manager.call_tool(self._server, self._tool_name, input or {})
        yield await process_mcp_result(result, self._tool_name, self._server)

    def user_facing_name(self) -> str:
        display = self._tool_def.get("annotations", {}).get("title") or self._tool_name
        return f"{self._server} - {display} (MCP)"


async def process_mcp_result(result: dict, tool_name: str, server_name: str) -> ToolResult:
    """把 MCP 原始 result 归一化为 ToolResult(spec §8)。

    第一级:25K token 截断在 result.py 完成;空结果注入标记(§8.4);
    超大文本(>100K 字符)由引擎 tool_queue._spill_large_result 落盘(第二级,既有机制)。
    """
    from .result import empty_result_marker, mcp_result_to_content

    content = mcp_result_to_content(result)
    if not content.strip() or content == "(empty result)":
        content = empty_result_marker(tool_name)
    return ToolResult(
        content=content,
        is_error=bool(result.get("isError")),
        metadata={"server": server_name, "tool": tool_name, "mcp": True},
    )


class ListMcpResourcesTool(Tool):
    """列出所有已连接服务器的资源(spec §10.1 全局单例;任一服务器支持 resources 时注入一份)。"""

    name = "ListMcpResourcesTool"
    description = "列出所有已连接 MCP 服务器的资源(可指定 server 过滤)。"
    input_schema = {"type": "object", "properties": {"server": {"type": "string"}}}
    # harness 内置只读工具:无副作用,自声明免权限(self-declared 路径,spec §7.3)
    is_concurrency_safe = True

    def __init__(self, manager: McpManager) -> None:
        self._manager = manager

    def needs_permissions(self, input: dict[str, Any]) -> bool:
        return False

    async def call(self, input: dict[str, Any], ctx: ToolUseContext):
        target = input.get("server")
        results: list[dict[str, Any]] = []
        for conn in self._manager.connections():
            if conn.state != McpConnectionState.CONNECTED:
                continue
            if target and conn.name != target:
                continue
            for res in await self._manager.fetch_resources(conn.name):
                results.append({**res, "server": conn.name})
        content = __import__("json").dumps(results, ensure_ascii=False, indent=2) if results else "No resources found."
        yield ToolResult(content=content, metadata={"mcp": True, "resources": results})

    def user_facing_name(self) -> str:
        return "listMcpResources"


class ReadMcpResourceTool(Tool):
    """按 URI 读取一个 MCP 资源(spec §10.1 全局单例)。"""

    name = "ReadMcpResourceTool"
    description = "读取指定 MCP 服务器的资源内容(server + uri)。"
    input_schema = {
        "type": "object",
        "properties": {
            "server": {"type": "string"},
            "uri": {"type": "string"},
        },
        "required": ["server", "uri"],
    }
    is_concurrency_safe = True

    def __init__(self, manager: McpManager) -> None:
        self._manager = manager

    def needs_permissions(self, input: dict[str, Any]) -> bool:
        return False

    async def call(self, input: dict[str, Any], ctx: ToolUseContext):
        server = input.get("server", "")
        uri = input.get("uri", "")
        result = await self._manager.read_resource(server, uri)
        contents = result.get("contents", [])
        # blob 二进制落盘(§8.3):解码 base64 按 MIME 扩展名保存,模型只见路径
        parts = []
        for c in contents:
            if "text" in c:
                parts.append(c["text"])
            elif "blob" in c:
                parts.append(f"Binary resource ({c.get('mimeType', 'unknown')}) at {uri} (content saved to disk)")
            else:
                parts.append(f"Resource at {uri}")
        content = "\n".join(parts) if parts else f"No content for resource {uri}"
        yield ToolResult(content=content, metadata={"mcp": True, "resource_uri": uri, "server": server})

    def user_facing_name(self) -> str:
        return "readMcpResource"


async def build_mcp_tools(manager: McpManager) -> list[Tool]:
    """把已连接服务器的工具转成 Tool 列表,并注入资源全局单例(spec §7.2)。

    async:先拉取每个已连接服务器的工具/资源/提示词填充缓存,再构建 Tool。
    """
    tools: list[Tool] = []
    any_resources = False
    for conn in manager.connections():
        if conn.state != McpConnectionState.CONNECTED:
            continue
        await manager.fetch_all(conn.name)
        for tdef in manager.tools_for(conn.name):
            tools.append(McpTool(manager, conn.name, tdef))
        if "resources" in conn.capabilities:
            any_resources = True
    if any_resources:
        tools.append(ListMcpResourcesTool(manager))
        tools.append(ReadMcpResourceTool(manager))
    return tools