"""写合并控制器单测:窗口、屏障、失败保序重试与自动暂停。

覆盖 DSH write-behind 契约的 Python 映射:enqueue 顶层拷贝、
flush 共享屏障与并发调用收敛、失败时 batch + pending 保序重试、
自动暂停(failure 后由 enqueue 重启)、显式 flush 对自动窗口的
优先级。定时器路径经真实事件循环驱动(短窗口)。
"""

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]  # 仓库根
_PACKAGES = Path(__file__).resolve().parents[3]  # packages/
_PKG = Path(__file__).resolve().parents[1]  # 本包目录(session-persistence)
sys.path.insert(0, str(_PKG))  # from src import ... → {包目录}/src
sys.path.insert(0, str(_PACKAGES))  # from core.session import ...

from session.session_persistence.src.write_behind import SessionWriteBehind  # noqa: E402


def _behind(write, max_delay_ms=200):
    """构造一个记录写调用历史的控制器;write 默认成功。"""
    calls = []
    errors = []

    async def _write(batch):
        calls.append([dict(e) for e in batch])

    controller = SessionWriteBehind(
        {
            "max_delay_ms": max_delay_ms,
            "write": write or _write,
            "report_background_failure": errors.append,
        }
    )
    return controller, calls, errors


def test_enqueue_copies_top_level():
    """队列持有独立顶层拷贝:调用方改动原事件不影响队列。"""
    controller, calls, _ = _behind(None)
    event = {"type": "turn/start", "seq": 0, "time": 1, "data": {}}
    controller.enqueue(event)
    event["seq"] = 99
    assert controller.pending[0]["seq"] == 0


def test_flush_drains_pending_and_shares_barrier():
    """flush 排干全部挂起事件,并发 flush 共享同一屏障。"""

    async def scenario():
        controller, calls, _ = _behind(None)
        controller.enqueue({"type": "a", "seq": 0})
        controller.enqueue({"type": "b", "seq": 1})
        first = controller.flush()
        second = controller.flush()
        assert first is second  # 共享同一屏障
        await first
        assert [e["type"] for e in calls[0]] == ["a", "b"]
        assert controller.barrier is None
        assert len(controller.pending) == 0

    asyncio.run(scenario())


def test_flush_after_enqueue_during_barrier_starts_new_window():
    """屏障落定后才入队的事件,进入新窗口而不是滞留在旧屏障后。"""

    async def scenario():
        controller, calls, _ = _behind(None)
        controller.enqueue({"type": "a", "seq": 0})
        await controller.flush()
        controller.enqueue({"type": "b", "seq": 1})
        assert controller.barrier is None
        assert len(controller.pending) == 1
        await controller.flush()
        assert [e["type"] for e in calls[1]] == ["b"]

    asyncio.run(scenario())


def test_failure_retains_batch_in_order_and_pauses_automatic():
    """耐久写失败:整批按序回到队列头,自动窗口暂停,重试成功恢复。"""

    async def scenario():
        attempts = []

        async def _flaky(batch):
            attempts.append([e["type"] for e in batch])
            if len(attempts) == 1:
                raise RuntimeError("disk full")

        controller, calls, errors = _behind(_flaky)
        controller.enqueue({"type": "a", "seq": 0})
        with pytest.raises(RuntimeError, match="disk full"):
            await controller.flush()
        assert len(attempts) == 1  # 第一次失败
        assert len(errors) == 0  # 显式 flush 的失败不上报后台
        assert [e["type"] for e in controller.pending] == ["a"]  # 保序保留
        assert controller.automatic_paused is True
        # 失败后 enqueue 重启自动窗口并解除暂停
        controller.enqueue({"type": "b", "seq": 1})
        assert controller.automatic_paused is False
        await controller.flush()
        assert len(attempts) == 2
        assert attempts[1] == ["a", "b"]  # 保序:旧批在新事件前

    asyncio.run(scenario())


def test_background_failure_reported():
    """后台写失败上报 report_background_failure(由自动窗口触发)。"""

    async def _flaky(batch):
        raise RuntimeError("boom")

    async def scenario():
        controller, calls, errors = _behind(_flaky, max_delay_ms=10)
        controller.enqueue({"type": "a", "seq": 0})
        # 等待自动窗口到期并完成写(轮询后台结果)
        for _ in range(100):
            if len(errors) > 0:
                break
            await asyncio.sleep(0.02)
        assert len(errors) == 1
        assert "boom" in str(errors[0])
        # 失败保留:事件仍按序在队列里
        assert [e["type"] for e in controller.pending] == ["a"]

    asyncio.run(scenario())


def test_automatic_window_writes_after_delay():
    """窗口到期自动写(不需要显式 flush)。"""

    async def scenario():
        controller, calls, _ = _behind(None, max_delay_ms=20)
        controller.enqueue({"type": "a", "seq": 0})
        for _ in range(100):
            if len(calls) == 1:
                break
            await asyncio.sleep(0.01)
        assert len(calls) == 1
        assert calls[0][0]["type"] == "a"

    asyncio.run(scenario())


def test_cancel_automatic_wait_keeps_pending():
    """cancelAutomaticWait 取消定时器但保留挂起事件(由 flush 驱动)。"""

    async def scenario():
        controller, calls, _ = _behind(None, max_delay_ms=10)
        controller.enqueue({"type": "a", "seq": 0})
        controller.cancel_automatic_wait()
        await asyncio.sleep(0.05)  # 超过窗口:不写,定时器已取消
        assert len(calls) == 0
        assert len(controller.pending) == 1
        await controller.flush()
        assert len(calls) == 1

    asyncio.run(scenario())


def test_has_work_tracks_pending_and_active():
    """hasWork:挂起事件或进行中的写都算有工作。"""

    async def scenario():
        controller, calls, _ = _behind(None)
        assert controller.has_work is False
        controller.enqueue({"type": "a", "seq": 0})
        assert controller.has_work is True
        await controller.flush()
        assert controller.has_work is False

    asyncio.run(scenario())


def test_enqueue_after_failure_restarts_window():
    """automaticPaused 时 enqueue 立即重启自动窗口(无屏障)。"""

    async def _flaky(batch):
        raise RuntimeError("boom")

    async def scenario():
        controller, calls, errors = _behind(_flaky, max_delay_ms=5)
        controller.enqueue({"type": "a", "seq": 0})
        # 等第一次后台失败
        for _ in range(100):
            if len(errors) == 1:
                break
            await asyncio.sleep(0.01)
        assert controller.automatic_paused is True
        controller.enqueue({"type": "b", "seq": 1})
        assert controller.automatic_paused is False
        # 新窗口很快到期,自动重试(保序)
        for _ in range(100):
            if len(errors) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(errors) == 2
        assert [e["type"] for e in controller.pending] == ["a", "b"]

    asyncio.run(scenario())
