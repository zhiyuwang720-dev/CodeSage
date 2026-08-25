"""AgentRegistry 测试:注册表生命周期、initiator 作用域、工厂委派。"""

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]  # 仓库根
_PACKAGES = Path(__file__).resolve().parents[3]  # packages/
sys.path.insert(0, str(_PACKAGES))
sys.path.insert(0, str(_ROOT / "cordis-py"))

from cordis import Context  # noqa: E402

from core.agent import (  # noqa: E402
    AgentRegistry,
    agent_events,
    emit_agent_event,
)
from core.session import Session  # noqa: E402


class FakeAgent:
    """最小 Agent 协议实现。"""

    def __init__(self, id_: str, ctx=None) -> None:
        self.id = id_
        self.session = Session.create(id_)
        self.status = "idle"
        self.ctx = ctx
        self.options = {}

    def __repr__(self) -> str:
        return f"<FakeAgent {self.id}>"


@pytest.fixture
def ctx():
    return Context()


@pytest.fixture
def registry(ctx):
    return AgentRegistry(ctx)


def test_lifecycle_created_disposed(ctx, registry):
    seen = []
    ctx.on("agent/created", lambda p: seen.append(("created", p["agent"].id)))
    ctx.on("agent/disposed", lambda p: seen.append(("disposed", p["agent"].id)))
    agent = FakeAgent("a1", ctx)
    dispose = registry.register(agent)
    assert registry.get("a1") is agent
    assert seen == [("created", "a1")]
    asyncio.run(dispose())
    assert registry.get("a1") is None
    assert seen == [("created", "a1"), ("disposed", "a1")]


def test_duplicate_register_rejected(ctx, registry):
    registry.register(FakeAgent("a1", ctx))
    with pytest.raises(RuntimeError, match="already registered"):
        registry.enter(FakeAgent("a1", ctx), None)


def test_enter_announce_detach_roundtrip(ctx, registry):
    agent = FakeAgent("a1", ctx)
    detach = registry.enter(agent, None)
    assert registry.get("a1") is agent
    # detach 幂等
    detach()
    detach()
    assert registry.get("a1") is None


def test_enter_id_mismatch_rejected(ctx, registry):
    agent = FakeAgent("a1", ctx)
    agent.session = Session.create("other")
    with pytest.raises(RuntimeError, match="does not match session id"):
        registry.enter(agent, None)


def test_announce_without_enter_rejected(ctx, registry):
    with pytest.raises(RuntimeError, match="not live in this registry"):
        registry.announce(FakeAgent("a1", ctx))


def test_double_announce_rejected(ctx, registry):
    agent = FakeAgent("a1", ctx)
    registry.enter(agent, None)
    registry.announce(agent)
    with pytest.raises(RuntimeError, match="already announced"):
        registry.announce(agent)


def test_created_listener_veto_rolls_back(ctx, registry):
    """同步抛错的 created 监听者否决发布,回滚附件(配对销毁)。"""
    seen = []

    def veto(payload):
        seen.append(payload["agent"].id)
        raise RuntimeError("veto")

    ctx.on("agent/created", veto)
    agent = FakeAgent("a1", ctx)
    with pytest.raises(RuntimeError, match="veto"):
        registry.register(agent)
    assert registry.get("a1") is None


def test_enter_during_announce_defers_detach(ctx, registry):
    """created 监听者里调用 detach:销毁边推迟到派发退栈。"""
    agent = FakeAgent("a1", ctx)
    detach = registry.enter(agent, None)
    seen = []

    def listener(payload):
        # 监听者持有高级 detach 能力,在派发中调用它
        detach()
        seen.append("listener")

    ctx.on("agent/created", listener)
    registry.announce(agent)
    assert seen == ["listener"]
    # 派发退栈后销毁边已执行
    assert registry.get("a1") is None


def test_status_invariant(ctx, registry):
    """no-op 状态转换是缺陷:全局监听抛错(经 emit 包含化为日志)。"""
    agent = FakeAgent("a1", ctx)
    registry.enter(agent, None)
    emit_agent_event(ctx, agent, "agent/status", {"status": "running"})
    # 重复状态:invariant 抛错,emit 包含化 —— 状态记录不更新
    emit_agent_event(ctx, agent, "agent/status", {"status": "running"})
    assert registry._invariant._last["a1"] == "running"


def test_initiator_sync_forms(ctx, registry):
    agent = FakeAgent("a1", ctx)
    assert registry.current_initiator() is None
    assert registry.without_initiator(lambda: registry.current_initiator()) is None

    def op():
        # 边界内可读与 require 都命中该 agent
        assert registry.current_initiator() is agent
        return registry.require_initiator()

    assert registry.with_initiator(agent, op) is agent
    # 同步返回后边界已结束(参考实现 语义一致)
    assert registry.current_initiator() is None


def test_initiator_async_inherits(ctx, registry):
    """协程里读 initiator:继承创建处的上下文。"""
    agent = FakeAgent("a1", ctx)

    async def op():
        await asyncio.sleep(0)
        return registry.current_initiator()

    async def main():
        # await 等价于 with_initiator 返回的调度 task
        return await registry.with_initiator(agent, op)

    assert asyncio.run(main()) is agent


def test_initiator_require_without_boundary(ctx, registry):
    with pytest.raises(RuntimeError, match="no initiating agent"):
        registry.require_initiator()


def test_initiator_exception_releases(ctx, registry):
    agent = FakeAgent("a1", ctx)

    def op():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        registry.with_initiator(agent, op)
    assert registry._active_initiator_runs == 0


def test_disposed_initiator_rejects_new_boundaries(ctx, registry):
    agent = FakeAgent("a1", ctx)
    registry.close_initiators()
    with pytest.raises(RuntimeError, match="initiator scope is disposed"):
        registry.with_initiator(agent, lambda: None)
    assert registry.current_initiator() is None


def test_set_factory_and_create_delegation(ctx, registry):
    calls = []

    class Factory:
        async def create_agent(self, owner_ctx, options):
            calls.append(("create", owner_ctx, options))
            return "handle"

        async def resume(self, owner_ctx, options):
            calls.append(("resume", owner_ctx, options))
            return "resumed"

    dispose_factory = registry.set_factory(Factory())
    assert asyncio.run(registry.create({"sessionId": "s1"})) == "handle"
    assert asyncio.run(registry.resume({"resumeSessionId": "s1"})) == "resumed"
    assert calls[0][0] == "create" and calls[0][1] is ctx
    assert calls[1][0] == "resume"
    # 重复注册拒绝
    with pytest.raises(RuntimeError, match="already registered"):
        registry.set_factory(Factory())
    asyncio.run(dispose_factory())  # fiber effect disposer 是协程
    with pytest.raises(RuntimeError, match="no agent factory registered"):
        asyncio.run(registry.create({"sessionId": "s2"}))


def test_create_without_factory(ctx, registry):
    with pytest.raises(RuntimeError, match="no agent factory registered"):
        asyncio.run(registry.create({"sessionId": "s1"}))


def test_owned_roots_and_list(ctx, registry):
    root = FakeAgent("root", ctx)
    child = FakeAgent("child", ctx)
    registry.enter(root, None)
    registry.enter(child, root)
    assert registry.roots() == [root]
    assert registry.list() == [root, child]
    assert registry.is_owned_by("child", root)
    assert not registry.is_owned_by("root", root)
    assert registry.get("absent") is None


def test_agent_dispatch_emits_injected(ctx, registry):
    """派发器注入 agent subject,载荷不能覆盖。"""
    agent = FakeAgent("a1", ctx)
    dispatcher = agent_events(ctx, agent)
    seen = []
    ctx.on("agent/inbox/inserted", lambda p: seen.append(p["agent"]))
    dispatcher.emit("agent/inbox/inserted", {"message": {"id": "m1"}})
    assert seen == [agent]


def test_dispatch_payload_validation(ctx, registry):
    agent = FakeAgent("a1", ctx)
    dispatcher = agent_events(ctx, agent)
    with pytest.raises(TypeError, match="requires payload field"):
        dispatcher.emit("agent/inbox/inserted", {})
    with pytest.raises(ValueError, match="unknown agent event"):
        dispatcher.emit("agent/nope", {})
