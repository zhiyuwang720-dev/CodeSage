"""Self-managed retry (design note #11): exponential backoff honoring retry-after.

The adapter layer never retries (maxRetries=0 in Kode's terms); this is the
single retry policy for the harness. retry-after is capped (a server saying
3600s must not sleep an hour) and a small jitter prevents synchronized
retry storms.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any

from .types import LLMError

DEFAULT_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 1.0
DEFAULT_MAX_DELAY = 30.0
#: Cap for server-provided retry-after; backoff never exceeds this either.
MAX_RETRY_AFTER = 60.0
#: Jitter fraction of the backoff delay.
JITTER_FRACTION = 0.1


def cancelled_error() -> LLMError:
    """Cancellation signal: never retried, never eligible for fallback."""
    return LLMError("cancelled", retryable=False, cancelled=True)


async def with_cancel(awaitable: Awaitable, cancel_event: asyncio.Event | None) -> Any:
    """Await *awaitable*, aborting early once *cancel_event* is set.

    httpx requests can't be cancelled mid-flight from outside, so we race
    the awaitable against the event and discard the loser. Cancellation
    surfaces as LLMError("cancelled") so callers handle it like any other
    provider error (retry/fallback see retryable=False, cancelled=True).
    The awaitable is owned as a task up front so it is always awaited or
    cancelled — never left to dangle unawaited.
    """
    if cancel_event is None:
        return await awaitable
    task = asyncio.ensure_future(awaitable)
    if cancel_event.is_set():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise cancelled_error()
    waiter = asyncio.ensure_future(cancel_event.wait())
    try:
        await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
        if cancel_event.is_set():
            raise cancelled_error()
        return task.result()
    finally:
        for t in (task, waiter):
            if not t.done():
                t.cancel()
        await asyncio.gather(task, waiter, return_exceptions=True)


async def with_retry(
    operation: Callable[[], Awaitable],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    cancel_event: asyncio.Event | None = None,
) -> Awaitable:
    """Run *operation* with exponential backoff + jitter; honor capped retry-after.

    Only retryable errors (408/409/429/5xx/network) are retried. The final
    attempt propagates the original error. When *cancel_event* is set, the
    backoff sleep aborts immediately with LLMError("cancelled").
    """
    for attempt in range(attempts):
        try:
            return await operation()
        except LLMError as exc:
            if not exc.retryable or attempt == attempts - 1:
                raise
            backoff = min(base_delay * (2**attempt), max_delay)
            raw_wait = exc.retry_after_seconds or backoff
            wait = min(raw_wait, MAX_RETRY_AFTER) + random.uniform(0, backoff * JITTER_FRACTION)
            await with_cancel(asyncio.sleep(wait), cancel_event)
    raise AssertionError("unreachable")
