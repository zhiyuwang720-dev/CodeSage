"""worktree isolation tests (phase 13 S7, spec §5.1/§5.4): slug 校验、创建/
清理/保留/非 git 报错(纯函数层)+ SubagentRunner 接线(cwd 注入、自动清理、
有变更保留 metadata、参数互斥、定义级 isolation、后台组合)。"""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from codesage.agents import AgentRegistry, SubagentRequest, SubagentRunner
from codesage.agents.worktree import (
    WorktreeError,
    cleanup_worktree,
    create_worktree,
    is_safe_segment,
    worktree_branch,
    worktree_slug,
)
from codesage.ai import LLMResponse, StreamEvent
from codesage.core import Session
from codesage.engine import AgentLoop, AgentLoopConfig
from codesage.permissions import PermissionEngine
from codesage.tools import ToolError, ToolRegistry
from codesage.tools.builtin.agent.agent import AgentTool
from codesage.tools.builtin.shell.bash import BashTool


def _git(cwd: Path, *args: str) -> tuple[int, str, str]:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def _git_repo(tmp_path: Path) -> Path:
    """含一个空提交的 git 仓库(worktree add 需要可解析的 HEAD)。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    code, _o, err = _git(repo, "init", "-q", "-b", "main")
    assert code == 0, err
    code, _o, err = _git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
                         "commit", "-q", "--allow-empty", "-m", "init")
    assert code == 0, err
    return repo


# ---- slug 校验(§5.1:防路径穿越,纵深防御)----


def test_slug_flattens_slashes():
    assert worktree_slug("agent-2026/08/15-1") == "agent-2026+08+15-1"


def test_slug_rejects_dotdot_segments():
    s = worktree_slug("../../etc/passwd")
    assert ".." not in s
    assert s == "etc+passwd"


def test_slug_truncates_to_64_chars():
    assert len(worktree_slug("a" * 200)) <= 64


def test_slug_fully_illegal_falls_back_to_agent():
    assert worktree_slug("/../..") == "agent"  # 全 "."/".." 段 → 兜底名


def test_is_safe_segment():
    assert is_safe_segment("agent-1")
    assert not is_safe_segment("")
    assert not is_safe_segment(".")
    assert not is_safe_segment("..")
    assert not is_safe_segment("a/b")
    assert not is_safe_segment("a" * 65)


# ---- 创建/清理/保留(§5.4 纯函数层)----


def test_create_worktree_non_git_repo_raises(tmp_path):
    with pytest.raises(WorktreeError, match="git 仓库"):
        create_worktree(tmp_path, "agent-1")


def test_create_and_cleanup_without_changes_removes_worktree_and_branch(tmp_path):
    repo = _git_repo(tmp_path)
    wt = create_worktree(repo, "agent-1")
    assert wt.is_dir()
    assert wt == repo / ".codesage" / "worktrees" / worktree_slug("agent-1")
    _code, out, _ = _git(repo, "branch", "--list", worktree_branch("agent-1"))
    assert "worktree-" in out  # 分支已建
    assert cleanup_worktree(repo, "agent-1") is True
    assert not wt.exists()
    _code, out, _ = _git(repo, "branch", "--list", worktree_branch("agent-1"))
    assert out.strip() == ""  # 分支连带删除


def test_cleanup_keeps_worktree_with_changes(tmp_path):
    repo = _git_repo(tmp_path)
    wt = create_worktree(repo, "agent-1")
    (wt / "out.txt").write_text("hi", encoding="utf-8")
    assert cleanup_worktree(repo, "agent-1") is False  # 有变更 → 保留
    assert wt.is_dir()
    assert (wt / "out.txt").read_text(encoding="utf-8") == "hi"


def test_cleanup_missing_worktree_is_noop(tmp_path):
    assert cleanup_worktree(tmp_path, "agent-1") is True


def test_cleanup_keeps_worktree_when_status_fails(tmp_path):
    """git status 失败(状态未知)时绝不强删(M2):损坏 index → 保留。"""
    repo = _git_repo(tmp_path)
    wt = create_worktree(repo, "agent-1")
    (repo / ".git" / "worktrees" / worktree_slug("agent-1") / "index").write_bytes(
        b"corrupt-index")
    assert cleanup_worktree(repo, "agent-1") is False
    assert wt.is_dir()  # 未知状态不删,宁可保留


def test_create_worktree_git_missing_raises(tmp_path, monkeypatch):
    """git 未安装 → FileNotFoundError 归一化为 WorktreeError(L3)。"""

    def no_git(*_a, **_kw):
        raise FileNotFoundError("git")

    monkeypatch.setattr("codesage.agents.worktree.subprocess.run", no_git)
    with pytest.raises(WorktreeError, match="git"):
        create_worktree(tmp_path, "agent-1")


# ---- 定义级 isolation 白名单(loader)----


def test_loader_isolation_whitelist():
    from codesage.agents.loader import build_definition

    d = build_definition("a", {"name": "a", "isolation": "worktree"}, "", "test")
    assert d.isolation == "worktree"
    d2 = build_definition("a", {"name": "a", "isolation": "evil"}, "", "test")
    assert d2.isolation is None  # 未知值 → None,不产生半有效配置


# ---- SubagentRunner 接线 ----


class FakeLLM:
    """Scripted stream; serves the child loop's turns."""

    def __init__(self, script):
        self.script = script
        self.calls = 0
        self.total_cost = [0.0]
        self.last_messages = None

    def stream(self, request, model="main"):
        self.last_messages = request.messages
        return self._gen()

    async def _gen(self):
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        events = self.script[idx](self.calls)
        for ev in events:
            await asyncio.sleep(0)
            yield ev

    async def complete(self, request, model="main"):
        return LLMResponse(content=[])


def tool_use_event(name, tid, input_json):
    return [
        StreamEvent(type="tool_use_start", tool_use_id=tid, tool_name=name),
        StreamEvent(type="tool_use_delta", input_json_delta=input_json),
        StreamEvent(type="done", stop_reason="tool_use"),
    ]


def text_event(text="answer"):
    return [StreamEvent(type="text_delta", text=text), StreamEvent(type="done", stop_reason="end_turn")]


def _make_parent(llm, cwd: Path, tmp_path: Path) -> AgentLoop:
    session = Session("parent-wt", tmp_path / "sessions")
    return AgentLoop(
        AgentLoopConfig(
            client=llm,
            tools=ToolRegistry([AgentTool(), BashTool()]),
            permissions=PermissionEngine(),
            system_prompt="parent system",
            cwd=cwd,
            session=session,
            max_turns=10,
            session_permissions={"allow": ["Agent", "Bash"]},  # 子代理 Bash 直通
        )
    )


def _runner(parent, req, tmp_path) -> SubagentRunner:
    return SubagentRunner(parent, req, AgentRegistry(), session_root=tmp_path / "subagents")


async def test_worktree_isolation_auto_cleanup_without_changes(tmp_path):
    """子代理只对话不动文件 → 终态 worktree 自动删除,零残留。"""
    repo = _git_repo(tmp_path)
    llm = FakeLLM([lambda i: text_event("child done")])
    runner = _runner(
        _make_parent(llm, repo, tmp_path),
        SubagentRequest(prompt="do it", name="general-purpose", isolation="worktree"),
        tmp_path,
    )
    result = await runner.run()
    assert result.is_error is False
    wt_root = repo / ".codesage" / "worktrees"
    assert not wt_root.exists() or not list(wt_root.iterdir())  # 自动清理


async def test_worktree_isolation_child_changes_kept_with_metadata(tmp_path):
    """子代理 Bash 相对路径写文件 → 落 worktree 内(父工作区不脏,隔离生效);
    有变更 → worktree 保留 + metadata 回填 path/branch 供宿主导入。"""
    repo = _git_repo(tmp_path)
    llm = FakeLLM([
        lambda i: tool_use_event("Bash", "b1", '{"command": "echo hi > out.txt"}'),
        lambda i: text_event("child done"),
    ])
    runner = _runner(
        _make_parent(llm, repo, tmp_path),
        SubagentRequest(prompt="do it", name="general-purpose", isolation="worktree"),
        tmp_path,
    )
    result = await runner.run()
    assert result.is_error is False
    wt = Path(result.metadata["worktree_path"])
    assert result.metadata["worktree_branch"] == worktree_branch(runner._agent_id)
    assert wt == repo / ".codesage" / "worktrees" / worktree_slug(runner._agent_id)
    assert (wt / "out.txt").read_text(encoding="utf-8").strip() == "hi"  # 子 cwd = worktree
    assert not (repo / "out.txt").exists()  # 父工作区干净 —— 隔离生效


async def test_worktree_isolation_cwd_param_mutually_exclusive(tmp_path):
    repo = _git_repo(tmp_path)
    runner = _runner(
        _make_parent(FakeLLM([]), repo, tmp_path),
        SubagentRequest(prompt="p", name="general-purpose", isolation="worktree", cwd=repo),
        tmp_path,
    )
    with pytest.raises(ToolError, match="mutually exclusive"):
        await runner.run()


async def test_worktree_isolation_non_git_repo_is_error_not_silent(tmp_path):
    """非 git 仓库 → ToolError 明确报错,绝不降级到父工作区执行。"""
    runner = _runner(
        _make_parent(FakeLLM([]), tmp_path, tmp_path),  # cwd 非 git
        SubagentRequest(prompt="p", name="general-purpose", isolation="worktree"),
        tmp_path,
    )
    with pytest.raises(ToolError, match="git 仓库"):
        await runner.run()


async def test_definition_level_isolation_applies_when_req_omits(tmp_path):
    """定义 frontmatter isolation=worktree → req 不传也生效(§5.4 参数链:
    effectiveIsolation = 工具参数 > 定义)。"""
    repo = _git_repo(tmp_path)
    defs_dir = tmp_path / "agents"
    defs_dir.mkdir()
    (defs_dir / "iso-agent.md").write_text(
        "---\nname: iso-agent\ndescription: isolated\nisolation: worktree\n---\nbody\n",
        encoding="utf-8",
    )
    registry = AgentRegistry(extra_dirs=(defs_dir,))
    llm = FakeLLM([
        lambda i: tool_use_event("Bash", "b1", '{"command": "echo x > def.txt"}'),
        lambda i: text_event("done"),
    ])
    parent = _make_parent(llm, repo, tmp_path)
    runner = SubagentRunner(
        parent,
        SubagentRequest(prompt="p", name="iso-agent"),
        registry,
        session_root=tmp_path / "subagents",
    )
    result = await runner.run()
    assert result.is_error is False
    # 定义级 isolation 生效:worktree 保留 + 子文件落在 worktree 内
    wt = Path(result.metadata["worktree_path"])
    assert (wt / "def.txt").read_text(encoding="utf-8").strip() == "x"
    assert not (repo / "def.txt").exists()


async def test_worktree_cleanup_on_cancellation(tmp_path):
    """取消路径(CancelledError)同样清理:无变更 → worktree 自动删除,不留
    孤儿(L1 三路径一致的实际闸门)。"""
    repo = _git_repo(tmp_path)
    llm = FakeLLM([
        lambda i: tool_use_event("Bash", "b1", '{"command": "sleep 30"}'),
        lambda i: text_event("never"),
    ])
    parent = _make_parent(llm, repo, tmp_path)
    runner = _runner(
        parent,
        SubagentRequest(prompt="p", name="general-purpose", isolation="worktree"),
        tmp_path,
    )
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.6)  # worktree 已创建,子 Bash 挂起中
    task.cancel()  # CancelledError 路径
    with pytest.raises(asyncio.CancelledError):
        await task
    wt_root = repo / ".codesage" / "worktrees"
    assert not wt_root.exists() or not list(wt_root.iterdir())


async def test_worktree_retained_content_carries_path(tmp_path):
    """保留场景:路径追加进 result.content(引擎 tool_result 块只带 content,
    metadata 不进父消息流 —— 契约送达点,M1)。"""
    repo = _git_repo(tmp_path)
    llm = FakeLLM([
        lambda i: tool_use_event("Bash", "b1", '{"command": "echo x > keep.txt"}'),
        lambda i: text_event("done"),
    ])
    runner = _runner(
        _make_parent(llm, repo, tmp_path),
        SubagentRequest(prompt="do it", name="general-purpose", isolation="worktree"),
        tmp_path,
    )
    result = await runner.run()
    assert "worktree 已保留" in result.content
    assert str(result.metadata["worktree_path"]) in result.content
    assert result.metadata["worktree_branch"] in result.content


async def test_background_worktree_combo(tmp_path):
    """后台 + worktree 组合(§6.1 × §5.4):launch 立即返回,子代理在 worktree
    中写文件 → 完成保留,宿主导入面一致(worktree 路径独立于转录隔离)。"""
    repo = _git_repo(tmp_path)
    llm = FakeLLM([
        lambda i: tool_use_event("Bash", "b1", '{"command": "echo bg > bg.txt"}'),
        lambda i: text_event("bg done"),
    ])
    parent = _make_parent(llm, repo, tmp_path)
    runner = _runner(
        parent,
        SubagentRequest(prompt="bg", name="general-purpose",
                        run_in_background=True, isolation="worktree"),
        tmp_path,
    )
    result = runner.launch()
    assert "async_launched" in result.content
    while parent._subagent_tasks:  # 等后台任务终态
        await asyncio.sleep(0.05)
    wt_dirs = list((repo / ".codesage" / "worktrees").glob("*"))
    assert len(wt_dirs) == 1
    assert (wt_dirs[0] / "bg.txt").read_text(encoding="utf-8").strip() == "bg"


async def test_agent_tool_isolation_parameter_end_to_end(tmp_path, monkeypatch):
    """Agent 工具 isolation=worktree 参数 → 子代理在 worktree 执行;父收尾
    自动清理(无变更)。同时验证 validate_input 白名单(非法值已在前置测试)。"""
    from codesage.agents import SubagentRunner as _OrigRunner

    def patched_runner(parent, req, registry):
        return _OrigRunner(parent, req, registry, session_root=tmp_path / "subagents")

    monkeypatch.setattr("codesage.agents.SubagentRunner", patched_runner)

    repo = _git_repo(tmp_path)
    llm = FakeLLM([
        lambda i: tool_use_event("Agent", "a1", json.dumps(
            {"name": "general-purpose", "prompt": "do it", "isolation": "worktree"})),
        lambda i: text_event("child done"),
        lambda i: text_event("parent final"),
    ])
    parent = _make_parent(llm, repo, tmp_path)
    async for _m in parent.run("go"):
        pass
    assert parent.last_stop_reason == "completed"
    wt_root = repo / ".codesage" / "worktrees"
    assert not wt_root.exists() or not list(wt_root.iterdir())  # 无变更 → 自动清理
