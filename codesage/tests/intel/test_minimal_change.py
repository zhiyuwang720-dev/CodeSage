"""引擎级影响面约束层测试(spec 20 §7:test_minimal_change.py)。

用脚本化 FakeIntel(不依赖真实 codebase-memory)验证:写操作拦截/读操作放行/多场景
建议(ambiguous/not_found/0/1/N 调用者)/拦一次重试放行/异常 fail-open/禁用开关。
"""

from __future__ import annotations

import pytest

from codesage.intel import MINIMAL_CHANGE_BLOCKED, WRITE_TOOLS, MinimalChangeGuard
from codesage.intel.minimal_change import minimal_change_enabled


class FakeIntel:
    """脚本化 intel:按序消费 impact 响应;可注入异常/wait_ready 失败。"""

    def __init__(self, script: list[dict | None] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[str] = []
        self.discoverable = True
        self.wait_ok = True

    async def wait_ready(self, timeout_s: float = 10.0) -> bool:
        return self.wait_ok

    async def impact_of_change(self, target: str) -> dict | None:
        self.calls.append(target)
        return self.script.pop(0) if self.script else None


def _impact(status: str, **kw) -> dict:
    return {"status": status, **kw}


def test_write_tools_include_edit_write():
    """spec 20 §4:约束层只约束写操作。"""
    assert WRITE_TOOLS == frozenset({"Write", "Edit"})


def test_minimal_change_enabled_by_default():
    """spec 20 §4.3:默认开启(可 CODESAGE_NO_MINIMAL_CHANGE 关闭)。"""
    assert minimal_change_enabled() is True


async def test_read_tool_passes(monkeypatch):
    """spec 20 §4:读操作(Read/Glob/Grep/Bash)放行,不做改动引导。"""
    monkeypatch.delenv("CODESAGE_NO_MINIMAL_CHANGE", raising=False)
    guard = MinimalChangeGuard(FakeIntel())
    for read_tool in ("Read", "Glob", "Grep", "Bash"):
        assert await guard.guard(read_tool, {"file_path": "x.py"}) is None
    assert guard._intel.calls == []


async def test_write_without_target_passes():
    """无 target 输入 → 放行(约束层不猜目标)。"""
    guard = MinimalChangeGuard(FakeIntel())
    assert await guard.guard("Edit", {"content": "x"}) is None


async def test_write_disabled_by_env(monkeypatch):
    """spec 20 §4.3:CODESAGE_NO_MINIMAL_CHANGE=1 关闭硬拦,全放行。"""
    monkeypatch.setenv("CODESAGE_NO_MINIMAL_CHANGE", "1")
    guard = MinimalChangeGuard(FakeIntel(script=[_impact("ok", callers_total=5)]))
    assert await guard.guard("Edit", {"file_path": "x.py"}) is None


async def test_no_intel_passes():
    """无 intel 服务 → 放行,不阻塞。"""
    guard = MinimalChangeGuard(None)
    assert await guard.guard("Edit", {"file_path": "x.py"}) is None


async def test_not_discoverable_passes():
    """cbm 未安装(discoverable False)→ 放行,零查询。"""
    intel = FakeIntel()
    intel.discoverable = False
    guard = MinimalChangeGuard(intel)
    assert await guard.guard("Edit", {"file_path": "x.py"}) is None
    assert intel.calls == []


async def test_wait_ready_timeout_fail_open():
    """索引未就绪(wait_ready 超时)→ fail-open 放行。"""
    intel = FakeIntel(script=[_impact("ok", callers_total=5)])
    intel.wait_ok = False
    guard = MinimalChangeGuard(intel)
    assert await guard.guard("Edit", {"file_path": "x.py"}) is None
    assert intel.calls == []


async def test_impact_error_fail_open():
    """查询失败(status=error)→ 放行:失败不是 YAGNI 信号,不误导。"""
    guard = MinimalChangeGuard(FakeIntel(script=[_impact("error")]))
    assert await guard.guard("Edit", {"file_path": "x.py"}) is None


async def test_exception_fail_open():
    """impact_of_change 抛异常 → 放行,不抛。"""

    class BoomIntel(FakeIntel):
        async def impact_of_change(self, target):
            raise RuntimeError("boom")

    guard = MinimalChangeGuard(BoomIntel())
    assert await guard.guard("Edit", {"file_path": "x.py"}) is None


async def test_ambiguous_advice_with_candidates():
    """歧义:拦截,建议列候选(qualified_name + file_path)+ 重试放行说明。"""
    sugs = [{"qualified_name": "a.AgentLoop", "file_path": "codesage/a.py"},
            {"qualified_name": "b.AgentLoop", "file_path": "codesage/b.py"}]
    guard = MinimalChangeGuard(FakeIntel(script=[_impact("ambiguous", suggestions=sugs)]))
    blocked = await guard.guard("Edit", {"file_path": "x.py"})
    assert blocked is not None
    assert blocked.is_error is True
    assert blocked.metadata.get("error_code") == MINIMAL_CHANGE_BLOCKED
    assert "a.AgentLoop (codesage/a.py)" in blocked.content
    assert "b.AgentLoop (codesage/b.py)" in blocked.content
    assert "重试该操作将放行" in blocked.content
    assert "add when" in blocked.content  # ponytail 输出契约


async def test_zero_callers_yagni():
    """无入站调用者 → 阶梯 1 YAGNI 提示。"""
    guard = MinimalChangeGuard(FakeIntel(script=[_impact("ok", callers_total=0)]))
    blocked = await guard.guard("Write", {"file_path": "new.py"})
    assert blocked is not None
    assert "阶梯 1" in blocked.content
    assert "YAGNI" in blocked.content
    assert "无入站调用者" in blocked.content


async def test_one_caller_root_cause():
    """1 个调用者 → 阶梯 2 根因修复(改共享函数优先)。"""
    guard = MinimalChangeGuard(FakeIntel(script=[_impact("ok", callers_total=1)]))
    blocked = await guard.guard("Edit", {"file_path": "x.py"})
    assert blocked is not None
    assert "阶梯 2" in blocked.content
    assert "根因" in blocked.content
    assert "共享函数" in blocked.content


async def test_n_callers_shared_fn_and_top3():
    """N 个调用者 → 影响数 + 前 3 调用者名 + 共享函数建议。"""
    callers = ["pkg.a.f1", "pkg.b.f2", "pkg.c.f3", "pkg.d.f4"]
    guard = MinimalChangeGuard(FakeIntel(script=[_impact("ok", callers_total=4, callers=callers)]))
    blocked = await guard.guard("Edit", {"file_path": "x.py"})
    assert blocked is not None
    assert "影响 4 个调用者" in blocked.content
    assert "pkg.a.f1" in blocked.content and "pkg.d.f4" not in blocked.content  # 只列前 3
    assert "共享函数" in blocked.content


async def test_not_found_yagni_hint():
    """图谱未找到(新文件/未索引符号)→ YAGNI 提示。"""
    guard = MinimalChangeGuard(FakeIntel(script=[_impact("not_found")]))
    blocked = await guard.guard("Edit", {"file_path": "codesage/new_mod.py"})
    assert blocked is not None
    assert "未找到" in blocked.content
    assert "YAGNI" in blocked.content


async def test_retry_same_target_passes():
    """拦一次语义:同一目标第二次操作放行,且不重复查询。"""
    intel = FakeIntel(script=[_impact("ok", callers_total=5)] * 2)
    guard = MinimalChangeGuard(intel)
    first = await guard.guard("Edit", {"file_path": "src/foo.py"})
    second = await guard.guard("Edit", {"file_path": "src/foo.py"})
    assert first is not None
    assert second is None
    assert len(intel.calls) == 1


async def test_normalize_path_separator_case():
    """目标归一化:分隔符/大小写不同的同一路径视为同一目标。"""
    intel = FakeIntel(script=[_impact("ok", callers_total=5)])
    guard = MinimalChangeGuard(intel)
    assert await guard.guard("Edit", {"file_path": r"src\foo.py"}) is not None
    assert await guard.guard("Edit", {"file_path": "SRC/foo.py"}) is None  # 同一目标,放行


async def test_different_targets_each_blocked_once():
    """不同目标各自拦一次(拦截记忆按目标隔离)。"""
    intel = FakeIntel(script=[_impact("ok", callers_total=5)] * 4)
    guard = MinimalChangeGuard(intel)
    assert await guard.guard("Edit", {"file_path": "a.py"}) is not None
    assert await guard.guard("Edit", {"file_path": "b.py"}) is not None
    assert await guard.guard("Edit", {"file_path": "a.py"}) is None  # 已拦过,放行
    assert await guard.guard("Edit", {"file_path": "b.py"}) is None  # 已拦过,放行
    assert len(intel.calls) == 2  # 只查两次
