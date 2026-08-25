"""包属 agent 生命周期不变式(参考实现 agent/invariant.ts 实现)。

参考实现 中这是独立的 cordis 配套插件(name='agent-invariant',
inject=['invariants']):监听 ``agent/status``,同一 agent 的
相邻状态重复即 fail。Python 实现把不变量服务内部化 —— 批次 2
尚无 invariants 服务,检查器由 AgentRegistry 在状态翻转时
直接调用,语义相同:状态转换必须真的翻转,no-op 转换是缺陷。
"""

from __future__ import annotations

__all__ = ["AgentStatusInvariant", "name", "inject"]

#: 配套插件名(参考实现 兼容标识;Python 实现为内部检查器)
name = "agent-invariant"
#: 参考实现 依赖 invariants 服务;内部化后无需注入(保留签名以对照)
inject = ["invariants"]  # pragma: no cover -- 见模块 docstring


class AgentStatusInvariant:
    """每 agent 状态单调检查:同状态重复转换即失败。

    记录每个 agent 的最近状态(以 id 为键;参考实现 用 WeakMap,Python
    用 id → 状态的普通 dict,条目随注册表条目一起回收)。重复
    状态抛出 ValueError —— 谁发射了重复转换,谁就该在发射点被
    抓住,而不是把错误状态散播给观察者。
    """

    def __init__(self) -> None:
        self._last: dict[str, str] = {}

    def record(self, agent_id: str, status: str) -> None:
        previous = self._last.get(agent_id)
        if previous == status:
            raise ValueError(f"agent/status repeated {status} (no-op transition)")
        self._last[agent_id] = status

    def forget(self, agent_id: str) -> None:
        """条目离开注册表时释放检查记录。"""
        self._last.pop(agent_id, None)
