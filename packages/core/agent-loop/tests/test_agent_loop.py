"""AgentLoop 服务测试:创建/恢复、回合驱动、取消、配置启动、拆解。

测试沿 core/agent/tests/test_registry.py 的 sys.path 模式;llm 与
systemPrompt 服务经 ctx.accessor 注册桩 —— 装配桩产空分节,流桩
按用例给定的事件序列产出 unified 事件。
"""

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]  # 仓库根
_PACKAGES = Path(__file__).resolve().parents[3]  # packages/
sys.path.insert(0, str(_PACKAGES))
sys.path.insert(0, str(_ROOT / "cordis-py"))

from cordis import Context  # noqa: E402

from core.agent import AgentRegistry  # noqa: E402
from core.agent_loop import AgentLoop  # noqa: E402
from core.session import SessionStore  # noqa: E402
from llm.llm.src.types import StreamEvent, Usage  # noqa: E402


class FakeSystemPrompt:
    """装配桩:空分节/空工具/空变量,记录装配调用上下文。"""

    def __init__(self) -> None:
        self.calls = []

    def variable(self, name, provider):
        return lambda: None

    async def assemble(self, context=None):
        self.calls.append(context)
        return {"sections": [], "contexts": [], "tools": [], "variables": {}}


class FakeLLM:
    """流桩:按事件序列产出 unified 事件,记录请求。"""

    def __init__(self, events) -> None:
        self.events = events
        self.calls = []

    async def stream(self, request, *, model="main"):
        self.calls.append((request, model))
        for event in self.events:
            yield event


@pytest.fixture
def ctx():
    return Context()


@pytest.fixture
def services(ctx):
    """装配最小服务面:agents/sessions 真服务,llm/systemPrompt 桩。"""
    AgentRegistry(ctx)
    SessionStore(ctx)
    system_prompt = FakeSystemPrompt()
    ctx.accessor("systemPrompt", {"get": lambda c, _: system_prompt})
    holder = {"llm": FakeLLM([])}
    ctx.accessor("llm", {"get": lambda c, _: holder["llm"]})
    return {"ctx": ctx, "system_prompt": system_prompt, "llm_holder": holder}


def make_loop(services, config=None):
    return AgentLoop(services["ctx"], config or {"maxParallelToolCalls": 2})


def test_create_publishes_agent(services):
    loop = make_loop(services)
    started = []
    services["ctx"].on("agent/session-start", lambda p: started.append(p))
    agent = loop.create("s1", {"provider": "fake", "model": "m1"})
    assert agent.id == "s1"
    assert agent.status == "idle"
    assert services["ctx"].agents.get("s1") is agent
    assert services["ctx"].sessions.get("s1") is agent.session
    assert started == [{"source": "startup", "agent": agent}]


def test_inject_without_wakeup_stays_idle(services):
    loop = make_loop(services)
    agent = loop.create("s2", {"provider": "fake", "model": "m1"})
    agent.inject({"role": "user", "content": [{"type": "text", "text": "hi"}]})
    assert agent.status == "idle"
    assert agent.inbox.has_pending


def test_followup_drives_turn_to_completion(services):
    fake = services["llm_holder"]["llm"]
    fake.events = [
        StreamEvent(type="text_delta", text="hello"),
        StreamEvent(type="usage", usage=Usage(input_tokens=9, output_tokens=5)),
        StreamEvent(type="done", stop_reason="end_turn"),
    ]
    loop = make_loop(services)
    agent = loop.create("s3", {"provider": "fake", "model": "m1"})

    async def scenario():
        # 驱动者要求在运行中事件循环内唤醒(与 cancel 测试同模式):
        # 无循环时 followup 的 _wake_driver 会拒绝启动。
        agent.followup({"role": "user", "content": [{"type": "text", "text": "hi"}]})
        assert agent.status == "running"
        await agent.when_idle()
        assert agent.status == "idle"

    asyncio.run(scenario())

    types = [e["type"] for e in agent.session.events]
    assert types.count("turn/start") == 1
    assert "user/message" in types
    assert "assistant/message" in types
    assert "request/header" in types
    turn_end = next(e for e in agent.session.events if e["type"] == "turn/end")
    assert turn_end["data"]["reason"]["kind"] == "completed"
    # provider/model 不进入交付形状(折进会话 header 事件),
    # 路由经 model 参数传达;消息走词表转换后为 LLMRequest
    llm_request, routed_model = fake.calls[0]
    assert routed_model == "fake:m1"
    assert llm_request.messages[0].role == "user"


def test_cancel_aborts_turn(services):
    class SlowLLM:
        async def stream(self, request, *, model="main"):
            for _ in range(200):
                await asyncio.sleep(0.005)
                yield StreamEvent(type="text_delta", text="x")

    services["llm_holder"]["llm"] = SlowLLM()
    loop = make_loop(services)
    agent = loop.create("s4", {"provider": "fake", "model": "m1"})

    async def scenario():
        agent.followup({"role": "user", "content": [{"type": "text", "text": "hi"}]})
        await asyncio.sleep(0.05)
        agent.cancel({"kind": "user-cancel"})
        await asyncio.wait_for(agent.when_idle(), timeout=2)

    asyncio.run(scenario())
    turn_end = next(e for e in agent.session.events if e["type"] == "turn/end")
    assert turn_end["data"]["reason"]["kind"] == "aborted"
    assert turn_end["data"]["reason"]["reason"] == {"kind": "user-cancel"}


def test_configured_agent_auto_start(services):
    make_loop(services, {"agents": [
        {"id": "cfg1", "provider": "fake", "model": "m1"},
    ]})
    ids = [a.id for a in services["ctx"].agents.list()]
    assert any(i.startswith("cfg1-session-") for i in ids)


def test_duplicate_configured_identity_rejected(services):
    with pytest.raises(RuntimeError, match="duplicate exact session identity"):
        make_loop(services, {"agents": [
            {"id": "a", "sessionId": "same"},
            {"id": "b", "sessionId": "same"},
        ]})


def test_lifecycle_dispose_tears_down(services):
    loop = make_loop(services)
    agent = loop.create("s5", {"provider": "fake", "model": "m1"})
    disposed = []
    services["ctx"].on("agent/disposed", lambda p: disposed.append(p["agent"].id))
    asyncio.run(loop._ownership.dispose())
    assert disposed == ["s5"]
    assert services["ctx"].agents.get("s5") is None
    assert services["ctx"].sessions.get("s5") is None
