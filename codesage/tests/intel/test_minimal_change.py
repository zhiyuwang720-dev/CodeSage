"""引擎级影响面约束层测试(spec 20 §7:test_minimal_change.py)。

用 mock intel(不依赖真实 codebase-memory)验证:写操作拦截/读操作放行/影响集计算/
最小集建议/禁用开关。
"""

import pytest

from codesage.intel import WRITE_TOOLS, MinimalChangeGuard, minimal_change_guard
from codesage.intel.minimal_change import minimal_change_enabled


class FakeIntel:
    """假 CodeIntelligenceService:固定影响面返回。"""

    def __init__(self, callers=0):
        self._callers = callers
        self.available = True

    async def impact_of_change(self, symbol):
        return {"callers_total": self._callers} if self.available else None


class FakeTool:
    def __init__(self, name):
        self.name = name


class FakeItem:
    def __init__(self, name, input_):
        self.tool = FakeTool(name)
        self.input = input_


def test_write_tools_include_edit_write():
    """spec 20 §4:约束层只约束写操作。"""
    assert "Write" in WRITE_TOOLS
    assert "Edit" in WRITE_TOOLS


def test_minimal_change_enabled_by_default():
    """spec 20 §4.3:默认开启(可 CODESAGE_NO_MINIMAL_CHANGE 关闭)。"""
    assert minimal_change_enabled() is True


@pytest.mark.asyncio
async def test_read_tool_passes(monkeypatch):
    """spec 20 §4:读操作(Read/Glob/Grep)放行,不做改动引导。"""
    monkeypatch.delenv("CODESAGE_NO_MINIMAL_CHANGE", raising=False)
    guard = MinimalChangeGuard(FakeIntel(callers=5))
    for read_tool in ("Read", "Glob", "Grep", "Bash"):
        assert await guard.guard(read_tool, {"file_path": "x.py"}) is None


@pytest.mark.asyncio
async def test_write_tool_intercepted_with_suggestion(monkeypatch):
    """spec 20 §4:写操作拦截,给影响面建议(非硬拦,是引导)。"""
    monkeypatch.delenv("CODESAGE_NO_MINIMAL_CHANGE", raising=False)
    guard = MinimalChangeGuard(FakeIntel(callers=3))
    suggestion = await guard.guard("Edit", {"file_path": "src/foo.py"})
    assert suggestion is not None
    assert "minimal-change" in suggestion
    assert "3" in suggestion  # 影响 3 个调用者


@pytest.mark.asyncio
async def test_write_no_callers_yagni_hint(monkeypatch):
    """spec 20 §5.2:无入站调用 → YAGNI 提示。"""
    monkeypatch.delenv("CODESAGE_NO_MINIMAL_CHANGE", raising=False)
    guard = MinimalChangeGuard(FakeIntel(callers=0))
    suggestion = await guard.guard("Write", {"file_path": "new.py"})
    assert suggestion is not None
    assert "YAGNI" in suggestion


@pytest.mark.asyncio
async def test_write_no_intel_passes(monkeypatch):
    """spec 20 §3:无 codebase-memory(available=False)时放行,不阻塞。"""
    monkeypatch.delenv("CODESAGE_NO_MINIMAL_CHANGE", raising=False)
    intel = FakeIntel(callers=5)
    intel.available = False
    guard = MinimalChangeGuard(intel)
    assert await guard.guard("Edit", {"file_path": "x.py"}) is None


@pytest.mark.asyncio
async def test_write_disabled_by_env(monkeypatch):
    """spec 20 §4.3:CODESAGE_NO_MINIMAL_CHANGE=1 关闭硬拦(仍可给建议,此处全放行)。"""
    monkeypatch.setenv("CODESAGE_NO_MINIMAL_CHANGE", "1")
    guard = MinimalChangeGuard(FakeIntel(callers=5))
    assert await guard.guard("Edit", {"file_path": "x.py"}) is None


def test_write_tools_helper():
    """spec 20 §4:WRITE_TOOLS 集合正确。"""
    assert WRITE_TOOLS == frozenset({"Write", "Edit"})