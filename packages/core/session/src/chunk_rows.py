"""assistant/chunk 增量跑的**无损存储打包**。

provider 流式吐 token 级增量,日志里会出现成百上千条近似的
事件行,JSON 信封比载荷本身还大(真实 DeepSeek 会话实测约 56 倍)。
本模块把「连续同块」的同类增量块事件的一段跑打包成**一条存储行**
—— text-chunks / reasoning-chunks / tool-call-chunks —— 再把行
展开回完全一样的事件。

**存储行是耐久的编码词汇,不是会话事件**:它们从不进入
Session.events,没有 SessionEventMap 条目,用的是裸(无斜杠)类型
标签 —— 读者不可能把它们与事件分类学混淆(先例:JSONL 头行的
session 标签)。编码器对形状**白名单化**:任何不能完整识别的形状
原样存储 —— 未知字段或未来的块变体只损失压缩率,绝不损失数据。
解码器先校验再展开:畸形行直接抛错,而不是静默丢一整个跑。
"""

from __future__ import annotations

__all__ = [
    "MIN_RUN",
    "ChunkRow",
    "StorageRecord",
    "build_row",
    "classify",
    "continues",
    "decode_storage_record",
    "expand_row",
    "pack_chunk_runs",
    "validate_row",
]

#: 跑的成员数下限:低于它,一行行的信封与它替代的事件行一样大。
#: 这是格式常量,不是可调参数:两种布局解码结果相同,改它永不会
#: 使已存储的日志失效。
MIN_RUN = 3


def _is_record(value) -> bool:
    return isinstance(value, dict)


def _is_number(value) -> bool:
    """JS typeof === 'number' 的等价:排除 bool(Python bool 是 int 子类)。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _has_exact_keys(value, keys: list[str]) -> bool:
    """精确键检查:value 恰有 keys 中每个键,且别无其他。"""
    if not isinstance(value, dict):
        return False
    return len(value) == len(keys) and all(k in value for k in keys)


def classify(event: dict):
    """把事件归类为可打包的增量种类;形状未白名单时返回 None(原样存)。

    输入既来自活体类型化 append,也来自解析过的 fixture 文件,
    所以检查是结构性的,不信任类型。整数时间保证间隔编码精确:
    分数时间会经浮点加减重建,未必能往返。
    """
    if event["type"] != "assistant/chunk":
        return None
    if not _has_exact_keys(event, ["type", "seq", "time", "data"]):
        return None
    if not isinstance(event["seq"], int) or isinstance(event["seq"], bool) or event["seq"] < 0:
        return None
    if not isinstance(event["time"], int) or isinstance(event["time"], bool):
        return None
    data = event["data"]
    if not _is_record(data) or not _has_exact_keys(data, ["turn", "step", "chunk"]):
        return None
    if not _is_number(data["turn"]) or not _is_number(data["step"]):
        return None
    chunk = data["chunk"]
    if not _is_record(chunk) or not _is_number(chunk["index"]):
        return None
    kind = chunk["type"]
    if kind in ("text-delta", "reasoning-delta"):
        if _has_exact_keys(chunk, ["type", "index", "text"]) and isinstance(chunk["text"], str):
            return kind
        return None
    if kind == "tool-call-delta":
        shape_ok = _has_exact_keys(chunk, ["type", "index", "id", "argumentsDelta"]) or (
            _has_exact_keys(chunk, ["type", "index", "id", "name", "argumentsDelta"])
            and isinstance(chunk["name"], str)
        )
        if shape_ok and isinstance(chunk["id"], str) and isinstance(chunk["argumentsDelta"], str):
            return kind
        return None
    # 白名单落空(解析过的数据):块开始/结束、usage、finish 及任何
    # 未来块变体保持一行一事件。
    return None


def _tool_call_of(event: dict) -> dict:
    """白名单 tool-call-delta 块的调用字段(仅 classify 返回后调用)。"""
    return event["data"]["chunk"]


def _index_of(event: dict) -> int:
    return event["data"]["chunk"]["index"]


def continues(prev: dict, next_: dict, kind: str) -> bool:
    """next 是否延续结束于 prev 的一个跑(种类已由调用方核对)。

    Python int 任意精度,时间差与重建都是精确整数运算 ——
    不存在 JS 双精度下的安全整数约束,直接做差值检查。
    """
    if next_["seq"] != prev["seq"] + 1:
        return False
    if next_["data"]["turn"] != prev["data"]["turn"] or next_["data"]["step"] != prev["data"]["step"]:
        return False
    if _index_of(next_) != _index_of(prev):
        return False
    if kind != "tool-call-delta":
        return True
    a = _tool_call_of(prev)
    b = _tool_call_of(next_)
    # name 必须在「存在」与「值」两维都一致 —— 混合跑不可表示。
    return a["id"] == b["id"] and ("name" in a) == ("name" in b) and a.get("name") == b.get("name")


def build_row(kind: str, run: list[dict]) -> dict:
    """为一个完成的跑(run.length >= MIN_RUN,成员按 continues 均匀)建行。"""
    first = run[0]
    base = {
        "turn": first["data"]["turn"],
        "step": first["data"]["step"],
        "index": _index_of(first),
        "dt": [run[i]["time"] - run[i - 1]["time"] for i in range(1, len(run))],
    }
    envelope = {"seq0": first["seq"], "time0": first["time"]}
    if kind == "tool-call-delta":
        call = _tool_call_of(first)
        data = {
            **base,
            "id": call["id"],
            **({"name": call["name"]} if "name" in call else {}),
            "args": [e["data"]["chunk"]["argumentsDelta"] for e in run],
        }
        return {"type": "tool-call-chunks", **envelope, "data": data}
    data = {**base, "texts": [e["data"]["chunk"]["text"] for e in run]}
    return {
        "type": "text-chunks" if kind == "text-delta" else "reasoning-chunks",
        **envelope,
        "data": data,
    }


def pack_chunk_runs(events: list[dict]) -> list[dict]:
    """打包一个事件批次:每个至少 MIN_RUN 成员的连续同块跑变成一行。

    纯且无状态 —— 对任何数组都安全,包括被 flush 边界切断的批次
    (被切的跑按批次各自打包)。
    """
    out: list[dict] = []
    kind = None
    run: list[dict] = []

    def flush():
        nonlocal kind, run
        if kind is not None and len(run) >= MIN_RUN:
            out.append(build_row(kind, run))
        else:
            out.extend(run)
        kind = None
        run = []

    for event in events:
        k = classify(event)
        if k is None:
            flush()
            out.append(event)
            continue
        last = run[-1] if run else None
        if k == kind and last is not None and continues(last, event, k):
            run.append(event)
            continue
        flush()
        kind = k
        run = [event]
    flush()
    return out


def _malformed(tag: str, why: str):
    raise ValueError(f"malformed {tag} storage row: {why}")


def _validate_run_data(tag: str, data: dict, payload_key: str) -> list[str]:
    """校验共享的运行数据字段与载荷/dt 元数;返回成员载荷。"""
    if not _is_number(data.get("turn")) or not _is_number(data.get("step")) or not _is_number(data.get("index")):
        _malformed(tag, "turn/step/index must be numbers")
    payload = data.get(payload_key)
    if not isinstance(payload, list) or len(payload) == 0 or any(not isinstance(e, str) for e in payload):
        _malformed(tag, f"{payload_key} must be a non-empty string array")
    dt = data.get("dt")
    if not isinstance(dt, list) or any(not isinstance(g, int) for g in dt):
        _malformed(tag, "dt must be an array of integers")
    if len(dt) != len(payload) - 1:
        _malformed(tag, f"dt length {len(dt)} does not match {len(payload)} members")
    return payload


def validate_row(value: dict, tag: str) -> dict:
    """校验一条行标签解析值的信封与数据,任何畸形抛错。"""
    if not _has_exact_keys(value, ["type", "seq0", "time0", "data"]):
        _malformed(tag, "envelope must be exactly {type, seq0, time0, data}")
    if not isinstance(value["seq0"], int) or isinstance(value["seq0"], bool) or value["seq0"] < 0:
        _malformed(tag, "seq0 must be a non-negative integer")
    if not isinstance(value["time0"], int) or isinstance(value["time0"], bool):
        _malformed(tag, "time0 must be an integer")
    data = value["data"]
    if not _is_record(data):
        _malformed(tag, "data must be an object")
    if tag == "tool-call-chunks":
        with_name = _has_exact_keys(data, ["turn", "step", "index", "id", "name", "dt", "args"])
        if not with_name and not _has_exact_keys(data, ["turn", "step", "index", "id", "dt", "args"]):
            _malformed(tag, "data must be exactly {turn, step, index, id, name?, dt, args}")
        if not isinstance(data["id"], str) or (with_name and not isinstance(data["name"], str)):
            _malformed(tag, "id (and name when present) must be strings")
        _validate_run_data(tag, data, "args")
    else:
        if not _has_exact_keys(data, ["turn", "step", "index", "dt", "texts"]):
            _malformed(tag, "data must be exactly {turn, step, index, dt, texts}")
        _validate_run_data(tag, data, "texts")
    return value


def expand_row(row: dict) -> list[dict]:
    """把一条已验证的行展开回其完全一致的原事件,按序。"""
    members = row["data"]["args"] if row["type"] == "tool-call-chunks" else row["data"]["texts"]
    events: list[dict] = []
    time = row["time0"]
    for k, member in enumerate(members):
        if k > 0:
            time += row["data"]["dt"][k - 1]
        if row["type"] == "text-chunks":
            chunk = {"type": "text-delta", "index": row["data"]["index"], "text": member}
        elif row["type"] == "reasoning-chunks":
            chunk = {"type": "reasoning-delta", "index": row["data"]["index"], "text": member}
        else:  # tool-call-chunks(validate_row 只放行三种行标签)
            chunk = {
                "type": "tool-call-delta",
                "index": row["data"]["index"],
                "id": row["data"]["id"],
                **({"name": row["data"]["name"]} if "name" in row["data"] else {}),
                "argumentsDelta": member,
            }
        events.append({
            "type": "assistant/chunk",
            "seq": row["seq0"] + k,
            "time": time,
            "data": {"turn": row["data"]["turn"], "step": row["data"]["step"], "chunk": chunk},
        })
    return events


def decode_storage_record(value):
    """把一条解析过的 JSONL 行值解码成它存储的会话事件。

    块行标签的值先校验再展开(畸形行抛错 —— 那是损坏的存储,把它
    当事件处理会静默丢整个跑);其他任何值单条事件直通,不校验。
    """
    if not _is_record(value):
        return [value]
    tag = value.get("type")
    if tag not in ("text-chunks", "reasoning-chunks", "tool-call-chunks"):
        return [value]
    return expand_row(validate_row(value, tag))
