import asyncio

import pytest

from app.domains.deep_review.services.git_runner import run_git_output_bounded


class _NeverEndingStream:
    async def read(self, _size: int) -> bytes:
        await asyncio.Event().wait()
        return b""


class _BlockingGitProcess:
    def __init__(self) -> None:
        self.stdout = _NeverEndingStream()
        self.stderr = _NeverEndingStream()
        self.returncode: int | None = None
        self.killed = False
        self.waited = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _DrainAwareStream:
    def __init__(self, chunks: list[bytes], drained: asyncio.Event) -> None:
        self.chunks = list(chunks)
        self.drained = drained

    async def read(self, _size: int) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        self.drained.set()
        return b""


class _DrainAwareProcess:
    def __init__(self) -> None:
        self.drained = asyncio.Event()
        self.stdout = _DrainAwareStream([b"abc", b"def", b"ghi"], self.drained)
        self.stderr = _DrainAwareStderr(self.drained)
        self.returncode: int | None = None

    def kill(self) -> None:
        self.returncode = -9
        self.drained.set()

    async def wait(self) -> int:
        await self.drained.wait()
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _DrainAwareStderr:
    def __init__(self, drained: asyncio.Event) -> None:
        self.drained = drained

    async def read(self, _size: int) -> bytes:
        await self.drained.wait()
        return b""


async def test_cancelling_bounded_git_output_kills_and_reaps_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _BlockingGitProcess()
    started = asyncio.Event()

    async def create_process(*_args, **_kwargs):
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    task = asyncio.create_task(
        run_git_output_bounded("unused", ["ls-tree"], max_bytes=1024, timeout_seconds=30)
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed
    assert process.waited


async def test_oversized_git_output_is_drained_without_deadlocking_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _DrainAwareProcess()

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    result = await run_git_output_bounded(
        "unused", ["ls-tree"], max_bytes=5, timeout_seconds=0.1,
    )

    assert result.truncated
    assert result.stdout == "abc"
    assert process.returncode == 0
