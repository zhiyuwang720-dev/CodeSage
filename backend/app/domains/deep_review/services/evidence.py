from __future__ import annotations

import asyncio
import re
import subprocess

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.pipeline import DiffHunk, EvidencePackage, ReviewFinding
from app.domains.deep_review.services.diff_engine import parse_hunks
from app.domains.deep_review.services.directory_filter import DirectoryFilter, FilterError, normalize_path
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
    if not content:
        return ""
    lines = content.splitlines()
    target = max(1, line or 1)
    start = max(0, target - 16)
    end = min(len(lines), target + 15)
    numbered = [f"{index + 1}|{lines[index]}" for index in range(start, end)]
    return _clip_bytes("\n".join(numbered), max(1000, config.max_evidence_bytes_per_finding // 4))


def _matching_hunk(hunks: list[DiffHunk], line: int | None) -> str:
    if not hunks:
        return ""
    if line is None:
        return hunks[0].content
    for hunk in hunks:
        start = hunk.new_start
        end = hunk.new_start + max(0, hunk.new_count - 1)
        if start <= line <= max(start, end):
            return hunk.content
    return hunks[0].content


def _identifiers(finding: ReviewFinding) -> list[str]:
    text = "\n".join((finding.title, finding.body, finding.evidence))
    quoted = re.findall(r"`([A-Za-z_][A-Za-z0-9_]{2,})`", text)
    called = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\(", text)
    candidates = [*quoted, *called]
    # These generic tokens made git grep scan very large repositories and did
    # not identify a meaningful caller. The Cross Harness can search a concrete
    # symbol itself when a candidate has no useful identifier here.
    common = {
        "the", "and", "for", "with", "return", "value", "error", "true", "false",
        "none", "nil", "len", "int", "str", "bool", "break", "continue", "range",
        "batch", "tensor", "position", "positions", "output", "outputs", "sequence",
        "sequences", "result", "data", "index", "line", "code", "check",
        "seqs", "func", "bytes", "hold", "window", "models", "append",
        "forward", "input", "inputs", "cache", "slice", "slices", "source",
        "call", "calls", "make", "type", "field", "file", "files", "empty",
        "length", "size", "count", "guard", "unsafe", "current", "other",
    }
    return list(dict.fromkeys(
        item for item in candidates
        if len(item) >= 6 and item.casefold() not in common
    ))[:6]


async def _search_head(
    snapshot: ReviewSnapshot,
    query: str,
    config: DeepReviewConfig,
    *,
    path_prefix: str | None = None,
) -> list[tuple[str, int, str]]:
    args = ["grep", "-n", "-I", "-F", "-m", "2", "-e", query, snapshot.head_commit]
    if path_prefix:
        args.extend(["--", path_prefix])
    result = await _run_git(
        snapshot.input.repo_path,
        args,
        timeout=config.tool_timeout_seconds,
        max_bytes=min(config.max_tool_output_bytes, 16_000),
    )
    output = result.stdout
    results: list[tuple[str, int, str]] = []
    for line in output.splitlines():
        try:
            without_ref = line.partition(":")[2]
            path, number_text, text = without_ref.split(":", 2)
            number = int(number_text)
        except (ValueError, IndexError):
            continue
        results.append((path, number, text))
        if len(results) >= config.max_search_results:
            break
    return results


async def _caller_snippets(
    snapshot: ReviewSnapshot,
    finding: ReviewFinding,
    config: DeepReviewConfig,
) -> list[str]:
    snippets: list[str] = []
    secret_filter = DirectoryFilter(config)
    per_snippet = max(250, config.max_evidence_bytes_per_finding // 8)
    for identifier in _identifiers(finding)[:3]:
        for raw_path, number, _text in await _search_head(snapshot, identifier, config):
            try:
                path = normalize_path(raw_path)
            except FilterError:
                continue
            if path == finding.file_path or secret_filter.is_secret_path(path):
                continue
            content = await _read_head(snapshot, path, config)
            if not content:
                continue
            lines = content.splitlines()
            start = max(0, number - 4)
            end = min(len(lines), number + 3)
            body = "\n".join(f"{index + 1}|{lines[index]}" for index in range(start, end))
            snippets.append(f"{path}:{number}\n{_clip_bytes(body, per_snippet)}")
            break
        if len(snippets) >= 2:
            break
    return snippets


def _import_context(content: str) -> str:
    imports = [line.strip() for line in content.splitlines() if line.strip().startswith(("import ", "from "))]
    return "IMPORTS: " + (", ".join(imports[:30]) or "none")


async def _related_code(
    snapshot: ReviewSnapshot,
    finding: ReviewFinding,
    blast_radius: list[str],
    identifiers: list[str],
    config: DeepReviewConfig,
) -> str:
    chunks: list[str] = []
    secret_filter = DirectoryFilter(config)
    per_chunk = max(250, config.max_evidence_bytes_per_finding // 8)
    for path in blast_radius:
        if path == finding.file_path:
            continue
        if secret_filter.is_secret_path(path):
            continue
        content = await _read_head(snapshot, path, config)
        if not content:
            continue
        lines = content.splitlines()
        for identifier in identifiers:
            number = next((index for index, line in enumerate(lines) if identifier in line), None)
            if number is None:
                continue
            start = max(0, number - 5)
            end = min(len(lines), number + 6)
            body = "\n".join(f"{index + 1}|{lines[index]}" for index in range(start, end))
            chunks.append(f"{path}:{number + 1}\n{_clip_bytes(body, per_chunk)}")
            break
        if len(chunks) >= 5:
            break
    return "\n\n".join(chunks)


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
    secret_filter = DirectoryFilter(config)
    hunks_by_path = {change.path: parse_hunks(change) for change in snapshot.changes}

    async def build(index: int, finding: ReviewFinding) -> EvidencePackage:
        try:
            path = normalize_path(finding.file_path)
            if path not in snapshot.allowed_paths or secret_filter.is_secret_path(path):
                return EvidencePackage(finding_index=index)

            content = await _read_head(snapshot, path, config)
            identifiers = _identifiers(finding)
            callers = await _caller_snippets(snapshot, finding, config)
            related = await _related_code(snapshot, finding, blast_radius, identifiers, config)
        except asyncio.CancelledError:
            raise
        except Exception:
            return EvidencePackage(finding_index=index)

        matched_hunk = _matching_hunk(hunks_by_path.get(path, []), finding.line_start)
        primary = _primary_code(content, finding.line_start, config)
        hunk_limit = config.max_evidence_bytes_per_finding // 4
        package = EvidencePackage(
            finding_index=index,
            primary_code=primary,
            caller_snippets=callers,
            cross_ref_snippets=[],
            diff_hunk=_clip_bytes(matched_hunk, hunk_limit),
            import_context=_clip_bytes(_import_context(content), 1000),
            related_code=related,
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
