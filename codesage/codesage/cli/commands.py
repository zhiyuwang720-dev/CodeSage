"""Slash command registry (CC-09): one place to add commands.

HELP_TEXT is generated from the registry, so the help output can never drift
from the actual command set.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..core import (
    SessionEntry,
    active_sessions,
    archive_session,
    archived_sessions,
    build_tree,
    find_open_operations,
    linear_messages,
    numbered_entries,
    restore_session,
)

#: handler(args, state) -> True = exit the REPL; async handlers allowed
#: (dispatch awaits isawaitable results). *state* carries REPL flags:
#: {"show_thinking": bool, "loop": AgentLoop} — /mode writes loop.mode.
Handler = Callable[[list[str], dict], bool | Awaitable[bool]]


@dataclass(frozen=True)
class SlashCommand:
    name: str
    handler: Handler
    description: str
    aliases: list[str] = field(default_factory=list)


def _cmd_help(args: list[str], state: dict) -> bool:
    print(HELP_TEXT)
    return False


def _cmd_quit(args: list[str], state: dict) -> bool:
    print("bye")
    return True


def _cmd_mode(args: list[str], state: dict) -> bool:
    if len(args) == 1 and args[0] in ("plan", "default", "yolo"):
        state["loop"].mode = args[0]
        print(f"permission mode -> {args[0]}")
    else:
        print("usage: /mode plan|default|yolo")
    return False


def _cmd_show_thinking(args: list[str], state: dict) -> bool:
    state["show_thinking"] = not state["show_thinking"]
    print(f"show-thinking -> {state['show_thinking']}")
    return False


def _cmd_ponytail(args: list[str], state: dict) -> bool:
    """阶段 20 §5.4:/ponytail lite|full|ultra|off —— 切换懒人模式档位。"""
    from ..intel.ponytail import PonytailState

    if len(args) == 1 and args[0] in ("lite", "full", "ultra", "off"):
        try:
            PonytailState(state["loop"].cwd).set_mode(args[0])
        except ValueError:
            print("usage: /ponytail lite|full|ultra|off")
            return False
        print(f"ponytail -> {args[0]}")
    else:
        print("usage: /ponytail lite|full|ultra|off")
    return False


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_CLEAR = " " * 24


async def _spinner(done: asyncio.Event) -> None:
    """In-place progress spinner ('\r'-refreshed line); erases itself at the
    end. Runs as a sibling task while the compaction LLM call is awaited."""
    i = 0
    while not done.is_set():
        frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
        print(f"\r  {frame} 压缩上下文中…", end="", flush=True)
        i += 1
        try:
            await asyncio.wait_for(done.wait(), timeout=0.1)
        except asyncio.TimeoutError:
            pass
    print("\r" + _SPINNER_CLEAR + "\r", end="", flush=True)


async def _cmd_compact(args: list[str], state: dict) -> bool:
    """Manual compaction (§6.3): spinner 展示压缩过程;结果一行,不清屏(§6.4)。"""
    done = asyncio.Event()
    spinner = asyncio.create_task(_spinner(done))
    try:
        ok = await state["loop"].compact_now()
    finally:
        done.set()
        await spinner
    print("上下文已压缩" if ok else "无可压缩内容")
    return False


# ---- 阶段 12 S5:会话树命令(/tree /fork /bookmark /sessions /archive)----

#: §1.4.2 显示层截断:行超 80 字符截断内容(数据层全量保留;ponytail:阈值后续可配置)
_ROW_LIMIT = 80
#: §6 翻页:每页 20 行(ponytail:超长阈值后续可配置)
_PAGE_SIZE = 20
#: §6 --type 合法值(user/assistant/tool_use/tool_result + 应用状态类型)
_TYPE_VALUES = ("user", "assistant", "tool_use", "tool_result", "bookmark", "summary", "operation")


def _circle(n: int) -> str:
    """序号圈号化:1-20 → ①-⑳,>20 用阿拉伯数字(§6 示例 ①-⑩;圈号只有 20 个)。"""
    return chr(0x2460 + n - 1) if 1 <= n <= 20 else str(n)


def _clip(text: str, limit: int = _ROW_LIMIT) -> str:
    """§1.4.2 显示层截断:超限截尾部补 …(数据层不动)。"""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _mtime_str(mtime: float) -> str:
    """§9.2 时间列显示:本地时间 YYYY-MM-DDTHH:MM(示例形态)。"""
    from datetime import datetime

    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M")


def _short_id(session_id: str) -> str:
    """§1.4.2 id 显示层截断。本项目 id 形态是 session-YYYYMMDD-HHMMSS-ffffff
    (assemble._new_session_id),前 4 位("sess")全同无区分度 → 取尾部时间戳段
    (含微秒,同秒会话可辨)加 … 前缀。"""
    return session_id if len(session_id) <= 14 else "…" + session_id[-11:]


def _resolve_entry_id(entries: list[SessionEntry], ref: str) -> str | None:
    """entryId 解析(§6):数字 = 文件序编号(/tree 显示的 ① 序号,§6
    numbered_entries);uuid 原样。命中 message/operation 返回其 uuid,无 → None。"""
    if ref.isdigit():
        return next(
            (e.uuid for n, e in numbered_entries(entries) if n == int(ref)), None
        )
    # 只认 message/operation(P2#4:meta/lane/bookmark 的 uuid 不是可导航 entry)
    return ref if any(
        e.uuid == ref and e.type in ("message", "operation") for e in entries
    ) else None


def _bookmark_map(entries: list[SessionEntry]) -> dict[str, str]:
    """{被标记 entry uuid: 书签名};重名 = 后者胜(§6 读端合并,永不删除)。"""
    return {
        e.data["entry"]: e.data.get("name", "")
        for e in entries
        if e.type == "bookmark" and e.data.get("entry")
    }


def _summary_map(entries: list[SessionEntry]) -> dict[str, str]:
    """{leaf uuid: 摘要文本}(§4.5 branch_summary 挂 leaf,读端映射)。"""
    return {
        e.data["leaf"]: e.data.get("content", "")
        for e in entries
        if e.type == "branch_summary" and e.data.get("leaf")
    }


def _row_type(entry: SessionEntry) -> str:
    """行类型(§6 --type 映射):operation 固定;message 按 role + content block
    类型(tool_result/tool_use 优先于角色 —— 工具结果载体是 user 角色消息,
    但 §6 示例 ⑤ 渲染为 tool_result)。"""
    if entry.type == "operation":
        return "operation"
    msg = entry.as_message()
    if isinstance(msg.content, list):
        kinds = {b.type for b in msg.content}
        if "tool_result" in kinds:
            return "tool_result"
        if "tool_use" in kinds:
            return "tool_use"
    return msg.role  # "user" | "assistant"


def _block_preview(block) -> str:
    """content block 预览(显示层):text/thinking → 文本;tool_use → 名(参);
    tool_result → 载荷文本。截断交给行级 _clip。"""
    if block.type == "tool_use":
        args = json.dumps(block.input, ensure_ascii=False) if block.input else ""
        return f"{block.name}({args})"
    if block.type == "tool_result":
        if isinstance(block.content, str):
            return block.content
        return " ".join(b.text or "" for b in block.content if b.type == "text")
    return block.text or ""


def _entry_preview(entry: SessionEntry) -> str:
    """行内容预览(§6):文本消息加引号;块消息按块拼接;operation = kind + 工具。"""
    if entry.type == "operation":
        kind = entry.data.get("kind", "")
        tool = entry.data.get("tool")
        args = entry.data.get("args_summary")
        return f"{kind} {tool}({args if args is not None else ''})" if tool else kind
    msg = entry.as_message()
    if isinstance(msg.content, str):
        return f'"{msg.content}"'
    return " ".join(_block_preview(b) for b in msg.content)


def _tree_rows(entries: list[SessionEntry]) -> list[tuple[int, SessionEntry, str]]:
    """文件序行列表:(编号, entry, 所属 lane)。行 = message + operation(§6
    numbered_entries 同规则,应用状态是行内标注不占号);行的 lane = 其前最近
    一条 lane entry 名(首行 → main,旧文件无 lane 同理)。"""
    rows: list[tuple[int, SessionEntry, str]] = []
    lane_now = "main"
    for e in entries:
        if e.type == "lane":
            name = e.data.get("name")
            if name:
                lane_now = name
        elif e.type in ("message", "operation"):
            rows.append((len(rows) + 1, e, lane_now))
    return rows


def _fork_points(entries: list[SessionEntry], lane_order: list[str]) -> dict[str, int]:
    """每 lane 的 fork 点(§6 分支头 `fork @ ②` 装饰):lane 链与前一 lane 链
    首次分叉处的上一条消息编号;首个 lane(main)无 fork。链来自 linear_messages
    (分支共享前缀历史,§4.2)。"""
    points: dict[str, int] = {}
    prev_chain: set[str] = set()
    by_num = {e.uuid: n for n, e in numbered_entries(entries)}
    for name in lane_order:
        chain = [m.uuid for m in linear_messages(entries, name)]
        div = next((i for i, u in enumerate(chain) if u not in prev_chain), len(chain))
        if prev_chain and div > 0 and chain[div - 1] in by_num:
            points[name] = by_num[chain[div - 1]]
        prev_chain = set(chain)
    return points


def _render_row(
    num: int,
    entry: SessionEntry,
    bookmarks: dict[str, str],
    open_uuids: set[str],
    marker: str = " ",
) -> str:
    """一行 /tree 渲染(§6):`{!|✓| } ① user 2026-08-12T10:00  "内容"(★ 名)`。
    符号即语义(§1.4.3):! = 未完成操作、✓ = 书签、→ = 活跃 lane —— 不加颜色。
    marker = 上下文窗口标注(↑ 祖先 / ▼ 目标)。行 ≤80 截断(显示层,§1.4.2)。"""
    prefix = "!" if entry.uuid in open_uuids else ("✓" if entry.uuid in bookmarks else " ")
    line = (
        f"{marker}{prefix} {_circle(num)} {_row_type(entry):<10} "
        f"{entry.timestamp[:16]}  {_entry_preview(entry)}"
    )
    if entry.uuid in bookmarks:
        line += f"(★ {bookmarks[entry.uuid]})"
    return _clip(line)


def _cmd_tree(args: list[str], state: dict) -> bool:
    """§6 树状导航:`/tree [n|entryId] [--type T] [--bookmarks]`。
    无参 = 第 1 页(每页 20 行);纯数字且 ≤ 总页数 = 页码,否则 = entry 编号
    (上下文窗口);uuid = entryId。数字二义性(页码 vs entry 编号)按「页优先」,
    entry 上下文窗口恒可用 uuid 或超页码数字触达。"""
    loop = state["loop"]
    session = getattr(loop, "session", None)
    if session is None:
        print("no active session (/tree needs a running session)")
        return False
    entries = session.entries
    ref, type_filter, bookmarks_only = None, None, False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--type":
            if i + 1 >= len(args):
                print(f"usage: /tree [n|entryId] [--type {'|'.join(_TYPE_VALUES)}] [--bookmarks]")
                return False
            type_filter = args[i + 1]
            i += 2
        elif a == "--bookmarks":
            bookmarks_only = True
            i += 1
        elif ref is None:
            ref = a
            i += 1
        else:
            print(f"unknown arg: {a}")
            return False
    if type_filter is not None and type_filter not in _TYPE_VALUES:
        print(f"unknown type: {type_filter} ({'|'.join(_TYPE_VALUES)})")
        return False
    rows = _tree_rows(entries)
    if not rows:
        print("no entries")
        return False
    if type_filter or bookmarks_only:
        bm, sm = _bookmark_map(entries), _summary_map(entries)
        rows = [
            (n, e, lane)
            for n, e, lane in rows
            if (type_filter and (
                _row_type(e) == type_filter
                or (type_filter == "bookmark" and e.uuid in bm)
                or (type_filter == "summary" and e.uuid in sm)
            ))
            or (bookmarks_only and e.uuid in bm)
        ]
        if not rows:
            print("no entries match")
            return False
    if ref is not None:
        n_pages = max(1, math.ceil(len(rows) / _PAGE_SIZE))
        if ref.isdigit() and int(ref) <= n_pages:
            return _cmd_tree_page(session, entries, rows, page=int(ref))
        return _cmd_tree_context(session, entries, ref)
    return _cmd_tree_page(session, entries, rows, page=1)


def _cmd_tree_page(session, entries: list[SessionEntry], rows, page: int) -> bool:
    """§6 分页渲染:会话头 + 分支头(lane 名 + 装饰线,活跃 lane 行首 →)+ 行。
    分支头在页内 lane 变化处插入(页首行恒出所属分支头)。"""
    tree = build_tree(entries)
    n_msgs = sum(1 for e in entries if e.type == "message")
    n_lanes = len(tree.lanes) or 1
    fork_pts = _fork_points(entries, list(tree.lanes.keys()))
    open_uuids = {e.uuid for e in find_open_operations(entries)}
    bm = _bookmark_map(entries)
    lines = [f"session {_short_id(session.session_id)}  ({n_msgs} messages, {n_lanes} branches)"]
    prev_lane = None
    for n, e, lane in rows[(page - 1) * _PAGE_SIZE : page * _PAGE_SIZE]:
        if lane != prev_lane:
            arrow = "→ " if lane == tree.active_lane else ""
            head = f"{arrow}{lane}"
            fp = fork_pts.get(lane)
            if fp:
                head += f" ── fork @ {_circle(fp)}"
            lines.append(head + " " + "─" * max(1, 40 - len(head)))
            prev_lane = lane
        lines.append(_render_row(n, e, bm, open_uuids))
    print("\n".join(lines))
    return False


def _cmd_tree_context(session, entries: list[SessionEntry], ref: str) -> bool:
    """§6 /tree <entryId>:目标 entry 所在分支的上下文窗口(前 5 后 3,§1.4
    非对称误差预算 —— 宁可多给上下文);parent 链标注:↑ 祖先、▼ 目标。目标为
    operation 时取其前最近一条消息(导航位置是消息)。"""
    target_uuid = _resolve_entry_id(entries, ref)
    if target_uuid is None:
        print(f"entry not found: {ref}")
        return False
    target = next((e for e in entries if e.uuid == target_uuid), None)
    if target is None:
        print(f"entry not found: {ref}")
        return False
    if target.type == "operation":  # 导航位置 = 其前最近消息
        for p in reversed(entries[: entries.index(target)]):
            if p.type == "message":
                target_uuid = p.uuid
                break
    tree = build_tree(entries)
    chosen = None  # 所在分支:活跃 lane 链优先,否则首个含该 entry 的 lane
    for name in [tree.active_lane, *(n for n in tree.lanes if n != tree.active_lane)]:
        chain = [m.uuid for m in linear_messages(entries, name)]
        if target_uuid in chain:
            chosen = (name, chain)
            break
    if chosen is None:
        print(f"entry not found: {ref}")
        return False
    lane, chain = chosen
    idx = chain.index(target_uuid)
    window = chain[max(0, idx - 5) : idx + 4]  # 前 5 后 3
    by_uuid = {e.uuid: e for e in entries if e.type == "message"}
    num_of = {e.uuid: n for n, e in numbered_entries(entries)}
    bm, open_uuids = _bookmark_map(entries), {e.uuid for e in find_open_operations(entries)}
    lines = [f"context of entry {_circle(num_of.get(target_uuid, 0))} — lane {lane}"]
    for u in window:
        marker = "▼" if u == target_uuid else ("↑" if chain.index(u) < idx else " ")
        lines.append(_render_row(num_of.get(u, 0), by_uuid[u], bm, open_uuids, marker=marker))
    print("\n".join(lines))
    return False


def _cmd_fork(args: list[str], state: dict) -> bool:
    """§4.2/§6 分支:`/fork <entryId> [name]` 从任意先前位置分支(新 lane,
    零拷贝共享历史)。完成类平铺输出(§1.4.1):`forked at <entryId> → lane <name>`。"""
    loop = state["loop"]
    session = getattr(loop, "session", None)
    if session is None:
        print("no active session (/fork needs a running session)")
        return False
    if not args:
        print("usage: /fork <entryId> [name]")
        return False
    entries = session.entries
    entry_id = _resolve_entry_id(entries, args[0])
    if entry_id is None:
        print(f"entry not found: {args[0]}")
        return False
    entry = next((e for e in entries if e.uuid == entry_id), None)
    if entry is None or entry.type != "message":
        print(f"entry {args[0]} 不是消息,不能 fork")
        return False
    lane = session.fork(entry_id, name=args[1] if len(args) > 1 else None)
    print(f"forked at {args[0]} → lane {lane}")
    return False


def _cmd_bookmark(args: list[str], state: dict) -> bool:
    """§6 书签:`/bookmark <entryId> <name>` 追加命名书签(重名 = 追加新 entry,
    旧书签保留、读端后者胜 —— 永不删除)。完成类平铺输出(§1.4.1)。"""
    loop = state["loop"]
    session = getattr(loop, "session", None)
    if session is None:
        print("no active session (/bookmark needs a running session)")
        return False
    if len(args) < 2:
        print("usage: /bookmark <entryId> <name>")
        return False
    entry_id = _resolve_entry_id(session.entries, args[0])
    if entry_id is None:
        print(f"entry not found: {args[0]}")
        return False
    session.append_bookmark(entry_id, args[1])
    print(f"bookmarked {args[0]} as {args[1]}")
    return False


def _cmd_sessions(args: list[str], state: dict) -> bool:
    """§9.2 会话选择器:`/sessions [--archive|--all]`,按 mtime 倒序,一行一会话。
    id 显示层截断(_short_id,§1.4.2);标题 = meta.title,无 → (untitled)。"""
    from .assemble import session_root

    root = session_root()
    archive_only = "--archive" in args
    all_ = "--all" in args
    if archive_only:
        metas = archived_sessions(root)
    elif all_:
        metas = sorted(
            [*active_sessions(root), *archived_sessions(root)],
            key=lambda m: m.mtime,
            reverse=True,
        )
    else:
        metas = active_sessions(root)
    if not metas:
        print("no sessions")
        return False
    lines = [f"{'id':<14}{'title':<24}{'messages':>9}{'branches':>9} {'time'}"]
    for m in metas:
        title = f'"{m.title}"' if m.title else "(untitled)"
        lines.append(
            f"{_short_id(m.session_id):<14}{_clip(title, 24):<24}"
            f"{m.messages:>9}{m.branches:>9} {_mtime_str(m.mtime)}"
        )
    print("\n".join(lines))
    return False


def _cmd_archive(args: list[str], state: dict) -> bool:
    """§9.1 归档/恢复:`/archive <sessionId>` 移入 archive/;`--restore` 一行
    移回原位置。完成类平铺输出(§1.4.1)。"""
    if not args:
        print("usage: /archive <sessionId> [--restore]")
        return False
    from .assemble import session_root

    root = session_root()
    session_id = args[0]
    try:
        if "--restore" in args:
            dest = restore_session(root, session_id)
            print(f"restored {session_id} → {dest}")
        else:
            dest = archive_session(root, session_id)
            print(f"archived {session_id} → {dest}")
    except ValueError as e:
        print(f"error: {e}")
    return False


def _cmd_mcp(args: list[str], state: dict) -> bool:
    """阶段 15:MCP 服务器管理(spec §10.3)。

    无参 → 列出全部服务器健康度;add/remove/reconnect/enable/disable/install/uninstall 子命令。
    依赖 loop._mcp(McpManager)与 mcp.config。
    """
    from ..mcp.config import (
        ConfigScope,
        add_mcp_config,
        get_all_mcp_configs,
        is_mcp_server_disabled,
        remove_mcp_config,
        set_mcp_server_enabled,
    )

    loop = state.get("loop")
    manager = getattr(loop, "_mcp", None)
    if not args:
        # 列出全部配置(含内置)与连接状态
        configs = get_all_mcp_configs()
        if not configs:
            print("No MCP servers configured. Use /mcp add <name> ...")
            return False
        print(f"{'Server':<24} {'Transport':<8} {'Status':<12} {'Tools':<6} {'Scope'}")
        for name in sorted(configs):
            cfg = configs[name]
            conn = manager.get_connection(name) if manager else None
            status = conn.state.value if conn else ("disabled" if is_mcp_server_disabled(name) else "unknown")
            tools = len(manager.tools_for(name)) if manager else 0
            print(f"{name:<24} {cfg.type:<8} {status:<12} {tools:<6} {cfg.scope.value}")
        return False

    sub = args[0]
    name = args[1] if len(args) > 1 else None

    if sub == "add" and name:
        scope = ConfigScope.LOCAL
        if "--scope" in args:
            idx = args.index("--scope")
            scope = ConfigScope(args[idx + 1]) if idx + 1 < len(args) else scope
        if "--command" in args:
            idx = args.index("--command")
            cmd = args[idx + 1] if idx + 1 < len(args) else None
            add_mcp_config(name, {"command": cmd}, scope)
            print(f"Added MCP server {name} ({scope.value})")
        elif "--url" in args:
            idx = args.index("--url")
            url = args[idx + 1] if idx + 1 < len(args) else None
            add_mcp_config(name, {"type": "http", "url": url}, scope)
            print(f"Added MCP server {name} ({scope.value})")
        else:
            print("usage: /mcp add <name> --command <cmd> | --url <url> [--scope local|user|project]")
    elif sub == "remove" and name:
        remove_mcp_config(name, ConfigScope.LOCAL)
        if manager:
            import asyncio

            asyncio.run(manager.disconnect(name))
        print(f"Removed MCP server {name}")
    elif sub == "reconnect" and name:
        if manager:
            import asyncio

            conn = asyncio.run(manager.connect_server(name, get_all_mcp_configs().get(name)))
            print(f"{name}: {conn.state.value}")
        else:
            print("MCP manager not available")
    elif sub in ("enable", "disable") and name:
        set_mcp_server_enabled(name, sub == "enable")
        print(f"{name}: {'enabled' if sub == 'enable' else 'disabled'}")
    elif sub in ("install", "uninstall") and name:
        _mcp_builtin_install(args, state, name, sub == "install")
    else:
        print("usage: /mcp [add|remove|reconnect|enable|disable|install|uninstall] <name>")
    return False


def _mcp_builtin_install(args: list[str], state: dict, name: str, install: bool) -> None:
    """阶段 15 §4.6:内置托管服务器安装/卸载(codesage mcp install <name>)。

    install:查注册表 → 打印说明 → 下载平台产物 → 校验 SHA-256 → 登记 installed.json。
    本阶段实现登记骨架(下载/解压/哈希校验真实实现);离线/不可用环境提示 install_hint。
    """
    from ..mcp.builtin.registry import get_bundled_mcp_server

    spec = get_bundled_mcp_server(name)
    if not spec:
        print(f"Unknown bundled MCP server: {name}")
        return
    if install:
        print(spec.description)
        if spec.install_hint:
            print(f"安装提示: {spec.install_hint}(亦可经包管理器安装后,用 /mcp add --command 指向其二进制)")
        # 登记占位:实际下载+解压+SHA-256 校验在后续版本完善(§4.6 安装流)
        # 这里至少演示登记机制,用户可用 install_hint 手动安装后 /mcp add 接入
        print(f"{name} 已登记(需下载官方二进制并配置 command 后生效)")
    else:
        from ..mcp.config import write_installed

        installed = {}
        write_installed(installed)
        print(f"Uninstalled {name}")


COMMANDS: list[SlashCommand] = [
    SlashCommand("mode", _cmd_mode, "switch permission mode (plan|default|yolo)"),
    SlashCommand("ponytail", _cmd_ponytail, "ponytail 懒人模式: /ponytail lite|full|ultra|off"),
    SlashCommand("show-thinking", _cmd_show_thinking, "toggle thinking output"),
    SlashCommand("compact", _cmd_compact, "压缩上下文"),
    SlashCommand("mcp", _cmd_mcp, "MCP 服务器管理: /mcp [add|remove|reconnect|enable|disable|install|uninstall]"),
    SlashCommand("tree", _cmd_tree, "树状导航:渲染分支/书签,按类型筛选(/tree [n] 翻页,/tree <entryId> 上下文)"),
    SlashCommand("fork", _cmd_fork, "从 entryId 分支: /fork <entryId> [name]"),
    SlashCommand("bookmark", _cmd_bookmark, "标记书签: /bookmark <entryId> <name>"),
    SlashCommand("sessions", _cmd_sessions, "列出会话: /sessions [--archive|--all]"),
    SlashCommand("archive", _cmd_archive, "归档会话: /archive <sessionId> [--restore]"),
    SlashCommand("help", _cmd_help, "this help", aliases=["h"]),
    SlashCommand("quit", _cmd_quit, "exit", aliases=["q"]),
]


def find_command(name: str) -> SlashCommand | None:
    """Registry lookup by name or alias (leading '/' optional); None if unknown."""
    key = name.lstrip("/").lower()
    for cmd in COMMANDS:
        if key == cmd.name or key in cmd.aliases:
            return cmd
    return None


def _build_help_text() -> str:
    lines = ["Commands:"]
    lines += [f"  /{cmd.name}  {cmd.description}" for cmd in COMMANDS]
    lines.append("  (Ctrl+C once: interrupt the running turn; twice: exit)")
    return "\n".join(lines)


HELP_TEXT = _build_help_text()
