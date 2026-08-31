import json

from app.services.skills_runtime.discovery import discover_skill_entries


def test_discover_skill_entries_reads_canonical_skill_roots(tmp_path):
    project_root = tmp_path
    library_root = project_root / "skill_library"
    skill_dir = library_root / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "description: Demo description\n"
        "tags: [finding, auth]\n"
        "---\n\n"
        "# Demo\n",
        encoding="utf-8",
    )
    (skill_dir / "metadata.json").write_text(
        json.dumps({"source_type": "manual", "source_url": "https://example.com/demo"}),
        encoding="utf-8",
    )

    entries = discover_skill_entries(library_root=library_root, project_root=project_root)

    assert [entry.slug for entry in entries] == ["demo-skill"]
    entry = entries[0]
    assert entry.name == "demo-skill"
    assert entry.description == "Demo description"
    assert entry.tags == ["finding", "auth"]
    assert entry.frontmatter["name"] == "demo-skill"
    assert entry.skill_file.replace("\\", "/").endswith("skill_library/demo-skill/SKILL.md")
    assert entry.metadata_json["workspace_relative_path"] == "skill_library/demo-skill"
    assert entry.metadata_json["skill_file_path"].replace("\\", "/").endswith("skill_library/demo-skill/SKILL.md")
    assert entry.metadata_json["references_root"].replace("\\", "/").endswith("skill_library/demo-skill/references")


def test_discover_skill_entries_ignores_non_skill_directories(tmp_path):
    project_root = tmp_path
    library_root = project_root / "skill_library"
    (library_root / "agents").mkdir(parents=True)
    (library_root / ".runtime").mkdir(parents=True)
    (library_root / "empty-skill").mkdir(parents=True)

    entries = discover_skill_entries(library_root=library_root, project_root=project_root)

    assert entries == []

def test_discover_skill_entries_parses_rich_frontmatter_fields(tmp_path):
    project_root = tmp_path
    library_root = project_root / "skill_library"
    skill_dir = library_root / "rich-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: Rich Skill\n"
        "description: Rich description\n"
        "tags:\n"
        "  - finding\n"
        "  - auth\n"
        "when_to_use: Use this for auth-heavy audits\n"
        "allowed-tools:\n"
        "  - read_file\n"
        "  - search_code\n"
        "argument-hint: <target>\n"
        "arguments:\n"
        "  - target\n"
        "  - mode\n"
        "version: 2.1.0\n"
        "model: claude-3-7-sonnet\n"
        "disable-model-invocation: true\n"
        "user-invocable: false\n"
        "context: fork\n"
        "agent: verification\n"
        "effort: high\n"
        "paths:\n"
        "  - backend/app/auth/**\n"
        "  - frontend/src/pages/Login.tsx\n"
        "hooks:\n"
        "  Stop:\n"
        "    - matcher: \".*\"\n"
        "      hooks:\n"
        "        - type: command\n"
        "          command: echo done\n"
        "shell:\n"
        "  type: bash\n"
        "  command: echo bootstrap\n"
        "---\n\n"
        "# Rich Skill\n",
        encoding="utf-8",
    )

    entries = discover_skill_entries(library_root=library_root, project_root=project_root)

    assert [entry.slug for entry in entries] == ["rich-skill"]
    entry = entries[0]
    assert entry.name == "Rich Skill"
    assert entry.description == "Rich description"
    assert entry.tags == ["finding", "auth"]
    assert entry.when_to_use == "Use this for auth-heavy audits"
    assert entry.allowed_tools == ["read_file", "search_code"]
    assert entry.argument_hint == "<target>"
    assert entry.argument_names == ["target", "mode"]
    assert entry.version == "2.1.0"
    assert entry.model == "claude-3-7-sonnet"
    assert entry.disable_model_invocation is True
    assert entry.user_invocable is False
    assert entry.execution_context == "fork"
    assert entry.agent == "verification"
    assert entry.effort == "high"
    assert entry.paths == ["backend/app/auth/**", "frontend/src/pages/Login.tsx"]
    assert entry.hooks["Stop"][0]["matcher"] == ".*"
    assert entry.shell == {"type": "bash", "command": "echo bootstrap"}
