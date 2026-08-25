"""一步 assistant 工具调用的调度:排他屏障与有界并行池。

模型一次可能产出多个工具调用,但它们不是同质的:有的声明可
并行(dispatch 重叠),有的声明排他(独占并形成排序屏障)。本
模块按活声明的并发模式把调用分群 —— 每群一个 barrier(排他)
或一个滚动池(并行,池上限 = 每 agent 的 maxParallelToolCalls,
类比操作系统的线程池:并发有界,最坏情况可预期)。

不变式:派发可以重叠,但**策略、结果、结果上下文保持模型序**
(committed 只跨连续模型序槽推进,结果按模型给出顺序入日志);
中止停止补员、排干已开始调用、给未开始调用记录合成错误结果
(重放保持有效);调度器内部失败则保留已记录的 tool/call 事件,
不伪造结果 —— 类比文件系统的日志回滚:宁可留下明确的空洞,
也不写没有依据的记录。
"""

from __future__ import annotations

import asyncio
import json

from llm.llm.src.messages import create_tool_result_message

from core.tools.src.index import (
    TOOL_ABORTED_BEFORE_DISPATCH,
    TOOL_RUNTIME_SCHEDULER,
)

from .constants import DEFAULT_MAX_PARALLEL_TOOL_CALLS

__all__ = ["execute_tool_calls"]


def parse_arguments(raw: str):
    """解析模型给出的参数:无效 JSON 保留原文,空输入映射 ``{}``。

    保留原文而不是抛错:模型偶发的残缺 JSON 是「可见的输入」,
    工具自己决定如何解释;解析失败的信息不能被解析器吞掉。
    """
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return raw


def append_tool_call(session, turn: int, step: int, block: dict) -> int:
    """记录一次已开始的调用,返回结果必须引用的事件 seq。"""
    event = session.append("tool/call", {
        "turn": turn,
        "step": step,
        "callId": block["id"],
        "name": block["name"],
        "arguments": block.get("arguments", ""),
    })
    return event["seq"]


def append_tool_result(session, turn: int, step: int, block: dict, result: dict,
                       call_seq: int) -> None:
    """按模型序追加结果,链接到它的调用事件。

    工具的私有展示载荷(meta,如结果时差异)一并持久化 —— UI 桥
    重放时复刻同一张卡;错误详情(info)只取结构化部分入日志。
    """
    message = create_tool_result_message(
        block["id"], result.get("content") or [], result.get("isError", False)
    )
    payload: dict = {
        "turn": turn,
        "step": step,
        "message": message,
    }
    error = result.get("error")
    if error is not None and error.get("info") is not None:
        payload["error"] = error["info"]
    if result.get("meta") is not None:
        payload["meta"] = result["meta"]
    session.append("tool/result", payload, surface_op="append", source_event_seqs=[call_seq])


def append_skipped_tool_call(session, turn: int, step: int, block: dict) -> None:
    """给取消后从未开始的模型调用记录成对的合成结果。

    中止发生在派发之前,该调用的结局不可知 —— 不写任何记录会让
    重放看到「调用过但无结果」的悬挂;写合成错误让重放依然有效:
    模型侧看到明确的「未执行」而不是消失。
    """
    call_seq = append_tool_call(session, turn, step, block)
    append_tool_result(session, turn, step, block, {
        "content": [{"type": "text", "text": "Error: tool call aborted before dispatch"}],
        "isError": True,
        "error": {
            "message": "tool call aborted before dispatch",
            "info": {"name": "AbortError", "code": TOOL_ABORTED_BEFORE_DISPATCH},
        },
    }, call_seq)


async def execute_tool_calls(ctx, turn: int, step: int, tool_calls: list,
                             signal, accept_context) -> dict:
    """按活并发模式调度一个 assistant 步骤的工具调用。

    普通完成与中止都以模型序提交已开始调用的结果;中止排干它们、
    给未开始调用记录合成结果,然后带着**仍保持中止**的信号返回
    (调用方接受已开始调用的上下文,机器把它折进 next-step 收件
    箱)。调度器内部失败停止新派发、排干已开始派发,以第一个失败
    拒绝且不伪造工具结果。

    @param ctx - 持有工具注册表与发起 agent 的循环上下文。
    @param turn / step - 当前回合与步骤号。
    @param tool_calls - 模型序的 assistant 调用块。
    @param signal - 步骤共享的中止信号。
    @param accept_context - 接受已提交结果上下文,交给下一步边界。
    @returns {'concluded': bool} —— 是否有已提交结果终结了回合。
    """
    agent = ctx.agents.require_initiator()
    session = agent.session

    # 输入各不相同:tools/execute 包装可能替换 exec.signal
    planned: list = []
    for block in tool_calls:
        planned.append({
            "block": block,
            "exec": {
                "callId": block["id"],
                "name": block["name"],
                "arguments": parse_arguments(block.get("arguments", "")),
                "agent": agent,
                "signal": signal,
                "rootCallId": None,
                "parent": None,
            },
        })

    next_ = 0
    concluded = False
    while next_ < len(planned):
        # 先提交再分类:注册表变化影响未开始的调用
        first = planned[next_]
        mode = ctx.tools.executionMode(first["exec"]).get("kind", "exclusive")
        group = planned[next_:] if mode == "parallel" else [first]
        outcome = await run_group(ctx, turn, step, group, mode, signal, accept_context)
        next_ += outcome["consumed"]
        concluded = concluded or outcome["concluded"]
        if outcome["aborted"]:
            for call in planned[next_:]:
                append_skipped_tool_call(session, turn, step, call["block"])
            return {"concluded": concluded}
    return {"concluded": concluded}


async def run_group(ctx, turn: int, step: int, group: list, mode: str,
                    signal, accept_context) -> dict:
    """跑一个排他屏障或并行池。

    组内调用在开始前重新分类;排他重分类等当前池排干并留给调用方
    的下一个屏障。结果与上下文按模型序提交。中止停止开始、排干并
    提交已开始调用、把它们的上下文收进属主批次、给跳过调用记录
    结果,返回 aborted 结局;调度器失败排干派发但不提交合成恢复。
    """
    agent = ctx.agents.require_initiator()
    session = agent.session
    agent_loop = getattr(ctx, "agentLoop", None)
    config = agent_loop.config if agent_loop is not None else {}
    max_parallel = config.get("maxParallelToolCalls", DEFAULT_MAX_PARALLEL_TOOL_CALLS)
    scheduler = ctx.tools[TOOL_RUNTIME_SCHEDULER]

    # slots 按模型序对应;已开始槽保留它的 tool/call seq 供结果引用
    slots: list = [None] * len(group)
    call_seqs: list = [-1] * len(group)
    next_to_start = 0
    committed = 0
    started = 0
    aborted: bool = signal.aborted
    concluded = False
    scheduler_failure: dict | None = None
    in_flight: dict[int, asyncio.Task] = {}

    def throw_scheduler_failure() -> None:
        if scheduler_failure is not None:
            raise scheduler_failure["error"]

    async def commit_ready() -> None:
        """只跨连续模型序槽推进提交。"""
        nonlocal committed, concluded
        while committed < len(group):
            slot = slots[committed]
            if slot is None:
                break
            call = group[committed]
            if slot["needs_post"]:
                result = await scheduler.finalize(slot["exec"], slot["result"])
            else:
                result = scheduler.finish(slot["exec"], slot["result"])
            append_tool_result(session, turn, step, call["block"], result,
                               call_seqs[committed])
            for context in result.get("additionalContexts") or []:
                accept_context(context)
            if result.get("concludesTurn") is True:
                concluded = True
            committed += 1

    async def dispatch_call(index: int, exec_) -> int:
        """派发本体;拒绝被包含进 scheduler_failure(不能 raise)。"""
        nonlocal scheduler_failure
        try:
            outcome = await scheduler.dispatch(exec_)
        except Exception as error:  # noqa: BLE001 -- 调度失败保留第一个
            if scheduler_failure is None:
                scheduler_failure = {"error": error}
            return index
        slots[index] = {
            "exec": exec_,
            "result": outcome["result"],
            "needs_post": outcome["kind"] == "post-result",
        }
        return index

    async def start_call(index: int) -> None:
        nonlocal started
        call = group[index]
        call_seqs[index] = append_tool_call(session, turn, step, call["block"])
        started += 1
        prepared = await scheduler.prepare(call["exec"])
        throw_scheduler_failure()
        kind = prepared["kind"]
        if kind == "dispatch":
            task = asyncio.ensure_future(dispatch_call(index, prepared["exec"]))
            in_flight[index] = task
        elif kind == "post-result":
            slots[index] = {"exec": prepared["exec"], "result": prepared["result"],
                            "needs_post": True}
        elif kind == "final-result":
            slots[index] = {"exec": prepared["exec"], "result": prepared["result"],
                            "needs_post": False}
        # 闭联合联合穷尽守卫:三种 kind 之外是契约违约,不静默

    async def fill_pool() -> None:
        """补满并行池;有序提交后再读模式,注册表变化可造屏障。"""
        nonlocal aborted, next_to_start
        while (not aborted and next_to_start < len(group)
                and len(in_flight) < max_parallel):
            next_call = group[next_to_start]
            if (next_to_start > 0 and mode == "parallel"
                    and ctx.tools.executionMode(next_call["exec"]).get("kind", "exclusive")
                    != "parallel"):
                break
            await start_call(next_to_start)
            next_to_start += 1
            throw_scheduler_failure()
            await commit_ready()
            throw_scheduler_failure()
            # 中止可能在 pre-execute await 时到达
            if signal.aborted:
                aborted = True

    # 有序 pre-execute 可能 await;只有派发/本体重叠。调度器失败
    # 停止新派发,在每个已开始派发停稳后到达回合边界。
    try:
        await fill_pool()
        while in_flight:
            # 每轮只取一个已停稳派发(race 语义):可能同时停稳
            # 多个,余下的在下一轮立即返回
            done, _ = await asyncio.wait(
                list(in_flight.values()), return_when=asyncio.FIRST_COMPLETED
            )
            task = next(iter(done))
            settled_index = next(i for i, t in in_flight.items() if t is task)
            del in_flight[settled_index]
            throw_scheduler_failure()
            await commit_ready()
            throw_scheduler_failure()
            # 中止可能在工具或有序提交 await 时到达
            if signal.aborted:
                aborted = True
            await fill_pool()
    except Exception as error:  # noqa: BLE001 -- 调度失败:排干后以首因拒绝
        if scheduler_failure is None:
            scheduler_failure = {"error": error}
        await asyncio.gather(*in_flight.values(), return_exceptions=True)
        raise scheduler_failure["error"]

    if aborted:
        # 已开始调用与已接受上下文先停稳;剩余模型调用在回合中止
        # 前收到一条有序合成结果
        for call in group[started:]:
            append_skipped_tool_call(session, turn, step, call["block"])
        return {"consumed": len(group), "aborted": True, "concluded": concluded}
    if committed != started:
        raise RuntimeError("tool-call scheduler: uncommitted settled calls")
    return {"consumed": started, "aborted": False, "concluded": concluded}
