"""L3 确定性集成:决策日志断言(内核验证体系)。

testing.md Phase 2 的「决策日志断言」:完整会话结束后,断言事件
日志的类型序列逐项一致 + 关键事件载荷抽查。这是事件溯源内核的
红利 —— 决策轨迹本身是可审计的数据,状态机每走一步都留痕,
重放与断言共用同一份日志,不存在测试之外的行为。

三个确定性场景各覆盖一类回合结局(完成 / 错误 / 中止):
- 工具回合:模型先产出工具调用,空注册表给 UNKNOWN_TOOL 失败,
  结果折回上下文,模型收尾 —— 一回合两次模型调用;
- 失败回合:流事件 error → 回合以结构化 error 结局;
- 取消回合:挂起流被取消 → 已产内容折成 interrupted 消息。

工具执行走真 ToolRuntime 契约版(空注册表 = 每次调用
UNKNOWN_TOOL):流程可运行、可断言 —— 契约先行意味着执行面
尚未实现时行为已经固定。
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
from core.tools.src.index import ToolRuntime  # noqa: E402
from llm.llm.src.types import StreamEvent, Usage  # noqa: E402


class ScriptedLLM:
    """流桩:按调用次序切换事件序列,超界复用最后一段。

    每个序列是一次模型调用的完整流;调用方(agent loop)按
    turn/step 次序消费,脚本的次序即模型行为的剧本。
    """

    def __init__(self, scripts) -> None:
        self.scripts = scripts
        self.calls = []

    async def stream(self, request, *, model="main"):
        self.calls.append((request, model))
        index = min(len(self.calls) - 1, len(self.scripts) - 1)
        for event in self.scripts[index]:
            yield event


class FakeSystemPrompt:
    """装配桩:空分节/空工具/空变量。"""

    def __init__(self) -> None:
        self.calls = []

    def variable(self, name, provider):
        return lambda: None

    async def assemble(self, context=None):
        self.calls.append(context)
        return {"sections": [], "contexts": [], "tools": [], "variables": {}}


@pytest.fixture
def services():
    """最小服务面:agents/sessions/tools 真服务,llm/systemPrompt 桩。"""
    ctx = Context()
    AgentRegistry(ctx)
    SessionStore(ctx)
    ToolRuntime(ctx)
    system_prompt = FakeSystemPrompt()
    ctx.accessor("systemPrompt", {"get": lambda c, _: system_prompt})
    holder = {"llm": ScriptedLLM([])}
    ctx.accessor("llm", {"get": lambda c, _: holder["llm"]})
    return {"ctx": ctx, "system_prompt": system_prompt, "llm_holder": holder}


def run_turn(services, script):
    """跑一个 followup 驱动的完整回合,返回 agent 与事件日志。"""
    services["llm_holder"]["llm"] = ScriptedLLM(script)
    loop = AgentLoop(services["ctx"], {"maxParallelToolCalls": 2})
    agent = loop.create("l3", {"provider": "fake", "model": "m1"})

    async def scenario():
        agent.followup({"role": "user", "content": [{"type": "text", "text": "hi"}]})
        await agent.when_idle()

    asyncio.run(scenario())
    return agent


def event_types(agent):
    return [e["type"] for e in agent.session.events]


def test_tool_turn_decision_log(services):
    """工具回合:完整事件序列逐项一致(决策日志断言)。"""
    agent = run_turn(services, [
        [  # 第一次调用:模型要求读文件
            StreamEvent(type="text_delta", text="reading "),
            StreamEvent(type="tool_use_start", tool_use_id="call-1", tool_name="read_file"),
            StreamEvent(type="tool_use_delta", input_json_delta='{"path": "a.txt"}'),
            StreamEvent(type="usage", usage=Usage(input_tokens=30, output_tokens=10)),
            StreamEvent(type="done", stop_reason="end_turn"),
        ],
        [  # 第二次调用:看到 UNKNOWN_TOOL 结果后收尾
            StreamEvent(type="text_delta", text="done"),
            StreamEvent(type="usage", usage=Usage(input_tokens=40, output_tokens=3)),
            StreamEvent(type="done", stop_reason="end_turn"),
        ],
    ])

    assert event_types(agent) == [
        "agent/inbox/spliced",   # followup 入队
        "turn/start",
        "agent/inbox/spliced",   # 回合认领输入
        "step/start",
        "user/message",
        "request/header",
        "request/context",
        "assistant/chunk",   # 文本增量
        "assistant/chunk",   # 工具调用开始
        "assistant/chunk",   # 工具调用参数增量
        "assistant/chunk",   # usage
        "assistant/chunk",   # finish
        "assistant/message",
        "tool/call",
        "tool/result",
        "step/end",
        "step/start",
        "assistant/chunk",   # 文本增量
        "assistant/chunk",   # usage
        "assistant/chunk",   # finish
        "assistant/message",
        "step/end",
        "turn/end",
    ]

    # 载荷抽查:输入入队、工具调用与结果成对、结果链到调用事件
    user_msg = next(e for e in agent.session.events if e["type"] == "user/message")
    assert user_msg["data"]["role"] == "user"

    tool_call = next(e for e in agent.session.events if e["type"] == "tool/call")
    assert tool_call["data"]["callId"] == "call-1"
    assert tool_call["data"]["name"] == "read_file"
    assert tool_call["data"]["arguments"] == '{"path": "a.txt"}'

    tool_result = next(e for e in agent.session.events if e["type"] == "tool/result")
    assert tool_result["sourceEventSeqs"] == [tool_call["seq"]]
    # 词表:tool 结果以 user 角色承载单一 tool-result 块(模型只认
    # user/assistant/toolResult 三种角色,结果块靠 toolCallId 关联)
    message = tool_result["data"]["message"]
    assert message["role"] == "user"
    assert message["source"]["kind"] == "tool"
    assert message["content"][0]["type"] == "tool-result"
    assert message["content"][0]["toolCallId"] == "call-1"
    assert message["content"][0]["isError"] is True
    # 空注册表契约语义:未知工具,结构化 name/code 保真
    assert tool_result["data"]["error"] == {
        "name": "ToolNotFoundError", "code": "UNKNOWN_TOOL",
    }
    assert message["content"][0]["content"][0]["text"] == (
        'ToolNotFoundError: unknown tool "read_file"'
    )

    turn_end = next(e for e in agent.session.events if e["type"] == "turn/end")
    assert turn_end["data"]["reason"] == {"kind": "completed"}

    # 两次模型调用:第一次产工具调用,第二次看到结果后收尾
    assert len(services["llm_holder"]["llm"].calls) == 2


def test_error_turn_decision_log(services):
    """失败回合:流 error → 回合以结构化 error 结局,序列可断言。"""
    agent = run_turn(services, [[StreamEvent(type="error", error="boom")]])

    assert event_types(agent) == [
        "agent/inbox/spliced",   # followup 入队
        "turn/start",
        "agent/inbox/spliced",   # 回合认领输入
        "step/start",
        "user/message",
        "request/header",
        "request/context",
        "assistant/chunk",   # finish(error)
        "step/end",
        "turn/end",
    ]
    turn_end = next(e for e in agent.session.events if e["type"] == "turn/end")
    # 故障注入的结局保真:message + 结构化 code,不压成 UNKNOWN
    assert turn_end["data"]["reason"]["kind"] == "error"
    assert turn_end["data"]["reason"]["error"]["message"] == "boom"
    assert turn_end["data"]["reason"]["error"]["code"] == "UNKNOWN"
    # 没有无来源的 assistant 消息:故障回合不留假内容
    assert not any(e["type"] == "assistant/message" for e in agent.session.events)


def test_cancel_turn_decision_log(services):
    """取消回合:interrupted 消息 + aborted 结局,序列结构精确。

    取消是检查点式的:信号只在流交出控制权的检查点生效,所以流
    必须持续产出(心跳)让检查点可达 —— 真实客户端流是网络 I/O,
    每帧到达即检查点;心跳桩模拟这一行为。已产内容折进
    interrupted 消息,回合以 aborted 结局收场。
    """

    class HeartbeatLLM:
        """产三个 delta 后以心跳维持流,让取消检查点可达。"""

        async def stream(self, request, *, model="main"):
            for _ in range(3):
                await asyncio.sleep(0.005)
                yield StreamEvent(type="text_delta", text="x")
            while True:
                await asyncio.sleep(0.005)
                yield StreamEvent(type="text_delta", text="x")

    services["llm_holder"]["llm"] = HeartbeatLLM()
    loop = AgentLoop(services["ctx"], {"maxParallelToolCalls": 2})
    agent = loop.create("l3", {"provider": "fake", "model": "m1"})

    async def scenario():
        agent.followup({"role": "user", "content": [{"type": "text", "text": "hi"}]})
        await asyncio.sleep(0.05)
        agent.cancel({"kind": "user-cancel"})
        await asyncio.wait_for(agent.when_idle(), timeout=2)

    asyncio.run(scenario())

    types = event_types(agent)
    head = [
        "agent/inbox/spliced",   # followup 入队
        "turn/start",
        "agent/inbox/spliced",   # 回合认领输入
        "step/start",
        "user/message",
        "request/header",
        "request/context",
    ]
    tail = [
        "assistant/message",   # 已产内容折进 interrupted 消息
        "step/end",
        "turn/end",
    ]
    # 头部与尾部逐项精确;中段全是 chunk(≥3 个真实 delta,
    # 心跳个数取决于取消时序,结构本身确定性)。
    assert types[:len(head)] == head
    assert types[-len(tail):] == tail
    middle = types[len(head):-len(tail)]
    assert len(middle) >= 3
    assert all(t == "assistant/chunk" for t in middle)

    assistant_msg = next(e for e in agent.session.events if e["type"] == "assistant/message")
    assert assistant_msg["data"]["interrupted"] is True
    turn_end = next(e for e in agent.session.events if e["type"] == "turn/end")
    assert turn_end["data"]["reason"] == {"kind": "aborted", "reason": {"kind": "user-cancel"}}
