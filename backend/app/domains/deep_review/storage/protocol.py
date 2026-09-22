"""Storage boundary for append-only Deep Review run events."""

from __future__ import annotations

from typing import Any, Protocol


class DeepReviewStoreError(RuntimeError):
    """Raised when a run event cannot be durably appended."""


class DeepReviewStore(Protocol):
    def append(self, run_id: str, record_type: str, payload: dict[str, Any]) -> None:
        """Append one domain event to a new, run-scoped storage stream."""
