"""会话表面的折叠层:事件日志之上、LLM 可见的有序视图。

append-only 日志仍是唯一事实源,但「模型见过哪些消息、以什么顺序」
需要一个派生视图 —— 这就是**表面**(surface):只由产生 LLM 消息的
三类事件(user/message、assistant/message、tool/result)组成的有序
序列,折叠规则即 `deriveEventMessage` 对每个事件的一对一投影。

两条折叠路径共用同一套规则:
- **全量重放**(fold_surface):把一整段日志从头扫到尾,得到当前
  节点序列与全部替换历史 —— 外部重建器、纯投影用它;
- **增量管理**(SurfaceManager):挂在活会话上,事件逐条追加时增量
  维护同一视图,并兼作 append 边界的表面校验器。

**表面替换**(surfaceOp = {op: replace, start, end}):compaction 等
离线整理会「遮蔽」一段既有表面范围,用一个新事件取代它。被遮蔽
的节点从模型可见序列消失,但日志里原事件永远在 —— 人类可读的
逐字稿(transcript)应当用 append 起源事件重建,替换副本只活在
模型可见面(这就是 is_append_surface_event 与替换事件的区分意义)。

**溯源契约**(sourceEventSeqs):每次替换必须声明它遮蔽了哪些源
事件,且必须完整覆盖被遮蔽范围 —— 不完整的遮蔽声明会把「用户
看过的对话」从逐字稿里抹掉,是数据损失,直接拒绝。
"""

from __future__ import annotations

from .types import SURFACE_EVENT_TYPES, is_surface_eligible_type

__all__ = [
    "SurfaceFoldReplacement",
    "SurfaceFoldResult",
    "SessionSurface",
    "SurfaceManager",
    "SurfacePlan",
    "apply_surface_plan",
    "assert_provenance",
    "assert_tool_result_rewrite",
    "derive_event_message",
    "fold_surface",
    "is_append_surface_event",
    "is_replacement_surface_event",
    "is_surface_event",
    "plan_surface_event",
    "replacement_range",
    "surface_op_of",
]


def is_surface_event(event: dict) -> bool:
    """一个事件是否既是表面类型又带表面标记(surfaceOp)。"""
    if not is_surface_eligible_type(event["type"]):
        return False
    return "surfaceOp" in event


def is_append_surface_event(event: dict) -> bool:
    """一个事件是否以其自身日志位置追加进表面(从未做过替换副本)。

    模型可见面有意遮蔽被替换的范围,所以它不适合做人类逐字稿的
    来源 —— 落地的替换会抹掉用户已经看过的对话。append 起源的
    事件才是逐字稿的耐久素材;替换副本只活在模型侧。
    """
    return is_surface_event(event) and event["surfaceOp"] == "append"


def is_replacement_surface_event(event: dict) -> bool:
    """一个事件是否遮蔽了既有表面范围(替换副本)。"""
    return is_surface_event(event) and event["surfaceOp"] != "append"


def derive_event_message(event: dict) -> dict | None:
    """把单个事件投影成它派生的 LLM 消息;不产生消息时返回 None。

    这是**每个节点的投影规则**:Session.derive_messages 把这条规则
    折叠在活表面上,外部重建器与纯投影对日志前缀的表面折叠同一
    函数,重建出任何一次请求当初赖以构建的确切消息。返回的即事件
    包装里嵌套的冻结消息 —— 交付、耐久历史、模型请求共享同一对象。

    故意不穷尽:只有产生消息的事件派生态历史;turn/step 边界、
    块、usage、错误都是 trace/replay 数据。
    """
    # 普通的提示与注入的上下文都以 user 角色投影:事件的模型面内容
    # 原样通过。不要在这里加 per-type 包装(如 <context>):包装归
    # 生产者所有 —— 生产者把它烤进 content(如 agent-instructions
    # 的 <system-reminder>);若重引入,须由事件 meta 与专门渲染器
    # 驱动,保持本投影是逐字的直通。
    if event["type"] == "user/message":
        return event["data"]
    if event["type"] == "assistant/message":
        # 空内容的 assistant/message 只为了承载 max-tokens 步的
        # usage,不应把无内容的 assistant 轮注入 provider 逐字稿。
        if len(event["data"]["message"]["content"]) == 0:
            return None
        return event["data"]["message"]
    if event["type"] == "tool/result":
        return event["data"]["message"]
    # 非表面事件(boundary/chunk/纯日志记录)不投影任何消息。
    # 合并可扩展联合:这里不做 assertNever。
    return None


class SurfaceFoldReplacement:
    """折叠过程中观察到的一次替换操作。"""

    __slots__ = ("seq", "start", "end", "shadowed_seqs")

    def __init__(self, seq: int, start: int, end: int, shadowed_seqs: list[int]) -> None:
        self.seq = seq  # 执行替换的事件的 seq
        self.start = start  # 声明被替换的起始 seq(含)
        self.end = end  # 声明被替换的结束 seq(含)
        self.shadowed_seqs = shadowed_seqs  # 实际被移除的表面条目,按表面顺序


class SurfaceFoldResult:
    """重放完日志表面操作后的完整结果。"""

    __slots__ = ("nodes", "replacements")

    def __init__(self, nodes: list[int], replacements: list[SurfaceFoldReplacement]) -> None:
        self.nodes = nodes  # 当前表面事件 seq,模型可见顺序
        self.replacements = replacements  # 替换操作,按事件顺序


class SessionSurface:
    """表面只读活投影:当前节点序列 + 位置替换代际计数。"""

    __slots__ = ("_state",)

    def __init__(self, state) -> None:
        self._state = state

    @property
    def nodes(self) -> tuple[int, ...]:
        return tuple(self._state.nodes)

    @property
    def replace_generation(self) -> int:
        return self._state.replace_generation


class _SurfaceFoldState:
    """完整折叠与增量折叠共享的可变状态。"""

    __slots__ = ("nodes", "replace_generation")

    def __init__(self) -> None:
        self.nodes: list[int] = []
        self.replace_generation = 0


class SurfacePlan:
    """一个已验证、尚未改写折叠状态的表面转移(append 或 replace)。"""

    __slots__ = ("kind", "seq", "start", "end", "start_idx", "end_idx", "shadowed_seqs")

    def __init__(
        self, kind: str, seq: int, start: int | None = None, end: int | None = None,
        start_idx: int | None = None, end_idx: int | None = None, shadowed_seqs: list[int] | None = None,
    ) -> None:
        self.kind = kind
        self.seq = seq
        self.start = start
        self.end = end
        self.start_idx = start_idx
        self.end_idx = end_idx
        self.shadowed_seqs = shadowed_seqs

    @staticmethod
    def append(seq: int) -> "SurfacePlan":
        return SurfacePlan("append", seq)

    @staticmethod
    def replace(seq: int, start: int, end: int, shadowed_seqs: list[int], start_idx: int, end_idx: int) -> "SurfacePlan":
        plan = SurfacePlan("replace", seq, start, end, start_idx, end_idx, shadowed_seqs)
        return plan


def _is_event_seq(value) -> bool:
    """一个运行值是否为非负整数(事件 seq;Python int 任意精度,无需 safe 约束)。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_replace_op(op) -> bool:
    """一个运行值是否精确的定位替换形状:{op, start, end} 且无其他键。"""
    if not isinstance(op, dict):
        return False
    keys = set(op.keys())
    if keys != {"op", "start", "end"}:
        return False
    return op["op"] == "replace" and _is_event_seq(op["start"]) and _is_event_seq(op["end"])


def surface_op_of(event: dict):
    """校验事件局部的表面资格,返回其操作(append / replace dict / None)。

    表面类型缺标记、非表面类型带标记、标记形状非法 —— 都是数据
    错误,直接抛。
    """
    type_ = event["type"]
    if not is_surface_eligible_type(type_):
        if "surfaceOp" in event:
            raise ValueError(f'session event "{type_}" is not surface-eligible and cannot carry surfaceOp')
        if "sourceEventSeqs" in event:
            raise ValueError(f'session event "{type_}" is not surface-eligible and cannot carry sourceEventSeqs')
        return None
    op = event.get("surfaceOp")
    if op is None and "surfaceOp" not in event:
        raise ValueError(f'session event "{type_}" is surface-eligible and requires a surfaceOp marker')
    if op == "append":
        return op
    if not isinstance(op, dict) or not _is_replace_op(op):
        raise ValueError(f'session event "{type_}" carries an invalid surfaceOp')
    return op


def assert_provenance(event: dict, shadowed_seqs: list[int]) -> None:
    """校验声明的 sourceEventSeqs:对既有日志条目与替换范围的双向契约。

    必须:是数组、无重复、全为合法 seq、全部早于当前事件 seq;
    且完整覆盖本次替换遮蔽的每个表面节点 —— 遮蔽不可隐而不报。
    """
    raw = event.get("sourceEventSeqs")
    sources: set[int] = set()
    if raw is not None:
        if not isinstance(raw, list):
            raise ValueError(f"sourceEventSeqs on event at seq {event['seq']} must be an array when present")
        if len(raw) == 0 and event["type"] != "assistant/message":
            raise ValueError("sourceEventSeqs must not be empty except on assistant/message")
        for source in raw:
            if not _is_event_seq(source):
                raise ValueError(f'session event "{event["type"]}" sourceEventSeqs must densely contain non-negative integers')
            if source in sources:
                raise ValueError("sourceEventSeqs must not contain duplicates")
            sources.add(source)
            if source >= event["seq"]:
                raise ValueError(f"sourceEventSeqs must reference earlier events: {source} >= current seq {event['seq']}")
    missing = [seq for seq in shadowed_seqs if seq not in sources]
    if missing:
        raise ValueError(
            f"surface replace: sourceEventSeqs must include every shadowed surface node; missing {', '.join(map(str, missing))}"
        )


def replacement_range(state: _SurfaceFoldState, op: dict):
    """定位一个替换范围而不改动当前折叠状态。"""
    nodes = state.nodes
    try:
        start_idx = nodes.index(op["start"])
    except ValueError:
        raise ValueError(f"surface replace: start seq {op['start']} not found in surface") from None
    try:
        end_idx = nodes.index(op["end"])
    except ValueError:
        raise ValueError(f"surface replace: end seq {op['end']} not found in surface") from None
    if start_idx > end_idx:
        raise ValueError(
            f"surface replace: start seq {op['start']} (index {start_idx}) is after end seq {op['end']} (index {end_idx})"
        )
    return start_idx, end_idx, list(nodes[start_idx : end_idx + 1])


def _is_deep_equal_json(a, b) -> bool:
    """会话事件 JSON 值域上的深结构相等(None/bool/int/float/str/数组/普通对象)。

    替代 node:util 的 isDeepStrictEqual,保持本模块零外部依赖。
    迭代式遍历(显式任务栈),避免深层 JSON 把调用栈压爆 ——
    与 json.py 的 walker 同一动机。
    """
    # (a, b) 待比较对的任务栈
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        if x is y:
            continue
        x_is_list = isinstance(x, (list, tuple))
        y_is_list = isinstance(y, (list, tuple))
        if x_is_list or y_is_list:
            if not x_is_list or not y_is_list or len(x) != len(y):
                return False
            for i in range(len(x)):
                stack.append((x[i], y[i]))
            continue
        if not isinstance(x, dict) or not isinstance(y, dict):
            if isinstance(x, (int, float, str, bool)) and isinstance(y, (int, float, str, bool)):
                # 标量:NaN 已在 json 校验被拒,这里数值相等即可
                return x == y
            if x is None or y is None:
                return x is y
            return False
        if len(x) != len(y):
            return False
        for key, xv in x.items():
            if key not in y:
                return False
            stack.append((xv, y[key]))
    return True


def assert_tool_result_rewrite(event: dict, shadowed_seqs: list[int], events: list[dict], base_seq: int) -> None:
    """把 tool/result 替换限定为「只改当前结果的内容」。

    一次替换遮蔽的必须是恰好一个当前节点、且原节点也是 tool/result;
    除去消息内容(整段清空后比较),其余一切 —— 消息身份、source、
    调用配对、data 其他字段 —— 必须逐字相同。任何越界改动都是
    用替换副本篡改历史,拒绝。
    """
    if event["type"] != "tool/result":
        return
    if len(shadowed_seqs) != 1:
        raise ValueError("tool/result surface replacement must rewrite exactly one current node")
    for original_seq in shadowed_seqs:
        original = events[original_seq - base_seq]
        if original is None or original["type"] != "tool/result":
            raise ValueError("tool/result surface replacement must target a current tool/result")
        original_rest = dict(original["data"])
        replacement_rest = dict(event["data"])
        original_content = original["data"]["message"]["content"]
        replacement_content = event["data"]["message"]["content"]
        original_result = original_content[0] if original_content else None
        replacement_result = replacement_content[0] if replacement_content else None
        original_rest["message"] = {
            **original["data"]["message"],
            "content": [{**(original_result or {}), "content": None}],
        }
        replacement_rest["message"] = {
            **event["data"]["message"],
            "content": [{**(replacement_result or {}), "content": None}],
        }
        if not _is_deep_equal_json(original_rest, replacement_rest):
            raise ValueError("tool/result surface replacement may change only content")


def plan_surface_event(state: _SurfaceFoldState, event: dict, expected_seq: int, events: list[dict], base_seq: int):
    """在事件的重放边界校验它,准备其原子折叠转移(不提交)。"""
    if event["seq"] != expected_seq:
        raise ValueError(f"session event seq {event['seq']} is not contiguous; expected {expected_seq}")
    surface_op = surface_op_of(event)
    if surface_op is None:
        return None
    if surface_op == "append":
        assert_provenance(event, [])
        return SurfacePlan.append(event["seq"])
    start_idx, end_idx, shadowed = replacement_range(state, surface_op)
    assert_provenance(event, shadowed)
    assert_tool_result_rewrite(event, shadowed, events, base_seq)
    return SurfacePlan.replace(event["seq"], surface_op["start"], surface_op["end"], shadowed, start_idx, end_idx)


def apply_surface_plan(state: _SurfaceFoldState, plan):
    """提交一个已验证的表面转移;发生替换时返回替换元数据。"""
    if plan is not None and plan.kind == "append":
        state.nodes.append(plan.seq)
    elif plan is not None and plan.kind == "replace":
        state.nodes[plan.start_idx : plan.end_idx + 1] = [plan.seq]
        state.replace_generation += 1
    if plan is None or plan.kind != "replace":
        return None
    return SurfaceFoldReplacement(plan.seq, plan.start, plan.end, plan.shadowed_seqs)


def apply_surface_event(state: _SurfaceFoldState, event: dict, expected_seq: int, events: list[dict], base_seq: int):
    """校验并提交一个事件,发生替换时返回替换元数据。"""
    return apply_surface_plan(state, plan_surface_event(state, event, expected_seq, events, base_seq))


def fold_surface(events: list[dict]) -> SurfaceFoldResult:
    """重放完整会话日志走一遍规范表面折叠。

    events 必须按连续 seq 顺序给出。任何事件违反表面元数据、
    源事件引用、范围、tool/result 重写规则时抛错。
    """
    state = _SurfaceFoldState()
    replacements: list[SurfaceFoldReplacement] = []
    for index, event in enumerate(events):
        replacement = apply_surface_event(state, event, index, events, 0)
        if replacement is not None:
            replacements.append(replacement)
    return SurfaceFoldResult(list(state.nodes), replacements)


class SurfaceManager(SessionSurface):
    """增量的有序表面视图 + append 边界校验器。

    挂在活会话上:日志每接受一个事件,视图增量推进;validate_next
    在候选事件尚未入日志前就完成校验(先校验后提交,与 invariant
    同一原子性哲学)。重放时若发现候选已入日志,则直接消费其已存
    计划,避免重复校验。
    """

    __slots__ = ("log", "base_seq", "_state", "_last_processed_seq", "_pending_plan")

    def __init__(self, log: list[dict], base_seq: int = 0) -> None:
        self.log = log  # 连续完整日志,或已加载的事件窗口(活引用,外部追加)
        self.base_seq = base_seq  # 窗口第一个事件的绝对 seq
        self._state = _SurfaceFoldState()
        self._last_processed_seq = base_seq - 1
        self._pending_plan = None  # validate_next 已校验、待确证入日志的候选

    @property
    def replace_generation(self) -> int:
        if self._last_processed_seq < self.base_seq + len(self.log) - 1:
            self._process_delta()
        return self._state.replace_generation

    @property
    def nodes(self) -> list[int]:
        if self._last_processed_seq < self.base_seq + len(self.log) - 1:
            self._process_delta()
        return self._state.nodes

    def validate_next(self, event: dict) -> None:
        """校验下一个候选事件,不改动已提交的表面。"""
        if self._last_processed_seq < self.base_seq + len(self.log) - 1:
            self._process_delta()
        expected_seq = self.base_seq + len(self.log)
        self._pending_plan = (event, expected_seq, plan_surface_event(self._state, event, expected_seq, self.log, self.base_seq))

    def _process_delta(self) -> None:
        """折叠上次访问以来追加的事件。"""
        tail_seq = self.base_seq + len(self.log) - 1
        for seq in range(self._last_processed_seq + 1, tail_seq + 1):
            index = seq - self.base_seq
            event = self.log[index]
            pending = self._pending_plan
            if pending is not None and pending[0] is event and pending[1] == seq:
                apply_surface_plan(self._state, pending[2])
            else:
                apply_surface_event(self._state, event, seq, self.log, self.base_seq)
            if pending is not None and pending[1] <= seq:
                self._pending_plan = None
            self._last_processed_seq = seq
