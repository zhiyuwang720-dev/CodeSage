"""MCP client (phase 15): contract types, JSON-RPC, transports, config, connection management.

契约层(types.py + jsonrpc.py)不依赖实现;transports/client 依赖契约层。MCP
= 让外部服务器按 JSON-RPC 协议暴露工具/资源/提示词,本包负责连接、桥接与结果治理。
"""

from .jsonrpc import (
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    decode,
    encode,
    next_id,
)
from .types import (
    MCP_METHODS,
    McpConnection,
    McpConnectionState,
    McpHttpServerConfig,
    McpJsonConfig,
    McpOAuthConfig,
    McpServerConfig,
    McpStdioServerConfig,
    ConfigScope,
    ScopedMcpServerConfig,
)
from .client import McpManager
from .tool import (
    ListMcpResourcesTool,
    McpTool,
    ReadMcpResourceTool,
    build_mcp_tools,
)

__all__ = [
    "MCP_METHODS",
    "McpConnection",
    "McpConnectionState",
    "McpHttpServerConfig",
    "McpJsonConfig",
    "McpOAuthConfig",
    "McpManager",
    "McpServerConfig",
    "McpStdioServerConfig",
    "ConfigScope",
    "ScopedMcpServerConfig",
    "McpTool",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
    "build_mcp_tools",
    "JsonRpcError",
    "JsonRpcNotification",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "decode",
    "encode",
    "next_id",
]