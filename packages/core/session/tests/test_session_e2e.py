"""三包全链路 e2e:core/session + session-persistence + jsonl 后端。

生命周期走真实路径:SessionStore 建会话 → 追加一个完整回合 →
flush 落盘 → 退休 → 全新 ctx/store/后端(同 root)重载 →
事件逐条一致 → resume 续写新回合 → 再次落盘重载验证。
不依赖任何 mock:磁盘字节即真相。
"""

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1]  # core/session 包目录
_ROOT = Path(__file__).resolve().parents[4]  # 仓库根
_PACKAGES = Path(__file__).resolve().parents[3]  # packages/
sys.path.insert(0, str(_CORE))
sys.path.insert(0, str(_PACKAGES))
sys.path.insert(0, str(_ROOT / "cordis-py"))

from cordis import Context  # noqa: E402

from core.session import SessionStore  # noqa: E402
from session.session_persistence_jsonl import JsonlSessionPersistence  # noqa: E402


def _closed_turn(s, turn=1):
    """invariant 平衡的完整回合(与持久化测试同款仪式)。"""
    s.append("turn/start", {"turn": turn})
    s.append("step/start", {"turn": turn, "step": 1})
    s.append(
        "user/message",
        {"role": "user", "id": f"u{turn}", "source": {"kind": "human"},
         "content": [{"type": "text", "text": f"hello {turn}"}]},
        surface_op="append",
    )
    s.append("step/end", {"turn": turn, "step": 1})
    s.append("turn/end", {"turn": turn, "reason": {"kind": "completed"}})


def _new_world(root):
    """全新 ctx + store + JSONL 后端(同一物理 root)。"""
    ctx = Context()
    store = SessionStore(ctx)
    persistence = JsonlSessionPersistence(ctx, {"root": str(root)})
    return ctx, store, persistence


def test_e2e_write_flush_reload_and_resume(tmp_path):
    """建会话 → 追加 → 落盘 → 新世界重载 → resume 续写 → 再落盘。"""
    import asyncio

    root = tmp_path / "root"

    async def scenario():
        # --- 第一个世界:写 ---
        ctx, store, persistence = _new_world(root)
        session = store.prepare(
            "s-e2e",
            {"meta": {"cwd": "C:/work", "createdAt": 1},
             "seed": [], "seed_source": "local"},
        )
        detach = store.enter(session)
        store.announce(session)
        _closed_turn(session, turn=1)
        await store.flush(session)
        detach()
        # 退休是异步排干:等它落定,磁盘写入完成
        retirement = persistence.coordinator.retirements.get("s-e2e")
        if retirement is not None:
            await retirement
        assert (root / "--C-work--").exists()  # 物理物化

        # --- 第二个世界:重载(同 root,新 ctx/store/后端)---
        ctx2, store2, persistence2 = _new_world(root)
        loaded = await persistence2.load("s-e2e")
        assert loaded.meta["id"] == "s-e2e"
        assert loaded.meta["cwd"] == "C:/work"
        # 事件逐条一致:内容、seq、type、时间戳(loaded 为 list,live 为冻结 tuple)
        original = session.events
        assert loaded.events == list(original)

        # --- resume:在持久化会话上续写新回合 ---
        prep = await persistence2.prepare("s-e2e")
        resumed = prep.session
        assert resumed.id == "s-e2e"
        # 恢复的种子含磁盘全部 6 个事件;种子末尾不是 end-seed,
        # 构造器重新标记边界 —— DSH 语义:resume 日志以新 end-seed 结尾
        # (种子末尾是 end-seed 时才会跳过,反复打开不使日志增长)
        assert [e["seq"] for e in resumed.events] == [0, 1, 2, 3, 4, 5, 6]
        assert resumed.events[-1]["type"] == "session/end-seed"
        detach2 = store2.enter(resumed)
        store2.announce(resumed)
        _closed_turn(resumed, turn=2)
        await store2.flush(resumed)
        detach2()
        retirement2 = persistence2.coordinator.retirements.get("s-e2e")
        if retirement2 is not None:
            await retirement2

        # --- 第三个世界:最终验证,事件与内存路径完全一致 ---
        ctx3, _, persistence3 = _new_world(root)
        final = await persistence3.load("s-e2e")
        expected_seq = [e["seq"] for e in original] + [
            e["seq"] for e in resumed.events[len(original):]
        ]
        assert [e["seq"] for e in final.events] == expected_seq
        assert final.events[-1]["type"] == "turn/end"
        # 续写的回合内容完整
        turn2_events = [e for e in final.events if e["data"].get("turn") == 2]
        assert len(turn2_events) == 4  # turn/start + step/start + user/message + turn/end
        # 合成关闭器 vs 真实回合:turn/end 是 completed 而非 interrupted
        assert final.events[-1]["data"]["reason"]["kind"] == "completed"

    asyncio.run(scenario())


def test_e2e_crash_tail_recovery_across_worlds(tmp_path):
    """磁盘撕裂尾:新世界 load 自动修复(截断 + 合成关闭器)。"""
    import asyncio
    import json
    import os

    from session.session_persistence_jsonl.src.format import log_path  # noqa: E402

    root = tmp_path / "root"

    async def scenario():
        # 第一个世界:写一个完整回合 + 撕裂尾字节(模拟崩溃)
        ctx, store, persistence = _new_world(root)
        session = store.prepare(
            "s-crash",
            {"meta": {"cwd": "C:/work", "createdAt": 1},
             "seed": [], "seed_source": "local"},
        )
        detach = store.enter(session)
        store.announce(session)
        _closed_turn(session, turn=1)
        await store.flush(session)
        detach()
        retirement = persistence.coordinator.retirements.get("s-crash")
        if retirement is not None:
            await retirement
        # 物理注入撕裂尾
        path = log_path(str(root), "C:/work", "s-crash", "none")
        with open(path, "ab") as handle:
            handle.write(b'{"type": "turn/start", "seq": 5, "time": 1, "data": {"turn": 2}}')

        # 第二个世界:load 平衡并修复
        ctx2, _, persistence2 = _new_world(root)
        loaded = await persistence2.load("s-crash")
        # 撕裂尾被截断;完整中断回合得到合成关闭器 —— 但本场景回合
        # 已完整关闭,撕裂事件(seq 5)被丢弃,事件前缀原样保留
        assert [e["seq"] for e in loaded.events] == [e["seq"] for e in session.events]
        assert all(e["data"]["reason"]["kind"] == "completed" for e in loaded.events if e["type"] == "turn/end")
        # 修复已耐久:文件不再含撕裂字节
        with open(path, "rb") as handle:
            data = handle.read()
        assert data.endswith(b'"completed"}}}\n')

    asyncio.run(scenario())
