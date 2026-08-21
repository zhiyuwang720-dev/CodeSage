"""CodeIntelligenceService(spec 20 §3):封装 codebase-memory-mcp 做自动索引 + 影响面查询。

把 codebase-memory 从「可选 MCP 服务器」提升为「核心服务」:启动自动索引当前项目,
暴露架构/影响面/调用链/变更影响查询,供引擎约束层与 agent 上下文消费。

实现:薄封装 subprocess 调 `cbm cli <tool> --project <key> ...`,解析 stdout JSON。
codebase-memory 是 MCP 服务器(15 已接入协议),本服务在其上做产品化(自动索引 + 产品化查询)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

#: 进程级已索引项目缓存(同 project_dir 只索引一次,消除重复 build_loop 的 daemon 开销)
_indexed_cache: dict[str, str] = {}


def discover_cbm_cli() -> str | None:
    """发现 codebase-memory-mcp 可执行路径。

    顺序:CODESAGE_CBM_CLI 显式覆盖 → PATH → 常见安装路径(Windows Program Files/
    LocalAppData,安装器默认目标)。
    """
    override = os.environ.get("CODESAGE_CBM_CLI")
    if override:
        return override
    from_path = shutil.which("codebase-memory-mcp")
    if from_path:
        return from_path
    # 安装器默认路径(安装后需重启终端才进 PATH,这里直接探测)
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "codebase-memory-mcp" / "codebase-memory-mcp.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "codebase-memory-mcp" / "codebase-memory-mcp.exe",
        Path.home() / ".local" / "bin" / "codebase-memory-mcp",
        Path.home() / "bin" / "codebase-memory-mcp",
    ]
    for cand in candidates:
        if cand.exists():
            return str(cand)
    return None


class CodeIntelligenceService:
    """代码智能引擎服务:自动索引 + 影响面查询(spec 20 §3)。"""

    def __init__(self, project_dir: Path, cbm_cli: str | None = None) -> None:
        self._project = project_dir
        self._cbm_cli = cbm_cli or discover_cbm_cli()
        self._project_key: str | None = None
        self._indexed = False
        self._last_error: str | None = None

    @property
    def available(self) -> bool:
        """codebase-memory 可用(可执行存在)。"""
        return bool(self._cbm_cli) and self._project_key is not None

    @property
    def project_key(self) -> str | None:
        return self._project_key

    def _run_cli(self, tool: str, args: list[str], timeout_s: float = 120.0) -> dict | None:
        """调 cbm cli <tool> [--project <key>] ...,解析 stdout JSON。失败返回 None(降级)。"""
        if not self._cbm_cli:
            return None
        cmd = [self._cbm_cli, "cli", tool]
        if self._project_key:
            cmd += ["--project", self._project_key]
        cmd += args
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout_s,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            # stdout 最后一行是 JSON 结果(前几行是 daemon 启动日志)
            for line in reversed(result.stdout.strip().splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    return json.loads(line)
            # 兼容非 JSON 输出(如 trace_path 的表格文本):原样返回文本
            if result.stdout.strip():
                return {"_text": result.stdout.strip()}
            return None
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, json.JSONDecodeError) as e:
            self._last_error = str(e)
            logger.warning("codebase-memory cli %s failed: %s", tool, e)
            return None

    async def ensure_indexed(self, timeout_s: float = 300.0) -> bool:
        """索引当前项目(幂等)。返回是否成功。失败降级不抛。

        进程级缓存:同 project_dir 只索引一次,后续直接复用 project_key。
        """
        if self._indexed:
            return True
        if not self._cbm_cli:
            logger.warning("codebase-memory-mcp not found; code intelligence disabled")
            return False
        # 进程级缓存命中
        root = str(self._project.resolve()).replace("\\", "/")
        cached_key = _indexed_cache.get(root)
        if cached_key:
            self._project_key = cached_key
            self._indexed = True
            logger.info("code intelligence reused cached index: %s", self._project_key)
            return True
        # 先查是否已索引(list_projects 含当前项目)
        projects = await asyncio.to_thread(self._run_cli, "list_projects", [], 30.0)
        if projects and projects.get("projects"):
            for p in projects["projects"]:
                if p.get("root_path", "").replace("\\", "/") == root:
                    self._project_key = p["name"]
                    self._indexed = True
                    _indexed_cache[root] = p["name"]
                    logger.info("code intelligence already indexed: %s", self._project_key)
                    return True
        # 未索引则 index_repository
        result = await asyncio.to_thread(
            self._run_cli, "index_repository", ["--repo-path", str(self._project)], timeout_s
        )
        if result and result.get("status") == "indexed":
            self._project_key = result.get("project")
            self._indexed = True
            _indexed_cache[root] = self._project_key
            logger.info("indexed %s: %s nodes / %s edges",
                        self._project_key, result.get("nodes"), result.get("edges"))
            return True
        self._last_error = result.get("status") if result else "index failed"
        logger.warning("code intelligence index failed: %s", self._last_error)
        return False

    async def get_architecture(self, aspects: str = "overview") -> dict | None:
        """库结构概要(语言/包/入口/热点),注入 agent 上下文(spec 20 §3)。

        codebase-memory 的 get_architecture 输出为 `key: value` 文本,解析为 dict。
        多个 aspect 需分开传(逗号分隔会被拒),故拆分。
        """
        if not self.available:
            return None
        args = []
        for a in aspects.split(","):
            args += ["--aspects", a.strip()]
        result = await asyncio.to_thread(self._run_cli, "get_architecture", args)
        if result is None:
            return None
        if "_text" in result:
            return self._parse_kv_text(result["_text"])
        return result

    @staticmethod
    def _parse_kv_text(text: str) -> dict:
        """解析 `key: value` 文本行为 dict(spec 20 §3 get_architecture/trace_path)。

        数字字符串转 int;true/false 转 bool;其余保留字符串。
        """
        out: dict = {}

        def _coerce(v: str):
            v = v.strip()
            if v.isdigit():
                return int(v)
            if v == "true":
                return True
            if v == "false":
                return False
            return v

        for line in text.splitlines():
            line = line.strip()
            if ":" in line and not line.startswith("  "):
                key, _, val = line.partition(":")
                out[key.strip()] = _coerce(val)
        return out

    async def impact_of_change(self, symbol_or_file: str) -> dict | None:
        """影响面分析:查目标符号/文件的调用链,返回影响集(spec 20 §3)。"""
        if not self.available:
            return None
        # 入站调用(谁调用它)→ 改动影响面;输出为 key:value 文本,解析为 dict
        inbound = await asyncio.to_thread(
            self._run_cli, "trace_path", ["--function-name", symbol_or_file, "--direction", "inbound"]
        )
        if inbound is None:
            return None
        if "_text" in inbound:
            return self._parse_kv_text(inbound["_text"])
        return inbound

    async def changed_symbols(self) -> dict | None:
        """detect_changes:未提交改动映射到受影响符号 + 风险分级(spec 20 §3)。"""
        if not self.available:
            return None
        return await asyncio.to_thread(self._run_cli, "detect_changes", [])

    async def trace(self, fn: str, direction: str = "inbound") -> dict | None:
        """调用链追踪(入站/出站),输出解析为 dict。"""
        if not self.available:
            return None
        result = await asyncio.to_thread(
            self._run_cli, "trace_path", ["--function-name", fn, "--direction", direction]
        )
        if result is None:
            return None
        if "_text" in result:
            return self._parse_kv_text(result["_text"])
        return result

    async def search(self, pattern: str, label: str = "") -> dict | None:
        """结构化搜索(按名字/标签),输出解析为 dict。"""
        if not self.available:
            return None
        args = ["--name-pattern", pattern]
        if label:
            args += ["--label", label]
        result = await asyncio.to_thread(self._run_cli, "search_graph", args)
        if result is None:
            return None
        if "_text" in result:
            return self._parse_kv_text(result["_text"])
        return result