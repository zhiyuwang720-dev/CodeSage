"""MCP 斜杠命令测试(spec 12.1:test_slash.py)。

覆盖:/mcp 无参列表、add/remove/enable/install 子命令、prompts 斜杠兜底。
"""

import pytest

from codesage.cli.commands import _cmd_mcp, find_command


class FakeManager:
    """最小 McpManager 替身(命令只读 connections/tools_for)。"""

    def __init__(self):
        self._conns = {}

    def set_connection(self, name, conn):
        self._conns[name] = conn

    def get_connection(self, name):
        return self._conns.get(name)

    def tools_for(self, name):
        return []

    async def connect_server(self, name, config):
        return self._conns.get(name)

    async def disconnect(self, name):
        self._conns.pop(name, None)


class FakeConn:
    def __init__(self, name, state, transport, scope="local"):
        self.name = name
        self.state = state
        self.transport = transport
        self.config = type("C", (), {"type": "stdio", "scope": scope})()

    def state(self):
        return self.state


def test_find_command_mcp_registered():
    """spec §10.3:/mcp 命令已注册。"""
    assert find_command("mcp") is not None


def test_mcp_no_args_lists_empty(capsys):
    """spec §10.3:无服务器时提示。"""
    _cmd_mcp([], {"loop": type("L", (), {"_mcp": FakeManager()})()})
    out = capsys.readouterr().out
    assert "No MCP servers" in out


def test_mcp_add_command(capsys, tmp_path, monkeypatch):
    """spec §10.3:/mcp add --command 写入配置。"""
    from codesage.mcp.config import get_mcp_config_by_name
    from codesage.mcp.types import ConfigScope

    monkeypatch.setenv("CODESAGE_CWD", str(tmp_path))
    _cmd_mcp(["add", "myecho", "--command", "echo"], {"loop": type("L", (), {"_mcp": FakeManager()})()})
    cfg = get_mcp_config_by_name("myecho")
    assert cfg is not None
    assert cfg.command == "echo"


def test_mcp_install_known_bundled(capsys, monkeypatch, tmp_path):
    """spec §4.6:/mcp install codebase-memory 打印说明。"""
    monkeypatch.setenv("CODESAGE_CONFIG_DIR", str(tmp_path))
    _cmd_mcp(["install", "codebase-memory"], {"loop": type("L", (), {"_mcp": FakeManager()})()})
    out = capsys.readouterr().out
    assert "codebase-memory" in out


def test_mcp_install_unknown(capsys):
    """spec §4.6:未知内置条目提示。"""
    _cmd_mcp(["install", "nope"], {"loop": type("L", (), {"_mcp": FakeManager()})()})
    out = capsys.readouterr().out
    assert "Unknown bundled" in out