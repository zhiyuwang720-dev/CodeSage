"""CodeIntelligenceService 测试(spec 20 §7:test_service.py)。

双路径:
- **单元路径**:ScriptedCaller 注入,锁参数格式(JSON 对象)/structuredContent 解包/表格 fallback/
  ambiguous 透传/文件聚合/缓存/超时重试/降级——不依赖外部二进制,CI 无 cbm 也全绿。
- **真实路径**:codebase-memory-mcp 端到端(未安装 skipif),锁「短名 run → ambiguous」修正行为
  (旧实现误判为 0 调用者的坑)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codesage.intel import CodeIntelligenceService, discover_cbm_cli
from codesage.intel.service import CallerError, parse_tool_result

#: 项目根(真实路径:索引目标 = CodeSage 自身,已验证 3786 节点)
PROJECT_ROOT = Path(__file__).resolve().parents[2]

_ROOT = str(PROJECT_ROOT).replace("\\", "/")


class ScriptedCaller:
    """脚本化 fake:按序消费 (tool, raw) 响应;raw 可为 CallerError 模拟失败。"""

    def __init__(self) -> None:
        self.script: list[tuple[str, dict | CallerError]] = []
        self.calls: list[tuple[str, dict, float]] = []

    def enqueue(self, tool: str, raw: dict | CallerError) -> "ScriptedCaller":
        self.script.append((tool, raw))
        return self

    async def call(self, tool: str, args: dict, *, timeout_s: float) -> dict:
        self.calls.append((tool, args, timeout_s))
        if not self.script:
            raise AssertionError(f"unexpected call: {tool} {args}")
        exp_tool, raw = self.script.pop(0)
        assert exp_tool == tool, f"expected {exp_tool}, got {tool}"
        if isinstance(raw, CallerError):
            raise raw
        return raw


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    """隔离模块级缓存(进程级索引缓存/错误限频),测试间互不污染。"""
    from codesage.intel import service as svc_mod

    monkeypatch.setattr(svc_mod, "_indexed_cache", {})
    monkeypatch.setattr(svc_mod, "_error_logged", set())


def _new_service(caller: ScriptedCaller) -> CodeIntelligenceService:
    return CodeIntelligenceService(PROJECT_ROOT, caller=caller)


async def _ready_service(caller: ScriptedCaller) -> CodeIntelligenceService:
    """构造已索引可用服务(list_projects 命中 root_path,不触发 re-index)。"""
    caller.enqueue("list_projects", {"projects": [{"name": "P", "root_path": _ROOT}]})
    svc = _new_service(caller)
    assert await svc.ensure_indexed() is True
    return svc


def _wrapped(sc: dict) -> dict:
    """模拟 `--json` 输出的 MCP 包装(structuredContent 路径)。"""
    return {"structuredContent": sc, "isError": False, "content": []}


# ---------------------------------------------------------------- 参数格式

async def test_call_args_json_object_shape():
    """参数为 JSON 对象(实测 `cli [--json] <tool> <json_args>`),非 --flag 风格。"""
    caller = ScriptedCaller().enqueue("list_projects", {"projects": []})
    svc = _new_service(caller)
    await svc.ensure_indexed()
    tool, args, _ = caller.calls[0]
    assert tool == "list_projects"
    assert args == {}


async def test_trace_args_shape():
    """trace_path 带全键:project/function_name/direction/format=json。"""
    caller = ScriptedCaller().enqueue("list_projects", {"projects": [{"name": "P", "root_path": _ROOT}]})
    svc = _new_service(caller)
    await svc.ensure_indexed()
    caller.enqueue("trace_path", _wrapped({"callers_total": 0}))
    await svc.trace("AgentLoop", "inbound")
    tool, args, _ = caller.calls[-1]
    assert tool == "trace_path"
    assert args == {"project": "P", "function_name": "AgentLoop", "direction": "inbound", "format": "json"}


# ---------------------------------------------------------------- 统一解包

def test_parse_structured_content():
    """structuredContent 优先(真结构化,数字已是 int)。"""
    raw = {"structuredContent": {"callers_total": 7}, "content": [], "isError": False}
    assert parse_tool_result(raw) == {"callers_total": 7}


def test_parse_content_embedded_json():
    """content[0].text 内嵌 JSON(如 list_projects 包装)按 JSON 解析。"""
    raw = {"content": [{"text": '{"projects": [{"name": "P"}]}'}], "isError": False}
    assert parse_tool_result(raw) == {"projects": [{"name": "P"}]}


def test_parse_table_text_fallback():
    """文本表格 fallback:数字/布尔 coercion,括号注释剥离,缩进行跳过。"""
    raw = {"content": [{"text": "clusters: 12  (cols: a, b)\n  node0\ncallers: 7\ndisabled: false"}], "isError": False}
    assert parse_tool_result(raw) == {"clusters": 12, "callers": 7, "disabled": False}


def test_parse_native_json_passthrough():
    """原生 JSON(list_projects 无包装)原样透传。"""
    assert parse_tool_result({"projects": []}) == {"projects": []}


def test_parse_is_error_raises():
    """isError → CallerError("server_error"),供上层降级。"""
    raw = {"content": [{"text": "boom"}], "isError": True}
    with pytest.raises(CallerError) as exc:
        parse_tool_result(raw)
    assert exc.value.tier == "server_error"


# ---------------------------------------------------------------- 索引

async def test_ensure_indexed_existing_project():
    """已索引项目:list_projects 命中 root_path → 不再调 index_repository。"""
    caller = ScriptedCaller().enqueue("list_projects", {"projects": [{"name": "P", "root_path": _ROOT}]})
    svc = _new_service(caller)
    assert await svc.ensure_indexed() is True
    assert svc.project_key == "P"
    assert svc.available is True
    assert [t for t, _, _ in caller.calls] == ["list_projects"]


async def test_ensure_indexed_reindex_idempotent():
    """未索引:index_repository 建索引;第二次 ensure_indexed 短路,零新调用。"""
    caller = (ScriptedCaller()
              .enqueue("list_projects", {"projects": []})
              .enqueue("index_repository", {"status": "indexed", "project": "P", "nodes": 3, "edges": 5}))
    svc = _new_service(caller)
    assert await svc.ensure_indexed() is True
    assert svc.project_key == "P"
    assert await svc.ensure_indexed() is True
    assert [t for t, _, _ in caller.calls] == ["list_projects", "index_repository"]


async def test_ensure_indexed_failure_returns_false():
    """索引失败(server_error)→ False,不抛,可降级。"""
    caller = (ScriptedCaller()
              .enqueue("list_projects", {"projects": []})
              .enqueue("index_repository", CallerError("server_error", "boom")))
    svc = _new_service(caller)
    assert await svc.ensure_indexed() is False
    assert svc.available is False


async def test_background_index_wait_ready():
    """后台线程索引不阻塞启动;wait_ready 限时轮询后可用。"""
    caller = (ScriptedCaller()
              .enqueue("list_projects", {"projects": []})
              .enqueue("index_repository", {"status": "indexed", "project": "P"}))
    svc = _new_service(caller)
    svc.start_background_index()
    assert await svc.wait_ready(timeout_s=10) is True
    assert svc.available is True


# ---------------------------------------------------------------- 影响面

async def test_impact_symbol_ok():
    """符号命中:callers_total 为 int(structured 解包),callers 列表。"""
    caller = ScriptedCaller()
    svc = await _ready_service(caller)
    caller.enqueue("trace_path", _wrapped({"callers_total": 7, "callers": {"cols": ["name", "hop"], "groups": []}}))
    impact = await svc.impact_of_change("AgentLoop")
    assert impact is not None
    assert impact["status"] == "ok"
    assert impact["kind"] == "symbol"
    assert impact["callers_total"] == 7
    assert impact["callers"] == []


async def test_impact_symbol_ambiguous():
    """短名歧义:透传 suggestions,不误判为「无调用者」(旧实现的坑)。"""
    suggestions = [{"qualified_name": "a.AgentLoop", "file_path": "codesage/a.py"}]
    caller = ScriptedCaller()
    svc = await _ready_service(caller)
    caller.enqueue("trace_path", {"status": "ambiguous", "suggestions": suggestions})
    impact = await svc.impact_of_change("AgentLoop")
    assert impact is not None
    assert impact["status"] == "ambiguous"
    assert impact["suggestions"] == suggestions


async def test_impact_file_aggregation():
    """文件目标:stem 歧义 → 按 file_path 过滤命中 → 前 3 个 qualified 聚合(有界)。"""
    caller = ScriptedCaller()
    svc = await _ready_service(caller)
    caller.enqueue("trace_path", {"status": "ambiguous", "suggestions": [
        {"qualified_name": "codesage.engine.loop.AgentLoop", "file_path": "codesage/engine/loop.py"},
        {"qualified_name": "codesage.engine.loop.run", "file_path": "codesage/engine/loop.py"},
        {"qualified_name": "other.mod.run", "file_path": "other/mod.py"},
    ]})
    caller.enqueue("trace_path", _wrapped({"callers_total": 3, "callers": {"groups": []}}))
    caller.enqueue("trace_path", _wrapped({"callers_total": 5, "callers": {"groups": []}}))
    impact = await svc.impact_of_change("codesage/engine/loop.py")
    assert impact is not None
    assert impact["kind"] == "file"
    assert impact["status"] == "ok"
    assert impact["callers_total"] == 8  # 3 + 5(未命中文件不追踪)
    trace_calls = [a for t, a, _ in caller.calls if t == "trace_path"]
    assert len(trace_calls) == 3  # stem + 2 个命中 qualified(≤4 次有界)


async def test_impact_file_not_found():
    """文件目标:歧义但无 file_path 命中 → not_found(YAGNI 场景)。"""
    caller = ScriptedCaller()
    svc = await _ready_service(caller)
    caller.enqueue("trace_path", {"status": "ambiguous", "suggestions": [
        {"qualified_name": "other.mod.run", "file_path": "other/mod.py"},
    ]})
    impact = await svc.impact_of_change("codesage/new_file.py")
    assert impact is not None
    assert impact["status"] == "not_found"


async def test_impact_cache_within_ttl():
    """同一目标 TTL 内二次查询命中缓存,零新调用(模型反复 Edit 场景)。"""
    caller = ScriptedCaller()
    svc = await _ready_service(caller)
    caller.enqueue("trace_path", _wrapped({"callers_total": 2}))
    first = await svc.impact_of_change("AgentLoop")
    second = await svc.impact_of_change("AgentLoop")
    assert first == second
    trace_calls = [a for t, a, _ in caller.calls if t == "trace_path"]
    assert len(trace_calls) == 1


async def test_unavailable_returns_none():
    """未索引(available False)→ 查询返回 None,零调用。"""
    caller = ScriptedCaller()
    svc = _new_service(caller)
    assert await svc.impact_of_change("AgentLoop") is None
    assert await svc.get_architecture() is None
    assert caller.calls == []


# ---------------------------------------------------------------- 健壮性

async def test_timeout_retry_once():
    """查询超时重试 1 次(工具在重试集内),第二次成功返回。"""
    caller = ScriptedCaller()
    svc = await _ready_service(caller)
    caller.enqueue("trace_path", CallerError("timeout", "slow"))
    caller.enqueue("trace_path", _wrapped({"callers_total": 4}))
    trace = await svc.trace("AgentLoop", "inbound")
    assert trace is not None
    assert trace["callers_total"] == 4
    trace_calls = [a for t, a, _ in caller.calls if t == "trace_path"]
    assert len(trace_calls) == 2


async def test_retry_exhausted_returns_none():
    """重试仍超时 → None(降级),不抛。"""
    caller = ScriptedCaller()
    svc = await _ready_service(caller)
    caller.enqueue("trace_path", CallerError("timeout", "slow"))
    caller.enqueue("trace_path", CallerError("timeout", "slow"))
    assert await svc.trace("AgentLoop", "inbound") is None


async def test_impact_failure_returns_error_status():
    """影响面查询失败 → status=error,不抛(guard 据 status 分支)。"""
    caller = ScriptedCaller()
    svc = await _ready_service(caller)
    caller.enqueue("trace_path", CallerError("server_error", "boom"))
    impact = await svc.impact_of_change("AgentLoop")
    assert impact is not None
    assert impact["status"] == "error"


# ---------------------------------------------------------------- 真实 cbm(未安装 skip)

def _has_cbm() -> bool:
    return discover_cbm_cli() is not None


@pytest.mark.skipif(not _has_cbm(), reason="codebase-memory-mcp 未安装")
async def test_real_ensure_indexed():
    """spec 20 §3:自动索引当前项目,幂等。"""
    svc = CodeIntelligenceService(PROJECT_ROOT)
    assert await svc.ensure_indexed() is True
    assert svc.available is True
    assert svc.project_key is not None
    assert await svc.ensure_indexed() is True


@pytest.mark.skipif(not _has_cbm(), reason="codebase-memory-mcp 未安装")
async def test_real_trace_structured():
    """trace_path format=json → structuredContent 解包,callers_total 为 int。"""
    svc = CodeIntelligenceService(PROJECT_ROOT)
    assert await svc.ensure_indexed() is True
    trace = await svc.trace("AgentLoop", "inbound")
    assert trace is not None
    assert isinstance(trace.get("callers_total"), int)
    assert trace["callers_total"] > 0


@pytest.mark.skipif(not _has_cbm(), reason="codebase-memory-mcp 未安装")
async def test_real_short_name_ambiguous():
    """短名 run → status=ambiguous(锁修正行为:不再误判为 0 调用者)。"""
    svc = CodeIntelligenceService(PROJECT_ROOT)
    assert await svc.ensure_indexed() is True
    impact = await svc.impact_of_change("run")
    assert impact is not None
    assert impact["status"] == "ambiguous"
    assert len(impact.get("suggestions", [])) > 0


@pytest.mark.skipif(not _has_cbm(), reason="codebase-memory-mcp 未安装")
async def test_real_architecture():
    """spec 20 §3:架构概要。"""
    svc = CodeIntelligenceService(PROJECT_ROOT)
    assert await svc.ensure_indexed() is True
    arch = await svc.get_architecture()
    assert arch is not None
    assert int(arch.get("total_nodes", 0)) > 0
