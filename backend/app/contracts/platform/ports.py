"""The three intentionally coarse platform ports used by local nodes."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .execution import AttemptContext, RunCommand, SubmissionReceipt


class RunSubmissionPort(Protocol):
    async def submit(self, command: RunCommand) -> str: ...
    async def request_cancel(self, task_id: str) -> bool: ...
    async def prepare_resume(self, task_id: str) -> str: ...


class RunOwnershipPort(Protocol):
    async def claim(self, task_id: str, *, node_id: str, delivery_id: str) -> AttemptContext: ...
    async def renew(self, context: AttemptContext) -> AttemptContext: ...
    async def assert_current(self, context: AttemptContext, *, transaction: Any) -> None: ...
    async def release(self, context: AttemptContext) -> None: ...


class ResultCommitPort(Protocol):
    async def commit(
        self,
        context: AttemptContext,
        business_write: Callable[[Any], Awaitable[None]],
    ) -> SubmissionReceipt: ...
