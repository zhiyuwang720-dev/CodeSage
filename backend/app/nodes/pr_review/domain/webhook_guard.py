"""webhook_guard: 内存版幂等去重 + 单 PR 并发上限(阶段 01 §3.2.3)。

思路来自 pr-agent github_app.py:77-78 的 DefaultDictWithTimeout 同 PR 并发上限
(GitHub MIT)。分布式部署时替换为 Redis 实现即可(接口不变)。
"""
from __future__ import annotations

import time

SEEN_TTL_SECONDS = 900  # 幂等窗口: 同 (repo, pr, head_sha) 15 分钟内只处理一次
DEFAULT_MAX_ACTIVE_PER_PR = 2  # §7: 单 PR 最多 2 个并发任务, 其余排队/丢弃


class WebhookGuard:
    def __init__(
        self,
        ttl_seconds: int = SEEN_TTL_SECONDS,
        max_active_per_pr: int = DEFAULT_MAX_ACTIVE_PER_PR,
        clock=time.monotonic,
    ):
        self.ttl_seconds = ttl_seconds
        self.max_active_per_pr = max_active_per_pr
        self._clock = clock  # 注入点: 测试可控时钟
        self._seen: dict[str, float] = {}
        self._active: dict[str, int] = {}

    @staticmethod
    def _pr_key(repo: str, pr_number: int | None) -> str:
        return f"{repo}#{pr_number}"

    def check_and_register(self, repo: str, pr_number: int | None, head_sha: str) -> str:
        """决策: duplicate(幂等命中) / busy(并发已满) / accepted(受理)。"""
        now = self._clock()
        # 清理过期幂等记录
        for key, ts in list(self._seen.items()):
            if now - ts > self.ttl_seconds:
                del self._seen[key]
        dedup_key = f"{self._pr_key(repo, pr_number)}@{head_sha}"
        if dedup_key in self._seen:
            return "duplicate"
        if self._active.get(self._pr_key(repo, pr_number), 0) >= self.max_active_per_pr:
            return "busy"
        self._seen[dedup_key] = now
        self._active[self._pr_key(repo, pr_number)] = self._active.get(self._pr_key(repo, pr_number), 0) + 1
        return "accepted"

    def release(self, repo: str, pr_number: int | None) -> None:
        key = self._pr_key(repo, pr_number)
        if key in self._active:
            self._active[key] -= 1
            if self._active[key] <= 0:
                del self._active[key]

    def active_count(self, repo: str, pr_number: int | None) -> int:
        return self._active.get(self._pr_key(repo, pr_number), 0)


# 进程级单例(webhook/reviews 端点共用)
webhook_guard = WebhookGuard()
