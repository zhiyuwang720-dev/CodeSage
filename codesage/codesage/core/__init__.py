"""Core domain layer (phase 04): session messages, API normalization, session storage."""

from .messages import SessionMessage, assistant_message, user_message
from .normalize import NO_CONTENT_MESSAGE, normalize_for_api
from .session import Session, find_session, list_sessions, most_recent_session

__all__ = [
    "NO_CONTENT_MESSAGE",
    "Session",
    "SessionMessage",
    "assistant_message",
    "find_session",
    "list_sessions",
    "most_recent_session",
    "normalize_for_api",
    "user_message",
]
