"""内建演示 MCP 服务器(spec §4.5):最小 stdio 服务器,测试桩 + 学习标本。

运行:python -m codesage.mcp.builtin.echo_server
输入输出走 stdin/stdout 行分隔 JSON(JSON-RPC 2.0),暴露 2 个工具 + 1 个资源 + 1 个提示词。

实现意图:用最少代码演示 MCP 服务端协议形态——任何外部服务器都长这样:
initialize 握手 → 声明 capabilities → 响应 tools/list / tools/call / 等。
"""

from __future__ import annotations

import json
import sys

#: 声明支持的能力(tools + resources + prompts 全开,便于测试覆盖)
CAPABILITIES = {"tools": {}, "resources": {}, "prompts": {}}

TOOLS = [
    {
        "name": "echo",
        "description": "Echo 输入的文本原样返回(测试用)。",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "add",
        "description": "两个整数相加(测试用)。",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
]

RESOURCES = [
    {"uri": "codesage://demo/version", "name": "codesage version", "mimeType": "text/plain", "description": "演示资源"},
]

PROMPTS = [
    {"name": "greet", "description": "问候指定名字。", "arguments": [{"name": "name", "description": "名字", "required": True}]},
]


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _respond(msg_id, result=None, error=None) -> None:
    _send({"jsonrpc": "2.0", "id": msg_id, "result": result, "error": error})


def handle(method: str, msg_id, params):
    """按 method 分发,模拟 MCP 服务端行为。"""
    if method == "initialize":
        _respond(
            msg_id,
            {
                "protocolVersion": "2025-03-26",
                "capabilities": CAPABILITIES,
                "serverInfo": {"name": "codesage-echo", "version": "0.1.0"},
            },
        )
    elif method == "notifications/initialized":
        pass  # 客户端已初始化通知,无需响应
    elif method == "tools/list":
        _respond(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        name = (params or {}).get("name")
        args = (params or {}).get("arguments", {}) or {}
        if name == "echo":
            _respond(msg_id, {"content": [{"type": "text", "text": str(args.get("text", ""))}]})
        elif name == "add":
            total = int(args.get("a", 0)) + int(args.get("b", 0))
            _respond(msg_id, {"content": [{"type": "text", "text": str(total)}]})
        else:
            _respond(msg_id, None, {"code": -32602, "message": f"unknown tool: {name}"})
    elif method == "resources/list":
        _respond(msg_id, {"resources": RESOURCES})
    elif method == "resources/read":
        _respond(msg_id, {"contents": [{"uri": "codesage://demo/version", "mimeType": "text/plain", "text": "0.1.0"}]})
    elif method == "prompts/list":
        _respond(msg_id, {"prompts": PROMPTS})
    elif method == "prompts/get":
        name = (params or {}).get("name")
        args = (params or {}).get("arguments", {}) or {}
        if name == "greet":
            who = args.get("name", "world")
            _respond(
                msg_id,
                {"description": "greet", "messages": [{"role": "user", "content": {"type": "text", "text": f"你好 {who}!"}}]},
            )
        else:
            _respond(msg_id, None, {"code": -32602, "message": f"unknown prompt: {name}"})
    else:
        _respond(msg_id, None, {"code": -32601, "message": f"method not found: {method}"})


def main() -> None:
    """stdio 主循环:读一行 JSON,处理,写回一行 JSON。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _respond(None, None, {"code": -32700, "message": "parse error"})
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if method is None or msg_id is None:  # 通知(无 id)无需响应
            continue
        handle(method, msg_id, msg.get("params", {}))


if __name__ == "__main__":
    main()