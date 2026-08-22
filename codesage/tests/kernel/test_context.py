"""Context tests — cordis context.ts + reflect.ts handler 翻译验证。"""

import asyncio

import pytest

from codesage.kernel import Context
from codesage.kernel.utils import INTERCEPT, ISOLATE


def test_root_construction():
    ctx = Context()
    assert ctx.root is ctx
    assert ctx.baseUrl is None
    assert ctx.fiber.uid == 0
    assert ctx.registry is not None
    assert ctx.reflect is not None
    assert ctx.events is not None
    assert ctx.logger is not None
    assert repr(ctx) == "Context <root>"


def test_mixins_exposed():
    ctx = Context()
    for name in ("on", "once", "parallel", "emit", "serial", "bail", "waterfall"):
        assert callable(getattr(ctx, name)), name
    for name in ("get", "set", "provide", "accessor", "mixin"):
        assert callable(getattr(ctx, name)), name
    for name in ("plugin", "inject"):
        assert callable(getattr(ctx, name)), name
    assert callable(ctx.effect)


def test_contains_semantics():
    ctx = Context()
    assert "events" in ctx
    assert "fiber" in ctx
    assert "on" in ctx  # mixin 在 props 注册了访问器(TS has trap 同款)
    assert "unknown_service" not in ctx


def test_special_properties_pass_through():
    ctx = Context()
    with pytest.raises(AttributeError):
        ctx._private
    with pytest.raises(AttributeError):
        ctx.prototype
    with pytest.raises(AttributeError):
        getattr(ctx, "123")  # 数字串是 special property(无 __getitem__,用 getattr)
    # 数字/下划线属性可正常设置(TS Reflect.set 直通)
    ctx.root._meta = 1
    assert ctx._meta == 1


def test_get_missing_service():
    ctx = Context()
    assert ctx.get("nope") is None
    assert ctx.nope is None  # 根 ctx 非严格:缺失服务返回 None(TS Reflect.get 同款)
    # 插件 ctx 才抛错(TS: 仅 fiber.runtime 存在时)
    with pytest.raises(RuntimeError, match=r'cannot get property "nope" without inject'):
        asyncio.run(ctx.plugin(lambda ctx, config: ctx.nope).wait())


def test_child_set_requires_provide():
    ctx = Context()
    child = ctx.extend()
    child.x = 1  # 纯 extend() 子 ctx 无 runtime → 可写(TS Reflect.set 同款)
    assert child.x == 1
    assert "x" not in ctx  # 不写回父
    # 插件 ctx 未提供名才抛错
    with pytest.raises(RuntimeError, match=r'cannot set property "x" without provide'):
        asyncio.run(ctx.plugin(lambda ctx, config: setattr(ctx, "x", 1)).wait())
    ctx.x = 1  # 根 ctx 可写
    assert ctx.x == 1


def test_extend_does_not_mutate_parent():
    ctx = Context()
    child = ctx.extend({"fiber": ctx.fiber, "extra": 1})
    assert child.extra == 1
    assert "extra" not in ctx  # 根 ctx 的 __getattr__ 返回 None,须用 in(hasattr 恒 True)
    assert child.reflect is ctx.reflect  # 服务共享


def test_isolate_independent_scope():
    ctx = Context()
    label1 = object()
    label2 = object()
    child1 = ctx.isolate("database", label1)
    child2 = ctx.isolate("database", label2)
    assert getattr(child1, ISOLATE)["database"] is label1
    assert getattr(child2, ISOLATE)["database"] is label2
    # 父 ctx 不受影响
    assert "database" not in getattr(ctx, ISOLATE)
    # 默认 label 每次不同
    a = ctx.isolate("database")
    b = ctx.isolate("database")
    assert getattr(a, ISOLATE)["database"] is not getattr(b, ISOLATE)["database"]


def test_intercept_copy_semantics():
    ctx = Context()
    child = ctx.intercept("logger", {"level": 2})
    assert getattr(child, INTERCEPT)["logger"] == {"level": 2}
    assert "logger" not in getattr(ctx, INTERCEPT)


def test_accessor_get_set():
    ctx = Context()
    holder = {"v": 1}
    ctx.accessor("computed", {"get": lambda ctx, receiver=None: holder["v"],
                              "set": lambda ctx, value, receiver=None: holder.__setitem__("v", value)})
    assert ctx.computed == 1
    ctx.computed = 2
    assert holder["v"] == 2


def test_accessor_conflict_with_service():
    ctx = Context()

    def plugin(ctx, config):
        ctx.provide("dup")

    async def run():
        await ctx.plugin(plugin).wait()  # 插件在 loop 内装载(同步创建会挂起)
        with pytest.raises(RuntimeError, match=r'property "dup" is already declared'):
            ctx.accessor("dup", {"get": lambda ctx, receiver=None: None})

    asyncio.run(run())


def test_service_resolution_walks_fiber_chain():
    """子 ctx 上的服务解析沿 fiber 链向上找实现。"""

    def provider(ctx, config):
        ctx.provide("greeting", "hello")

    class consumer:
        inject = ["greeting"]

        def __init__(self, ctx, config):
            self.ctx = ctx

        def __cordis_init__(self):
            self.ctx.root.seen = self.ctx.greeting

    ctx = Context()

    async def run():
        # 插件须在 loop 内创建(_spawn 的 ensure_future 需要运行中的 loop)
        await ctx.plugin(provider).wait()
        await ctx.plugin(consumer).wait()
        assert ctx.seen == "hello"

    asyncio.run(run())


def test_inject_missing_stays_pending():
    """inject 声明了但从未提供的服务 → 插件保持 PENDING 不装载。"""

    class consumer:
        inject = ["greeting"]

        def __init__(self, ctx, config):
            self.ctx = ctx

        def __cordis_init__(self):
            self.ctx.root.seen = True

    ctx = Context()

    async def run():
        await ctx.plugin(consumer).wait()
        await asyncio.sleep(0.05)
        assert "seen" not in ctx  # hasattr 对根 ctx 恒 True(缺服务返回 None)

    asyncio.run(run())
