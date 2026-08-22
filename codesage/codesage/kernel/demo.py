"""内核自检 demo(阶段 21 验收 §6.4):Fake LLM + 工具 + 最小 loop。

manifest 装载跑通「输入 → 模型 → 工具 → 结果」:
- llm 插件:提供可调用服务 ctx.llm(Fake,两轮应答:先工具请求,再最终答案)
- tools 插件:提供 ctx.tools(工具表,calc)
- loop 插件:inject [llm, tools] —— 依赖就绪才激活(拓扑序)

运行:python -m codesage.kernel.demo
"""

from __future__ import annotations

import asyncio

from . import Context
from .loader import Loader


def llm_plugin(ctx: Context, config) -> None:
    """Fake LLM:带记忆的两轮应答(先 tool 请求,收到结果后给最终答案)。"""
    calls = 0

    def llm(prompt: str) -> str:
        nonlocal calls
        calls += 1
        return "calc:2+2" if calls == 1 else "答案是 4"

    ctx.provide("llm", llm)


def tools_plugin(ctx: Context, config) -> None:
    tools = {"calc": lambda expr: str(eval(expr, {"__builtins__": {}}))}
    ctx.provide("tools", tools)


def loop_plugin(ctx: Context, config) -> None:
    """最小 agent loop:输入 → 模型 → 工具 → 结果。"""
    prompt = config["prompt"]
    reply = ctx.llm(prompt)
    print(f"[demo] 输入: {prompt}")
    print(f"[demo] 模型: {reply}")
    if reply.startswith("calc:"):
        expr = reply.removeprefix("calc:")
        result = ctx.tools["calc"](expr)
        print(f"[demo] 工具 calc({expr!r}) -> {result}")
        reply = ctx.llm(f"{expr} = {result}")
    print(f"[demo] 结果: {reply}")
    assert reply == "答案是 4"


def main() -> str:
    manifest = [
        {"id": "llm", "name": "llm_plugin"},
        {"id": "tools", "name": "tools_plugin"},
        {"id": "loop", "name": "loop_plugin",
         "config": {"prompt": "2+2=?"}, "inject": ["llm", "tools"]},
    ]
    plugins = {"llm_plugin": llm_plugin, "tools_plugin": tools_plugin,
               "loop_plugin": loop_plugin}

    async def run() -> str:
        ctx = Context()
        loader = Loader(ctx, manifest, plugins)
        loader.mount()
        fibers = list(loader._fibers.values())
        await asyncio.gather(*(f.wait() for f in fibers))
        return "loop:ok"

    return asyncio.run(run())


if __name__ == "__main__":
    main()
