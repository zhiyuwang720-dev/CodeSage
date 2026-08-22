"""RegistryService tests — cordis registry.ts 翻译验证。"""

import asyncio

import pytest

from codesage.kernel import Context, Fiber, resolve_inject


def test_plugin_shapes_function():
    seen = []

    def plugin(ctx, config):
        seen.append(config)

    ctx = Context()
    fiber = ctx.plugin(plugin, {"a": 1})
    asyncio.run(fiber.wait())
    assert seen == [{"a": 1}]


def test_plugin_shapes_class():
    class plugin:
        def __init__(self, ctx, config):
            ctx.root.seen = config["x"]

    ctx = Context()
    asyncio.run(ctx.plugin(plugin, {"x": 7}).wait())
    assert ctx.seen == 7


def test_plugin_shapes_apply_object():
    seen = []
    obj = {"name": "my", "apply": lambda ctx, config: seen.append(config)}
    ctx = Context()
    asyncio.run(ctx.plugin(obj, {"x": 2}).wait())
    assert seen == [{"x": 2}]


def test_plugin_invalid_shape():
    ctx = Context()
    with pytest.raises(TypeError, match="invalid plugin"):
        ctx.plugin(123)


def test_plugin_returns_awaitable_fiber():
    ctx = Context()
    fiber = ctx.plugin(lambda ctx, config: None)
    assert isinstance(fiber, Fiber)
    # await fiber 直接可用(TS PromiseLike)
    asyncio.run(fiber.wait())


def test_runtime_shared_by_callback():
    ctx = Context()

    def plugin(ctx, config):
        pass

    async def run():
        f1 = ctx.plugin(plugin)
        f2 = ctx.plugin(plugin)
        assert f1.runtime is f2.runtime
        assert len(f1.runtime.fibers) == 2
        assert ctx.registry.has(plugin)
        assert ctx.registry.size == 1
        await asyncio.gather(f1.wait(), f2.wait())

    asyncio.run(run())


def test_delete_disposes_fibers_and_runtime():
    disposed = []

    def plugin(ctx, config):
        def dispose():
            disposed.append(True)

        return dispose

    async def run():
        ctx = Context()
        await ctx.plugin(plugin).wait()
        runtime = ctx.registry.get(plugin)
        assert runtime is not None
        ctx.registry.delete(plugin)  # dispose 是 fire-and-forget(TS 同款)
        assert ctx.registry.get(plugin) is None
        await asyncio.sleep(0.02)
        assert disposed == [True]

    asyncio.run(run())


def test_inject_deps():
    seen = []

    def dep_plugin(ctx, config):
        ctx.provide("database", "db")
        return lambda: None

    async def run():
        ctx = Context()
        ctx.plugin(dep_plugin)
        # 回调签名 (ctx, config)(TS Plugin.Callback 同款)
        await ctx.inject(["database"], lambda ctx, config: seen.append(ctx.database)).wait()
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert seen == ["db"]


def test_resolve_inject_shapes():
    assert resolve_inject(["a", "b"]) == {"a": None, "b": None}
    assert resolve_inject({"a": {"x": 1}}) == {"a": {"x": 1}}
    # __proto__ 继承链:父级在前,子级覆盖
    assert resolve_inject({"__proto__": {"a": 1, "b": 2}, "b": 3}) == {"a": 1, "b": 3}
    assert resolve_inject(None) == {}


def test_counter_increments_per_fiber():
    ctx = Context()

    async def run():
        f1 = ctx.plugin(lambda ctx, config: None)
        f2 = ctx.plugin(lambda ctx, config: None)
        assert f2.uid == f1.uid + 1
        await asyncio.gather(f1.wait(), f2.wait())

    asyncio.run(run())
