"""Core domain layer (phase 04/12): session messages, API normalization, session storage.

Session storage migrated to the core/session package (phase 12); the export
surface is unchanged — all existing imports keep working.
"""

from .messages import SessionMessage, assistant_message, user_message
from .normalize import NO_CONTENT_MESSAGE, normalize_for_api
from .session import (
    ENTRY_TYPES,
    Session,
    SessionEntry,
    TreeNode,
    TreeView,
    build_tree,
    find_open_operations,
    find_session,
    linear_messages,
    list_sessions,
    make_bookmark_entry,
    make_branch_summary_entry,
    make_lane_entry,
    make_meta_entry,
    make_message_entry,
    make_model_change_entry,
    make_operation_entry,
    most_recent_session,
    parse_entry,
)

__all__ = [
    "ENTRY_TYPES",
    "NO_CONTENT_MESSAGE",
    "Session",
    "SessionEntry",
    "SessionMessage",
    "TreeNode",
    "TreeView",
    "assistant_message",
    "build_tree",
    "find_open_operations",
    "find_session",
    "linear_messages",
    "list_sessions",
    "make_bookmark_entry",
    "make_branch_summary_entry",
    "make_lane_entry",
    "make_meta_entry",
    "make_message_entry",
    "make_model_change_entry",
    "make_operation_entry",
    "most_recent_session",
    "normalize_for_api",
    "parse_entry",
    "user_message",
]
