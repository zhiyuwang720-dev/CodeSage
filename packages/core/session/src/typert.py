"""轻量类型注册表:类型名 → 运行时校验器。

DSH 中 typert 是全局注入的依赖类型系统(装饰器注入、类型名检查
跨包共享);cordis-py 没有对应物,本包自建一个最小注册表承载
同一概念:类型名集中登记、`is` 查询形状资格、`check` 强校验。

**边界**:不挂任何 ctx,不参与依赖注入 —— 这里的用途是让
「类型名」有权威落点(例如后续包校验事件 data 形状时按名取
校验器)。真正需要全局注入时再提为独立包(注释在案)。
"""

from __future__ import annotations

from collections.abc import Callable

__all__ = ["check", "define", "is_", "known"]

#: 注册表:类型名 → 校验器(value 为合法该类型的实例时返回 True)。
_REGISTRY: dict[str, Callable[[object], bool]] = {}


def define(name: str, validator: Callable[[object], bool]) -> None:
    """登记一个类型名及其校验器(声明合并的落点)。"""
    _REGISTRY[name] = validator


def is_(name: str, value: object) -> bool:
    """查询一个值是否该类型(未登记的类型名按 False 处理)。"""
    validator = _REGISTRY.get(name)
    if validator is None:
        return False
    try:
        return validator(value)
    except Exception:
        return False


def check(name: str, value: object) -> None:
    """强校验:不匹配即抛 TypeError。"""
    if not is_(name, value):
        raise TypeError(f"value does not satisfy registered type {name!r}")


def known() -> tuple[str, ...]:
    """已登记的类型名清单(调试与自省用)。"""
    return tuple(sorted(_REGISTRY))
