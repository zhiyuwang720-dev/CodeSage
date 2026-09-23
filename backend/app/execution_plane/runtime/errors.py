"""Typed Runtime failures used to make retry decisions without parsing text."""

from __future__ import annotations


class NonRetryableModelCallError(RuntimeError):
    """A model request failed for a deterministic reason and must not be retried."""

    recoverable = False

    def __init__(
        self,
        message: str,
        *,
        error_kind: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.status_code = status_code
