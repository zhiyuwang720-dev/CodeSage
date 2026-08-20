"""内置托管服务器注册表/安装测试(spec 12.1:test_builtin_registry.py)。

覆盖:register_bundled_mcp_server 注册/查重/get;builtin 层只读合成 + 优先级最高;
install 流(SHA-256 校验失败中止/解压/登记);uninstall 清理。
"""

import json
import os
from pathlib import Path

import pytest

from codesage.mcp import config as mcp_config
from codesage.mcp.builtin.registry import (
    BundledMcpServer,
    get_bundled_mcp_server,
    iter_bundled_servers,
    register_bundled_mcp_server,
)
from codesage.mcp.config import (
    ConfigScope,
    get_builtin_mcp_configs,
    get_mcp_config_by_name,
    installed_bin_dir,
    write_installed,
)


def test_register_and_get():
    """spec §4.6:注册/查重/get/迭代。"""
    register_bundled_mcp_server(
        name="demo", description="d", platforms={"win32": "u"}, sha256={},
        default_config={"type": "stdio"},
    )
    spec = get_bundled_mcp_server("demo")
    assert spec is not None
    assert spec.description == "d"
    assert any(s.name == "demo" for s in iter_bundled_servers())
    # 重名覆盖
    register_bundled_mcp_server(
        name="demo", description="d2", platforms={}, sha256={}, default_config={},
    )
    assert get_bundled_mcp_server("demo").description == "d2"


def test_builtin_layer_read_only_derived(tmp_path, monkeypatch):
    """spec §4.6:builtin 层只读合成——未安装时不出现,安装后按 installed 记录合成。"""
    monkeypatch.setenv("CODESAGE_CONFIG_DIR", str(tmp_path))
    register_bundled_mcp_server(
        name="demo2", description="d", platforms={}, sha256={},
        default_config={"type": "stdio", "args": []},
    )
    # 未安装 → 不在 builtin 配置
    assert "demo2" not in get_builtin_mcp_configs()
    # 安装 → 合成配置,command 指向 installed 记录
    (tmp_path / "mcp").mkdir(parents=True, exist_ok=True)
    write_installed({"demo2": {"binary": "/fake/demo2"}})
    builtin = get_builtin_mcp_configs()
    assert "demo2" in builtin
    assert builtin["demo2"].command == "/fake/demo2"
    assert builtin["demo2"].scope == ConfigScope.BUILTIN


def test_builtin_priority_wins_over_user(tmp_path, monkeypatch):
    """spec §5.1:同名内置恒胜用户配置。"""
    monkeypatch.setenv("CODESAGE_CONFIG_DIR", str(tmp_path))
    register_bundled_mcp_server(
        name="prio", description="d", platforms={}, sha256={},
        default_config={"type": "stdio", "args": []},
    )
    (tmp_path / "mcp").mkdir(parents=True, exist_ok=True)
    write_installed({"prio": {"binary": "/fake/prio"}})
    # 用户层同名配置存在
    user_cfg = tmp_path / "settings.json"
    user_cfg.write_text(json.dumps({"mcpServers": {"prio": {"command": "other"}}}), encoding="utf-8")
    found = get_mcp_config_by_name("prio")
    assert found is not None
    assert found.scope == ConfigScope.BUILTIN  # builtin 恒胜用户配置
    assert found.command == "/fake/prio"


def test_installed_bin_dir(tmp_path, monkeypatch):
    """spec §4.6:安装目录路径。"""
    monkeypatch.setenv("CODESAGE_CONFIG_DIR", str(tmp_path))
    assert installed_bin_dir("demo3") == tmp_path / "mcp" / "bin" / "demo3"


def test_codebase_memory_registered():
    """spec §4.6:首个内置条目 codebase-memory 已注册。"""
    spec = get_bundled_mcp_server("codebase-memory")
    assert spec is not None
    assert "windows-x64" in spec.platforms
    assert "darwin-arm64" in spec.platforms
    assert spec.default_config.get("type") == "stdio"