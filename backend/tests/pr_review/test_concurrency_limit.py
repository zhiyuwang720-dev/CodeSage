"""spec §6 test_concurrency_limit: 同 PR 并发事件第二个等待/丢弃(内存守卫接入)。"""
from app.services.pr_review.webhook_guard import WebhookGuard


def test_same_pr_concurrency_capped():
    clock = [100.0]
    guard = WebhookGuard(clock=lambda: clock[0])

    assert guard.check_and_register("o/r", 1, "a" * 40) == "accepted"
    assert guard.check_and_register("o/r", 1, "b" * 40) == "accepted"
    assert guard.check_and_register("o/r", 1, "c" * 40) == "busy", "同 PR 第 3 个并发被拒"
    assert guard.active_count("o/r", 1) == 2


def test_release_frees_slot():
    guard = WebhookGuard()
    guard.check_and_register("o/r", 1, "a" * 40)
    guard.check_and_register("o/r", 1, "b" * 40)
    guard.release("o/r", 1)
    assert guard.check_and_register("o/r", 1, "c" * 40) == "accepted"


def test_other_pr_not_affected():
    guard = WebhookGuard(max_active_per_pr=1)
    assert guard.check_and_register("o/r", 1, "a" * 40) == "accepted"
    assert guard.check_and_register("o/r", 2, "a" * 40) == "accepted", "不同 PR 互不影响"


def test_seen_ttl_expiry():
    clock = [1000.0]
    guard = WebhookGuard(ttl_seconds=900, clock=lambda: clock[0])
    assert guard.check_and_register("o/r", 1, "a" * 40) == "accepted"
    assert guard.check_and_register("o/r", 1, "a" * 40) == "duplicate"
    clock[0] += 901
    assert guard.check_and_register("o/r", 1, "a" * 40) == "accepted", "TTL 过期后允许重跑"
