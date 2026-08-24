"""请求头重建工具:从完整的 request/header 事件反推请求构建时的头。

任何一个持有会话日志的人,取「最新一份规范快照」即可重建任何一次
请求当初构建所依据的 EpochHeader;主循环用同一个相等辅助避免把
未变化的头记进日志。

DSH 中 callConfigEquals 来自 dsh-llm 包;core/session 保持零依赖,
这里按 DSH call-config.ts 的字段语义内联一份(provider/model/
reasoningEffort/temperature/maxTokens 逐字段 + stop 数组缺省感知)。
"""

from __future__ import annotations

__all__ = ["canonical_header", "fold_request_header", "header_equals"]


def _config_equals(a, b) -> bool:
    """两份 config 是否等价(照 DSH callConfigEquals 字段语义)。

    config 是 JSON dict,缺省字段视作 None:显式 None 与未提供的
    缺省等价 —— 调用方补全与不补全不该造成「看起来变了」。
    """
    for field in ("provider", "model", "reasoningEffort", "temperature", "maxTokens"):
        if a.get(field) != b.get(field):
            return False
    a_stop = a.get("stop")
    b_stop = b.get("stop")
    if a_stop is None or b_stop is None:
        return a_stop is b_stop
    return len(a_stop) == len(b_stop) and all(s == t for s, t in zip(a_stop, b_stop))


def _same_schema(a, b) -> bool:
    """工具 schema 的规范 JSON 相等(同一路径装配出的顺序可比)。"""
    return a == b


def canonical_header(header: dict) -> dict:
    """把头规范化:空的 system 与空的 tools 列表变成缺省字段。

    与请求构建时的表示一致 —— 记日志、折叠、比较都只用这一种表示。
    """
    adapter_defaults = header.get("adapterDefaults")
    result = {"config": header["config"]}
    if adapter_defaults is not None and (adapter_defaults.get("reasoningEffort") is True or adapter_defaults.get("maxTokens") is True):
        result["adapterDefaults"] = adapter_defaults
    system = header.get("system")
    if system is not None and len(system) > 0:
        result["system"] = system
    tools = header.get("tools")
    if tools is not None and len(tools) > 0:
        result["tools"] = tools
    return result


def header_equals(a: dict, b: dict) -> bool:
    """规范头上的逐字段相等;工具 schema 按序比较。"""
    if (
        not _config_equals(a["config"], b["config"])
        or a.get("adapterDefaults", {}).get("reasoningEffort") != b.get("adapterDefaults", {}).get("reasoningEffort")
        or a.get("adapterDefaults", {}).get("maxTokens") != b.get("adapterDefaults", {}).get("maxTokens")
        or a.get("system") != b.get("system")
    ):
        return False
    at = a.get("tools") or []
    bt = b.get("tools") or []
    return len(at) == len(bt) and all(_same_schema(t, u) for t, u in zip(at, bt))


def fold_request_header(events: list[dict], from_=None):
    """把一段日志(或前缀)的头事件折叠成最后一份快照之后生效的头。

    非头事件跳过。这是纯离线重建路径;活会话在内存里增量维护同一
    折叠。
    """
    state = from_
    for event in events:
        if event["type"] == "request/header":
            state = canonical_header(event["data"]["header"])
    return state
