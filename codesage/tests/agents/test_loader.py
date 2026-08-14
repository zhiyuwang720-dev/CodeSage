"""Agent loader tests (phase 13 S1): frontmatter parsing, silent skip,
priority merge, cache invalidation, builtin trio, fork_context force."""

from pathlib import Path

import pytest

from codesage.agents import AgentRegistry, BUILTIN_AGENTS
from codesage.agents.loader import load_dir

_FENCE = "---"


def _write_agent(dir_path: Path, fname: str, fm: str, body: str = "do work") -> Path:
    p = dir_path / fname
    p.write_text(f"{_FENCE}\n{fm}{_FENCE}\n{body}\n", encoding="utf-8")
    return p


def test_flow_list_forms(tmp_path):
    """[a, b] / comma / space forms all parse as frozensets."""
    _write_agent(
        tmp_path,
        "a.md",
        "name: a\n"
        'description: "db migration specialist"\n'
        "tools: [Read, Bash]\n"
        "disallowed_tools: Agent, Write\n"
        "max_turns: 30\n"
        "permission_mode: plan\n",
    )
    a = load_dir(tmp_path)["a"]
    assert a.tools == frozenset({"Read", "Bash"})
    assert a.disallowed_tools == frozenset({"Agent", "Write"})
    assert a.max_turns == 30
    assert a.permission_mode == "plan"
    assert a.source == "project"
    assert a.body == "do work"


def test_space_separated_flow_list(tmp_path):
    _write_agent(tmp_path, "s.md", "name: s\ntools: Read Bash Glob\n")
    assert load_dir(tmp_path)["s"].tools == frozenset({"Read", "Bash", "Glob"})


def test_missing_fence_skipped(tmp_path):
    p = tmp_path / "plain.md"
    p.write_text("name: x\ndescription: d\n", encoding="utf-8")
    assert load_dir(tmp_path) == {}


def test_missing_name_skipped(tmp_path):
    _write_agent(tmp_path, "noname.md", "description: d\n")
    assert load_dir(tmp_path) == {}


def test_unknown_fields_ignored(tmp_path):
    _write_agent(
        tmp_path,
        "u.md",
        "name: u\ndescription: d\nmcpServers: {x: 1}\nskills: [a]\nsomeFuture: true\n",
    )
    a = load_dir(tmp_path)["u"]
    assert a.name == "u"
    assert a.hooks is None
    assert a.color is None


def test_hooks_map_parsed_and_stored(tmp_path):
    _write_agent(
        tmp_path,
        "h.md",
        "name: h\ndescription: d\nhooks:\n  PreToolUse: echo hi\n  PostToolUse: echo bye\n",
    )
    a = load_dir(tmp_path)["h"]
    assert a.hooks == {"PreToolUse": "echo hi", "PostToolUse": "echo bye"}


def test_priority_project_over_user_over_builtin(tmp_path):
    user_dir = tmp_path / "user"
    proj_dir = tmp_path / "proj"
    user_dir.mkdir()
    proj_dir.mkdir()
    # User overrides builtin Explore
    _write_agent(user_dir, "explore.md", "name: Explore\ndescription: user explore\n")
    # Project overrides both: same name as builtin + new agent
    _write_agent(proj_dir, "general-purpose.md", "name: general-purpose\ndescription: proj gp\n")
    _write_agent(proj_dir, "custom.md", "name: custom\ndescription: proj custom\n")
    reg = AgentRegistry(user_dir=user_dir, project_dir=proj_dir)
    assert reg.get("Explore").description == "user explore"
    assert reg.get("Explore").source == "user"
    assert reg.get("general-purpose").description == "proj gp"
    assert reg.get("general-purpose").source == "project"
    assert reg.get("custom").source == "project"
    # Builtin Plan untouched
    assert reg.get("Plan").description == BUILTIN_AGENTS["Plan"].description
    assert reg.get("Plan").source == "builtin"


def test_lru_cache_invalidates_on_change(tmp_path):
    p = _write_agent(tmp_path, "c.md", "name: c\ndescription: v1\n")
    assert load_dir(tmp_path)["c"].description == "v1"
    # Same content again → same result (cache hit path)
    assert load_dir(tmp_path)["c"].description == "v1"
    # Edit (size changes) → cache key changes → fresh result
    p.write_text(f"{_FENCE}\nname: c\ndescription: v2-longer\n{_FENCE}\nbody\n", encoding="utf-8")
    assert load_dir(tmp_path)["c"].description == "v2-longer"


def test_fork_context_forces_inherit(tmp_path):
    with pytest.warns(UserWarning, match="fork_context"):
        _write_agent(
            tmp_path,
            "f.md",
            "name: f\ndescription: d\nfork_context: true\nmodel: sonnet\n",
        )
        a = load_dir(tmp_path)["f"]
    assert a.fork_context is True
    assert a.model is None  # forced to inherit
    assert a.max_turns == 50


def test_builtin_trio_registered():
    reg = AgentRegistry()  # no dirs → builtins only
    names = reg.names()
    assert names == ["Explore", "Plan", "general-purpose"]
    gp = reg.get("general-purpose")
    assert gp.tools is None  # full parent pool
    assert gp.source == "builtin"
    for name in ("Explore", "Plan"):
        a = reg.get(name)
        assert "Agent" in a.disallowed_tools
        assert "Write" in a.disallowed_tools
        assert "Edit" in a.disallowed_tools
        assert a.model is None  # inherit
        assert a.tools is None  # blacklist-style restriction


def test_get_unknown_lists_available(tmp_path):
    _write_agent(tmp_path, "k.md", "name: known\ndescription: d\n")
    reg = AgentRegistry(user_dir=tmp_path)
    with pytest.raises(KeyError) as exc:
        reg.get("nope")
    assert "unknown agent 'nope'" in str(exc.value)
    assert "known" in str(exc.value)  # available names in the message


def test_load_dir_missing_dir_is_empty(tmp_path):
    assert load_dir(tmp_path / "absent") == {}


def test_non_utf8_file_skipped(tmp_path):
    (tmp_path / "bin.md").write_bytes(b"\xff\xfe\x00name: x\n")
    assert load_dir(tmp_path) == {}


def test_yaml11_boolean_spellings(tmp_path):
    """yes/no/on/off must not survive as strings (bool('no') is True)."""
    _write_agent(tmp_path, "b.md", "name: b\ndescription: d\nfork_context: no\nbackground: no\n")
    a = load_dir(tmp_path)["b"]
    assert a.fork_context is False
    assert a.background is False
    _write_agent(tmp_path, "b2.md", "name: b2\ndescription: d\nfork_context: yes\nbackground: on\n")
    a = load_dir(tmp_path)["b2"]
    assert a.fork_context is True
    assert a.background is True


def test_unterminated_fence_skipped(tmp_path):
    p = tmp_path / "u.md"
    p.write_text("---\nname: u\ndescription: d\n", encoding="utf-8")  # no closing fence
    assert load_dir(tmp_path) == {}


def test_fence_inside_body_kept(tmp_path):
    _write_agent(
        tmp_path,
        "m.md",
        "name: m\ndescription: d\n",
        body="body one\n---\nbody two",
    )
    a = load_dir(tmp_path)["m"]
    assert a.body == "body one\n---\nbody two"


def test_same_size_edit_invalidates(tmp_path):
    """Same-size edit inside one mtime tick must still reload (digest key)."""
    _write_agent(tmp_path, "s.md", "name: s\ndescription: abcdefgh\n")
    assert load_dir(tmp_path)["s"].description == "abcdefgh"
    _write_agent(tmp_path, "s.md", "name: s\ndescription: hijklmno\n")  # same length
    assert load_dir(tmp_path)["s"].description == "hijklmno"


def test_bom_file_parsed(tmp_path):
    p = tmp_path / "bom.md"
    p.write_text("﻿---\nname: bom\ndescription: d\n---\nbody\n", encoding="utf-8")
    assert load_dir(tmp_path)["bom"].name == "bom"


def test_blank_line_in_hooks_keeps_entries(tmp_path):
    _write_agent(
        tmp_path,
        "h2.md",
        "name: h2\ndescription: d\n"
        "hooks:\n  PreToolUse: echo a\n\n  PostToolUse: echo b\n",
    )
    a = load_dir(tmp_path)["h2"]
    assert a.hooks == {"PreToolUse": "echo a", "PostToolUse": "echo b"}


def test_flow_map_hooks(tmp_path):
    _write_agent(tmp_path, "fm.md", "name: fm\ndescription: d\nhooks: {PreToolUse: echo a}\n")
    assert load_dir(tmp_path)["fm"].hooks == {"PreToolUse": "echo a"}


def test_invalid_max_turns_inherits(tmp_path):
    _write_agent(tmp_path, "t.md", "name: t\ndescription: d\nmax_turns: -5\n")
    assert load_dir(tmp_path)["t"].max_turns is None
    _write_agent(tmp_path, "t2.md", "name: t2\ndescription: d\nmax_turns: true\n")
    assert load_dir(tmp_path)["t2"].max_turns is None  # bool is an int subclass
    _write_agent(tmp_path, "t3.md", "name: t3\ndescription: d\nmax_turns: 0\n")
    assert load_dir(tmp_path)["t3"].max_turns is None


def test_numeric_name_skipped(tmp_path):
    _write_agent(tmp_path, "n.md", "name: 123\ndescription: d\n")
    assert load_dir(tmp_path) == {}


def test_from_default_paths_falls_back_to_cwd(tmp_path, monkeypatch):
    """No git root → project agents load from cwd (config/agents_md.py precedent)."""
    agent_dir = tmp_path / ".claude" / "agents"
    agent_dir.mkdir(parents=True)
    _write_agent(agent_dir, "proj.md", "name: proj\ndescription: d\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codesage.agents.registry.find_git_root", lambda start: None)
    reg = AgentRegistry.from_default_paths(cwd=tmp_path)
    assert reg.get("proj").source == "project"
