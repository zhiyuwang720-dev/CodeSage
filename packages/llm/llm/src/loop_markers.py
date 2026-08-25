"""代理循环请求标记:识别请求是否来自 agent 循环(重放锚点)。

LLM 客户端对所有请求一视同仁,但代理循环需要从客户端事件流里
认出哪些请求属于它 —— 这是「重放导航 / 断点续跑 / 取消传播」的
锚点。标记只是内存侧的事实,不入请求体:提供者不知道、也不该
知道 harness 的组织边界。

参考实现的 WeakSet 语义以对象生命周期为准(对象被回收即清);
Python 以请求身份为生命周期,set 随客户端实例一起销毁,不依赖
GC 时机,也绝不泄漏。

**不变量**:标记必须发生在请求发出之前。请求已入队后补标记,
行为未定义 —— 客户端拿到的是捕获时的上下文。
"""

from __future__ import annotations

__all__ = ["is_agent_loop_request", "mark_agent_loop_request"]


#: 已标记为代理循环请求的身份集合(对象身份 → 1)
_AGENT_LOOP_REQUESTS: set[int] = set()


def mark_agent_loop_request(request: dict) -> None:
    """把一个 LLM 请求标记为代理循环的请求。

    必须在请求发出之前调用。标记随客户端实例生命周期释放:
    关闭时清空,不依赖 GC 时机。
    """
    _AGENT_LOOP_REQUESTS.add(id(request))


def is_agent_loop_request(request: dict) -> bool:
    """请求是否被标记为代理循环的请求。"""
    return id(request) in _AGENT_LOOP_REQUESTS


def _clear_agent_loop_requests() -> None:
    """清空全部标记(客户端生命周期结束时调用)。"""
    _AGENT_LOOP_REQUESTS.clear()
