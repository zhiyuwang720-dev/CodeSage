"""git_providers: VCS 差异隔离层(阶段 01 §3.1)。

接口从 pr-agent 基类(GitHub MIT, git_provider.py 20 抽象方法)按需精简为
本仓库实际用到的 3 个能力: 解析输入 / 取 diff / 取 CI 状态。
范围控制(§3.4): 只做 GitHub + plain-diff; 其余 provider 按需扩展注册表。
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol

_GITHUB_PR_URL = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)"
    r"/pull/(?P<number>\d+)/?$"
)


class UnsupportedVCSError(Exception):
    """不支持的 VCS/输入(§7: 显式拒绝, 对应 HTTP 501)。"""


@dataclass(frozen=True)
class GitHubRepoInfo:
    owner: str
    repo: str
    number: int


def parse_github_pr_url(pr_url: str) -> GitHubRepoInfo:
    m = _GITHUB_PR_URL.match(pr_url.strip())
    if not m:
        raise UnsupportedVCSError(f"无法解析 GitHub PR URL: {pr_url!r}")
    return GitHubRepoInfo(m["owner"], m["repo"], int(m["number"]))


class Provider(Protocol):
    """精简 provider 协议(替代 pr-agent 20 方法基类)。"""

    name: str

    def get_diff(self) -> str: ...

    def get_ci_status(self, head_sha: str | None) -> dict | None: ...


class GitHubProvider:
    """GitHub REST API 取 diff/CI; token 可选(私有仓库/提额)。"""

    name = "github"

    def __init__(
        self,
        info: GitHubRepoInfo,
        token: str | None = None,
        api_base: str = "https://api.github.com",
        fetcher: Callable[[str], tuple[int, str]] | None = None,
    ):
        self.info = info
        self.token = token
        self.api_base = api_base.rstrip("/")
        self._fetcher = fetcher  # 注入点: 测试/离线用, 免网络

    def _get(self, path: str, accept: str = "application/vnd.github+json") -> tuple[int, str]:
        if self._fetcher is not None:
            return self._fetcher(f"{self.api_base}{path}")
        url = f"{self.api_base}{path}"
        req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "codesage-pr-review"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
            return exc.code, exc.read().decode("utf-8", errors="replace")

    def get_diff(self) -> str:
        """PR 统一 diff(Accept: vnd.github.v3.diff)。404/无权限时抛错。"""
        status, body = self._get(
            f"/repos/{self.info.owner}/{self.info.repo}/pulls/{self.info.number}",
            accept="application/vnd.github.v3.diff",
        )
        if status != 200:
            raise RuntimeError(f"GitHub API {status}: 无法获取 PR diff")
        return body

    def get_ci_status(self, head_sha: str | None) -> dict | None:
        """§7: CI 不可用返回 None, 不阻塞审查。"""
        if not head_sha:
            return None
        status, body = self._get(
            f"/repos/{self.info.owner}/{self.info.repo}/commits/{head_sha}/check-runs"
        )
        if status != 200:
            return None
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None
        runs = [
            {"name": r.get("name"), "conclusion": r.get("conclusion"), "status": r.get("status")}
            for r in data.get("check_runs", [])
        ]
        return {"head_sha": head_sha, "check_runs": runs}


class PlainDiffProvider:
    """纯 diff 输入(benchmark 主通道): diff 即数据, 无网络。"""

    name = "plain_diff"

    def __init__(self, diff_text: str):
        self.diff_text = diff_text

    def get_diff(self) -> str:
        return self.diff_text

    def get_ci_status(self, head_sha: str | None) -> dict | None:
        return None


def provider_for_input(
    pr_url: str | None = None, diff_text: str | None = None
) -> Provider:
    """输入解析: pr_url 优先; 纯 diff 次之; 都无则报错。"""
    if pr_url:
        if "github.com" in pr_url:
            return GitHubProvider(parse_github_pr_url(pr_url))
        raise UnsupportedVCSError(
            f"暂不支持的 PR URL(需 GitHub 或 plain-diff): {pr_url!r}"
        )
    if diff_text is not None:
        return PlainDiffProvider(diff_text)
    raise ValueError("需要 pr_url 或 diff_text 之一")


def run_git(repo_dir, *args: str, check: bool = True) -> str:
    """本地 git 命令(text 输出)。clone/diff/log 全走本地 git, 离线可用。"""
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {proc.stderr.strip()}")
    return proc.stdout


def clone_repo(source: str, dest, extra_args: list[str] | None = None) -> None:
    """git clone; source 兼容本地路径/file://(测试 fixture 与私有 SSH 复用)。"""
    cmd = ["git", "clone", *(extra_args or []), source, str(dest)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git clone 失败: {proc.stderr.strip()}")
