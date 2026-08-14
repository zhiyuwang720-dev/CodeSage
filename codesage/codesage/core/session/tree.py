"""Tree view (phase 12 S2): parent-chain tree, lane parsing, linear projection.

Pure functions over parsed entries (spec §4): message entries form the tree
nodes keyed by parent chain (roots = parentless messages, possibly several —
each branch starts at its own root); lane entries resolve into a
{name: leaf} mapping (active lane = last valid lane entry, §3.4);
bookmarks/summaries hang off target nodes by their entry/leaf field — a
read-side mapping, message entries are never mutated (§4.1). Application-state
entries never enter linear_messages (PI-10 partial, §3.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..messages import SessionMessage
from .entry import SessionEntry


@dataclass(slots=True)
class TreeNode:
    """树节点 = 一条 message entry;书签/摘要按字段挂载(读端映射,不改消息)。"""

    entry: SessionEntry
    children: list["TreeNode"] = field(default_factory=list)
    bookmarks: list[SessionEntry] = field(default_factory=list)  # data["entry"] == 本节点 uuid
    summaries: list[SessionEntry] = field(default_factory=list)  # data["leaf"] == 本节点 uuid


@dataclass(slots=True)
class TreeView:
    """build_tree 结果:根列表 + uuid 索引 + lane 映射(活跃 lane = 最后一条合法 lane entry)。"""

    roots: list[TreeNode]  # parent 为 None 的消息(文件可能多条根 = 分支起点)
    nodes: dict[str, TreeNode]  # uuid 索引
    lanes: dict[str, str]  # {name: leaf_uuid};同名后者胜(§3.4 指针推进)
    active_lane: str  # 最后一条合法 lane entry 的 name;缺 lane(旧文件)→ "main"
    active_leaf: str | None  # 其 leaf;缺 lane → None(调用方兜底最后一条消息)


def _parse_lanes(entries: list[SessionEntry]) -> tuple[dict[str, str], str, str | None]:
    """按出现顺序遍历 lane entry 得 {name: leaf} 映射,同名后者胜;活跃 lane =
    最后一条合法 lane entry(语义损坏行缺字段 → 跳过,与 Session._active_lane
    同规则,R4)。"""
    lanes: dict[str, str] = {}
    active_name, active_leaf = "main", None
    for e in entries:
        if e.type != "lane":
            continue
        name, leaf = e.data.get("name"), e.data.get("leaf")
        if name is None or leaf is None:
            continue  # 语义损坏:跳过,退回上一个合法 lane
        lanes[name] = leaf
        active_name, active_leaf = name, leaf
    return lanes, active_name, active_leaf


def build_tree(entries: list[SessionEntry]) -> TreeView:
    """message 按 parent 链组织节点(uuid 索引);根 = parent 为 None 的消息
    (或 parent 悬空 —— 手写坏文件不丢节点);书签/摘要按字段挂到目标节点。"""
    nodes = {e.uuid: TreeNode(entry=e) for e in entries if e.type == "message"}
    for e in entries:  # 应用状态挂载(读端映射,不修改消息 entry)
        if e.type == "bookmark":
            node = nodes.get(e.data.get("entry"))
            if node is not None:
                node.bookmarks.append(e)
        elif e.type == "branch_summary":
            node = nodes.get(e.data.get("leaf"))
            if node is not None:
                node.summaries.append(e)
        # operation 无 entry/leaf 字段,无处可挂 —— 按位置消费(find_open_operations,S3)
    roots: list[TreeNode] = []
    for node in nodes.values():
        parent = node.entry.parent
        if parent is None or parent not in nodes:
            roots.append(node)
        else:
            nodes[parent].children.append(node)
    lanes, active_name, active_leaf = _parse_lanes(entries)
    return TreeView(roots, nodes, lanes, active_name, active_leaf)


def linear_messages(
    entries: list[SessionEntry], lane: str | None = None
) -> list[SessionMessage]:
    """沿 lane 的 leaf 从根(无 parent)沿 parent 链走到 leaf,投影为 SessionMessage
    列表(丢弃应用状态 entry)。lane=None → 活跃 lane(§4.3);未知 lane / 悬空
    leaf → 退回最后一条消息(与 04 load 兜底一致)。旧文件单 lane 行为 = 04 load()。"""
    lanes, active_name, _ = _parse_lanes(entries)
    leaf = lanes.get(lane or active_name)  # 未知 lane → None → 兜底
    by_uuid = {e.uuid: e for e in entries if e.type == "message"}
    if leaf not in by_uuid:
        leaf = next((e.uuid for e in reversed(entries) if e.type == "message"), None)
    chain: list[SessionEntry] = []
    seen: set[str] = set()
    cur = leaf
    while cur is not None and cur in by_uuid and cur not in seen:
        seen.add(cur)  # 防手写坏文件成环(与 Session._chain 同 seen-set 模式)
        chain.append(by_uuid[cur])
        cur = by_uuid[cur].parent
    chain.reverse()
    return [e.as_message() for e in chain]  # chain 全为 message entry,恒非 None
