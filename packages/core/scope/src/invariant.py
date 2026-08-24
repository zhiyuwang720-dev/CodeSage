"""作用域派发的检查岗(可选插件)。

派发拦截器:如果事件名是「作用域筛选事件」(见 scoped_events_generated),
强制要求派发的 thisArg 是作用域载体,且载体键与事件 payload 里的主题
一致。防止两类静默错误:
- 忘挂路由牌:作用域事件直接派发,没经 scope_target 造载体;
- 挂错牌:载体键与 payload 里的 agent/scope 不是同一个对象。

"""

from __future__ import annotations

from typing import Any

from .index import carrier_key_of, is_scope_carrier
from .scoped_events_generated import _NOT_SCOPED, scoped_subject_resolver_for

#: 检查岗插件名与依赖(TS 同款:companion plugin 挂在 invariants 服务下)。
PACKAGE_NAME = "@deepseek-ai/dsh-scope"
name = "scope-invariant"
inject = ["invariants"]


def install(ctx, fail) -> None:
    """安装到子注册 fiber:拦截所有派发,对作用域事件做载体检查。

    fail 是 invariants 服务提供的失败上报(带调用栈归因)。
    """

    def listener(mode, event_name, args, this_arg) -> None:
        subject_of = scoped_subject_resolver_for(event_name)
        if subject_of is _NOT_SCOPED:
            return
        if not is_scope_carrier(this_arg):
            fail(
                f'"{event_name}" is a scope-filtered event but was dispatched '
                "without a scope carrier — "
                "pass scope_target(base, subject) as the dispatch thisArg "
                "(agent events: use agentEvents(ctx, agent))"
            )
        if subject_of is not None and carrier_key_of(this_arg) != subject_of(args):
            fail(
                f'"{event_name}" was dispatched with a scope carrier keyed to a '
                "DIFFERENT subject than its arguments name — "
                "the carrier key and the event's subject must be the same object "
                "(use agentEvents(ctx, agent))"
            )

    ctx.on("internal/dispatch", listener, {"global": True})


def apply(ctx) -> Any:
    """注册检查岗;setup 成功后返回安装注册的 disposer。"""
    return ctx.invariants.register(PACKAGE_NAME, install)
