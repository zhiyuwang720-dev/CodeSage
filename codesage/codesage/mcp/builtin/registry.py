"""内置托管服务器注册表(spec §4.6/§10.3):记录已知优质 MCP 服务器的安装信息。

「内置」≠「捆绑」——CodeSage 不把第三方二进制打进发行包。注册表只记录如何按需安装与启动,
用户显式运行 `codesage mcp install <name>` 下载+校验后即成为普通 stdio 服务器走既有管线。

首个内置条目 = codebase-memory-mcp(本地代码知识图谱引擎,158 语言 tree-sitter 索引,
15 个只读图查询工具)。源码:https://github.com/DeusData/codebase-memory-mcp
(设计与基准见 arXiv:2603.27277,官方发布含 SLSA3/VirusTotal 审计记录)。

该注册表也是 19 插件机制的种子(spec §15)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BundledMcpServer:
    """一个内置托管服务器的安装说明(spec §4.6)。"""

    name: str
    description: str
    platforms: dict[str, str]  # 平台键 -> 下载 URL 模板(安装时按平台展开)
    sha256: dict[str, str]  # 平台/文件名 -> 期望 SHA-256(下载后强制校验)
    default_config: dict  # 装好后写入 builtin 配置的默认段(command 指向安装路径)
    install_hint: str | None = None  # 非自动安装时的指引(如 apt/npm/pip 命令)

#: 内置条目注册表(进程内单例,镜像 14 register_bundled_skill)
_bundled: dict[str, BundledMcpServer] = {}


def register_bundled_mcp_server(
    name: str,
    description: str,
    platforms: dict[str, str],
    sha256: dict[str, str],
    default_config: dict,
    install_hint: str | None = None,
) -> None:
    """注册一个内置条目(重名覆盖)。字段即 BundledMcpServer 的字段,构造与注册合一。"""
    _bundled[name] = BundledMcpServer(
        name=name,
        description=description,
        platforms=platforms,
        sha256=sha256,
        default_config=default_config,
        install_hint=install_hint,
    )

def get_bundled_mcp_server(name: str) -> BundledMcpServer | None:
    return _bundled.get(name)

def iter_bundled_servers() -> list[BundledMcpServer]:
    return list(_bundled.values())


def register_codebase_memory() -> None:
    """注册首个内置条目(spec §4.6:codebase-memory-mcp)。

    URL 模板按平台展开;安装时校验 SHA-256 后再解压(config.py 的 install 流负责下载与校验)。
    """
    base = "https://github.com/DeusData/codebase-memory-mcp/releases/latest/download/codebase-memory-mcp-{platform}-{arch}.{ext}"
    register_bundled_mcp_server(
        name="codebase-memory",
        description=(
            "本地代码知识图谱引擎:158 种语言 tree-sitter 索引 + 15 个只读图查询工具"
            "(search_graph/trace_path/query_graph/get_code_snippet/detect_changes 等),"
            "100% 本地运行,无 LLM/无 API key/无遥测。"
        ),
        platforms={
            "windows-x64": base.format(platform="windows", arch="amd64", ext="zip"),
            "darwin-arm64": base.format(platform="darwin", arch="arm64", ext="tar.gz"),
            "darwin-x64": base.format(platform="darwin", arch="amd64", ext="tar.gz"),
            "linux-arm64": base.format(platform="linux", arch="arm64", ext="tar.gz"),
            "linux-x64": base.format(platform="linux", arch="amd64", ext="tar.gz"),
        },
        sha256={},  # 安装时从官方 checksums.txt 拉取后写入 installed.json,注册表不硬编码哈希
        default_config={"type": "stdio", "args": [], "env": None},
        install_hint="codebase-memory-mcp",  # 亦可经 npm/pip/go install 安装,URL 模板为官方默认渠道
    )

register_codebase_memory()