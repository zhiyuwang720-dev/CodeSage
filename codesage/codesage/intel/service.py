"""CodeIntelligenceService(spec 20 §3):封装 codebase-memory-mcp 做自动索引 + 影响面查询。

把 codebase-memory 从「可选 MCP 服务器」提升为「核心服务」:启动自动索引当前项目,
暴露架构/影响面/调用链/变更影响查询,供引擎约束层与 agent 上下文消费。

实现:subprocess 调 `cbm cli --json <tool> <json_args>`(参数为 JSON 对象,非 --flag 风格),
统一解包 MCP 包装输出(structuredContent 优先)。传输走 seam(``ToolCaller`` Protocol):
后续切 MCP 客户端调用(15)只换 Provider,服务逻辑零改动(spec 20 §0.2 seam 思想)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: 进程级已索引项目缓存(同 project_dir 只索引一次,消除重复 build_loop 的 daemon 开销)
_indexed_cache: dict[str, str] = {}

#: 查询/索引超时(秒)。索引大库慢,查询是引擎热路径(每次写操作),必须短。
QUERY_TIMEOUT_S = 15.0
INDEX_TIMEOUT_S = 300.0

#: 影响面结果缓存(会话级 LRU + TTL):模型对同一目标反复 Edit 时零查询。
_IMPACT_CACHE_SIZE = 128
_IMPACT_CACHE_TTL_S = 30.0

#: 错误限频日志:(tool, tier) 组合只 warn 一次,避免热路径刷屏。
_error_logged: set[tuple[str, str]] = set()

#: 超时重试 1 次的工具(索引不重试——大库重试只会更慢)。
_RETRY_TOOLS = frozenset({"trace_path", "search_graph", "list_projects"})


class CallerError(Exception):
    """cbm CLI 调用失败,带分级 tier:process|timeout|protocol|server_error。"""

    def __init__(self, tier: str, message: str) -> None:
        super().__init__(message)
        self.tier = tier


class ToolCaller(Protocol):
    """传输 seam(spec 20 §0.2):Service Provider 接口,测试注入 FakeCaller。"""

    async def call(self, tool: str, args: dict[str, Any], *, timeout_s: float) -> dict[str, Any]: ...


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


class _CliCaller:
    """subprocess 传输(cbm cli)。参数为 JSON 对象(实测签名 `cli [--json] <tool> <json_args>`)。"""

    def __init__(self, cbm_cli: str) -> None:
        self._cbm_cli = cbm_cli

    async def call(self, tool: str, args: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        return await asyncio.to_thread(self._run, tool, args, timeout_s)

    def _run(self, tool: str, args: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        cmd = [self._cbm_cli, "cli", "--json", tool, json.dumps(args)]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout_s,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as e:
            raise CallerError("timeout", f"{tool} timed out after {timeout_s}s") from e
        except (OSError, subprocess.SubprocessError) as e:
            raise CallerError("process", str(e)) from e
        stdout = result.stdout.strip()
        if not stdout:
            raise CallerError("protocol", f"{tool} returned empty output")
        try:
            return json.loads(stdout)  # 整体 JSON
        except json.JSONDecodeError:
            pass
        # 兼容:daemon 启动日志混入 stdout 时,取最后一行 JSON
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        raise CallerError("protocol", f"{tool} returned non-JSON output")


def parse_tool_result(raw: dict[str, Any]) -> dict[str, Any]:
    """统一解包 cbm CLI 输出(MCP 包装)。

    优先级:structuredContent(真结构化,数字已为 int)→ content[0].text(内嵌 JSON → 表格解析)
    → 原样透传(原生 JSON)。isError → CallerError("server_error")。
    """
    if raw.get("isError"):
        raise CallerError("server_error", str(raw.get("content", ""))[:200])
    if isinstance(raw.get("structuredContent"), dict):
        return raw["structuredContent"]
    content = raw.get("content")
    if isinstance(content, list) and content:
        text = content[0].get("text") if isinstance(content[0], dict) else None
        if isinstance(text, str) and text.strip():
            try:
                return json.loads(text.strip())  # content 内嵌 JSON(如 list_projects)
            except (json.JSONDecodeError, TypeError):
                return _parse_table_text(text)
    return raw


def _parse_table_text(text: str) -> dict[str, Any]:
    """尽力解析 `key: value` 文本行为 dict(get_architecture/detect_changes 等表格 fallback)。

    数字/布尔 coercion;括号注释从数字值剥离,字符串值原样保留。表格 section 摘要行
    (如 `clusters: 12 (cols: ...)`)只取数字;行内数据(缩进行)跳过 —— 结构化查询请走
    format=json(ponytail: 文本解析天花板,structuredContent 不可用时 best-effort,seam 留升级口)。
    """
    out: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("  ") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        out[key.strip()] = _coerce(val)
    return out


def _coerce(v: str) -> Any:
    v = v.strip()
    digits = v.split(" (", 1)[0].strip()  # 剥离括号注释后再判数字
    if digits.isdigit():
        return int(digits)
    if v in ("true", "false"):
        return v == "true"
    return v


def _flatten_groups(callers: Any) -> list[str]:
    """展平 trace_path structuredContent 的 callers 分组树为 qualified name 列表。

    分组树形状未文档化(实测 limit 内 groups 为空),防御式递归收集 rows/groups 的
    name 字段。ponytail: 解析失败返回空表,不致命(建议主要消费 callers_total)。
    """
    out: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            name = node.get("name") or node.get("qualified_name")
            if isinstance(name, str):
                out.append(name)
            for key in ("rows", "groups", "children"):
                _walk(node.get(key))
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    try:
        if isinstance(callers, dict):
            for key in ("rows", "groups"):
                _walk(callers.get(key))
    except Exception:  # noqa: BLE001
        return []
    return out


def _is_path_like(target: str) -> bool:
    return (
        "/" in target or "\\" in target
        or Path(target).suffix.lower() in {
            ".py", ".ts", ".js", ".tsx", ".jsx", ".go", ".rs", ".java",
            ".c", ".cpp", ".h", ".rb", ".kt", ".php", ".swift",
        }
    )


def _same_file(suggestion_path: str, target_path: str) -> bool:
    """suggestion 的 file_path(相对项目根,如 codesage/engine/loop.py)vs 写目标路径。"""
    if not suggestion_path:
        return False
    s = suggestion_path.replace("\\", "/")
    t = target_path.replace("\\", "/")
    return s == t or t.endswith(s) or s.endswith(t) or Path(s).name == Path(t).name


class CodeIntelligenceService:
    """代码智能引擎服务:自动索引 + 影响面查询(spec 20 §3)。"""

    def __init__(self, project_dir: Path, cbm_cli: str | None = None, caller: ToolCaller | None = None) -> None:
        self._project = project_dir
        self._cbm_cli = cbm_cli or discover_cbm_cli()
        self._caller: ToolCaller | None = (
            caller if caller is not None else (_CliCaller(self._cbm_cli) if self._cbm_cli else None)
        )
        self._project_key: str | None = None
        self._indexed = False
        self._index_lock = asyncio.Lock()
        self._bg_thread: threading.Thread | None = None
        self._impact_cache: OrderedDict[tuple[str, str], tuple[float, dict[str, Any]]] = OrderedDict()

    @property
    def discoverable(self) -> bool:
        """cbm 可执行存在(未索引也可查询链路)。"""
        return self._caller is not None

    @property
    def available(self) -> bool:
        """cbm 存在且已索引(可做影响面查询)。"""
        return self._caller is not None and self._indexed

    @property
    def project_key(self) -> str | None:
        return self._project_key

    async def _run_caller(
        self, tool: str, args: dict[str, Any], *, timeout_s: float = QUERY_TIMEOUT_S, retry: bool = False,
    ) -> dict[str, Any] | None:
        """调 caller + 统一解包;任何失败 → None(降级),错误按 (tool, tier) 限频日志。"""
        if self._caller is None:
            return None
        try:
            raw = await self._caller.call(tool, args, timeout_s=timeout_s)
            return parse_tool_result(raw)
        except CallerError as e:
            key = (tool, e.tier)
            if key not in _error_logged:
                _error_logged.add(key)
                logger.warning("codebase-memory %s failed (%s): %s", tool, e.tier, e)
            if retry and e.tier == "timeout":
                try:
                    raw = await self._caller.call(tool, args, timeout_s=timeout_s)
                    return parse_tool_result(raw)
                except CallerError as e2:
                    key2 = (tool, e2.tier)
                    if key2 not in _error_logged:
                        _error_logged.add(key2)
                        logger.warning("codebase-memory %s retry failed (%s): %s", tool, e2.tier, e2)
                    return None
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning("codebase-memory %s unexpected failure: %s", tool, e)
            return None

    async def ensure_indexed(self, timeout_s: float = INDEX_TIMEOUT_S) -> bool:
        """索引当前项目(幂等 + 并发锁 + 进程级缓存)。失败返回 False,不抛。"""
        if self._indexed:
            return True
        if self._caller is None:
            return False
        async with self._index_lock:
            if self._indexed:
                return True
            root = str(self._project.resolve()).replace("\\", "/")
            cached_key = _indexed_cache.get(root)
            if cached_key:
                self._project_key = cached_key
                self._indexed = True
                logger.info("code intelligence reused cached index: %s", self._project_key)
                return True
            projects = await self._run_caller("list_projects", {}, timeout_s=30.0, retry=True)
            if projects:
                for p in projects.get("projects", []):
                    if str(p.get("root_path", "")).replace("\\", "/") == root:
                        self._project_key = p["name"]
                        self._indexed = True
                        _indexed_cache[root] = p["name"]
                        logger.info("code intelligence already indexed: %s", self._project_key)
                        return True
            result = await self._run_caller(
                "index_repository", {"repo_path": str(self._project)}, timeout_s=timeout_s, retry=False,
            )
            if result and result.get("status") == "indexed":
                self._project_key = result.get("project")
                self._indexed = True
                _indexed_cache[root] = self._project_key
                logger.info("indexed %s: %s nodes / %s edges",
                            self._project_key, result.get("nodes"), result.get("edges"))
                return True
            logger.warning("code intelligence index failed: %s",
                           result.get("status") if result else "no result")
            return False

    def start_background_index(self, timeout_s: float = INDEX_TIMEOUT_S) -> None:
        """后台 daemon 线程索引,不阻塞启动(替代原同步 _asyncio.run 阻塞 300s)。"""
        if self._caller is None or self._indexed:
            return
        self._bg_thread = threading.Thread(
            target=asyncio.run, args=(self.ensure_indexed(timeout_s),), daemon=True
        )
        self._bg_thread.start()

    async def wait_ready(self, timeout_s: float = 10.0) -> bool:
        """等待后台索引完成(限时轮询,供约束层 fail-open:未就绪放行)。"""
        deadline = time.monotonic() + timeout_s
        while not self._indexed and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        return self._indexed

    async def get_architecture(self, aspects: str = "overview") -> dict[str, Any] | None:
        """库结构概要(语言/包/入口/热点),注入 agent 上下文(spec 20 §3)。"""
        if not self.available:
            return None
        return await self._run_caller("get_architecture", {
            "project": self._project_key,
            "aspects": [a.strip() for a in aspects.split(",")],
        })

    async def impact_of_change(self, symbol_or_file: str) -> dict[str, Any] | None:
        """影响面分析,归一化结果契约(guard 的分支依据):

        ``{"target", "kind": symbol|file, "status": ok|ambiguous|not_found|error,
        "callers_total", "callers": list[str] 前 20, "suggestions"}``
        """
        if not self.available:
            return None
        target = str(symbol_or_file)
        cache_key = (target, "inbound")
        hit = self._impact_cache.get(cache_key)
        if hit is not None and time.monotonic() - hit[0] < _IMPACT_CACHE_TTL_S:
            return hit[1]
        result = (
            await self._impact_for_file(target)
            if _is_path_like(target) else await self._impact_for_symbol(target)
        )
        if result is not None:
            self._impact_cache[cache_key] = (time.monotonic(), result)
            self._impact_cache.move_to_end(cache_key)
            while len(self._impact_cache) > _IMPACT_CACHE_SIZE:
                self._impact_cache.popitem(last=False)
        return result

    async def _impact_for_symbol(self, name: str) -> dict[str, Any]:
        raw = await self._run_caller("trace_path", {
            "project": self._project_key, "function_name": name,
            "direction": "inbound", "format": "json",
        })
        if raw is None:
            return {"target": name, "kind": "symbol", "status": "error"}
        if raw.get("status") == "ambiguous":
            # 短名歧义:现有代码误判为「无调用者」的坑 —— 透传候选,由模型/调用方消歧
            return {"target": name, "kind": "symbol", "status": "ambiguous",
                    "suggestions": raw.get("suggestions", [])}
        return {
            "target": name, "kind": "symbol", "status": "ok",
            "callers_total": int(raw.get("callers_total", 0) or 0),
            "callers": _flatten_groups(raw.get("callers"))[:20],
        }

    async def _impact_for_file(self, path: str) -> dict[str, Any]:
        """文件级影响:stem 查符号 → ambiguous 按 file_path 过滤命中 → 前 3 个 qualified 聚合。

        ponytail: 多符号文件只追首 3 个命中符号(≤4 次调用有界);search_graph JSON 形状
        确认后可升级为直接文件级查询。
        """
        base = Path(path).stem
        raw = await self._run_caller("trace_path", {
            "project": self._project_key, "function_name": base,
            "direction": "inbound", "format": "json",
        })
        if raw is None:
            return {"target": path, "kind": "file", "status": "error"}
        if raw.get("status") == "ambiguous":
            hits = [s for s in raw.get("suggestions", []) if _same_file(s.get("file_path", ""), path)]
            if not hits:
                return {"target": path, "kind": "file", "status": "not_found"}
            total, callers = 0, []
            for s in hits[:3]:
                r = await self._run_caller("trace_path", {
                    "project": self._project_key, "function_name": s.get("qualified_name"),
                    "direction": "inbound", "format": "json",
                })
                if r and r.get("status") != "ambiguous":
                    total += int(r.get("callers_total", 0) or 0)
                    callers.extend(_flatten_groups(r.get("callers")))
            return {"target": path, "kind": "file", "status": "ok",
                    "callers_total": total, "callers": list(dict.fromkeys(callers))[:20]}
        # 唯一命中(stem 恰好唯一)
        return {"target": path, "kind": "file", "status": "ok",
                "callers_total": int(raw.get("callers_total", 0) or 0),
                "callers": _flatten_groups(raw.get("callers"))[:20]}

    async def changed_symbols(self) -> dict[str, Any] | None:
        """detect_changes:未提交改动映射到受影响符号 + 风险分级(相对 main)。"""
        if not self.available:
            return None
        return await self._run_caller("detect_changes", {"project": self._project_key})

    async def trace(self, fn: str, direction: str = "inbound") -> dict[str, Any] | None:
        """调用链追踪(入站/出站)。"""
        if not self.available:
            return None
        return await self._run_caller("trace_path", {
            "project": self._project_key, "function_name": fn,
            "direction": direction, "format": "json",
        }, retry=True)

    async def search(self, pattern: str, label: str = "") -> dict[str, Any] | None:
        """结构化搜索(按名字/标签)。"""
        if not self.available:
            return None
        args: dict[str, Any] = {"project": self._project_key, "name_pattern": pattern}
        if label:
            args["label"] = label
        return await self._run_caller("search_graph", args, retry=True)
