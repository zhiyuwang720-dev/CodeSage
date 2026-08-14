"""CLI resume injection tests (phase 12 S4, spec §4.5/§7.3/§10.1 L408).

--resume:branch_summary 摘要注入(摘要 boundary 消息 + leaf 链前 2 条 user);
跨 lane 过滤(leaf ∉ 目标 lane 链则跳过);无摘要 → 07 旧逻辑回归。
--continue:中断恢复提示(§1.4.1 注意类三段式 —— [!] 前缀 + entry 序号 +
动作建议,**结构断言不逐字比对文案**);--lane 选分支,未知 lane → exit 1。
"""

import pytest

from codesage.cli import main
from codesage.config import paths
from codesage.core import Session, assistant_message, user_message
from codesage.tools import ToolRegistry, get_builtin_tools


class FakeLoop:
    """Minimal loop: records inputs, yields one text answer (test_main 同款)."""

    def __init__(self):
        self.tools = ToolRegistry(get_builtin_tools())
        self.mode = "default"
        self.inputs = []

    async def run(self, user_input):
        self.inputs.append(user_input)
        yield assistant_message("ok")


def _patch_build_loop(monkeypatch, calls):
    def fake(**kw):
        calls.append(kw)
        return FakeLoop()

    monkeypatch.setattr("codesage.cli.build_loop", fake)


def _patch_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / ".codesage")


# ---- --resume:branch_summary 摘要注入(§4.5)----

def test_resume_injects_summary_and_two_users(tmp_path, monkeypatch, capsys):
    _patch_config_dir(monkeypatch, tmp_path)
    root = paths.config_dir() / "sessions"
    s = Session("sess-a", root)
    s.append_message(user_message("q1"))
    s.append_message(assistant_message("a1"))
    s.append_message(user_message("q2"))
    s.append_message(assistant_message("a2"))
    e3 = s.append_message(user_message("q3"))
    s.append_branch_summary("摘要:auth 修复完成", e3.uuid)  # leaf = 链上最新消息
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--resume", "hi"]) == 0
    history = calls[0]["history"]
    assert history is not None
    assert len(history) == 3  # 摘要 boundary + 恰 2 条 user(前导链)
    assert history[0].is_compaction_summary and "auth 修复完成" in history[0].content
    users = [m for m in history if m.role == "user" and not m.is_compaction_summary]
    assert [m.content for m in users] == ["q1", "q2"]  # leaf(q3)之前的 2 条 user
    assert "resuming sess-a" in capsys.readouterr().out


def test_resume_summary_leaf_mid_chain_takes_users_before_leaf(tmp_path, monkeypatch):
    # 压缩在链中发生(§4.5 leaf = 切点后第一条消息,非最新):leaf=u3 在链中,
    # 注入取切点前的 2 条 user(u1,u2),不含切点后的 u4
    _patch_config_dir(monkeypatch, tmp_path)
    root = paths.config_dir() / "sessions"
    s = Session("sess-am", root)
    s.append_message(user_message("u1"))
    s.append_message(assistant_message("a1"))
    s.append_message(user_message("u2"))
    s.append_message(assistant_message("a2"))
    e3 = s.append_message(user_message("u3"))
    s.append_message(assistant_message("a3"))
    s.append_message(user_message("u4"))
    s.append_branch_summary("中间摘要", e3.uuid)  # leaf = 切点后第一条消息
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--resume", "hi"]) == 0
    history = calls[0]["history"]
    assert "中间摘要" in history[0].content
    users = [m for m in history if m.role == "user" and not m.is_compaction_summary]
    assert [m.content for m in users] == ["u1", "u2"]


def test_resume_cross_lane_skips_other_branch_summary(tmp_path, monkeypatch):
    # main 链 q1,a1,q2,a2;fork b1 @ q2 → q3,a3;main 的摘要挂 a2(∉ b1 链,
    # 文件序在前)→ 跨 lane 跳过,命中 b1 自己的摘要(§4.5 跨 lane 过滤)
    _patch_config_dir(monkeypatch, tmp_path)
    root = paths.config_dir() / "sessions"
    s = Session("sess-b", root)
    s.append_message(user_message("q1"))
    s.append_message(assistant_message("a1"))
    e2 = s.append_message(user_message("q2"))
    a2 = s.append_message(assistant_message("a2"))
    s.append_lane("b1", e2.uuid)  # fork @ q2 → 活跃 lane = b1
    e3 = s.append_message(user_message("q3"))
    s.append_message(assistant_message("a3"))
    s.append_branch_summary("main 摘要", a2.uuid)  # leaf ∉ b1 链 → 应跳过
    s.append_branch_summary("b1 摘要", e3.uuid)
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--resume", "hi"]) == 0  # 活跃 lane = b1
    history = calls[0]["history"]
    assert "b1 摘要" in history[0].content
    users = [m for m in history if m.role == "user" and not m.is_compaction_summary]
    assert [m.content for m in users] == ["q1", "q2"]  # b1 链上 leaf(q3)之前


def test_resume_falls_back_to_old_logic_when_no_summary_on_lane(tmp_path, monkeypatch, capsys):
    # --lane main:摘要只挂在别的分支(b1)→ 无命中 → 07 旧逻辑(最后 10 条)
    _patch_config_dir(monkeypatch, tmp_path)
    root = paths.config_dir() / "sessions"
    s = Session("sess-c", root)
    s.append_message(user_message("q1"))
    s.append_message(assistant_message("a1"))
    e2 = s.append_message(user_message("q2"))
    s.append_message(assistant_message("a2"))
    s.append_lane("b1", e2.uuid)
    e3 = s.append_message(user_message("q3"))
    s.append_message(assistant_message("a3"))
    s.append_branch_summary("b1 摘要", e3.uuid)  # leaf ∈ b1 链,∉ main 链
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--resume", "--lane", "main", "hi"]) == 0
    out = capsys.readouterr().out
    assert "resuming sess-c" in out and "showing last" in out
    assert calls[0]["history"] is None  # 未注入 → 新会话(07 语义)


def test_resume_no_summary_old_behavior(tmp_path, monkeypatch, capsys):
    # 07 回归:无 branch_summary → 旧行为(最后 10 条展示,history=None)
    _patch_config_dir(monkeypatch, tmp_path)
    root = paths.config_dir() / "sessions"
    s = Session("sess-d", root)
    for i in range(12):
        s.append_message(user_message(f"q{i}"))
        s.append_message(assistant_message(f"a{i}"))
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--resume", "hi"]) == 0
    out = capsys.readouterr().out
    assert "resuming sess-d" in out and "showing last 10" in out
    assert calls[0]["history"] is None


# ---- --continue:中断恢复提示(§7.3,§1.4.1 三段式结构断言)----

def test_continue_interrupt_notice_structure(tmp_path, monkeypatch, capsys):
    # 文件末尾 operation = 未完成(§7.2 启发式)→ [!] 前缀 + 事实(工具名/参数)
    # + entry 序号 + 动作建议,三要素齐备(结构断言,不逐字比对文案)
    _patch_config_dir(monkeypatch, tmp_path)
    root = paths.config_dir() / "sessions"
    s = Session("sess-e", root)
    s.append_message(user_message("q1"))
    s.append_message(assistant_message("a1"))
    s.append_operation("tool_started", tool="Bash", args_summary="npm run deploy")
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--continue", "hi"]) == 0
    err = capsys.readouterr().err
    assert "Continuing session sess-e" in err
    assert "[!]" in err  # 前缀
    assert "Bash" in err and "npm run deploy" in err  # 事实(tool + args_summary)
    assert "entry 3" in err  # entry 序号 = 文件序编号(第 3 条:q1,a1,op)
    assert " —— " in err  # 序号与动作建议间的分隔符(动作在分隔符之后)
    assert len(calls[0]["history"]) == 2  # 提示不注入 history(原样继续)


def test_continue_no_interrupt_no_notice(tmp_path, monkeypatch, capsys):
    # 末尾是消息(操作已完成)→ 无 [!] 提示,维持既有输出
    _patch_config_dir(monkeypatch, tmp_path)
    root = paths.config_dir() / "sessions"
    s = Session("sess-f", root)
    s.append_message(user_message("q1"))
    s.append_message(assistant_message("a1"))
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--continue", "hi"]) == 0
    err = capsys.readouterr().err
    assert "Continuing session sess-f" in err
    assert "[!]" not in err


# ---- --continue --lane(§4.4)----

def test_continue_lane_unknown_exits_1(tmp_path, monkeypatch, capsys):
    _patch_config_dir(monkeypatch, tmp_path)
    root = paths.config_dir() / "sessions"
    s = Session("sess-g", root)
    s.append_message(user_message("q1"))
    s.append_message(assistant_message("a1"))
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--continue", "--lane", "ghost", "hi"]) == 1
    assert not calls  # build_loop 未到达
    assert "lane not found" in capsys.readouterr().err


def test_continue_lane_known_history_is_lane_chain(tmp_path, monkeypatch):
    # 多分支文件,--lane b1 → history = b1 链(q1,a1,q2,q3,a3),非活跃 main 链
    _patch_config_dir(monkeypatch, tmp_path)
    root = paths.config_dir() / "sessions"
    s = Session("sess-h", root)
    s.append_message(user_message("q1"))
    s.append_message(assistant_message("a1"))
    e2 = s.append_message(user_message("q2"))
    s.append_message(assistant_message("a2"))
    s.append_lane("b1", e2.uuid)  # fork @ q2 → b1 成为活跃 lane
    s.append_message(user_message("q3"))
    s.append_message(assistant_message("a3"))
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--continue", "--lane", "b1", "hi"]) == 0
    assert [m.content for m in calls[0]["history"]] == ["q1", "a1", "q2", "q3", "a3"]


# ---- --session-id 共用注入分支(§5)+ load_lane 续写 E2E ----

def test_session_id_resume_injects_summary(tmp_path, monkeypatch, capsys):
    # §5:--session-id 与 --resume 共用注入分支(指定 session 的活跃 lane 摘要)
    _patch_config_dir(monkeypatch, tmp_path)
    root = paths.config_dir() / "sessions"
    s = Session("sess-j", root)
    s.append_message(user_message("q1"))
    s.append_message(assistant_message("a1"))
    e2 = s.append_message(user_message("q2"))
    s.append_branch_summary("摘要:q2 处压缩", e2.uuid)
    calls = []
    _patch_build_loop(monkeypatch, calls)

    assert main(["--session-id", "sess-j", "hi"]) == 0
    history = calls[0]["history"]
    assert history is not None and history[0].is_compaction_summary
    assert "q2 处压缩" in history[0].content
    assert [m.content for m in history[1:]] == ["q1"]  # leaf(q2)之前唯一 user
    assert "resuming sess-j" in capsys.readouterr().out


def test_load_lane_append_continues_named_lane(tmp_path):
    # E2E:load_lane(命名 lane)后 append_message → 重读,新消息挂在命名 lane
    # 链上(append_message 恒写 self._lane 的 lane 指针,与 load/append_lane
    # 共享 —— --continue --lane 的续写机制),活跃 lane main 不受影响
    s = Session("sess-i", tmp_path / "sessions")
    s.append_message(user_message("q1"))
    s.append_message(assistant_message("a1"))
    e2 = s.append_message(user_message("q2"))
    s.append_message(assistant_message("a2"))
    s.append_lane("b1", e2.uuid)  # fork @ q2 → 活跃 lane = b1
    s.append_message(user_message("q3"))
    s.append_message(assistant_message("a3"))
    s.load_lane("b1")  # 重建游标到 b1 leaf(a3)
    s.append_message(user_message("q4"))

    assert [m.content for m in s.load_lane("b1")] == ["q1", "a1", "q2", "q3", "a3", "q4"]
    assert [m.content for m in s.load_lane("main")] == ["q1", "a1", "q2", "a2"]
