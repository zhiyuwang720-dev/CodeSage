"""Core domain layer (phase 04): session messages, API normalization, session storage."""

from .messages import SessionMessage, assistant_message, user_message
from .normalize import normalize_for_api
from .session import Session

__all__ = [
    "Session",
    "SessionMessage",
    "assistant_message",
    "normalize_for_api",
    "user_message",
]
