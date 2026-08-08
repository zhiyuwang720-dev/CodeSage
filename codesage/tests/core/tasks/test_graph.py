"""tasks 契约层 + 只读图验证测试(镜像 spec §9.1 清单)。

纯函数无 IO:直接构造 Task 列表喂入 validate_task_graph,无需任何 fixture。
"""

from codesage.core.tasks import (
    MissingTaskDependency,
    Task,
    TaskStatus,
    TaskSummary,
    TaskUpdate,
    validate_task_graph,
)


def _task(id: str, blocks=(), blocked_by=()) -> Task:
    return Task(
        id=id,
        subject=f"任务{id}",
        description="",
        blocks=list(blocks),
        blocked_by=list(blocked_by),
    )


# ---- types 契约 ----

def test_task_defaults():
    task = Task(id="1", subject="修复登录 bug", description="详情")
    assert task.status == TaskStatus.PENDING
    assert task.active_form is None
    assert task.owner is None
    assert task.blocks == []
    assert task.blocked_by == []
    assert task.metadata == {}


def test_task_status_enum_values():
    assert TaskStatus.PENDING.value == "pending"
    assert TaskStatus.IN_PROGRESS.value == "in_progress"
    assert TaskStatus.COMPLETED.value == "completed"


def test_task_update_fields():
    update = TaskUpdate(task_id="3", add_blocks=["1"], metadata={"k": "v"})
    assert update.task_id == "3"
    assert update.subject is None
    assert update.status is None
    assert update.add_blocks == ["1"]
    assert update.add_blocked_by == []
    assert update.metadata == {"k": "v"}


def test_task_summary_shape():
    task = _task("1", blocked_by=["2"])
    summary = TaskSummary(
        id=task.id, subject=task.subject, status=task.status,
        owner=task.owner, blocked_by=task.blocked_by,
    )
    assert summary.id == "1"
    assert summary.status == TaskStatus.PENDING
    assert summary.blocked_by == ["2"]


def test_task_file_json_uses_snake_case():
    # 任务文件 JSON 与源码一致用 snake_case(spec §3.2 camelCase 样例仅 Kode 对照)
    dumped = Task(id="1", subject="s", description="d", active_form="f").model_dump_json()
    assert '"active_form"' in dumped
    assert '"blocked_by"' in dumped
    assert "blockedBy" not in dumped


# ---- 正常图 ----

def test_empty_graph_is_valid():
    result = validate_task_graph([])
    assert result.valid
    assert result.duplicate_task_ids == []
    assert result.missing_dependencies == []
    assert result.cycles == []


def test_linear_dependency_graph_is_valid():
    tasks = [_task("1", blocks=["2"]), _task("2", blocks=["3"]), _task("3")]
    result = validate_task_graph(tasks)
    assert result.valid
    assert result.duplicate_task_ids == []
    assert result.missing_dependencies == []
    assert result.cycles == []


# ---- 环检测 ----

def test_triangle_cycle_detected_and_normalized():
    tasks = [_task("1", blocks=["2"]), _task("2", blocks=["3"]), _task("3", blocks=["1"])]
    result = validate_task_graph(tasks)
    assert not result.valid
    assert result.cycles == [["1", "2", "3", "1"]]  # 首成员重复于尾


def test_cycle_normalization_lexicographic_start():
    # 2→3→1→2 与 1→2→3→1 是同一环:只报告字典序最小起点的一个规范形式
    tasks = [_task("2", blocks=["3"]), _task("3", blocks=["1"]), _task("1", blocks=["2"])]
    result = validate_task_graph(tasks)
    assert result.cycles == [["1", "2", "3", "1"]]


def test_self_loop_detected():
    # 自环只能经外部文件编辑产生(mutation 时拒绝);诊断层照实报告
    tasks = [_task("1", blocks=["1"])]
    result = validate_task_graph(tasks)
    assert not result.valid
    assert result.cycles == [["1", "1"]]


def test_self_loop_via_blocked_by():
    # blocked_by 侧自环同样检出
    tasks = [_task("1", blocked_by=["1"])]
    result = validate_task_graph(tasks)
    assert not result.valid
    assert result.cycles == [["1", "1"]]


def test_two_disjoint_cycles_reported_in_order():
    # 两个不相交环同时报告,顺序按任务列表序确定
    tasks = [
        _task("1", blocks=["2"]), _task("2", blocks=["1"]),
        _task("3", blocks=["4"]), _task("4", blocks=["3"]),
    ]
    result = validate_task_graph(tasks)
    assert not result.valid
    assert result.cycles == [["1", "2", "1"], ["3", "4", "3"]]


def test_deep_chain_has_no_cycle():
    tasks = [_task(str(i), blocks=[str(i + 1)]) for i in range(1, 10)] + [_task("10")]
    result = validate_task_graph(tasks)
    assert result.valid
    assert result.cycles == []


def test_tail_back_to_head_forms_cycle():
    tasks = [_task(str(i), blocks=[str(i + 1)]) for i in range(1, 10)] + [_task("10", blocks=["1"])]
    result = validate_task_graph(tasks)
    assert not result.valid
    assert result.cycles == [["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "1"]]


# ---- missing / duplicate / asymmetric ----

def test_missing_dependency_both_declarations():
    tasks = [_task("1", blocks=["9"]), _task("2", blocked_by=["8"])]
    result = validate_task_graph(tasks)
    assert not result.valid
    assert result.missing_dependencies == [
        MissingTaskDependency("1", "9", "blocks"),
        MissingTaskDependency("2", "8", "blocked_by"),
    ]


def test_missing_dependency_deduped_within_task():
    # 同任务重复声明同一缺失目标只记一次(blocks/blocked_by 各算一条)
    tasks = [_task("1", blocks=["9", "9"]), _task("2", blocked_by=["8", "8"])]
    result = validate_task_graph(tasks)
    assert result.missing_dependencies == [
        MissingTaskDependency("1", "9", "blocks"),
        MissingTaskDependency("2", "8", "blocked_by"),
    ]


def test_duplicate_task_ids():
    tasks = [_task("1"), _task("1")]
    result = validate_task_graph(tasks)
    assert not result.valid
    assert result.duplicate_task_ids == ["1"]


def test_asymmetric_dependency_is_diagnostic_only():
    # 仅 blocks 单端声明(目标缺 blocked_by)→ 非致命,valid 不受影响
    tasks = [_task("1", blocks=["2"]), _task("2")]
    result = validate_task_graph(tasks)
    assert result.valid
    assert result.asymmetric_dependencies == [("1", "2", ["blocked_by"])]


def test_asymmetric_reverse_declaration():
    # 仅 blocked_by 单端声明(源头缺 blocks)→ 同样非致命
    tasks = [_task("2", blocked_by=["1"]), _task("1")]
    result = validate_task_graph(tasks)
    assert result.valid
    assert result.asymmetric_dependencies == [("1", "2", ["blocks"])]


def test_symmetric_declaration_no_asymmetric():
    # 双端对称声明:blocks + blocked_by 同时声明同一边 → 无不对称
    tasks = [_task("1", blocks=["2"]), _task("2", blocked_by=["1"])]
    result = validate_task_graph(tasks)
    assert result.valid
    assert result.asymmetric_dependencies == []


def test_whitespace_ids_normalized():
    # 镜像 Kode cloneTask:id 与依赖项 trim、空串跳过;空 id 记入 duplicate
    tasks = [
        _task(" 1 ", blocks=[" 2 "]),
        _task("2"),
        _task("1"),       # trim 后与 " 1 " 同 id → duplicate
        _task("   "),     # 空 id → duplicate
        _task("3", blocks=[" "]),  # trim 后空串依赖跳过,不记 missing
    ]
    result = validate_task_graph(tasks)
    assert result.duplicate_task_ids == ["1", ""]
    assert result.missing_dependencies == []
    assert result.cycles == []
    assert result.asymmetric_dependencies == [("1", "2", ["blocked_by"])]
    assert not result.valid  # duplicate 非空 → 无效


# ---- 纯函数 ----

def test_validate_is_pure_and_deterministic():
    # 无 IO:直接构造 Task 喂入;同输入两次调用结果全等(dataclass 值相等)
    tasks = [_task("1", blocks=["2"]), _task("2", blocks=["3"]), _task("3", blocks=["1"])]
    first = validate_task_graph(tasks)
    second = validate_task_graph(tasks)
    assert first == second
    assert first.cycles == second.cycles == [["1", "2", "3", "1"]]
