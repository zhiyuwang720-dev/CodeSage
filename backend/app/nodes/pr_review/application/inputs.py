"""快速 PR 审查的固定输入、运行身份与恢复校验。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.models.agent_task import AgentTask
from app.nodes.pr_review.contracts.review_execution import (
    ArtifactRef,
    ReviewRunIdentity,
    build_config_fingerprint,
    sha256_bytes,
)
from app.nodes.pr_review.contracts.review_context import (
    CONTEXT_POLICY_VERSION,
    DIFF_PARSER_VERSION,
    TOOL_PROFILE_VERSION,
    RepositorySnapshotRef,
    ReviewCapabilities,
    ReviewMode,
)
from app.infrastructure.repositories.snapshots import (
    GitSnapshotReader,
    SnapshotError,
    create_snapshot_ref,
    run_git,
)
from app.infrastructure.persistence.review_artifacts import LocalReviewArtifactStore
from app.nodes.pr_review.persistence.execution_identity import (
    review_execution_identity_store,
)


class ReviewInputError(ValueError):
    """任务声明的输入无法固定或无法安全恢复。"""

    def __init__(self, message: str, *, code: str = "input_invalid"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreparedReviewInput:
    identity: ReviewRunIdentity
    artifact_ref: ArtifactRef
    diff_bytes: bytes
    source_dir: str | None
    pr_url: str | None
    review_mode: ReviewMode = "diff_only"
    snapshot_ref: RepositorySnapshotRef | None = None

    @property
    def diff_text(self) -> str:
        return self.diff_bytes.decode("utf-8", errors="replace")


async def _fixed_git_input(
    scope: dict[str, Any], *, repository_key: str
) -> tuple[bytes, str, str, str, RepositorySnapshotRef]:
    raw_repository = (
        scope.get("repository_path")
        or scope.get("source_dir")
        or scope.get("local_repository_path")
    )
    if not raw_repository:
        raise ReviewInputError(
            "repository_required 输入必须提供 worker 可达的 repository_path/source_dir",
            code="source_unavailable",
        )
    repository = Path(str(raw_repository)).resolve()
    if not repository.is_dir():
        raise ReviewInputError(
            f"Git 仓库目录不存在: {repository}", code="source_unavailable"
        )
    base_ref = str(scope.get("base_sha") or scope.get("base_ref") or "").strip()
    head_ref = str(scope.get("head_sha") or scope.get("head_ref") or "").strip()
    if not base_ref or not head_ref:
        raise ReviewInputError("Git 输入必须同时声明固定 base/head")
    basis = str(scope.get("diff_basis") or "two_dot")
    if basis not in {"two_dot", "merge_base", "provided_patch"}:
        raise ReviewInputError(f"不支持的 diff_basis: {basis}")
    snapshot = await create_snapshot_ref(
        repository,
        repository_key=repository_key,
        base_revision=base_ref,
        head_revision=head_ref,
        diff_basis="merge_base" if basis == "merge_base" else "two_dot",
    )
    diff = await run_git(
        repository,
        "diff",
        "--binary",
        "--no-ext-diff",
        snapshot.effective_base_sha,
        snapshot.head_sha,
    )
    return (
        diff,
        snapshot.effective_base_sha,
        snapshot.head_sha,
        str(repository),
        snapshot,
    )


def _canonical_patch(content: bytes) -> str:
    return content.decode("utf-8", errors="replace").replace("\r\n", "\n").rstrip("\n")


def _requested_mode(scope: dict[str, Any], *, has_source: bool) -> ReviewMode:
    explicit = scope.get("review_mode")
    if explicit not in (None, "diff_only", "repository_required"):
        raise ReviewInputError("review_mode 必须是 diff_only 或 repository_required")
    mode: ReviewMode = explicit or ("repository_required" if has_source else "diff_only")
    if mode == "diff_only" and has_source:
        raise ReviewInputError("diff_only 与源码声明冲突；请移除源码参数或改为 repository_required")
    return mode


@dataclass(frozen=True)
class _PreparedCandidate:
    identity: ReviewRunIdentity
    diff_bytes: bytes
    source_dir: str | None
    pr_url: str | None
    review_mode: ReviewMode
    snapshot_ref: RepositorySnapshotRef | None


async def _prepare_candidate(
    task: AgentTask, *, compatibility_config: dict[str, Any]
) -> _PreparedCandidate:
    scope = dict((task.audit_scope or {}).get("pr_review") or {})
    raw_source = scope.get("repository_path") or scope.get("source_dir") or scope.get("local_repository_path")
    has_source = bool(raw_source or scope.get("clone_source"))
    mode = _requested_mode(scope, has_source=has_source)
    repository_key = str(
        scope.get("repository_key")
        or task.repository_url_snapshot
        or task.project_id
    )
    diff_path = scope.get("diff_file_path")
    source_dir: str | None = None
    snapshot: RepositorySnapshotRef | None = None
    if diff_path:
        path = Path(str(diff_path)).resolve()
        if not path.is_file():
            raise ReviewInputError(f"diff 输入不存在: {path}", code="input_corrupted")
        diff_bytes = await asyncio.to_thread(path.read_bytes)
        source_kind = "diff"
        base_sha = scope.get("base_sha") or task.commit_sha
        head_sha = scope.get("head_sha")
        if mode == "repository_required":
            generated, base_sha, head_sha, source_dir, snapshot = await _fixed_git_input(
                scope, repository_key=repository_key
            )
            if _canonical_patch(generated) != _canonical_patch(diff_bytes):
                raise ReviewInputError(
                    "provided patch 与固定 base/head 的 Git diff 不一致",
                    code="source_diff_mismatch",
                )
    else:
        if scope.get("pr_url") and not raw_source:
            raise ReviewInputError(
                "PR URL 尚未解析为固定源码快照；请先由可信获取层准备 repository_path/base/head",
                code="source_unavailable",
            )
        diff_bytes, base_sha, head_sha, source_dir, snapshot = await _fixed_git_input(
            scope, repository_key=repository_key
        )
        source_kind = "git"
        mode = "repository_required"
    if not diff_bytes.strip():
        raise ReviewInputError("diff 输入为空", code="input_invalid")
    compatibility = {
        **compatibility_config,
        "context_policy_version": CONTEXT_POLICY_VERSION,
        "tool_profile_version": TOOL_PROFILE_VERSION,
        "diff_parser_version": DIFF_PARSER_VERSION,
        "review_mode": mode,
    }
    identity = ReviewRunIdentity(
        run_id=str(uuid4()),
        task_id=str(task.id),
        source_kind=source_kind,
        repository_key=repository_key,
        pr_number=scope.get("pr_number"),
        base_sha=base_sha,
        head_sha=head_sha,
        diff_sha256=sha256_bytes(diff_bytes),
        config_fingerprint=build_config_fingerprint(compatibility),
    )
    return _PreparedCandidate(
        identity=identity,
        diff_bytes=diff_bytes,
        source_dir=source_dir,
        pr_url=scope.get("pr_url"),
        review_mode=mode,
        snapshot_ref=snapshot,
    )


async def prepare_review_input(
    task: AgentTask,
    *,
    compatibility_config: dict[str, Any],
) -> tuple[ReviewRunIdentity, bytes, str | None, str | None]:
    """先固定真实输入，再构造与执行来源完全一致的身份。

    显式 diff 的优先级高于 URL/Git；Git 模式只接受本地仓库和可解析 commit，
    因而执行阶段不会再次按移动 ref 或远端 PR 获取不同内容。
    """

    candidate = await _prepare_candidate(task, compatibility_config=compatibility_config)
    return (
        candidate.identity,
        candidate.diff_bytes,
        candidate.source_dir,
        candidate.pr_url,
    )


async def preflight_review_input(
    prepared: PreparedReviewInput,
    *,
    execution_attempt_id: str,
    worker_id: str,
) -> ReviewCapabilities:
    common = ["list_changes", "read_diff", "search_diff", "finalize_review"]
    if prepared.review_mode == "diff_only":
        return ReviewCapabilities(
            run_id=prepared.identity.run_id,
            execution_attempt_id=execution_attempt_id,
            mode="diff_only",
            source_status="not_requested",
            capabilities=common,
            limitations=["审查仅覆盖提供的 diff；未核实未修改源码与跨文件调用方"],
            worker_id=worker_id,
        )
    if not prepared.source_dir or prepared.snapshot_ref is None:
        return ReviewCapabilities(
            run_id=prepared.identity.run_id,
            execution_attempt_id=execution_attempt_id,
            mode="repository_required",
            source_status="unavailable",
            reason_code="source_unavailable",
            limitations=["固定源码快照不可用"],
            worker_id=worker_id,
        )
    try:
        await GitSnapshotReader(Path(prepared.source_dir), prepared.snapshot_ref).verify()
    except SnapshotError as exc:
        return ReviewCapabilities(
            run_id=prepared.identity.run_id,
            execution_attempt_id=execution_attempt_id,
            mode="repository_required",
            source_status="unavailable",
            snapshot_id=prepared.snapshot_ref.snapshot_id,
            reason_code=exc.code,
            limitations=[str(exc)],
            worker_id=worker_id,
        )
    return ReviewCapabilities(
        run_id=prepared.identity.run_id,
        execution_attempt_id=execution_attempt_id,
        mode="repository_required",
        source_status="available",
        snapshot_id=prepared.snapshot_ref.snapshot_id,
        capabilities=[*common, "read_source", "search_source"],
        worker_id=worker_id,
    )


async def initialize_or_resume_review_input(
    db,
    task: AgentTask,
    *,
    compatibility_config: dict[str, Any],
    artifact_root: str | Path,
    delivery_id: str,
) -> PreparedReviewInput:
    """幂等初始化身份+输入引用，或校验并加载既有不可变输入。"""

    prepared_candidate = await _prepare_candidate(
        task, compatibility_config=compatibility_config
    )
    candidate = prepared_candidate.identity
    candidate_bytes = prepared_candidate.diff_bytes
    source_dir = prepared_candidate.source_dir
    pr_url = prepared_candidate.pr_url
    store = LocalReviewArtifactStore(artifact_root)
    persisted = await review_execution_identity_store.load(db, str(task.id))
    if persisted is None:
        reference = store.write_bytes(
            run_id=candidate.run_id,
            kind="input_diff",
            relative_path="input/review.diff",
            content=candidate_bytes,
            media_type="text/x-diff",
        )
        row = await review_execution_identity_store.initialize(
            db, candidate, delivery_id=delivery_id, commit=False
        )
        identity = ReviewRunIdentity.model_validate(row.identity_json)
        config = dict(task.agent_config or {})
        config["review_execution_artifact_root"] = str(store.root)
        config["review_execution_input_artifact"] = reference.model_dump(mode="json")
        if source_dir:
            config["review_execution_source_dir"] = source_dir
        config["review_execution_mode"] = prepared_candidate.review_mode
        if prepared_candidate.snapshot_ref is not None:
            config["review_execution_snapshot"] = prepared_candidate.snapshot_ref.model_dump(mode="json")
        task.agent_config = config
        await db.commit()
        return PreparedReviewInput(
            identity,
            reference,
            candidate_bytes,
            source_dir,
            pr_url,
            prepared_candidate.review_mode,
            prepared_candidate.snapshot_ref,
        )

    identity = await review_execution_identity_store.validate_resume(db, candidate)
    config = dict(task.agent_config or {})
    raw_reference = config.get("review_execution_input_artifact")
    saved_root = config.get("review_execution_artifact_root")
    if not raw_reference or not saved_root:
        raise ReviewInputError("恢复任务缺少可信输入引用，请创建新任务")
    reference = ArtifactRef.model_validate(raw_reference)
    if reference.run_id != identity.run_id or reference.sha256 != identity.diff_sha256:
        raise ReviewInputError("恢复输入引用与运行身份不一致")
    diff_bytes = LocalReviewArtifactStore(saved_root).read_verified(reference)
    raw_snapshot = config.get("review_execution_snapshot")
    snapshot = RepositorySnapshotRef.model_validate(raw_snapshot) if raw_snapshot else None
    return PreparedReviewInput(
        identity,
        reference,
        diff_bytes,
        config.get("review_execution_source_dir") or source_dir,
        pr_url,
        config.get("review_execution_mode") or prepared_candidate.review_mode,
        snapshot or prepared_candidate.snapshot_ref,
    )
