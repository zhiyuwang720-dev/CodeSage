"""EventsService — translation of vendor/cordis/src/events.ts.

事件总线(ctx.events,其方法经 mixin 暴露为 ctx.on/emit/...):
- 五派发:emit(同步,不等)/ parallel(并发,等全部)/ serial(顺序,首个
  bail 值即返回)/ bail(同步,bail)/ waterfall(next 链,可 veto)
- 监听者记录 {ctx, callback, prepend, global};随拥有 fiber 卸载自动移除
- dispatch 解析可选 thisArg(首个参数是对象/函数),经 filter 过滤
  (Service.__cordis_filter__ 默认按 isolate 标签)
- internal/listener bail 钩子 + internal/update 特例(DisposableList 管理)

与 TS 的差异:reflect.bind(listener) 的调用追踪 Proxy 省略(Python 无)。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from .fiber import FiberState
from .service import FILTER
from .utils import AggregateError, DisposableList, is_object

if TYPE_CHECKING:
    from .context import Context


def is_bailed(value: Any) -> bool:
    """bail 语义:非 null/false/undefined 即算 bail(TS isBailed)。"""
    return value is not None and value is not False


@dataclass(slots=True)
class Hook:
    """监听者记录(TS events.ts Hook)。"""

    ctx: "Context"
    callback: Callable
    prepend: bool = False
    global_: bool = False


class EventsService:
    def __init__(self, ctx: "Context") -> None:
        self.ctx = ctx
        self._hooks: dict[str, list[Hook]] = {}
        #: 当前派发的 thisArg(TS 里监听者经 callback.bind(thisArg) 获得
        #: `this`;Python 无绑定,桥接监听者从这里读当前 fiber)
        self._dispatch_this: Any = None

        # internal/update 特例:非 global 监听者登记到 fiber 专用列表,
        # 由 global+prepend 的桥接监听者按序调用(TS 同款机制)
        def internal_listener(name, listener, options) -> Any:
            if name == "internal/update" and not options.get("global"):
                hooks = self.ctx.fiber._hooks.setdefault("internal/update", DisposableList())
                # push 返回 disposer(truthy)→ on() 跳过常规注册(TS 同款)
                return hooks.push(listener)
            return None

        self.on("internal/listener", internal_listener)

        def update_bridge(config, no_save, next_fn) -> Any:
            # TS: 桥接监听者 bound 到派发的 thisArg(= 正在更新的 fiber),
            # 读它的 _hooks;退化为 root fiber 保底
            fiber = self._dispatch_this or self.ctx.fiber
            cbs = list(fiber._hooks.get("internal/update") or [])
            it = iter(cbs)

            def _next() -> Any:
                try:
                    cb = next(it)
                except StopIteration:
                    return next_fn()
                return cb(config, no_save, _next)

            return _next()

        self.on("internal/update", update_bridge, {"global": True, "prepend": True})

    # --- 派发 ---

    def dispatch(self, type_: str, args: list) -> list[Callable]:
        """解析 thisArg 与事件名,应用过滤,返回匹配回调列表。
        就地消费 args(TS shift 同款),调用方用消费后的剩余参数调回调。"""
        this_arg = args.pop(0) if args and is_object(args[0]) else None
        name: str = args.pop(0)
        self._dispatch_this = this_arg
        if not name.startswith("internal/"):
            self.emit("internal/dispatch", type_, name, args, this_arg)
        filter_fn = getattr(this_arg, FILTER, None) if this_arg is not None else None
        matched = []
        for hook in self._hooks.get(name, []):
            if hook.global_ or filter_fn is None or filter_fn(hook.ctx):
                matched.append(hook.callback)
        return matched

    async def parallel(self, *args: Any) -> None:
        """并发运行全部监听者,等全部;任一失败抛 AggregateError。
        (TS 内部以 'emit' 模式上报 internal/dispatch)"""
        args = list(args)
        cbs = self.dispatch("emit", args)
        results = await asyncio.gather(*(cb(*args) for cb in cbs), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise AggregateError(errors)

    def emit(self, *args: Any) -> None:
        """同步按注册序调用,忽略返回值。"""
        args = list(args)
        for cb in self.dispatch("emit", args):
            cb(*args)

    async def serial(self, *args: Any) -> Any:
        """顺序 await,首个 bail 值(非 null/false)即返回。"""
        args = list(args)
        for cb in self.dispatch("serial", args):
            result = await cb(*args)
            if is_bailed(result):
                return result
        return None

    def bail(self, *args: Any) -> Any:
        """同步顺序,首个 bail 值即返回。"""
        args = list(args)
        for cb in self.dispatch("bail", args):
            result = cb(*args)
            if is_bailed(result):
                return result
        return None

    def waterfall(self, *args: Any) -> Any:
        """中间件链:最后参数为最内层 next;不调 next 即 veto。"""
        args = list(args)
        cbs = self.dispatch("waterfall", args)
        inner = args.pop()
        it = iter(cbs)

        def next_() -> Any:
            try:
                cb = next(it)
            except StopIteration:
                return inner()
            return cb(*args)

        args.append(next_)
        return next_()

    # --- 注册 ---

    def register(self, label: str, hooks: list[Hook], callback: Callable, options: dict) -> Callable[[], None]:
        """以 fiber effect 存储监听者(随 fiber 卸载自动移除)。"""
        return self.ctx.fiber.effect(
            lambda: self._register_impl(hooks, callback, options), label
        )

    def _register_impl(self, hooks: list[Hook], callback: Callable, options: dict):
        hook = Hook(self.ctx, callback, options.get("prepend", False), options.get("global", False))
        hooks.insert(0, hook) if options.get("prepend") else hooks.append(hook)
        return lambda: self.unregister(hooks, callback)

    def unregister(self, hooks: list[Hook], callback: Callable) -> bool:
        for i, hook in enumerate(hooks):
            if hook.callback is callback:
                del hooks[i]
                return True
        return False

    def on(self, name: str, listener: Callable, options: bool | dict | None = None) -> Callable[[], bool]:
        """注册监听者(随 fiber 卸载移除);options 为 bool 时等价 {prepend}。"""
        if not isinstance(options, dict):
            options = {"prepend": bool(options)}
        self.ctx.fiber.assert_active()
        # 特殊事件:internal/listener bail 钩子可接管注册(TS: truthy 即接管)
        result = self.bail(self.ctx, "internal/listener", name, listener, options)
        if result:
            return result
        hooks = self._hooks.setdefault(name, [])
        self.register(f"ctx.on({name})", hooks, listener, options)

        def remove() -> bool:
            return self.unregister(hooks, listener)

        return remove

    def once(self, name: str, listener: Callable, options: bool | dict | None = None) -> Callable[[], bool]:
        """只调用一次的监听者(自销毁包装)。"""
        holder: dict[str, Callable] = {}

        def wrapped(*args: Any) -> Any:
            dispose = holder.get("dispose")
            if dispose is not None:
                dispose()
            return listener(*args)

        holder["dispose"] = self.on(name, wrapped, options)
        return holder["dispose"]
