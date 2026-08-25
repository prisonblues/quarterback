"""`qb-classify-command`: does this shell command destroy uncommitted work? (#185)

**Every case here is a P1 or P2 a panel round found in the regex this replaced.**
That is the reason the file exists, and the reason it is this long: nine of the
findings were one premise wearing nine faces — a regular expression cannot parse
a shell command — and the only way to know the premise is gone rather than
patched is to hold every face against the thing that replaced it.

The regex survives as a prefilter in `qb-hook`, where it decides nothing and
exists to keep the common case fork-free. Everything that decides is here.

Run: pytest harness/tests
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CLASSIFY = Path(__file__).resolve().parents[1] / "bin" / "qb-classify-command"

pytestmark = pytest.mark.skipif(shutil.which("python3") is None, reason="needs python3")


def classify(command: str) -> dict:
    got = subprocess.run(["python3", str(CLASSIFY)], input=command,
                         capture_output=True, text=True, timeout=30)
    assert got.returncode == 0, got.stderr
    return json.loads(got.stdout)


def destructive(command: str) -> bool:
    return classify(command)["destructive"]


# ------------------------------------------------- the nine P1 bypasses, by name


@pytest.mark.parametrize("cmd", [
    # 447-F01 — the pattern special-cased `-C <path>` as the ONLY two-token global
    # option, so every other one stalled the scan mid-option and the whole match
    # failed. Not the target lookup: the classification itself.
    "git -c core.filemode=false reset --hard",
    "git --work-tree /path reset --hard",
    "git --git-dir /p/.git reset --hard",
    "git --namespace ns reset --hard",
    "git -c a=b -c c=d reset --hard",
    "git -c core.pager=less -C /tmp reset --hard",
])
def test_a_git_option_that_takes_a_value_does_not_hide_the_verb(cmd):
    assert destructive(cmd), cmd


def test_a_dry_run_in_one_clause_does_not_excuse_another(): 
    """447-F14. `git clean -n` removes nothing, and the exemption for it was
    tested against the WHOLE command — so a harmless first clause excused a
    destructive second one."""
    assert destructive("git clean -n && git reset --hard")
    assert not destructive("git clean -n")
    assert not destructive("git clean -fdn")


def test_a_help_in_one_clause_does_not_excuse_another():
    """447-F13, the same shape through `--help`."""
    assert destructive("git status --help; git reset --hard")
    assert not destructive("git restore --help")
    assert not destructive("git reset --hard --help")


def test_the_escape_hatch_is_scoped_to_its_own_clause():
    """447-F15/F17. Under grep, `^` anchors per LINE — so a hatch on line 1
    excused a reset on line 2, and `echo QB_ALLOW_SHARED_TREE=1; git reset
    --hard`, which assigns nothing at all, excused itself."""
    assert classify("QB_ALLOW_SHARED_TREE=1 git reset --hard")["allowed_by"]
    assert not classify("QB_ALLOW_SHARED_TREE=1 echo hi\ngit reset --hard")["allowed_by"]
    assert not classify("echo QB_ALLOW_SHARED_TREE=1; git reset --hard")["allowed_by"]
    assert not classify("QB_ALLOW_SHARED_TREE=10 git reset --hard")["allowed_by"]
    assert not classify("git reset --hard # QB_ALLOW_SHARED_TREE=1")["allowed_by"]


def test_the_work_tree_wins_over_dash_c_when_both_are_given():
    """447-F08. `-C` says where git RUNS; `--work-tree` says which tree it
    OPERATES ON. With both present the second is the one whose files change, and
    the regex preferred `-C` unconditionally."""
    assert classify("git -C /a --work-tree=/b reset --hard")["target"] == "/b"
    assert classify("git -C /a reset --hard")["target"] == "/a"


def test_the_target_comes_from_the_clause_that_matched():
    """447-F16. Both extractions ran leftmost-first over the whole string, so the
    tree that got checked could belong to a different clause entirely."""
    assert classify("git -C /elsewhere status; git -C /peer reset --hard")["target"] == "/peer"


def test_a_force_flag_is_found_wherever_it_sits():
    """447-F03. The pattern required `-f` immediately after `checkout`."""
    assert destructive("git checkout -f main")
    assert destructive("git checkout main -f")
    assert destructive("git checkout --quiet --force main")


# ---------------------------------------------------------------- P2 gap-closers


def test_a_bare_checkout_of_a_tracked_file_is_destructive(tmp_path):
    """447-F02. `git checkout <file>` overwrites that file and looks exactly like
    checking out a branch; only the filesystem tells them apart."""
    (tmp_path / "app.py").write_text("x\n")
    assert destructive(f"git -C {tmp_path} checkout app.py")
    assert not destructive(f"git -C {tmp_path} checkout some-branch")


def test_restore_staged_alone_touches_only_the_index():
    """447-F05. `git restore --staged X` unstages; it does not overwrite the
    working file, and refusing it is a false positive."""
    assert not destructive("git restore --staged app.py")
    assert destructive("git restore --staged --worktree app.py")
    assert destructive("git restore app.py")


def test_a_path_qualified_git_is_still_git():
    """447-F04. The anchor required `git` as a bare token."""
    assert destructive("/usr/bin/git reset --hard")


def test_a_nested_shell_is_looked_inside():
    """Documented as an accepted limit while a regex did the work. Once the
    command is tokenised it is one recursion, so the limit goes."""
    assert destructive("bash -c 'git reset --hard'")
    assert destructive('sh -c "git clean -fd"')


@pytest.mark.parametrize("cmd,want", [
    ("git switch -f main", True),
    ("git switch --discard-changes main", True),
    ("git switch main", False),
    ("git switch -c feat/x", False),
    ("git clean -fd", True),
    ("git clean --force", True),
    ("git clean --dry-run --force", False),
    ("git worktree remove --force ../wt", True),
    ("git worktree remove ../wt", False),
    ("git merge --abort", True),
    ("git rebase --abort", True),
    ("git read-tree --reset -u HEAD", True),
    ("git checkout-index -a -f", True),
    ("git rm -f app.py", True),
    ("git status", False),
    ("git diff", False),
    ("git add .", False),
    ("git commit -am 'ok'", False),
    ("git reset --soft HEAD~1", False),
    ("git reset HEAD~1", False),
    ("git checkout main && git status", False),
    ("git log --oneline -3", False),
    ("git worktree list", False),
    ("grep -rn 'git reset --hard' harness/", False),
    ("echo git reset --hard", False),
    ("git stash", False),          # guarded a layer down, deliberately not here
    ("git stash list", False),
])
def test_the_verb_table(cmd, want):
    assert destructive(cmd) is want, cmd


# ------------------------------------------------------------------- honesty


def test_a_command_that_will_not_tokenise_says_so():
    """An unparseable command is one we know nothing about, and the one state
    that must never be dressed up as knowledge. The hook reads `parsed: false`
    and lets it through — fail-open, as everywhere else."""
    out = classify('git reset --hard "')
    assert out["parsed"] is False
    assert out["destructive"] is False


def test_an_unresolvable_target_is_unknown_not_absent():
    """Nothing here expands `$SOME_DIR`. Reporting it as a path resolves to a
    directory that is not a checkout, and the guard would then shrug at a command
    about to hard-reset whatever the variable holds. None puts it back on the
    cwd, which is the conservative half of being wrong."""
    out = classify('git -C "$SOME_DIR" reset --hard')
    assert out["destructive"] is True
    assert out["target"] is None


def test_echoing_the_words_is_not_doing_it():
    """The one thing the prefilter cannot decide and the tokeniser can."""
    assert not destructive("echo 'git reset --hard'")
    assert not destructive("# git reset --hard")
