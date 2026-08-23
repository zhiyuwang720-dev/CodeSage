"""Service isolation helpers — translation of vendor/loader/src/config/isolate.ts.

``entry.options.isolate = {服务名: true | 标签}``:
- ``true`` → LocalRealm(suffix ``'#'+id``):该 entry 私有服务实现
- 字符串标签 → GlobalRealm(suffix ``'@'+label``):同标签 entry 共享

patch-context 7 步:realm 解析 → 服务 diff → map re-point → ``next()`` →
服务实现转移 → ``reflect.notify``(自定义 filter)→ delim 清理。

Python 映射:TS Symbol → 字符串(suffix 拼出);``Object.create`` 原型链 →
``_ProtoDict``(``_proto`` 引用可变 = setPrototypeOf);``swap`` →
clear+update 保持对象 identity;ctx 上的 delim 键以 ``_`` 前缀避开
``Context.__setattr__`` 拦截。
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from ..utils import INTERCEPT, ISOLATE

if TYPE_CHECKING:
    from ..context import Context
    from .entry import Entry

__all__ = ["Realm", "LocalRealm", "GlobalRealm", "isolate"]


class _ProtoDict(dict):
    """原型链模拟:get/``__getitem__``/``__contains__`` 沿 ``_proto`` 回退。

    TS ``Object.create(parent)`` 的 dict 等价:own 优先,未命中沿链;
    ``_proto`` 引用可变,即 ``Object.setPrototypeOf`` 的等价物。
    """

    def __init__(self, proto: dict | None = None) -> None:
        super().__init__()
        self._proto = proto

    def __getitem__(self, key: str) -> Any:
        # 只判 own(dict.__contains__):重载的 __contains__ 含 proto 链,
        # 用它判 own 会令链上命中键在 super().__getitem__ 处 KeyError
        if super().__contains__(key):
            return super().__getitem__(key)
        if self._proto is not None:
            return _proto_get(self._proto, key)
        raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        return super().__contains__(key) or (self._proto is not None and key in self._proto)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


def _proto_get(map_: dict, key: str) -> Any:
    """沿 ``_proto`` 链读(普通 dict 退化:只查 own)。"""
    node = map_
    seen: set[int] = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if key in node:
            return node[key]
        node = getattr(node, "_proto", None)
    return None


def _ctx_lookup(ctx: "Context", name: str) -> Any:
    """沿 ctx.__dict__ → _fallback 链读(TS Reflect.get 的裸链语义)。"""
    node: Any = ctx
    seen: set[int] = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if name in node.__dict__:
            return node.__dict__[name]
        node = node.__dict__.get("_fallback")
    return None


def _swap(target: dict, source: dict | None) -> None:
    """清空并装入 source 的内容,保持 target 对象 identity(TS swap)。"""
    for key in list(target.keys()):
        del target[key]
    if source:
        target.update(source)


class Realm:
    """Symbol realm used to isolate service implementations by entry or label."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    @property
    def suffix(self) -> str:
        raise NotImplementedError

    def access(self, key: str, create: bool = False) -> str:
        if create:
            if key not in self.store:
                self.store[key] = f"{key}{self.suffix}"
            return self.store[key]
        return self.store.get(key) or f"{key}{self.suffix}"

    def delete(self, key: str) -> None:
        self.store.pop(key, None)

    @property
    def size(self) -> int:
        return len(self.store)


class LocalRealm(Realm):
    """Entry-local isolation realm."""

    def __init__(self, entry: "Entry") -> None:
        super().__init__()
        self.entry = entry

    @property
    def suffix(self) -> str:
        return "#" + self.entry.options["id"]


class GlobalRealm(Realm):
    """Named isolation realm shared by entries that use the same label."""

    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label

    @property
    def suffix(self) -> str:
        return "@" + self.label


def isolate(ctx: "Context", config: Any = None) -> None:
    """Install loader hooks that apply `intercept` and `isolate` entry options.

    TS runner 恒以 (ctx, config) 调用回调,JS 对多余实参静默丢弃;Python
    需要显式默认参(1:1 等价)。
    """
    realms: dict[str, GlobalRealm] = {}
    delims: dict[str, str] = {}

    def access(entry: "Entry", name: str, create: bool = False) -> str | None:
        realm: "Realm | None" = None
        isolate_opts = entry.options.get("isolate") or {}
        label = isolate_opts.get(name)
        if not label:
            return None
        if label is True:
            realm = getattr(entry, "realm", None)
            if realm is None:
                realm = LocalRealm(entry)
                entry.realm = realm
        elif create:
            realm = realms.setdefault(label, GlobalRealm(label))
        else:
            realm = realms.get(label)
        return realm.access(name, create) if realm is not None else None

    def on_entry_init(entry: "Entry") -> None:
        old_isolate = entry.ctx.__dict__.get(ISOLATE, {})
        old_intercept = entry.ctx.__dict__.get(INTERCEPT, {})
        object.__setattr__(entry.ctx, ISOLATE, _ProtoDict(old_isolate))
        object.__setattr__(entry.ctx, INTERCEPT, _ProtoDict(old_intercept))

    async def on_patch_context(entry: "Entry", next_fn: Any) -> None:
        # step 1: generate new isolate map
        new_map = _ProtoDict(entry.parent.ctx.__dict__.get(ISOLATE, {}))
        for name in entry.options.get("isolate") or {}:
            new_map[name] = access(entry, name, True)

        # step 2: generate service diff
        diff: dict[str, tuple] = {}
        old_map = entry.ctx.__dict__.get(ISOLATE, {})
        for name in set(new_map) | set(delims):
            if _proto_get(new_map, name) == _proto_get(old_map, name):
                continue
            delim = delims.setdefault(name, f"__cordis_delim:{name}")
            object.__setattr__(entry.ctx, delim, f"{name}#{entry.id}")
            for sym in (old_map.get(name), _proto_get(new_map, name)):
                if not sym:
                    continue
                impl = entry.ctx.reflect.store.get(sym)
                if impl is None:
                    continue
                if not impl.fiber:
                    entry.ctx.logger.warn(ValueError(f"expected service {name} to be implemented"))
                    continue
                diff[name] = (
                    old_map.get(name),
                    _proto_get(new_map, name),
                    entry.ctx.__dict__[delim],
                    _ctx_lookup(impl.fiber.ctx, delim),
                )
                if entry.ctx.__dict__[delim] != _ctx_lookup(impl.fiber.ctx, delim):
                    break

        # step 3: set prototype for transferred context
        iso_map = entry.ctx.__dict__[ISOLATE]
        icp_map = entry.ctx.__dict__[INTERCEPT]
        iso_map._proto = entry.parent.ctx.__dict__.get(ISOLATE, {})
        icp_map._proto = entry.parent.ctx.__dict__.get(INTERCEPT, {})
        _swap(iso_map, new_map)
        _swap(icp_map, entry.options.get("intercept"))

        # step 4: reload fiber
        result = next_fn()
        if result is not None and inspect.isawaitable(result):
            await result

        # step 5: replace service impl
        for symbol1, symbol2, flag1, flag2 in diff.values():
            if (
                flag1 == flag2
                and symbol1 in entry.ctx.reflect.store
                and symbol2 not in entry.ctx.reflect.store
            ):
                entry.ctx.reflect.store[symbol2] = entry.ctx.reflect.store[symbol1]
                del entry.ctx.reflect.store[symbol1]

        # step 6: reflect notify
        def notify_filter(fctx: "Context", name: str) -> bool:
            symbol1, symbol2, flag1, flag2 = diff[name]
            symbol3 = _proto_get(fctx.__dict__.get(ISOLATE, {}), name)
            flag3 = _ctx_lookup(fctx, delims[name])
            return (symbol1 == symbol3 or symbol2 == symbol3) and (flag1 == flag3) != (flag1 == flag2)

        ctx.reflect.notify(list(diff.keys()), notify_filter)

        # step 7: clean up delimiters
        for name in list(delims):
            if name not in new_map.keys():
                entry.ctx.__dict__.pop(delims[name], None)

    def on_partial_dispose(entry: "Entry", legacy: dict, active: bool) -> None:
        legacy_isolate = legacy.get("isolate") or {}
        for name, label in legacy_isolate.items():
            if label is True:
                continue
            if active and (entry.options.get("isolate") or {}).get(name) == label:
                continue
            realm = realms.get(label)
            if not realm:
                continue

            # realm garbage collection
            for other in ctx.loader.entries():
                # has reference to this realm
                if (other.options.get("isolate") or {}).get(name) == realm.label:
                    return
            realm.delete(name)
            if not realm.size:
                del realms[realm.label]

    ctx.on("loader/entry-init", on_entry_init)
    ctx.on("loader/patch-context", on_patch_context)
    ctx.on("loader/partial-dispose", on_partial_dispose)
