"""Bounded, redacted diagnostic content storage.

The store is deliberately independent from business transactions. Capture
failure downgrades diagnostic completeness but never rolls back a review.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

import os
from contextlib import contextmanager
from opentelemetry.trace import Span

from app.nodes.pr_review.contracts.review_execution import ArtifactRef
from app.core.config import settings
from app.infrastructure.observability.privacy import redact_text, sanitize
from app.infrastructure.observability.tracing import get_observability_context
from app.infrastructure.persistence.review_artifacts import LocalReviewArtifactStore

logger = logging.getLogger(__name__)

REDACTION_VERSION = "2"
CAPTURE_STATUS_CAPTURED = "captured"
CAPTURE_STATUS_TRUNCATED = "truncated"
CAPTURE_STATUS_MISSING = "missing"
CAPTURE_STATUS_DISABLED = "disabled"


@contextmanager
def _file_lock(path: Path):
    handle = path.open("a+")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()



@dataclass(frozen=True)
class ContentCapture:
    status: str
    preview: str | None
    preview_truncated: bool
    original_redacted_bytes: int
    stored_bytes: int
    artifact: ArtifactRef | None
    reason: str | None = None


class DiagnosticContentStore:
    def __init__(
        self,
        root: str | Path,
        *,
        enabled: bool = False,
        preview_bytes: int = 32768,
        file_limit_bytes: int = 16 * 1024 * 1024,
        run_limit_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        self.enabled = bool(enabled)
        self.preview_bytes = max(0, int(preview_bytes))
        self.file_limit_bytes = max(0, int(file_limit_bytes))
        self.run_limit_bytes = max(0, int(run_limit_bytes))
        self._local = LocalReviewArtifactStore(root)
        self.root = self._local.root
        self._lock = threading.Lock()

    def _run_bytes(self, run_id: str) -> int:
        run_root = self.root / run_id
        if not run_root.exists():
            return 0
        return sum(             path.stat().st_size             for path in run_root.rglob("*")             if path.is_file() and not path.name.endswith(".meta.json") and path.name != ".quota.lock"         )

    def _serialize(self, content: Any, media_type: str) -> str:
        if isinstance(content, bytes):
            text = content.decode("utf-8", errors="replace")
        elif isinstance(content, str):
            text = content
        else:
            text = json.dumps(sanitize(content), ensure_ascii=False, sort_keys=True)
        redacted, _ = redact_text(text, max_bytes=max(self.file_limit_bytes, 1))
        return redacted

    def capture(
        self,
        *,
        run_id: str,
        kind: str,
        content: Any,
        media_type: str = "application/json",
    ) -> ContentCapture:
        text = self._serialize(content, media_type)
        original_size = len(text.encode("utf-8"))
        if not self.enabled:
            return ContentCapture(
                status=CAPTURE_STATUS_DISABLED,
                preview=None,
                preview_truncated=False,
                original_redacted_bytes=original_size,
                stored_bytes=0,
                artifact=None,
                reason="capture_disabled",
            )

        with self._lock:
            with _file_lock(self.root / ".quota.lock"):
                current_run_bytes = self._run_bytes(run_id)
                remaining_run = max(0, self.run_limit_bytes - current_run_bytes)
                allowed = min(self.file_limit_bytes, remaining_run)
                if allowed <= 0:
                    return ContentCapture(
                        status=CAPTURE_STATUS_MISSING,
                        preview=None,
                        preview_truncated=False,
                        original_redacted_bytes=original_size,
                        stored_bytes=0,
                        artifact=None,
                        reason="run_quota_exceeded",
                    )
                encoded = text.encode("utf-8")
                truncated = len(encoded) > allowed
                reason = None
                if truncated:
                    reason = "run_quota" if remaining_run < self.file_limit_bytes else "file_limit"
                    text = encoded[:allowed].decode("utf-8", errors="ignore")
                preview_bytes = text.encode("utf-8")[: self.preview_bytes]
                preview_truncated = len(preview_bytes) < len(text.encode("utf-8"))
                preview = preview_bytes.decode("utf-8", errors="ignore")
                safe_kind = "".join(ch for ch in kind if ch.isalnum() or ch in "-_") or "content"
                artifact_id = uuid4().hex
                relative_path = f"{safe_kind}/{artifact_id}.json"
                try:
                    artifact = self._local.write_bytes(
                        run_id=run_id,
                        kind=kind,
                        relative_path=relative_path,
                        content=text.encode("utf-8"),
                        media_type=media_type,
                    )
                    artifact = artifact.model_copy(update={"artifact_id": artifact_id})
                    meta_relative_path = f"{safe_kind}/{artifact_id}.meta.json"
                    self._local.write_bytes(
                        run_id=run_id,
                        kind=f"{kind}_meta",
                        relative_path=meta_relative_path,
                        content=json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False).encode("utf-8"),
                        media_type="application/json",
                    )
                except Exception:
                    logger.warning("diagnostic content write failed", exc_info=True)
                    return ContentCapture(
                        status=CAPTURE_STATUS_MISSING,
                        preview=preview or None,
                        preview_truncated=preview_truncated,
                        original_redacted_bytes=original_size,
                        stored_bytes=0,
                        artifact=None,
                        reason="write_failed",
                    )
                return ContentCapture(
                    status=CAPTURE_STATUS_TRUNCATED if truncated else CAPTURE_STATUS_CAPTURED,
                    preview=preview,
                    preview_truncated=preview_truncated,
                    original_redacted_bytes=original_size,
                    stored_bytes=len(text.encode("utf-8")),
                    artifact=artifact,
                    reason=reason,
                )

    def find_artifact(self, run_id: str, artifact_id: str) -> ArtifactRef | None:
        run_root = self.root / run_id
        if not run_root.exists():
            return None
        matches = list(run_root.rglob(f"{artifact_id}.meta.json"))
        if len(matches) != 1:
            return None
        payload = json.loads(matches[0].read_text(encoding="utf-8"))
        try:
            return ArtifactRef.model_validate(payload)
        except Exception:
            return None

    def read_verified(self, reference: ArtifactRef) -> bytes:
        return self._local.read_verified(reference)


_store: DiagnosticContentStore | None = None
_store_lock = threading.RLock()


def configure_content_store(
    *,
    root: str | Path | None = None,
    enabled: bool | None = None,
    preview_bytes: int | None = None,
    file_limit_bytes: int | None = None,
    run_limit_bytes: int | None = None,
) -> DiagnosticContentStore:
    global _store
    configured_root = Path(root or settings.OTEL_CONTENT_ROOT)
    if not configured_root.is_absolute():
        configured_root = Path.cwd() / configured_root
    with _store_lock:
        _store = DiagnosticContentStore(
            configured_root,
            enabled=settings.OTEL_CAPTURE_CONTENT if enabled is None else enabled,
            preview_bytes=settings.OTEL_CONTENT_PREVIEW_BYTES if preview_bytes is None else preview_bytes,
            file_limit_bytes=(
                settings.OTEL_CONTENT_FILE_LIMIT_BYTES if file_limit_bytes is None else file_limit_bytes
            ),
            run_limit_bytes=settings.OTEL_CONTENT_RUN_LIMIT_BYTES if run_limit_bytes is None else run_limit_bytes,
        )
        return _store


def get_content_store() -> DiagnosticContentStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = configure_content_store()
    return _store


def reset_content_store() -> None:
    global _store
    with _store_lock:
        _store = None


def capture_to_span(
    span: Span,
    *,
    kind: str,
    content: Any,
    media_type: str = "application/json",
    value_attribute: str | None = None,
    extra_attributes: Mapping[str, Any] | None = None,
) -> ContentCapture | None:
    """Capture content and project only bounded preview/source metadata to a span."""

    context = get_observability_context()
    run_id = str(context.get("review_run_id") or context.get("run_id") or "") or None
    prefix = f"codesage.{kind}"
    if extra_attributes:
        for key, value in extra_attributes.items():
            if isinstance(value, (str, bool, int, float)):
                span.set_attribute(f"{prefix}.{key}", value)
    if not run_id:
        span.set_attribute(f"{prefix}.capture_status", CAPTURE_STATUS_MISSING)
        span.set_attribute(f"{prefix}.reason", "missing_run_id")
        return None
    try:
        result = get_content_store().capture(run_id=run_id, kind=kind, content=content, media_type=media_type)
    except Exception:
        logger.warning("diagnostic content capture failed", exc_info=True)
        span.set_attribute(f"{prefix}.capture_status", CAPTURE_STATUS_MISSING)
        span.set_attribute(f"{prefix}.reason", "capture_error")
        return None

    span.set_attribute(f"{prefix}.capture_status", result.status)
    span.set_attribute(f"{prefix}.preview_truncated", result.preview_truncated)
    span.set_attribute(f"{prefix}.original_redacted_bytes", result.original_redacted_bytes)
    span.set_attribute(f"{prefix}.stored_bytes", result.stored_bytes)
    if result.reason:
        span.set_attribute(f"{prefix}.reason", result.reason)
    if result.preview is not None and value_attribute:
        span.set_attribute(value_attribute, result.preview)
    if result.artifact is not None:
        span.set_attribute(f"{prefix}.artifact_id", result.artifact.artifact_id)
        span.set_attribute(f"{prefix}.relative_path", result.artifact.relative_path)
        span.set_attribute(f"{prefix}.sha256", result.artifact.sha256)
    return result


__all__ = [
    "CAPTURE_STATUS_CAPTURED",
    "CAPTURE_STATUS_DISABLED",
    "CAPTURE_STATUS_MISSING",
    "CAPTURE_STATUS_TRUNCATED",
    "ContentCapture",
    "DiagnosticContentStore",
    "capture_to_span",
    "configure_content_store",
    "get_content_store",
    "reset_content_store",
]
