from __future__ import annotations

import asyncio
import subprocess

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.pipeline import DiffHunk, EvidencePackage, ReviewFinding
from app.domains.deep_review.services.diff_engine import find_hunk_for_new_line, parse_hunks
from app.domains.deep_review.services.directory_filter import DirectoryFilter, normalize_path
from app.domains.deep_review.services.input_builder import ReviewSnapshot
from app.domains.deep_review.services.git_runner import BoundedGitResult


class EvidenceError(RuntimeError):
    pass


# Git for Windows can stall when eight evidence subprocesses start at once.
# Cross may still inspect missing context with its four bounded read tools.
EVIDENCE_CONCURRENCY = 4

async def _run_git(
    repo_path: str,
    args: list[str],
    *,
    timeout: int,
    max_bytes: int,
) -> BoundedGitResult:
    # Git for Windows may leave asyncio pipe readers waiting after a large
    # grep is cancelled. A bounded synchronous subprocess in a worker thread
    # gives this deterministic stage a reliable timeout and reaps the child.
    def run() -> BoundedGitResult:
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, *args], capture_output=True,
                timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EvidenceError(str(exc)) from exc
        stdout = result.stdout[:max_bytes]
        stderr = result.stderr[:max_bytes]
        return BoundedGitResult(
            result.returncode,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
            len(result.stdout) > max_bytes or len(result.stderr) > max_bytes,
        )

    return await asyncio.to_thread(run)


async def _read_head(snapshot: ReviewSnapshot, path: str, config: DeepReviewConfig) -> str:
    # The size preflight keeps subprocess.run from buffering an oversized blob.
    size = await _run_git(
        snapshot.input.repo_path,
        ["cat-file", "-s", f"{snapshot.head_commit}:{path}"],
        timeout=config.tool_timeout_seconds,
        max_bytes=64,
    )
    if size.returncode != 0 or size.truncated:
        return ""
    try:
        if int(size.stdout.strip()) > config.max_file_bytes:
            return ""
    except ValueError:
        return ""
    result = await _run_git(
        snapshot.input.repo_path,
        ["show", f"{snapshot.head_commit}:{path}"],
        timeout=config.tool_timeout_seconds,
        max_bytes=config.max_file_bytes,
    )
    if result.returncode != 0 or result.truncated or "\x00" in result.stdout:
        return ""
    return _clip_bytes(result.stdout, config.max_file_bytes)


def _clip_bytes(value: str, limit: int) -> str:
    raw = value.encode("utf-8")
    return value if len(raw) <= limit else raw[:limit].decode("utf-8", errors="ignore")


def _primary_code(content: str, line: int | None, config: DeepReviewConfig) -> str:
    # No line means no trustworthy local excerpt. Line 1 is not a substitute
    # for locating a file-level or cross-file claim.
    if not content or line is None:
        return ""
    lines = content.splitlines()
    target = max(1, line)
    start = max(0, target - 16)
    end = min(len(lines), target + 15)
    numbered = [f"{index + 1}|{lines[index]}" for index in range(start, end)]
    return _clip_bytes("\n".join(numbered), max(1000, config.max_evidence_bytes_per_finding // 4))


def _matching_hunk(hunks: list[DiffHunk], line: int | None) -> str:
    matched = find_hunk_for_new_line(hunks, line)
    return matched.content if matched is not None else ""


def _fit_evidence_budget(
    package: EvidencePackage,
    *,
    finding_index: int,
    max_bytes: int,
) -> EvidencePackage:
    value = package.model_copy(deep=True)
    for _attempt in range(32):
        if len(value.model_dump_json().encode("utf-8")) <= max_bytes:
            return value
        value.truncated = True
        if value.caller_snippets:
            value.caller_snippets.pop()
            continue
        if value.related_code:
            value.related_code = _clip_bytes(
                value.related_code, max(0, len(value.related_code.encode("utf-8")) // 2)
            )
            continue
        if value.primary_code:
            value.primary_code = _clip_bytes(
                value.primary_code, max(0, len(value.primary_code.encode("utf-8")) // 2)
            )
            continue
        if value.diff_hunk:
            value.diff_hunk = _clip_bytes(
                value.diff_hunk, max(0, len(value.diff_hunk.encode("utf-8")) // 2)
            )
            continue
        if value.import_context:
            value.import_context = _clip_bytes(
                value.import_context, max(0, len(value.import_context.encode("utf-8")) // 2)
            )
            continue
        break
    return EvidencePackage(finding_index=finding_index, truncated=True)


async def build_evidence_packages(
    findings: list[ReviewFinding],
    snapshot: ReviewSnapshot,
    blast_radius: list[str],
    config: DeepReviewConfig,
) -> dict[int, EvidencePackage]:
    # Cross selectively reads callers/consumers with its fixed-head tools.
    # The old broad identifier search had no remaining consumer and could
    # stall large repositories before Cross even began.
    del blast_radius
    secret_filter = DirectoryFilter(config)
    hunks_by_path = {change.path: parse_hunks(change) for change in snapshot.changes}

    async def build(index: int, finding: ReviewFinding) -> EvidencePackage:
        try:
            path = normalize_path(finding.file_path)
            if path not in snapshot.allowed_paths or secret_filter.is_secret_path(path):
                return EvidencePackage(finding_index=index)
        except asyncio.CancelledError:
            raise
        except Exception:
            return EvidencePackage(finding_index=index)

        matched_hunk = _matching_hunk(hunks_by_path.get(path, []), finding.line_start)
        try:
            content = await _read_head(snapshot, path, config) if finding.line_start is not None else ""
        except asyncio.CancelledError:
            raise
        except Exception:
            content = ""
        primary = _primary_code(content, finding.line_start, config)
        hunk_limit = config.max_evidence_bytes_per_finding // 4
        package = EvidencePackage(
            finding_index=index,
            primary_code=primary,
            diff_hunk=_clip_bytes(matched_hunk, hunk_limit),
            truncated=len(matched_hunk.encode("utf-8")) > hunk_limit,
        )
        return _fit_evidence_budget(
            package,
            finding_index=index,
            max_bytes=config.max_evidence_bytes_per_finding,
        )

    semaphore = asyncio.Semaphore(EVIDENCE_CONCURRENCY)

    async def bounded_build(index: int, finding: ReviewFinding) -> EvidencePackage:
        async with semaphore:
            return await build(index, finding)

    packages = await asyncio.gather(
        *(bounded_build(index, finding) for index, finding in enumerate(findings))
    )
    return {package.finding_index: package for package in packages}
