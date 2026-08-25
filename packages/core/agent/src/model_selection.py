"""agent 作用域模型选择:提示装配与请求路由的耦合(参考实现 agent/model-selection.ts 实现)。

运行时入口(agent-loop)持有一个可变选择:代理指到哪台模型,当
前回合就用哪台。把它耦合到两个扩展点:

- ``system-prompt/assemble``(waterfall):提示装配在委派前**快照**
  所选模型,并把 provider/model 对写进装配变量 —— 并发切换只
  作用于更晚的回合,不会把两块表面拆散;
- ``agent/request``(waterfall):请求配置采纳装配时快照的
  provider/model,并应用所选推理强度 —— 选择缺席时清掉任何
  继承的推理强度,恢复所选模型的提供者/默认行为。
"""

from __future__ import annotations

__all__ = ["ModelSelectionRef", "install_model_selection"]


class ModelSelectionRef:
    """可变选择 + 当前步骤捕获的值。

    - current:为进入提示装配的下一个步骤选定的模型;
    - assembled:当前步骤进入提示装配时捕获的选择。
    """

    def __init__(self) -> None:
        self.current: dict | None = None
        self.assembled: dict | None = None


def install_model_selection(agent_ctx, selection: ModelSelectionRef):
    """把可变选择耦合到 agent 作用域的两个 waterfall 扩展点。

    @param agent_ctx: 所选 agent 的作用域上下文。
    @param selection: 调用方入口拥有的可变选择。
    @returns 两个作用域 waterfall 监听者的合并 disposer。
    """

    async def _assemble(payload, next_):
        # waterfall 契约:next 委托给默认装配
        selected = selection.current
        assembled = await next_()
        selection.assembled = selected
        if selected is None:
            return assembled
        return {
            **assembled,
            "variables": {
                **(assembled.get("variables") or {}),
                "provider": selected.get("provider"),
                "model": selected.get("model"),
            },
        }

    async def _request(payload, next_):
        resolved = await next_()
        selected = selection.assembled
        if selected is None:
            return resolved
        # 剥掉继承的推理强度,再应用所选(缺席即清除,恢复默认)
        without = {k: v for k, v in resolved.items() if k != "reasoningEffort"}
        merged = {
            **without,
            "provider": selected.get("provider"),
            "model": selected.get("model"),
        }
        if selected.get("reasoningEffort") is not None:
            merged["reasoningEffort"] = selected["reasoningEffort"]
        return merged

    dispose_assembly = agent_ctx.on("system-prompt/assemble", _assemble)
    dispose_request = agent_ctx.on("agent/request", _request)

    def dispose():
        dispose_assembly()
        dispose_request()

    return dispose
