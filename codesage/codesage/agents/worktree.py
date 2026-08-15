"""worktree isolation (spec 13 §5.1/§5.4, S7):子代理在独立 git worktree 中执行。

文件系统隔离与转录隔离正交:worktree 内操作不落父工作区,未提交变更对
子代理不可见(从 HEAD 检出)是特性而非缺陷。非 git 仓库显式报错,不静默
降级。slug 校验防路径穿越到 .claude/worktrees/ 之外(对齐 CC worktree.ts)。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_SLUG_MAX = 64
_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


class WorktreeError(Exception):
    """worktree 创建/清理失败;转 ToolError 交父自愈(非 git 报错路径)。"""


def worktree_slug(agent_id: str) -> str:
    """agent_id → 安全 slug(对齐 CC worktree.ts:66-85):斜杠 flatten 为
    '+',每段仅 [a-zA-Z0-9._-],禁 '.'/'..' 段,总长 ≤64。

    agent_id 是本进程生成(agent-YYYYmmdd-HHMMSS-ffffff),理论上已安全;
    校验是纵深防御 —— 防未来 id 来源变更把路径穿越注入 .claude/worktrees/ 外。
    """
    slug = agent_id.replace("/", "+")
    slug = "".join(c for c in slug if c.isalnum() or c in "._-+")[: _SLUG_MAX]
    # 拒绝 '.'/'..' 段(纯段清洗兜底;常规 agent_id 不会走到这里)
    segments = [s for s in slug.split("+") if s not in ("", ".", "..")]
    return "+".join(segments) or "agent"  # 全非法 → 兜底名,防空路径


def is_safe_segment(segment: str) -> bool:
    """单段合法性:非空、非 ./..(正则允许 '.'/'..' 字符,须显式排除)、
    仅 [a-zA-Z0-9._-]、长度 ≤64。"""
    return (segment not in (".", "..")
            and 0 < len(segment) <= _SLUG_MAX
            and bool(_SEGMENT_RE.match(segment)))


def worktree_path(cwd: Path, agent_id: str) -> Path:
    """worktree 落点:.claude/worktrees/<slug>。slug 逐段经 is_safe_segment
    终检(L2 纵深防御最后一道闸):清洗逻辑出 bug 时拒绝而非静默穿越。"""
    slug = worktree_slug(agent_id)
    if not all(is_safe_segment(seg) for seg in slug.split("+")):
        raise WorktreeError(f"unsafe worktree slug: {slug!r}")
    return cwd / ".claude" / "worktrees" / slug


def worktree_branch(agent_id: str) -> str:
    return f"worktree-{worktree_slug(agent_id)}"


def _git(cwd: Path, *args: str) -> tuple[int, str, str]:
    """git 调用封装:返回 (returncode, stdout, stderr)。

    30s 超时(M3):Windows stale index.lock 让 git 无限等待 —— 调用点在父
    事件循环内同步阻塞,挂死即全局卡死;超时归一化为 124 供调用方处置。
    git 未安装 → FileNotFoundError 归一化为 127(保持 WorktreeError 包装面)。
    """
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           timeout=30)
    except FileNotFoundError:
        return 127, "", "git executable not found"
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out after 30s"
    return r.returncode, r.stdout, r.stderr


def create_worktree(cwd: Path, agent_id: str) -> Path:
    """自父 cwd 当前 HEAD 检出独立 worktree(分支 worktree-<slug>)。

    非 git 仓库 → WorktreeError(Agent 工具转错误 tool_result,不静默降级);
    其余失败同样 WorktreeError(附 git 原始报错)。
    """
    wt = worktree_path(cwd, agent_id)
    branch = worktree_branch(agent_id)
    code, _out, err = _git(cwd, "worktree", "add", "-b", branch, str(wt), "HEAD")
    if code != 0:
        msg = (err or _out).strip()
        if "not a git repository" in msg.lower() or "git executable not found" in msg.lower():
            raise WorktreeError(
                "isolation=worktree 需要 git 仓库(父工作区非 git、未初始化或 git 未安装)")
        raise WorktreeError(f"git worktree add failed: {msg}")
    return wt


def cleanup_worktree(cwd: Path, agent_id: str) -> bool:
    """S7 收尾(§5.4,挂 run() 终态单点):worktree 内 git diff 无任何文件
    变更 → 自动删除(True);有变更 → 保留(False,宿主经 worktree_path/
    worktree_branch 导入/合并,CC 同款)。删除失败也保留(防破坏,可逆)。"""
    wt = worktree_path(cwd, agent_id)
    branch = worktree_branch(agent_id)
    if not wt.exists():
        return True  # 创建失败/未创建 → 无事可清
    code, out, _err = _git(wt, "status", "--porcelain")
    if code != 0:
        return False  # 状态未知 → 保留(M2):恰在最不确定有无变更的时刻绝不强删
    if out.strip():
        return False  # 有未提交变更 → 保留
    code, _out, _err = _git(cwd, "worktree", "remove", "--force", str(wt))
    if code != 0:
        return False  # 删除失败(Windows 文件锁等)→ 保留,可手动 git worktree remove
    _git(cwd, "branch", "-D", branch)  # best-effort:worktree remove 不删分支
    return True
