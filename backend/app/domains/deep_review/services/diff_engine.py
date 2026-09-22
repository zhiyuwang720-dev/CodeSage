from __future__ import annotations

import re
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


def cluster_changes(changes: list[FileChange]) -> list[ChangeCluster]:
    grouped: dict[str, list[FileChange]] = defaultdict(list)
    for change in changes:
        directory = change.path.rsplit("/", 1)[0] if "/" in change.path else "root"
        grouped[directory].append(change)

    clusters: list[ChangeCluster] = []
    for index, directory in enumerate(sorted(grouped)):
        files = grouped[directory]
        languages = [language for language in (detect_language(item.path) for item in files) if language]
        primary = max(sorted(set(languages)), key=languages.count) if languages else ""
        clusters.append(
            ChangeCluster(
                id=f"cluster_{index}",
                name=directory,
                files=[item.path for item in files],
                primary_language=primary,
            )
        )
    return clusters


def build_anatomy(changes: list[FileChange], related_paths: list[str] | None = None) -> Anatomy:
    stats = compute_stats(changes)
    clusters = cluster_changes(changes)
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
