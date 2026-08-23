"""Context — translation of vendor/cordis/src/context.ts.

Context 是服务容器与插件作用域。TS 中它是 Proxy:普通属性读取走服务解析
(ReflectService.handler);Python 等价为 ``__getattr__/__setattr__/
__contains__``(仅当常规查找失败时触发,与 Proxy get 的 Reflect.get
先行语义一致)。

作用域:``extend(meta)`` 创建子 ctx(原型继承 → 浅拷贝 __dict__ + meta
覆盖);``isolate(name, label)`` 独立服务作用域;``intercept(name, config)``
追加服务配置拦截。三者都不改动父 ctx。

Python 映射差异(见 docs/modules/21 映射表):
- Proxy handler 的 isSpecialProperty 分支 → ``__getattr__`` 内对
  ``_`` 前缀/保留名/数字串直接抛 AttributeError(缺失语义)
- getTraceable/bind(调用栈追踪 Proxy)省略,receiver 参数恒为 None
- enhanceError(错误栈整形)省略,错误消息与 TS 一致
"""

from __future__ import annotations

from typing import Any

from .events import EventsService
from .fiber import Fiber
from .logger import LoggerService
from .reflect import ReflectService
from .registry import RegistryService
from .utils import FALLBACK, INTERCEPT, ISOLATE, is_special_property


#: 哨兵:区分「沿链未命中」与「命中值为 None」
_MISSING = object()


def _get_real(ctx: "Context", name: str) -> Any:
    """沿 ``_fallback`` 原型链查真实属性(loader 的 ctx re-point)。

    TS ``Object.setPrototypeOf(entry.ctx, entry.parent.ctx)`` 后,``prop in
    target`` / ``Reflect.get`` 沿裸对象链只命中 own 属性 —— 不触发祖先的
    服务解析(服务解析在下方走 ``self.fiber`` 链,两者语义不同)。
    """
    seen: set[int] = set()
    node = ctx.__dict__.get(FALLBACK)
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if name in node.__dict__:
            return node.__dict__[name]
        node = node.__dict__.get(FALLBACK)
    return _MISSING


class Context:
    """根上下文:构造时安装内置服务(fiber/reflect/registry/events/logger)。

    内置服务是 ctx 的常规属性(TS 构造器里的 own property),不是服务仓库
    条目;``ctx.get('logger')`` 返回 None,与 TS 一致。
    """

    def __init__(self) -> None:
        object.__setattr__(self, ISOLATE, {})
        object.__setattr__(self, INTERCEPT, {})
        object.__setattr__(self, "root", self)
        object.__setattr__(self, "baseUrl", None)
        object.__setattr__(self, FALLBACK, None)
        object.__setattr__(self, "fiber", Fiber(self, {}, {}, None))
        object.__setattr__(self, "reflect", ReflectService(self))
        object.__setattr__(self, "registry", RegistryService(self))
        object.__setattr__(self, "events", EventsService(self))
        object.__setattr__(self, "logger", LoggerService(self))
        # TS 同款:构造完成后清空根 fiber 的效果记录(根 fiber 不卸载,
        # 服务与 mixin 常驻;dispose = restart 时无可清理)
        self.fiber._disposables.clear()

    def __repr__(self) -> str:
        return f"Context <{self.fiber.name}>"

    # --- Proxy 等价(TS ReflectService.handler) ---

    def __getattr__(self, name: str) -> Any:
        """TS handler.get:accessor 计算属性 → 服务解析(带 internal/get
        waterfall)。特殊属性直接缺失(AttributeError)。"""
        if is_special_property(name):
            raise AttributeError(name)
        reflect = self.__dict__.get("reflect")
        if reflect is None:
            raise AttributeError(name)
        prop = reflect.props.get(name)
        if prop is not None and prop["type"] == "accessor":
            return prop["get"](self, None)
        # 原型链真实属性优先(TS prop-in-target → Reflect.get;loader 场景)
        real = _get_real(self, name)
        if real is not _MISSING:
            return real
        if not self.fiber.runtime:
            # 根 ctx:无 waterfall,非严格读仓库
            return reflect.get(name, False)
        error = RuntimeError(f'cannot get property "{name}" without inject')
        return self.events.waterfall(
            "internal/get",
            self,
            name,
            error,
            lambda: self._resolve_service(name, error),
        )

    def __setattr__(self, name: str, value: Any) -> None:
        """TS handler.set:accessor set 钩子 → internal/set waterfall →
        reflect.set;根 ctx 直接写属性,子 ctx 未提供名抛错。"""
        if is_special_property(name):
            object.__setattr__(self, name, value)
            return
        reflect = self.__dict__.get("reflect")
        prop = reflect.props.get(name) if reflect is not None else None
        if prop is None:
            if name in self.__dict__:
                # 已有 own 属性直接写(TS prop-in-target → Reflect.set)
                object.__setattr__(self, name, value)
                return
            if not self.fiber.runtime:
                object.__setattr__(self, name, value)
                return
            raise RuntimeError(f'cannot set property "{name}" without provide')
        error = RuntimeError(f'cannot set property "{name}" without provide')
        if prop["type"] == "accessor":
            set_fn = prop.get("set")
            if set_fn is None:
                return
            set_fn(self, value, None)
            return
        self.events.waterfall(
            "internal/set",
            self,
            name,
            value,
            error,
            lambda: self.reflect.set(name, value, error),
        )

    def __contains__(self, name: str) -> bool:
        """TS handler.has:own 属性(含原型链)或已声明的 props(不含类方法)。"""
        if name in self.__dict__:
            return True
        if _get_real(self, name) is not _MISSING:
            return True
        reflect = self.__dict__.get("reflect")
        return reflect is not None and name in reflect.props

    def _resolve_service(self, name: str, error: RuntimeError) -> Any:
        """沿 fiber 链向上找实现(TS handler.get 内联 walk):
        越过 isolate 边界、遇到无 runtime 祖先或依赖缺失即抛错。"""
        key = getattr(self, ISOLATE).get(name)
        fiber = self.fiber
        while True:
            impl = fiber.store.get(name) if fiber.store else None
            if impl is not None:
                return impl.value
            if name in fiber.inject:
                raise RuntimeError(
                    f'cannot get required service "{name}" in inactive context'
                )
            if not fiber.runtime:
                raise error
            if getattr(fiber.parent, ISOLATE).get(name) is not key:
                raise error
            fiber = fiber.parent.fiber

    # --- 作用域 ---

    def extend(self, meta: dict | None = None) -> "Context":
        """创建子 ctx:继承全部属性,meta 覆盖(TS Object.create + 定义
        own props)。父 ctx 不被修改。"""
        child = object.__new__(type(self))
        object.__setattr__(child, "__dict__", dict(self.__dict__))
        for key, value in (meta or {}).items():
            object.__setattr__(child, key, value)
        return child

    def isolate(self, name: str, label: Any = None) -> "Context":
        """创建独立服务作用域的子 ctx:该服务名在子 ctx 下解析到新标签。
        相同 label 的两次 isolate() 合并作用域。"""
        shadow = dict(getattr(self, ISOLATE))
        shadow[name] = label if label is not None else object()
        return self.extend({ISOLATE: shadow})

    def intercept(self, name: str, config: Any) -> "Context":
        """创建携带服务拦截配置的子 ctx:该服务名的插件配置合并 config。"""
        shadow = dict(getattr(self, INTERCEPT))
        shadow[name] = config
        return self.extend({INTERCEPT: shadow})
