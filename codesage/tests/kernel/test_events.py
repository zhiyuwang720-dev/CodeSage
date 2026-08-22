"""EventsService tests — cordis events.ts 翻译验证:五派发/过滤/生命周期。"""

import asyncio

import pytest

from codesage.kernel import Context, Service
from codesage.kernel.utils import AggregateError


def _run(coro):
    return asyncio.run(coro)


def test_emit_order_and_args():
    order = []

    ctx = Context()
    ctx.on("e", lambda x: order.append(("a", x)))
    ctx.on("e", lambda x: order.append(("b", x)))
    ctx.emit("e", 1)
    assert order == [("a", 1), ("b", 1)]


def test_emit_prepend():
    order = []

    ctx = Context()
    ctx.on("e", lambda: order.append("first"))
    ctx.on("e", lambda: order.append("second"), True)  # True → prepend
    ctx.emit("e")
    assert order == ["second", "first"]


def test_emit_with_this_arg():
    """首参为对象时作为 thisArg 消费,监听者只收剩余参数。"""
    seen = []

    class obj:
        pass

    target = obj()
    ctx = Context()
    ctx.on("e", lambda x: seen.append(x))
    ctx.emit(target, "e", 42)
    assert seen == [42]


def test_once():
    seen = []

    ctx = Context()
    ctx.once("e", lambda x: seen.append(x))
    ctx.emit("e", 1)
    ctx.emit("e", 2)
    assert seen == [1]


def test_on_disposer_removes():
    seen = []

    ctx = Context()
    remove = ctx.on("e", lambda: seen.append(1))
    ctx.emit("e")
    assert remove() is True
    ctx.emit("e")
    assert seen == [1]


def test_listener_persists_on_fiber_unload():
    """ctx.on 把监听者登记到根 fiber 的 effect(TS events.register 用
    this.ctx.fiber = root)→ 插件卸载不自动移除,仅显式 disposer 移除。"""
    seen = []

    def plugin(ctx, config):
        ctx.on("e", lambda: seen.append(1))

    async def run():
        ctx = Context()
        fiber = ctx.plugin(plugin)
        await fiber.wait()
        ctx.emit("e")
        assert seen == [1]
        await fiber.dispose()
        ctx.emit("e")
        assert seen == [1, 1]  # 根 fiber 常驻,监听者仍在

    _run(run())


def test_parallel_waits_all():
    order = []

    async def a(x):
        order.append(("a", x))

    async def b(x):
        order.append(("b", x))

    ctx = Context()
    ctx.on("e", a)
    ctx.on("e", b)

    async def run():
        await ctx.parallel("e", 1)
        assert order == [("a", 1), ("b", 1)]

    _run(run())


def test_parallel_aggregates_errors():
    async def boom():
        raise ValueError("x")

    ctx = Context()
    ctx.on("e", boom)

    async def run():
        with pytest.raises(AggregateError) as e:
            await ctx.parallel("e")
        assert isinstance(e.value.errors[0], ValueError)

    _run(run())


def test_serial_first_bail_wins():
    order = []

    async def a(x):
        order.append("a")
        return None

    async def b(x):
        order.append("b")
        return "bailed"

    async def c(x):
        order.append("c")

    ctx = Context()
    ctx.on("e", a)
    ctx.on("e", b)
    ctx.on("e", c)

    async def run():
        result = await ctx.serial("e", 1)
        assert result == "bailed"
        assert order == ["a", "b"]  # c 未运行

    _run(run())


def test_bail_stops():
    order = []

    ctx = Context()
    ctx.on("e", lambda: (order.append("a"), None)[1])
    ctx.on("e", lambda: (order.append("b"), 7)[1])
    ctx.on("e", lambda: order.append("c"))
    result = ctx.bail("e")
    assert result == 7
    assert order == ["a", "b"]


def test_waterfall_chain_and_inner():
    order = []

    def m1(next):
        order.append("m1")
        next()

    def m2(next):
        order.append("m2")
        next()

    ctx = Context()
    ctx.on("w", m1)
    ctx.on("w", m2)
    ctx.waterfall("w", lambda: order.append("inner"))
    assert order == ["m1", "m2", "inner"]


def test_waterfall_veto():
    order = []

    def veto(next):
        order.append("veto")
        # 不调 next → 否决链尾

    def never(next):
        order.append("never")

    ctx = Context()
    ctx.on("w", veto)
    ctx.on("w", never)
    result = ctx.waterfall("w", lambda: order.append("inner"))
    assert order == ["veto"]
    assert result is None


def test_internal_dispatch_observable():
    """非 internal 事件派发前上报 internal/dispatch(消费后的参数)。"""
    seen = []

    ctx = Context()
    ctx.on("internal/dispatch", lambda mode, name, args, this_arg: seen.append((mode, name, list(args), this_arg)))
    ctx.emit("pub", 1, 2)
    assert seen == [("emit", "pub", [1, 2], None)]


def test_filter_excludes_hooks():
    """thisArg 携带 filter 时,不匹配 ctx 的监听者被排除。"""
    seen = []

    class svc(Service):
        provide = "svc"

        def __cordis_filter__(self, ctx):
            return False

    ctx = Context()
    s = svc(ctx)
    ctx.on("e", lambda x: seen.append(x))
    ctx.emit(s, "e", 1)
    assert seen == []  # filter 全拒
    ctx.emit(ctx, "e", 2)  # ctx 无 filter → 正常派发
    assert seen == [2]


def test_global_bypasses_filter():
    seen = []

    class svc(Service):
        provide = "svc"

        def __cordis_filter__(self, ctx):
            return False

    ctx = Context()
    s = svc(ctx)
    ctx.on("e", lambda x: seen.append(x), {"global": True})
    ctx.emit(s, "e", 1)
    assert seen == [1]


def test_internal_events_not_dispatched_to_public():
    """internal/* 事件不触发 internal/dispatch 上报。"""
    seen = []

    ctx = Context()
    ctx.on("internal/dispatch", lambda *args: seen.append(args))
    ctx.emit("internal/foo", 1)
    assert seen == []


def test_filter_default_passes_same_scope():
    """Service 默认 filter:hook.ctx 与服务的 isolate 标签一致 → 放行。"""
    seen = []

    class svc(Service):
        provide = "svc"

    ctx = Context()
    s = svc(ctx)
    ctx.on("e", lambda x: seen.append(x))
    ctx.emit(s, "e", 1)
    assert seen == [1]
