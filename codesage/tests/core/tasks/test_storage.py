"""TaskStore 存储层测试(镜像 spec §9.1):CRUD/落盘形状/高水位/双向依赖/环预防/sanitize。"""

import asyncio
import json

import pytest

from codesage.core.tasks import TaskStatus, TaskStore, TaskStoreError, TaskUpdate


@pytest.fixture
def store(tmp_path) -> TaskStore:
    return TaskStore(tmp_path)


def _upd(task_id: str, **fields) -> TaskUpdate:
    """构造 TaskUpdate 快捷方式。"""
    return TaskUpdate(task_id=task_id, **fields)


async def _seed(store: TaskStore, n: int = 3, task_list_id: str = "default"):
    """创建 n 个任务,返回 Task 列表(id 依次 1..n)。"""
    return [await store.create(task_list_id, subject=f"任务{i}", description="d") for i in range(1, n + 1)]


# ---- CRUD 全链路 + 落盘形状 ----

async def test_create_writes_one_json_file_per_task(tmp_path):
    store = TaskStore(tmp_path)
    task = await store.create("default", subject="修复登录 bug", description="详情",
                              active_form="修复中", metadata={"k": "v"})
    assert task.id == "1"
    path = tmp_path / "default" / "1.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["id"] == "1"
    assert data["subject"] == "修复登录 bug"
    assert data["status"] == "pending"
    assert data["active_form"] == "修复中"
    assert data["blocks"] == []
    assert data["blocked_by"] == []  # snake_case
    assert data["metadata"] == {"k": "v"}
    assert (tmp_path / "default" / ".highwatermark").read_text() == "1"


async def test_crud_roundtrip(store):
    created = await store.create("l", subject="s", description="d")
    assert store.get("l", created.id) == created
    updated = await store.update("l", _upd(created.id, subject="s2", status=TaskStatus.IN_PROGRESS))
    assert updated.subject == "s2"
    assert updated.status == TaskStatus.IN_PROGRESS
    assert store.get("l", created.id).subject == "s2"
    await store.delete("l", created.id)
    assert store.get("l", created.id) is None


async def test_empty_subject_rejected(store):
    with pytest.raises(TaskStoreError, match="required"):
        await store.create("l", subject="  ", description="d")


async def test_update_unknown_task_raises(store):
    with pytest.raises(TaskStoreError, match="Task not found"):
        await store.update("l", _upd("42", subject="x"))


# ---- ID 自增 + 高水位 ----

async def test_ids_increment_and_highwatermark_prevents_reuse(store):
    tasks = await _seed(store, 3)
    assert [t.id for t in tasks] == ["1", "2", "3"]
    await store.delete("default", "3")
    again = await store.create("default", subject="x", description="d")
    assert again.id == "4"  # 高水位防重用,不退回 3


async def test_ids_sorted_ascending_in_list(store):
    tasks = await _seed(store, 3)
    await store.delete("default", tasks[1].id)
    assert [t.id for t in store.list("default")] == ["1", "3"]


# ---- 损坏文件跳过不致命 ----

async def test_corrupt_task_file_skipped(store):
    await _seed(store, 2)
    (store._root / "default" / "2.json").write_text("{not json", encoding="utf-8")
    assert [t.id for t in store.list("default")] == ["1"]
    assert store.get("default", "2") is None  # 损坏视为不存在


# ---- 删除清引用(双向) ----

async def test_delete_clears_references(store):
    t1 = await store.create("l", subject="1", description="d")
    t2 = await store.create("l", subject="2", description="d")
    await store.update("l", _upd(t1.id, add_blocks=[t2.id]))
    assert store.get("l", t2.id).blocked_by == [t1.id]
    await store.delete("l", t2.id)
    assert store.get("l", t1.id).blocks == []


async def test_delete_unknown_task_raises(store):
    with pytest.raises(TaskStoreError, match="Task not found"):
        await store.delete("l", "9")


# ---- 状态机(§6.2) ----

async def test_completed_is_terminal(store):
    t = await store.create("l", subject="s", description="d")
    await store.update("l", _upd(t.id, status=TaskStatus.COMPLETED))
    with pytest.raises(TaskStoreError, match="is completed"):
        await store.update("l", _upd(t.id, status=TaskStatus.IN_PROGRESS))
    with pytest.raises(TaskStoreError, match="is completed"):
        await store.update("l", _upd(t.id, status=TaskStatus.PENDING))


async def test_in_progress_can_revert_to_pending(store):
    t = await store.create("l", subject="s", description="d")
    await store.update("l", _upd(t.id, status=TaskStatus.IN_PROGRESS))
    await store.update("l", _upd(t.id, status=TaskStatus.PENDING))  # 放弃允许
    assert store.get("l", t.id).status == TaskStatus.PENDING


async def test_owner_explicit_only(store):
    t = await store.create("l", subject="s", description="d")
    await store.update("l", _upd(t.id, status=TaskStatus.IN_PROGRESS))
    assert store.get("l", t.id).owner is None  # 不做自动分配(§6.2)
    await store.update("l", _upd(t.id, owner="alice"))
    assert store.get("l", t.id).owner == "alice"


# ---- summaries 过滤(§6.4) ----

async def test_summaries_filter_completed_blockers(store):
    t1 = await store.create("l", subject="1", description="d")
    t2 = await store.create("l", subject="2", description="d")
    t3 = await store.create("l", subject="3", description="d")
    await store.update("l", _upd(t2.id, add_blocked_by=[t1.id, t3.id]))
    await store.update("l", _upd(t1.id, status=TaskStatus.COMPLETED))
    summaries = {s.id: s for s in store.summaries("l")}
    assert summaries["2"].blocked_by == ["3"]  # completed blocker 过滤;未完成保留
    assert summaries["1"].blocked_by == []
    assert summaries["3"].blocked_by == []


async def test_summaries_filter_nonexistent_blocker(store, tmp_path):
    # 手工编辑文件制造悬空引用(存储不变量靠 mutation 预防,读取诊断兜底)
    await store.create("l", subject="1", description="d")
    path = tmp_path / "l" / "1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["blocked_by"] = ["99"]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.summaries("l")[0].blocked_by == []


# ---- 双向依赖维护 + 幂等(§5.1) ----

async def test_add_blocks_bidirectional_and_idempotent(store):
    t1 = await store.create("l", subject="1", description="d")
    t2 = await store.create("l", subject="2", description="d")
    await store.update("l", _upd(t1.id, add_blocks=[t2.id]))
    await store.update("l", _upd(t1.id, add_blocks=[t2.id]))  # 幂等:重复声明不重复写
    assert store.get("l", t1.id).blocks == [t2.id]
    assert store.get("l", t2.id).blocked_by == [t1.id]


async def test_add_blocked_by_bidirectional(store):
    t1 = await store.create("l", subject="1", description="d")
    t2 = await store.create("l", subject="2", description="d")
    await store.update("l", _upd(t2.id, add_blocked_by=[t1.id]))
    assert store.get("l", t1.id).blocks == [t2.id]
    assert store.get("l", t2.id).blocked_by == [t1.id]


# ---- mutation 环预防(§5.2) ----

async def test_self_loop_rejected(store):
    t1 = await store.create("l", subject="1", description="d")
    with pytest.raises(TaskStoreError, match="cannot depend on itself"):
        await store.update("l", _upd(t1.id, add_blocks=[t1.id]))
    with pytest.raises(TaskStoreError, match="cannot depend on itself"):
        await store.update("l", _upd(t1.id, add_blocked_by=[t1.id]))


async def test_cycle_rejected(store):
    t1 = await store.create("l", subject="1", description="d")
    t2 = await store.create("l", subject="2", description="d")
    t3 = await store.create("l", subject="3", description="d")
    await store.update("l", _upd(t1.id, add_blocks=[t2.id]))
    await store.update("l", _upd(t2.id, add_blocks=[t3.id]))
    with pytest.raises(TaskStoreError, match="would create a cycle"):
        await store.update("l", _upd(t3.id, add_blocks=[t1.id]))
    with pytest.raises(TaskStoreError, match="would create a cycle"):
        await store.update("l", _upd(t1.id, add_blocked_by=[t3.id]))
    # 环未落盘:图仍无环
    assert store.get("l", t1.id).blocks == [t2.id]


async def test_missing_dependency_rejected(store):
    t1 = await store.create("l", subject="1", description="d")
    with pytest.raises(TaskStoreError, match="Task not found: 99"):
        await store.update("l", _upd(t1.id, add_blocks=["99"]))


async def test_same_batch_add_blocks_and_blocked_by_cycle_rejected(store):
    # 同批 add_blocks + add_blocked_by 构成 2-cycle:逐边先查后加,第二边拒绝(#7)
    t1 = await store.create("l", subject="1", description="d")
    t3 = await store.create("l", subject="3", description="d")
    with pytest.raises(TaskStoreError, match="would create a cycle"):
        await store.update("l", _upd(t1.id, add_blocks=[t3.id], add_blocked_by=[t3.id]))
    assert store.get("l", t1.id).blocks == []  # 整体未落盘


# ---- metadata 合并(§6.3) ----

async def test_metadata_merge_and_delete_key(store):
    t = await store.create("l", subject="s", description="d", metadata={"a": 1, "b": 2})
    await store.update("l", _upd(t.id, metadata={"b": None, "c": 3}))
    assert store.get("l", t.id).metadata == {"a": 1, "c": 3}


# ---- 锁内重读契约(§4):并发 update 不丢更新 ----

async def test_concurrent_updates_do_not_lose_fields(store):
    # 锁外读的旧值不可作 patch 基础:并发双 update 各改一个字段,两处都保留
    t = await store.create("l", subject="s", description="d")
    await asyncio.gather(
        store.update("l", _upd(t.id, subject="新主题")),
        store.update("l", _upd(t.id, description="新详情")),
    )
    assert store.get("l", t.id).subject == "新主题"
    assert store.get("l", t.id).description == "新详情"


# ---- task_list_id sanitize(§3.1) ----

async def test_task_list_id_sanitized(store):
    await store.create("a/b c", subject="s", description="d")
    assert (store._root / "a-b-c" / "1.json").exists()
    assert not (store._root / "a" / "b c" / "1.json").exists()


async def test_task_list_id_all_invalid_falls_back_to_default(store):
    # 全非法字符 → sanitize 空串 → "default" 兜底,防根目录直落(#4)
    await store.create("???", subject="s", description="d")
    assert (store._root / "default" / "1.json").exists()


# ---- 高水位损坏/写失败兜底(#2 / #7) ----

async def test_corrupt_highwatermark_falls_back_to_file_scan(store):
    await _seed(store, 2)
    (store._root / "default" / ".highwatermark").write_text("garbage", encoding="utf-8")
    t = await store.create("default", subject="x", description="d")
    assert t.id == "3"  # 高水位损坏 → 0,文件扫描兜底:max(文件 1,2) + 1


async def test_delete_aborts_when_highwatermark_write_fails(store, monkeypatch):
    from codesage.core.tasks import storage as storage_mod

    t1 = await store.create("l", subject="1", description="d")
    monkeypatch.setattr(storage_mod, "_write_highwatermark", lambda dir, mark: False)
    with pytest.raises(TaskStoreError, match="highwatermark"):
        await store.delete("l", t1.id)
    assert store.get("l", t1.id) is not None  # 主文件保留,无 ID 重用窗口


async def test_resolve_task_list_id_fallback(monkeypatch):
    from codesage.core.tasks import resolve_task_list_id

    monkeypatch.delenv("CODESAGE_TASK_LIST_ID", raising=False)
    assert resolve_task_list_id() == "default"
    assert resolve_task_list_id("team-a") == "team-a"
    monkeypatch.setenv("CODESAGE_TASK_LIST_ID", "env-list")
    assert resolve_task_list_id() == "env-list"
    assert resolve_task_list_id("explicit") == "explicit"  # explicit 优先
