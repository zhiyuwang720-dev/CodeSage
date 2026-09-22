from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ChangeType, FileChange, ReviewInput
from .directory_filter import DirectoryFilter, FilterResult, normalize_path


class ReviewInputError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewSnapshot:
    input: ReviewInput
    base_commit: str
    head_commit: str
    merge_base: str
    commit_messages: list[str]
    changes: list[FileChange]
    diff_by_path: dict[str, str]
    filter_result: FilterResult

    @property
    def review_paths(self) -> list[str]:
        return self.filter_result.review_paths

    @property
    def allowed_paths(self) -> set[str]:
        return {*self.filter_result.review_paths, *self.filter_result.context_paths}


def _run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReviewInputError(f"git command failed: {' '.join(args)}: {exc}") from exc
    if check and result.returncode != 0:
        raise ReviewInputError(result.stderr.strip() or f"git command failed: {' '.join(args)}")
    return result


def _resolve_commit(repo: Path, ref: str) -> str:
    if not ref.strip() or ref.strip().startswith("-"):
        raise ReviewInputError(f"invalid ref: {ref}")
    result = _run_git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    value = result.stdout.strip()
    if not value:
        raise ReviewInputError(f"ref not found: {ref}")
    return value


def _parse_name_status_null(value: str) -> dict[str, tuple[ChangeType, str | None]]:
    result: dict[str, tuple[ChangeType, str | None]] = {}
    fields = [item for item in value.split("\0") if item]
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if status.startswith(("R", "C")):
            if index + 1 >= len(fields):
                break
            old, new = fields[index], fields[index + 1]
            index += 2
            result[new] = (ChangeType.RENAMED, old)
        else:
            if index >= len(fields):
                break
            path = fields[index]
            index += 1
            kind = {
                "A": ChangeType.ADDED,
                "D": ChangeType.DELETED,
            }.get(status, ChangeType.MODIFIED)
            result[path] = (kind, None)
    return result


def _parse_numstat_null(
    value: str, rename_old_by_new: dict[str, str | None]
) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for line in value.splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        raw_added, raw_deleted = fields[0], fields[1]
        raw_path = fields[-1]
        if "=>" in raw_path:
            brace_match = re.match(r"^(?P<prefix>[^{}]*)\{(?P<old>[^{}]*) => (?P<new>[^{}]*)\}(?P<suffix>[^{}]*)$", raw_path)
            if brace_match:
                old_path = brace_match.group("prefix") + brace_match.group("old") + brace_match.group("suffix")
                path = brace_match.group("prefix") + brace_match.group("new") + brace_match.group("suffix")
            else:
                old_path, path = raw_path.rsplit("=>", 1)
                old_path, path = old_path.strip(), path.strip()
        else:
            path = raw_path
        additions = 0 if raw_added == "-" else int(raw_added)
        deletions = 0 if raw_deleted == "-" else int(raw_deleted)
        result[path] = (additions, deletions)
    return result


def _split_diff(diff_text: str) -> tuple[dict[str, str], dict[str, ChangeType]]:
    patches: dict[str, str] = {}
    kinds: dict[str, ChangeType] = {}
    current: list[str] = []

    def finish() -> None:
        if not current:
            return
        text = "\n".join(current).rstrip() + "\n"
        header_old = header_new = ""
        header_match = re.match(r"^diff --git a/(.+?) b/(.+)$", current[0])
        if header_match:
            header_old, header_new = header_match.group(1), header_match.group(2)
        old_path = new_path = ""
        for line in current:
            if line.startswith("--- a/"):
                old_path = line[6:].split("\t", 1)[0]
            elif line.startswith("+++ b/"):
                new_path = line[6:].split("\t", 1)[0]
            elif line.startswith("--- /dev/null"):
                old_path = "/dev/null"
            elif line.startswith("+++ /dev/null"):
                new_path = "/dev/null"
        path = new_path if new_path not in {"", "/dev/null"} else (
            old_path if old_path not in {"", "/dev/null"} else (
                header_new if header_new not in {"", "/dev/null"} else header_old
            )
        )
        if path in {"", "/dev/null"}:
            return
        kind = ChangeType.MODIFIED
        if "/dev/null" in {old_path}:
            kind = ChangeType.ADDED
        elif "/dev/null" in {new_path}:
            kind = ChangeType.DELETED
        elif "GIT binary patch" in text or "Binary files " in text:
            kind = ChangeType.BINARY
        elif any("rename from " in line for line in current):
            kind = ChangeType.RENAMED
        normalized = normalize_path(path)
        patches[normalized] = text
        kinds[normalized] = kind

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            finish()
            current = [line]
        elif current:
            current.append(line)
    finish()
    return patches, kinds


def build_review_snapshot(review_input: ReviewInput, config: DeepReviewConfig) -> ReviewSnapshot:
    repo = Path(review_input.repo_path).expanduser().resolve()
    if not repo.is_dir():
        raise ReviewInputError(f"repository not found: {repo}")
    is_worktree = _run_git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    if is_worktree.returncode != 0 or is_worktree.stdout.strip() != "true":
        raise ReviewInputError(f"not a git worktree: {repo}")

    base_commit = _resolve_commit(repo, review_input.base_ref)
    head_commit = _resolve_commit(repo, review_input.head_ref)
    merge_base = _run_git(repo, "merge-base", base_commit, head_commit).stdout.strip()
    range_value = f"{merge_base}..{head_commit}"

    name_status = _parse_name_status_null(
        _run_git(repo, "diff", "--name-status", "-z", "--find-renames", range_value).stdout
    )
    numstat = _parse_numstat_null(
        _run_git(repo, "diff", "--numstat", "--find-renames", range_value).stdout
        ,
        {new: old for new, (_kind, old) in name_status.items() if old is not None},
    )
    raw_diff = _run_git(repo, "diff", "--no-color", "--find-renames", range_value).stdout
    if len(raw_diff.encode("utf-8")) > config.max_diff_bytes:
        raise ReviewInputError(
            f"diff exceeds max_diff_bytes: {len(raw_diff.encode('utf-8'))} > {config.max_diff_bytes}"
        )
    patches, diff_kinds = _split_diff(raw_diff)
    commit_messages = [
        line
        for line in _run_git(repo, "log", "--format=%s", range_value).stdout.splitlines()
        if line.strip()
    ]

    changes: list[FileChange] = []
    for path, (kind, old_path) in name_status.items():
        normalized = normalize_path(path)
        patch = patches.get(normalized, "")
        if not patch and kind is not ChangeType.DELETED:
            kind = diff_kinds.get(normalized, kind)
        elif "GIT binary patch" in patch or "Binary files " in patch:
            kind = ChangeType.BINARY
        additions, deletions = numstat.get(path, (0, 0))
        changes.append(
            FileChange(
                path=normalized,
                old_path=old_path,
                change_type=kind,
                diff=patch,
                additions=additions,
                deletions=deletions,
            )
        )

    snapshot_input = review_input.model_copy(update={"commit_messages": commit_messages})
    return ReviewSnapshot(
        input=snapshot_input,
        base_commit=base_commit,
        head_commit=head_commit,
        merge_base=merge_base,
        commit_messages=commit_messages,
        changes=changes,
        diff_by_path=patches,
        filter_result=DirectoryFilter(config).filter(changes),
    )
