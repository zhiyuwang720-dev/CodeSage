"""会话的类型词汇:事件形状、会话元数据、表面契约。

DSH 的 types.ts 以 TypeScript 类型系统刻画整个会话模型 —— 判别式
联合的事件、合并可扩展的事件表、branded 的会话 id。Python 没有
编译期类型,本模块承载同一份契约的运行时形状:

- 常量:格式版本、生命周期状态、表面事件集合;
- 事件词表:每种事件的 data 形状(文档化契约,运行校验在
  invariant.py 与 index.py 的接纳边界);
- 纯辅助:事件形状断言、id 铸造。

**为什么数据校验不集中在这里**:DSH 的事件数据没有 zod 运行时
schema —— append 边界只做无损 JSON 校验(可序列化),关系校验
(seq 连续、turn/step 嵌套)在 invariant,消息形状校验在 seed/load
边界(index 的 assert 族)。Python 保持同一分工:本模块定形状,
各边界做各自该做的检查。
"""

from __future__ import annotations

__all__ = [
    "SESSION_FORMAT_VERSION",
    "SessionId",
    "SURFACE_EVENT_TYPES",
    "TODO_STATUSES",
    "TURN_END_REASON_KINDS",
    "REQUEST_HEADER_REASONS",
    "is_surface_eligible_type",
]

#: 磁盘上的会话格式版本,写入每个新建的 SessionHeader,读路径
#: 强制校验。单一事实源:写点与加载检查都读它。harness 未发布,
#: 钉在 0 —— 不承诺兼容,不兼容的日志直接拒绝,不提供迁移。
#:
#: 版本是单个单调整数,无主次之分。何时需要升版由「写方发出的
#: 内容」决定,不由「新读者能接受什么」决定:旧运行时无法在完整
#: 语义正确性下处理新日志时就必须升版("能解析"不等于"正确" ——
#: 静默跳过塑造重建的内容就是错读)。只有结构性变更够格:header
#: 形状、事件信封、核心事件语义、表面机制。新增普通事件类型不
#: 升版 —— 每事件的 ignorable 守卫覆盖词表增长。拿不准就升:近
#: 似的升级步骤几乎免费,漏升则让旧运行时静默错读新日志。
SESSION_FORMAT_VERSION = 0


def SessionId(id: str) -> str:  # noqa: N802 -- 对齐 DSH 的构造函数命名
    """把一个字符串铸造为会话 id。

    保留此函数是为了让"铸造"这个语义动作有落点,后续若引入 id 校验或类型系统可在此收口。
    """
    return id


#: 表面事件类型:产生 LLM 消息、可出现在有序表面上的三类事件。
#: 只有这些事件类型可以携带 surfaceOp 与 sourceEventSeqs。
SURFACE_EVENT_TYPES: frozenset[str] = frozenset({
    "user/message",
    "assistant/message",
    "tool/result",
})

#: todo 条目的三种生命周期状态。
TODO_STATUSES = ("pending", "in_progress", "completed")

#: turn/end 的原因集合(合并可扩展的和类型,核心六种)。
TURN_END_REASON_KINDS = (
    "completed",    # 正常完成
    "aborted",      # 取消请求打断了活跃 turn(携带取消原因)
    "blocked",      # 阻塞
    "error",        # turn 失败,error 恒为结构化失败
    "max-tokens",   # 至少一步触及输出 token 上限
    "interrupted",  # 持久化后端在重载时关闭了崩溃孤儿 turn(循环永不发出)
)

#: request/header 快照的三种落账理由。
REQUEST_HEADER_REASONS = ("initial", "resume", "change")


def is_surface_eligible_type(type_: str) -> bool:
    """一个事件类型能否加入模型可见的表面。"""
    return type_ in SURFACE_EVENT_TYPES
