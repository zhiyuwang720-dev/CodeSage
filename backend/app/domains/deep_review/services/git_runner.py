from __future__ import annotations

import asyncio
from typing import NamedTuple


class BoundedGitResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    truncated: bool


async def run_git_output_bounded(
    repo_path: str,
    args: list[str],
    *,
    max_bytes: int,
    timeout_seconds: int,
) -> BoundedGitResult:
    process = await asyncio.create_subprocess_exec(
        "git", "-C", repo_path, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def read_limited(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        used = 0
        truncated = False
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            used += len(chunk)
            if used <= max_bytes + 1:
                chunks.append(chunk)
            if used > max_bytes:
                truncated = True
                break
        return b"".join(chunks), truncated

    try:
        (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.wait_for(
            asyncio.gather(read_limited(process.stdout), read_limited(process.stderr)),
            timeout=timeout_seconds,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except TimeoutError:
            process.kill()
            await process.wait()
        return BoundedGitResult(
            process.returncode or 0,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
            stdout_truncated or stderr_truncated,
        )
    except (TimeoutError, asyncio.TimeoutError):
        process.kill()
        await process.wait()
        raise TimeoutError("git output timeout") from None
