"""Fiber tests — cordis fiber.ts 翻译验证:状态机/effect/epoch 重载。"""

import asyncio

import pytest

from codesage.kernel import Context, CordisError, FiberState


def _run(coro):
    return asyncio.run(coro)


def test_state_transitions_to_active():
    async def run():
        ctx = Context()
        fiber = ctx.plugin(lambda ctx, config: None)
        await fiber.wait()
        assert fiber.state is FiberState.ACTIVE

    _run(run())


def test_startup_error_fails_fiber():
    async def run():
        ctx = Context()
        fiber = ctx.plugin(lambda ctx, config: 1 / 0)
        with pytest.raises(ZeroDivisionError):
            await fiber.wait()
        assert fiber.state is FiberState.FAILED

    _run(run())


def test_effect_disposer_runs_on_unload():
    order = []

    def plugin(ctx, config):
        ctx.effect(lambda: order.append("e1"))
        ctx.effect(lambda: order.append("e2"))

    async def run():
        ctx = Context()
        fiber = ctx.plugin(plugin)
        await fiber.wait()
        await fiber.dispose()
        assert order == ["e1", "e2"]  # 按注册序执行

    _run(run())


def test_effect_iterable_shape_skips_none():
    """TS safeCollect:可迭代结果里的 None 项跳过,函数项收集。"""
    order = []

    def plugin(ctx, config):
        ctx.effect(lambda: [order.append("a"), lambda: order.append("ra")])
        ctx.effect(lambda: order.append("b"))

    async def run():
        ctx = Context()
        fiber = ctx.plugin(plugin)
        await fiber.wait()
        assert order == ["a", "b"]
        await fiber.dispose()
        assert order == ["a", "b", "ra"]

    _run(run())


def test_effect_invalid_shape():
    ctx = Context()
    with pytest.raises(TypeError, match="Invalid effect"):
        ctx.effect(lambda: 123)


def test_effect_iterable_invalid_item():
    ctx = Context()
    with pytest.raises(TypeError, match="Invalid effect"):
        ctx.effect(lambda: [lambda: None, 42])


def test_effect_on_disposed_fiber_raises():
    async def run():
        ctx = Context()
        fiber = ctx.plugin(lambda ctx, config: None)
        await fiber.dispose()
        with pytest.raises(CordisError) as e:
            fiber.ctx.effect(lambda: None)
        assert e.value.code == CordisError.INACTIVE_EFFECT  # 常量即完整消息

    _run(run())


def test_disposer_idempotent():
    runs = []

    def plugin(ctx, config):
        return lambda: runs.append(1)

    async def run():
        ctx = Context()
        fiber = ctx.plugin(plugin)
        await fiber.wait()
        await fiber.dispose()
        await fiber.dispose()  # 二次调用无效果
        assert len(runs) == 1

    _run(run())


def test_epoch_reload_on_provide():
    """依赖的服务延迟提供 → 立即装载;注销 → 退回 PENDING;再提供 → 重载。"""
    events = []

    class app:
        inject = ["database"]

        def __init__(self, ctx, config):
            self.ctx = ctx

        def __cordis_init__(self):
            events.append("loaded:" + self.ctx.database)

    async def run():
        ctx = Context()
        fiber = ctx.plugin(app)
        await asyncio.sleep(0.02)
        assert events == []

        disposer = ctx.provide("database", "db1")
        await fiber.wait()
        assert events == ["loaded:db1"]

        await disposer()
        await fiber.wait()
        assert fiber.state is FiberState.PENDING

        ctx.provide("database", "db2")
        await fiber.wait()
        assert events == ["loaded:db1", "loaded:db2"]

    _run(run())


def test_restart():
    runs = []

    def plugin(ctx, config):
        runs.append(config)

    async def run():
        ctx = Context()
        fiber = ctx.plugin(plugin, {"v": 1})
        await fiber.wait()
        await fiber.restart()
        assert runs == [{"v": 1}, {"v": 1}]

    _run(run())


def test_update_config_and_restart():
    seen = []

    def plugin(ctx, config):
        seen.append(config)

    async def run():
        ctx = Context()
        fiber = ctx.plugin(plugin, {"v": 1})
        await fiber.wait()
        await fiber.update({"v": 2})
        assert seen == [{"v": 1}, {"v": 2}]

    _run(run())


def test_update_veto_on_root_fiber():
    """internal/update 监听者注册在根 ctx → 挂在根 fiber 的 _hooks;
    根 fiber 更新时桥接监听者读根 fiber → 否决生效(TS 同款机制)。"""
    called = []

    ctx = Context()
    ctx.on("internal/update", lambda config, no_save, next: called.append(config))
    result = ctx.fiber.update({"v": 2})
    assert called == [{"v": 2}]
    assert result is None  # 否决 → 无 restart


def test_update_not_vetoed_by_root_listener():
    """插件 fiber 更新:桥接监听者读的是该 fiber 的 _hooks(空)→ 根 ctx
    的监听者不生效,更新照常重启(TS 同款)。"""
    seen = []

    def plugin(ctx, config):
        seen.append(config)

    async def run():
        ctx = Context()
        ctx.on("internal/update", lambda config, no_save, next: None)
        fiber = ctx.plugin(plugin, {"v": 1})
        await fiber.wait()
        await fiber.update({"v": 2})
        assert seen == [{"v": 1}, {"v": 2}]

    _run(run())


def test_internal_status_events():
    statuses = []

    def plugin(ctx, config):
        pass

    async def run():
        ctx = Context()
        ctx.on("internal/status", lambda fiber, old: statuses.append((old, fiber.state)))
        fiber = ctx.plugin(plugin)
        await fiber.wait()
        assert (FiberState.PENDING, FiberState.LOADING) in statuses
        assert (FiberState.LOADING, FiberState.ACTIVE) in statuses

    _run(run())


def test_get_effects_labels():
    def plugin(ctx, config):
        ctx.effect(lambda: None, "ctx.provide(a)")

    async def run():
        ctx = Context()
        fiber = ctx.plugin(plugin)
        await fiber.wait()
        labels = [e["label"] for e in fiber.getEffects()]
        assert "ctx.provide(a)" in labels

    _run(run())


def test_async_plugin_effect_disposed():
    order = []

    async def plugin(ctx, config):
        # effect 的 fn 是 EXECUTE:返回 disposer(lambda: lambda: ...)
        ctx.effect(lambda: lambda: order.append("e"))

    async def run():
        ctx = Context()
        fiber = ctx.plugin(plugin)
        await fiber.wait()
        assert order == []
        await fiber.dispose()
        assert order == ["e"]

    _run(run())
