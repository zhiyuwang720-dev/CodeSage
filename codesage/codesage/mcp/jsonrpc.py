"""JSON-RPC 2.0 消息编解码(spec §3.3,零依赖自研)。

MCP 的"语言"就是 JSON-RPC:请求/响应/通知三种消息,单行 JSON 序列化,
按 id 配对请求与响应。协议细节见 spec §3.3 与 `docs/claude-mcp实现.md` §1.2。
"""

import itertools
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

#: 请求 id 计数器(进程内自增,id 用于配对请求/响应)
_id_counter = itertools.count(1)


def next_id() -> int:
    """取下一个请求 id。"""
    return next(_id_counter)


class JsonRpcError(BaseModel):
    """JSON-RPC 错误对象(code/message/data)。"""

    code: int
    message: str
    data: Any = None


class JsonRpcRequest(BaseModel):
    """请求消息(带 id,必有 method)。"""

    jsonrpc: str = "2.0"
    id: int | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JsonRpcResponse(BaseModel):
    """响应消息(带 id,result 与 error 二选一)。"""

    jsonrpc: str = "2.0"
    id: int | None = None
    result: Any = None
    error: JsonRpcError | None = None


class JsonRpcNotification(BaseModel):
    """通知消息(无 id,服务器主动推送,无需响应)。"""

    jsonrpc: str = "2.0"
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


def encode(message: BaseModel) -> str:
    """消息编码为单行 JSON 字符串(stdio 传输的线上格式)。"""
    return message.model_dump_json()

def decode(line: str) -> JsonRpcRequest | JsonRpcResponse | JsonRpcNotification:
    """单行 JSON 解码为消息对象;畸形行抛 ValueError(调用方按协议忽略/记录)。

    id 存在 = 请求或响应;无 id = 通知。响应中 error 字段存在 = 错误响应。
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError as e:
        raise ValueError(f"malformed JSON-RPC line: {e}") from e

    if not isinstance(data, dict) or data.get("jsonrpc") != "2.0":
        raise ValueError("not a JSON-RPC 2.0 message")

    try:
        if "method" in data:
            if "id" in data:
                return JsonRpcRequest.model_validate(data)
            return JsonRpcNotification.model_validate(data)
        return JsonRpcResponse.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"invalid JSON-RPC message: {e}") from e