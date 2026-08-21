"""CodeIntelligenceService 测试(spec 20 §7:test_service.py)。

用真实 codebase-memory-mcp(本机已装)做端到端验证;未安装时跳过。
覆盖:ensure_indexed 幂等/架构概要/影响面/调用链。
"""

import os
from pathlib import Path

import pytest

from codesage.intel import CodeIntelligenceService, discover_cbm_cli

#: 项目根(索引目标 = CodeSage 自身,已验证 3786 节点)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _has_cbm() -> bool:
    return discover_cbm_cli() is not None


@pytest.mark.skipif(not _has_cbm(), reason="codebase-memory-mcp 未安装")
@pytest.mark.asyncio
async def test_ensure_indexed():
    """spec 20 §3:自动索引当前项目,幂等。"""
    svc = CodeIntelligenceService(PROJECT_ROOT)
    ok = await svc.ensure_indexed()
    assert ok is True
    assert svc.available is True
    assert svc.project_key is not None
    # 幂等:再次调用不重复索引
    ok2 = await svc.ensure_indexed()
    assert ok2 is True


@pytest.mark.skipif(not _has_cbm(), reason="codebase-memory-mcp 未安装")
@pytest.mark.asyncio
async def test_get_architecture():
    """spec 20 §3:架构概要(语言/包)。"""
    svc = CodeIntelligenceService(PROJECT_ROOT)
    await svc.ensure_indexed()
    arch = await svc.get_architecture()
    assert arch is not None
    assert int(arch.get("total_nodes", 0)) > 0


@pytest.mark.skipif(not _has_cbm(), reason="codebase-memory-mcp 未安装")
@pytest.mark.asyncio
async def test_trace_agentloop():
    """spec 20 §3:调用链追踪(入站)。"""
    svc = CodeIntelligenceService(PROJECT_ROOT)
    await svc.ensure_indexed()
    trace = await svc.trace("AgentLoop", "inbound")
    assert trace is not None
    assert int(trace.get("callers_total", 0)) > 0


@pytest.mark.skipif(not _has_cbm(), reason="codebase-memory-mcp 未安装")
@pytest.mark.asyncio
async def test_search_agentloop():
    """spec 20 §3:结构化搜索。"""
    svc = CodeIntelligenceService(PROJECT_ROOT)
    await svc.ensure_indexed()
    found = await svc.search(".*AgentLoop.*", "Class")
    assert found is not None
    assert int(found.get("total", 0)) > 0


def test_available_false_without_cli():
    """spec 20 §3:无 codebase-memory 时降级(available=False,不抛)。"""
    svc = CodeIntelligenceService(PROJECT_ROOT, cbm_cli="/nonexistent/cbm")
    assert svc.available is False