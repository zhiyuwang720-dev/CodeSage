"""agent 作用域派发与提示装配辅助(参考实现 agent/dispatch.ts 实现)。

融合派发器 ``agent_events`` 把 agent 主题与作用域载体耦合在
一起:作用域键与载荷里的 ``agent`` 不可能分歧 —— 派发器注入
subject(spread 在前,结构上可接受的载荷即使带了 ``agent`` 字段
也覆盖不了注入值)。重复派发方(循环驱动者)在 agent 构造器里
构建一次并复用,热路径免分配。
"""

from __future__ import annotations

import asyncio
import inspect

from core.scope import scope_target

from .runtime_types import AGENT_SUBJECT_EVENTS

__all__ = [
    "AGENT_PAYLOAD_FIELDS",
    "AgentEventDispatch",
    "agent_carrier",
    "agent_events",
    "assemble_context_for",
    "emit_agent_event",
]

#: 各 agent 主题事件的必需载荷字段(agent 由派发器注入,不在此列)
AGENT_PAYLOAD_FIELDS: dict[str, tuple] = {
    "agent/created": (),
    "agent/disposed": (),
    "agent/status": ("status",),
    "agent/inbox/inserted": ("message",),
    "agent/inbox/claimed": ("message", "turn"),
    "agent/inbox/discarded": ("message",),
    "agent/session-start": ("source",),
    "agent/pre-step": ("messages", "turn", "step", "signal"),
    "agent/request": ("turn", "step", "signal"),
    "agent/request-error": ("turn", "step", "provider", "failure", "retryPolicy", "signal"),
    "agent/turn-stopping": ("turn", "signal"),
    "agent/error": ("turn", "step", "error"),
}


def _observe_rejection(awaitable, ctx, id: str, name: str) -> None:
    """观察异步监听者返回值的拒绝并记日志(与 core/session 同款)。

    同步派发边界无法回滚,拒绝只能被记录;无运行中事件循环时
    忽略 —— 同步上下文里本就无法调度它。
    """
    try:
        task = asyncio.ensure_future(awaitable)
    except RuntimeError:
        return
    task.add_done_callback(
        lambda t: t.exception()
        and ctx.logger.warn(f'agent "{id}": {name} listener rejected: {t.exception()}')
    )


class AgentEventDispatch:
    """agent_events 返回的融合派发器:每方法都以 agent 作用域载体
    为 thisArg、把 agent 本身注入载荷来派发命名事件。"""

    def __init__(self, ctx, agent, carrier) -> None:
        self._ctx = ctx
        self._agent = agent
        self._carrier = carrier

    def _fused(self, payload: dict) -> dict:
        """注入 subject:spread 在前,载荷自带 agent 字段覆盖不了。"""
        return {**payload, "agent": self._agent}

    def _validate(self, name: str, payload: dict) -> None:
        required = AGENT_PAYLOAD_FIELDS.get(name, ())
        for key in required:
            if key not in payload:
                raise TypeError(f'agent event "{name}" requires payload field "{key}"')

    def emit(self, name: str, payload: dict) -> None:
        """即发即忘通知(agent 作用域)。

        每个监听者独立调用:同步抛错与返回的 Promise 拒绝都被
        记日志并包含 —— 通知不能否决生命周期推进,也不能饿死
        更晚的观察者。
        """
        if name not in AGENT_SUBJECT_EVENTS:
            raise ValueError(f'unknown agent event "{name}"')
        self._validate(name, payload)
        args = [self._carrier, name, self._fused(payload)]
        callbacks = self._ctx.events.dispatch("emit", args)
        for callback in callbacks:
            try:
                returned = callback(*args)
                if inspect.isawaitable(returned):
                    _observe_rejection(returned, self._ctx, self._agent.id, name)
            except Exception as error:  # noqa: BLE001 -- 通知包含化,见 docstring
                self._ctx.logger.warn(f'agent event "{name}" listener threw: {error}')

    async def serial(self, name: str, payload: dict) -> None:
        """被等待的顺序派发(cordis serial):首 bail 值即返回。"""
        if name not in AGENT_SUBJECT_EVENTS:
            raise ValueError(f'unknown agent event "{name}"')
        self._validate(name, payload)
        return await self._ctx.serial(self._carrier, name, self._fused(payload))

    def waterfall(self, name: str, payload: dict, *rest):
        """环绕中间件派发(cordis waterfall)。

        声明的事件参数在载荷之后,rest 即事件载荷后的全部参数
        —— 最后一个元素是最内层 next(监听者链包装的默认)。
        """
        if name not in AGENT_SUBJECT_EVENTS:
            raise ValueError(f'unknown agent event "{name}"')
        self._validate(name, payload)
        return self._ctx.waterfall(self._carrier, name, self._fused(payload), *rest)


def agent_carrier(agent) -> object:
    """构建一个 agent subject 的融合作用域载体。

    载体是无状态路由对象。agent_events 接受现有载体,重复派发
    同一 agent 的调用方(循环驱动者)构建一次并在构造器里复用。
    """
    return scope_target(agent, agent)


def agent_events(ctx, agent, carrier=None) -> AgentEventDispatch:
    """构建把 agent subject 与其作用域载体耦合的派发器。"""
    if carrier is None:
        carrier = agent_carrier(agent)
    return AgentEventDispatch(ctx, agent, carrier)


def emit_agent_event(ctx, agent, name: str, payload: dict) -> None:
    """一次性发出一条包含化 agent 通知,不持有派发器。"""
    agent_events(ctx, agent).emit(name, payload)


def assemble_context_for(agent, signal=None) -> dict:
    """构建提示装配上下文:agent 与作用域同设,agent 作用域的提示
    与工具贡献不会被静默遗漏。"""
    if signal is None:
        return {"agent": agent, "scope": agent}
    return {"agent": agent, "scope": agent, "signal": signal}
