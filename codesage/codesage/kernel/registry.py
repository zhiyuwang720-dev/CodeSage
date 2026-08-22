"""RegistryService — translation of vendor/cordis/src/registry.ts.

插件注册表(ctx.registry,经 mixin 暴露为 ctx.plugin/ctx.inject):
- 插件形状:函数 / 类(构造器)/ 带 apply 方法的对象
- runtime 记录:{name, callback, fibers(DisposableList), Config};同一
  callback 的所有 fiber 共享;最后一个 fiber 卸载时删除
- ctx.plugin(plugin, config) → Fiber(可 await:等装载完成,错误重抛)
- ctx.inject(deps, callback) = ctx.plugin({inject, apply, name})

与 TS 的差异:@Inject 装饰器省略(类上直接声明 inject 属性);
Fiber 直接实现 __await__(TS 用 Object.create 包 then)。
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator

from .fiber import Fiber
from .utils import DisposableList

if TYPE_CHECKING:
    from .context import Context

#: Inject 声明(list[str] 或 dict[str, config] 或 None)
Inject = list[str] | dict[str, Any] | None


def _plugin_attr(plugin: Any, name: str, default: Any = None) -> Any:
    """插件双形态读取:dict(ctx.inject 产物)或对象(TS 属性访问)。"""
    if isinstance(plugin, dict):
        return plugin.get(name, default)
    return getattr(plugin, name, default)


def _is_applicable(plugin: Any) -> bool:
    return plugin is not None and callable(_plugin_attr(plugin, "apply"))


@dataclass(slots=True)
class Runtime:
    """插件 runtime 记录(TS Plugin.Runtime)。"""

    name: str | None = None
    callback: Callable | None = None
    Config: Any = None
    fibers: DisposableList = field(default_factory=DisposableList)


def resolve_inject(inject: Inject, result: dict[str, Any] | None = None) -> dict[str, Any]:
    """规范化依赖声明 → {服务名: 拦截配置|None}。继承的类级 inject 先合并。"""
    if result is None:
        result = {}
    if not inject:
        return result
    if isinstance(inject, list):
        for name in inject:
            result[name] = None
    elif isinstance(inject, dict):
        # 类继承:父类 inject 在前,子类覆盖
        if "__proto__" in inject:
            result.update(resolve_inject(inject.get("__proto__")))
            for name, config in inject.items():
                if name != "__proto__":
                    result[name] = config
        else:
            for name, config in inject.items():
                result[name] = config
    return result


class RegistryService:
    def __init__(self, ctx: "Context") -> None:
        self.ctx = ctx
        self._counter = 0
        self._internal: dict[Callable, Runtime] = {}

    @property
    def counter(self) -> int:
        """分配下一个 fiber uid(每次读取自增)。"""
        self._counter += 1
        return self._counter

    @property
    def size(self) -> int:
        return len(self._internal)

    # --- 解析与查询 ---

    def resolve(self, plugin: Any) -> Callable | None:
        """解析插件形状 → 标识 callback;无效返回 None。
        类与函数都 callable,直接作为 callback(TS typeof function)。"""
        try:
            if callable(plugin):
                return plugin
            if _is_applicable(plugin):
                return _plugin_attr(plugin, "apply")
        except Exception:
            pass
        return None

    def get(self, plugin: Any) -> Runtime | None:
        key = self.resolve(plugin)
        return self._internal.get(key) if key else None

    def has(self, plugin: Any) -> bool:
        key = self.resolve(plugin)
        return bool(key) and key in self._internal

    def delete(self, plugin: Any) -> Runtime | None:
        key = self.resolve(plugin)
        runtime = self._internal.get(key) if key else None
        if runtime is None:
            return None
        del self._internal[key]
        for fiber in runtime.fibers:
            result = fiber.dispose()
            # TS 的 dispose() 是 promise(微任务即跑);Python 需运行中的
            # loop 才可调度,无 loop 时放弃(同步上下文场景,见模块文档)
            if inspect.isawaitable(result):
                try:
                    asyncio.ensure_future(result)
                except RuntimeError:
                    pass
        return runtime

    def keys(self) -> Iterator[Callable]:
        return iter(self._internal.keys())

    def values(self) -> Iterator[Runtime]:
        return iter(self._internal.values())

    def entries(self) -> Iterator[tuple[Callable, Runtime]]:
        return iter(self._internal.items())

    # --- 装载 ---

    def inject(self, deps: Inject, callback: Callable) -> "Fiber":
        """ctx.inject:依赖就绪后执行 callback。"""
        return self.plugin({"inject": deps, "apply": callback, "name": getattr(callback, "__name__", None)})

    def plugin(self, plugin: Any, config: Any = None) -> "Fiber":
        """启动插件,返回可 await 的 fiber(等装载完成)。"""
        callback = self.resolve(plugin)
        if not callback:
            raise TypeError(
                "invalid plugin, expect function or object with an 'apply' method, "
                f"received {type(plugin).__name__}"
            )
        self.ctx.fiber.assert_active()

        runtime = self._internal.get(callback)
        if runtime is None:
            name = _plugin_attr(plugin, "name")
            if name == "apply":
                name = None
            runtime = Runtime(
                name=name,
                callback=callback,
                Config=_plugin_attr(plugin, "Config"),
            )
            self._internal[callback] = runtime

        inject = _plugin_attr(plugin, "inject")
        return Fiber(self.ctx, config, resolve_inject(inject), runtime)
