"""快速 PR 审查的固定输入、运行身份与恢复校验。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.models.agent_task import AgentTask
from app.contracts.review_execution import (
    ArtifactRef,
    ReviewRunIdentity,
    build_config_fingerprint,
    sha256_bytes,
)
from app.infrastructure.persistence.review_artifacts import LocalReviewArtifactStore
from app.control_plane.execution_ownership import review_execution_ownership


class ReviewInputError(ValueError):
    """任务声明的输入无法固定或无法安全恢复。"""


@dataclass(frozen=True)
class PreparedReviewInput:
    identity: ReviewRunIdentity
    artifact_ref: ArtifactRef
    diff_bytes: bytes
    source_dir: str | None
    pr_url: str | None

    @property
    def diff_text(self) -> str:
        return self.diff_bytes.decode("utf-8", errors="replace")


async def _git(repository: Path, *args: str) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repository),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise ReviewInputError(f"Git 输入解析失败: {detail or 'unknown git error'}")
    return stdout


async def _fixed_git_input(scope: dict[str, Any]) -> tuple[bytes, str, str, str]:
    raw_repository = (
        scope.get("repository_path")
        or scope.get("source_dir")
        or scope.get("local_repository_path")
    )
    if not raw_repository:
        raise ReviewInputError("Git 输入必须提供本地 repository_path/source_dir")
    repository = Path(str(raw_repository)).resolve()
    if not repository.is_dir():
        raise ReviewInputError(f"Git 仓库目录不存在: {repository}")
    base_ref = str(scope.get("base_sha") or scope.get("base_ref") or "").strip()
    head_ref = str(scope.get("head_sha") or scope.get("head_ref") or "").strip()
    if not base_ref or not head_ref:
        raise ReviewInputError("Git 输入必须同时声明固定 base/head")
    base_sha = (await _git(repository, "rev-parse", "--verify", f"{base_ref}^{{commit}}"))
    head_sha = (await _git(repository, "rev-parse", "--verify", f"{head_ref}^{{commit}}"))
    base = base_sha.decode().strip().lower()
    head = head_sha.decode().strip().lower()
    diff = await _git(repository, "diff", "--binary", "--no-ext-diff", base, head)
    return diff, base, head, str(repository)


async def prepare_review_input(
    task: AgentTask,
    *,
    compatibility_config: dict[str, Any],
) -> tuple[ReviewRunIdentity, bytes, str | None, str | None]:
    """先固定真实输入，再构造与执行来源完全一致的身份。

    显式 diff 的优先级高于 URL/Git；Git 模式只接受本地仓库和可解析 commit，
    因而执行阶段不会再次按移动 ref 或远端 PR 获取不同内容。
    """

    scope = dict((task.audit_scope or {}).get("pr_review") or {})
    diff_path = scope.get("diff_file_path")
    source_dir: str | None = None
    if diff_path:
        path = Path(str(diff_path)).resolve()
        if not path.is_file():
            raise ReviewInputError(f"diff 输入不存在: {path}")
        diff_bytes = await asyncio.to_thread(path.read_bytes)
        source_kind = "diff"
        base_sha = scope.get("base_sha") or task.commit_sha
        head_sha = scope.get("head_sha")
    else:
        diff_bytes, base_sha, head_sha, source_dir = await _fixed_git_input(scope)
        source_kind = "git"
    repository_key = str(
        scope.get("repository_key")
        or source_dir
        or task.repository_url_snapshot
        or task.project_id
    )
    identity = ReviewRunIdentity(
        run_id=str(uuid4()),
        task_id=str(task.id),
        source_kind=source_kind,
        repository_key=repository_key,
        pr_number=scope.get("pr_number"),
        base_sha=base_sha,
        head_sha=head_sha,
        diff_sha256=sha256_bytes(diff_bytes),
        config_fingerprint=build_config_fingerprint(compatibility_config),
    )
    return identity, diff_bytes, source_dir, scope.get("pr_url")


async def initialize_or_resume_review_input(
    db,
    task: AgentTask,
    *,
    compatibility_config: dict[str, Any],
    artifact_root: str | Path,
    delivery_id: str,
) -> PreparedReviewInput:
    """幂等初始化身份+输入引用，或校验并加载既有不可变输入。"""

    candidate, candidate_bytes, source_dir, pr_url = await prepare_review_input(
        task, compatibility_config=compatibility_config
    )
    store = LocalReviewArtifactStore(artifact_root)
    persisted = await review_execution_ownership.load_identity(db, str(task.id))
    if persisted is None:
        reference = store.write_bytes(
            run_id=candidate.run_id,
            kind="input_diff",
            relative_path="input/review.diff",
            content=candidate_bytes,
            media_type="text/x-diff",
        )
        row = await review_execution_ownership.initialize(
            db, candidate, delivery_id=delivery_id, commit=False
        )
        identity = ReviewRunIdentity.model_validate(row.identity_json)
        config = dict(task.agent_config or {})
        config["review_execution_artifact_root"] = str(store.root)
        config["review_execution_input_artifact"] = reference.model_dump(mode="json")
        if source_dir:
            config["review_execution_source_dir"] = source_dir
        task.agent_config = config
        await db.commit()
        return PreparedReviewInput(identity, reference, candidate_bytes, source_dir, pr_url)

    identity = await review_execution_ownership.validate_resume_identity(db, candidate)
    config = dict(task.agent_config or {})
    raw_reference = config.get("review_execution_input_artifact")
    saved_root = config.get("review_execution_artifact_root")
    if not raw_reference or not saved_root:
        raise ReviewInputError("恢复任务缺少可信输入引用，请创建新任务")
    reference = ArtifactRef.model_validate(raw_reference)
    if reference.run_id != identity.run_id or reference.sha256 != identity.diff_sha256:
        raise ReviewInputError("恢复输入引用与运行身份不一致")
    diff_bytes = LocalReviewArtifactStore(saved_root).read_verified(reference)
    return PreparedReviewInput(
        identity,
        reference,
        diff_bytes,
        config.get("review_execution_source_dir") or source_dir,
        pr_url,
    )
