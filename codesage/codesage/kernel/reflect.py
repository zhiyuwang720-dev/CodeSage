"""ReflectService — translation of vendor/cordis/src/reflect.ts.

ctx 的"反射与服务解析层":属性定义(props: service | accessor)、按 isolate
标签存储的实现(store)、服务注册/注销/通知、mixins(ctx.on 等方法来源)。

与 TS 的差异:
- Proxy handler(属性解析)内联进 context.py 的 __getattr__/__setattr__
- trace()/bind():TS 的调用栈追踪 Proxy,Python 无对应物,省略
- ``Object.create(this.ctx)`` 过滤 ctx → 轻量 filter 包装对象
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from .fiber import Fiber, FiberState
from .service import FILTER  # 事件过滤键
from .utils import ISOLATE

if TYPE_CHECKING:
    from .context import Context

#: Property 类型判别(TS Property.type)
SERVICE_PROP = "service"
ACCESSOR_PROP = "accessor"

#: 轻量过滤 ctx:携带 FILTER 谓词,供 internal/service 等事件 thisArg 使用
class _FilterCtx:
    def __init__(self, base: Context, filter_fn: Callable) -> None:
        self._base = base
        setattr(self, FILTER, filter_fn)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


@dataclass(slots=True)
class Impl:
    """服务实现记录(TS reflect.ts Impl)。"""

    name: str
    fiber: Fiber
    value: Any = None
    check: Callable[[], bool] | None = None


class ReflectService:
    def __init__(self, ctx: Context) -> None:
        self.ctx: Context = ctx
        #: 实现按 isolate 标签 keyed(TS store: Dict<Impl, symbol>)
        self.store: dict[Any, Impl] = {}
        #: 属性定义(service | accessor),按名
        self.props: dict[str, dict] = {}

        # TS 构造器里做 mixin —— ctx.on/ctx.get 等即来自这里
        self.mixin("reflect", ["get", "set", "provide", "accessor", "mixin"])
        self.mixin("fiber", ["runtime", "effect"])
        self.mixin("registry", ["inject", "plugin"])
        self.mixin("events", ["on", "once", "parallel", "emit", "serial", "bail", "waterfall"])

    # --- 读取 ---

    def get(self, name: str, strict: bool = True) -> Any:
        """读服务(无 inject 门槛);未提供或非活跃 → None。"""
        impl = self._get_impl(name, strict)
        return impl.value if impl else None

    def _get_impl(self, name: str, strict: bool = True) -> Impl | None:
        key = getattr(self.ctx, ISOLATE).get(name)
        impl = self.store.get(key) if key else None
        if impl is None:
            return None
        if strict and impl.fiber.state is not FiberState.ACTIVE:
            return None
        return impl

    def set(self, name: str, value: Any, error: BaseException | None = None) -> bool:
        """覆盖已提供服务;只能由提供方 fiber 设置。"""
        key = getattr(self.ctx, ISOLATE).get(name)
        impl = self.store.get(key)
        if impl is None:
            raise RuntimeError(f'cannot set property "{name}" without provide')
        if impl.fiber is not self.ctx.fiber:
            raise RuntimeError(f'cannot set property "{name}" in multiple fibers')
        impl.value = value
        return True

    # --- 注册 ---

    def provide(self, name: str, value: Any = None, check: Callable[[], bool] | None = None):
        """注册服务(由当前 fiber 拥有);返回注销 disposer(fiber 卸载自动跑)。"""
        return self.ctx.fiber.effect(
            lambda: self._provide_impl(name, value, check),
            f"ctx.provide({name})",
        )

    def _provide_impl(self, name: str, value: Any, check: Callable[[], bool] | None):
        props = self.props
        if name not in props:
            props[name] = {"type": SERVICE_PROP}
        elif props[name]["type"] is not SERVICE_PROP:
            raise RuntimeError(f'property "{name}" is already declared as {props[name]["type"]}')
        props[name] = {"type": SERVICE_PROP}

        # root 首次提供该服务名 → 建立全局 isolate 标签
        root_isolate = getattr(self.ctx.root, ISOLATE)
        if name not in root_isolate:
            root_isolate[name] = object()
        key = getattr(self.ctx, ISOLATE)[name]
        impl = Impl(name=name, value=value, fiber=self.ctx.fiber, check=check)
        if key in self.store:
            raise RuntimeError(
                f'service "{name}" has been registered at <{self.store[key].fiber.name}>'
            )
        self.store[key] = impl
        self.ctx.fiber.store[name] = impl  # type: ignore[index]
        if self.ctx.fiber.state is FiberState.ACTIVE:
            self.notify([name])

        async def disposer() -> None:
            del self.store[key]
            fibers = self.notify([name])
            await asyncio.gather(*(f.wait() for f in fibers))
            # 确保自身访问先于依赖清理
            del self.ctx.fiber.store[name]  # type: ignore[index]

        return disposer

    def accessor(self, name: str, options: dict) -> Any:
        """定义计算属性(get/set 钩子);返回注销 disposer。"""
        return self.ctx.fiber.effect(
            lambda: self._accessor_impl(name, options),
            f"ctx.accessor({name})",
        )

    def _accessor_impl(self, name: str, options: dict):
        if name in self.props:
            raise RuntimeError(
                f'property "{name}" is already declared as {self.props[name]["type"]}'
            )
        self.props[name] = {"type": ACCESSOR_PROP, **options}

        def disposer() -> None:
            del self.props[name]

        return disposer

    def mixin(self, source: str | Any, mixins: list[str] | dict[str, str]) -> Any:
        """把服务的成员暴露到 ctx(如 ctx.on → ctx.events.on);返回注销 disposer。"""
        return self.ctx.fiber.effect(
            lambda: self._mixin_impl(source, mixins),
            f"ctx.mixin({source})",
        )

    def _mixin_impl(self, source: str | Any, mixins: list[str] | dict[str, str]) -> list:
        entries = (
            [(k, k) for k in mixins]
            if isinstance(mixins, list)
            else list(mixins.items())
        )

        def get_target(ctx: Context):
            return getattr(ctx, source) if isinstance(source, str) else source

        disposers = []
        for key, ctx_key in entries:
            options = {
                "get": lambda ctx, receiver=None, key=key: _mixin_get(get_target(ctx), key),
                "set": lambda ctx, value, receiver=None, key=key: _mixin_set(get_target(ctx), key, value),
            }
            disposers.append(self.accessor(ctx_key, options))
        return disposers

    # --- 通知 ---

    def notify(
        self,
        names: list[str],
        filter_fn: Callable[[Context, str], bool] | None = None,
    ) -> list[Fiber]:
        """服务变更 → 重检所有依赖 fiber;发 internal/service 事件。"""
        if filter_fn is None:
            def filter_fn(ctx: Context, name: str) -> bool:
                return getattr(ctx, ISOLATE).get(name) is getattr(self.ctx, ISOLATE).get(name)

        fibers: list[Fiber] = []
        for runtime in self.ctx.registry.values():
            for fiber in runtime.fibers:
                has_update = False
                for name in names:
                    if name not in fiber.inject:
                        continue
                    if not filter_fn(fiber.ctx, name):
                        continue
                    has_update = True
                    fiber._check_impl(name)
                if not has_update:
                    continue
                fiber._refresh()
                fibers.append(fiber)

        for name in names:
            self.ctx.events.emit(
                _FilterCtx(self.ctx, lambda ctx, name=name: filter_fn(ctx, name)),
                "internal/service",
                name,
                _impl_value(self._get_impl(name, False)),
            )
        return fibers


def _mixin_get(service: Any, key: str) -> Any:
    if service is None:
        return None
    return getattr(service, key)


def _mixin_set(service: Any, key: str, value: Any) -> bool:
    if service is None:
        return False
    setattr(service, key, value)
    return True


def _impl_value(impl: Impl | None) -> Any:
    return impl.value if impl else None
