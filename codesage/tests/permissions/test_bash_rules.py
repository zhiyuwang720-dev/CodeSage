"""Bash command static-analysis tests (Kode core subset)."""

from pathlib import Path

from codesage.permissions.bash_rules import analyze_bash_command, split_commands


def _analyze(cmd, cwd: Path):
    return analyze_bash_command(cmd, working_dirs=[cwd.resolve()], cwd=cwd)


# ---- splitting ----

def test_split_commands_ignores_quotes():
    assert split_commands('a && b || c; d | e') == ["a", "b", "c", "d", "e"]
    assert split_commands('echo "a && b" && c') == ['echo "a && b"', "c"]
    assert split_commands("echo 'x;y'") == ["echo 'x;y'"]


# ---- rm/rmdir deny protection ----

def test_rm_protected_targets_denied(tmp_path):
    for cmd in ["rm -rf /", "rm -rf /home", "rm -rf /root", "rm -rf ~", "rm -rf ."]:
        assert _analyze(cmd, tmp_path).decision == "deny", cmd
    assert _analyze("rm", tmp_path).decision == "deny"  # no target
    assert _analyze("rm -rf", tmp_path).decision == "deny"
    assert _analyze("rmdir", tmp_path).decision == "deny"


def test_rm_inside_working_dir_allowed(tmp_path):
    d = _analyze("rm -f old.txt", tmp_path)
    assert d.decision == "allow"


def test_rm_outside_working_dir_asks(tmp_path):
    d = _analyze("rm -f /etc/passwd", tmp_path)
    assert d.decision == "ask"


def test_sudo_rm_protected_target_denied(tmp_path):
    d = _analyze("sudo rm -rf /", tmp_path)
    assert d.decision == "deny"


# ---- write targets / redirection ----

def test_redirect_outside_working_dir_asks(tmp_path):
    assert _analyze("echo hi > /etc/passwd", tmp_path).decision == "ask"
    assert _analyze("echo hi >> /etc/issue", tmp_path).decision == "ask"
    assert _analyze("echo hi 2> /etc/issue", tmp_path).decision == "ask"


def test_redirect_inside_working_dir_allowed(tmp_path):
    assert _analyze("echo hi > out.txt", tmp_path).decision == "allow"


def test_mv_cp_dest_outside_asks(tmp_path):
    assert _analyze("cp a.txt /etc/passwd", tmp_path).decision == "ask"
    assert _analyze("mv a.txt /etc/passwd", tmp_path).decision == "ask"
    assert _analyze("mkdir /opt/x", tmp_path).decision == "ask"


def test_sed_inplace_outside_asks(tmp_path):
    assert _analyze("sed -i s/x/y/ /etc/passwd", tmp_path).decision == "ask"


# ---- cd compounds ----

def test_cd_with_write_compound_asks(tmp_path):
    assert _analyze("cd /tmp && rm x", tmp_path).decision == "ask"
    assert _analyze("cd /tmp && mv x y", tmp_path).decision == "ask"
    assert _analyze("cd /tmp && mkdir d", tmp_path).decision == "ask"
    assert _analyze("cd /tmp && echo hi > f", tmp_path).decision == "ask"


def test_cd_without_write_allowed(tmp_path):
    assert _analyze("cd /tmp && ls", tmp_path).decision == "allow"


# ---- injection patterns ----

def test_injection_patterns_ask(tmp_path):
    for cmd in [
        "rm `ls`", "rm $(ls)", "echo ${PATH}", "cat <(ls)",
        "IFS=; rm x", "cat <<EOF", "rm >(cat)",
    ]:
        assert _analyze(cmd, tmp_path).decision == "ask", cmd


def test_plain_command_allowed(tmp_path):
    for cmd in ["ls", "grep -r foo .", "cat file.txt", "python build.py"]:
        assert _analyze(cmd, tmp_path).decision == "allow", cmd
