"""Loader config expression helpers — translation of vendor/loader/src/config/utils.ts.

``evaluate``:TS ``new Function('ctx','expr','with(ctx){return eval(expr)}')``
→ Python ``eval(expr, _ContextScope(ctx))``。**表达式方言是 Python,不是
JS**(include 的 ``!!js`` 标签按 Python 表达式写,文档已注明)。

``_ContextScope``:eval 作用域 dict,未命中键走 ``ctx`` 属性解析(含服务
解析)—— 等价 TS ``with(ctx)`` 中 ctx 是 Proxy 的语义。内建符号禁用
(``__builtins__ = {}``),表达式只能访问 ctx 暴露的名字。

``interpolate``:递归替换 dict 里的 ``{"__jsExpr": str}`` 节点;只递归
dict/list,类实例原样穿透(TS 的 typeof-object 分支在 Python 只映射到
dict/list)。
"""

from __future__ import annotations

from typing import Any


def is_js_expr(value: Any) -> bool:
    """Return true when a value is a serialized loader expression node."""
    return isinstance(value, dict) and "__jsExpr" in value


class _ContextScope(dict):
    """eval 作用域:未命中键走 ctx 属性解析(TS with(ctx) 的 Proxy 等价)。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__()
        # 显式放空内建:表达式只能访问 ctx 暴露的名字(禁用 eval/open 等)
        self["__builtins__"] = {}
        self._ctx = ctx

    def __getitem__(self, key: str) -> Any:
        if key in self:
            return super().__getitem__(key)
        try:
            return getattr(self._ctx, key)
        except AttributeError:
            raise KeyError(key) from None


def evaluate(ctx: Any, expr: str) -> Any:
    """Evaluate a Python expression against a loader context scope."""
    return eval(expr, _ContextScope(ctx))  # noqa: S307


def interpolate(ctx: Any, value: Any) -> Any:
    """Recursively replace serialized expression nodes with evaluated values."""
    if is_js_expr(value):
        return evaluate(ctx, value["__jsExpr"])
    if value is None or not isinstance(value, (dict, list)):
        return value
    if isinstance(value, list):
        return [interpolate(ctx, item) for item in value]
    return {key: interpolate(ctx, item) for key, item in value.items()}
