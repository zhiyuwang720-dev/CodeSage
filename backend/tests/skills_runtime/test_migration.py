import json

from app.services.skills_runtime.migration import load_agent_bindings


def test_load_agent_bindings_reads_primary_bindings_file(tmp_path):
    library_root = tmp_path / "skill_library"
    bindings_file = library_root / "agents" / "finding" / "bindings.json"
    bindings_file.parent.mkdir(parents=True)
    bindings_file.write_text(
        json.dumps(
            {
                "agent_type": "finding",
                "skills": [
                    {"slug": "alpha", "enabled": True, "always_include": True, "sort_order": 2},
                    {"slug": "beta", "enabled": False, "sort_order": 3},
                ],
            }
        ),
        encoding="utf-8",
    )

    bindings = load_agent_bindings(library_root=library_root, agent_type="finding")

    assert [binding.slug for binding in bindings] == ["alpha", "beta"]
    assert bindings[0].always_include is True
    assert bindings[1].enabled is False


def test_load_agent_bindings_synthesizes_missing_entries_from_legacy_mirror_binding(tmp_path):
    library_root = tmp_path / "skill_library"
    mirror_dir = library_root / "agents" / "finding" / "gamma"
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "binding.json").write_text(
        json.dumps(
            {
                "agent_type": "finding",
                "skill_id": "gamma",
                "enabled": True,
                "always_include": False,
                "sort_order": 5,
                "match_keywords": ["auth"],
            }
        ),
        encoding="utf-8",
    )

    bindings = load_agent_bindings(library_root=library_root, agent_type="finding")

    assert [binding.slug for binding in bindings] == ["gamma"]
    assert bindings[0].match_keywords == ["auth"]
