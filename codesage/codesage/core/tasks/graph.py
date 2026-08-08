"""只读图验证:对任务列表做四类诊断 —— 纯函数、无 IO、确定性。

镜像 spec §5.3 与 Kode taskGraph.ts 的 createGraphIndex + normalizeCycle:
先按 Kode cloneTask 归一化(id 与依赖项 trim、空串跳过、重复项去重),
再 edges 从 blocks + blocked_by 双声明归一(同一条边两个声明合并去重);
环检测 DFS 三色标记(visiting/visited),环规范化到字典序最小起点
且首成员重复于尾(便于渲染);
valid = duplicate_task_ids / missing_dependencies / cycles 三项全空,
asymmetric_dependencies(仅单端声明)只诊断不判无效。
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import Task


@dataclass(slots=True)
class MissingTaskDependency:
    """一条声明了但目标任务不存在的依赖。declaration: "blocks" | "blocked_by"。"""

    task_id: str
    dependency_id: str
    declaration: str


@dataclass(slots=True)
class TaskGraphValidation:
    """一次 validate_task_graph 的完整结果。"""

    valid: bool
    duplicate_task_ids: list[str]  # 同 ID 出现多次(归一化后)
    missing_dependencies: list[MissingTaskDependency]  # 引用不存在的任务
    cycles: list[list[str]]  # 每环重复首成员于尾(镜像 Kode normalizeCycle)
    asymmetric_dependencies: list[tuple[str, str, list[str]]]  # 仅单端声明(非致命);第三元素 = 缺失的声明端


def _normalize_cycle(cycle: list[str]) -> list[str]:
    """环规范化:旋转到字典序最小起点,首成员重复于尾(镜像 Kode normalizeCycle)。"""
    start = min(range(len(cycle)), key=lambda i: cycle[i:])
    norm = cycle[start:] + cycle[:start]
    norm.append(norm[0])
    return norm


def validate_task_graph(tasks: list[Task]) -> TaskGraphValidation:
    """纯函数、无 IO、确定性 —— 测试与 13 的 supervisor 共用。

    0. 归一化(镜像 Kode cloneTask):id 与依赖项 trim、空串跳过;空 id 记入 duplicate
    1. duplicate_task_ids:同 ID 出现多次,按出现顺序去重后收集
    2. missing_dependencies:blocks/blocked_by 声明了但目标不存在的依赖(含声明来源)
    3. cycles:DFS 三色标记检出,环规范化后按发现顺序
    4. asymmetric_dependencies:仅单端声明(blocks 或 blocked_by 缺一端),非致命
    """
    # 0) 归一化 + 1) ID 去重(镜像 Kode uniqueIds)
    seen: set[str] = set()
    duplicate_task_ids: list[str] = []
    dup_recorded: set[str] = set()
    normed: list[tuple[str, list[str], list[str]]] = []  # (id, blocks, blocked_by)
    by_id: dict[str, tuple[str, list[str], list[str]]] = {}
    for task in tasks:
        tid = task.id.strip()
        if not tid:
            # 空 id 是退化任务:记入 duplicate,不参与建图
            if "" not in dup_recorded:
                duplicate_task_ids.append("")
                dup_recorded.add("")
            continue
        if tid in seen and tid not in dup_recorded:
            duplicate_task_ids.append(tid)
            dup_recorded.add(tid)
        seen.add(tid)
        blocks = list(dict.fromkeys(d.strip() for d in task.blocks if d.strip()))
        blocked_by = list(dict.fromkeys(d.strip() for d in task.blocked_by if d.strip()))
        entry = (tid, blocks, blocked_by)
        normed.append(entry)
        by_id[tid] = entry  # 重复 id 以最后一条为准(重复已另行标记)

    # 2) 双声明归一:同一对 (source, target)(source blocks target)合并去重;
    #    missing 按任务内声明去重;幻影节点(引用缺失任务)不进环边,仅记 missing
    edges: dict[str, set[str]] = {}
    missing: list[MissingTaskDependency] = []
    for tid, blocks, blocked_by in normed:
        targets = edges.setdefault(tid, set())
        for dep in blocks:
            if dep not in seen:
                missing.append(MissingTaskDependency(tid, dep, "blocks"))
                continue
            targets.add(dep)
        for dep in blocked_by:
            if dep not in seen:
                missing.append(MissingTaskDependency(tid, dep, "blocked_by"))
                continue
            edges.setdefault(dep, set()).add(tid)

    # 3) 环检测:DFS 三色标记(visiting=1 / visited=2),邻接按字典序保证确定性
    # ponytail: 递归 DFS(Python 递归限 ~990)——规范场景(数百任务)内安全;
    # 任务量接近递归深度时改显式栈迭代化,输出不变
    cycles: list[list[str]] = []
    color: dict[str, int] = {}
    path: list[str] = []

    def dfs(node: str) -> None:
        color[node] = 1
        path.append(node)
        for nxt in sorted(edges.get(node, ())):
            if nxt not in color:
                dfs(nxt)
            elif color[nxt] == 1:  # 回边:nxt 在当前路径上 → 成环
                cycle = path[path.index(nxt):]
                cycles.append(_normalize_cycle(cycle))
        path.pop()
        color[node] = 2

    for tid, _, _ in normed:
        if tid not in color:
            dfs(tid)

    # 4) 非对称依赖:边的两端节点都必须存在(missing 已另行诊断),仅单端声明则记录
    asymmetric: list[tuple[str, str, list[str]]] = []
    checked: set[tuple[str, str]] = set()
    for tid, blocks, blocked_by in normed:
        for dep in blocks:
            edge = (tid, dep)
            if edge in checked or dep not in by_id:
                continue
            checked.add(edge)
            if tid not in by_id[dep][2]:  # 目标任务的 blocked_by 缺这条声明
                asymmetric.append((tid, dep, ["blocked_by"]))
        for dep in blocked_by:
            edge = (dep, tid)
            if edge in checked or dep not in by_id:
                continue
            checked.add(edge)
            if tid not in by_id[dep][1]:  # 源头的 blocks 缺这条声明
                asymmetric.append((dep, tid, ["blocks"]))

    return TaskGraphValidation(
        valid=not duplicate_task_ids and not missing and not cycles,
        duplicate_task_ids=duplicate_task_ids,
        missing_dependencies=missing,
        cycles=cycles,
        asymmetric_dependencies=asymmetric,
    )
