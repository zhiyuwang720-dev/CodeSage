"""Deterministic, complete unified-diff index for Plan 21A."""
from __future__ import annotations

import hashlib
import re
import shlex
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.review_context import ChangeUnit, DIFF_PARSER_VERSION


_HUNK = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? \+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@"
)


class IndexedDiffLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str
    kind: Literal["context", "added", "deleted", "metadata"]
    old_line: int | None = None
    new_line: int | None = None


class DiffHunkIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hunk_id: str
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[IndexedDiffLine] = Field(default_factory=list)


class DiffFileIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str
    old_path: str
    new_path: str
    status: Literal["added", "modified", "deleted", "renamed", "binary", "metadata"] = "modified"
    binary: bool = False
    patch_size_bytes: int = 0
    hunks: list[DiffHunkIndex] = Field(default_factory=list)
    metadata: list[str] = Field(default_factory=list)

    @property
    def display_path(self) -> str:
        return self.new_path if self.new_path != "/dev/null" else self.old_path


class DiffIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    parser_version: str = DIFF_PARSER_VERSION
    diff_sha256: str
    files: list[DiffFileIndex] = Field(default_factory=list)
    change_units: list[ChangeUnit] = Field(default_factory=list)
    parse_errors: list[dict] = Field(default_factory=list)

    def file(self, file_id: str) -> DiffFileIndex | None:
        return next((item for item in self.files if item.file_id == file_id), None)


def _id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _header_paths(line: str) -> tuple[str, str] | None:
    try:
        tokens = shlex.split(line, posix=True)
    except ValueError:
        return None
    if len(tokens) != 4 or tokens[:2] != ["diff", "--git"]:
        return None
    old_path, new_path = tokens[2], tokens[3]
    return (
        old_path[2:] if old_path.startswith("a/") else old_path,
        new_path[2:] if new_path.startswith("b/") else new_path,
    )


def parse_unified_diff(diff_text: str, *, diff_sha256: str) -> DiffIndex:
    files: list[DiffFileIndex] = []
    errors: list[dict] = []
    current: DiffFileIndex | None = None
    current_hunk: DiffHunkIndex | None = None
    old_line = new_line = 0
    file_bytes = 0

    def finish_file() -> None:
        nonlocal current, current_hunk, file_bytes
        if current is None:
            return
        current.patch_size_bytes = file_bytes
        if not current.hunks and current.status == "modified":
            current.status = "metadata"
        files.append(current)
        current = None
        current_hunk = None
        file_bytes = 0

    for number, raw in enumerate(diff_text.splitlines(keepends=True), start=1):
        line = raw.rstrip("\r\n")
        if line.startswith("diff --git "):
            finish_file()
            paths = _header_paths(line)
            if paths is None:
                errors.append({"line": number, "code": "invalid_file_header", "text": line[:256]})
                continue
            old_path, new_path = paths
            current = DiffFileIndex(
                file_id=_id("file", diff_sha256, old_path, new_path),
                old_path=old_path,
                new_path=new_path,
            )
            file_bytes = len(raw.encode("utf-8"))
            continue
        if current is None:
            if line.strip():
                errors.append({"line": number, "code": "content_outside_file", "text": line[:256]})
            continue
        file_bytes += len(raw.encode("utf-8"))
        if line.startswith("@@@"):
            errors.append({"line": number, "code": "combined_diff_unsupported", "file_id": current.file_id})
            current_hunk = None
            continue
        match = _HUNK.match(line)
        if match:
            old_line = int(match.group("old"))
            new_line = int(match.group("new"))
            current_hunk = DiffHunkIndex(
                hunk_id=_id("hunk", current.file_id, str(len(current.hunks)), line),
                header=line,
                old_start=old_line,
                old_count=int(match.group("old_count") or 1),
                new_start=new_line,
                new_count=int(match.group("new_count") or 1),
            )
            current.hunks.append(current_hunk)
            continue
        if line.startswith("new file mode"):
            current.status = "added"
        elif line.startswith("deleted file mode"):
            current.status = "deleted"
        elif line.startswith("rename from "):
            current.status = "renamed"
            current.old_path = line[len("rename from ") :]
        elif line.startswith("rename to "):
            current.status = "renamed"
            current.new_path = line[len("rename to ") :]
        elif line.startswith(("GIT binary patch", "Binary files ")):
            current.binary = True
            current.status = "binary"
            current_hunk = None

        if current_hunk is None:
            current.metadata.append(line)
            continue
        if line.startswith("+") and not line.startswith("+++"):
            current_hunk.lines.append(IndexedDiffLine(text=line, kind="added", new_line=new_line))
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            current_hunk.lines.append(IndexedDiffLine(text=line, kind="deleted", old_line=old_line))
            old_line += 1
        elif line.startswith(" "):
            current_hunk.lines.append(
                IndexedDiffLine(text=line, kind="context", old_line=old_line, new_line=new_line)
            )
            old_line += 1
            new_line += 1
        else:
            current_hunk.lines.append(IndexedDiffLine(text=line, kind="metadata"))
    finish_file()

    units: list[ChangeUnit] = []
    for file in files:
        if file.hunks:
            for hunk in file.hunks:
                units.append(
                    ChangeUnit(
                        unit_id=_id("unit", diff_sha256, file.file_id, hunk.hunk_id),
                        file_id=file.file_id,
                        hunk_id=hunk.hunk_id,
                        path=file.display_path,
                        status=file.status,
                        old_start=hunk.old_start,
                        old_count=hunk.old_count,
                        new_start=hunk.new_start,
                        new_count=hunk.new_count,
                    )
                )
        else:
            units.append(
                ChangeUnit(
                    unit_id=_id("unit", diff_sha256, file.file_id, "metadata"),
                    file_id=file.file_id,
                    path=file.display_path,
                    status=file.status,
                )
            )
    return DiffIndex(
        diff_sha256=diff_sha256,
        files=files,
        change_units=units,
        parse_errors=errors,
    )
