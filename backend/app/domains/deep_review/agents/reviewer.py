from __future__ import annotations

import asyncio
import re
import subprocess
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.exc import SQLAlchemyError

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ChangeType
from app.domains.deep_review.schemas.pipeline import (
    ReviewDimension,
    ReviewFinding,
    ReviewerResult,
    SemanticBrief,
)
from app.domains.deep_review.services.directory_filter import FilterError, normalize_path
from app.domains.deep_review.services.input_builder import ReviewSnapshot
from app.domains.deep_review.services.output_formatter import build_reviewer_prompt
from app.domains.deep_review.services.prompt_loader import load_prompt
from app.domains.deep_review.services.reviewer_result_mapper import map_reviewer_result
from app.domains.deep_review.services.runtime import AgentCallResult
from app.domains.deep_review.tools.catalog import build_review_tools
from app.execution_plane.session.store import AuditSessionPersistenceError


ShortTag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


def _is_persistence_failure(exc: BaseException) -> bool:
    """Detect a typed transcript persistence failure wrapped by RuntimeBridge."""
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (AuditSessionPersistenceError, SQLAlchemyError)):
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _safe_error_message(exc: BaseException) -> str:
    """Keep a short diagnostic while removing common credential forms."""
    message = " ".join(str(exc).split()) or exc.__class__.__name__
    message = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [REDACTED]",
        message,
    )
    message = re.sub(
        r"(?i)\b(api[_-]?key|authorization|access[_-]?token|token)\s*[:=]\s*[^,;\s]+",
        r"\1=[REDACTED]",
        message,
    )
    message = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", message)
    return f"{exc.__class__.__name__}: {message}"[:500]


class ReviewFindingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(
        min_length=1,
        max_length=512,
        description="Repository-relative changed file; it must be owned by this dimension.",
    )
    line_start: int | None = Field(default=None, ge=1, description="Start line in the fixed head snapshot.")
    line_end: int | None = Field(default=None, ge=1, description="End line in the fixed head snapshot; may be omitted when line_start is set.")
    severity: Literal["critical", "high", "medium", "low"] = Field(
        description="Severity based on verified impact, not a fixed category mapping."
    )
    title: str = Field(min_length=5, max_length=160, description="Specific, actionable issue title.")
    body: str = Field(
        min_length=20,
        max_length=4000,
        description="Trigger condition, failure mechanism, and user or system consequence.",
    )
    evidence: str = Field(
        default="",
        max_length=3000,
        description="Code facts supporting the claim; do not invent unread implementation details.",
    )
    suggestion: str = Field(default="", max_length=2000, description="Optional repair direction.")
    confidence: float = Field(default=0.5, ge=0, le=1, description="Confidence that this Finding is correct.")
    tags: list[ShortTag] = Field(default_factory=list, max_length=8, description="A few retrieval tags.")


class ReviewerResultDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ReviewFindingDraft] = Field(
        default_factory=list,
        max_length=32,
        description="Zero or more actionable findings; there is no finding quota.",
    )
    summary: str = Field(default="", max_length=1000, description="Investigation conclusion for this dimension.")


@dataclass(slots=True)
class ReviewerAgentOutcome:
    call: AgentCallResult[ReviewerResult]
    diagnostics: list[str] = field(default_factory=list)
    rejected_count: int = 0

    @property
    def degraded(self) -> bool:
        return bool(self.rejected_count and self.call.value and not self.call.value.findings)


async def _head_line_count(
    snapshot: ReviewSnapshot,
    path: str,
    timeout: int,
    max_file_bytes: int,
) -> int | None:
    def read() -> int | None:
        try:
            tree = subprocess.run(
                [
                    "git",
                    "-C",
                    snapshot.input.repo_path,
                    "--literal-pathspecs",
                    "ls-tree",
                    "-r",
                    "-z",
                    snapshot.head_commit,
                    "--",
                    path,
                ],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if tree.returncode != 0:
                return None
            rows = [row for row in tree.stdout.split(b"\0") if row]
            matching = []
            for row in rows:
                metadata, separator, found_path = row.partition(b"\t")
                fields = metadata.split()
                if separator and found_path.decode("utf-8", "replace") == path and len(fields) == 3:
                    matching.append((fields[0], fields[1], fields[2]))
            if (
                len(matching) != 1
                or matching[0][0] not in {b"100644", b"100755"}
                or matching[0][1] != b"blob"
            ):
                return None
            content = subprocess.run(
                [
                    "git",
                    "-C",
                    snapshot.input.repo_path,
                    "cat-file",
                    "blob",
                    matching[0][2].decode("ascii"),
                ],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if (
                content.returncode != 0
                or len(content.stdout) > max_file_bytes
                or b"\x00" in content.stdout
            ):
                return None
            return len(content.stdout.decode("utf-8", "replace").splitlines())
        except (OSError, subprocess.TimeoutExpired):
            return None

    return await asyncio.to_thread(read)


async def _head_line_counts(
    snapshot: ReviewSnapshot,
    paths: list[str],
    config: DeepReviewConfig,
) -> dict[str, int | None]:
    values = await asyncio.gather(
        *(
            _head_line_count(
                snapshot,
                path,
                config.tool_timeout_seconds,
                config.max_file_bytes,
            )
            for path in paths
        )
    )
    return dict(zip(paths, values))


def _reported_target_paths(
    draft: ReviewerResultDraft,
    dimension: ReviewDimension,
    snapshot: ReviewSnapshot,
) -> list[str]:
    """Only inspect head files the model reported and the dimension owns."""
    target_files = set(dimension.target_files)
    deleted_paths = {
        change.path for change in snapshot.changes if change.change_type is ChangeType.DELETED
    }
    reported: set[str] = set()
    for finding in draft.findings:
        try:
            path = normalize_path(finding.file_path)
        except (FilterError, TypeError, ValueError):
            continue
        if path in target_files and path not in deleted_paths:
            reported.add(path)
    return [path for path in dimension.target_files if path in reported]


async def run_reviewer_agent(
    runtime_factory,
    *,
    snapshot: ReviewSnapshot,
    semantic: SemanticBrief,
    dimension: ReviewDimension,
    config: DeepReviewConfig,
) -> ReviewerAgentOutcome:
    harness_result = None
    try:
        runtime = runtime_factory.for_role(config.reviewer_role)
        prompt = build_reviewer_prompt(
            snapshot=snapshot,
            dimension=dimension,
            semantic=semantic,
        )
        system_prompt = load_prompt("reviewer_fallback" if dimension.fallback else "reviewer")
        harness_result = await runtime.harness(
            prompt,
            schema=ReviewerResultDraft,
            cwd=snapshot.input.repo_path,
            system_prompt=system_prompt,
            max_turns=config.reviewer_turn_limit(len(dimension.target_files)),
            tool_allowlist={"file_read", "file_read_diff", "file_find", "code_search"},
            tools=build_review_tools(
                repo_path=snapshot.input.repo_path,
                head_commit=snapshot.head_commit,
                snapshot=snapshot,
                config=config,
            ),
        )
        draft = ReviewerResultDraft.model_validate(harness_result.parsed)
        line_counts = await _head_line_counts(
            snapshot,
            _reported_target_paths(draft, dimension, snapshot),
            config,
        )
        mapped = map_reviewer_result(
            draft.model_dump(mode="json"),
            dimension=dimension,
            snapshot=snapshot,
            head_line_counts=line_counts,
        )
        return ReviewerAgentOutcome(
            call=AgentCallResult(
                value=mapped.result,
                session_id=harness_result.session_id,
                usage=harness_result.usage,
                cost_usd=harness_result.cost_usd,
            ),
            diagnostics=mapped.diagnostics,
            rejected_count=mapped.rejected_count,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if _is_persistence_failure(exc):
            raise
        return ReviewerAgentOutcome(
            call=AgentCallResult(
                value=None,
                error=_safe_error_message(exc),
                session_id=harness_result.session_id if harness_result is not None else getattr(exc, "session_id", None),
                usage=harness_result.usage if harness_result is not None else getattr(exc, "usage", None),
                cost_usd=harness_result.cost_usd if harness_result is not None else getattr(exc, "cost_usd", None),
            )
        )
