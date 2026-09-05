"""sink → /stream 契约集成测试(修复 5 + 修复 2 后端侧)。

_build_pr_review_event_sink 把 review 运行时事件映射为 EventManager 队列事件;
stream_events 遇 task_complete 终止(修复 5 的 done→task_complete)。本测试验证:
- thinking 生命周期: content_delta/reasoning_delta → thinking_start/thinking_token(带 token metadata)
- message dict 只取 content, 不整 dump(修复 5 的 INFO 原始 dict 问题)
- tool_call 名称/输入正确落位
- done(task_complete=True) → task_complete, 且 stream_events 排空后终止
全离线: EventManager 不接 DB 工厂, 只用内存队列。asyncio_mode=auto。
"""
from __future__ import annotations

from app.api.v1.endpoints.agent_tasks import _build_pr_review_event_sink
from app.services.agent.event_manager import EventManager


def _push_and_drain(sink, em: EventManager, task_id: str, events: list[dict]) -> list[dict]:
    """async 场景: 用 asyncio.run 推事件并同步排空队列。"""
    import asyncio

    async def run():
        for ev in events:
            await sink(ev)
        # 排空内存队列
        drained: list[dict] = []
        while not em._event_queues[task_id].empty():
            drained.append(em._event_queues[task_id].get_nowait())
        return drained

    return asyncio.run(run())


def _mk_em(task_id: str) -> EventManager:
    em = EventManager()  # 无 DB 工厂: 不落库
    em.create_queue(task_id)
    return em


def test_sink_thinking_lifecycle_and_token_metadata():
    em = _mk_em("t-1")
    sink = _build_pr_review_event_sink("t-1", em)
    drained = _push_and_drain(
        sink, em, "t-1",
        [
            {"type": "meta", "repo": "o/r", "pr_number": 7},
            {"type": "perspective_start", "perspective": "security"},
            {"type": "reasoning_delta", "perspective": "security", "content": "第一段"},
            {"type": "reasoning_delta", "perspective": "security", "content": "第二段"},
            {"type": "tool_call", "perspective": "security", "tool_call": {"name": "search_code", "input": {"keyword": "auth"}}},
            {"type": "done", "perspective": "security", "task_complete": True, "message": "完成"},
        ],
    )
    types = [e["event_type"] for e in drained]
    assert types == [
        "review_meta",
        "review_perspective_start",
        "thinking_start",
        "thinking_token",
        "thinking_token",
        "tool_call",
        "thinking_end",
        "task_complete",
    ]
    # thinking_token 带 token(前端 onThinkingToken 触发条件)。
    # 注意: accumulated 会被 EventManager._normalize_event_metadata 从 thinking_token
    # metadata 剥掉(高频事件瘦身), 前端 AgentStreamHandler 用本地 thinkingBuffer 拼接;
    # thinking_end 事件才保留 accumulated(供 onThinkingEnd 落盘标题)。
    token_events = [e for e in drained if e["event_type"] == "thinking_token"]
    assert token_events[0]["metadata"]["token"] == "第一段"
    assert "accumulated" not in token_events[0]["metadata"]
    assert token_events[-1]["metadata"]["token"] == "第二段"
    # tool_call 名称/输入落位
    tool = [e for e in drained if e["event_type"] == "tool_call"][0]
    assert tool["tool_name"] == "search_code"
    assert tool["tool_input"] == {"keyword": "auth"}
    assert tool["phase"] == "security"
    # thinking_end 带 accumulated
    end = [e for e in drained if e["event_type"] == "thinking_end"][0]
    assert end["metadata"]["accumulated"] == "第一段第二段"
    # task_complete 为最终事件
    assert drained[-1]["event_type"] == "task_complete"


def test_sink_message_dict_flat_content():
    """message 事件是 dict 时只取 content 字段(修复 5 的原始 dict dump 问题)。"""
    em = _mk_em("t-2")
    sink = _build_pr_review_event_sink("t-2", em)
    drained = _push_and_drain(
        sink, em, "t-2",
        [
            {"type": "assistant_start", "perspective": "quality", "message": {
                "id": "msg-1", "session_id": "sess-1", "role": "assistant",
                "content": "干净的正文", "metadata": {}, "payload": {},
            }},
            {"type": "done", "perspective": "quality", "task_complete": True,
             "message": {"id": "msg-2", "content": "审查完成"}},
        ],
    )
    start = [e for e in drained if e["event_type"] == "assistant_start"][0]
    assert start["message"] == "干净的正文"  # 不是 str(dict)
    assert "{'id'" not in start["message"] and "session_id" not in start["message"]
    complete = [e for e in drained if e["event_type"] == "task_complete"][0]
    assert complete["message"] == "审查完成"


def test_sink_perspective_done_calls_progress_cb():
    """perspective_done 触发 progress 回调(修复 4 的进度推进钩子)。"""
    em = _mk_em("t-3")
    called: list[str] = []

    async def progress_cb(perspective: str) -> None:
        called.append(perspective)

    sink = _build_pr_review_event_sink("t-3", em, progress_cb=progress_cb)
    _push_and_drain(
        sink, em, "t-3",
        [
            {"type": "perspective_done", "perspective": "security", "turn_count": 4, "findings": 2},
            {"type": "perspective_done", "perspective": "architecture", "turn_count": 3, "findings": 0},
            {"type": "done", "task_complete": True},
        ],
    )
    assert called == ["security", "architecture"]


def test_stream_terminates_on_task_complete():
    """stream_events 收到 task_complete 后终止(修复 2 后端侧的流终结)。"""
    import asyncio

    em = _mk_em("t-4")
    sink = _build_pr_review_event_sink("t-4", em)
    events = [
        {"type": "meta", "repo": "o/r", "pr_number": 7},
        {"type": "perspective_start", "perspective": "security"},
        {"type": "done", "perspective": "security", "task_complete": True, "message": "完成"},
    ]

    async def run():
        for ev in events:
            await sink(ev)
        # 流消费: 应从队列读到所有事件, 且遇 task_complete 停止(不出现心跳)
        collected: list[str] = []
        async for item in em.stream_events("t-4", after_sequence=0):
            collected.append(item["event_type"])
            if item["event_type"] == "task_complete":
                break
        return collected

    collected = asyncio.run(run())
    assert collected[-1] == "task_complete"
    assert "heartbeat" not in collected


def test_sink_usage_accumulation_and_flush():
    """07-P1: done 带 usage → llm_usage 事件 + token 累计; 07-P1.1: 迭代按 done 逐轮累计,
    flush 增量回写 AgentTask 统计列(实时可观测 + Plan B checkpoint 统计源)。"""
    from types import SimpleNamespace

    em = _mk_em("t-5")

    class FakeDB:
        async def commit(self):
            pass

    task = SimpleNamespace(total_iterations=0, tool_calls_count=0, tokens_used=0)
    sink = _build_pr_review_event_sink("t-5", em, task=task, db=FakeDB())
    drained = _push_and_drain(
        sink, em, "t-5",
        [
            {"type": "tool_call", "perspective": "security", "tool_call": {"name": "search_code"}},
            {"type": "done", "perspective": "security", "usage": {"total_tokens": 120}},
            {"type": "done", "perspective": "security", "usage": {"total_tokens": 30}},
            {"type": "perspective_done", "perspective": "security", "turn_count": 4, "findings": 2},
            {"type": "done", "task_complete": True, "message": "完成"},
        ],
    )
    # llm_usage 事件: 每轮 done 带 usage 产生一条, 携带 tokens_used
    llm = [e for e in drained if e["event_type"] == "llm_usage"]
    assert [e["tokens_used"] for e in llm] == [120, 30]
    assert all(e["phase"] == "security" for e in llm)
    # 07-P1.1: 迭代按运行时 done 逐轮累计(2 个带 perspective 的 done = 2 轮;
    # perspective_done 的 turn_count 仅用于日志措辞不再累计, 避免双计;
    # 执行器收尾补发的 task_complete done 无 perspective 不计)。
    assert task.total_iterations == 2
    assert task.tool_calls_count == 1
    assert task.tokens_used == 150
    # 无 usage 的 done 不产生 llm_usage(零 token 不刷屏)
    assert len([e for e in drained if e["event_type"] == "task_complete"]) == 1


def test_sink_usage_fallback_input_output_tokens():
    """07-P1.1: usage 无 total_tokens 时回退 input/output, 再回退 prompt/completion。"""
    em = _mk_em("t-6")
    drained = _push_and_drain(
        _build_pr_review_event_sink("t-6", em),
        em, "t-6",
        [
            {"type": "done", "perspective": "security", "usage": {"input_tokens": 11, "output_tokens": 22}},
            {"type": "done", "perspective": "quality", "usage": {"prompt_tokens": 3, "completion_tokens": 4}},
            {"type": "done", "perspective": "architecture", "usage": {"total_tokens": 100}},
            {"type": "done", "task_complete": True, "message": "完成"},
        ],
    )
    llm = [e for e in drained if e["event_type"] == "llm_usage"]
    # input/output 兜底 → 33; prompt/completion 兜底 → 7; total_tokens 直取 → 100
    assert [e["tokens_used"] for e in llm] == [33, 7, 100]
