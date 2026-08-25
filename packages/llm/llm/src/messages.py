"""消息值类型与不可变构造器:消息一出生就钉死角色与身份,不可再改。

会话事件里嵌套的消息是**普通 dict 形状**(``{id, role, content,
source}``)—— Python 版没有 branded 类型,消息契约就是形状契约。
本模块提供构造器族:调用方给出内容与来源,构造器补上稳定身份
与角色标签,产出标准形状。

**冻结语义**:构造器返回的 dict 尚未冻结 —— 冻结发生在 session
边界(Session.append 的快照器把入日志值深度冻结)。构造器不管
冻结,因为同一份构造结果可能走不同接纳路径,冻结是接纳边界的
职责。

**source 形状**(来源:谁产生了这条消息):
- ``{kind: 'user'}`` — 人
- ``{kind: 'plugin', plugin: 名, form?}`` — 插件注入;form 是
  语义分类(instructions/catalog/snapshot/notice/relay/recall),
  绝不描述外观(颜色/图标/排序是消费者的业务)
- ``{kind: 'model', provider, model, replayState?}`` — 模型产出
- ``{kind: 'tool', callId}`` — 工具结果
"""

from __future__ import annotations

import uuid

__all__ = [
    "CONTEXT_SUMMARY_MAX_CHARS",
    "MessageId",
    "bound_context_summary",
    "create_assistant_message",
    "create_message",
    "create_tool_result_message",
    "create_user_message",
    "is_token_delta",
]


def MessageId() -> str:  # noqa: N802 -- 构造函数式命名(工厂即调用)
    """铸造一条消息的稳定身份(uuid hex)。

    消息身份是编译期标记类型,构造即 ``crypto.randomUUID()``;
    Python 保持同一语义:构造返回唯一 hex 串,后续所有表示边界
    (会话事件、模型请求、UI)共享同一身份。
    """
    return uuid.uuid4().hex


#: notice 摘要上限:摘要跟随折叠的逐字稿行、并提交进耐久日志,
#: 而它的输入(任务标签、目标、工具参数)是调用方文本,没有自己的
#: 长度。超限截断,不是抛错。
CONTEXT_SUMMARY_MAX_CHARS = 120


def bound_context_summary(summary: str) -> str:
    """把一条 notice 摘要约束到 CONTEXT_SUMMARY_MAX_CHARS 内。

    超出时在边界截断并加省略号(截断到 119 字符 + 1 个省略号)。
    """
    if len(summary) <= CONTEXT_SUMMARY_MAX_CHARS:
        return summary
    return f"{summary[:CONTEXT_SUMMARY_MAX_CHARS - 1]}…"


def create_message(input_: dict) -> dict:
    """创建一条带新身份的完整消息并返回(未冻结)。

    input_ 提供 role/content/source,身份在此铸造 —— 调用方永远
    不需要也不应该自己指定 id。
    """
    return {
        **input_,
        "id": MessageId(),
    }


def create_user_message(input_: dict) -> dict:
    """创建一条 user 角色消息:固定 role 标签 + 新身份。"""
    return create_message({**input_, "role": "user"})


def create_assistant_message(input_: dict) -> dict:
    """创建一条 assistant 角色消息:固定 role 与 model source 标签。

    input_ 提供 content 与 ``source: {provider, model, replayState?}``;
    kind 字段由本构造器钉死为 ``'model'``,调用方给什么都不会覆盖。
    """
    source = dict(input_.get("source", {}))
    source["kind"] = "model"
    return create_message({
        "role": "assistant",
        "content": input_.get("content", []),
        "source": source,
    })


def create_tool_result_message(call_id: str, content: list, is_error: bool) -> dict:
    """创建一条工具结果消息:user 角色、单一 tool-result 块。

    ``toolCallId`` 与调用方的 call_id 关联 —— 模型侧靠它把结果
    对应回工具调用。is_error 声明执行失败(结果块携带错误标记)。
    """
    return create_user_message({
        "source": {"kind": "tool", "callId": call_id},
        "content": [{
            "type": "tool-result",
            "toolCallId": call_id,
            "content": content,
            "isError": is_error,
        }],
    })


def is_token_delta(chunk: dict) -> bool:
    """流块是否携带可见模型输出(首个 token 边界)。

    客户端步骤计时与会话统计共用这个判定;空增量(心跳、空的
    工具调用帧)不算首个 token。
    """
    chunk_type = chunk.get("type")
    if chunk_type in ("text-delta", "reasoning-delta"):
        return chunk.get("text", "") != ""
    if chunk_type == "tool-call-delta":
        return chunk.get("argumentsDelta", "") != "" or chunk.get("name") is not None
    return False
