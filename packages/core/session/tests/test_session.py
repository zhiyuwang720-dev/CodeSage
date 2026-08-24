"""会话内核主测试:Session/SessionStore/install 与校验族。

照 DSH session.spec.ts 的核心断言面,按 Python 移植的契约等价覆盖:
创建与仓库生命周期、append 的提交原子性(先快照后入日志再通知)、
事件冻结、派生消息与折叠缓存、种子/恢复边界、分叉五错误码、校验族。

注意与 invariant 的分工:invariant(回合/步骤嵌套、调用配对)由
持久化层在落盘边界消费,Session.append 只做表面校验 —— 这里对
活 append 只断言表面错误(缺标记/带错标记/非 JSON),不断言嵌套错误。
"""

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]  # 仓库根
_PACKAGES = Path(__file__).resolve().parents[3]  # packages/
sys.path.insert(0, str(_PACKAGES))
sys.path.insert(0, str(_ROOT / "cordis-py"))

from cordis import Context  # noqa: E402

from core.session import (  # noqa: E402
    SESSION_FORMAT_VERSION,
    Session,
    SessionForkError,
    SessionStore,
    adopt_session_event,
    assert_adapter_defaults,
    assert_message_event_shape,
    assert_session_event_envelope,
    assert_supported_request_header,
    install,
    snapshot_session_event,
    snapshot_session_header,
    validate_session_header,
)


# ---- 仪式辅助:完整回合流程(与 test_surface.py 同款风格) ----


def _turn_start(s, turn=1):
    return s.append("turn/start", {"turn": turn})


def _step_start(s, step=1):
    return s.append("step/start", {"turn": 1, "step": step})


def _user(s, text="hello", id_="u1"):
    return s.append(
        "user/message",
        {"role": "user", "id": id_, "source": {"kind": "human"},
         "content": [{"type": "text", "text": text}]},
        surface_op="append",
    )


def _assistant(s, text="hi there", id_="a1"):
    return s.append(
        "assistant/message",
        {"turn": 1, "step": 1,
         "message": {"role": "assistant", "id": id_,
                     "source": {"kind": "model", "provider": "p", "model": "m"},
                     "content": [{"type": "text", "text": text}]}},
        surface_op="append",
    )


def _tool_call(s, cid="c1"):
    return s.append(
        "tool/call", {"turn": 1, "step": 1, "callId": cid, "name": "read", "arguments": "{}"}
    )


def _tool_result(s, cid="c1"):
    return s.append(
        "tool/result",
        {"turn": 1, "step": 1,
         "message": {"role": "user", "id": f"t{cid}", "source": {"kind": "tool", "callId": cid},
                     "content": [{"type": "tool-result", "toolCallId": cid, "content": []}]}},
        surface_op="append",
    )


def _step_end(s):
    return s.append("step/end", {"turn": 1, "step": 1})


def _turn_end(s):
    return s.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})


def _closed_turn(s, turn=1):
    """一段完整、invariant 平衡的回合仪式。"""
    _turn_start(s, turn)
    _step_start(s)
    _user(s, "hello", f"u{turn}")
    _assistant(s, "hi", f"a{turn}")
    cid = f"c{turn}"
    _tool_call(s, cid)
    _tool_result(s, cid)
    _step_end(s)
    _turn_end(s)


# ---- 种子事件构造(纯字典,store.create 的 seed 参数用) ----


def _turn_start_ev(seq):
    return {"type": "turn/start", "seq": seq, "time": 1, "data": {"turn": 1}}


def _user_ev(seq):
    return {"type": "user/message", "seq": seq, "time": 1, "data": {"role": "user", "id": f"u{seq}",
            "source": {"kind": "human"}, "content": []}, "surfaceOp": "append"}


def _end_seed_ev(seq):
    return {"type": "session/end-seed", "seq": seq, "time": 1, "data": {}}


# ---- fixture ----


@pytest.fixture
def ctx():
    return Context()


@pytest.fixture
def store(ctx):
    return SessionStore(ctx)


# ---- 创建 ----


def test_create_mints_ids(store):
    a = store.create()
    b = store.create()
    assert a.id == "session-1"
    assert b.id == "session-2"
    assert a.id != b.id
    # 显式 id 占位后,mint 跳过冲突继续递增
    store.create("session-3")
    c = store.create()
    assert c.id == "session-4"
    assert store.get("session-3") is not None


def test_create_duplicate_rejected(store):
    store.create("s-dup")
    with pytest.raises(ValueError, match="already exists"):
        store.create("s-dup")
    with pytest.raises(ValueError, match="already exists"):
        store.prepare("s-dup")
    # 同 id 的另一实例也拒绝入仓库
    other = Session.create("s-dup")
    with pytest.raises(ValueError, match="already exists"):
        store.enter(other)


def test_create_meta_becomes_header(store):
    s = store.create(
        "s-meta",
        {"meta": {"cwd": "C:/work", "origin": "subagent", "delegationDepth": 2, "seedLength": 0}},
    )
    assert s.header["id"] == "s-meta"
    assert s.header["version"] == SESSION_FORMAT_VERSION
    assert s.header["cwd"] == "C:/work"
    assert s.header["origin"] == "subagent"
    assert s.header["delegationDepth"] == 2
    assert isinstance(s.header["createdAt"], int) and s.header["createdAt"] >= 0
    with pytest.raises(TypeError):
        s.header["cwd"] = "/elsewhere"
    # 相对 cwd 拒绝
    with pytest.raises(ValueError):
        store.create("s-rel", {"meta": {"cwd": "relative/path"}})
    # 显式 createdAt 保留
    s2 = store.create("s-ts", {"meta": {"createdAt": 42}})
    assert s2.header["createdAt"] == 42


# ---- append 与提交原子性 ----


def test_append_assigns_seq_and_snapshots(store):
    s = store.create("s-ap")
    ev = s.append("turn/start", {"turn": 1})
    assert ev["type"] == "turn/start"
    assert ev["seq"] == 0
    assert s.seq == 1
    assert s.events[0] is ev  # 返回与日志同一事件
    # data 快照分离:入日志后改输入不影响日志
    data = {"turn": 1, "nested": {"x": [1, 2]}}
    ev2 = s.append("request/context", data)
    data["turn"] = 99
    data["nested"]["x"].append(3)
    assert ev2["data"]["turn"] == 1
    assert ev2["data"]["nested"]["x"] == [1, 2]
    # 深度冻结:任何改写都抛 TypeError
    with pytest.raises(TypeError):
        ev2["data"]["nested"]["x"].append(4)
    with pytest.raises(TypeError):
        ev2["data"] = {}
    with pytest.raises(TypeError):
        s.events[0]["seq"] = 1


def test_append_rejects_bad_input(store):
    s = store.create("s-bad")
    msg = {"role": "user", "id": "u", "source": {"kind": "human"}, "content": []}
    # 表面类型缺标记
    with pytest.raises(ValueError):
        s.append("user/message", msg)
    # 非表面类型带标记
    with pytest.raises(ValueError):
        s.append("turn/start", {"turn": 1}, surface_op="append")
    # 非 JSON data
    with pytest.raises(ValueError, match="non-JSON"):
        s.append("turn/start", {"turn": 1j})
    # 非 JSON surface 元数据
    with pytest.raises(ValueError, match="non-JSON"):
        s.append("user/message", msg, surface_op={"op": "replace", "start": 1j, "end": 2})
    # legacy 词汇:delta 类型与 fallback 原因
    with pytest.raises(ValueError, match="legacy"):
        s.append("request/header-delta", {})
    with pytest.raises(ValueError, match="fallback"):
        s.append("request/header", {"reason": "fallback", "header": {}})
    assert s.seq == 0  # 全部拒绝,日志未被污染


def test_append_publishes_and_contains_observer_errors(store):
    ctx = store.ctx
    seen = []
    ctx.events.on("session/event", lambda s, e: seen.append((s.id, e["seq"], e["type"])))
    # 第二个监听者抛错:包含化,不影响第一个监听者与已提交事件
    def throwing(s, e):
        raise RuntimeError("boom")
    ctx.events.on("session/event", throwing)
    s = store.create("s-pub")
    _turn_start(s)
    assert seen == [("s-pub", 0, "turn/start")]
    assert s.seq == 1


def test_append_reentrancy_contained(store):
    """观察者里嵌套 append 被拒且包含化:已提交事实不因通知而变。"""
    ctx = store.ctx
    s = store.create("s-reent")
    attempts = []

    def nested(session, event):
        try:
            s.append("turn/start", {"turn": 99})
        except ValueError as e:
            attempts.append(str(e))

    ctx.events.on("session/event", nested)
    ev = s.append("turn/start", {"turn": 1})
    assert len(attempts) == 1
    assert "reenter" in attempts[0]
    assert s.seq == 1
    assert s.events[0] is ev


def test_events_snapshot_stable_and_frozen(store):
    s = store.create("s-snap")
    snap = s.events
    assert snap == ()
    _turn_start(s)
    assert len(snap) == 0  # 先前返回的数组不随之增长
    assert len(s.events) == 1
    with pytest.raises(TypeError):
        s.events[0]["seq"] = 9


# ---- 派生消息 ----


def test_derive_messages_full_flow(store):
    s = store.create("s-derive")
    _closed_turn(s)
    msgs = s.derive_messages()
    assert len(msgs) == 3
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"][0]["text"] == "hello"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"][0]["text"] == "hi"
    assert msgs[2]["content"][0]["type"] == "tool-result"
    assert msgs[2]["content"][0]["toolCallId"] == "c1"
    with pytest.raises(TypeError):
        msgs[0]["content"].append({"type": "text", "text": "x"})
    # 快照稳定性:后续 append 不增长已持有的数组
    snapshot = s.derive_messages()
    _closed_turn(s, turn=2)
    assert len(snapshot) == 3
    assert len(s.derive_messages()) == 6
    # 空内容 assistant(只承载 usage 的 max-tokens 步)不进入派生
    s.append("assistant/message", {"turn": 3, "step": 1,
             "message": {"role": "assistant", "id": "a-empty",
                         "source": {"kind": "model", "provider": "p", "model": "m"}, "content": []}},
             surface_op="append")
    assert len(s.derive_messages()) == 6


def test_derive_cache_identity(store):
    """未触发重写的 append 不重建消息对象;每次调用返回新鲜数组。"""
    s = store.create("s-cache")
    _turn_start(s)
    _step_start(s)
    _user(s, "hello")
    msgs1 = s.derive_messages()
    s.append("assistant/chunk", {"turn": 1, "step": 1, "chunk": {"type": "text-delta", "index": 0, "text": "x"}})
    msgs2 = s.derive_messages()
    assert msgs2 is not msgs1
    assert msgs2 == msgs1
    assert msgs2[0] is msgs1[0]


def test_derive_rebuilds_on_replace(store):
    s = store.create("s-replace")
    _turn_start(s)
    _step_start(s)
    u = _user(s, "hello")
    _assistant(s, "hi")
    _step_end(s)
    _turn_end(s)
    assert len(s.derive_messages()) == 2
    # 表面替换:遮蔽原 user/message,派生随 generation 重建
    s.append("user/message", {"role": "user", "id": "u-new", "source": {"kind": "human"},
                              "content": [{"type": "text", "text": "replaced"}]},
             surface_op={"op": "replace", "start": u["seq"], "end": u["seq"]},
             source_event_seqs=[u["seq"]])
    msgs = s.derive_messages()
    assert len(msgs) == 2
    assert msgs[0]["id"] == "u-new"
    assert msgs[0]["content"][0]["text"] == "replaced"


# ---- 请求头/上下文折叠 ----


def test_request_header_incremental_fold(store):
    s = store.create("s-hdr")
    assert s.request_header() is None
    _turn_start(s)
    s.append("request/header", {"header": {"config": {"provider": "deepseek", "model": "m"}, "system": "v1"}})
    h = s.request_header()
    assert h["config"]["provider"] == "deepseek"
    assert h["system"] == "v1"
    s.append("request/header", {"header": {"config": {"provider": "deepseek", "model": "m"}, "system": "v2"}})
    assert s.request_header()["system"] == "v2"
    with pytest.raises(TypeError):
        s.request_header()["system"] = "v3"


def test_request_context_fold(store):
    s = store.create("s-ctx")
    assert s.request_context() is None
    s.append("request/context", {"route": "main", "input": {"mood": "happy"}})
    c = s.request_context()
    assert c["route"] == "main"
    with pytest.raises(TypeError):
        c["route"] = "other"
    # 后续事件覆盖
    s.append("request/context", {"route": "second"})
    assert s.request_context()["route"] == "second"


# ---- end-seed 标记 ----


def test_end_seed_marker(store):
    s = store.create()
    assert s.events == ()  # 无种子 → 无标记
    assert s.first_live_seq == 0
    s2 = store.create("s-seed", {"seed": [_turn_start_ev(0)]})
    assert s2.seq == 2
    assert s2.events[-1]["type"] == "session/end-seed"
    assert s2.first_live_seq == 1
    # 已以标记结尾的种子不重复标记
    s3 = store.create("s-seed3", {"seed": [_turn_start_ev(0), _user_ev(1), _end_seed_ev(2)]})
    assert s3.seq == 3
    assert [e["type"] for e in s3.events].count("session/end-seed") == 1
    assert s3.first_live_seq == 3


# ---- 种子校验与恢复 ----


def test_seed_validation(store):
    # seq 不连续
    with pytest.raises(ValueError, match="contiguous"):
        store.create("s-gap", {"seed": [_turn_start_ev(0), _user_ev(5)]})
    # 坏信封(多余键)
    with pytest.raises(ValueError, match="envelope"):
        store.create("s-env", {"seed": [{"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}, "evil": 1}]})
    # 非 JSON 数据
    with pytest.raises(ValueError, match="losslessly"):
        store.create("s-json", {"seed": [{"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1j}}]})
    # 表面事件形状错误
    with pytest.raises(ValueError, match="identified message"):
        store.create("s-shape", {"seed": [{"type": "user/message", "seq": 0, "time": 1, "data": {"x": 1}, "surfaceOp": "append"}]})


def test_seed_snapshot_separation(store):
    seed_ev = _turn_start_ev(0)
    s = store.create("s-sep", {"seed": [seed_ev, _user_ev(1)]})
    seed_ev["data"]["turn"] = 99
    seed_ev["data"] = {"turn": 100}
    assert s.events[0]["data"]["turn"] == 1


def test_from_restore(store):
    seed = [_turn_start_ev(0)]
    meta = {"version": SESSION_FORMAT_VERSION, "id": "s-r", "createdAt": 1, "cwd": str(Path.cwd())}
    s = store.prepare("s-r", {"seed": seed, "meta": meta, "seed_source": "persistence"})
    assert s.header["cwd"] == str(Path.cwd())
    assert s.header["createdAt"] == 1
    with pytest.raises(TypeError):
        s.header["cwd"] = "/elsewhere"
    # 恢复模式所有权转让:改输入不再影响日志(已冻结)
    seed[0]["data"]["turn"] = 9
    assert s.events[0]["data"]["turn"] == 1
    # 坏 header 拒绝
    with pytest.raises(ValueError):
        store.prepare("s-r2", {"seed": seed, "meta": {**meta, "version": 1}, "seed_source": "persistence"})
    # 非普通记录(类实例)拒绝
    class NotAPlainRecord(dict):
        pass
    with pytest.raises(ValueError):
        store.prepare("s-r3", {"seed": seed, "meta": NotAPlainRecord(**meta), "seed_source": "persistence"})


# ---- 分叉 ----


def test_fork_creates_live_child(store):
    s = store.create("s-fork")
    _closed_turn(s)
    child = store.fork(s, child_session_id="s-child")
    assert child.id == "s-child"
    assert store.get("s-child") is child
    # 子会话日志 = 源前缀 + end-seed 标记
    assert [e["type"] for e in child.events][:-1] == [e["type"] for e in s.events]
    assert child.events[-1]["type"] == "session/end-seed"
    # header 血缘
    assert child.header["parentSession"] == "s-fork"
    assert child.header["seedLength"] == s.seq
    assert "cwd" not in child.header  # 源无 cwd 时不带
    # 子会话可继续 append,源不受影响
    _turn_start(child, turn=2)
    assert child.seq == s.seq + 2
    assert s.seq == 8


def test_fork_boundary(store):
    s = store.create("s-fork2")
    _turn_start(s)  # 0
    _step_start(s)  # 1
    _user(s, "hello")  # 2
    _step_end(s)  # 3
    _turn_end(s)  # 4
    child = store.fork(s, boundary=4, child_session_id="s-b")
    assert child.header["seedLength"] == 5  # 含端点,events[:5]
    assert child.seq == 6  # 5 个种子 + end-seed
    assert child.events[0]["seq"] == 0
    assert child.events[4]["type"] == "turn/end"
    # 按 id 分叉
    child2 = store.fork("s-fork2", child_session_id="s-b2")
    assert child2.header["seedLength"] == 5
    # 缺省 id:自动 mint
    child3 = store.fork(s)
    assert child3.id.startswith("session-")


def test_fork_error_codes(store):
    s = store.create("s-err")
    _closed_turn(s)
    store.create("s-exists")

    def expect(code, fn):
        with pytest.raises(SessionForkError) as e:
            fn()
        assert e.value.code == code

    expect("SESSION_ALREADY_EXISTS", lambda: store.fork(s, child_session_id="s-exists"))
    expect("SESSION_NOT_FOUND", lambda: store.fork("ghost"))
    # 未入仓库的独立实例
    detached = Session.create("s-lonely")
    expect("SESSION_NOT_FOUND", lambda: store.fork(detached))
    # 同 id 的另一实例(仓库里的是 s)
    other = Session.create("s-err")
    expect("SESSION_NOT_LIVE", lambda: store.fork(other))
    # 边界
    expect("INVALID_BOUNDARY", lambda: store.fork(s, boundary=999))
    expect("INVALID_BOUNDARY", lambda: store.fork(s, boundary=-1))
    expect("INVALID_BOUNDARY", lambda: store.fork(s, boundary="3"))


def test_fork_rejects_open_turn(store):
    s = store.create("s-open")
    _turn_start(s)
    _step_start(s)
    _user(s, "hello")
    # 缺省边界 = 最后事件,位于打开的回合内
    with pytest.raises(SessionForkError) as e:
        store.fork(s)
    assert e.value.code == "OPEN_TURN"
    # 显式边界停在 turn/start 上也拒绝
    with pytest.raises(SessionForkError) as e:
        store.fork(s, boundary=0)
    assert e.value.code == "OPEN_TURN"
    # 边界停在 turn/end 上合法(含端点)
    _step_end(s)
    _turn_end(s)
    child = store.fork(s, boundary=4)
    assert child.header["seedLength"] == 5
    assert child.events[-2]["type"] == "turn/end"


def test_fork_empty_source(store):
    s = store.create("s-empty")
    child = store.fork(s, child_session_id="s-empty-child")
    assert child.seq == 1  # 仅 end-seed 标记
    assert child.header["seedLength"] == 0
    assert child.header["parentSession"] == "s-empty"


# ---- 生命周期:prepare → enter → announce → detach ----


def test_lifecycle_ordering(store):
    ctx = store.ctx
    order = []
    ctx.events.on("session/created", lambda s: order.append(("created", s.id)))
    ctx.events.on("session/disposed", lambda s: order.append(("disposed", s.id)))
    s = store.prepare("s-life")
    assert store.get("s-life") is None
    assert order == []
    detach = store.enter(s)
    assert store.get("s-life") is s
    assert store.list() == [s]
    assert order == []
    store.announce(s)
    assert order == [("created", "s-life")]
    detach()
    assert store.get("s-life") is None
    assert store.list() == []
    assert order == [("created", "s-life"), ("disposed", "s-life")]
    # detach 一次性:再调无副作用
    detach()
    assert order == [("created", "s-life"), ("disposed", "s-life")]


def test_announce_twice_rejected(store):
    s = store.prepare("s-twice")
    detach = store.enter(s)
    store.announce(s)
    with pytest.raises(ValueError, match="already announced"):
        store.announce(s)
    detach()


def test_announce_unattached_rejected(store):
    s = store.prepare("s-orphan")
    with pytest.raises(ValueError, match="not live"):
        store.announce(s)
    # 分离后的对象也拒绝
    s2 = store.prepare("s-gone")
    detach = store.enter(s2)
    detach()
    with pytest.raises(ValueError, match="not live"):
        store.announce(s2)


def test_create_veto_rolls_back(store):
    """同步抛错的 session/created 监听者否决创建,并配对销毁回滚。"""
    ctx = store.ctx
    disposed = []
    ctx.events.on("session/disposed", lambda s: disposed.append(s.id))

    def veto(session):
        raise RuntimeError("veto")

    ctx.events.on("session/created", veto)
    with pytest.raises(RuntimeError, match="veto"):
        store.create("s-veto")
    assert store.get("s-veto") is None
    assert store.list() == []
    assert disposed == ["s-veto"]  # 回滚触发配对销毁边


def test_flush(store):
    s = store.create("s-flush")
    # 无监听者参与:返回 False
    assert asyncio.run(store.flush(s)) is False
    ctx = store.ctx
    seen = []
    ctx.events.on("session/flush", lambda session: seen.append(session.id))
    assert asyncio.run(store.flush(s)) is True
    assert seen == ["s-flush"]

    # 异步监听者也落定
    async def async_listener(session):
        seen.append(f"{session.id}-async")

    ctx.events.on("session/flush", async_listener)
    assert asyncio.run(store.flush(s)) is True
    assert seen[-1] == "s-flush-async"


def test_flush_listener_failure_propagates(store):
    s = store.create("s-flush2")
    ctx = store.ctx

    def fail(session):
        raise RuntimeError("disk full")

    ctx.events.on("session/flush", fail)
    with pytest.raises(RuntimeError, match="disk full"):
        asyncio.run(store.flush(s))


# ---- 校验族 ----


def test_validate_session_header():
    good = {"version": SESSION_FORMAT_VERSION, "id": "s-h", "createdAt": 100, "cwd": str(Path.cwd()),
            "parentSession": "p", "seedLength": 0, "origin": "subagent",
            "delegationDepth": 1, "agentPreset": "default"}
    frozen = validate_session_header("s-h", good)
    assert frozen is not good
    with pytest.raises(TypeError):
        frozen["cwd"] = "/x"
    with pytest.raises(ValueError):
        validate_session_header("s-h", None)
    with pytest.raises(ValueError):
        validate_session_header("s-h", [1])
    with pytest.raises(ValueError):
        validate_session_header("s-h", {**good, "version": 1})
    with pytest.raises(ValueError):
        validate_session_header("s-other", good)
    with pytest.raises(ValueError):
        validate_session_header("s-h", {**good, "createdAt": -1})
    with pytest.raises(ValueError):
        validate_session_header("s-h", {**good, "createdAt": 1.5})
    with pytest.raises(ValueError):
        validate_session_header("s-h", {**good, "cwd": "relative"})
    with pytest.raises(ValueError):
        validate_session_header("s-h", {**good, "origin": "primary"})
    with pytest.raises(ValueError):
        validate_session_header("s-h", {**good, "seedLength": "many"})
    with pytest.raises(ValueError):
        validate_session_header("s-h", {**good, "delegationDepth": -1})
    # 多余字段不拒绝(插件字段)
    validate_session_header("s-h", {**good, "custom": {"x": 1}})


def test_snapshot_session_header():
    h = snapshot_session_header("s-x")
    assert h["id"] == "s-x"
    assert h["version"] == SESSION_FORMAT_VERSION
    assert h["createdAt"] >= 0
    with pytest.raises(TypeError):
        h["id"] = "other"
    with pytest.raises(ValueError):
        snapshot_session_header("s-x", {"version": SESSION_FORMAT_VERSION, "id": "s-y", "createdAt": 1})


def test_assert_session_event_envelope():
    base = {"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}}
    assert_session_event_envelope(base, 0)  # 不抛
    with pytest.raises(ValueError, match="envelope"):
        assert_session_event_envelope({**base, "extra": 1}, 0)
    # seq 连续性不在此边界检查(种子循环/append 各自检查,与 DSH 一致)
    assert_session_event_envelope({**base, "seq": 1}, 0)
    with pytest.raises(ValueError, match="envelope"):
        assert_session_event_envelope({**base, "seq": 1.5}, 0)
    with pytest.raises(ValueError, match="envelope"):
        assert_session_event_envelope({**base, "time": 1.5}, 0)
    with pytest.raises(ValueError, match="envelope"):
        assert_session_event_envelope({**base, "ignorable": False}, 0)
    with pytest.raises(ValueError, match="legacy"):
        assert_session_event_envelope({"type": "request/header-delta", "seq": 0, "time": 1, "data": {}}, 0)
    # 表面事件需要完整消息形状
    with pytest.raises(ValueError, match="identified message"):
        assert_session_event_envelope({"type": "user/message", "seq": 0, "time": 1, "data": {"x": 1}}, 0)
    # request/header 需要 provider/model
    with pytest.raises(ValueError, match="provider/model"):
        assert_session_event_envelope(
            {"type": "request/header", "seq": 0, "time": 1,
             "data": {"header": {"config": {}, "system": ""}}}, 0)


def test_assert_adapter_defaults():
    assert_adapter_defaults(None, {"provider": "p"}, 0)  # 不抛
    assert_adapter_defaults({"reasoningEffort": True}, {"reasoningEffort": "high"}, 0)  # 不抛
    with pytest.raises(ValueError):
        assert_adapter_defaults({"reasoningEffort": "high"}, {}, 0)  # 值必须是字面 true
    with pytest.raises(ValueError):
        assert_adapter_defaults({"temperature": True}, {}, 0)  # 未知键
    with pytest.raises(ValueError):
        assert_adapter_defaults({"maxTokens": True}, {}, 0)  # 标记的字段不在 config


def test_assert_message_event_shape():
    ok = {"type": "tool/result", "seq": 0, "time": 1, "data": {
        "turn": 1, "step": 1,
        "message": {"role": "user", "id": "t1", "source": {"kind": "tool", "callId": "c1"},
                    "content": [{"type": "tool-result", "toolCallId": "c1", "content": []}]}}}
    assert_message_event_shape(ok, "x")  # 不抛
    bad_id = {"type": "user/message", "seq": 0, "time": 1, "data": {"role": "user", "id": ""}}
    with pytest.raises(ValueError, match="identified message"):
        assert_message_event_shape(bad_id, "x")
    wrong_role = {"type": "user/message", "seq": 0, "time": 1,
                  "data": {"role": "assistant", "id": "u1", "source": {"kind": "human"}, "content": []}}
    with pytest.raises(ValueError, match="role"):
        assert_message_event_shape(wrong_role, "x")
    # assistant 必须 model 来源
    bad_assistant = {"type": "assistant/message", "seq": 0, "time": 1,
                     "data": {"message": {"role": "assistant", "id": "a1", "source": {"kind": "human"}, "content": []}}}
    with pytest.raises(ValueError, match="model source"):
        assert_message_event_shape(bad_assistant, "x")
    # tool/result:callId 不匹配
    mismatched = {"type": "tool/result", "seq": 0, "time": 1, "data": {
        "message": {"role": "user", "id": "t1", "source": {"kind": "tool", "callId": "c1"},
                    "content": [{"type": "tool-result", "toolCallId": "c-other", "content": []}]}}}
    with pytest.raises(ValueError, match="mismatched"):
        assert_message_event_shape(mismatched, "x")
    # 多块拒绝
    two_blocks = {"type": "tool/result", "seq": 0, "time": 1, "data": {
        "message": {"role": "user", "id": "t1", "source": {"kind": "tool", "callId": "c1"},
                    "content": [{"type": "tool-result", "toolCallId": "c1", "content": []},
                                {"type": "text", "text": "x"}]}}}
    with pytest.raises(ValueError, match="one tool-result"):
        assert_message_event_shape(two_blocks, "x")


def test_assert_supported_request_header():
    assert_supported_request_header("turn/start", {"turn": 1}, "x")  # 不抛
    assert_supported_request_header("request/header", {"header": {}}, "x")  # 不抛
    with pytest.raises(ValueError, match="legacy"):
        assert_supported_request_header("request/header-delta", {}, "x")
    with pytest.raises(ValueError, match="fallback"):
        assert_supported_request_header("request/header", {"reason": "fallback", "header": {}}, "x")


# ---- 事件接纳边界 ----


def test_adopt_and_snapshot_session_event():
    ev = {"type": "assistant/message", "seq": 0, "time": 1,
          "data": {"turn": 1, "step": 1,
                   "message": {"role": "assistant", "id": "a1",
                               "source": {"kind": "model", "provider": "p", "model": "m"},
                               "content": [{"type": "text", "text": "hi"}]}}}
    adopted = adopt_session_event(ev)
    assert adopted is ev  # 所有权转让,不拷贝
    with pytest.raises(TypeError):
        adopted["data"]["message"]["id"] = "a2"
    # 形状错误拒绝
    with pytest.raises(ValueError):
        adopt_session_event({"type": "assistant/message", "seq": 0, "time": 1, "data": {}})
    # 分离版:返回新对象
    snap = snapshot_session_event(ev)
    assert snap is not ev
    assert snap["data"]["message"]["id"] == "a1"
    with pytest.raises(TypeError):
        snap["data"]["message"]["role"] = "user"


# ---- 插件安装面 ----


def test_install(ctx):
    install(ctx)
    assert isinstance(ctx.sessions, SessionStore)
    s = ctx.sessions.create()
    assert s.id == "session-1"
    _turn_start(s)
    assert s.seq == 1
    assert len(ctx.sessions.list()) == 1
