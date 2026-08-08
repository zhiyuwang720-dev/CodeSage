"""_dir_lock 与并发原子性测试(镜像 spec §9.1):O_EXCL 互斥/stale 回收/超限/清理/gather 并发。"""

import asyncio
import os
import threading
import time

import pytest

from codesage.core.tasks import TaskStore, TaskStoreError
from codesage.core.tasks.storage import _dir_lock


def test_lock_mutual_exclusion(tmp_path):
    # 同进程二次获取 → 锁被占用,重试上限内一直失败 → TaskStoreError
    with _dir_lock(tmp_path, retries=2, delay_s=0.01):
        assert (tmp_path / ".lock").exists()
        with pytest.raises(TaskStoreError, match="Failed to acquire"):
            with _dir_lock(tmp_path, retries=2, delay_s=0.01):
                pass
    assert not (tmp_path / ".lock").exists()  # 退出后清理


def test_stale_lock_reclaimed(tmp_path):
    # 他进程残留锁(mtime 超 stale_s)→ 视为死锁回收重建
    lock = tmp_path / ".lock"
    lock.write_text("9999 0\n", encoding="utf-8")
    old = time.time() - 60
    os.utime(lock, (old, old))
    with _dir_lock(tmp_path, retries=2, delay_s=0.01, stale_s=10.0):
        assert lock.read_text().split()[0] == str(os.getpid())
    assert not lock.exists()


def test_lock_timeout_raises(tmp_path):
    # 新鲜锁(不 stale)→ 全部重试失败 → 抛错
    lock = tmp_path / ".lock"
    lock.write_text("9999 0\n", encoding="utf-8")
    with pytest.raises(TaskStoreError, match="Failed to acquire"):
        with _dir_lock(tmp_path, retries=1, delay_s=0.01, stale_s=100.0):
            pass


def test_lock_released_on_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with _dir_lock(tmp_path):
            raise RuntimeError("boom")
    assert not (tmp_path / ".lock").exists()


def test_thread_race_single_holder(tmp_path):
    """两线程同时抢同一目录锁:任一时刻仅一持锁,无异常,锁文件最终清理(#6)。"""
    holder = [0]
    max_holder = [0]
    errors = []
    guard = threading.Lock()

    def worker():
        try:
            with _dir_lock(tmp_path, retries=30, delay_s=0.01):
                with guard:
                    holder[0] += 1
                    max_holder[0] = max(max_holder[0], holder[0])
                time.sleep(0.01)  # 拉长持锁窗口,增大真实交错概率
                with guard:
                    holder[0] -= 1
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert max_holder[0] == 1  # 从未同时双持锁
    assert not (tmp_path / ".lock").exists()


async def test_concurrent_creates_unique_ids(tmp_path):
    # 进程内 asyncio.Lock 单飞:并发 create N 个,ID 无重复无丢失
    store = TaskStore(tmp_path)
    tasks = await asyncio.gather(*[
        store.create("l", subject=f"任务{i}", description="d") for i in range(20)
    ])
    ids = sorted(int(t.id) for t in tasks)
    assert ids == list(range(1, 21))
    assert len(store.list("l")) == 20
