"""Loader + Patch tests — specs/21 §6.2:行装载/disabled 跳过/按 id patch。"""

import asyncio

from codesage.kernel import Context
from codesage.kernel.loader import Loader, _interpolate


def _run(coro):
    return asyncio.run(coro)


async def _wait_all(loader):
    """在 loop 内等全部已装载 fiber(避免 loop 外建 gather future)。"""
    fibers = list(loader._fibers.values())
    if fibers:
        await asyncio.gather(*(f.wait() for f in fibers))


def test_mount_loads_enabled_rows():
    seen = []

    def plugin(ctx, config):
        seen.append(config)

    ctx = Context()
    loader = Loader(ctx, [
        {"id": "a", "name": "p", "config": {"v": 1}},
        {"id": "b", "name": "p", "config": {"v": 2}},
    ], plugins={"p": plugin})
    loader.mount()
    _run(_wait_all(loader))
    assert seen == [{"v": 1}, {"v": 2}]  # manifest 序


def test_mount_skips_disabled_rows():
    seen = []

    def plugin(ctx, config):
        seen.append(config["id"])

    ctx = Context()
    loader = Loader(ctx, [
        {"id": "a", "name": "p", "config": {"id": "a"}},
        {"id": "b", "name": "p", "config": {"id": "b"}, "disabled": True},
    ], plugins={"p": plugin})
    loader.mount()
    assert "b" not in loader._fibers
    _run(_wait_all(loader))
    assert seen == ["a"]


def test_inject_topology_activation_order():
    """行级 inject:依赖先激活,加载顺序 = 拓扑序。"""
    order = []

    def provider(ctx, config):
        ctx.provide("database", config["url"])
        order.append("provider")

    def consumer(ctx, config):
        order.append("consumer:" + ctx.database)

    ctx = Context()
    loader = Loader(ctx, [
        {"id": "c", "name": "consumer", "inject": ["database"]},
        {"id": "p", "name": "provider", "config": {"url": "db://1"}},
    ], plugins={"provider": provider, "consumer": consumer})
    loader.mount()
    _run(_wait_all(loader))
    assert order == ["provider", "consumer:db://1"]


def test_inject_cycle_stays_pending():
    """循环依赖:双方保持 PENDING 不激活(TS cordis 同款行为)。"""
    seen = []

    def a(ctx, config):
        seen.append("a")

    def b(ctx, config):
        seen.append("b")

    ctx = Context()

    async def run():
        loader = Loader(ctx, [
            {"id": "a", "name": "a", "inject": ["svc_b"]},
            {"id": "b", "name": "b", "inject": ["svc_a"]},
        ], plugins={"a": a, "b": b})
        loader.mount()  # loop 内装载:裸协程不悬挂
        await asyncio.sleep(0.02)
        assert seen == []
        assert all(f.state.value == "pending" for f in loader._fibers.values())

    _run(run())


def test_patch_replaces_config_last_wins():
    seen = []

    def plugin(ctx, config):
        seen.append(config)

    async def run():
        ctx = Context()
        loader = Loader(ctx, [{"id": "a", "name": "p", "config": {"v": 1}}],
                        plugins={"p": plugin})
        loader.mount()
        await loader._fibers["a"].wait()
        await loader.apply_patches([
            {"id": "a", "config": {"v": 2}},
            {"id": "a", "config": {"v": 3}},  # last-wins
        ])
        # 每次 patch 各触发一次重启(TS entry.update 同款);生效的是最后一个
        assert seen == [{"v": 1}, {"v": 2}, {"v": 3}]
        assert list(loader.rows)[0]["config"] == {"v": 3}  # 行内持久
        assert loader._fibers["a"].config == {"v": 3}  # 运行态 = last-wins

    _run(run())


def test_patch_inserts_new_rows():
    seen = []

    def plugin(ctx, config):
        seen.append(config)

    async def run():
        ctx = Context()
        loader = Loader(ctx, [], plugins={"p": plugin})
        loader.mount()
        await loader.apply_patches([{"id": "new", "name": "p", "config": {"v": 9}}])
        assert seen == [{"v": 9}]
        assert list(loader.rows)[0]["id"] == "new"

    _run(run())


def test_patch_disables_row():
    seen = []

    def plugin(ctx, config):
        seen.append(config)

    async def run():
        ctx = Context()
        loader = Loader(ctx, [{"id": "a", "name": "p", "config": {"v": 1}}],
                        plugins={"p": plugin})
        loader.mount()
        await loader._fibers["a"].wait()
        await loader.apply_patches([{"id": "a", "disabled": True}])
        assert seen == [{"v": 1}]  # 卸载后不再装载
        assert "a" not in loader._fibers

    _run(run())


def test_patch_config_on_pending_fiber():
    """依赖未就绪时 patch → 就绪后按新 config 装载。"""
    seen = []

    def provider(ctx, config):
        ctx.provide("database", "db")

    def consumer(ctx, config):
        seen.append(config)

    async def run():
        ctx = Context()
        loader = Loader(ctx, [
            {"id": "c", "name": "consumer", "config": {"v": 1}, "inject": ["database"]},
            {"id": "p", "name": "provider"},
        ], plugins={"provider": provider, "consumer": consumer})
        loader.mount()
        await loader.apply_patches([{"id": "c", "config": {"v": 2}}])  # 仍 PENDING
        await asyncio.sleep(0.02)
        assert seen == [{"v": 2}]  # 拓扑激活用 patch 后的 config

    _run(run())


def test_env_interpolation():
    import os

    os.environ["CS_LOADER_TEST"] = "from-env"
    try:
        assert _interpolate({"k": "$env:CS_LOADER_TEST", "n": 1}) == {
            "k": "from-env", "n": 1}
        assert _interpolate(["$env:CS_LOADER_TEST", "x"]) == ["from-env", "x"]
        assert _interpolate("$env:MISSING_CS_LOADER") == ""
        assert _interpolate("plain") == "plain"
    finally:
        del os.environ["CS_LOADER_TEST"]
