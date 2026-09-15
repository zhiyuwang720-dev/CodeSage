from __future__ import annotations

import json
from pathlib import Path

from app.db.base import Base
import app.models  # noqa: F401  # register every current mapper


ROOT = Path(__file__).resolve().parent


def _load(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_22a_t01_inventory_covers_every_registered_table():
    inventory = _load("ownership-inventory.json")
    entries = inventory["tables"]
    declared = {entry["table"] for entry in entries}
    registered = set(Base.metadata.tables)
    assert len(entries) == len(declared), "ownership inventory contains duplicate tables"
    assert declared == registered
    required = {
        "definition", "readers_writers", "api_ui_consumers",
        "foreign_keys_relationships", "migration_dependency",
        "target_owner", "disposition",
    }
    assert all(required <= set(entry) for entry in entries)


def test_22a_t09_compatibility_map_has_removal_milestones():
    mappings = _load("compatibility-map.json")["mappings"]
    assert mappings
    assert all(item["old"] != item["new"] for item in mappings)
    assert all(item["callers"] and item["removal"] for item in mappings)
