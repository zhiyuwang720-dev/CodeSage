"""Retry policy tests: backoff, retry-after, non-retryable propagation."""

import asyncio

import pytest

from codesage.ai import LLMError
from codesage.ai.retry import with_retry


def _fake_sleep(monkeypatch, sleeps):
    """Record sleep durations without recursing into the patched sleep."""
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: (sleeps.append(s), real_sleep(0))[1])


async def test_retries_then_succeeds(monkeypatch):
    sleeps = []
    _fake_sleep(monkeypatch, sleeps)
    calls = []

    async def op():
        calls.append(1)
        if len(calls) < 3:
            raise LLMError("rate limited", status_code=429, retryable=True)
        return "ok"

    assert await with_retry(op, attempts=4, base_delay=1.0) == "ok"
    assert len(calls) == 3
    # exponential backoff + jitter (0..10% of backoff)
    assert len(sleeps) == 2
    assert 1.0 <= sleeps[0] <= 1.1
    assert 2.0 <= sleeps[1] <= 2.2


async def test_honors_retry_after(monkeypatch):
    sleeps = []
    _fake_sleep(monkeypatch, sleeps)

    async def op():
        raise LLMError("slow down", status_code=429, retryable=True, retry_after_seconds=7.0)

    with pytest.raises(LLMError):
        await with_retry(op, attempts=2, base_delay=1.0)
    assert len(sleeps) == 1  # one retry (last attempt propagates without sleeping)
    assert 7.0 <= sleeps[0] <= 7.1  # retry-after wins over backoff, + jitter


async def test_retry_after_capped(monkeypatch):
    sleeps = []
    _fake_sleep(monkeypatch, sleeps)

    async def op():
        raise LLMError("slow down", status_code=429, retryable=True, retry_after_seconds=3600.0)

    with pytest.raises(LLMError):
        await with_retry(op, attempts=2, base_delay=1.0)
    assert sleeps[0] <= 60.1  # capped at MAX_RETRY_AFTER + jitter


async def test_non_retryable_propagates_immediately(monkeypatch):
    calls = []
    monkeypatch.setattr(asyncio, "sleep", lambda s: asyncio.sleep(0))

    async def op():
        calls.append(1)
        raise LLMError("bad request", status_code=400, retryable=False)

    with pytest.raises(LLMError):
        await with_retry(op, attempts=4)
    assert len(calls) == 1


async def test_5xx_is_retried():
    calls = []

    async def op():
        calls.append(1)
        if len(calls) == 1:
            raise LLMError("boom", status_code=502, retryable=True)
        return "ok"

    assert await with_retry(op, attempts=3, base_delay=0) == "ok"
    assert len(calls) == 2


async def test_final_attempt_propagates_original():
    async def op():
        raise LLMError("always down", status_code=500, retryable=True)

    with pytest.raises(LLMError) as exc_info:
        await with_retry(op, attempts=2, base_delay=0)
    assert exc_info.value.status_code == 500
