"""scope 包:给「贡献」贴标签,让每个 Agent 只看到自己的。

一个组合里通常跑着多个 agent(主 agent + 子 agent)。每个 agent 都会
贡献工具、提示词段、变量、监听器。没有作用域时所有贡献混在一个大
锅里互相干扰;作用域给贡献打上属主标签,让贡献只对属主及其祖先可见,
事件沿作用域链向上流(祖先可观察子孙,子孙看不见祖先)。

三个核心概念(详见 index.py):
- 作用域 = 一个带标签的 ctx:铸造时盖戳,ctx 派生自动继承,任何在其下
  注册的东西自动携带标签,贡献者无需额外动作;
- 作用域链 = 单向父指针:绑定一次(唯一重绑通道在原绑定者手里),
  事件沿链向上流、注册视图沿链向下继承(就近遮蔽);
- 路由载体 = 事件上的路由牌:派发机制按牌筛选监听器。

边界:scope 不是权限机制 —— 只管「看得见/看不见」,不管「能不能用」,
与 CodeSage 的权限链(deny > ask > allow)正交。


  index.py        三概念:铸造/路由牌/绑定/链
  store.py        每作用域一张表 + 插入序表
  scoped_events_generated.py 作用域事件主题表
  invariant.py    派发检查岗(依赖 invariants 服务)

依赖 cordis 内核(在cordis-py 仓库)。
"""

from .index import (
    Scope,
    ScopeParentBinding,
    bind_scope_parent,
    carrier_key_of,
    create_scope,
    is_scope_carrier,
    scope_chain_of,
    scope_of,
    scope_parent_of,
    scope_target,
)
from .store import AnonymousEntries, NamedEntries, ScopeLayer, ScopedLayers

__all__ = [
    "AnonymousEntries",
    "NamedEntries",
    "Scope",
    "ScopeLayer",
    "ScopeParentBinding",
    "ScopedLayers",
    "bind_scope_parent",
    "carrier_key_of",
    "create_scope",
    "is_scope_carrier",
    "scope_chain_of",
    "scope_of",
    "scope_parent_of",
    "scope_target",
]
