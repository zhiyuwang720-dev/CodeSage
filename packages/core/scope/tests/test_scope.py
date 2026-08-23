"""scope 包测试:标签继承、作用域链、路由牌筛选、注册表、事件流。

运行:python -m pytest tests/ -q
前置:scope 包在 packages/core/ 下,cordis-py 在仓库根(独立仓库),
本文件自行插入 sys.path,不依赖安装。
"""

import asyncio
import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[2]  # packages/core
_ROOT = Path(__file__).resolve().parents[4]  # 仓库根(CodeSage)
# cordis 内核独立仓库(与 CodeSage 平级的兄弟目录,见项目约定)
sys.path.insert(0, str(_CORE))
sys.path.insert(0, str(_ROOT.parent / "cordis-py"))

from cordis import Context  # noqa: E402
from cordis.service import FILTER  # noqa: E402

from scope import (  # noqa: E402
    AnonymousEntries,
    NamedEntries,
    ScopedLayers,
    bind_scope_parent,
    carrier_key_of,
    create_scope,
    is_scope_carrier,
    scope_chain_of,
    scope_of,
    scope_parent_of,
    scope_target,
)


class _Key:
    """测试作用域键:普通对象(TS 的对象约束)。"""


# --- 作用域与标签 ---


def test_scope_tag_inherits():
    ctx = Context()
    key = _Key()
    scope = create_scope(ctx, key)
    assert scope_of(scope.ctx) is key
    child = scope.ctx.extend({"extra": 1})
    assert scope_of(child) is key  # 子 ctx 继承标签
    assert scope_of(ctx) is None  # 原 ctx 无标签


def test_scope_bind_and_chain():
    parent, child, grand = _Key(), _Key(), _Key()
    bind_scope_parent(child, parent)
    bind_scope_parent(grand, child)
    assert scope_parent_of(grand) is child
    assert scope_parent_of(parent) is None
    assert scope_chain_of(grand) == [grand, child, parent]


def test_cycle_detection():
    parent, child = _Key(), _Key()
    bind_scope_parent(child, parent)
    with pytest.raises(ValueError):
        bind_scope_parent(parent, child)  # 成环:拒绝


def test_rebind_only_original_binder():
    parent, child, next_parent = _Key(), _Key(), _Key()
    binding = bind_scope_parent(child, parent)
    with pytest.raises(ValueError):
        bind_scope_parent(child, next_parent)  # 重复绑定:拒绝
    binding.rebind(next_parent)  # 原绑定者可以重绑
    assert scope_parent_of(child) is next_parent


# --- 路由载体 ---


def test_carrier_admits_untagged_and_ancestors():
    ctx = Context()
    parent, child = _Key(), _Key()
    bind_scope_parent(child, parent)
    carrier = scope_target({}, child)
    assert is_scope_carrier(carrier)
    assert carrier_key_of(carrier) is child
    assert not is_scope_carrier({})
    assert carrier_key_of({}) is None
    filter_fn = getattr(carrier, FILTER)
    assert filter_fn(ctx) is True  # 无标签监听器:放行
    assert filter_fn(create_scope(ctx, child).ctx) is True  # 自身
    assert filter_fn(create_scope(ctx, parent).ctx) is True  # 祖先:放行
    assert filter_fn(create_scope(ctx, _Key()).ctx) is False  # 无关:不放行


def test_unkeyed_carrier_admits_only_untagged():
    ctx = Context()
    carrier = scope_target({}, None)
    assert carrier_key_of(carrier) is None
    filter_fn = getattr(carrier, FILTER)
    assert filter_fn(ctx) is True
    assert filter_fn(create_scope(ctx, _Key()).ctx) is False


# --- 事件流:向上流,不向下流 ---


async def _event_flow():
    ctx = Context()
    key = _Key()
    scope = create_scope(ctx, key)
    await scope.ctx.fiber.wait()  # 等作用域 fiber 装载完成(noop 插件)
    received_global, received_scoped, received_parent = [], [], []

    ctx.on("demo/event", lambda payload: received_global.append(payload))
    scope.ctx.on("demo/event", lambda payload: received_scoped.append(payload))

    carrier = scope_target({}, key)
    ctx.emit(carrier, "demo/event", {"n": 1})  # 派发到子作用域
    assert received_global == [{"n": 1}]  # 组合级:收到
    assert received_scoped == [{"n": 1}]  # 作用域自己:收到

    ctx.emit(scope_target({}, None), "demo/event", {"n": 2})  # 组合级事件
    assert received_scoped == [{"n": 1}]  # 子作用域:收不到(不向下流)
    assert received_global == [{"n": 1}, {"n": 2}]

    parent = _Key()
    bind_scope_parent(key, parent)
    parent_scope = create_scope(ctx, parent)
    await parent_scope.ctx.fiber.wait()
    parent_scope.ctx.on("demo/event", lambda payload: received_parent.append(payload))
    ctx.emit(carrier, "demo/event", {"n": 3})  # 再派发到子作用域
    assert received_parent == [{"n": 3}]  # 祖先:收到(向上流)


def test_event_flow_up_not_down():
    asyncio.run(_event_flow())


# --- 插入序表 ---


def test_named_entries_insertion_order_and_undo():
    entries = NamedEntries(lambda name: ValueError(f"duplicate {name}"))
    undo_a = entries.insert("a", 1)
    undo_b = entries.insert("b", 2)
    assert entries.get("a") == 1
    assert list(entries.values()) == [1, 2]  # 插入序
    with pytest.raises(ValueError):
        entries.insert("a", 3)  # 重名拒绝
    undo_a()
    undo_a()  # 幂等
    assert not entries.has("a")
    assert list(entries.values()) == [2]
    undo_b()
    assert entries.is_empty()


def test_named_entries_table_generation():
    # 换代纪律:清空后旧迭代器脱离新代(TS Map 允许迭代中插入并可见,
    # Python dict 迭代中修改抛 RuntimeError —— 移植边界,行为取 Python)
    entries = NamedEntries(lambda name: ValueError())
    undo_a = entries.insert("a", 1)
    it_old = iter(entries.values())
    assert list(it_old) == [1]  # 换代前捕获:看到旧代
    undo_a()  # 空表 → 换代
    entries.insert("b", 2)  # 新代
    assert list(entries.values()) == [2]  # 新代内容
    assert list(it_old) == []  # 旧迭代器停在旧代(已空),不抛错


def test_anonymous_entries_separate_identity():
    entries = AnonymousEntries()
    undo_a = entries.append(1)
    undo_b = entries.append(1)  # 相等值互不合并
    assert list(entries.values()) == [1, 1]
    undo_a()
    assert list(entries.values()) == [1]
    undo_b()
    assert entries.is_empty()


# --- ScopedLayers:每作用域一张层 ---


def _layers():
    return ScopedLayers(
        lambda scope: NamedEntries(lambda name: ValueError(f"duplicate {name}")),
        lambda: None,
    )


async def _scoped_layers_merge_and_shadow():
    ctx = Context()
    layers = _layers()
    layers.global_.insert("g", "global")
    parent, child = _Key(), _Key()
    bind_scope_parent(child, parent)

    parent_scope = create_scope(ctx, parent)
    await parent_scope.ctx.fiber.wait()
    child_scope = create_scope(ctx, child)
    await child_scope.ctx.fiber.wait()

    layers.effect(parent_scope.ctx, lambda layer: layer.insert("g", "parent"), "parent")
    assert layers.peek(parent).get("g") == "parent"

    # 子作用域视角:全局 + 父层 + 自己的层,同名取最近
    layers.effect(child_scope.ctx, lambda layer: layer.insert("g", "child"), "child")
    merged = layers.merge(child, lambda layer: layer)
    assert merged == {"g": "child"}
    assert list(merged.keys()) == ["g"]  # 插入序保持

    # 祖先视角:看不到子孙的贡献
    assert layers.merge(parent, lambda layer: layer) == {"g": "parent"}


def test_scoped_layers_merge_and_shadow():
    asyncio.run(_scoped_layers_merge_and_shadow())


async def _scoped_layers_effect_lifecycle():
    ctx = Context()
    changes = []
    layers = ScopedLayers(
        lambda scope: NamedEntries(lambda name: ValueError()),
        lambda: changes.append(1),
    )
    key = _Key()
    scope = create_scope(ctx, key)
    await scope.ctx.fiber.wait()

    dispose = layers.effect(scope.ctx, lambda layer: layer.insert("t", 1), "t")
    assert changes == [1]  # 注册即通知
    assert layers.peek(key).get("t") == 1

    # 注册失败:回收刚建的空层
    with pytest.raises(ValueError):
        layers.effect(
            scope.ctx,
            lambda layer: (_ for _ in ()).throw(ValueError()),
            "fail",
        )
    assert layers.peek(key).get("t") == 1  # 原注册不受影响

    await dispose()  # 撤销 → 空层回收 + 通知
    assert changes == [1, 1]
    assert layers.peek(key) is None  # 空层已回收


def test_scoped_layers_effect_lifecycle():
    asyncio.run(_scoped_layers_effect_lifecycle())


async def _scope_dispose_reclaims_registration():
    ctx = Context()
    layers = _layers()
    key = _Key()
    scope = create_scope(ctx, key)
    await scope.ctx.fiber.wait()

    layers.effect(scope.ctx, lambda layer: layer.insert("x", 1), "x")
    assert layers.peek(key).get("x") == 1

    await scope.dispose()
    await scope.dispose()  # 幂等:共享同一完成
    assert layers.peek(key) is None  # 注册随作用域卸载逆序撤销,空层回收


def test_scope_dispose_reclaims_registration():
    asyncio.run(_scope_dispose_reclaims_registration())
