"""tools 持久事件词表(参考实现 tools/types.ts 实现)。

Code Mode 的 ``run_code`` 桥把嵌套子调度记成两条持久事件:
``tool/code-dispatch-start``(调度真正开始时)与
``tool/code-dispatch``(子调用结算时),按 ``subCallId`` 配对。
两条都是 log-only 事件:``deriveMessages()`` 忽略它们,子调用
永不回流到模型上下文;持久化与 UI 却拿到每一次调用。

参考实现 在 session 的 SessionEventMap 上做声明合并注册;Python 侧
落点在 session 包的 ``extend_event_types`` 注册表。session 包
实现时已把这两个事件列入词表(与 参考实现 session/types.ts 相同的
合并结果),这里的注册是幂等 no-op —— 保留模块结构与 参考实现 对齐。
"""

from __future__ import annotations

from typing import TypedDict

#: 声明合并的 Python 落点:注册进 session 事件词表(幂等 no-op,
#: session 包已列入;保留调用以对齐 参考实现 types.ts 的注册结构)。
from core.session import extend_event_types  # noqa: E402

extend_event_types("tool/code-dispatch-start", "tool/code-dispatch")

__all__ = [
    "CodeDispatchEventData",
    "CodeDispatchStartEventData",
]

#: call id 在 参考实现 是 llm/brand 的透明牌子类型,运行时即字符串。
#: 这里不另立类型,契约上以 str 承交。
CallId = str


class CodeDispatchStartEventData(TypedDict):
    """一次 Code Mode 嵌套子调度开始时的持久载荷。

    arguments 是派发前已 JSON 归一化的值 —— 先归一化再 append,
    这条事件永远不会因载荷形状而失败。
    """

    rootCallId: CallId
    parentCallId: CallId
    subCallId: CallId
    name: str
    arguments: object


class CodeDispatchEventData(CodeDispatchStartEventData):
    """一次嵌套子调度结算时的持久载荷。

    content + isError 沿用 ``tool/result`` 自己的词汇 —— UI 渲染
    子调用走的正是渲染原生调用的那套代码路径;abort 也结算为
    isError 结果,开始过的子调用必然恰好结算一次。
    """

    isError: bool
    content: list
