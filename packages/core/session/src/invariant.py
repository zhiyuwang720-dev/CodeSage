"""会话事件日志的关系不变式:seq 连续、turn/step 嵌套、调用配对。

append-only 日志是唯一事实源,但"任何字节都合法"不等于"任何日志
都能重建正确会话"。本模块定义日志的**关系契约**:事件之间必须
保持的结构关系 ——

- seq 严格递增(seq = log.length 连续性契约,整个系统校验/恢复/
  水位的地基);
- turn/step 括号嵌套(turn/start … turn/end,内部再套 step/start …
  step/end);
- tool/call 与 tool/result 配对(先调后果,同一步内)。

validate_event 是纯函数:校验一个候选事件,不改动已提交的 trace,
返回这个事件将对 trace 施加的**延迟转移**(transition)。先校验后
提交,失败不可能部分改写状态 —— 这是日志接纳的原子性来源。

DSH 中这些校验经 cordis 的 invariants companion 双保险注册
(内部派发预校验 + 提交推进);Python 版将同一套纯函数内置进
Session.append 的接纳边界 —— 语义一字不差,装配点从插件钩子
变为构造器的内部防线。校验失败即抛错(DSH 的 InvariantFailure
可注入以记录,这里直接以异常终结非法事件)。
"""

from __future__ import annotations

from collections.abc import Callable

__all__ = [
    "SessionTrace",
    "SessionTraceTransition",
    "apply_transition",
    "fresh_trace",
    "seed_trace",
    "validate_event",
]


class SessionTrace:
    """每个会话的关系簿记:校验时只读、提交时经转移更新。

    字段含义(照 DSH):
    - last_seq:已提交的最大 seq,校验严格递增用(空日志为 -1);
    - open_turn / open_step:当前打开的 turn/step 号(None 表示关闭);
    - next_turn / next_step:下一个 turn/start、step/start 应当携带
      的编号 —— 括号由事件自己编号,契约要求编号按序递增;
    - pending_calls:当前 step 中已调用尚未收到结果的 tool/call id
      集合(step/end 时整体清空)。
    """

    __slots__ = ("last_seq", "open_turn", "open_step", "next_turn", "next_step", "pending_calls")

    def __init__(
        self,
        last_seq: int = -1,
        open_turn: int | None = None,
        open_step: int | None = None,
        next_turn: int = 1,
        next_step: int = 1,
    ) -> None:
        self.last_seq = last_seq
        self.open_turn = open_turn
        self.open_step = open_step
        self.next_turn = next_turn
        self.next_step = next_step
        self.pending_calls: set[str] = set()

    def copy(self) -> "SessionTrace":
        clone = SessionTrace(self.last_seq, self.open_turn, self.open_step, self.next_turn, self.next_step)
        clone.pending_calls = set(self.pending_calls)
        return clone


class SessionTraceTransition:
    """一个已接受事件对已提交 trace 的延迟转移。

    scalars 是五标量(seq/turn/step 指针)的下一状态;
    pending_calls 描述调用集合的定向变化:
    - none:不变;
    - ("add", call_id):登记一个未决调用;
    - ("delete", call_id):销掉一个已配对调用;
    - "clear":整体清空(step/end 边界)。
    """

    __slots__ = ("scalars", "pending_calls")

    def __init__(self, scalars: tuple, pending_calls) -> None:
        self.scalars = scalars
        self.pending_calls = pending_calls


def fresh_trace() -> SessionTrace:
    """一条空日志的初始 trace。"""
    return SessionTrace()


def require_open_step(trace: SessionTrace, kind: str, turn: int, step: int) -> None:
    """断言一个步内事件点名的是当前打开的 turn 与 step。"""
    if trace.open_turn != turn or trace.open_step != step:
        raise ValueError(
            f"{kind} names turn {turn}/step {step} but open is "
            f"turn {trace.open_turn}/step {trace.open_step}"
        )


def validate_event(trace: SessionTrace, event: dict) -> SessionTraceTransition:
    """校验一个候选事件(不改动 trace),返回它将施加的转移。

    事件是已通过信封检查的普通 dict(见 index 的接纳边界):
    {type, seq, time, data, surfaceOp?, sourceEventSeqs?, ignorable?}。
    校验失败抛 ValueError。
    """
    seq: int = event["seq"]
    if seq <= trace.last_seq:
        raise ValueError(f"seq must strictly increase: saw {seq} after {trace.last_seq}")
    open_turn = trace.open_turn
    open_step = trace.open_step
    next_turn = trace.next_turn
    next_step = trace.next_step
    pending_calls = None  # none

    event_type: str = event["type"]
    data: dict = event["data"]

    if event_type == "turn/start":
        # 打开新 turn:前一个必须已关闭,编号必须接续。
        if trace.open_turn is not None:
            raise ValueError(f"turn/start {data['turn']} while turn {trace.open_turn} is still open")
        if data["turn"] != trace.next_turn:
            raise ValueError(f"turn/start expected turn {trace.next_turn}, got {data['turn']}")
        open_turn = data["turn"]
        next_step = 1
    elif event_type == "turn/end":
        # 关闭 turn:必须配对当前打开 turn,且其内不能有打开的 step。
        if trace.open_turn != data["turn"]:
            raise ValueError(f"turn/end {data['turn']} does not match open turn {trace.open_turn}")
        if trace.open_step is not None:
            raise ValueError(f"turn/end {data['turn']} while step {trace.open_step} is still open")
        open_turn = None
        next_turn += 1
    elif event_type == "step/start":
        # 打开 step:turn 必须打开,step 编号必须接续,不能重入。
        if trace.open_turn != data["turn"]:
            raise ValueError(f"step/start in turn {data['turn']} but open turn is {trace.open_turn}")
        if trace.open_step is not None:
            raise ValueError(f"step/start {data['step']} while step {trace.open_step} is still open")
        if data["step"] != trace.next_step:
            raise ValueError(
                f"step/start expected step {trace.next_step} in turn {data['turn']}, got {data['step']}"
            )
        open_step = data["step"]
    elif event_type == "step/end":
        require_open_step(trace, "step/end", data["turn"], data["step"])
        pending_calls = "clear"
        open_step = None
        next_step += 1
    elif event_type == "assistant/chunk":
        require_open_step(trace, "assistant/chunk", data["turn"], data["step"])
    elif event_type == "assistant/message":
        require_open_step(trace, "assistant/message", data["turn"], data["step"])
    elif event_type == "tool/call":
        require_open_step(trace, "tool/call", data["turn"], data["step"])
        pending_calls = ("add", data["callId"])
    elif event_type == "tool/result":
        # 表面替换(compact 重写)是持久 turn 工作,不是对原调用的
        # 二次执行 —— 只要求它发生在某个打开的 turn 内。
        if event.get("surfaceOp") != "append":
            if trace.open_turn is None:
                raise ValueError("tool/result surface replacement appended outside any open turn")
        else:
            require_open_step(trace, "tool/result", data["turn"], data["step"])
            call_id = data["message"]["source"]["callId"]
            # 崩溃恢复会合成 TOOL_NOT_STARTED 结果:那次调用从未
            # 被记录为 tool/call,免除配对要求。
            content0 = data["message"]["content"][0]
            error = data.get("error")
            synthetic_not_started = (
                content0.get("isError") is True
                and error is not None
                and error.get("code") == "TOOL_NOT_STARTED"
            )
            if call_id not in trace.pending_calls and not synthetic_not_started:
                raise ValueError(f"tool/result for {call_id} with no prior tool/call in this step")
            pending_calls = ("delete", call_id)
    elif event_type == "user/message":
        # 用户消息不受括号约束:任何位置都合法(合成注入可发生在
        # turn 外)。
        pass
    elif event_type == "session/end-seed":
        # 不受约束:非平衡的种子合法地把它放在打开的 turn 内。
        pass
    elif event_type in ("todo/write", "request/header", "request/context"):
        # 上下文与插件所有的纯日志事件可在模型执行之间追加,但核心
        # 执行事件必须包在 turn 内。
        if trace.open_turn is None:
            raise ValueError(
                f"{event_type} appended outside any open turn (core execution events must be turn-enclosed)"
            )
    # 其余事件类型(词表扩展):关系约束归拥有它们的插件,核心不
    # 设限 —— 合并可扩展的 and 类型,这里不 assertNever。

    return SessionTraceTransition(
        (seq, open_turn, open_step, next_turn, next_step),
        pending_calls,
    )


def apply_transition(trace: SessionTrace, transition: SessionTraceTransition) -> None:
    """在事件提交后应用它已校验的转移。"""
    trace.last_seq, trace.open_turn, trace.open_step, trace.next_turn, trace.next_step = transition.scalars
    pc = transition.pending_calls
    if pc is None:
        return
    if pc == "clear":
        trace.pending_calls.clear()
    elif pc[0] == "add":
        trace.pending_calls.add(pc[1])
    else:
        trace.pending_calls.discard(pc[1])


def seed_trace(events) -> SessionTrace:
    """从一段既有日志全量重放,构建其已提交 trace。

    种子/恢复路径(构造、重载、fork 种子)用同一套校验走一遍,
    得到与逐条 append 完全相同的簿记状态。
    """
    trace = fresh_trace()
    for event in events:
        apply_transition(trace, validate_event(trace, event))
    return trace
