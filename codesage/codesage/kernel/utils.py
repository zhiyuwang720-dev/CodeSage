"""Shared internals — translation of vendor/cordis/src/utils.ts.

TS 用 unique symbol + 原型链 + Proxy 实现的机制,Python 对应:
- symbols → 带 ``__cordis_`` 前缀的字符串常量(防公开属性名冲突)
- DisposableList(WeakMap 键) → 普通 list + 按值删除
- isObject / isConstructor → 直接等价

省略(无 Python 等价物,见 docs/modules/21 映射表):
- getTraceable / createTraceable / createCallable:TS 的动态 Proxy 追踪,
  Python 方法天然绑定,ctx 属性解析直接返回服务对象
- composeError / buildOuterStack:调用栈拼接工程,Python traceback 自带
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

# --- symbols 等价(cordis.ts utils.symbols) ---

SHADOW = "__cordis_shadow__"
RECEIVER = "__cordis_receiver__"
ORIGINAL = "__cordis_original__"
METADATA = "__cordis_metadata__"
INIT_HOOKS = "__cordis_init_hooks__"
CHECK_PROTO = "__cordis_check_proto__"
EFFECT = "__cordis_effect__"
FILTER = "__cordis_filter__"
ISOLATE = "__cordis_isolate__"
INTERCEPT = "__cordis_intercept__"
FALLBACK = "__cordis_fallback__"
#: loader 牌子:Entry 挂到子 ctx 的键 / EntryGroup 插件标记(TS symbol)
ENTRY = "__cordis_entry__"
GROUP = "__cordis_group__"
INIT = "__cordis_init__"
CHECK = "__cordis_check__"
CONFIG = "__cordis_config__"
INVOKE = "__cordis_invoke__"
EXTEND = "__cordis_extend__"
TRACKER = "__cordis_tracker__"
RESOLVE_CONFIG = "__cordis_resolve_config__"

#: 保留名(ctx 属性解析直接放行,不参与服务解析)
RESERVED_WORDS = ("prototype", "then")

#: isSpecialProperty 等价: - 下划线前缀 - 保留名 - 数字串
def is_special_property(prop: str) -> bool:
    return (
        prop.startswith("_")
        or prop in RESERVED_WORDS
        or prop.isdigit()
    )


def is_object(value: Any) -> bool:
    """TS isObject 等价:非 None 且非原始值(字符串/数字/布尔)。"""
    return value is not None and not isinstance(value, (str, bytes, int, float, bool))


def is_constructor(func: Any) -> bool:
    """TS isConstructor:生成器/异步函数非构造器,类/普通函数是。"""
    if not isinstance(func, type):
        return False
    # 协程/生成器函数在 Python 里不是 type,不会走到这
    return True


class AggregateError(Exception):
    """并发错误聚合(TS 内建 AggregateError,Python 无同名内建)。

    message 可选:TS 构造签名 ``AggregateError(errors, message?)``。
    """

    def __init__(self, errors: list, message: str | None = None) -> None:
        self.errors = errors
        super().__init__(message or f"{len(errors)} error(s) aggregated")


class DisposableList:
    """有序可删除集合(TS DisposableList):push 返回移除函数,clear 逆序。"""

    def __init__(self) -> None:
        self._items: list[Any] = []

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._items)

    def push(self, value: Any) -> Callable[[], None]:
        self._items.append(value)
        return lambda: self.delete(value)

    def delete(self, value: Any) -> bool:
        try:
            self._items.remove(value)
            return True
        except ValueError:
            return False

    def clear(self) -> list[Any]:
        """取出全部并清空,返回逆序(供逆序回滚)。"""
        values = self._items
        self._items = []
        return list(reversed(values))
