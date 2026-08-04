"""Self-managed retry (design note #11): exponential backoff honoring retry-after.

The adapter layer never retries (maxRetries=0 in Kode's terms); this is the
single retry policy for the harness.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .types import LLMError

DEFAULT_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 1.0
DEFAULT_MAX_DELAY = 30.0


async def with_retry(
    operation: Callable[[], Awaitable],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
) -> Awaitable:
    """Run *operation* with exponential backoff; honor retry-after when given.

    Only retryable errors (429/5xx/network) are retried. The final attempt
    propagates the original error.
    """
    for attempt in range(attempts):
        try:
            return await operation()
        except LLMError as exc:
            if not exc.retryable or attempt == attempts - 1:
                raise
            backoff = min(base_delay * (2**attempt), max_delay)
            wait = exc.retry_after_seconds or backoff
            await asyncio.sleep(wait)
    raise AssertionError("unreachable")
