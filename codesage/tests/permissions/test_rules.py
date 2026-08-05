"""Rule matching tests."""

from pathlib import Path

from codesage.permissions.rules import (
    bash_rule_matches,
    bash_rules_match,
    extract_rules,
    match_first,
    parse_rule,
    path_rule_matches,
    tool_rule_matches,
)


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


def test_negation_cancels_earlier_match():
    assert match_first(["/repo/**", "!/repo/secret/**"], "Write", Path("/repo/secret/x")) is None
    assert match_first(["!/repo/secret/**"], "Write", Path("/repo/secret/x")) is None
    assert match_first(["Bash", "!Bash"], "Bash", None) is None


def test_negation_not_matching_leaves_earlier_match():
    assert match_first(["/repo/**", "!/other/**"], "Write", Path("/repo/x.txt")) == "/repo/**"
    assert match_first(["Bash", "!Read"], "Bash", None) == "Bash"


# ---- A1: Tool(content) rule parsing and read/write set separation ----

def test_parse_rule_shapes():
    assert parse_rule("Read(/abs/**)") == ("Read", "/abs/**")
    assert parse_rule("Bash(rm *)") == ("Bash", "rm *")
    assert parse_rule("Skill(foo:*)") == ("Skill", "foo:*")
    assert parse_rule("Bash") == ("Bash", None)
    assert parse_rule("/home/u/**") == ("/home/u/**", None)
    assert parse_rule("  Read( /x ) ") == ("Read", "/x")
    assert parse_rule("Read(/x") == (None, None)  # unbalanced — never matches
    assert parse_rule("(x)") == (None, None)  # no tool name — never matches


def test_content_rule_scoped_to_read_set():
    """allow:["Read(/tmp/**)"] — only file reads inside /tmp, never writes."""
    rule = ["Read(/tmp/**)"]
    assert match_first(rule, "Read", Path("/tmp/a.txt")) == "Read(/tmp/**)"
    assert match_first(rule, "Read", Path("/other/a.txt")) is None  # outside pattern
    assert match_first(rule, "Write", Path("/tmp/a.txt")) is None  # read rule never writes
    assert match_first(rule, "Edit", Path("/tmp/a.txt")) is None
    assert match_first(rule, "Grep", Path("/tmp/a.txt")) == "Read(/tmp/**)"  # same read set
    assert match_first(rule, "LS", Path("/tmp/a.txt")) == "Read(/tmp/**)"  # same read set


def test_content_rule_scoped_to_write_set():
    """deny:["Edit(/x/**)"] — hits Edit/Write, never a Read."""
    rule = ["Edit(/x/**)"]
    assert match_first(rule, "Edit", Path("/x/f.py")) == "Edit(/x/**)"
    assert match_first(rule, "Write", Path("/x/f.py")) == "Edit(/x/**)"  # same write set
    assert match_first(rule, "Read", Path("/x/f.py")) is None  # does not block reads
    assert match_first(rule, "Grep", Path("/x/f.py")) is None
    assert match_first(rule, "Edit", Path("/y/f.py")) is None  # outside pattern
    assert match_first(["Write(/x/**)"], "Edit", Path("/x/f.py")) == "Write(/x/**)"


def test_content_rule_tool_name_mismatch_never_hits():
    """A rule for one tool never applies to a different tool."""
    assert match_first(["Read(/x/**)"], "Bash", None) is None
    assert match_first(["Bash(rm *)"], "Read", Path("/x/a")) is None
    assert match_first(["Skill(foo:*)"], "Bash", None) is None


# ---- A2: Bash command rules ----

def test_bash_rule_matches_exact_and_prefix():
    assert bash_rule_matches("git status", "git status")
    assert bash_rule_matches("git status", "  git   status  ")  # whitespace-normalized
    assert not bash_rule_matches("git status", "git diff")
    assert bash_rule_matches("rm *", "rm -rf x")
    assert not bash_rule_matches("rm *", "ls")
    assert not bash_rule_matches("rm *", "rmx")  # prefix needs the space


def test_bash_rules_match_subcommands():
    # deny/ask: any hit on a subcommand matches the compound
    assert bash_rules_match(["Bash(rm *)"], "ls && rm -rf x", require_all=False) == "Bash(rm *)"
    assert bash_rules_match(["Bash(rm *)"], "ls", require_all=False) is None
    # whole-command exact match wins before splitting (Kode exactKey)
    assert bash_rules_match(["Bash(a && b)"], "a && b", require_all=False) == "Bash(a && b)"
    assert bash_rules_match(["Bash(rm *)"], "rm -rf x && rm -rf y", require_all=False) == "Bash(rm *)"
    # allow: every subcommand must hit
    assert bash_rules_match(["Bash(git status)"], "git status", require_all=True) == "Bash(git status)"
    assert bash_rules_match(["Bash(git status)"], "git status && ls", require_all=True) is None
    assert bash_rules_match(["Bash(git status)", "Bash(ls)"], "git status && ls", require_all=True) == "Bash(git status)"
    # bare "Bash" is a tool-level rule, not a command rule
    assert bash_rules_match(["Bash"], "ls", require_all=True) is None
    assert bash_rules_match([], "ls", require_all=False) is None
