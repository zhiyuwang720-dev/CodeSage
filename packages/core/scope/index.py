"""scope 包核心:作用域原语 —— 给「贡献」贴标签,让每个 Agent 只看到自己的。

这个模块解决组合里多 agent 的贡献隔离问题:每个 agent 都会注册工具、
追加提示词段、声明变量、挂监听器。没有作用域时所有贡献混在一起互相
可见互相干扰;作用域给贡献打上属主标签,贡献只对属主及其祖先可见。

三个概念贯穿全文:
- 作用域(Scope):一个带标签的 ctx。铸造作用域时在 ctx 上盖一个戳
  (标签 → 身份对象),ctx 派生子 ctx 时戳自动继承(extend 拷贝),
  所以任何在作用域 ctx 下注册的东西自动携带这个戳,贡献者无需额外动作。
- 作用域链(scope parents):作用域父子关系的父指针,单向向上。事件沿
  链向上流:祖先的监听器能收到子孙作用域的事件,子孙收不到祖先的。
  类比公司广播:总部广播全员听得到,部门广播只在本部门及其下属内。
- 路由载体(carrier):作用域里派发事件时,事件对象上挂一张「路由牌」
  (一个 filter 函数),cordis 派发机制按牌筛选监听器。

与 TS 原版(@deepseek-ai/dsh-scope/src/index.ts)逐文件 1:1 对应,映射差异:
- 标签键:Symbol('dsh.scope') → 字符串(只能经 __dict__ 存取,不经属性
  语法 —— 双下划线键会触发 name mangling,下划线前缀会命中特殊属性拦截)
- 弱引用表:WeakMap → weakref.WeakKeyDictionary(key 不可弱引用即抛错,
  与 TS 的 WeakMap 对象约束一致)
- filter 符号:CordisContext.filter → service.FILTER 字符串属性
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable
from weakref import WeakKeyDictionary

from cordis.context import _MISSING, _get_real
from cordis.service import FILTER

if TYPE_CHECKING:
    from cordis import Context
    from cordis.fiber import Fiber

#: 作用域标签的键(TS 的 Symbol('dsh.scope') 的 Python 等价物)。
#: 只能经 __dict__ 存取,不能经属性语法访问。
K_SCOPE = "__dsh_scope__"

#: 每个载体的路由键:键在册 = 是载体;不在册 = 不是(TS 的 carrierKeys)。
#: 用弱引用表,载体不存活后自然消失。
_carrier_keys: WeakKeyDictionary = WeakKeyDictionary()

#: 每个作用域键的父指针,单向向上(TS 的 scopeParents)。一个关系支撑
#: 两个方向:注册视图沿链向下继承(子作用域看到祖先的层),事件放行沿
#: 链向上延伸(带祖先标签的监听器收到派发给子孙键的事件)。
_scope_parents: WeakKeyDictionary = WeakKeyDictionary()


def _link_scope_parent(key: Any, parent: Any) -> None:
    """链的唯一写入口,绑定与每次重绑共用:先做环检测,再落父指针。

    每个链消费者都会沿父链走到根,所以闭环的链是非法结构,必须拒绝;
    检测通过后写入 —— 检测与写入在同一步完成,不存在只检不写的中间态。
    """
    cursor = parent
    while cursor is not None:
        if cursor is key:
            raise ValueError("dsh-scope: scope parent link would form a cycle")
        cursor = _scope_parents.get(cursor)
    _scope_parents[key] = parent


class ScopeParentBinding:
    """特权句柄:唯一能移动一个作用域键父链接的凭证。

    重绑只在「旧父作用域下产出的东西都不再被保留」时才合法(空白会话
    重组契约,由持有者自行保证 —— 因为这条关系看不到会话日志里记了
    什么,所以约束只能落在持有者头上)。
    """

    def __init__(self, key: Any) -> None:
        self._key = key

    def rebind(self, parent: Any) -> None:
        """把绑定的键重链到不同的父作用域,环检测与绑定一致。"""
        _link_scope_parent(self._key, parent)


def bind_scope_parent(key: Any, parent: Any) -> ScopeParentBinding:
    """把 parent 绑定为 key 的外层作用域,只允许一次。

    已绑定的键再绑抛错:没有开放的改链通道,一个作用域的祖先只能由
    原绑定者移动。会成环的绑定被拒绝。
    """
    if key in _scope_parents:
        raise ValueError(
            "dsh-scope: scope key is already bound to a parent; "
            "re-linking requires the binding returned by the original bind"
        )
    _link_scope_parent(key, parent)
    return ScopeParentBinding(key)


def scope_parent_of(key: Any) -> Any | None:
    """读一个作用域键的外层作用域;根作用域返回 None。"""
    return _scope_parents.get(key)


def scope_chain_of(key: Any) -> list:
    """从 key 到根祖先的链,最近者在前:[key, parent, grandparent, …]。"""
    chain: list = []
    cursor = key
    while cursor is not None:
        chain.append(cursor)
        cursor = _scope_parents.get(cursor)
    return chain


def _scope_noop(ctx: "Context", config: Any = None) -> None:
    """无操作插件:仅作为作用域 fiber 的载体(TS 同款)。"""


async def _quiesce_fiber(fiber: "Fiber") -> None:
    """跟着 fiber 走完异步卸载:即使原始 disposer 已被别人领走,也追到
    彻底停稳(防幽灵回调 —— 销毁后绝无残留的异步行为)。"""
    await fiber.dispose()
    while fiber.inertia is not None:
        await fiber.inertia


class Scope:
    """一个已铸造的作用域:注册入口 ctx + 两层销毁边界。

    - ctx:作用域属主的注册入口,所有作用域贡献经它注册;
    - raw_dispose:fiber 的精确 disposer,嵌套进组合 effect 时用
      (精确身份有承重作用,组合 effect 靠它按序 teardown);
    - dispose:销毁全部作用域贡献;并发调用共享同一个完成。
    """

    def __init__(self, ctx: "Context", raw_dispose: Callable, fiber: "Fiber") -> None:
        self.ctx = ctx
        self.raw_dispose = raw_dispose
        self._fiber = fiber
        self._disposing: asyncio.Task | None = None

    def dispose(self) -> asyncio.Task:
        """销毁作用域并等停稳;重复调用 await 同一个完成。

        需要运行中的事件循环(cordis 异步 API 的通用前提)。
        """
        if self._disposing is None:
            self._disposing = asyncio.create_task(_quiesce_fiber(self._fiber))
        return self._disposing


def create_scope(ctx: "Context", key: Any, options: dict | None = None) -> Scope:
    """在 ctx 下铸造一个作用域:ctx 上盖标签,派生 ctx 自动继承。

    作用域 ctx 继承铸造方插件的依赖 API,拥有经由它产生的全部注册。
    可选的 parent 参数把新键放进作用域链(绑定仅此一次,由内部持有)。
    """
    if options is not None and options.get("parent") is not None:
        bind_scope_parent(key, options["parent"])
    fiber = ctx.plugin(_scope_noop)
    scoped = fiber.ctx.extend({K_SCOPE: key})
    return Scope(scoped, fiber.dispose, fiber)


def scope_of(ctx: "Context") -> Any | None:
    """读 ctx 继承到的最近作用域标签;无标签 ctx 返回 None。

    先查 own 表(extend 拷贝使子 ctx 自带标签),再沿 _fallback 链
    (loader 的 ctx re-point 场景 —— TS 的 [[Get]] 沿原型链语义)。
    """
    tag = ctx.__dict__.get(K_SCOPE)
    if tag is not None:
        return tag
    real = _get_real(ctx, K_SCOPE)
    return None if real is _MISSING else real


class _Carrier:
    """路由牌:无属性的普通对象,仅携带 filter 函数(派发机制按它筛选)。

    filter 经 service.FILTER 属性挂载;派发时 cordis 的 dispatch 会读取
    它并对每个监听者的 ctx 求值。
    """


def scope_target(base: Any, key: Any) -> Any:
    """造一张路由牌:保留 base 的既有 filter,按 key 的父链放行监听器。

    放行规则:
    - 无标签 ctx 的监听器(组合级常驻):永远放行,组合可以观察全局;
    - 带标签的监听器:标签命中 key 自身或 key 的任意祖先 → 放行
      (祖先作用域观察它下辖的所有子孙);
    - 标签在 key 之下:不放行 —— 事件沿链向上流,永不向下流。
    """
    base_filter = getattr(base, FILTER, None)

    def filter_fn(ctx: "Context") -> bool:
        if base_filter is not None and not base_filter(ctx):
            return False
        tag = scope_of(ctx)
        if tag is None:
            return True
        cursor = key
        while cursor is not None:
            if cursor == tag:
                return True
            cursor = _scope_parents.get(cursor)
        return False

    carrier = _Carrier()
    setattr(carrier, FILTER, filter_fn)
    _carrier_keys[carrier] = key
    return carrier


def is_scope_carrier(value: Any) -> bool:
    """判断一个值是否由 scope_target 铸造的路由牌(在册即真)。"""
    return isinstance(value, _Carrier) and value in _carrier_keys


def carrier_key_of(value: Any) -> Any | None:
    """读载体的路由键;非载体或无键载体返回 None。"""
    if not is_scope_carrier(value):
        return None
    return _carrier_keys[value]
