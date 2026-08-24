"""index.py —— Session 与 SessionStore:事件溯源会话服务(DSH index.ts 移植)。

DSH index.ts(1157 行)是整个会话包的门面:内存里的会话对象
(Session)、活会话仓库(SessionStore,挂 ctx.sessions)、以及全部
接纳/恢复边界的校验族。本文件按同一分工移植:

- Session:append-only 事件日志的持有者。日志是唯一事实源,
  消息历史(derive_messages)、请求头(request_header)都是从
  日志派生的缓存纯函数;append 是唯一写入通道,先校验后入日志,
  一旦入日志即不可变(快照产物为冻结结构)。
- SessionStore:活会话仓库,一个 cordis 服务。会话生命周期
  prepare → enter → announce → flush 四步:
  prepare 只构造(校验 id/meta,不进仓库);enter 装发布钩子并
  入仓库,返回 detach 一次性能力;announce 宣布创建(同步抛错
  可否决并触发配对回滚);flush 是持久化耐久检查点(并行等待
  全部监听者落盘)。持久化不是本包的事 —— 插件订阅 session/event
  并在 session/flush 时排干缓冲(DSH 原话:persistence is a
  plugin concern)。
- 校验族:validate_session_header 族管创建元数据,assert 族管
  恢复边界的信封/消息形状 —— 与 invariant 的分工:invariant 管
  关系(seq 连续、turn/step 嵌套),本文件管形状(键、类型、
  消息身份),各管一摊。

错误消息文本照 DSH 逐字保留(英文),注释为架构解读(中文)。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from weakref import WeakKeyDictionary

from cordis import Service

from .json import FrozenDict, FrozenList, snapshot_json_value
from .request_header import fold_request_header
from .surface import (
    SessionSurface,
    SurfaceManager,
    derive_event_message as _derive_event_message,
)
from .types import SESSION_FORMAT_VERSION, SessionId

from packages.core.scope import scope_of, scope_target  # 路由牌铸造(enter 边界)

__all__ = [
    "Session",
    "SessionStore",
    "SessionForkError",
    "SessionForkSource",
    "adopt_session_event",
    "snapshot_session_event",
    "validate_session_header",
    "validate_restored_session_header",
    "snapshot_session_header",
    "assert_session_event_envelope",
    "assert_current_llm_shape",
    "assert_adapter_defaults",
    "assert_message_event_shape",
    "has_provider_model",
    "assert_supported_request_header",
    "collect_session_callbacks",
    "invoke_contained_session_observers",
    "install",
]


def _now_ms() -> int:
    """当前毫秒时间戳(DSH Date.now())—— 事件入日志时刻。"""
    return int(time.time() * 1000)


def _is_safe_int(value) -> bool:
    """JS Number.isSafeInteger 的 Python 形态:int 且非 bool。

    JS 的 safe-integer 同时约束了类型与上界(±2^53-1);Python 的
    int 无界,类型正确即放行 —— 快照器已在源头拒绝负零等异常值。
    bool 是 int 子类,必须显式排除(JS 里 true 不是 safe integer)。
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _freeze_json(value):
    """把一棵(已校验或待校验的)普通 JSON 树转换为冻结结构。

    DSH 的 deepFreeze 原地冻结调用方的对象;Python 没有原地冻结,
    只能经无损通道重建一份冻结结构(快照产物)。对已冻结的输入
    (快照产物回流)原样返回,不拷贝 —— 保持身份。
    """
    if isinstance(value, (FrozenDict, FrozenList)):
        return value
    snapshot = snapshot_json_value(value)
    if snapshot is None:
        raise ValueError("value is not losslessly JSON-serializable")
    return snapshot


# ---- 校验族:创建元数据(header) ----


def validate_session_header(id: str, input):
    """校验并冻结一份(分离的)创建元数据。

    只认普通 JSON 记录;字段级白名单校验,多余字段不拒绝(header
    可携带插件字段,DSH 同样只校验它认识的键)。返回值是冻结结构:
    header 不参与重放,但它是对外发布的创建事实,不可变。
    """
    if input is None or not isinstance(input, dict):
        raise ValueError("session header is not a plain JSON record")
    if input.get("version") != SESSION_FORMAT_VERSION:
        raise ValueError(
            f"session header version must be {SESSION_FORMAT_VERSION}, got {str(input.get('version'))}"
        )
    if input.get("id") != id:
        raise ValueError(
            f'session header id "{str(input.get("id"))}" does not match session id "{id}"'
        )
    created_at = input.get("createdAt")
    if not _is_safe_int(created_at) or created_at < 0:
        raise ValueError("session header createdAt must be a non-negative safe integer")
    if "cwd" in input and input["cwd"] is not None:
        cwd = input["cwd"]
        if not isinstance(cwd, str):
            raise ValueError("session header cwd must be a string")
        if not _is_absolute_path(cwd):
            raise ValueError(f'session header cwd must be an absolute path, got "{cwd}"')
    if "parentSession" in input and input["parentSession"] is not None:
        if not isinstance(input["parentSession"], str):
            raise ValueError("session header parentSession must be a string")
    if "seedLength" in input and input["seedLength"] is not None:
        seed_length = input["seedLength"]
        if not _is_safe_int(seed_length) or seed_length < 0:
            raise ValueError("session header seedLength must be a non-negative safe integer")
    if "origin" in input and input["origin"] is not None:
        if input["origin"] != "subagent":
            raise ValueError('session header origin must be "subagent"')
    if "delegationDepth" in input and input["delegationDepth"] is not None:
        depth = input["delegationDepth"]
        if not _is_safe_int(depth) or depth < 0:
            raise ValueError("session header delegationDepth must be a non-negative safe integer")
    if "agentPreset" in input and input["agentPreset"] is not None:
        if not isinstance(input["agentPreset"], str):
            raise ValueError("session header agentPreset must be a string")
    return _freeze_json(input)


def _is_absolute_path(value: str) -> bool:
    """绝对路径判定(DSH isAbsolute)。Windows 下驱动器/UNC 前缀也是绝对。"""
    from os import path

    return path.isabs(value)


def validate_restored_session_header(id: str, input):
    """恢复边界:先验证「普通记录」原型,再走常规校验。

    恢复的 header 来自持久化(独占所有权),DSH 先查原型链防止
    类实例混入;Python 侧等价物是类型精确性 —— 只接受普通 dict
    与我们自己的 FrozenDict(dict 子类是 JS 类实例的对应物,拒绝),
    序列与其它类实例一律拒绝,再委托 validate_session_header。
    """
    if input is not None and type(input) not in (dict, FrozenDict):
        raise ValueError("session header is not a plain JSON record")
    return validate_session_header(id, input)


def snapshot_session_header(id: str, source=None):
    """分离、校验并冻结一份创建元数据(缺省合成最小 header)。

    source 缺省时合成 {version, id, createdAt}:脱离 store 单独
    构造的 Session(如测试)也能保证 session.header 恒在。
    """
    input_ = (
        {"version": SESSION_FORMAT_VERSION, "id": id, "createdAt": _now_ms()}
        if source is None
        else source
    )
    snapshot = snapshot_json_value(input_)
    if snapshot is None:
        raise ValueError("session header is not losslessly JSON-serializable")
    return validate_session_header(id, snapshot)


# ---- 校验族:事件信封与消息形状(seed/load 边界) ----


def assert_session_event_envelope(value: dict, index: int) -> None:
    """校验固定事件信封(一次性 JSON 物化后)。

    拒绝七键之外的任何键(合并可扩展的事件类型仍走七键信封,
    插件字段装在 data 里,不占信封层);拒绝 legacy 的
    request/header-delta 词汇;表面类型再下探 LLM 形状。
    """
    if value.get("type") == "request/header-delta":
        raise ValueError(
            f"seed event at index {index} uses unsupported legacy request/header-delta format"
        )
    for key in value:
        if key not in ("type", "seq", "time", "data", "surfaceOp", "sourceEventSeqs", "ignorable"):
            raise ValueError(f"seed event at index {index} has an invalid event envelope")
    type_ = value.get("type")
    seq = value.get("seq")
    time_ = value.get("time")
    if (
        not isinstance(type_, str)
        or not _is_safe_int(seq)
        or seq < 0
        or not _is_safe_int(time_)
        or "data" not in value
        or ("ignorable" in value and value["ignorable"] is not True)
    ):
        raise ValueError(f"seed event at index {index} has an invalid event envelope")
    if type_ in ("request/header", "user/message", "assistant/message", "tool/result"):
        assert_current_llm_shape(value, index)


def assert_current_llm_shape(event: dict, index: int) -> None:
    """在 seed/load 边界拒绝过时请求头与畸形消息。"""
    data = event.get("data")
    record = data if isinstance(data, dict) else None
    if event["type"] == "request/header":
        header = record.get("header") if record is not None else None
        header_record = header if isinstance(header, dict) else None
        config = header_record.get("config") if header_record is not None else None
        if not has_provider_model(config):
            raise ValueError(f"seed request/header at index {index} lacks provider/model")
        config_record = config
        reasoning_effort = config_record.get("reasoningEffort")
        if reasoning_effort is not None and (
            not isinstance(reasoning_effort, str) or len(reasoning_effort) == 0
        ):
            raise ValueError(
                f"seed request/header at index {index} has an invalid reasoningEffort"
            )
        adapter_defaults = (
            header_record.get("adapterDefaults") if header_record is not None else None
        )
        assert_adapter_defaults(adapter_defaults, config_record, index)
    type_ = event["type"]
    if type_ not in ("user/message", "assistant/message", "tool/result"):
        return
    assert_message_event_shape(event, f"seed {type_} at index {index}")


_ALLOWED_ADAPTER_KEYS = frozenset({"reasoningEffort", "maxTokens"})


def assert_adapter_defaults(value, config: dict, index: int) -> None:
    """校验持久化请求头里的适配器缺省标记。

    标记只允许 {reasoningEffort, maxTokens} 两键,值必须是字面
    true(JS 语义:1 不等于 true),且被标记的字段必须真实存在于
    config —— 标记是「这个字段由适配器兜底」的声明,指向不存在的
    字段是数据错误。
    """
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"seed request/header at index {index} has invalid adapterDefaults")
    if (
        any(key not in _ALLOWED_ADAPTER_KEYS for key in value)
        or any(marker is not True for marker in value.values())
        or (value.get("reasoningEffort") is True and "reasoningEffort" not in config)
        or (value.get("maxTokens") is True and "maxTokens" not in config)
    ):
        raise ValueError(f"seed request/header at index {index} has invalid adapterDefaults")


def assert_message_event_shape(event: dict, subject: str) -> None:
    """只校验安全重放一条消息所需的事件特定不变式。

    与 invariant 的分工:这里不管 seq/turn 关系,只保证投影能
    发生 —— 消息身份、角色、来源、内容形状、tool-result 单块
    与 callId 配对。surface 替换的完整约束在 surface.py,不在
    本边界。
    """
    type_ = event["type"]
    if type_ not in ("user/message", "assistant/message", "tool/result"):
        return
    data = event.get("data")
    record = data if isinstance(data, dict) else None
    message = record if type_ == "user/message" else (record.get("message") if record is not None else None)
    if not isinstance(message, dict) or not isinstance(message.get("id"), str) or message["id"] == "":
        raise ValueError(f"{subject} lacks an identified message")
    expected_role = "assistant" if type_ == "assistant/message" else "user"
    if message.get("role") != expected_role:
        raise ValueError(f'{subject} message must have role "{expected_role}"')
    source = message.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("kind"), str) or source["kind"] == "":
        raise ValueError(f"{subject} message has invalid source")
    if not isinstance(message.get("content"), list):
        raise ValueError(f"{subject} message has invalid content")
    if type_ == "assistant/message":
        if source["kind"] != "model" or not has_provider_model(source):
            raise ValueError(f"{subject} message must have model source")
        return
    if type_ != "tool/result":
        return
    if source["kind"] != "tool" or not isinstance(source.get("callId"), str) or source["callId"] == "":
        raise ValueError(f"{subject} message must have tool source")
    content = message["content"]
    block = content[0] if content else None
    if (
        len(content) != 1
        or not isinstance(block, dict)
        or block.get("type") != "tool-result"
        or not isinstance(block.get("content"), list)
    ):
        raise ValueError(f"{subject} message must contain one tool-result block")
    if block.get("toolCallId") != source["callId"]:
        raise ValueError(f"{subject} message has mismatched tool call ids")


def has_provider_model(value) -> bool:
    """未知值是否携带当前的 provider/model 对(两个非空字符串)。"""
    if not isinstance(value, dict):
        return False
    return (
        isinstance(value.get("provider"), str)
        and len(value["provider"]) > 0
        and isinstance(value.get("model"), str)
        and len(value["model"]) > 0
    )


def assert_supported_request_header(type_: str, data, location: str) -> None:
    """拒绝随 legacy delta 编解码移除的请求头词汇。

    写点(append)与恢复边界(seed)共用:拒绝 request/header-delta
    类型与 reason="fallback" 的旧头 —— 新运行时不再能正确读取它们。
    """
    if type_ == "request/header-delta":
        raise ValueError(
            f"{location} uses unsupported legacy request/header-delta format"
        )
    if (
        type_ == "request/header"
        and isinstance(data, dict)
        and data.get("reason") == "fallback"
    ):
        raise ValueError(
            f'{location} uses unsupported legacy request/header reason "fallback"'
        )


# ---- 事件接纳边界 ----


def adopt_session_event(event: dict) -> dict:
    """接纳一个独占所有权的事件:校验形状并冻结其标识消息。

    调用方转让一棵不再保留、且不与其它事件共享可变子节点的对象
    图(来自持久化等可信边界的解析结果)。返回同一事件对象(其
    消息被冻结)。所有权不独占时用 snapshot_session_event。
    """
    assert_message_event_shape(event, f"session event at seq {event['seq']}")
    if event["type"] == "user/message":
        frozen = _freeze_json(event["data"])
        if frozen is not event["data"]:
            event["data"] = frozen
    elif event["type"] in ("assistant/message", "tool/result"):
        frozen = _freeze_json(event["data"]["message"])
        if frozen is not event["data"]["message"]:
            event["data"]["message"] = frozen
    return event


def snapshot_session_event(event: dict) -> dict:
    """分离一个事件,并对其标识消息保持深度不可变。"""
    return adopt_session_event(snapshot_json_value(event))


# ---- 观察者基础(发布钩子) ----


def collect_session_callbacks(ctx, args: list) -> list:
    """解析一份监听者快照(含 cordis 的内部派发检查)。

    dispatch 就地消费 args 的前两项:thisArg(路由牌)与事件名;
    剩余参数留给调用方逐个回调。collect 只解析、不调用 ——
    让调用方拥有「先快照、后入日志、再通知」的提交原子性。
    """
    return list(ctx.events.dispatch("emit", args))


def _observe_rejection(awaitable, ctx, id: str, name: str) -> None:
    """观察一个异步监听者返回值的拒绝并记日志(不传播)。

    同步派发边界无法回滚,拒绝只能被记录(DSH:rejection is too
    late to roll back and must be logged instead of becoming
    unhandled)。无运行中事件循环时忽略 —— 同步上下文里本就无法
    调度它。
    """
    try:
        task = asyncio.ensure_future(awaitable)
    except RuntimeError:
        return
    task.add_done_callback(
        lambda t: t.exception()
        and ctx.logger.warn(f'session "{id}": {name} listener rejected: {t.exception()}')
    )


def invoke_contained_session_observers(ctx, name: str, id: str, args: list, callbacks: list) -> None:
    """逐个调用一份只读监听者快照,单监听者错误包含化。

    observe-only 事件(session/event 与 session/disposed)的监听者
    失败只记日志:事件已入日志,通知失败不改变已提交的事实,也
    不能阻止后续监听者看到同一事件(DSH:observer failures are
    logged and contained)。
    """
    for callback in callbacks:
        try:
            returned = callback(*args)
            if inspect.isawaitable(returned):
                _observe_rejection(returned, ctx, id, name)
        except Exception as error:
            ctx.logger.warn(f'session "{id}": {name} listener threw: {error}')


# ---- 活条目与仓库附件 ----


class SessionEntry:
    """一个精确活条目的全部可变生命周期状态。

    一次 enter 造一个:会话、路由牌(carrier,派发时按它筛选
    监听者)、发布用的 ctx、以及 announce/append 互斥与延迟
    detach 标记。detach 由 enter 装配 —— 一次性能力与延迟语义
    都在闭包里,条目自身只存布尔标记。
    """

    __slots__ = (
        "id",
        "session",
        "carrier",
        "emit_ctx",
        "announced",
        "announcing",
        "appending",
        "detach_requested",
        "detach",
    )

    def __init__(self, id: str, session: "Session", carrier, emit_ctx) -> None:
        self.id = id
        self.session = session
        self.carrier = carrier
        self.emit_ctx = emit_ctx
        self.announced = False  # session/created 已发出(配对 session/disposed 的门槛)
        self.announcing = False  # 创建派发进行中(同步边界,detach 需延迟)
        self.appending = False  # append 发布进行中(重入护栏 + detach 延迟)
        self.detach_requested = False  # detach 被要求但派发未退栈
        self.detach = None  # 由 enter 装配(闭包,非 None)

    def __repr__(self) -> str:  # pragma: no cover -- 调试辅助
        return f"SessionEntry(id={self.id!r}, announced={self.announced})"


#: 会话 → 活条目的弱附件。弱引用:条目生命周期由 store 所有,
#: 会话对象被外部丢弃时不应把条目钉在内存里。Python 侧对应
#: DSH 的 WeakMap —— Session 必须可弱引用(普通类即可)。
attachments = WeakKeyDictionary()


# ---- 会话本体 ----


class Session:
    """一个事件溯源会话:append-only 的事件日志。

    普通类(不是 Service)—— 活实例经 ctx.sessions.create() 创建,
    分离实例经 Session.create/from_restore 创建;用已有事件日志
    播种即重放/分叉一个会话。日志是唯一事实源:消息历史、请求头
    都是从它派生的纯函数;事件一旦入日志,其嵌套数据全部深度冻结
    (快照产物),任何写操作抛 TypeError —— 持久化历史不可改写。

    Python 移植注记:DSH 用 deepFreeze 原地冻结调用方传入的种子
    事件;Python 无原地冻结,快照器产出一份冻结拷贝。所有权语义
    (调用方不得保留可变别名)不变。
    """

    #: 私有 append-only 日志(唯一事实源)。
    _log: list

    #: 表面的单一增量属主:校验候选 + 维护投影状态。
    _surface_manager: SurfaceManager

    def __init__(self, id: str, seed=None, header=None, mode: str = "snapshot") -> None:
        """构造会话。私有通道:经 Session.create / Session.from_restore。

        mode='snapshot' 是常规创建(种子经快照分离);mode='restore'
        是恢复创建(种子为独占所有权的新鲜解析值,校验后直接冻结,
        不额外拷贝 —— 所有权已转让)。
        """
        self._log = []
        self._surface_manager = SurfaceManager(self._log)
        # 派生缓存(每次 append 后失效/推进):事件快照、请求头折叠、
        # 上下文折叠、派生消息 —— 全部实例级,避免类级共享。
        self._events_snapshot = None
        self._header_fold = None
        self._header_fold_seq = 0
        self._context_fold = None
        self._context_fold_seq = 0
        self._derived: list = []
        self._derived_nodes = 0
        self._derived_generation = 0
        restored_header = validate_restored_session_header(id, header) if mode == "restore" else None
        if seed is not None:
            # 种子校验到与 append 相同的不变式:每个事件的 data 必须
            # JSON 可序列化,seq 必须从 0 连续(全系统依赖的
            # seq = log.length 契约)。否则坏种子只会在落盘时才暴露
            # 为后端拒绝,或活日志与磁盘静默分叉。
            for index, source in enumerate(seed):
                # 种子是持久化/重放边界:一次无损 JSON 通道完成
                # 校验与分离;restore 模式所有权已转让,直接冻结。
                snapshot = source if mode == "restore" else snapshot_json_value(source)
                if snapshot is None:
                    raise ValueError(
                        f"seed event at index {index} is not losslessly JSON-serializable"
                    )
                assert_session_event_envelope(snapshot, index)
                assert_supported_request_header(snapshot["type"], snapshot["data"], f"seed event at index {index}")
                if snapshot["seq"] != index:
                    raise ValueError(
                        f"seed event at index {index} has seq {snapshot['seq']} "
                        f"(expected {index}); seed must be contiguous from 0"
                    )
                # 种子经与活 append 相同的增量通道接纳:候选在入
                # 日志前先校验,失败不会部分污染表面。
                try:
                    self._surface_manager.validate_next(snapshot)
                except ValueError as error:
                    raise ValueError(
                        f"invalid seed event at index {index}: {error}"
                    ) from None
                self._log.append(
                    _freeze_json(snapshot) if mode == "restore" else snapshot
                )
        # 本进程内第一个 append 的 seq:构造种子长度(无种子为 0)。
        self.first_live_seq = len(self._log)
        self.header = restored_header if restored_header is not None else snapshot_session_header(id, header)
        # 在这里追加 end-seed 标记:后端捕获创建种子时标记已在
        # events 里 —— 加载路径无写入。已以该标记结尾的种子不重复
        # 标记:冷会话在首次触达时恢复,反复打开不得使日志随每次
        # 打开而增长。
        if seed is not None and (not self._log or self._log[-1]["type"] != "session/end-seed"):
            self.append("session/end-seed", {})

    @staticmethod
    def create(id: str, seed=None, header=None) -> "Session":
        """分离式创建:校验并快照借用来的种子事件与存储元数据。"""
        return Session(id, seed, header)

    @staticmethod
    def from_restore(id: str, seed, header) -> "Session":
        """恢复式创建:接受新鲜持久化值的独占所有权。

        存储格式、事件信封、seq 连续性、表面迁移与 header 字段
        在冻结前全部校验。
        """
        return Session(id, seed, header, "restore")

    @property
    def surface(self) -> SessionSurface:
        """本会话事件日志上的有序表面(只读活投影)。"""
        return self._surface_manager

    @property
    def id(self) -> str:
        """会话身份,派生自其持久 header 的单一拷贝。"""
        return self.header["id"]

    @property
    def events(self) -> tuple:
        """事件日志的不可变快照。

        快照在下次 append 前复用;先前返回的数组不会随之增长。
        事件及其嵌套数据在接纳时已深度冻结,任何改写都会抛错。
        """
        if self._events_snapshot is None:
            self._events_snapshot = tuple(self._log)
        return self._events_snapshot

    @property
    def seq(self) -> int:
        """下一个事件的序号 —— 恒为日志长度(seq = log.length 契约)。"""
        return len(self._log)

    def append(self, type_: str, data, *, surface_op=None, source_event_seqs=None) -> dict:
        """向日志追加一个事件,并同步通知观察者。

        热路径从不阻塞 I/O —— 持久化插件异步缓冲。事件入日志即
        提交:观察者失败被逐监听者记录并包含,不改变返回值,也
        不阻止后续监听者看到同一已接受事件。

        surface_op/source_event_seqs 是表面元数据:消息产生类事件
        (user/message、assistant/message、tool/result)必须声明如何
        进入表面(表面是派生消息历史的唯一来源);非表面类型携带
        标记会被表面校验拒绝。

        返回入日志的事件 —— 其 seq/time 是分配值,data 是入日志
        的 SNAPSHOT(读 event["data"] 永远看到日志里的值,而非
        调用方仍可变的输入)。
        """
        surface_metadata = {}
        if source_event_seqs is not None:
            surface_metadata["sourceEventSeqs"] = source_event_seqs
        if surface_op is not None:
            surface_metadata["surfaceOp"] = surface_op
        data_snapshot = snapshot_json_value(data)
        if data_snapshot is None:
            raise ValueError(f'session event "{type_}" carries non-JSON-serializable data')
        assert_supported_request_header(type_, data_snapshot, f'session event "{type_}"')
        surface_metadata_snapshot = snapshot_json_value(surface_metadata)
        if surface_metadata_snapshot is None:
            raise ValueError(
                f'session event "{type_}" carries non-JSON-serializable surface metadata'
            )
        entry = attachments.get(self)
        if entry is not None and entry.appending:
            raise ValueError(
                "session append cannot reenter while another append is being published"
            )
        # 事件信封是冻结结构:data 是快照产物(已冻结),surface
        # 元数据同;外层信封一次构造,之后整体不可写。
        event = FrozenDict(
            {
                "type": type_,
                "seq": len(self._log),
                "time": _now_ms(),
                "data": data_snapshot,
                **surface_metadata_snapshot,
            }
        )
        self._surface_manager.validate_next(event)
        if entry is not None:
            entry.appending = True
        try:
            callbacks = None
            callback_args = [self, event]
            if entry is not None:
                # 发布快照在入日志前解析,回调在入日志后运行 ——
                # 提交原子性(DSH:listener snapshot resolves before
                # the log push, but callbacks run after it)。
                callbacks = collect_session_callbacks(
                    entry.emit_ctx, [entry.carrier, "session/event", *callback_args]
                )
            self._log.append(event)
            self._events_snapshot = None
            if callbacks is not None and entry is not None:
                invoke_contained_session_observers(
                    entry.emit_ctx, "session/event", entry.id, callback_args, callbacks
                )
            return event
        finally:
            if entry is not None:
                entry.appending = False
                if entry.detach_requested and not entry.announcing:
                    entry.detach()

    # 请求头折叠缓存(实例级,见 __init__)

    def request_header(self):
        """日志最后一条 request/header 事件之后生效的 EpochHeader。

        下一次请求将与它比较;尚无 header 事件时为 None。增量维护:
        每个 header 事件只折叠一次,逐步读取的成本是 O(新事件)。
        折叠产物冻结:按引用暴露的会话状态若可被原地改写,会与
        日志脱同步 —— 改写抛 TypeError。
        """
        if self._header_fold_seq < len(self._log):
            folded = fold_request_header(
                list(self._log[self._header_fold_seq:]), self._header_fold
            )
            # 尚无 header 事件时折叠结果为 None —— 快照器无法表达
            # 「null 之外的无值」,None 在此是合法状态,跳过冻结。
            self._header_fold = _freeze_json(folded) if folded is not None else None
            self._header_fold_seq = len(self._log)
        return self._header_fold

    # request/context 折叠缓存(实例级,见 __init__)

    def request_context(self):
        """最新解析的路由元数据;尚无 request/context 事件时为 None。

        每个事件折叠一次。data 已是冻结快照,这里只重建顶层
        (DSH 的 { ...event.data } 同款浅拷贝)后冻结。
        """
        if self._context_fold_seq < len(self._log):
            for event in self._log[self._context_fold_seq:]:
                if event["type"] == "request/context":
                    self._context_fold = FrozenDict(event["data"])
            self._context_fold_seq = len(self._log)
        return self._context_fold

    # 派生消息缓存(实例级,见 __init__)

    def derive_messages(self) -> list:
        """派生 LLM 消息历史:沿 surfaceOp 维护的有序消息产生事件。

        表面是派生历史的唯一来源:每个消息产生事件都记录其
        surfaceOp,裸事件(chunk、turn 边界)正确缺席,compaction
        的 replace 把被遮蔽节点从派生中删除。

        缓存:每个表面节点首次见到时投影一次,调用成本是
        O(新节点);表面重写(替换,replace_generation 变化)触发
        重建。返回的是每次调用的新鲜数组(后续 append 不会增长
        调用方已持有的数组);数组里的 Message 对象共享且深度冻结,
        内容复用已冻结的持久事件数据 —— 缓存无需二次深拷贝,
        消费者也改不动日志。
        """
        surface = self.surface
        nodes = surface.nodes
        generation = surface.replace_generation
        if generation != self._derived_generation:
            self._derived = []
            self._derived_nodes = 0
            self._derived_generation = generation
        for seq in nodes[self._derived_nodes:]:
            # 表面序列由本日志构建 —— seq 恒为合法索引。
            msg = self.derive_event_message(self._log[seq])
            # 空内容 assistant/message(只承载 usage 的 max-tokens
            # 步)投影为 None,不得进入记录。
            if msg is not None:
                self._derived.append(msg)
        self._derived_nodes = len(nodes)
        return list(self._derived)

    def derive_event_message(self, event: dict) -> dict | None:
        """纯函数 derive_event_message 的实例面(surface.py)。"""
        return _derive_event_message(event)


# ---- 分叉错误 ----


#: 分叉源:活会话对象或其在活仓库里的 id。
SessionForkSource = Session | str


class SessionForkError(Exception):
    """会话分叉拒绝的类型化错误,携带五码之一。

    - SESSION_NOT_FOUND:分叉源 id 在活仓库中不存在;
    - SESSION_NOT_LIVE:给出的会话对象不是仓库的活实例;
    - SESSION_ALREADY_EXISTS:请求的子会话 id 已被占用;
    - INVALID_BOUNDARY:边界不是连续存在的 seq;
    - OPEN_TURN:所选前缀结束在未关闭的 turn 内。
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code
        self.name = "SessionForkError"


# ---- 活会话仓库 ----


class SessionStore(Service):
    """内存会话仓库(ctx.sessions)。

    持久化故意不在这里实现 —— 持久化插件订阅 session/event,并在
    session/flush 与 dispose 时排干(DSH:persistence is intentionally
    not implemented here)。服务构造即注册:super().__init__(ctx)
    经 ctx.reflect.provide("sessions", self) 挂到 ctx 上。
    """

    provide = "sessions"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.store: dict[str, SessionEntry] = {}
        self.counter = 0
        # DSH 在此经 ctx.inject(['typert'], ...) 注册 typert 查找;
        # Python 移植把类型注册表内部化(typert.py,包内自建),
        # 无人消费该查找 —— 留作可扩展位,见 src/typert.py 注释。

    def create(self, id: str | None = None, options: dict | None = None) -> Session:
        """创建归属当前 fiber 的会话:卸载该 fiber 即停止通知并移除。

        options.seed 用这些事件的拷贝填充会话(重放/分叉);
        options.meta 附加创建元数据(校验过的绝对 cwd、血缘、
        委托深度)作为不可变 header(version/id/createdAt 由仓库补)。

        声明后同步抛错的 session/created 监听者可否决创建,并把
        附件回滚(配对销毁)而不是泄漏仓库条目与发布钩子 ——
        这正是把 enter 的 detach 先 yield 进 effect、再 announce 的
        原因(generator effect 同步失败时逆序回滚已收集的 disposer)。

        对「会话必须与其 agent 循环按序拆解」的组合(循环的收尾
        事件要在附件结束前发布),不要用本方法 —— 用 prepare +
        enter + announce 把生命周期折进 agent 自己的 effect。
        """
        session = self.prepare(id, options)

        def _create_effect():
            yield self.enter(session)
            self.announce(session)

        self.ctx.fiber.effect(_create_effect, "sessions.create()")
        return session

    def prepare(self, id: str | None = None, options: dict | None = None) -> Session:
        """构造会话但不入仓库:校验 id/cwd 并构建 Session。

        与 enter + announce 配对:持有复合 ctx.effect 的调用方
        (agent 工厂)把会话生命周期折进那一个 effect,使 fiber 卸载
        时会话 + agent 按一条有序链拆解 —— 而不是作为竞争兄弟
        effect —— 后者会在驱动者的收尾事件提交前移除发布钩子,
        丢掉事件。

        options:seed=种子事件;meta=创建元数据(createdAt 缺省
        取当前时刻,其余键缺省省略);seed_source='persistence' 时
        元数据与事件必须是独占所有权的新鲜分离图,经
        Session.from_restore 校验并原地冻结 —— 调用方不得保留
        可变别名。
        """
        if id is None:
            while True:
                self.counter += 1
                session_id = SessionId(f"session-{self.counter}")
                if session_id not in self.store:
                    break
        else:
            session_id = SessionId(id)
        if session_id in self.store:
            raise ValueError(f'session "{session_id}" already exists')
        options = options or {}
        if options.get("seed_source") == "persistence":
            return Session.from_restore(session_id, options["seed"], options["meta"])
        seed = options.get("seed")
        meta = options.get("meta") or {}
        header: dict = {
            "version": SESSION_FORMAT_VERSION,
            "id": session_id,
            "createdAt": meta.get("createdAt"),
        }
        # meta?.createdAt ?? Date.now():缺省(未提供/显式 None)取当前时刻。
        if header["createdAt"] is None:
            header["createdAt"] = _now_ms()
        for key in ("cwd", "parentSession", "seedLength", "origin", "delegationDepth", "agentPreset"):
            if meta.get(key) is not None:
                header[key] = meta[key]
        return Session.create(session_id, seed, header)

    def enter(self, session: Session):
        """把一个 prepare 过的会话放入仓库:安装发布钩子并加入。

        返回 DETACH disposer(钩子 + 仓库移除)。不发出
        session/created —— 调用方在自己的 effect 里先 yield 这个
        disposer 再 announce,使抛错的创建监听者能回滚附件。

        prepare 与 enter 是跨包的公开原语,调用方可能在两者之间
        插入任意工作(或另一次 create),所以重复 id 必须重查:
        一个过期的 prepared 会话不得覆盖同 id 的活条目 —— 它的
        detach 会删掉真正的会话。create 便捷方法与 agent 工厂
        背靠背调用,不会触发;公开 API 不能做此假设。

        从同步的 session/created 监听者里调用时,移除与销毁等到
        那次创建派发退栈。
        """
        id_ = session.id
        carrier = scope_target(session, scope_of(self.ctx))
        if id_ in self.store:
            raise ValueError(f'session "{id_}" already exists')
        if session in attachments:
            raise ValueError(f'session "{id_}" is already attached to a store')
        entry = SessionEntry(id_, session, carrier, self.ctx)
        entry.detach = lambda: self._detach_entered(entry)
        self.store[id_] = entry
        attachments[session] = entry
        entered = True

        def detach():
            nonlocal entered
            if not entered:
                return
            entered = False
            # 生命周期监听者可能持有高级 detach 能力:保持条目与
            # 发布钩子存活到同步的创建/append 派发退栈,然后发布
            # 配对销毁边。
            if entry.announcing or entry.appending:
                entry.detach_requested = True
                return
            entry.detach()

        return detach

    def _detach_entered(self, entry: SessionEntry) -> None:
        """移除一个精确的已入仓库会话,已宣布时发出配对销毁边。"""
        entry.detach_requested = False
        # 过期能力不得移除属于后续同 id 生命周期的观察者与存储
        # (enter 在一次性 detach 能力存活期间拒绝替换,这里兜底)。
        if self.store.get(entry.id) is not entry:
            return
        del self.store[entry.id]
        del attachments[entry.session]
        if entry.announced:
            self._emit_disposed(entry)

    def announce(self, session: Session) -> None:
        """对已 enter 的会话发出恰好一次 session/created。

        与 enter 分离,使调用方能先 yield detach disposer(回滚
        安全)。同步抛错的监听者否决发布;随之 yield 的 detach
        触发配对销毁边。异步监听者的拒绝太迟、无法回滚,记日志
        而不是变成未处理异常。
        """
        entry = self._live_entry_for(session)
        if entry.announced or entry.announcing:
            raise ValueError(f'session "{entry.id}" was already announced')
        # 先标记再派发:cordis 的派发可能先投递给较早的监听者再
        # 抛错,回滚必须仍把部分创建与销毁配对;监听者也不能
        # 递归地创建第二条生命周期边。
        entry.announced = True
        entry.announcing = True
        callback_args = [session]
        try:
            callbacks = collect_session_callbacks(
                self.ctx, [entry.carrier, "session/created", session]
            )
            for callback in callbacks:
                returned = callback(*callback_args)
                if inspect.isawaitable(returned):
                    _observe_rejection(returned, self.ctx, entry.id, "session/created")
        finally:
            entry.announcing = False
            if entry.detach_requested and not entry.appending:
                entry.detach()

    def _emit_disposed(self, entry: SessionEntry) -> None:
        """发出配对销毁通知,单监听者包含化。"""
        callback_args = [entry.session]
        try:
            callbacks = collect_session_callbacks(
                self.ctx, [entry.carrier, "session/disposed", entry.session]
            )
            invoke_contained_session_observers(
                self.ctx, "session/disposed", entry.id, callback_args, callbacks
            )
        except Exception as error:
            self.ctx.logger.warn(f'session "{entry.id}": session/disposed dispatch threw: {error}')

    async def flush(self, session: Session) -> bool:
        """派发被等待的 session/flush 耐久检查点。

        THE flush 入口:仓库持有 carrier,调用方(检查点策略的
        每请求屏障、空闲检查点、拆解排干、自读存储的消费者)必须
        走这里而不是裸派发 ctx.parallel —— 一个属主、一种拼写。
        每个监听者都成功落定后,返回是否至少一个耐久监听者参与;
        抛出第一个注册监听者的失败(所有监听者先全部落定)。
        """
        entry = self._live_entry_for(session)
        callback_args = [session]
        callbacks = collect_session_callbacks(
            self.ctx, [entry.carrier, "session/flush", session]
        )

        async def _settle(callback):
            # allSettled 语义:同步抛错与拒绝值都落定为失败结果。
            try:
                result = callback(*callback_args)
                if inspect.isawaitable(result):
                    return await result
                return result
            except BaseException as error:
                return error

        results = await asyncio.gather(*(_settle(cb) for cb in callbacks))
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return len(callbacks) > 0

    def _live_entry_for(self, session: Session) -> SessionEntry:
        """返回精确的活条目;分离/prepare 过的对象拒绝。"""
        entry = attachments.get(session)
        if entry is None or self.store.get(entry.id) is not entry:
            raise ValueError(f'session "{session.id}" is not live in this store')
        return entry

    def get(self, id: str) -> Session | None:
        """查活会话;无活会话持有该 id 时返回 None。"""
        entry = self.store.get(id)
        return entry.session if entry is not None else None

    def list(self) -> list:
        """全部活会话,按创建顺序;返回新数组,改动不影响仓库。"""
        return [entry.session for entry in self.store.values()]

    def fork(self, source, boundary: int | None = None, child_session_id: str | None = None) -> Session:
        """从活源会话的稳定前缀创建一个活子会话。

        boundary 是含端点的源事件 seq;缺省取源当前最后事件。所选
        切片可以以回合间事件结尾,但不得结束在打开的回合内。
        """
        if child_session_id is not None and self.get(child_session_id) is not None:
            raise SessionForkError(
                f'session "{child_session_id}" already exists', "SESSION_ALREADY_EXISTS"
            )
        live_source = self._resolve_fork_source(source)
        seed = self._fork_seed(live_source, boundary)
        meta: dict = {}
        if live_source.header.get("cwd") is not None:
            meta["cwd"] = live_source.header["cwd"]
        meta["parentSession"] = live_source.id
        meta["seedLength"] = len(seed)
        return self.create(child_session_id, {"seed": seed, "meta": meta})

    def _fork_seed(self, session: Session, requested_boundary: int | None) -> list:
        """解析分叉边界并切出种子切片(含端点)。"""
        events = session.events
        if requested_boundary is None:
            if not events:
                return []
            boundary = events[-1]["seq"]
        else:
            boundary = requested_boundary
        if not _is_safe_int(boundary) or boundary < 0:
            raise SessionForkError(
                f'fork boundary for session "{session.id}" must be a non-negative safe integer, got {boundary}',
                "INVALID_BOUNDARY",
            )
        if boundary >= len(events):
            last_seq = events[-1]["seq"] if events else None
            raise SessionForkError(
                f'fork boundary {boundary} does not exist in session "{session.id}" (last seq: {last_seq if last_seq is not None else "none"})',
                "INVALID_BOUNDARY",
            )
        boundary_event = events[boundary]
        if boundary_event is None or boundary_event["seq"] != boundary:
            raise SessionForkError(
                f'fork boundary {boundary} does not match a contiguous event seq in session "{session.id}"',
                "INVALID_BOUNDARY",
            )
        last_turn_boundary = None
        for event in events[: boundary + 1]:
            if event["type"] in ("turn/start", "turn/end"):
                last_turn_boundary = event
        if last_turn_boundary is not None and last_turn_boundary["type"] == "turn/start":
            raise SessionForkError(
                f'fork boundary {boundary} in session "{session.id}" ends inside open turn {last_turn_boundary["data"]["turn"]}',
                "OPEN_TURN",
            )
        return list(events[: boundary + 1])

    def _resolve_fork_source(self, source) -> Session:
        """解析分叉源:字符串走 id 查活;对象必须是仓库的活实例。"""
        if isinstance(source, str):
            session = self.get(source)
            if session is None:
                raise SessionForkError(f'session "{source}" not found', "SESSION_NOT_FOUND")
            return session
        live = self.get(source.id)
        if live is None:
            raise SessionForkError(f'session "{source.id}" not found', "SESSION_NOT_FOUND")
        if live is not source:
            raise SessionForkError(
                f'session "{source.id}" is not the live store instance', "SESSION_NOT_LIVE"
            )
        return source


# ---- 插件安装面 ----


def install(ctx) -> None:
    """安装 session 服务:构造即经 Service 基类注册,ctx.sessions 可用。

    与 llm_deepseek 的 install 不同,本包是服务的提供方而非消费方,
    不需要 install.inject 声明依赖 —— ctx.events/fiber/reflect/
    logger 均为 cordis 内建服务;scope 可选,缺省时 enter 以无标签
    路由工作(scope_of 返回 None,派发放行全部组合级监听者)。
    """
    SessionStore(ctx)
