from __future__ import annotations

import re
import hashlib
from collections import defaultdict
from pathlib import PurePosixPath

from app.domains.deep_review.schemas.input import ChangeType, FileChange
from app.domains.deep_review.schemas.pipeline import Anatomy, ChangeCluster, DiffHunk, DiffStats

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")

_LANGUAGES = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".rb": "ruby", ".php": "php", ".cs": "csharp", ".c": "c", ".cpp": "cpp",
    ".swift": "swift", ".sh": "shell", ".sql": "sql", ".yaml": "yaml",
    ".yml": "yaml", ".json": "json", ".toml": "toml",
}


def parse_hunks(change: FileChange) -> list[DiffHunk]:
    hunks: list[DiffHunk] = []
    current: DiffHunk | None = None
    lines: list[str] = []

    def finish() -> None:
        nonlocal current
        if current is not None:
            current.content = "\n".join(lines)
            hunks.append(current)
            current = None
            lines.clear()

    for line in change.diff.splitlines():
        match = _HUNK.match(line)
        if match:
            finish()
            current = DiffHunk(
                old_start=int(match.group(1)),
                old_count=int(match.group(2) or 1),
                new_start=int(match.group(3)),
                new_count=int(match.group(4) or 1),
                header=line,
            )
            continue
        if current is not None:
            lines.append(line)
    finish()
    return hunks


def detect_language(path: str) -> str:
    return _LANGUAGES.get(PurePosixPath(path).suffix.lower(), "")


def is_test_file(path: str) -> bool:
    lowered = path.lower()
    return any(marker in lowered for marker in ("test_", "_test.", "tests/", "test/", "__tests__/", "/spec/"))


def compute_stats(changes: list[FileChange]) -> DiffStats:
    test_files = sum(1 for item in changes if is_test_file(item.path))
    code_files = max(1, len(changes) - test_files)
    return DiffStats(
        total_files=len(changes),
        total_additions=sum(item.additions for item in changes),
        total_deletions=sum(item.deletions for item in changes),
        files_added=sum(item.change_type is ChangeType.ADDED for item in changes),
        files_modified=sum(item.change_type is ChangeType.MODIFIED for item in changes),
        files_deleted=sum(item.change_type is ChangeType.DELETED for item in changes),
        files_renamed=sum(item.change_type is ChangeType.RENAMED for item in changes),
        files_binary=sum(item.change_type is ChangeType.BINARY for item in changes),
        test_files=test_files,
        test_to_code_ratio=round(test_files / code_files, 4),
    )


def _cluster_id(name: str) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    return f"cluster_{digest}"


def _directory_key(path: str, depth: int) -> tuple[str, ...]:
    parts = path.split("/")
    return tuple(parts[: max(1, depth)]) if len(parts) > 1 else ("root",)


def _group_name(parts: tuple[str, ...]) -> str:
    return "/".join(parts) if parts else "root"


def _bounded_groups(
    prefix: tuple[str, ...],
    changes: list[FileChange],
    max_files: int,
) -> list[tuple[tuple[str, ...], list[FileChange]]]:
    if len(changes) <= max_files:
        return [(prefix, changes)]

    next_index = len(prefix)
    grouped: dict[str, list[FileChange]] = defaultdict(list)
    can_split_by_path = False
    for change in changes:
        parts = change.path.split("/")
        if next_index < len(parts) - 1:
            key = parts[next_index]
            can_split_by_path = True
        else:
            key = parts[-1]
        grouped[key].append(change)

    if not can_split_by_path:
        ordered = sorted(changes, key=lambda item: item.path)
        return [
            ((*prefix, f"part_{index:03d}"), ordered[index : index + max_files])
            for index in range(0, len(ordered), max_files)
        ]

    groups: list[tuple[tuple[str, ...], list[FileChange]]] = []
    for key in sorted(grouped):
        groups.extend(_bounded_groups((*prefix, key), grouped[key], max_files))
    return groups


def cluster_changes(
    changes: list[FileChange],
    *,
    directory_depth: int = 1,
    max_files_per_cluster: int = 24,
) -> list[ChangeCluster]:
    if max_files_per_cluster < 1:
        raise ValueError("max_files_per_cluster must be positive")
    if directory_depth < 1:
        raise ValueError("directory_depth must be positive")

    grouped: dict[tuple[str, ...], list[FileChange]] = defaultdict(list)
    for change in changes:
        grouped[_directory_key(change.path, directory_depth)].append(change)

    bounded: list[tuple[tuple[str, ...], list[FileChange]]] = []
    for prefix in sorted(grouped):
        bounded.extend(_bounded_groups(prefix, grouped[prefix], max_files_per_cluster))

    clusters: list[ChangeCluster] = []
    for prefix, files in sorted(bounded, key=lambda item: _group_name(item[0])):
        directory = _group_name(prefix)
        files = sorted(files, key=lambda item: item.path)
        languages = [language for language in (detect_language(item.path) for item in files) if language]
        primary = max(sorted(set(languages)), key=languages.count) if languages else ""
        clusters.append(
            ChangeCluster(
                id=_cluster_id(directory),
                name=directory,
                files=[item.path for item in files],
                primary_language=primary,
            )
        )
    return clusters


def build_anatomy(
    changes: list[FileChange],
    related_paths: list[str] | None = None,
    *,
    directory_depth: int = 1,
    max_files_per_cluster: int = 24,
) -> Anatomy:
    stats = compute_stats(changes)
    clusters = cluster_changes(
        changes,
        directory_depth=directory_depth,
        max_files_per_cluster=max_files_per_cluster,
    )
    directories = sorted({item.path.rsplit("/", 1)[0] if "/" in item.path else "root" for item in changes})
    summary = (
        f"Files: {stats.total_files}; +{stats.total_additions}/-{stats.total_deletions}; "
        f"binary={stats.files_binary}; tests={stats.test_files}\n"
        "Directories: " + (", ".join(directories) or "none")
    )
    for cluster in clusters:
        summary += f"\n- {cluster.name}: {len(cluster.files)} files ({cluster.primary_language or 'unknown'})"
    return Anatomy(
        files=changes,
        hunks={change.path: parse_hunks(change) for change in changes},
        stats=stats,
        clusters=clusters,
        directories=directories,
        related_paths=sorted(set(related_paths or [])),
        summary=summary,
    )
