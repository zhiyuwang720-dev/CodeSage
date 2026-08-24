"""共享缓冲、串行化、采纳、修复与拆解编排(第一方后端)。

DSH coordinator.ts 的 Python 移植。第三方后端可以直接实现公共
持久化接缝;第一方后端组合本协调器获得其余一切:缓冲、串行化、
游标、采纳、崩溃修复排序、拆解静默。

架构叙事:
- **每 id 串行化链**:同一会话的全部操作挂在一条链上依次执行,
  一个会话的写永不交错;错误不毒化链(下一位等待者照常运行)。
- **惰性物化**:create 只记录意图,首个 append 原子物化 header+事件;
  从未 append 的身份不留任何痕迹,reclaim 用它区分废弃 id 与
  持久化冲突。
- **崩溃修复**:读路径 adopt/snapshot 存储事件时内联执行 legacy
  迁移与回合平衡(interruptedTurnClosers);commitPrepared 把修复
  落盘后丢弃旧内存视图(修订号已变),重读精确提交后的图。
- **写路径**:活会话的 session/event 进 write-behind 缓冲,固定
  窗口后落盘;flush 是显式耐久屏障;dispose 逆序拆解(事件准入
  先关,再排干,再关后端)。

与 Node 的差异(注释即文档):
- AbortSignal 参数全部取消(Python 无原生等价物);取消语义由
  调用方在编排层自行处理。
- ``this.ctx.effect(...)`` → ``ctx.fiber.effect(execute, label)``
  (cordis-py 无 ctx.effect;fiber.effect 支持 async disposer,
  卸载时逆序 await)。
- ``Promise.withResolvers``/promise 链 → asyncio Future/_serialize
  链;``Promise.allSettled`` → asyncio.gather(return_exceptions=True)。
- 英文错误消息保留逐字(与 DSH 一致)。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Callable

from core.session import (
    KNOWN_SESSION_EVENT_TYPES,
    SESSION_FORMAT_VERSION,
    SessionPreparation,
    adopt_session_event,
    interrupted_turn_closers,
    snapshot_json_value,
    snapshot_session_event,
)

from .index import SessionInspection, SessionLocation
from .preparations import SessionPreparations
from .revision import SessionPersistenceRevision
from .write_behind import SessionWriteBehind

#: 协调器保留的分离会话准备条目数(超出按 LRU 淘汰)。
DEFAULT_PREPARED_SESSION_CACHE_SIZE = 5

#: 活会话批次开始写前的最大有意等待。
DEFAULT_WRITE_BATCH_MAX_DELAY_MS = 200

#: Node 定时器实现接受的最大写批延迟(2^31-1,MAX_TIMER_DELAY_MS)。
MAX_WRITE_BATCH_DELAY_MS = 2147483647

#: Number.MAX_SAFE_INTEGER(TS 安全整数上界)。
_MAX_SAFE_INTEGER = 2**53 - 1


class SessionPersistenceCorruptionError(Exception):
    """后端读取成功后,耐久会话内容未通过验证。"""

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.name = "SessionPersistenceCorruptionError"
        self.cause = cause


class SessionFormatUnsupportedError(Exception):
    """存储日志完好,但本构建无法忠实解释:header 携带不支持的
    格式版本,或事件的类型本构建未知且未被标记 ignorable。

    与 SessionPersistenceCorruptionError 不同 —— 没有损坏;后端按
    会话保留工件时,原始日志在 location 仍可读。
    """

    def __init__(self, message: str, location: SessionLocation | None = None) -> None:
        super().__init__(message)
        self.name = "SessionFormatUnsupportedError"
        self.location = location


class AggregateError(Exception):
    """JS AggregateError 的 Python 等价物:携带一组被收集的排干错误。

    Python 无内建同名异常(ExceptionGroup 需要 3.11+ 且语义不同),
    这里按 JS 语义定义 —— message 可选,errors 按收集顺序保留。
    """

    def __init__(self, errors: list, message: str = "") -> None:
        super().__init__(message)
        self.errors = errors


def sessionFormatVersionRefusal(id: str, version: int) -> str:
    """对携带本构建读不了的格式版本的存储会话,给出方向性拒绝文本。

    协调器的加载时检查与必须在解码版本相关结构**之前**拒绝的后端
    共享此文本:未来格式可能连今天的结构检查都过不了,用户必须
    看到「升级 harness」而不是「损坏」。
    """
    if version > SESSION_FORMAT_VERSION:
        return (
            f'session "{id}" uses log format v{version}, but this harness reads only '
            f"v{SESSION_FORMAT_VERSION}: the log was written by a newer harness — "
            "upgrade the harness to open it"
        )
    return (
        f'session "{id}" uses log format v{version}, older than the supported '
        f"v{SESSION_FORMAT_VERSION}, and this build ships no upgrade path for it"
    )


class StoredPrefix:
    """一个存储会话的 header、有效连续事件前缀、源限定修订号与
    可选的撕裂尾标记。修订号标识确切的前缀;协调器只检查标记
    是否存在并把它的值交还 commitRepair —— 标记类型由后端所有。
    """

    __slots__ = ("meta", "events", "revision", "tornMarker")

    def __init__(self, meta: dict, events: list, revision: SessionPersistenceRevision, tornMarker: Any = None) -> None:
        self.meta = meta
        self.events = events
        self.revision = revision
        self.tornMarker = tornMarker


class StoredSuffix:
    """一个存储会话的 header 加 seq >= fromSeq 的事件(可寻址后缀读
    的返回形状)。非变更读不携带撕裂标记:无可修复。
    """

    __slots__ = ("meta", "events")

    def __init__(self, meta: dict, events: list) -> None:
        self.meta = meta
        self.events = events


class PersistenceBackend:
    """PersistenceCoordinator 与具体后端之间的存储契约:编排调用的
    最小耐久原语集合。后端(文件/行/对象存储…)实现这些;协调器
    提供其余一切。

    TornMarker 是后端的不透明撕裂尾修复令牌,协调器完全当它不透明。
    方法(除 name 外均为后端必须实现或可选钩子):
      loadStored / readStoredRevision / loadStoredFrom(可选)/
      appendBatch / commitRepair / list / locate(可选)/ close(可选)
    """

    name: str = ""


async def _settled_errors(promises) -> list:
    """收集一组 promise 的拒绝原因(本身不抛)。"""
    results = await asyncio.gather(*promises, return_exceptions=True)
    return [r for r in results if isinstance(r, BaseException)]


def _is_safe_integer(value: Any) -> bool:
    """TS Number.isSafeInteger:整数且在安全整数界内(排除 bool)。"""
    return isinstance(value, int) and not isinstance(value, bool) and abs(value) <= _MAX_SAFE_INTEGER


def _is_number(value: Any) -> bool:
    """TS typeof === 'number':int/float 均可,排除 bool。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_record(value: Any) -> dict | None:
    """返回对象记录,不把数组扩成消息负载(TS asRecord)。"""
    return value if isinstance(value, dict) else None


def _has_only_keys(record: dict, required: list, optional: list | None = None) -> bool:
    """记录是否含全部必需键且无可选扩展集之外的键。"""
    optional = optional or []
    allowed = set(required) | set(optional)
    return all(key in allowed for key in record) and all(key in record for key in required)


def _seed_covers_prefix(seed: list, prefix: list) -> bool:
    """活会话种子是否逐字重现持久化前缀(DSH JSON.stringify 等价:
    键序一致的冻结图,json.dumps 值比较)。"""
    if len(prefix) > len(seed):
        return False
    for index, event in enumerate(prefix):
        seedEvent = seed[index]
        if seedEvent is None or json.dumps(seedEvent) != json.dumps(event):
            return False
    return True


def _assert_supported_events(events: list, id: str) -> None:
    """拒绝本构建无法重放的 v0 旧词表事件(逐个找首个命中,照 DSH
    的 find 语义)。"""
    for event in events:
        if event.get("type") == "request/header-delta":
            raise RuntimeError(
                f'session "{id}" contains unsupported legacy request/header-delta event at seq {event.get("seq")}'
            )
        if event.get("type") == "mode/set":
            raise RuntimeError(
                f'session "{id}" contains unsupported legacy mode/set event at seq {event.get("seq")}'
            )
        data = _as_record(event.get("data"))
        if event.get("type") == "request/header" and data is not None and data.get("reason") == "fallback":
            raise RuntimeError(
                f'session "{id}" contains unsupported legacy request/header reason "fallback" at seq {event.get("seq")}'
            )


def _legacy_message_id(id: str, seq: int) -> str:
    """为身份机制存在前持久化的消息铸稳定导入身份。"""
    return f"legacy-message:{id}:{seq}"


def _replacement_start(event: dict) -> int | None:
    """读替换目标,把畸形 surface 元数据留给会话校验器。"""
    op = _as_record(event.get("surfaceOp"))
    if op is None or op.get("op") != "replace" or not _is_number(op.get("start")):
        return None
    return op["start"]


def _needs_legacy_prefix(event: dict) -> bool:
    """一个后缀事件是否需要只有前缀存储才具备的事实。"""
    data = _as_record(event.get("data"))
    if event.get("type") == "steering/message":
        return True
    if data is None:
        return False
    if event.get("type") == "user/message":
        return "id" not in data and "content" in data
    if event.get("type") == "assistant/message":
        return "message" not in data and "content" in data
    if event.get("type") == "tool/result":
        return "message" not in data and "callId" in data
    return False


def _migrate_legacy_steering_event(event: dict, id: str) -> dict:
    """把已移除的 steering 表面事件升级为当前 user/message 等价物。"""
    if event.get("type") != "steering/message":
        return event
    data = _as_record(event.get("data"))
    if data is None:
        raise RuntimeError(
            f'session "{id}" contains malformed pre-react-loop steering/message at seq {event.get("seq")}'
        )
    wrapped = _as_record(data.get("message"))
    if wrapped is not None and _is_safe_integer(data.get("turn")) and _has_only_keys(data, ["turn", "message"]):
        return {**event, "type": "user/message", "data": wrapped}
    if not _is_safe_integer(data.get("turn")) or not _has_only_keys(data, ["turn", "content", "source"]):
        raise RuntimeError(
            f'session "{id}" contains malformed pre-react-loop steering/message at seq {event.get("seq")}'
        )
    message = {k: v for k, v in data.items() if k != "turn"}
    return {
        **event,
        "type": "user/message",
        "data": {
            **message,
            "id": _legacy_message_id(id, event.get("seq")),
            "role": "user",
        },
    }


def _migrate_legacy_turn_start_event(event: dict, id: str) -> dict:
    """校验完整的旧 turn/start 信封后移除过时 trigger。"""
    if event.get("type") != "turn/start":
        return event
    data = _as_record(event.get("data"))
    if data is None or "trigger" not in data:
        return event
    trigger = _as_record(data.get("trigger"))
    if (
        not _is_safe_integer(data.get("turn"))
        or data.get("turn") < 1
        or not _has_only_keys(data, ["turn", "trigger"])
        or trigger is None
        or not isinstance(trigger.get("kind"), str)
        or len(trigger.get("kind")) == 0
    ):
        raise RuntimeError(
            f'session "{id}" contains malformed pre-react-loop turn/start at seq {event.get("seq")}'
        )
    return {**event, "data": {"turn": data["turn"]}}


def _migrate_legacy_turn_end_event(event: dict, id: str) -> dict:
    """升级旧回合结尾,保留最新 master 信封(逐字移植 DSH)。"""
    if event.get("type") != "turn/end":
        return event
    data = _as_record(event.get("data"))
    # 非记录当前信封不可能匹配旧形状。
    if data is None:
        return event

    def malformed():
        raise RuntimeError(
            f'session "{id}" contains malformed pre-react-loop turn/end at seq {event.get("seq")}'
        )

    reason = _as_record(data.get("reason"))
    if (
        not _is_safe_integer(data.get("turn"))
        or data.get("turn") < 1
        or not _has_only_keys(data, ["turn", "reason"])
        or reason is None
        or not isinstance(reason.get("kind"), str)
    ):
        return malformed()

    currentReason = None
    kind = reason["kind"]
    if kind in ("completed", "blocked", "max-tokens", "interrupted"):
        if not _has_only_keys(reason, ["kind"]):
            return malformed()
        return event
    if kind == "aborted":
        if "reason" in reason:
            return event
        if not _has_only_keys(reason, ["kind"]):
            return malformed()
        currentReason = {"kind": "aborted", "reason": {"kind": "legacy"}}
    elif kind == "disposed":
        if not _has_only_keys(reason, ["kind"]):
            return malformed()
        currentReason = {"kind": "aborted", "reason": {"kind": "disposed"}}
    elif kind == "error":
        if "error" in reason:
            return event
        if not _is_safe_integer(reason.get("step")) or reason.get("step") < 0:
            return malformed()
        failure = _as_record(reason.get("failure"))
        if (
            failure is not None
            and _has_only_keys(reason, ["kind", "step", "failure"])
            and _has_only_keys(failure, ["message", "code"], ["status", "providerRetryAfterMs", "requestId"])
            and isinstance(failure.get("message"), str)
            and isinstance(failure.get("code"), str)
            and (failure.get("status") is None or _is_number(failure.get("status")))
            and (failure.get("providerRetryAfterMs") is None or _is_number(failure.get("providerRetryAfterMs")))
            and (failure.get("requestId") is None or isinstance(failure.get("requestId"), str))
        ):
            currentReason = {"kind": "error", "error": failure}
        else:
            messageKeys = (
                ["kind", "step", "message"]
                if reason.get("code") is None
                else ["kind", "step", "message", "code"]
            )
            if (
                not _has_only_keys(reason, messageKeys)
                or not isinstance(reason.get("message"), str)
                or (reason.get("code") is not None and not isinstance(reason.get("code"), str))
            ):
                return malformed()
            currentReason = {
                "kind": "error",
                "error": {
                    "message": reason["message"],
                    "code": reason["code"] if isinstance(reason.get("code"), str) else "UNKNOWN",
                },
            }
    else:
        return event

    return {**event, "data": {**data, "reason": currentReason}}


def _migrate_legacy_message_event(event: dict, id: str, messageIds: dict) -> dict:
    """把一条身份机制前的消息事件升级为当前包装形状。

    看起来像当前形状的畸形事件保持原样,由校验器拒绝 —— 不把
    损坏伪装成 legacy 数据。
    """
    data = _as_record(event.get("data"))
    if data is None:
        return event
    if event.get("type") == "user/message":
        if (
            "id" in data
            or "role" in data
            or "message" in data
            or "content" not in data
            or "source" not in data
        ):
            return event
        return {
            **event,
            "data": {
                **data,
                "id": _legacy_message_id(id, event.get("seq")),
                "role": "user",
            },
        }
    if event.get("type") == "assistant/message":
        if "message" in data or "content" not in data or "provenance" not in data:
            return event
        eventData = {k: v for k, v in data.items() if k not in ("content", "provenance")}
        provenance = _as_record(data.get("provenance"))
        return {
            **event,
            "data": {
                **eventData,
                "message": {
                    "id": _legacy_message_id(id, event.get("seq")),
                    "role": "assistant",
                    "content": data["content"],
                    "source": {
                        **(provenance if provenance is not None else {}),
                        "kind": "model",
                    },
                },
            },
        }
    if event.get("type") == "tool/result":
        if (
            "message" in data
            or "callId" not in data
            or "content" not in data
            or "isError" not in data
        ):
            return event
        eventData = {k: v for k, v in data.items() if k not in ("callId", "content", "isError")}
        inheritedId = _replacement_start(event)
        return {
            **event,
            "data": {
                **eventData,
                "message": {
                    "id": (
                        _legacy_message_id(id, event.get("seq"))
                        if inheritedId is None
                        else messageIds.get(inheritedId)
                    ),
                    "role": "user",
                    "content": [
                        {
                            "type": "tool-result",
                            "toolCallId": data["callId"],
                            "content": data["content"],
                            "isError": data["isError"],
                        }
                    ],
                    "source": {"kind": "tool", "callId": data["callId"]},
                },
            },
        }
    return event


def _event_message_id(event: dict) -> str | None:
    """读一个已验证当前事件携带的身份消息。"""
    data = _as_record(event.get("data"))
    if event.get("type") == "user/message":
        message = data
    else:
        message = _as_record(data.get("message")) if data is not None else None
    if message is None or not isinstance(message.get("id"), str):
        return None
    return message["id"]


def _snapshot_stored_events(events: list, id: str) -> list:
    """把存储事件物化为升级、验证后的快照(不可变消息)。"""
    _assert_supported_events(events, id)
    messageIds = {}
    result = []
    for event in events:
        migratedStart = _migrate_legacy_turn_start_event(event, id)
        migratedTurn = _migrate_legacy_turn_end_event(migratedStart, id)
        migratedSteering = _migrate_legacy_steering_event(migratedTurn, id)
        snapshot = snapshot_session_event(_migrate_legacy_message_event(migratedSteering, id, messageIds))
        messageId = _event_message_id(snapshot)
        if messageId is not None:
            messageIds[snapshot["seq"]] = messageId
        result.append(snapshot)
    return result


def _adopt_stored_events(events: list, id: str) -> list:
    """升级并验证一个独占拥有的后端结果,原地不拷贝。"""
    _assert_supported_events(events, id)
    messageIds = {}
    for index, event in enumerate(events):
        migratedStart = _migrate_legacy_turn_start_event(event, id)
        migratedTurn = _migrate_legacy_turn_end_event(migratedStart, id)
        migratedSteering = _migrate_legacy_steering_event(migratedTurn, id)
        adopted = adopt_session_event(_migrate_legacy_message_event(migratedSteering, id, messageIds))
        events[index] = adopted
        messageId = _event_message_id(adopted)
        if messageId is not None:
            messageIds[adopted["seq"]] = messageId
    return events


class SessionState:
    """协调器内存簿记里一个会话的写状态。"""

    __slots__ = ("meta", "cursor", "materialized", "owner")

    def __init__(self, meta: dict, cursor: int, materialized: bool, owner=None) -> None:
        self.meta = meta  #: 会话 header
        self.cursor = cursor  #: 后端期望追加的下一 seq(存储日志长度)
        self.materialized = materialized  #: 惰性创建是否已产生耐久工件
        self.owner = owner  #: 经 onCreated 绑定的活 Session(若有)


class LiveSessionState:
    """一个活会话的初始化与有界写合并控制器。"""

    __slots__ = ("init", "writes")

    def __init__(self, init, writes) -> None:
        self.init = init  #: 初始化的 promise(种子物化/采纳)
        self.writes = writes  #: SessionWriteBehind


class PreparedSessionSource:
    """一个已验证冷源与从它构建的精确未发布 Session。"""

    __slots__ = ("inspection", "session", "revision", "sessionLength", "tornMarker", "closers")

    def __init__(self, inspection, session, revision, sessionLength, tornMarker, closers) -> None:
        self.inspection = inspection
        self.session = session
        self.revision = revision
        self.sessionLength = sessionLength  #: 构造器持有的种子标记追加后的会话长度
        self.tornMarker = tornMarker
        self.closers = closers


def _resolved_future() -> asyncio.Future:
    """一个已落定的 Future(TS Promise.resolve 等价物)。"""
    future = asyncio.get_running_loop().create_future()
    future.set_result(None)
    return future


def _observe_rejection(future: asyncio.Future) -> None:
    """取回 future 的拒绝值(避免「exception was never retrieved」),
    实际等待方仍通过 await 看到真实异常。"""

    def _retrieve(_future):
        if _future.done() and not _future.cancelled():
            _future.exception()

    future.add_done_callback(_retrieve)


class PersistenceCoordinator:
    """拥有后端无关的会话写路径编排。

    后端构造一个 ``PersistenceCoordinator(ctx, self)``、实现
    PersistenceBackend,并把写/读服务方法委托给协调器对应方法。
    构造器安装写路径监听者、每会话退休与后端拆解 effect。

    所有按 id 的操作都串行化(每 id 一条 promise 链):并发 flush /
    flush 撞 load 永不交错存储写。
    """

    def __init__(self, ctx, backend: PersistenceBackend, options: dict | None = None) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 链式串行化、写合并定时器与拆解排干都是 asyncio 基建,
            # 同步构造是无意义的用法 —— 给出明确诊断而非深层 RuntimeError。
            raise RuntimeError(
                "PersistenceCoordinator must be constructed within a running event loop"
            ) from None
        if options is None:
            options = {
                "preparedSessionCacheSize": DEFAULT_PREPARED_SESSION_CACHE_SIZE,
                "writeBatchMaxDelayMs": DEFAULT_WRITE_BATCH_MAX_DELAY_MS,
            }
        preparedCacheSize = options.get("preparedSessionCacheSize")
        if not _is_safe_integer(preparedCacheSize) or preparedCacheSize < 1:
            raise TypeError("preparedSessionCacheSize must be a positive safe integer")
        writeDelay = options.get("writeBatchMaxDelayMs")
        if (
            not _is_safe_integer(writeDelay)
            or writeDelay < 1
            or writeDelay > MAX_WRITE_BATCH_DELAY_MS
        ):
            raise TypeError(
                f"writeBatchMaxDelayMs must be an integer between 1 and {MAX_WRITE_BATCH_DELAY_MS}"
            )
        self.ctx = ctx
        self.backend = backend
        self.writeBatchMaxDelayMs = writeDelay
        #: 后端簿记,按会话 id(不是活 Session 对象)。
        self.states: dict[str, SessionState] = {}
        #: 生命周期与写合并状态,按精确的活 Session。
        self.live: dict = {}
        #: 已精确拆解的生命周期,其缓冲尾仍在排干。
        self.retirements: dict[str, asyncio.Future] = {}
        #: 共享冷读、未发布预留与完成的 LRU 条目。
        self.preparations: SessionPreparations = SessionPreparations(preparedCacheSize)
        #: 每会话串行化:同一 id 的每个操作排在前一个之后。
        self.chains: dict[str, asyncio.Future] = {}
        self.installWritePath()

    # --- 公共 API(后端的服务方法委托到这里)---

    async def create(self, meta: dict) -> None:
        """为惰性创建注册分离的会话元数据;重复的已跟踪/已持久化
        id 拒绝。"""
        # 排队前快照:调用方的后续变更不得让键与 header 分叉。
        snapshot = snapshot_json_value(meta)
        if snapshot is None:
            raise TypeError("session metadata must be losslessly JSON-serializable")
        if not _is_safe_integer(snapshot.get("createdAt")) or snapshot["createdAt"] < 0:
            raise TypeError("session metadata createdAt must be a non-negative safe integer")
        await self._serialize(snapshot["id"], lambda: self._createCore(snapshot))

    async def _createCore(self, meta: dict) -> None:
        # 绝不覆盖既有会话:SessionId 本身就是身份。
        if meta["id"] in self.states or self.preparations.has(meta["id"]):
            raise RuntimeError(f'session "{meta["id"]}" already exists in this backend')
        # 该 id 下(任意 scope)已存在工件就挡住创建:load/resume 只按
        # id 识别会话,第二个工件会让 resume 不确定。
        if await self.backend.loadStored(meta["id"]) is not None:
            raise RuntimeError(
                f'session "{meta["id"]}" already has a persisted log on disk; load/resume it instead of creating'
            )
        # 纯惰性:只记录意图。首个 append 前无工件。
        self.states[meta["id"]] = SessionState(meta, 0, False)

    async def append(self, id: str, events: list) -> None:
        """耐久持久化一批事件,遵守追加式与连续 seq 契约;拒绝
        非 JSON 可序列化的 event.data。"""
        # 在排队等链之前,一次遍历完成整批的校验与深快照:检查后
        # 的取值与持久化的值严格一致(单遍物化,不重读访问器)。
        batch = snapshot_json_value(events)
        if batch is None:
            raise TypeError(
                "session event batch is not losslessly JSON-serializable because it contains non-JSON-serializable data"
            )
        await self._serialize(id, lambda: self._appendCore(id, batch))

    async def _appendCore(self, id: str, events: list) -> None:
        # 全部 append 路由在此汇聚:公共服务、活 write-behind 排干、
        # HMR 种子/后缀采纳。legacy 形状拒绝停留在这一共享边界,
        # 过时的插件不能持久化本后端拒绝装载的形状。未知类型守卫
        # 刻意只放在读侧:append 时拒绝会让活会话的耐久性半路
        # 停摆,比日志下次装载时大声拒绝更贵(取舍归日志版本机制)。
        _assert_supported_events(events, id)
        if len(events) == 0:
            return
        self.preparations.assertWritable(id)
        state = self.states.get(id)
        if state is None:
            state = await self._adopt(id)

        # 连续性契约:每个事件的 seq 必须续上存储日志。
        for i, event in enumerate(events):
            if event["seq"] != state.cursor + i:
                raise RuntimeError(
                    f'append seq mismatch for "{id}": expected {state.cursor + i} at index {i}, got {event["seq"]}'
                )

        await self.backend.appendBatch(state.meta, events, state.materialized)
        # 耐久写即事务:一旦提交就标记物化并推进游标(跨后端一致)。
        state.materialized = True
        state.cursor += len(events)
        self.preparations.invalidate(id)

    async def prepare(self, id: str) -> SessionPreparation:
        """准备并预留 resume 用的精确未发布 Session。

        修订号重试在耐久日志保持一个读/查往返不变后收敛;持续
        的外部写者可能延迟完成。返回一份发布或回滚后释放的所有权。
        """
        while True:
            await self._waitForRetirement(id)
            if self.ctx.sessions.get(id) is not None:
                raise RuntimeError(f'cannot prepare session "{id}" while it is live')
            reservation = await self.preparations.reserve(
                id,
                lambda: self._serialize(id, lambda: self._prepareCore(id)),
                lambda source: self._serialize(id, lambda: self._commitPrepared(source)),
            )
            if reservation is None:
                continue
            if self.ctx.sessions.get(id) is not None:
                self.preparations.release(reservation, False)
                raise RuntimeError(f'cannot prepare session "{id}" while it is live')
            return SessionPreparation.create(
                reservation.source.session,
                {
                    "release": lambda: self.preparations.release(
                        reservation,
                        reservation.state.owner is None
                        and len(reservation.source.session.events) == reservation.source.sessionLength,
                    )
                },
            )

    async def load(self, id: str) -> SessionInspection:
        """提交恢复并返回其不可变逻辑视图,不发布。"""
        while True:
            await self._waitForRetirement(id)
            live = self.ctx.sessions.get(id)
            if live is not None:
                return await self._loadLiveSnapshot(live)
            reservation = await self.preparations.reserve(
                id,
                lambda: self._serialize(id, lambda: self._prepareCore(id)),
                lambda source: self._serialize(id, lambda: self._commitPrepared(source)),
            )
            if reservation is None:
                continue
            attached = self.ctx.sessions.get(id)
            if attached is not None:
                self.preparations.discard(reservation)
                return await self._loadLiveSnapshot(attached)
            self.preparations.discard(reservation)
            return reservation.source.inspection

    async def inspect(self, id: str) -> SessionInspection:
        """不发布也不提交恢复地检查一个逻辑会话。

        过期的 ready 源被重载;已处于 committing/reserved 的源保持
        独占,检查可以借用它的不可变视图。
        """
        while True:
            if id in self.retirements:
                await self._waitForRetirement(id)
            live = self.ctx.sessions.get(id)
            if live is not None:
                return self._inspectLive(live)
            try:
                source = await self.preparations.inspect(
                    id, lambda: self._serialize(id, lambda: self._prepareCore(id))
                )
                attached = self.ctx.sessions.get(id)
                if attached is not None:
                    return self._inspectLive(attached)
                current = await self._serialize(id, lambda: self._isPreparedSourceCurrent(source))
                published = self.ctx.sessions.get(id)
                if published is not None:
                    return self._inspectLive(published)
                if current:
                    return source.inspection
                if self.preparations.discardReady(id, source) == "retained":
                    return source.inspection
            except BaseException:
                attached = self.ctx.sessions.get(id)
                if attached is not None:
                    return self._inspectLive(attached)
                raise

    async def readFrom(self, id: str, fromSeq: int) -> dict:
        """从 fromSeq 起读存储事件,分离且非变更(服务 readFrom 背后
        的从 seq 读原语)。与写同链;带 loadStoredFrom 钩子的后端
        只读后缀,其余后端读前缀在此前跳。"""
        if not _is_safe_integer(fromSeq) or fromSeq < 0:
            raise TypeError(f"readFrom fromSeq must be a non-negative safe integer, got {fromSeq}")
        retirement = self.retirements.get(id)
        if retirement is not None:
            await retirement
        return await self._serialize(id, lambda: self._readFromCore(id, fromSeq))

    async def _readFromCore(self, id: str, fromSeq: int) -> dict:
        loadStoredFrom = getattr(self.backend, "loadStoredFrom", None)
        if loadStoredFrom is not None:
            suffix = await loadStoredFrom(id, fromSeq)
            if suffix is None:
                raise RuntimeError(f'session "{id}" not found')
            self._assertStoredId(id, suffix.meta)
            self._assertVersion(suffix.meta)
            if any(_needs_legacy_prefix(event) for event in suffix.events):
                # 后缀含需要更早消息身份事实的 legacy 形状:回退完整前缀。
                whole = await self._readStoredPrefix(id)
                return {"meta": whole["meta"], "events": [e for e in whole["events"] if e["seq"] >= fromSeq]}
            events = _snapshot_stored_events(suffix.events, id)
            self._assertEventsSupported(suffix.meta, events)
            return {"meta": dict(suffix.meta), "events": events}
        whole = await self._readStoredPrefix(id)
        # 顺序回退:从 0 连续 seq 使后缀成为索引切片。
        return {"meta": whole["meta"], "events": whole["events"][fromSeq:]}

    async def _readStoredPrefix(self, id: str) -> dict:
        """读一个分离的物理前缀,无逻辑恢复、无缓存。"""
        stored = await self.backend.loadStored(id)
        if stored is None:
            raise RuntimeError(f'session "{id}" not found')
        self._assertStoredId(id, stored.meta)
        self._assertVersion(stored.meta)
        events = _snapshot_stored_events(stored.events, id)
        self._assertEventsSupported(stored.meta, events)
        return {"meta": dict(stored.meta), "events": events}

    async def _prepareCore(self, id: str) -> PreparedSessionSource:
        """读、内存修复、验证并冻结一个冷源,恰一次。"""
        stored = await self.backend.loadStored(id)
        if stored is None:
            raise RuntimeError(f'session "{id}" not found')
        try:
            meta = stored.meta
            events = stored.events
            self._assertStoredId(id, meta)
            self._assertVersion(meta)
            storedEvents = _adopt_stored_events(events, id)
            self._assertEventsSupported(meta, storedEvents)

            # 保留完整的中断事件,只合成缺失的关闭器。
            closers = [adopt_session_event(e) for e in interrupted_turn_closers(storedEvents)]
            balanced = storedEvents + closers
            session = self.ctx.sessions.prepare(
                id, {"seed": balanced, "meta": meta, "seed_source": "persistence"}
            )
            inspection = SessionInspection(session.header, balanced)
            return PreparedSessionSource(
                inspection, session, stored.revision, len(session.events), stored.tornMarker, closers
            )
        except BaseException as error:
            # 不支持的格式是对完好日志的拒绝而非损坏 —— 原样抛出,
            # 让调用方能指向原始工件。
            if isinstance(error, SessionFormatUnsupportedError):
                raise
            raise SessionPersistenceCorruptionError(
                f'stored session "{id}" failed validation: {error}', cause=error
            ) from error

    async def _commitPrepared(self, source: PreparedSessionSource) -> tuple | None:
        """提交一份已准备的修复并建立其无属主的耐久游标。"""
        id = source.inspection.meta["id"]
        cursor = len(source.inspection.events)
        existing = self.states.get(id)
        if existing is not None and existing.owner is not None:
            raise RuntimeError(f'session "{id}" already has a live persistence owner')
        if not await self._isPreparedSourceCurrent(source):
            return None
        if source.tornMarker is not None or len(source.closers) > 0:
            await self.backend.commitRepair(source.inspection.meta, source.tornMarker, source.closers)
            # 修复改变了耐久修订号:重读精确提交后的图,而不是把旧
            # 内存视图关联到更新的修订号。
            return None
        state = existing if existing is not None else SessionState(source.inspection.meta, cursor, True)
        state.meta = source.inspection.meta
        state.cursor = cursor
        state.materialized = True
        self.states[id] = state
        return (source, state)

    async def _isPreparedSourceCurrent(self, source: PreparedSessionSource) -> bool:
        """一个缓存源是否仍指向当前耐久日志修订号。"""
        return await self.backend.readStoredRevision(source.inspection.meta["id"]) == source.revision

    async def _loadLiveSnapshot(self, session) -> SessionInspection:
        """返回一个已活 Session 的耐久不可变视图。"""
        events = session.events
        await self._flush(session)
        state = self.states.get(session.id)
        # 成功的 flush 必然发布了这个活会话的耐久状态。
        if state is None:
            raise RuntimeError(f'session "{session.id}" lost persistence state during load')
        if len(events) == 0:
            raise RuntimeError(f'session "{session.id}" not found')
        if len(interrupted_turn_closers(events)) > 0:
            raise RuntimeError(
                f'cannot load session "{session.id}" while its live turn is open; use the live Session or wait for the turn to close'
            )
        return SessionInspection(state.meta, events)

    @staticmethod
    def _inspectLive(session) -> SessionInspection:
        """借用已活 Session 的不可变视图。"""
        return SessionInspection(session.header, session.events)

    async def _waitForRetirement(self, id: str) -> None:
        """等待一个退休中的生命周期(无调用方取消参数)。"""
        retirement = self.retirements.get(id)
        if retirement is not None:
            await retirement

    # 列举是后端直读,不需要协调器状态。

    # --- 每 id 串行化 + 采纳辅助 ---

    def _serialize(self, id: str, op: Callable[[], Any]) -> asyncio.Future:
        """在同一会话 id 的进行中操作之后运行 op,一个会话的写永不
        交错。错误不毒化链。注意:被串行化的公共方法不得互相调用
        (会死锁),它们调用未串行化的 ``*Core`` 辅助。"""
        prior = self.chains.get(id)

        async def _chain_runner():
            if prior is not None:
                try:
                    await prior
                except BaseException:
                    pass  # 前驱失败照常排队(TS then(run, run))
            try:
                result = op()
                if inspect.isawaitable(result):
                    result = await result
            except BaseException as error:
                # 调用方经 next_future 看到真实拒绝(TS 的 next 会拒绝);
                # 本链尾吞掉异常(TS 的 tail),下一位等待者只看到落定。
                if not next_future.done():
                    next_future.set_exception(error)
                if self.chains.get(id) is chain_task:
                    del self.chains[id]
                return
            if not next_future.done():
                next_future.set_result(result)
            # 已落定的尾链没有串行化价值;只删上面装的确切尾链
            # (更晚的操作可能已替换它)。
            if self.chains.get(id) is chain_task:
                del self.chains[id]

        next_future = asyncio.get_running_loop().create_future()
        chain_task = asyncio.ensure_future(_chain_runner())
        self.chains[id] = chain_task
        return next_future

    async def _adopt(self, id: str) -> SessionState:
        """为存储中发现但内存还没有的会话建状态。

        在 id 的串行化链内运行,因此用 Core 辅助而不是重入公共
        prepare/load 方法。
        """
        while True:
            source = self.preparations.takeReady(id)
            if source is None:
                source = await self._prepareCore(id)
            committed = await self._commitPrepared(source)
            if committed is not None:
                return committed[1]

    def _assertVersion(self, meta: dict) -> None:
        if meta.get("version") == SESSION_FORMAT_VERSION:
            return
        raise self._unsupported(meta, sessionFormatVersionRefusal(meta["id"], meta["version"]))

    def _assertEventsSupported(self, meta: dict, events: list) -> None:
        """拒绝含本构建不知道的事件类型的日志,除非写者标记了
        ignorable:未识别的必需事件可能改变其余日志的解释方式,
        静默跳过会重建出错误的会话(事件信封契约)。运行在已
        规范化的事件上 —— legacy 形状已升级或已给出各自的具体
        诊断。"""
        for event in events:
            if event["type"] in KNOWN_SESSION_EVENT_TYPES or event.get("ignorable") is True:
                continue
            raise self._unsupported(
                meta,
                f'session "{meta["id"]}" contains event type "{event["type"]}" (seq {event["seq"]}) unknown to this harness and not marked ignorable; refusing to interpret the log — it was likely written by a newer harness',
            )

    def _unsupported(self, meta: dict, reason: str) -> SessionFormatUnsupportedError:
        """构造指向原始工件(后端有的话)的格式拒绝。"""
        locate = getattr(self.backend, "locate", None)
        location = locate(meta) if locate is not None else None
        if location is None:
            return SessionFormatUnsupportedError(reason)
        return SessionFormatUnsupportedError(f"{reason} (raw log: {location.path})", location)

    @staticmethod
    def _assertStoredId(id: str, meta: dict) -> None:
        """拒绝不绑定到请求会话 id 的后端元数据。"""
        if meta.get("id") != id:
            raise RuntimeError(
                f'stored session identity mismatch: requested "{id}", header contains "{meta.get("id")}"'
            )

    # --- 写路径(session/event → flush 排干)---

    def installWritePath(self) -> None:
        ctx = self.ctx

        # 先在监听者之前注册 disposer:Cordis 逆序拆解 effect,事件
        # 准入先关闭,再让这最后一段排干到静默并关闭后端。注意 execute
        # 必须**返回** async 函数本体:可等待的返回值会被 cordis-py
        # 当场 await 并收集其结果,而不是注册成 disposer。
        def _disposer():
            async def _dispose():
                disposeError = None
                try:
                    errors = await _settled_errors(
                        [self._flush(session) for session in list(self.live.keys())]
                    )
                    while len(self.chains) > 0:
                        await asyncio.gather(*list(self.chains.values()), return_exceptions=True)
                    if len(errors) > 0:
                        raise AggregateError(errors, f"{self.backend.name} dispose failed")
                except BaseException as error:
                    disposeError = error
                    raise
                finally:
                    close = getattr(self.backend, "close", None)
                    if close is not None:
                        try:
                            result = close()
                            if inspect.isawaitable(result):
                                await result
                        except BaseException as closeError:
                            # 关闭失败只能补充拆解上下文;保留已捕获的
                            # 排干 AggregateError 作主失败。只在排干
                            # 成功时才单独浮出关闭错误。
                            if disposeError is None:
                                raise closeError

            return _dispose

        ctx.fiber.effect(_disposer, f"{self.backend.name} write path")

        # 创建时捕获 header,分叉种子只持久化一次。
        ctx.events.on("session/created", lambda session: self._initFor(session))

        # 为每个冻结事件保留持久化自有副本并启动其有界窗口。
        def _on_event(session, event):
            live = self._initFor(session)
            live.writes.enqueue(event)

        ctx.events.on("session/event", _on_event)

        # 调用方用 flush 作为缓冲写的即时耐久屏障。
        ctx.events.on("session/flush", lambda session: self._flush(session))

        # 会话拆解只观察:退休自带失败处理。
        ctx.events.on("session/disposed", lambda session: self.retire(session))

        # HMR:热重载不重放 session/created,种子化既有活会话
        # (与 dsh-invariants 对齐)。
        for session in ctx.sessions.list():
            self._initFor(session)

    def retire(self, session) -> None:
        """启动并观察一个已拆解会话的最后排干。"""
        if session not in self.live:
            return
        try:
            retirement = asyncio.ensure_future(self._retireCore(session))
        except RuntimeError:
            # 同步拆解路径(无运行中的事件循环):状态与缓冲保留在
            # live 里,由下一次循环内 flush/拆解兜底 —— 不丢失数据,
            # 只是延迟排干。协调器已约束构造在循环内,这里仅兜底
            # 边界调用。
            self.ctx.logger.warn(
                f'{self.backend.name}: session "{session.id}" retired outside an event loop; deferred to the next async flush'
            )
            return
        self.retirements[session.id] = retirement

        def _forget():
            if self.retirements.get(session.id) is retirement:
                del self.retirements[session.id]

        def _warn(_retirement):
            if _retirement.done() and not _retirement.cancelled():
                error = _retirement.exception()
                if error is not None:
                    self.ctx.logger.warn(
                        f'{self.backend.name}: session "{session.id}" retirement failed: {error}'
                    )

        retirement.add_done_callback(lambda t: _forget())
        retirement.add_done_callback(_warn)

    async def _retireCore(self, session) -> None:
        """排干并释放一个精确已拆解 Session 生命周期持有的状态。"""
        await self._flush(session)
        id = session.header["id"]
        await self._serialize(id, lambda: self._releaseRetired(session, id))

    def _releaseRetired(self, session, id: str) -> None:
        if session in self.live:
            del self.live[session]
        state = self.states.get(id)
        if state is not None and state.owner is session:
            del self.states[id]

    def _initFor(self, session) -> LiveSessionState:
        """返回一个活会话的唯一生命周期控制器,需要时创建。"""
        existing = self.live.get(session)
        if existing is not None:
            return existing
        reservation = self.preparations.reservationFor(session)
        if reservation is not None:
            restored = self._attachPrepared(session, reservation)
            self.live[session] = restored
            return restored
        # 会话拥有这个稳定深冻结快照;后端只序列化它。
        seed = session.events
        live = LiveSessionState(_resolved_future(), None)
        live.writes = self._createWriteBehind(session, lambda: live.init)
        self.live[session] = live
        live.init = self._serialize(session.header["id"], lambda: self._onCreated(session, seed))
        _observe_rejection(live.init)
        return live

    def _attachPrepared(self, session, reservation) -> LiveSessionState:
        """绑定一个精确的已准备 Session,只持久化其未发布后缀。"""
        source = reservation.source
        state = reservation.state
        if (
            source.session is not session
            or state.owner is not None
            or state.cursor != len(source.inspection.events)
            or session.first_live_seq != state.cursor
        ):
            raise RuntimeError(
                f'session "{session.id}" preparation no longer matches its persistence state'
            )
        suffix = [dict(event) for event in session.events[state.cursor:]]
        self.preparations.attach(reservation)
        state.owner = session
        live = LiveSessionState(_resolved_future(), None)
        live.writes = self._createWriteBehind(session, lambda: live.init)
        if len(suffix) > 0:
            live.init = self._serialize(session.id, lambda: self._appendCore(session.id, suffix))
            _observe_rejection(live.init)
        return live

    async def _seedMatchesPersisted(self, id: str, seed: list, cursor: int) -> bool:
        """活会话的 seed 是否重现前 cursor 个持久化事件。cursor 为 0
        (尚未持久化任何东西)平凡匹配。活会话认领 load()/create()
        留下的无属主状态时使用。"""
        if cursor == 0:
            return True
        stored = await self.backend.loadStored(id)
        # cursor > 0 意味着会话已物化,所以它存在。
        if stored is None:
            return False
        self._assertStoredId(id, stored.meta)
        return _seed_covers_prefix(seed, _snapshot_stored_events(stored.events, id)[:cursor])

    async def _onCreated(self, session, seed: list) -> None:
        """session/created 时把后端的内存状态同步到活会话。

        四种情形,按后端是否跟踪该 id 与工件是否存在:
        1. 已跟踪 → 无操作(或 seed 匹配时认领无属主状态,或回收
           真正被遗弃的 id,否则作为冲突拒绝)。
        2. 未跟踪、同 cwd 存在工件且是活事件的 seq 对齐 PREFIX →
           采纳它,持久化任何活后缀。
        3. 未跟踪、另一 cwd 存在工件或不是前缀 → 拒绝(冲突)。
        4. 未跟踪且无工件 → 真正的新会话:注册 meta(惰性)并持久化
           其 seed 一次。
        """
        id = session.header["id"]
        tracked = self.states.get(id)
        if tracked is not None:
            # 情形 1:已跟踪。
            if tracked.owner is session:
                return
            if tracked.owner is None:
                # 公共 create()/load() API 留下的无属主状态。第一个
                # 活会话认领 —— 但只在 cwd scope 与 seed 都匹配时。
                # 同 id 无属主工件在别的 cwd 是冲突而非认领:认领会
                # 让这个活会话的事件穿过存储 header 的 cwd 追加。
                # seed 守卫保证活事件重现持久化前缀;否则复用该 id
                # 的新会话可能把首段事件当作已写而滤掉。
                if tracked.meta.get("cwd") != session.header.get("cwd"):
                    raise RuntimeError(
                        f'session "{id}" is already persisted at a different cwd (persisted: {tracked.meta.get("cwd")}, live: {session.header.get("cwd")}) (id collision)'
                    )
                if not await self._seedMatchesPersisted(id, seed, tracked.cursor):
                    raise RuntimeError(
                        f'session "{id}" is already persisted with {tracked.cursor} event(s) that do not match this live session (id collision)'
                    )
                tracked.owner = session
                # 持久化超出持久化前缀的 seed 后缀。构造器种子事件
                # 从不发 session/event,缓冲永远看不到它们。
                suffix = seed[tracked.cursor:]
                if len(suffix) > 0:
                    await self._appendCore(id, suffix)
                return
            owner = self.live.get(tracked.owner)
            if not tracked.materialized and (owner is None or not owner.writes.has_work):
                del self.states[id]
            else:
                raise RuntimeError(
                    f'session "{id}" is already bound to a different live session in this backend (id collision)'
                )

        # 情形 2/3:跨存储解析一次该 id,再让采纳在修复或状态发布
        # 前拒绝 cwd 失配。
        live = await self.backend.loadStored(id)
        if live is not None:
            # 不走冷准备:那会把打开回合崩溃修复成 interrupted ——
            # 对 HMR 是错的,活 Session 仍是权威,可能稍后追加真实
            # 的 step/turn 结尾。
            await self._adoptLivePrefix(session, seed, live)
            return

        # 情形 4:真正的新会话。注册其 meta(惰性),再持久化其 seed
        # (创建时已出现的事件)一次。
        meta = {**session.header}
        await self._createCore(meta)
        # 把这个状态绑到活会话,让稍后复用该 id 的**另一个**会话
        # 被识别为冲突(情形 1)而不是静默无操作。
        created = self.states.get(id)
        if created is not None:
            created.owner = session
        if len(seed) > 0:
            await self._appendCore(id, seed)

    async def _adoptLivePrefix(self, session, seed: list, stored: StoredPrefix) -> None:
        """把存储前缀采纳为活会话的历史(HMR/重载):验证 seed 覆盖
        存储前缀,截断任何撕裂尾(不是打开回合 —— 活 Session 仍是
        权威),绑定所有权,并持久化领先于存储前缀的活后缀。"""
        meta = stored.meta
        events = stored.events
        self._assertStoredId(session.header["id"], meta)
        if meta.get("cwd") != session.header.get("cwd"):
            raise RuntimeError(
                f'session "{session.header["id"]}" is already persisted at a different cwd (persisted: {meta.get("cwd")}, live: {session.header.get("cwd")}) (id collision)'
            )
        self._assertVersion(meta)
        storedEvents = _snapshot_stored_events(events, session.header["id"])
        self._assertEventsSupported(meta, storedEvents)
        if not _seed_covers_prefix(seed, storedEvents):
            raise RuntimeError(
                f'session "{session.header["id"]}" already has a persisted log on disk that does not match this live session (id collision)'
            )
        # 只截断修复(无关闭器):这里不关闭打开回合。
        if stored.tornMarker is not None:
            await self.backend.commitRepair(meta, stored.tornMarker, [])
        self.states[session.header["id"]] = SessionState(
            {**meta}, len(storedEvents), True, owner=session
        )
        suffix = seed[len(storedEvents):]
        if len(suffix) > 0:
            await self._appendCore(session.header["id"], suffix)

    async def _flush(self, session) -> None:
        """把缓冲排干到静默点:等待初始化,再耐久排干全部挂起事件。"""
        live = self._initFor(session)
        live.writes.cancel_automatic_wait()
        try:
            await live.init
        except BaseException:
            # 退休/拆解期间准入已关,但普通 flush 可能撞上初始化
            # 挂起时的最后一次 enqueue。
            live.writes.cancel_automatic_wait()
            raise
        await live.writes.flush()

    def _createWriteBehind(self, session, ready: Callable[[], Any]) -> SessionWriteBehind:
        """构建一个围绕初始化与 id 串行化的包内私有写控制器。"""
        return SessionWriteBehind(
            {
                "max_delay_ms": self.writeBatchMaxDelayMs,
                "write": lambda batch: self._writeBatch(session, ready, batch),
                "report_background_failure": lambda error: self.ctx.logger.warn(
                    f'{self.backend.name}: background write for session "{session.id}" failed (buffered events retained): {error}'
                ),
            }
        )

    async def _writeBatch(self, session, ready, batch) -> None:
        await ready()
        await self._serialize(session.header["id"], lambda: self._appendLiveBatch(session.header["id"], batch))

    async def _appendLiveBatch(self, id: str, batch: list) -> None:
        """滤掉初始化已存储的事件后,追加一个控制器自有前缀。"""
        state = self.states.get(id)
        # state 总由被等待的初始化设置。
        cursor = state.cursor if state is not None else 0
        fresh = [event for event in batch if event["seq"] >= cursor]
        await self._appendCore(id, fresh)
