"""Read-only access to fixed Git objects for PR review."""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from app.contracts.review_context import (
    DiffBasis,
    RepositorySnapshotRef,
    snapshot_identity_payload,
    stable_hash,
)


class SnapshotError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_repository_path(path: str) -> str:
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise SnapshotError("source_invalid_path", "repository path is invalid")
    normalized = path.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if (
        candidate.is_absolute()
        or normalized.startswith(("/", "//"))
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise SnapshotError("source_invalid_path", f"unsafe repository path: {path!r}")
    return candidate.as_posix()


async def run_git(repository: Path, *args: str, timeout: float = 10.0) -> bytes:
    if not repository.is_dir():
        raise SnapshotError("source_unavailable", f"repository is unavailable: {repository}")
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    process = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "diff.external=",
        "-c",
        "core.attributesFile=",
        "-C",
        str(repository),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise SnapshotError("source_timeout", "Git operation timed out") from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        lowered = detail.lower()
        code = "source_permission_denied" if "permission denied" in lowered else "source_revision_missing"
        raise SnapshotError(code, detail or "Git object lookup failed")
    return stdout


async def resolve_commit(repository: Path, revision: str) -> str:
    if not revision or revision.startswith("-"):
        raise SnapshotError("source_revision_missing", "revision is required")
    raw = await run_git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}")
    return raw.decode("ascii", errors="strict").strip().lower()


async def create_snapshot_ref(
    repository: Path,
    *,
    repository_key: str,
    base_revision: str,
    head_revision: str,
    diff_basis: DiffBasis,
) -> RepositorySnapshotRef:
    base_tip = await resolve_commit(repository, base_revision)
    head_sha = await resolve_commit(repository, head_revision)
    effective_base = base_tip
    if diff_basis == "merge_base":
        effective_base = (
            await run_git(repository, "merge-base", base_tip, head_sha)
        ).decode("ascii").strip().lower()
    object_format = "sha1"
    try:
        object_format = (
            await run_git(repository, "rev-parse", "--show-object-format")
        ).decode("ascii").strip() or "sha1"
    except SnapshotError:
        pass
    payload = snapshot_identity_payload(
        repository_key=repository_key,
        effective_base_sha=effective_base,
        head_sha=head_sha,
        original_base_tip=base_tip,
        diff_basis=diff_basis,
        git_object_format=object_format,
    )
    manifest_hash = stable_hash(payload)
    return RepositorySnapshotRef(
        snapshot_id=f"snapshot-{manifest_hash[:24]}",
        manifest_hash=manifest_hash,
        **payload,
    )


@dataclass(frozen=True)
class GitSnapshotReader:
    repository: Path
    snapshot: RepositorySnapshotRef

    async def verify(self) -> None:
        for revision in (self.snapshot.effective_base_sha, self.snapshot.head_sha):
            resolved = await resolve_commit(self.repository, revision)
            if resolved != revision:
                raise SnapshotError("source_revision_missing", f"fixed revision changed: {revision}")
        await run_git(self.repository, "ls-tree", "-r", "--name-only", self.snapshot.head_sha)

    def commit_for_side(self, side: Literal["base", "head"]) -> str:
        return self.snapshot.effective_base_sha if side == "base" else self.snapshot.head_sha

    async def read_blob(self, *, side: Literal["base", "head"], path: str, max_bytes: int = 1_048_576) -> bytes:
        safe_path = validate_repository_path(path)
        commit = self.commit_for_side(side)
        entry = await run_git(self.repository, "ls-tree", commit, "--", safe_path)
        if not entry:
            raise SnapshotError("source_not_found", f"path is not present in fixed snapshot: {safe_path}")
        header = entry.decode("utf-8", errors="replace").split("\t", 1)[0].split()
        if len(header) < 3 or header[1] != "blob" or header[0] == "120000":
            raise SnapshotError("source_unsupported", f"unsupported Git object at {safe_path}")
        size = int((await run_git(self.repository, "cat-file", "-s", header[2])).decode("ascii").strip())
        if size > max_bytes:
            raise SnapshotError("source_too_large", f"blob exceeds {max_bytes} bytes")
        return await run_git(self.repository, "cat-file", "blob", header[2])

    async def list_paths(self, *, side: Literal["base", "head"] = "head") -> list[str]:
        raw = await run_git(
            self.repository,
            "ls-tree",
            "-r",
            "-z",
            "--name-only",
            self.commit_for_side(side),
        )
        return [item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item]
