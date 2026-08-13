"""Session storage (phase 12): typed-entry JSONL, tree branches via lane pointers.

The phase 04 core/session.py migrated into a package: entry models (entry.py),
Session class + file enumeration (session.py), tree views (tree.py) and
archiving (archive.py) land in later steps (S2/S5).
"""

from .entry import (
    ENTRY_TYPES,
    SessionEntry,
    make_bookmark_entry,
    make_branch_summary_entry,
    make_lane_entry,
    make_meta_entry,
    make_message_entry,
    make_model_change_entry,
    make_operation_entry,
    parse_entry,
)
from .session import Session, find_session, list_sessions, most_recent_session

__all__ = [
    "ENTRY_TYPES",
    "Session",
    "SessionEntry",
    "find_session",
    "list_sessions",
    "make_bookmark_entry",
    "make_branch_summary_entry",
    "make_lane_entry",
    "make_meta_entry",
    "make_message_entry",
    "make_model_change_entry",
    "make_operation_entry",
    "most_recent_session",
    "parse_entry",
]
