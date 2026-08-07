"""Hooks matching & config parsing (phase 09, S2): matcher, if-rule eval, settings parsing.

S2 交付:matcher 三级匹配(§2.3)、`if_rule_matches`(§2.4,复用 permissions/rules.py 零
新解析器)、settings.hooks 解析(§3.1/§3.2,含 http URL 白名单)与执行流审计 sink
(§8.1 HookJsonlSink 一行子类)。执行体(S3/S4/S10)与 HookManager(S5)在后续步骤。
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..permissions.audit import JsonlAuditSink
from ..permissions.rules import (
    READ_TOOLS,
    WRITE_TOOLS,
    bash_rule_matches,
    parse_rule,
    path_rule_matches,
    tool_rule_matches,
)
from ..tools import ToolRegistry, ToolError
from .types import EVENTS, MATCHER_IGNORED_EVENTS, HookSpec

logger = logging.getLogger("codesage.hooks")

#: 精确/管道分支:纯 [a-zA-Z0-9_|]+ 时按 `|` 分割逐个精确比较(§2.3 第 1 级)
_PIPE_ONLY = re.compile(r"^[a-zA-Z0-9_|]+$")


@dataclass(slots=True)
class HookGroup:
    """一个 matcher 组的解析结果(§3.1):组级 matcher + 组内钩子(配置顺序)。"""

    event: str
    matcher: str | None  # None = 匹配所有(§2.3 空或 *)
    hooks: list[HookSpec]


def match_matcher(matcher: str | None, value: str) -> bool:
    """matcher 三级匹配(§2.3,对齐 CC hooks.ts:1346-1381)。

    1. 空或 ``*`` → 匹配所有;
    2. 纯 [a-zA-Z0-9_|]+ → ``|`` 分割后逐个精确比较(OR);
    3. 其他 → 正则(search 语义);非法正则 → 永不匹配 + warning(§8.2)。
    """
    if not matcher or matcher == "*":
        return True
    if _PIPE_ONLY.match(matcher):
        return value in matcher.split("|")
    try:
        return re.search(matcher, value) is not None
    except re.error:
        logger.warning("invalid matcher regex %r: hook never matches (§2.3)", matcher)
        return False


def url_allowed(url: str, whitelist: list[str]) -> bool:
    """HTTP 钩子 URL 白名单(§4.9):``*`` 通配匹配;默认空列表 = 全禁。"""
    return any(fnmatch.fnmatch(url, entry) for entry in whitelist)


def _path_field(tool_name: str, tool_input: dict[str, Any]) -> Any:
    """文件工具的路径字段(§2.4):Read/Write/Edit 用 file_path(required),LS/Glob/Grep 用 path。"""
    if tool_name in ("Read", "Write", "Edit"):
        return tool_input.get("file_path")
    if tool_name in ("LS", "Glob", "Grep"):
        return tool_input.get("path")
    return None


def if_rule_matches(
    rule: str, tool_name: str, tool_input: dict[str, Any], registry: ToolRegistry
) -> bool:
    """hook 级 if 条件求值(§2.4,薄封装,复用 permissions/rules.py)。

    恒 false:工具不存在(registry.get 为 None)/ validate_input 抛错 / 文件工具路径字段缺失
    (不猜测 cwd)。Bash 走 bash_rule_matches(字符串前缀,非 CC 的 tree-sitter 语句级,差异
    如实标注,§2.4);文件内容规则按读/写集分组(rules.py 同语义);其他内容规则退化为工具级
    匹配(parsed_name == tool_name)。**裸路径 if 规则**(如 "/src/**",无 Tool() 前缀)→
    content 为 None 走 tool_rule_matches,路径串不可能等于/glob 命中工具名 → 恒 false。
    """
    tool = registry.get(tool_name)
    if tool is None:
        return False
    try:
        tool.validate_input(tool_input)
    except ToolError:
        # 校验失败 → 恒 false(§2.4);tools/base.py:95 契约只抛 ToolError
        return False
    parsed_name, content = parse_rule(rule)
    if parsed_name is None:
        return False
    if content is None:
        return tool_rule_matches(rule, tool_name)  # 裸工具名(精确/glob)→ 工具级
    if parsed_name in READ_TOOLS:
        if tool_name not in READ_TOOLS:
            return False
        path = _path_field(tool_name, tool_input)
    elif parsed_name in WRITE_TOOLS:
        if tool_name not in WRITE_TOOLS:
            return False
        path = _path_field(tool_name, tool_input)
    elif parsed_name == "Bash":
        if tool_name != "Bash":
            return False
        command = tool_input.get("command")
        return isinstance(command, str) and bash_rule_matches(content, command)
    else:
        return parsed_name == tool_name  # 其他内容规则(WebFetch/Skill…)→ 工具级
    if not isinstance(path, str) or not path:
        return False
    return path_rule_matches(content, Path(path))


def parse_hook_config(
    settings_hooks: Any,
    *,
    http_hook_urls: list[str] | None = None,
    warn: Callable[[str], None] | None = None,
) -> dict[str, list[HookGroup]]:
    """从 settings.hooks 解析 事件 → matcher 组 → 钩子(§3.1)。

    丢弃无效条目 + warning:未知事件名、非列表组、非 dict 组、matcher 类型错、http URL
    未命中 http_hook_urls 白名单(默认 [] 全禁,§4.9)、单钩子字段非法(经 HookSpec)。
    UserPromptSubmit/Stop 带 matcher → warning 不丢弃(§2.3);非可求值事件带 if →
    HookSpec.if_evaluable=False(S1 已置标志,这里透传)。settings.hooks 非 dict →
    error 日志,视为无钩子。结果仅含至少一个钩子的事件。
    """
    whitelist = http_hook_urls or []
    if not isinstance(settings_hooks, dict):
        logger.error(
            "settings.hooks must be an object, got %s: no hooks configured",
            type(settings_hooks).__name__,
        )
        return {}

    def _warn(msg: str) -> None:
        logger.warning(msg)
        if warn is not None:
            warn(msg)

    out: dict[str, list[HookGroup]] = {}
    for event, groups in settings_hooks.items():
        if event not in EVENTS:
            _warn(f"unknown hook event {event!r}: entry discarded")
            continue
        if not isinstance(groups, list):
            _warn(f"hooks for event {event!r} must be a list of groups: entry discarded")
            continue
        parsed_groups: list[HookGroup] = []
        for group in groups:
            if not isinstance(group, dict):
                _warn(f"hook group for event {event!r} must be an object: group discarded")
                continue
            raw_hooks = group.get("hooks")
            if not isinstance(raw_hooks, list):
                _warn(f"hook group for event {event!r} requires a 'hooks' list: group discarded")
                continue
            matcher = group.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                _warn(f"matcher for event {event!r} must be a string: group discarded")
                continue
            if matcher and event in MATCHER_IGNORED_EVENTS:
                _warn(f"matcher is ignored for event {event!r} (§2.3): {matcher!r}")
            hooks: list[HookSpec] = []
            for entry in raw_hooks:
                spec = HookSpec.from_dict(entry, event, warn=_warn)
                if spec is None:
                    continue
                if spec.type == "http" and not url_allowed(spec.url or "", whitelist):
                    _warn(
                        f"http hook url {spec.url!r} not in http_hook_urls whitelist: "
                        f"hook discarded (§4.9)"
                    )
                    continue
                hooks.append(spec)
            if hooks:
                parsed_groups.append(HookGroup(event=event, matcher=matcher, hooks=hooks))
        if parsed_groups:
            out[event] = parsed_groups
    return out


class HookJsonlSink(JsonlAuditSink):
    """执行流审计 sink(§8.1):hooks.jsonl 追加写 + fsync。

    emit 的 asdict(event) 是泛型的,一行子类即完成,audit.py 零改动。
    """
