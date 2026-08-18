"""共享 Markdown frontmatter 解析(阶段 13 agents / 14 skills 共用)。

从 agents/loader.py 提取的行为保持重构:解析逻辑零改动,只把字段集合
参数化 —— 不同模块的白名单字段不同(agents 用 tools/disallowed_tools,
skills 用 allowed-tools/arguments/paths/aliases)。调用方通过
``list_fields`` / ``map_fields`` 声明各自的 flow-list 字段与单层 map 字段。

支持 YAML 最小子集(零依赖,对齐 CC js-yaml default schema):标量、
flow 列表(逗号或空格分隔,可带方括号)、单层 map。未知键原样保留,
白名单过滤由调用方的 build_definition 负责。
"""

from __future__ import annotations

from typing import Any

#: 默认 flow-list 字段集(空:调用方按需扩展)
DEFAULT_LIST_FIELDS: frozenset[str] = frozenset()
#: 默认单层 map 字段集(hooks 是各模块共有的 map 字段)
DEFAULT_MAP_FIELDS: frozenset[str] = frozenset({"hooks"})


def parse_scalar(raw: str) -> Any:
    """解析标量值:去除引号 + YAML 1.1 布尔拼写 + 整数/null。"""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    low = raw.lower()
    # YAML 1.1 boolean spellings (js-yaml default schema — CC parity):
    # yes/no/on/off must not survive as strings (bool("no") is True).
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


def parse_flow_list(raw: str) -> list[str]:
    """解析 flow 列表:[a, b] / 逗号 / 空格分隔三种形态。"""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [p.strip("'\"") for p in raw.replace(",", " ").split() if p]


def parse_flow_map(raw: str) -> dict[str, Any]:
    """解析单行 flow map:{k: v, ...}(值仅标量)。"""
    raw = raw.strip()
    if raw.startswith("{") and raw.endswith("}"):
        raw = raw[1:-1]
    result: dict[str, Any] = {}
    for part in raw.split(","):
        k, _, v = part.partition(":")
        k = k.strip()
        if k:
            result[k] = parse_scalar(v)
    return result


def parse_value(
    raw: str,
    key: str,
    *,
    list_fields: frozenset[str] = DEFAULT_LIST_FIELDS,
    map_fields: frozenset[str] = DEFAULT_MAP_FIELDS,
) -> Any:
    """按字段声明分发解析形态:list 字段走 flow-list,map 字段走 flow-map,其余标量。"""
    if key in list_fields:
        return parse_flow_list(raw)
    if key in map_fields and raw.startswith("{"):
        return parse_flow_map(raw)
    return parse_scalar(raw)


def parse_frontmatter(
    text: str,
    *,
    list_fields: frozenset[str] = DEFAULT_LIST_FIELDS,
    map_fields: frozenset[str] = DEFAULT_MAP_FIELDS,
) -> tuple[dict[str, Any], int] | None:
    """解析 ``---`` 围栏的 frontmatter 块。

    返回 (parsed_dict, 结束围栏之后的行号),无成对围栏 → None —— 未闭合的
    开场围栏视为 malformed(gray-matter/CC 视为无 frontmatter,由调用方跳过)。
    单趟扫描:正文从结束围栏后开始,正文里的 ``---`` 保持正文文本。
    """
    lines = text.splitlines()
    # BOM:Windows 编辑器常带,str.strip() 去不掉(U+FEFF);显式 lstrip
    if not lines or lines[0].lstrip("\ufeff").strip() != "---":
        return None
    end: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None
    result: dict[str, Any] = {}
    i = 1
    while i < end:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        key, sep, rest = line.partition(":")
        key = key.strip()
        if not sep or not key:
            i += 1
            continue
        rest = rest.strip()
        if rest or key not in map_fields:
            result[key] = (
                parse_value(rest, key, list_fields=list_fields, map_fields=map_fields)
                if rest
                else None
            )
            i += 1
            continue
        # 单层 map(如 hooks):缩进的 ``subkey: value`` 行
        sub: dict[str, Any] = {}
        j = i + 1
        while j < end:
            subline = lines[j]
            if subline[:1] in (" ", "\t"):
                j += 1
                if not subline.strip() or subline.lstrip().startswith("#"):
                    continue  # map 内的空行/注释行跳过
                sk, ssep, sv = subline.strip().partition(":")
                if ssep and sk:
                    sub[sk] = parse_scalar(sv)
                continue
            if not subline.strip():
                # 空行:下一行仍是缩进则属于 map
                nxt = j + 1
                while nxt < end and not lines[nxt].strip():
                    nxt += 1
                if nxt < end and lines[nxt][:1] in (" ", "\t"):
                    j += 1
                    continue
            break
        result[key] = sub
        i = j
    return result, end + 1
