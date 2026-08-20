"""types 契约层测试(spec 12.1 镜像清单:test_types.py)。"""

import pytest
from pydantic import ValidationError

from codesage.mcp import MCP_METHODS
from codesage.mcp.types import (
    ConfigScope,
    McpHttpServerConfig,
    McpOAuthConfig,
    McpStdioServerConfig,
    ScopedMcpServerConfig,
)


def test_scope_enum_has_builtin():
    """spec §3.1:BUILTIN 是最顶层 scope。"""
    assert ConfigScope.BUILTIN.value == "builtin"
    assert ConfigScope.PROJECT.value == "project"
    assert ConfigScope.ENTERPRISE.value == "enterprise"


def test_stdio_config_requires_command():
    """spec §3.1:stdio 配置 command 必填。"""
    with pytest.raises(ValidationError):
        McpStdioServerConfig(args=["-y"])
    cfg = McpStdioServerConfig(command="npx", args=["-y", "@x/y"])
    assert cfg.type is None  # 缺省 = stdio
    assert cfg.args == ["-y", "@x/y"]


def test_stdio_type_may_be_explicit():
    cfg = McpStdioServerConfig(type="stdio", command="echo")
    assert cfg.type == "stdio"


def test_http_config_requires_https():
    """spec §3.1:http 远程 url 必须 https;localhost 例外。"""
    with pytest.raises(ValidationError):
        McpHttpServerConfig(type="http", url="http://api.example.com/mcp")
    cfg = McpHttpServerConfig(type="http", url="https://api.example.com/mcp")
    assert cfg.type == "http"
    local = McpHttpServerConfig(type="http", url="http://localhost:8080/mcp")
    assert local.url.startswith("http://localhost")


def test_oauth_metadata_requires_https():
    """spec §9.2:auth_server_metadata_url 必须 https。"""
    with pytest.raises(ValidationError):
        McpOAuthConfig(auth_server_metadata_url="http://auth.example.com")
    cfg = McpOAuthConfig(auth_server_metadata_url="https://auth.example.com", client_id="abc")
    assert cfg.client_id == "abc"


def test_scoped_config_flattens_stdio_fields():
    """spec §3.1:平铺归一,stdio 字段可直接构造。"""
    cfg = ScopedMcpServerConfig(
        name="echo",
        scope=ConfigScope.LOCAL,
        command="python",
        args=["-m", "codesage.mcp.builtin.echo_server"],
    )
    assert cfg.type == "stdio"
    assert cfg.signature() == "stdio:python:['-m', 'codesage.mcp.builtin.echo_server']"


def test_scoped_config_signature_normalizes_url():
    """spec §5.3:URL 签名去掉尾部斜杠,scope 不参与。"""
    a = ScopedMcpServerConfig(name="s", scope=ConfigScope.USER, url="https://api.x.com/mcp/")
    b = ScopedMcpServerConfig(name="s", scope=ConfigScope.LOCAL, url="https://api.x.com/mcp")
    assert a.signature() == b.signature()
    assert a.signature() == "url:https://api.x.com/mcp"
    assert a.signature() != "url:https://api.x.com/mcp/"

def test_mcp_methods_constants():
    """spec §3.3:协议方法名常量。"""
    assert MCP_METHODS.TOOLS_CALL == "tools/call"
    assert MCP_METHODS.TOOLS_LIST == "tools/list"
    assert MCP_METHODS.NOTIFICATION_TOOLS_LIST_CHANGED == "notifications/tools/list_changed"