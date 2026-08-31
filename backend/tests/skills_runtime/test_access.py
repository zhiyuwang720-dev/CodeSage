import os

import pytest

from app.services.skills_runtime.access import list_skill_resources, read_skill_body, read_skill_resource
from app.services.skills_runtime.discovery import discover_skill_entries


def _make_entry(tmp_path):
    project_root = tmp_path
    library_root = project_root / "skill_library"
    skill_dir = library_root / "demo-skill"
    references_dir = skill_dir / "references" / "core"
    references_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo description\n---\n\n# Demo\n",
        encoding="utf-8",
    )
    (references_dir / "guide.md").write_text("hello", encoding="utf-8")
    return discover_skill_entries(library_root=library_root, project_root=project_root)[0]


def test_access_reads_skill_body_and_resources(tmp_path):
    entry = _make_entry(tmp_path)

    body = read_skill_body(entry)
    listing = list_skill_resources(entry, "references/core")
    resource = read_skill_resource(entry, "references/core/guide.md")

    assert body["slug"] == "demo-skill"
    assert listing["items"][0]["path"] == "references/core/guide.md"
    assert resource["content"] == "hello"


def test_access_rejects_parent_escape(tmp_path):
    entry = _make_entry(tmp_path)

    with pytest.raises(ValueError, match="outside"):
        read_skill_resource(entry, "../outside.txt")


def test_access_rejects_symlink_escape_when_supported(tmp_path):
    entry = _make_entry(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link_path = tmp_path / "skill_library" / "demo-skill" / "references" / "core" / "leak.md"

    try:
        os.symlink(outside, link_path)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported in this environment")

    with pytest.raises(ValueError, match="outside"):
        read_skill_resource(entry, "references/core/leak.md")
