"""快速审查生产依赖的唯一组装点。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.db.session import async_session_factory, get_pr_review_sync_session_factory


@dataclass(frozen=True)
class ReviewUseCaseDependencies:
    async_session_factory: Callable[[], Any] = async_session_factory
    sync_session_factory: Callable[[], Any] = get_pr_review_sync_session_factory
    llm_service: Any | None = None
    event_stream_factory: Callable[[], Any] | None = None
