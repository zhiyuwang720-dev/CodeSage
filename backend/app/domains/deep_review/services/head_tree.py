"""Bounded fixed-head metadata shared by Blast Radius and Planner validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.services.directory_filter import DirectoryFilter, FilterError, normalize_path
from app.domains.deep_review.services.git_runner import run_git_binary_bounded


SOURCE_SUFFIXES = frozenset({
    ".py", ".pyi", ".go", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hxx", ".java",
})
REGULAR_MODES = frozenset({"100644", "100755"})


class HeadTreeUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HeadEntry:
    path: str
    mode: str
    oid: str


@dataclass(frozen=True, slots=True)
class HeadTreeIndex:
    head_commit: str
    entries: dict[str, HeadEntry]

    def get(self, path: str) -> HeadEntry | None:
        return self.entries.get(path)

    def source_entries(self, suffixes: frozenset[str]) -> list[HeadEntry]:
        return [
            entry for path, entry in sorted(self.entries.items())
            if PurePosixPath(path).suffix.lower() in suffixes
        ]


def _decode_entry(record: bytes) -> HeadEntry | None:
    metadata, separator, raw_path = record.partition(b"\t")
    if not separator:
        raise HeadTreeUnavailable("malformed fixed-head tree entry")
    fields = metadata.split(b" ")
    if len(fields) != 3:
        raise HeadTreeUnavailable("malformed fixed-head tree metadata")
    try:
        mode, kind, oid = (field.decode("ascii") for field in fields)
        path = normalize_path(raw_path.decode("utf-8"))
    except (UnicodeDecodeError, FilterError):
        return None
    except ValueError as exc:
        raise HeadTreeUnavailable("invalid fixed-head metadata encoding") from exc
    if kind != "blob" or mode not in REGULAR_MODES:
        return None
    return HeadEntry(path=path, mode=mode, oid=oid)


async def load_head_tree_index(
    repo_path: str, head_commit: str, config: DeepReviewConfig, *, timeout_seconds: float,
) -> HeadTreeIndex:
    result = await run_git_binary_bounded(
        repo_path, ["ls-tree", "-r", "-z", head_commit],
        max_bytes=config.max_import_tree_bytes,
        timeout_seconds=timeout_seconds,
    )
    if result.returncode != 0 or result.stdout_truncated or result.stderr_truncated:
        raise HeadTreeUnavailable("fixed-head tree unavailable or exceeds byte budget")
    path_filter = DirectoryFilter(config)
    entries: dict[str, HeadEntry] = {}
    path_bytes = 0
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        entry = _decode_entry(record)
        if entry is None or PurePosixPath(entry.path).suffix.lower() not in SOURCE_SUFFIXES:
            continue
        if not path_filter.is_related_path_allowed(entry.path):
            continue
        path_bytes += len(entry.path.encode("utf-8"))
        if len(entries) >= config.max_blast_tree_entries or path_bytes > config.max_blast_index_path_bytes:
            raise HeadTreeUnavailable("fixed-head source index exceeds entry or path-byte budget")
        entries[entry.path] = entry
    return HeadTreeIndex(head_commit=head_commit, entries=entries)


async def validate_head_context_paths(
    repo_path: str,
    head_commit: str,
    candidates: Iterable[str],
    config: DeepReviewConfig,
    *,
    head_tree: HeadTreeIndex | None = None,
) -> set[str]:
    """Fail closed for unverified paths; do not require a complete tree listing."""
    path_filter = DirectoryFilter(config)
    normalized: set[str] = set()
    for candidate in candidates:
        try:
            path = normalize_path(candidate)
        except FilterError:
            continue
        if path_filter.is_related_path_allowed(path):
            normalized.add(path)
    ordered = sorted(normalized)[:512]
    valid = {
        path for path in ordered
        if head_tree is not None and head_tree.head_commit == head_commit and head_tree.get(path) is not None
    }
    missing = [path for path in ordered if path not in valid]
    for offset in range(0, len(missing), 64):
        batch = missing[offset:offset + 64]
        try:
            result = await run_git_binary_bounded(
                repo_path,
                ["--literal-pathspecs", "ls-tree", "-z", head_commit, "--", *batch],
                max_bytes=min(250_000, max(4096, sum(len(p.encode("utf-8")) + 100 for p in batch))),
                timeout_seconds=config.tool_timeout_seconds,
            )
        except (OSError, TimeoutError):
            continue
        if result.returncode != 0 or result.stdout_truncated or result.stderr_truncated:
            continue
        for record in result.stdout.split(b"\0"):
            if not record:
                continue
            try:
                entry = _decode_entry(record)
            except HeadTreeUnavailable:
                continue
            if entry is not None and entry.path in batch:
                valid.add(entry.path)
    return valid
