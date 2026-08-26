"""demo agent:一条可追踪的最小内核链路(理解内核的调试入口)。

用法:
    python demo_agent.py            # 逐段打印追踪 + 完整事件日志
    python -m pytest tests/ -q      # 链路断言测试(见 tests/test_demo.py)

本文件不引入任何新机制 —— 它用真实内核服务(注册表/会话/工具契约)
加两个桩(模型流、系统提示装配)跑完一个完整回合,并在每个阶段
停下打印「你现在在哪」。对照源码顺序阅读:

    followup ──► inbox ──► _wake_driver ──► _kick
    (agent.py   (agent/src   (agent.py:410)  (agent.py:468)
     :347)       /inbox.py)

    _kick → _turn —— 连续回合直到队列空 (agent.py:522)
      ├─ turn/start 事件 (agent.py:534)
      ├─ _pre_step (agent.py:488)
      │   ├─ inbox.claim 认领输入 → agent/inbox/spliced 事件
      │   ├─ systemPrompt.assemble 装配提示
      │   ├─ 决策点 agent/pre-step(无监听者 → 默认放行)
      │   └─ runtime_context.project 投影运行时上下文
      ├─ step/start + user/message 事件 (agent.py:556/560)
      ├─ _step (agent.py:601)
      │   ├─ _build_request (agent.py:715) → request/header+request/context 事件
      │   ├─ llm.stream → assembler 逐块拼装 → assistant/chunk 事件
      │   ├─ assistant/message 事件
      │   ├─ 工具调用 → execute_tool_calls (tool_calls.py)
      │   │   └─ 空注册表 → ToolNotFoundError(UNKNOWN_TOOL) → tool/result 事件
      │   │      结果折回 next-step 收件箱 → 回合再开一步
      │   └─ 第二轮 step:模型看到错误结果,收尾
      └─ turn/end 事件 (agent.py:590)

一次完整工具回合 = 两次模型调用,事件日志 23 条 —— 与
core/agent-loop/tests/test_decision_log.py 的断言完全同构。
"""

import asyncio
import sys
from pathlib import Path

_PACKAGES = Path(__file__).resolve().parents[1]
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PACKAGES))
sys.path.insert(0, str(_ROOT / "cordis-py"))

from cordis import Context  # noqa: E402

from core.agent import AgentRegistry  # noqa: E402
from core.agent_loop import AgentLoop  # noqa: E402
from core.session import SessionStore  # noqa: E402
from core.tools.src.index import ToolRuntime  # noqa: E402
from llm.llm.src.types import StreamEvent, Usage  # noqa: E402


def trace(label: str, where: str) -> None:
    """打一个检查点:阶段名 + 源码位置(文件:函数/行)。"""
    print(f"  --> {label}")
    print(f"      {where}")


class ScriptedLLM:
    """模型流桩:按调用次序返回脚本段,超界复用最后一段。

    剧本即行为:第一次调用产一个工具调用,第二次调用(工具结果
    已折回上下文)收尾 —— 模型在这里是确定性输入,不依赖真实
    API,追踪完全可复现。
    """

    def __init__(self, scripts: list) -> None:
        self.scripts = scripts
        self.calls = []

    async def stream(self, request, *, model="main"):
        self.calls.append((request, model))
        index = min(len(self.calls) - 1, len(self.scripts) - 1)
        for event in self.scripts[index]:
            yield event


class FakeSystemPrompt:
    """系统提示桩:空分节/空工具/空变量 —— 装配机制真实,内容为空。"""

    def variable(self, name, provider):
        return lambda: None

    async def assemble(self, context=None):
        return {"sections": [], "contexts": [], "tools": [], "variables": {}}


def build_services():
    """服务面:真实内核服务 + 两个桩。

    返回 (ctx, llm_holder):llm_holder 用于换剧本(run_demo 里换)。
    """
    ctx = Context()
    AgentRegistry(ctx)   # agents —— 发起者/归属边界 (core/agent)
    SessionStore(ctx)    # sessions —— 事件日志会话 (core/session)
    ToolRuntime(ctx)     # tools —— 契约版执行:空注册表 → 每次调用 UNKNOWN_TOOL
    ctx.accessor("systemPrompt", {"get": lambda c, _: FakeSystemPrompt()})
    holder = {"llm": ScriptedLLM([])}
    ctx.accessor("llm", {"get": lambda c, _: holder["llm"]})
    return ctx, holder


def _summary(event: dict) -> str:
    """事件载荷摘要:一行内看到这个事件在说什么。"""
    data = event.get("data", {})
    kind = event["type"]
    if kind == "agent/inbox/spliced":
        return f"op={data.get('op')} target={data.get('target')}"
    if kind in ("turn/start", "step/start", "step/end"):
        return f"turn={data.get('turn')} step={data.get('step')}"
    if kind in ("user/message", "assistant/message"):
        msg = data.get("message", {})
        blocks = [b.get("type") for b in msg.get("content", [])]
        return f"role={msg.get('role')} blocks={blocks} source={msg.get('source')}"
    if kind == "assistant/chunk":
        return f"chunk={str(data.get('chunk'))[:60]}"
    if kind == "tool/call":
        return f"callId={data.get('callId')} name={data.get('name')}"
    if kind == "tool/result":
        err = data.get("error")
        return f"error={err}" if err else "meta 私有载荷"
    if kind == "request/header":
        return f"reason={data.get('reason')}"
    if kind == "request/context":
        return "context 快照"
    if kind == "turn/end":
        return f"reason={data.get('reason')}"
    return f"data={str(data)[:60]}"


def run_demo(verbose: bool = True):
    """跑一次完整工具回合,返回 (事件日志, 模型调用记录)。"""
    if verbose:
        print("demo agent —— 内核最小链路追踪")
        print("=" * 68)

    ctx, holder = build_services()

    # 剧本:调用 1 → 工具调用(读文件);调用 2 → 看到 UNKNOWN_TOOL 后收尾
    holder["llm"] = ScriptedLLM([
        [
            StreamEvent(type="text_delta", text="reading "),
            StreamEvent(type="tool_use_start", tool_use_id="call-1", tool_name="read_file"),
            StreamEvent(type="tool_use_delta", input_json_delta='{"path": "a.txt"}'),
            StreamEvent(type="usage", usage=Usage(input_tokens=30, output_tokens=10)),
            StreamEvent(type="done", stop_reason="end_turn"),
        ],
        [
            StreamEvent(type="text_delta", text="done"),
            StreamEvent(type="usage", usage=Usage(input_tokens=40, output_tokens=3)),
            StreamEvent(type="done", stop_reason="end_turn"),
        ],
    ])

    if verbose:
        trace("创建循环:AgentLoop(ctx, config) —— 内核边界类", "agent-loop/src/index.py AgentLoop.create")
    loop = AgentLoop(ctx, {"maxParallelToolCalls": 2})
    agent = loop.create("demo", {"provider": "fake", "model": "m1"})

    if verbose:
        trace("followup:输入进收件箱并唤醒驱动者", "agent-loop/src/agent.py followup/send")

    async def scenario():
        agent.followup({"role": "user", "content": [{"type": "text", "text": "hi"}]})
        if verbose:
            print("   (驱动者 _kick 异步启动;when_idle 等到它排干)")
        await agent.when_idle()

    asyncio.run(scenario())

    if verbose:
        print()
        print("回合结束。完整事件日志(决策轨迹即审计数据):")
        print("=" * 68)
        for e in agent.session.events:
            print(f"  {e['seq']:>2}  {e['type']:<22} {_summary(e)}")
        print("=" * 68)
        print(f"模型调用次数: {len(holder['llm'].calls)}(两次 = 工具结果折回生效)")
        reason = [e for e in agent.session.events if e["type"] == "turn/end"][0]["data"]["reason"]
        print(f"回合结局:     {reason}")
    return agent.session.events, holder["llm"].calls


if __name__ == "__main__":
    import sys as _sys
    import warnings

    # 现代终端(Windows Terminal / VS Code)默认 UTF-8:显式重编码,
    # 避免在 GBK 代码页的旧终端上因字符集崩溃(显示乱码但不中断)。
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # cordis fiber 在解释器退出时卸载,已无事件循环可调度 reload
    # 协程,产生无害的 "was never awaited" 噪音 —— 与内核链路无关,
    # pytest 环境下同样链路不出现,仅脚本进程退出时触发。
    warnings.filterwarnings(
        "ignore", message="coroutine .* was never awaited", category=RuntimeWarning
    )
    run_demo()
