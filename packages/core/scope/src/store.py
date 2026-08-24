"""作用域注册表的共享底座:插入序表 + 每作用域一层的所有权。

被 tools、system-prompt 等作用域感知注册表复用的两个零件:
- NamedEntries / AnonymousEntries:插入序表 + 幂等撤销 + 空表换代;
- ScopedLayers:全局层 + 惰性作用域层,查找沿父链合并,最近的层胜出。

「每作用域一张表」的模型:全局层常驻,作用域层首次贡献时才创建,
彻底清空即回收 —— 长期运行的组合不会因空作用域泄漏内存。
"""

from __future__ import annotations

from typing import Any, Callable

from .index import scope_chain_of, scope_of


class ScopeLayer:
    """一个作用域对注册表的聚合贡献。实现方提供:是否每张表都空。"""

    def is_empty(self) -> bool:
        raise NotImplementedError


class NamedEntries:
    """具名插入序表:值借用;迭代器在同一张非空表代内存活。

    表清空时换一张新表 —— 旧迭代器与后续插入脱离(迭代中途表被复用
    会语义错乱,换代是纪律不是巧合)。每次成功插入返回该条目的幂等
    撤销:重复撤销无害,且只删除自己的那一条。
    """

    def __init__(self, duplicate_error: Callable[[str], Exception]) -> None:
        self._duplicate_error = duplicate_error
        self._data: dict[str, Any] = {}

    def insert(self, name: str, value: Any) -> Callable[[], None]:
        """插入一个唯一名字;重名抛调用方提供的重复错误。"""
        data = self._data
        if name in data:
            raise self._duplicate_error(name)
        data[name] = value
        active = True

        def undo() -> None:
            nonlocal active
            if not active:
                return
            active = False
            data.pop(name, None)
            if len(data) == 0 and self._data is data:
                self._data = {}

        return undo

    def get(self, name: str) -> Any | None:
        """按名读一个值;缺席返回 None。"""
        return self._data.get(name)

    def has(self, name: str) -> bool:
        """判一个名是否在册。"""
        return name in self._data

    def keys(self):
        """按插入序迭代在册名字(活视图)。"""
        return self._data.keys()

    def entries(self):
        """按插入序迭代 (名, 值)(活视图)。"""
        return self._data.items()

    def values(self):
        """按插入序迭代值(活视图)。"""
        return self._data.values()

    def is_empty(self) -> bool:
        """这张表是否全空。"""
        return len(self._data) == 0


class AnonymousEntries:
    """匿名插入序表:每次追加是独立注册,相等值互不合并。

    值借用,迭代器在同一张非空表代内存活,表清空换代(与具名表同款
    纪律)。撤销精确到自己的那一次追加。
    """

    def __init__(self) -> None:
        self._data: dict[Any, Any] = {}

    def append(self, value: Any) -> Callable[[], None]:
        """追加一个独立拥有的值,返回它的幂等撤销。"""
        data = self._data
        key = object()
        data[key] = value
        active = True

        def undo() -> None:
            nonlocal active
            if not active:
                return
            active = False
            data.pop(key, None)
            if len(data) == 0 and self._data is data:
                self._data = {}

        return undo

    def values(self):
        """按插入序迭代值(活视图)。"""
        return self._data.values()

    def is_empty(self) -> bool:
        return len(self._data) == 0


class ScopedLayers:
    """拥有一个注册表的全局层与各作用域层。

    读操作绝不创建作用域层。注册从 ctx 派生可见性与 effect 所有权:
    先收集撤销再通知,只回收完全空的聚合层。
    """

    def __init__(
        self,
        create_layer: Callable[[Any | None], ScopeLayer],
        on_change: Callable[[], None],
    ) -> None:
        self._create_layer = create_layer
        self._on_change = on_change
        self._scoped: dict[Any, ScopeLayer] = {}
        # TS 属性名 global(Python 保留字 → global_)
        self.global_: ScopeLayer = create_layer(None)

    def peek(self, scope: Any | None) -> ScopeLayer | None:
        """读既有精确作用域层:链盲,不沿祖先链,也不创建新层。

        刻意链盲:调用方寻址「某个作用域自己的贡献」(它的限制、它的
        守卫)时,不能悄悄捡到祖先的 —— 需要继承语义时用 chain_layers。
        """
        if scope is None:
            return None
        return self._scoped.get(scope)

    def chain_layers(self, scope: Any | None) -> list:
        """沿作用域父链的既有层,最远祖先在前、精确作用域最后。

        按序叠放时最近的层有最后发言权;不存在的层跳过。
        """
        layers: list = []
        for key in reversed(scope_chain_of(scope)):
            layer = self._scoped.get(key)
            if layer is not None:
                layers.append(layer)
        return layers

    def merge(self, scope: Any | None, pick: Callable[[ScopeLayer], NamedEntries]) -> dict:
        """物化全局具名表 + 沿链各层影子:最远祖先在前,同名取最近。"""
        merged = dict(pick(self.global_).entries())
        for layer in self.chain_layers(scope):
            for name, value in pick(layer).entries():
                merged[name] = value
        return merged

    def effect(
        self,
        ctx,
        action: Callable[[ScopeLayer], Callable[[], None]],
        label: str = "anonymous",
        notify: bool = True,
    ) -> Callable:
        """把一次同步的表变更挂到它的注册 ctx 上(可见性 + 所有权)。

        用生成器 effect:首个 yield 前完成「选层 + 执行动作 + 收集撤销」,
        yield 产出的撤销函数由 fiber 逆序收集(卸载/注销时执行)。

        TS 的 options 对象在此拆成 label 与 notify 两个具名参数。
        """
        scope = scope_of(ctx)

        def run():
            layer: ScopeLayer
            created = False
            if scope is None:
                layer = self.global_
            else:
                existing = self._scoped.get(scope)
                if existing is None:
                    layer = self._create_layer(scope)
                    self._scoped[scope] = layer
                    created = True
                else:
                    layer = existing

            try:
                undo = action(layer)
            except BaseException:
                # 注册失败:回收刚建的空层(彻底空的层不保留)
                if scope is not None and created and layer.is_empty():
                    del self._scoped[scope]
                raise

            def undo_registration() -> None:
                undo()
                if scope is not None and layer.is_empty():
                    del self._scoped[scope]
                if notify:
                    self._on_change()

            yield undo_registration
            if notify:
                self._on_change()

        return ctx.effect(run, label)
