"""Core domain layer (phase 04/12): session messages, API normalization, session storage.

Session storage migrated to the core/session package (phase 12); the export
surface is unchanged — all existing imports keep working.

Shared markdown frontmatter parsing (phase 14 S1) lives in core/frontmatter:
extracted from agents/loader.py so both agents (13) and skills (14) reuse it.
"""

from .frontmatter import (
    parse_flow_list,
    parse_flow_map,
    parse_frontmatter,
    parse_scalar,
    parse_value,
)
from .messages import SessionMessage, assistant_message, user_message
from .normalize import NO_CONTENT_MESSAGE, normalize_for_api
from .session import (
    ENTRY_TYPES,
    Session,
    SessionEntry,
    SessionMeta,
    TreeNode,
    TreeView,
    active_sessions,
    archive_session,
    archived_sessions,
    build_tree,
    find_open_operations,
    find_session,
    lane_names,
    linear_messages,
    list_sessions,
    numbered_entries,
    restore_session,
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
    "SessionMeta",
    "TreeNode",
    "TreeView",
    "active_sessions",
    "archive_session",
    "archived_sessions",
    "assistant_message",
    "build_tree",
    "find_open_operations",
    "find_session",
    "lane_names",
    "linear_messages",
    "list_sessions",
    "numbered_entries",
    "parse_flow_list",
    "parse_flow_map",
    "parse_frontmatter",
    "parse_scalar",
    "parse_value",
    "restore_session",
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
