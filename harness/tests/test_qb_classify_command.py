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
    ("git rm -f app.py", True),
    ("git status", False),
    ("git diff", False),
    ("git add .", True),             # sweeps — see the harm table below
    ("git commit -am 'ok'", True),
    ("git add app.py", False),
    ("git commit -m 'just mine'", False),
    ("git reset --soft HEAD~1", False),
    ("git reset HEAD~1", False),
    ("git checkout main && git status", False),
    ("git log --oneline -3", False),
    ("git worktree list", False),
    ("grep -rn 'git reset --hard' harness/", False),
    ("echo git reset --hard", False),
    ("git stash", False),          # the PUSH is guarded a layer down (#210)
    ("git stash list", False),
    ("git stash pop", True),       # the pop is guarded by nobody else (#739)
    ("git stash apply", True),
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


# ------------------------------------------------------- the harm that was missing


@pytest.mark.parametrize("cmd,harm", [
    ("git reset --hard", "destroys"),
    ("git clean -fd", "destroys"),
    ("git commit -a -m wip", "sweeps"),
    ("git commit -am wip", "sweeps"),
    ("git add .", "sweeps"),
    ("git add -A", "sweeps"),
    ("git add -u", "sweeps"),
    ("git add ./src", "sweeps"),
    ("git commit -m 'only what I staged'", None),
    ("git add one.py two.py", None),
])
def test_the_two_harms_are_told_apart(cmd, harm):
    """The guard knew one harm and #185's evidence says the commoner one is the
    other. Counted by mechanism, the five incidents are: `git commit -a` sweeping
    up a peer's work (3860), exactly that happening (3879), a half-wired include
    breaking everyone's build (4004, not a command), a claim race (3853, not a
    command), and two hard resets. The verb list covered the last row alone.

    They need different words as well as different coverage: nothing is destroyed
    when your commit absorbs somebody's in-flight file, and its author has still
    lost it."""
    assert classify(cmd)["harm"] == harm, cmd


def test_a_named_path_is_not_a_sweep():
    """The distinction that keeps this usable. `git add .` in a shared tree stages
    whatever a peer left lying about; `git add app.py` stages what you named."""
    assert not destructive("git add app.py")
    assert destructive("git add .")


# ------------------------------------------------ the harm that fell between the gates


@pytest.mark.parametrize("cmd", [
    "git stash pop",
    "git stash apply",
    "git stash pop stash@{2}",
    "git stash apply --index stash@{1}",
    "git stash branch hotfix",              # applies the entry, then drops it
    "git -C /peer stash pop",
    "git status && git stash pop",          # the clause, not the command
    "bash -c 'git stash pop'",
])
def test_taking_from_the_shared_stash_is_a_harm(cmd):
    """#739. `refs/stash` lives in the COMMON git dir, so every worktree of a repo
    shares one stack and `stash@{0}` is whatever the last pusher meant — from any
    worktree, not necessarily this one.

    #210 refuses pushes onto it and CANNOT refuse a pop: a pop deletes its entry
    through the reflog, which raises no ref transaction while another entry
    remains underneath. The protection there is to keep the stack empty, and the
    gap is what happens when it is not — an agent hits the push refusal and pops
    next, which where the files do not clash applies a sibling's work into its
    tree and drops the entry from under them.

    This layer is the only one that can see a pop at all: it reads the command
    string before git runs, so the reflog fact that makes a pop unhookable does
    not apply to it."""
    assert classify(cmd)["harm"] == "takes", cmd


@pytest.mark.parametrize("cmd", [
    "git stash",                            # a bare stash is a push — #210's job
    "git stash push -u",
    "git stash list",
    "git stash show -p stash@{0}",
    "git stash drop",                       # deletions are how a dirty stack drains
    "git stash clear",
])
def test_the_rest_of_the_stash_verb_is_left_alone(cmd):
    """Two of these are deliberate and worth stating. `push` belongs to the
    reference-transaction hook, which is better placed for it — it catches a
    stash typed outside Claude Code entirely. And `drop`/`clear` take nothing
    into a tree: they are the one route out of a stack that already has
    somebody's pre-guard entries on it, and a guard that closed it would strand
    them for good."""
    assert not destructive(cmd), cmd


def test_the_tree_hatch_does_not_open_the_stash_hazard():
    """One hatch per HAZARD, not one per gate. A shared working tree and a shared
    stash stack are different configurations and are consented to separately, so
    an agent that has settled the tree question with its peer has said nothing
    about whose entry sits at stash@{0}."""
    assert classify("QB_ALLOW_SHARED_TREE=1 git stash pop")["allowed_by"] is None
    assert classify("QB_ALLOW_SHARED_STASH=1 git reset --hard")["allowed_by"] is None


def test_the_stash_hatch_is_the_same_name_the_lower_gate_honours():
    """The standing objection to guarding stash here was that two gates on one
    command means two escape hatches under different names. It is answered by
    there being one name: `QB_ALLOW_SHARED_STASH` is what the
    reference-transaction hook already reads for the push side."""
    hatch = "QB_ALLOW_SHARED_STASH=1"
    assert classify(f"{hatch} git stash pop")["allowed_by"] == hatch
    assert classify(f"{hatch} bash -c 'git stash pop'")["allowed_by"] == hatch


def test_a_hatch_on_another_clause_does_not_excuse_a_pop():
    """The clause-scoping property, held against the new harm. Every exemption in
    this file is scoped to the clause it was found in, and a third harm arriving
    later is exactly how that stops being true."""
    assert classify("QB_ALLOW_SHARED_STASH=1 echo hi\ngit stash pop")["allowed_by"] is None
    assert destructive("QB_ALLOW_SHARED_STASH=1 echo hi; git stash pop")


def test_a_hatch_on_one_clause_does_not_speak_for_the_next():
    """Found by an independent reviewer on the change that added the third harm,
    and it is the clause-scoping defect again — surviving in the one place a
    clause's verdict is allowed to stand for the whole command.

    Returning at the FIRST harmful clause carried that clause's hatch out with it,
    and the hook reads `allowed_by` and stands down. So a consented reset excused
    an unconsented pop behind it. Every other exemption here is clause-scoped;
    this one was not, and a third harm with a hatch of its own is what made it
    reachable across two different hazards rather than one."""
    v = classify("QB_ALLOW_SHARED_TREE=1 git reset --hard; git stash pop")
    assert (v["harm"], v["allowed_by"]) == ("takes", None)

    # The same shape within one hazard, which predates the third harm.
    v = classify("QB_ALLOW_SHARED_TREE=1 git reset --hard; git add .")
    assert (v["harm"], v["allowed_by"]) == ("sweeps", None)

    # And pointed the other way, so this is not a rule about which harm is first.
    v = classify("QB_ALLOW_SHARED_STASH=1 git stash pop; git reset --hard")
    assert (v["harm"], v["allowed_by"]) == ("destroys", None)


def test_a_command_whose_every_harm_is_hatched_still_reports_the_hatch():
    """The other half, and the one that keeps the hatch working at all. `allowed_by`
    is how the hook is told to stand down, so a command with nothing unconsented
    left in it has to come back carrying one — otherwise fixing the leak above
    would have turned every escape hatch into a refusal."""
    assert classify("QB_ALLOW_SHARED_TREE=1 git reset --hard")["allowed_by"]
    assert classify("QB_ALLOW_SHARED_TREE=1 git reset --hard; git status")["allowed_by"]
    both = classify(
        "QB_ALLOW_SHARED_TREE=1 git reset --hard; QB_ALLOW_SHARED_STASH=1 git stash pop")
    assert both["allowed_by"] == "QB_ALLOW_SHARED_TREE=1"


# ------------------------------------------------- every harm, not just the winner


def test_every_harmful_clause_is_reported_in_order():
    """The summary names one harm; `harms` names them all. Since the harms stopped
    sharing a predicate, a caller with two questions cannot use the summary — it
    hides the later harms behind a question that was never put to them."""
    v = classify("git stash pop; git reset --hard")
    assert [h["harm"] for h in v["harms"]] == ["takes", "destroys"]
    assert v["harm"] == "takes", "the summary is still the first unhatched harm"

    v = classify("git reset --hard; git stash pop; git add .")
    assert [h["harm"] for h in v["harms"]] == ["destroys", "takes", "sweeps"]


def test_each_harm_carries_its_own_hatch_and_target():
    """The two facts a walk needs per clause, and neither survives a summary: two
    clauses can name two different trees, and a hatch consents to one hazard."""
    v = classify("QB_ALLOW_SHARED_TREE=1 git reset --hard; git stash pop")
    assert [h["allowed_by"] for h in v["harms"]] == ["QB_ALLOW_SHARED_TREE=1", None]

    v = classify("git -C /a reset --hard; git -C /b add .")
    assert [h["target"] for h in v["harms"]] == ["/a", "/b"]


def test_a_nested_shells_harms_are_flattened_not_summarised():
    """`bash -c '…'` used to contribute one harm however many it held, so a command
    inside it could be masked the same way. Its harms are this command's harms,
    each keeping its own hatch state."""
    v = classify("bash -c 'git reset --hard; git stash pop'")
    assert [h["harm"] for h in v["harms"]] == ["destroys", "takes"]

    v = classify("QB_ALLOW_SHARED_STASH=1 bash -c 'git reset --hard; git stash pop'")
    assert [h["allowed_by"] for h in v["harms"]] == [None, "QB_ALLOW_SHARED_STASH=1"]


def test_nothing_harmful_reports_an_empty_harms_list():
    """The shape has to be stable, because the reader indexes it. `parsed: false`
    included — a command we could not tokenise has no harms to list, and an absent
    key would read to a `jq` walk as a harm it could not see."""
    assert classify("git status")["harms"] == []
    assert classify('git reset --hard "')["harms"] == []


@pytest.mark.parametrize("cmd", [
    "git stash apply refs/worktree/qb-stash/fix-issue-739",
    "git stash pop refs/worktree/qb-stash/fix-issue-739",
    "git stash apply deadbeef1234",
    "git stash branch hotfix refs/worktree/qb-stash/x",
])
def test_an_explicit_object_off_the_shared_stack_is_not_this_harm(cmd):
    """`qb-stash apply`/`pop` — the replacement this guard's own refusal advises —
    run `git stash apply <refs/worktree/...>`. That namespace is per-worktree and
    invisible to every sibling, so refusing it because some unrelated entry sits on
    `refs/stash` would be the guard refusing the command it recommends.

    A raw sha is out for a different reason: looking one up means the caller
    already ran `git stash list` or `show` and chose it, which is the deliberate
    act the hatch exists for."""
    assert not destructive(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "git stash pop",                    # no argument at all — the default is shared
    "git stash apply",
    "git stash branch hotfix",
    "git stash pop stash@{0}",
    "git stash apply stash@{2}",
    "git stash branch hotfix stash@{1}",
    "git stash pop refs/stash",
])
def test_the_shared_stack_is_still_named_however_it_is_spelled(cmd):
    assert classify(cmd)["harm"] == "takes", cmd


# ------------------------------------------------- the rung below a refusal (#745)


@pytest.mark.parametrize("cmd", [
    "git checkout-index -a -f",
    "git checkout-index -f -a",
    "git checkout-index -af",
    "git checkout-index --all --force",
    "git checkout-index -a",                          # before anyone reaches for -f
    "git checkout-index -f -- app.py",                # one file, the same overwrite
    "git checkout-index --force --prefix=/tmp/s/ -a",
    "git -c core.filemode=false checkout-index -a -f",
    "sh -c 'git checkout-index -a -f'",
    "git status && git checkout-index -a -f",
])
def test_restoring_the_tree_from_the_index_is_destructive(cmd):
    """2026-09-04, lexray. A sub-agent doing the red/green step of `/fix-issue`
    found the ref-restore form refused, ran this instead, and rewrote every
    tracked file in its worktree from the index — finished edits reverted, a new
    module truncated, nothing said. It is the worst of that family: `-a` names no
    path, there is no ref to make it read as a discard, and it is silent when it
    works."""
    assert classify(cmd)["harm"] == "destroys", cmd


@pytest.mark.parametrize("cmd", [
    "git checkout-index app.py",     # no -f: git will not write over a file that exists
    "git checkout-index",
    "git checkout-index --help",
    "git checkout-index --stdin",
])
def test_a_checkout_index_that_cannot_overwrite_is_left_alone(cmd):
    assert not destructive(cmd), cmd


@pytest.mark.parametrize("cmd,want", [
    ("git read-tree --reset -u HEAD", True),
    ("git read-tree -u --reset HEAD", True),
    ("git read-tree --reset --update HEAD", True),
    ("git read-tree -m -u --reset HEAD", True),
    ("git read-tree -um --reset HEAD", True),
    # `--reset` alone rewrites the INDEX and stops there, and index-only damage
    # is consistently not this harm — `reset HEAD~1` and `restore --staged` are
    # both false for the same reason. `-u` alone is not a command git accepts
    # (`fatal: -u is meaningless without -m, --reset, or --prefix`), and `-m -u`,
    # which it does, is a merge that carries the local change forward rather than
    # writing over it. All three measured on git 2.54.0.
    ("git read-tree --reset HEAD", False),
    ("git read-tree -u HEAD", False),
    ("git read-tree -m -u HEAD", False),
    ("git read-tree HEAD", False),
    ("git read-tree --help", False),
])
def test_the_next_rung_down_needs_both_halves(cmd, want):
    assert destructive(cmd) is want, cmd


def test_the_new_verbs_answer_to_the_tree_hatch_like_the_rest():
    """They are the `destroys` harm, so they are escaped by the hazard's own
    hatch and by no other — a caller who has settled it with the peer standing
    in the tree gets through, and `QB_ALLOW_SHARED_STASH` is not that caller."""
    assert classify("QB_ALLOW_SHARED_TREE=1 git checkout-index -a -f")["allowed_by"]
    assert classify("QB_ALLOW_SHARED_TREE=1 git read-tree --reset -u HEAD")["allowed_by"]
    assert not classify("QB_ALLOW_SHARED_STASH=1 git checkout-index -a -f")["allowed_by"]
