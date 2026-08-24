"""中断会话日志的崩溃恢复修复。

崩溃会留下一个完全写好的最终 turn 尾巴,但缺少恢复一个
provider 合法逐字稿所需的工具、step、turn 边界。本模块供给
确定性的合成事件把开放尾巴关掉。

确定性是修复的底线:seq 顺延最后一个真实事件、时间戳复用它的
时间 —— 永不发明「未来」的时间,重放结果可复现。合成事件走
invariant 的豁免通道(tool/result 的 TOOL_NOT_STARTED 免除配对),
所以修复产物可以安全地追加回日志。
"""

from __future__ import annotations

__all__ = ["TOOL_NOT_STARTED", "TOOL_OUTCOME_UNKNOWN", "interrupted_turn_closers"]

#: 恢复码:assistant 的工具请求从未到达被记录的调用起点。
TOOL_NOT_STARTED = "TOOL_NOT_STARTED"

#: 恢复码:已记录的工具调用,其完成结果未被持久化记录。
TOOL_OUTCOME_UNKNOWN = "TOOL_OUTCOME_UNKNOWN"


def interrupted_turn_closers(events: list[dict]) -> list[dict]:
    """返回关闭开放尾巴 turn 的确定性合成事件,按序追加。

    未配对的调用先收到错误结果,随后是开放的 step/end 与
    interrupted 的 turn/end;seq 顺延日志、时间戳复用最后一个
    真实事件。日志已平衡(或为空)时返回空表。

    合成消息文案是模型可见的用户侧提示,保留英文原文。
    """
    open_turn = None
    open_step = None
    # 每个 turn 边界重置,使更早的调用不会漏进尾巴修复。
    # assistant 消息注册调用;后续 tool/call 事件把自己的 seq
    # 补进 sourceEventSeqs。
    pending_calls: dict[str, dict] = {}
    for event in events:
        event_type = event["type"]
        if event_type == "turn/start":
            open_turn = event["data"]["turn"]
            open_step = None
            pending_calls.clear()
        elif event_type == "turn/end":
            open_turn = None
            open_step = None
            pending_calls.clear()
        elif event_type == "step/start":
            open_step = event["data"]["step"]
        elif event_type == "step/end":
            pending_calls.clear()
            open_step = None
        elif event_type == "assistant/message":
            # assistant 消息携带工具调用块;每个调用挂起,直到同
            # callId 的 tool/result 入账。
            for block in event["data"]["message"]["content"]:
                if block["type"] == "tool-call":
                    pending_calls[block["id"]] = {"step": event["data"]["step"]}
        elif event_type == "tool/call":
            # 让合成结果引用 tool/call 的 seq。
            entry = pending_calls.get(event["data"]["callId"])
            if entry is not None:
                entry["callSeq"] = event["seq"]
        elif event_type == "tool/result":
            pending_calls.pop(event["data"]["message"]["source"]["callId"], None)
        # 其他事件类型不移动 turn/step 边界游标。

    # 平衡日志(无崩溃中间 turn):没有要关的。开放的 turn 意味着
    # events 非空(它的 turn/start 已入账),所以 last 必然存在。
    last = events[-1] if events else None
    if open_turn is None or last is None:
        return []

    # 最后一个真实事件提供 seq 基数与合成事件的时戳。
    seq = last["seq"] + 1
    time = last["time"]
    closers: list[dict] = []

    # 先关调用再关 step:provider 拒绝悬挂的 assistant 调用;
    # dict 插入顺序保留其逐字稿顺序。
    for call_id, entry in pending_calls.items():
        started = "callSeq" in entry
        message = {
            "id": f"interrupted-tool-result-{call_id}-{seq}",
            "role": "user",
            "source": {"kind": "tool", "callId": call_id},
            "content": [{
                "type": "tool-result",
                "toolCallId": call_id,
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": (
                        "The tool call was interrupted after it was recorded, but no result was durably recorded. "
                        "Its outcome is unknown. Decide whether to retry from the tool semantics: retry only if the "
                        "operation is read-only or idempotent; if it may have side effects, first verify external state "
                        "or ask the user. Do not retry blindly."
                        if started else
                        "The tool call was interrupted before the Harness recorded it as started. Retry it if it is still needed."
                    ),
                }],
            }],
        }
        closer = {
            "type": "tool/result",
            "seq": seq,
            "time": time,
            "data": {
                "turn": open_turn,
                "step": entry["step"],
                "message": message,
                "error": (
                    {"name": "ToolOutcomeUnknownError", "code": TOOL_OUTCOME_UNKNOWN}
                    if started else
                    {"name": "ToolNotStartedError", "code": TOOL_NOT_STARTED}
                ),
            },
            "surfaceOp": "append",
        }
        seq += 1
        if started:
            closer["sourceEventSeqs"] = [entry["callSeq"]]
        closers.append(closer)

    # 再关开放的 step —— turn/end 时 step 还开着是不变式违规,所以
    # step 边界必须先于 turn 边界合成。
    if open_step is not None:
        closers.append({
            "type": "step/end",
            "seq": seq,
            "time": time,
            "data": {"turn": open_turn, "step": open_step},
        })
        seq += 1
    closers.append({
        "type": "turn/end",
        "seq": seq,
        "time": time,
        "data": {"turn": open_turn, "reason": {"kind": "interrupted"}},
    })
    return closers
