"""Rule matching tests."""

from pathlib import Path

from codesage.permissions.rules import extract_rules, match_first, path_rule_matches, tool_rule_matches


def test_tool_rule_exact_and_glob():
    assert tool_rule_matches("Bash", "Bash")
    assert tool_rule_matches("mcp__*__*", "mcp__server__read")
    assert tool_rule_matches("Skill(foo:*)", "Skill(foo:bar)")
    assert not tool_rule_matches("Skill(foo:*)", "Skill(any)")
    assert not tool_rule_matches("Bash", "Read")


def test_path_rule_prefix():
    assert path_rule_matches("/home/u", Path("/home/u/proj/file.py"))
    assert path_rule_matches("/home/u/**", Path("/home/u/proj/deep/file.py"))
    assert path_rule_matches("/home/u/*", Path("/home/u/file.py"))
    assert not path_rule_matches("/home/u/*", Path("/home/u/proj/file.py"))


def test_path_rule_glob():
    assert path_rule_matches("**/secret*", Path("/a/b/secret.txt"))
    assert path_rule_matches("*.env", Path("/a/.env"))


def test_path_rule_windows_separators():
    assert path_rule_matches("C:/repo/**", Path(r"C:\repo\sub\file.py"))
    assert not path_rule_matches("C:/repo/*", Path(r"C:\repo\sub\file.py"))


def test_extract_rules_shapes():
    rules = extract_rules({"allow": ["Read"], "deny": "Bash", "ask": ["Grep", "LS"], "junk": 1})
    assert rules == {"allow": ["Read"], "deny": ["Bash"], "ask": ["Grep", "LS"]}
    assert extract_rules(None) == {"allow": [], "deny": [], "ask": []}


def test_match_first_returns_rule():
    assert match_first(["/home/u/**"], "Write", Path("/home/u/x.txt")) == "/home/u/**"
    assert match_first(["Bash"], "Bash", None) == "Bash"
    assert match_first(["Read"], "Bash", None) is None
