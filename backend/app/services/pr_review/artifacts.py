"""快速审查的最小本地产物存储。"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from app.services.contracts.review_execution import ArtifactRef, sha256_bytes


class ArtifactIntegrityError(ValueError):
    """产物路径或内容完整性校验失败。"""


class LocalReviewArtifactStore:
    def __init__(self, artifact_root: str | Path):
        self.root = Path(artifact_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _run_root(self, run_id: str, *, create: bool) -> Path:
        if not run_id or run_id in (".", "..") or any(c in run_id for c in "/\\:"):
            raise ArtifactIntegrityError("run_id 不能包含路径分隔符")
        path = self.root / run_id
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def _resolve(self, run_id: str, relative_path: str, *, create_parent: bool) -> Path:
        # 先借契约完成跨平台绝对路径、盘符和 traversal 校验。
        placeholder = ArtifactRef(
            artifact_id="validation",
            run_id=run_id,
            kind="validation",
            relative_path=relative_path,
            sha256="0" * 64,
            size_bytes=0,
            media_type="application/octet-stream",
        )
        run_root = self._run_root(run_id, create=create_parent)
        candidate = run_root.joinpath(*placeholder.relative_path.split("/"))
        current = run_root
        for part in placeholder.relative_path.split("/"):
            current = current / part
            if current.exists() and current.is_symlink():
                raise ArtifactIntegrityError(f"产物路径不允许符号链接: {relative_path}")
        if create_parent:
            candidate.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = candidate.parent.resolve()
        try:
            resolved_parent.relative_to(run_root)
        except ValueError as exc:
            raise ArtifactIntegrityError("产物路径越过 run 根目录") from exc
        return candidate

    def write_bytes(
        self,
        *,
        run_id: str,
        kind: str,
        relative_path: str,
        content: bytes,
        media_type: str,
    ) -> ArtifactRef:
        target = self._resolve(run_id, relative_path, create_parent=True)
        temp = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temp.write_bytes(content)
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink()
        return ArtifactRef(
            artifact_id=str(uuid4()),
            run_id=run_id,
            kind=kind,
            relative_path=relative_path,
            sha256=sha256_bytes(content),
            size_bytes=len(content),
            media_type=media_type,
        )

    def read_verified(self, reference: ArtifactRef) -> bytes:
        target = self._resolve(reference.run_id, reference.relative_path, create_parent=False)
        if not target.is_file():
            raise ArtifactIntegrityError(f"产物不存在: {reference.relative_path}")
        content = target.read_bytes()
        if len(content) != reference.size_bytes:
            raise ArtifactIntegrityError(f"产物大小不匹配: {reference.relative_path}")
        if sha256_bytes(content) != reference.sha256:
            raise ArtifactIntegrityError(f"产物哈希不匹配: {reference.relative_path}")
        return content

