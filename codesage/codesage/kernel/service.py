"""Service base class — translation of vendor/cordis/src/service.ts.

子类 ``super(ctx, name)`` 即完成注册:构造器调 ``ctx.reflect.provide(name,
self, check)`` —— 服务随提供方 fiber 卸载自动注销。

与 TS 的差异:
- 可调用服务(invoke)→ 子类实现 ``__call__``;无 createCallable 包装
- ``Service[Symbol.hasInstance]`` 链式 instanceof → Python 天然多态,无需
- ``@Inject`` 装饰器 → 无(类上直接声明 ``inject`` 属性即可)
- intercept 原型链 → 写时复制 dict(extend 时全量复制,读时单层)
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from .utils import INTERCEPT, ISOLATE, is_object

if TYPE_CHECKING:
    from .context import Context

#: TS Service 静态 symbol 键。子类覆写对应方法名:
#:   init   → __cordis_init__()   实例化后回调(class 插件)
#:   check  → check()             服务可用性谓词(提供给 reflect.provide)
#:   invoke → __call__()          可调用服务
#:   extend → __cordis_extend__() 派生服务实例
#:   filter → __cordis_filter__() 事件派发隔离过滤(protected)
#:   resolve_config → __cordis_resolve_config__() 拦截配置合并
INIT = "__cordis_init__"
CHECK = "__cordis_check__"
CONFIG = "__cordis_config__"
INVOKE = "__cordis_invoke__"
EXTEND = "__cordis_extend__"
FILTER = "__cordis_filter__"
TRACKER = "__cordis_tracker__"
RESOLVE_CONFIG = "__cordis_resolve_config__"


class Service:
    """在 ctx 上暴露命名 API 的服务基类。"""

    #: 默认服务名(TS 静态字段 ``provide``;构造时 name 缺省用它)
    provide: str = ""

    def __init__(self, ctx: Context, name: str | None = None) -> None:
        name = name or self.__class__.provide
        self.ctx: Context = ctx
        self.name: str = name
        check = getattr(self, "check", None)
        ctx.reflect.provide(name, self, check if callable(check) else None)

    # --- 子类可覆写 ---

    def __cordis_filter__(self, ctx: Context) -> bool:
        """事件派发过滤:仅同 isolate 标签的 ctx 可见(TS protected [filter])。"""
        return getattr(ctx, ISOLATE).get(self.name) is getattr(
            self.ctx, ISOLATE
        ).get(self.name)

    def __cordis_extend__(self, props: dict | None = None) -> Any:
        """派生服务实例(TS [extend]):浅拷贝 + 属性覆盖。"""
        out = copy.copy(self)
        if props:
            for k, v in props.items():
                setattr(out, k, v)
        return out

    def __cordis_resolve_config__(self, base: Any = None, head: Any = None) -> Any:
        """合并 intercept 配置:写时复制链,单层读即含全部祖先(根最近优先)。"""
        intercept = getattr(self.ctx, INTERCEPT)
        configs: list[Any] = []
        if self.name in intercept:
            configs.insert(0, intercept[self.name])
        if base is not None:
            configs.insert(0, base)
        if head is not None:
            configs.append(head)
        merge = getattr(self.__class__, "Config", None)
        if merge is not None and callable(getattr(merge, "merge", None)):
            return merge.merge(*configs)
        result: dict = {}
        for c in configs:
            if is_object(c):
                result.update(c)
        return result
