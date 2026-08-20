"""jsonrpc 编解码测试(spec 12.1 镜像清单:test_jsonrpc.py)。"""

import json

import pytest

from codesage.mcp import decode, encode, next_id
from codesage.mcp.jsonrpc import JsonRpcError, JsonRpcNotification, JsonRpcRequest, JsonRpcResponse


def test_next_id_increments():
    """spec §3.3:请求 id 自增。"""
    a, b = next_id(), next_id()
    assert b == a + 1


def test_encode_request():
    req = JsonRpcRequest(id=1, method="tools/list", params={})
    line = encode(req)
    parsed = json.loads(line)
    assert parsed == {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    assert line.endswith("\n") is False  # 单行,无尾换行(stdio 由 transport 加)


def test_encode_response():
    resp = JsonRpcResponse(id=1, result={"tools": []})
    parsed = json.loads(encode(resp))
    assert parsed["result"] == {"tools": []}
    assert "error" not in parsed or parsed["error"] is None


def test_decode_request():
    msg = decode('{"jsonrpc":"2.0","id":2,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}')
    assert isinstance(msg, JsonRpcRequest)
    assert msg.id == 2
    assert msg.method == "initialize"
    assert msg.params["protocolVersion"] == "2025-03-26"


def test_decode_response_with_result():
    msg = decode('{"jsonrpc":"2.0","id":2,"result":{"serverInfo":{"name":"echo"}}}')
    assert isinstance(msg, JsonRpcResponse)
    assert msg.id == 2
    assert msg.result["serverInfo"]["name"] == "echo"
    assert msg.error is None


def test_decode_error_response():
    msg = decode('{"jsonrpc":"2.0","id":3,"error":{"code":-32000,"message":"Connection closed"}}')
    assert isinstance(msg, JsonRpcResponse)
    assert msg.error is not None
    assert msg.error.code == -32000
    assert msg.error.message == "Connection closed"


def test_decode_notification():
    msg = decode('{"jsonrpc":"2.0","method":"notifications/tools/list_changed","params":{}}')
    assert isinstance(msg, JsonRpcNotification)
    assert msg.method == "notifications/tools/list_changed"


def test_decode_malformed_lines_raise():
    """spec §3.3:畸形行抛 ValueError,调用方按协议忽略/记录。"""
    # '{"jsonrpc":"2.0","id":1}' 是合法响应形状(无 method),不在此列
    for bad in ("not json", "{broken", '{"jsonrpc":"1.0"}'):
        with pytest.raises(ValueError):
            decode(bad)


def test_error_model_roundtrip():
    err = JsonRpcError(code=-32001, message="Request timeout", data={"detail": "x"})
    parsed = json.loads(encode(JsonRpcResponse(id=1, error=err)))
    assert parsed["error"] == {"code": -32001, "message": "Request timeout", "data": {"detail": "x"}}