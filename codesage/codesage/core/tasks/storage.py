"""TaskStore:一任务一 JSON 文件 + 高水位 ID + 双向依赖 + 双层锁(spec §3-§4)。

一任务一文件(原子写 tmp+rename,复用 config/atomic.py);mutation 全过
「进程内 asyncio.Lock + 跨进程目录级 O_EXCL 文件锁」,读不锁(原子写保证
读者只见完整旧/新文件,容忍瞬时不一致)。删除 = 真删文件 + 清引用 + 高水位
防 ID 重用;依赖环靠 mutation 时预防(自环/存在性/DFS),外部编辑污染靠
graph.validate_task_graph 读取时诊断(§5)。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Awaitable, Callable

from ...config.atomic import atomic_write, read_json_lossy
from ...config.paths import config_dir
from .types import Task, TaskStatus, TaskSummary, TaskUpdate

logger = logging.getLogger("codesage.tasks.storage")

_SANITIZE_TASK_LIST_ID = re.compile(r"[^A-Za-z0-9_-]+")


class TaskStoreError(Exception):
    """任务存储层错误;工具层捕获转 is_error 交模型自愈(主规格 #2)。"""


def _sanitize_task_list_id(task_list_id: str) -> str:
    """非法字符替换为 '-' 并去首尾(镜像 Kode sanitizeTaskListId / session.py:18)。

    全非法 → 空串,兜底 "default":防任务直落存储根目录污染(#4)。
    """
    return _SANITIZE_TASK_LIST_ID.sub("-", task_list_id).strip("-") or "default"


def resolve_task_list_id(explicit: str | None = None) -> str:
    """explicit 参数 > CODESAGE_TASK_LIST_ID env > "default" 兜底(§8.1)。

    阶段 13 在此插入 team name 层(teammate 共享同一列表)。
    """
    if explicit:
        return explicit
    return os.getenv("CODESAGE_TASK_LIST_ID", "").strip() or "default"


def _pid_alive(pid: int) -> bool:
    """pid 活性检查(13 §11.4 R3 升级):进程不存在 → False。

    Windows 无 os.kill(pid, 0) 语义 → ctypes OpenProcess;POSIX 用
    kill 0 探测(PermissionError = 存在但不可信号,算存活)。
    OpenProcess 失败一律视为死:Windows 对不存在的 pid 也可能返回
    ERROR_ACCESS_DENIED(防 pid 枚举),「拒绝 = 存活」会把死锁误判为
    活锁而永不回收 —— 误回收(旧实现同款,无回归)比卡死可接受。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_lock(dir: Path, *, retries: int = 30, delay_s: float = 0.05,
                  stale_s: float = 10.0) -> Path:
    """同步锁体(在 asyncio.to_thread 线程内执行):O_EXCL 创建锁文件。

    失败路径(13 §11.4):mtime 超 stale_s 且锁内 pid 已死 → 死锁残留
    unlink 重试 —— 与既有时间超时相比,pid 活性检查消除「活进程长任务
    持锁超时被误回收」窗口(R3);pid 存活则继续等。超限抛 TaskStoreError。
    """
    dir.mkdir(parents=True, exist_ok=True)  # 锁文件在目录内:目录先存在
    lock_path = dir / ".lock"
    for _ in range(retries):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except (FileExistsError, PermissionError):
            # PermissionError: Windows AV 瞬时占用,与 atomic.py:41-46 先例同构
            try:
                if time.time() - lock_path.stat().st_mtime > stale_s:
                    try:
                        holder_pid = int(lock_path.read_text().split()[0])
                    except (OSError, ValueError, IndexError):
                        holder_pid = -1
                    if not _pid_alive(holder_pid):  # 锁主进程已死 → 真陈旧
                        lock_path.unlink()
            except OSError:
                pass
            time.sleep(delay_s)
            continue
        try:
            os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
            os.close(fd)
        except OSError:
            # O_EXCL 成功后写失败:清除毒锁再抛,不留孤儿锁(#5)
            try:
                lock_path.unlink()
            except OSError:
                pass
            raise
        return lock_path
    raise TaskStoreError("Failed to acquire task store lock")


def _release_lock(lock_path: Path) -> None:
    """退出只删自己持有的锁:stale 误判时后继者已建新锁,删它会放行双写者。"""
    try:
        if lock_path.read_text().split()[0] == str(os.getpid()):
            lock_path.unlink()
    except OSError:
        pass


@asynccontextmanager
async def _dir_lock(dir: Path, **kw):
    """目录级跨进程锁 async 入口(13 §11.4 R2):锁体(含重试 sleep)挪
    asyncio.to_thread,阻塞不卡事件循环;API 形态 from with → async with。
    kw 透传 _acquire_lock(测试用短重试/大 stale 窗口)。"""
    lock_path = await asyncio.to_thread(_acquire_lock, dir, **kw)
    try:
        yield
    finally:
        await asyncio.to_thread(_release_lock, lock_path)


def _task_path(dir: Path, task_id: str) -> Path:
    return dir / f"{task_id}.json"


def _read_task(dir: Path, task_id: str) -> Task | None:
    """读单任务文件;缺失或损坏 → None(损坏跳过不致命,主规格 #2)。

    文件名是 id 权威来源(镜像 Kode Object.keys):内容 id 不一致以文件名为准。
    """
    data = read_json_lossy(_task_path(dir, task_id), {})
    if not data:
        return None
    try:
        return Task.model_validate({**data, "id": task_id})
    except (ValueError, TypeError):
        return None


def _write_task(dir: Path, task: Task) -> None:
    """一任务一文件原子写(主规格 #14 tmp+rename)。"""
    atomic_write(_task_path(dir, task.id), task.model_dump_json(indent=2))


def _read_highwatermark(dir: Path) -> int:
    """历史最高任务 ID;缺失/损坏 → 0(损坏降级,下次创建靠文件扫描兜底)。"""
    try:
        return int((dir / ".highwatermark").read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def _write_highwatermark(dir: Path, mark: int) -> bool:
    """写回高水位;失败降级 best-effort(不致命,§3.3),返回是否成功。

    delete 路径依赖返回值:高水位写失败时文件扫描兜底不成立(文件已删),
    必须中止删除避免 ID 重用窗口(#2)。
    """
    try:
        atomic_write(dir / ".highwatermark", str(mark))
        return True
    except OSError:
        return False


def _next_id(dir: Path) -> str:
    """目录锁内调用:max(现有任务文件最大 ID, 高水位) + 1,写回高水位(§3.3)。"""
    files_max = max((int(p.stem) for p in dir.glob("*.json") if p.stem.isdigit()), default=0)
    next_id = max(files_max, _read_highwatermark(dir)) + 1
    _write_highwatermark(dir, next_id)
    return str(next_id)


def _has_path(tasks_by_id: dict[str, Task], from_id: str, to_id: str) -> bool:
    """沿 blocks 边从 from_id 出发能否到达 to_id(DFS,防加边成环,§5.2)。"""
    stack, visited = [from_id], set()
    while stack:
        cur = stack.pop()
        if cur == to_id:
            return True
        if cur in visited:
            continue
        visited.add(cur)
        task = tasks_by_id.get(cur)
        if task:
            stack.extend(b for b in task.blocks if b not in visited)
    return False


def _add_edge(tasks: dict[str, Task], source: Task, target: Task) -> None:
    """双向维护:source blocks target,两处各存一份(§5.1);重复声明幂等。"""
    if target.id not in source.blocks:
        source.blocks.append(target.id)
    if source.id not in target.blocked_by:
        target.blocked_by.append(source.id)


class TaskStore:
    """一任务一文件存储;mutation 全过 asyncio.Lock + 目录级文件锁,读不锁(§4)。

    on_change(13 §11.2):mutation 后单点回调,引擎注入 hooks.dispatch 包装
    → TaskCreated/TaskUpdated/TaskCompleted/TaskDeleted 事件;无订阅方为
    None(零路径)。回调在锁外调用、异常吞日志(fail-open,不拖慢 mutation)。
    """

    def __init__(self, root: Path | None = None,
                 on_change: "Callable[[str, Task, str], Awaitable[None]] | None" = None) -> None:
        self._root = root or config_dir() / "tasks"
        self._in_proc = asyncio.Lock()  # 进程内单飞:mutation 全部过它
        self.on_change = on_change

    async def _emit(self, event: str, task: Task, task_list_id: str) -> None:
        """mutation 后单点触发(§11.2);订阅方故障仅日志。"""
        if self.on_change is None:
            return
        try:
            await self.on_change(event, task, task_list_id)
        except Exception:  # noqa: BLE001 - fail-open:钩子故障不炸 mutation
            logger.exception("task on_change handler failed for %s", event)

    def _dir(self, task_list_id: str) -> Path:
        return self._root / _sanitize_task_list_id(task_list_id)

    def _read_all(self, dir: Path) -> dict[str, Task]:
        """目录内全部任务(以文件名为 id);损坏文件跳过。"""
        tasks: dict[str, Task] = {}
        for p in dir.glob("*.json"):
            if p.stem.isdigit():
                task = _read_task(dir, p.stem)
                if task is not None:
                    tasks[p.stem] = task
        return tasks

    # ---- mutation(锁内) ----

    async def create(self, task_list_id: str, *, subject: str, description: str,
                     active_form: str | None = None, metadata: dict | None = None,
                     owner: str | None = None) -> Task:
        """创建任务:ID 自增(高水位防删除后重用)。空 subject/description 拒绝。

        owner(13 §11.1 自动分配):缺省 None,工具层从 ToolUseContext 注入
        当前 agent 名 —— 「teammate 创建即归属自己」。
        """
        if not subject.strip() or not description.strip():
            raise TaskStoreError("subject and description are required")
        task_list_id = resolve_task_list_id(task_list_id)
        async with self._in_proc:
            dir = self._dir(task_list_id)
            dir.mkdir(parents=True, exist_ok=True)
            async with _dir_lock(dir):
                tid = _next_id(dir)
                task = Task(id=tid, subject=subject.strip(), description=description.strip(),
                            active_form=active_form, metadata=metadata or {},
                            owner=owner or None)
                _write_task(dir, task)
        await self._emit("TaskCreated", task, task_list_id)
        return task

    async def update(self, task_list_id: str, update: TaskUpdate) -> Task:
        """读-改-写契约:目录锁内重读任务文件再 patch(§4,锁外 get 结果不可作 patch 基础)。

        校验顺序(§5.2):自环 → 目标存在性 → 逐边成环检查(先查后加);
        全部通过后一次性应用。model_fields_set 区分「未传」与「显式 None」;
        metadata 为 dict 时键值合并,值为 None 的键删除(§6.3)。
        """
        task_list_id = resolve_task_list_id(task_list_id)
        task_id = update.task_id
        add_blocks = update.add_blocks
        add_blocked_by = update.add_blocked_by
        fields = update.model_fields_set
        async with self._in_proc:
            dir = self._dir(task_list_id)
            async with _dir_lock(dir):
                task = _read_task(dir, task_id)
                if task is None:
                    raise TaskStoreError(f"Task not found: {task_id}")
                # 1) 自环拒绝 + 2) 目标存在性(整批先校验)
                if task_id in add_blocks or task_id in add_blocked_by:
                    raise TaskStoreError(f"Task #{task_id} cannot depend on itself")
                others = self._read_all(dir)
                for dep in add_blocks + add_blocked_by:
                    if dep not in others:
                        raise TaskStoreError(f"Task not found: {dep}")
                # 3) 成环拒绝:逐边「先查后加」(spec §5.2 依序检查)
                others[task_id] = task
                for dep in add_blocks:  # 边 task_id → dep
                    if _has_path(others, dep, task_id):
                        raise TaskStoreError(f"Adding dependency {task_id} -> {dep} would create a cycle")
                    _add_edge(others, task, others[dep])
                for dep in add_blocked_by:  # 边 dep → task_id
                    if _has_path(others, task_id, dep):
                        raise TaskStoreError(f"Adding dependency {dep} -> {task_id} would create a cycle")
                    _add_edge(others, others[dep], task)
                # 应用字段(状态机:非 completed → completed 允许;completed 拒绝回退)
                was_completed = task.status == TaskStatus.COMPLETED
                if "subject" in fields and update.subject is not None:
                    task.subject = update.subject
                if "description" in fields and update.description is not None:
                    task.description = update.description
                if "active_form" in fields and update.active_form is not None:
                    task.active_form = update.active_form
                if "owner" in fields and update.owner is not None:
                    task.owner = update.owner
                if "status" in fields and update.status is not None:
                    if task.status == TaskStatus.COMPLETED and update.status != TaskStatus.COMPLETED:
                        raise TaskStoreError(f"Task #{task_id} is completed")
                    task.status = update.status
                if "metadata" in fields and update.metadata is not None:
                    merged = dict(task.metadata)
                    for key, value in update.metadata.items():
                        if value is None:
                            merged.pop(key, None)
                        else:
                            merged[key] = value
                    task.metadata = merged
                # 双向维护:两端的文件都写(各自原子)
                _write_task(dir, task)
                for dep in dict.fromkeys(add_blocks + add_blocked_by):
                    _write_task(dir, others[dep])
        # §11.2:状态机语义 — 进入 completed 报 TaskCompleted,其余报 TaskUpdated
        await self._emit(
            "TaskCompleted" if task.status == TaskStatus.COMPLETED and not was_completed
            else "TaskUpdated", task, task_list_id)
        return task

    async def delete(self, task_list_id: str, task_id: str) -> None:
        """真删:高水位更新 + 删文件 + 清理其余任务对它的引用(§3.4,引用清理 best-effort)。"""
        task_list_id = resolve_task_list_id(task_list_id)
        async with self._in_proc:
            dir = self._dir(task_list_id)
            async with _dir_lock(dir):
                path = _task_path(dir, task_id)
                if not path.exists():
                    raise TaskStoreError(f"Task not found: {task_id}")
                task = _read_task(dir, task_id)
                mark = max(_read_highwatermark(dir), int(task_id) if task_id.isdigit() else 0)
                # 高水位写失败 → 中止删除:文件已删时扫描兜底不成立,避免 ID 重用窗口(#2)
                if not _write_highwatermark(dir, mark):
                    raise TaskStoreError("failed to persist task store highwatermark")
                path.unlink()
                for other in self._read_all(dir).values():
                    changed = False
                    if task_id in other.blocks:
                        other.blocks.remove(task_id)
                        changed = True
                    if task_id in other.blocked_by:
                        other.blocked_by.remove(task_id)
                        changed = True
                    if changed:
                        try:
                            _write_task(dir, other)  # 单任务写原子;失败不致命
                        except OSError:
                            pass
        if task is not None:
            await self._emit("TaskDeleted", task, task_list_id)

    async def claim(self, task_list_id: str, task_id: str, agent: str) -> Task:
        """原子认领(13 §11.1,CC claimTaskWithBusyCheck):owner 设 agent。

        目录锁内 busy 检查:任务 in_progress 且 owner 非本 agent → 拒绝
        (队友进行中的任务不可抢);pending/空闲任务直接归属,状态不自动
        改(模型负责 in_progress 升级,认领与状态解耦)。
        """
        task_list_id = resolve_task_list_id(task_list_id)
        async with self._in_proc:
            dir = self._dir(task_list_id)
            async with _dir_lock(dir):
                task = _read_task(dir, task_id)
                if task is None:
                    raise TaskStoreError(f"Task not found: {task_id}")
                if task.status == TaskStatus.IN_PROGRESS and task.owner not in (None, agent):
                    raise TaskStoreError(
                        f"Task #{task_id} is in progress by {task.owner}")
                task.owner = agent
                _write_task(dir, task)
        await self._emit("TaskUpdated", task, task_list_id)
        return task

    async def unassign_agent(self, task_list_id: str, agent: str) -> int:
        """清空某 agent 的全部 owner(CC unassignTeammateTasks):只回退非
        completed 任务(11 R6 语义 —— completed 的归属是历史记录,不动)。

        返回回退条数;锁内遍历+写,缺失任务跳过。
        """
        task_list_id = resolve_task_list_id(task_list_id)
        cleared = 0
        async with self._in_proc:
            dir = self._dir(task_list_id)
            async with _dir_lock(dir):
                for task in self._read_all(dir).values():
                    if task.owner == agent and task.status != TaskStatus.COMPLETED:
                        task.owner = None
                        _write_task(dir, task)
                        cleared += 1
        return cleared

    # ---- 只读(不锁) ----

    def get(self, task_list_id: str, task_id: str) -> Task | None:
        """单任务;不存在/损坏 → None(工具层转 "Task not found" is_error,§6.3)。"""
        return _read_task(self._dir(resolve_task_list_id(task_list_id)), task_id)

    def list(self, task_list_id: str) -> list[Task]:
        """全部任务按 ID 升序(文件序即 ID 序);损坏文件跳过不致命。"""
        dir = self._dir(resolve_task_list_id(task_list_id))
        return [t for _, t in sorted(self._read_all(dir).items(), key=lambda kv: int(kv[0]))]

    def summaries(self, task_list_id: str) -> list[TaskSummary]:
        """TaskList 摘要:blocked_by 只保留「现存且未完成」的阻塞者(§6.4)。"""
        tasks = self.list(task_list_id)  # list 内部已 resolve
        done = {t.id for t in tasks if t.status == TaskStatus.COMPLETED}
        ids = {t.id for t in tasks}
        return [TaskSummary(id=t.id, subject=t.subject, status=t.status, owner=t.owner,
                            blocked_by=[b for b in t.blocked_by if b in ids and b not in done])
                for t in tasks]


_store: TaskStore | None = None


def get_task_store() -> TaskStore:
    """模块级单例(工具装配点之外直取用);测试直接构造 TaskStore(tmp_path)。"""
    global _store
    if _store is None:
        _store = TaskStore()
    return _store
