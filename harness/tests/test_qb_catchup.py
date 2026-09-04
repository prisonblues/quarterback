"""Tests for qb-catchup — acting on the origin-moved signal (#83).

These drive REAL git repositories, because what is worth pinning here is not
string manipulation: it is which checkouts get rewritten and, far more
importantly, which do not. Every refusal in this file is one the tool must make
against a directory that really is in that state.

The safety property is the whole feature. Rewriting a checkout somebody is
working in is exactly the disaster #45 was filed for — an agent ran `git rebase
origin/main` inside a directory another agent held, and the holder found its
branch checked out at somebody else's commit with conflict markers in four
files. So the interesting assertions are the ones where nothing happens.

`worktree-holder` is stubbed rather than pointed at a board: its own suite covers
what it answers, and what matters here is that qb-catchup *acts* on each answer —
including exit 4, "could not tell", which this tool must treat as a refusal where
`prune-worktrees` treats it as permission to proceed.

Run: pytest harness/tests
"""

import importlib.machinery
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
HARNESS = Path(__file__).resolve().parent.parent
CATCHUP = BIN / "qb-catchup"
sys.path.insert(0, str(BIN))


def git(where, *args, check=True):
    return subprocess.run(["git", "-C", str(where), *args],
                          capture_output=True, text=True, check=check)


def commit(where, name, text="x", days_ago=0, seconds_ahead=0):
    """A commit, optionally dated into the past — or into the future.

    `days_ago` exists because the verdict this tool now reaches turns on AGE and not
    on count (#573): the same two commits are the ordinary state of work this morning
    and a single point of failure a fortnight later, and only a dated fixture can tell
    those two apart.

    `seconds_ahead` is the other end of the same axis and it is deliberately in SECONDS.
    Clock skew between a fleet's machines is ordinarily a handful of them, and an age
    computed as `(now - committed) / 60` truncates toward zero — so the whole
    sub-minute band came out as an age of 0, which is not negative, sits inside the
    grace window, and reads as work from this morning.
    """
    (Path(where) / name).write_text(text)
    git(where, "add", name)
    env = None
    if days_ago or seconds_ahead:
        when = (datetime.now(UTC) - timedelta(days=days_ago, seconds=-seconds_ahead)
                ).strftime("%Y-%m-%dT%H:%M:%S%z")
        env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(["git", "-C", str(where), "-c", "user.email=t@e", "-c", "user.name=T",
                    "commit", "-qm", name],
                   capture_output=True, text=True, check=True, env=env)


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch, tmp_path):
    """No global or system git config reaches these tests.

    The sibling of `test_qb_doctor.py`'s `_hermetic_git`, for the reason its
    docstring gives — this host's `~/.gitconfig` made a check take the wrong branch
    there. It matters here now because #573 made this tool's answer a function of
    exactly that surface: `remote.<r>.fetch` and the `refs/remotes/` inventory, either
    of which a developer's config can add to, and a run that also executes `qb-doctor`
    in-process against the same repo.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "gitconfig-system"))


@pytest.fixture
def fleet(tmp_path):
    """A bare 'remote', a main checkout tracking it, and a way to add worktrees.

    Shaped like the real thing: linked worktrees share the common git dir, which
    is why one fetch updates every one of them — a property qb-catchup relies on
    and this fixture therefore has to reproduce rather than fake.
    """
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)

    main = tmp_path / "proj"
    subprocess.run(["git", "clone", "-q", str(remote), str(main)], check=True)
    git(main, "config", "user.email", "t@e")
    git(main, "config", "user.name", "T")
    commit(main, "first")
    git(main, "push", "-q", "-u", "origin", "main")

    # A second clone standing in for "somebody else's machine", so the remote can
    # be advanced without touching anything under test.
    elsewhere = tmp_path / "elsewhere"
    subprocess.run(["git", "clone", "-q", str(remote), str(elsewhere)], check=True)
    git(elsewhere, "config", "user.email", "t@e")
    git(elsewhere, "config", "user.name", "T")

    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir()

    class Fleet:
        def __init__(self):
            self.tmp = tmp_path
            self.main = main
            self.remote = remote
            self.elsewhere = elsewhere
            self.stub_dir = stub_dir
            self.holder_rc = 0
            self.holder_name = "zeus/ember-marten"
            self.holder_calls = tmp_path / "holder.calls"
            self.git_calls = tmp_path / "git.calls"

        def worktree(self, branch, at=None):
            d = tmp_path / f"proj-{branch.replace('/', '-')}"
            git(main, "worktree", "add", "-q", "-b", branch, str(d), at or "main")
            return d

        def land_branch(self, branch):
            """Merge `branch` into main on the remote, the way a PR merge does — and
            leave `origin/<branch>` where it was, which is the state #573 is about."""
            git(self.elsewhere, "fetch", "-q", "origin")
            git(self.elsewhere, "checkout", "-q", "main")
            git(self.elsewhere, "reset", "-q", "--hard", "origin/main")
            git(self.elsewhere, "merge", "-q", "--no-ff", "-m", f"Merge {branch}",
                f"origin/{branch}")
            git(self.elsewhere, "push", "-q", "origin", "main")

        def land_upstream(self, name="landed"):
            """Advance origin/main the way another machine's merge would."""
            git(self.elsewhere, "fetch", "-q", "origin")
            git(self.elsewhere, "checkout", "-q", "main")
            git(self.elsewhere, "reset", "-q", "--hard", "origin/main")
            commit(self.elsewhere, name)
            git(self.elsewhere, "push", "-q", "origin", "main")

        def stub_holder(self, rc=0, holder="zeus/ember-marten"):
            """A worktree-holder on PATH that answers however the test needs.

            PATH-first resolution is qb-catchup's own rule, so a stub here wins
            over the installed copy — the same mechanism the seat scripts use.
            """
            (stub_dir / "worktree-holder").write_text(
                "#!/bin/sh\n"
                f'echo "$@" >> {self.holder_calls}\n'
                'for a in "$@"; do\n'
                '  [ "$a" = "--json" ] && {\n'
                f'    printf \'{{"held":true,"holders":[{{"holder":"{holder}"}}]}}\\n\'\n'
                "    exit %d\n" % rc
                + "  }\n"
                "done\n"
                f"exit {rc}\n")
            (stub_dir / "worktree-holder").chmod(0o755)

        def stub_date_that_fails(self):
            """A `date` on PATH that refuses to answer.

            Two different failures live behind this, both silent. An EMPTY `date`
            becomes a unary minus inside `$(( ))`, so the age comes out negative and
            the sweep confidently reports clock skew about a clock it never read. A
            NON-NUMERIC one is an arithmetic syntax error, which under `set -u` and
            without `set -e` leaves the variable unset and aborts the shell on the
            next test of it — an unattended sweep dying of a clock."""
            (stub_dir / "date").write_text("#!/bin/sh\nexit 1\n")
            (stub_dir / "date").chmod(0o755)

        def stub_date_that_lies(self):
            """A `date` that exits 0 and answers with something that is not a number.

            The other half of the pair above, and the half that used to ABORT rather
            than mislead: a non-numeric operand is an arithmetic syntax error, which
            under `set -u` and without `set -e` leaves the variable unset and kills the
            shell on the next test of it — an unattended sweep dying of a clock."""
            (stub_dir / "date").write_text("#!/bin/sh\necho not-a-number\n")
            (stub_dir / "date").chmod(0o755)

        def stub_git_that_records(self, refuse=""):
            """A `git` on PATH that records every invocation, then behaves normally.

            `refuse` names a subcommand it fails instead, so a test can ask what the
            sweep does when one specific query cannot be answered."""
            real = shutil.which("git")
            refusal = (f'for a in "$@"; do [ "$a" = "{refuse}" ] && exit 128; done\n'
                       if refuse else "")
            (stub_dir / "git").write_text(
                "#!/bin/sh\n"
                f'printf "%s\\n" "$*" >> {self.git_calls}\n'
                + refusal
                + f'exec {real} "$@"\n')
            (stub_dir / "git").chmod(0o755)

        def git_ran(self, needle):
            """Every recorded `git` invocation whose argv contains `needle`."""
            if not self.git_calls.exists():
                return []
            return [c for c in self.git_calls.read_text().splitlines() if needle in c]

        def run(self, *args, fetch=False, cwd=None):
            env = dict(os.environ)
            env["PATH"] = f"{stub_dir}:{env['PATH']}"
            no_fetch = [] if fetch else ["--no-fetch"]
            return subprocess.run(
                [str(CATCHUP), "-C", str(cwd or self.main), *no_fetch, *args],
                capture_output=True, text=True, env=env, timeout=60)

        def head(self, where):
            return git(where, "rev-parse", "HEAD").stdout.strip()

    f = Fleet()
    f.stub_holder(rc=0)
    yield f


# ------------------------------------------------------------- it moves things


def test_a_checkout_that_is_strictly_behind_is_fast_forwarded(fleet):
    """The case that bit: a merge lands elsewhere and this checkout is behind by
    a clean fast-forward."""
    fleet.land_upstream()
    git(fleet.main, "fetch", "-q", "origin")
    before = fleet.head(fleet.main)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "fast-forwarded" in done.stdout, done.stdout
    after = fleet.head(fleet.main)
    assert after != before
    assert after == git(fleet.main, "rev-parse", "origin/main").stdout.strip()


def test_a_checkout_already_current_is_reported_and_left(fleet):
    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "already current" in done.stdout, done.stdout
    assert "0 moved" in done.stdout


def test_dry_run_says_what_it_would_do_and_does_nothing(fleet):
    fleet.land_upstream()
    git(fleet.main, "fetch", "-q", "origin")
    before = fleet.head(fleet.main)

    done = fleet.run("--dry-run")
    assert done.returncode == 0, done.stderr
    assert "would fast-forward" in done.stdout, done.stdout
    assert fleet.head(fleet.main) == before, "a dry run moved the branch"


# ------------------------------------------------------------ it refuses things


def test_a_held_worktree_is_left_alone_and_the_holder_is_named(fleet):
    """A skip has to be somebody to talk to, not a shrug. #45 is what happens
    when this answer is ignored."""
    fleet.land_upstream()
    git(fleet.main, "fetch", "-q", "origin")
    before = fleet.head(fleet.main)
    fleet.stub_holder(rc=3, holder="zeus/amber-otter")

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "zeus/amber-otter" in done.stdout, done.stdout
    assert "left alone" in done.stdout
    assert fleet.head(fleet.main) == before, "a held worktree was rewritten"


def test_could_not_tell_is_a_refusal_and_not_a_licence(fleet):
    """THE OPPOSITE OF WHAT prune-worktrees DOES with the same exit code, and
    deliberately. There, refusing on a board outage means leaving real debris
    uncollected, so it proceeds. Here it would mean rewriting a live checkout
    because the board happened to be down.
    """
    fleet.land_upstream()
    git(fleet.main, "fetch", "-q", "origin")
    before = fleet.head(fleet.main)
    fleet.stub_holder(rc=4)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "cannot tell" in done.stdout, done.stdout
    assert fleet.head(fleet.main) == before, "a board outage became permission to rewrite"


def test_the_first_could_not_tell_settles_it_for_the_whole_sweep(fleet):
    """"Could not tell" is a property of the run — no board, or one that is down
    — not of the directory. prune-worktrees learned this the expensive way: up to
    twenty `curl --max-time 5` stalls on a plain dry run, each ending in the same
    warning."""
    fleet.worktree("feat/a")
    fleet.worktree("feat/b")
    fleet.stub_holder(rc=4)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    asked = fleet.holder_calls.read_text().splitlines() if fleet.holder_calls.exists() else []
    assert len(asked) == 1, f"asked {len(asked)} times about a run-wide answer: {asked}"


def test_a_worktree_with_uncommitted_changes_is_left_alone(fleet):
    fleet.land_upstream()
    git(fleet.main, "fetch", "-q", "origin")
    before = fleet.head(fleet.main)
    (fleet.main / "scratch.txt").write_text("half an idea")

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "uncommitted changes" in done.stdout, done.stdout
    assert fleet.head(fleet.main) == before


def test_work_on_no_remote_is_left_alone_and_said_loudly(fleet):
    """The state #45 was actually in, and the one thing here that cannot be
    reconstructed from the remote — now asked as `--not --remotes` and dated."""
    commit(fleet.main, "mine-only", days_ago=19)
    # CAPTURED BEFORE THE RUN, which is the whole of what the assertion below is worth.
    # It used to compare `fleet.head(...)` with `git rev-parse HEAD` in the same
    # directory — and `Fleet.head` IS that call, so the one line guarding the "left
    # alone" half of this test's name was an expression compared with itself and could
    # not fail. Every other "nothing happened" test in this file captures first.
    before = fleet.head(fleet.main)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "on no remote ref" in done.stdout, done.stdout
    assert "19 days old" in done.stdout, done.stdout
    assert "if this disk failed that work is gone" in done.stdout, "the loud part is the point"
    assert fleet.head(fleet.main) == before, "a worktree carrying the only copy was moved"


def test_a_diverged_branch_is_a_rebase_and_is_refused(fleet):
    """Fast-forward means fast-forward. Most re-integrations are fast-forwards;
    the interesting ones are exactly the ones an automaton should not attempt
    unattended."""
    fleet.land_upstream()
    git(fleet.main, "fetch", "-q", "origin")
    commit(fleet.main, "mine-too")
    before = fleet.head(fleet.main)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "that is a rebase, not a fast-forward" in done.stdout, done.stdout
    assert fleet.head(fleet.main) == before


def test_a_detached_worktree_is_left_alone(fleet):
    wt = fleet.worktree("feat/detach")
    git(wt, "checkout", "-q", "--detach")
    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "detached, left alone" in done.stdout, done.stdout


def test_a_branch_with_no_upstream_is_left_alone(fleet):
    fleet.worktree("feat/local-only")
    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "no upstream" in done.stdout, done.stdout


# ------------------------------------------------------------------ the sweep


def test_every_worktree_is_swept_not_just_the_one_it_was_pointed_at(fleet):
    """One fetch and one pass covers the machine, which is the point: the manual
    ending this replaces was paid once PER worktree."""
    a = fleet.worktree("feat/a")
    b = fleet.worktree("feat/b")
    done = fleet.run(cwd=a)
    assert done.returncode == 0, done.stderr
    for name in ("proj", a.name, b.name):
        assert name in done.stdout, f"{name} was not swept: {done.stdout}"
    assert "of 3 worktree(s)" in done.stdout


def test_a_sweep_that_finds_nothing_is_an_error_not_an_empty_result(fleet, tmp_path):
    """Reporting "0 worktrees, all current" for a run outside a repository is the
    quietest possible way to do nothing at all. prune-worktrees makes the same
    check for the same reason."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    done = fleet.run(cwd=outside)
    assert done.returncode == 1
    assert "not a git repository" in done.stderr, done.stderr


def test_a_fast_forward_that_git_refuses_is_reported_and_sets_the_exit_code(fleet):
    """The count says a fast-forward is possible and `--ff-only` is what makes a
    disagreement a refusal rather than a merge commit nobody asked for. Forced
    here with an interrupted merge, which leaves the tree clean but the branch
    unmergeable — so the tool gets past every earlier guard and has to handle
    git saying no.
    """
    fleet.land_upstream()
    git(fleet.main, "fetch", "-q", "origin")
    gitdir = git(fleet.main, "rev-parse", "--absolute-git-dir").stdout.strip()
    (Path(gitdir) / "MERGE_HEAD").write_text(fleet.head(fleet.main) + "\n")

    done = fleet.run()
    assert done.returncode == 1, f"a refused fast-forward passed silently: {done.stdout}"
    assert "fast-forward refused" in done.stdout, done.stdout


def test_the_summary_counts_what_happened(fleet):
    fleet.worktree("feat/a")
    fleet.land_upstream()
    git(fleet.main, "fetch", "-q", "origin")
    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "1 moved" in done.stdout, done.stdout
    assert "of 2 worktree(s)" in done.stdout


def test_quiet_says_nothing_and_still_acts(fleet):
    fleet.land_upstream()
    git(fleet.main, "fetch", "-q", "origin")
    before = fleet.head(fleet.main)
    done = fleet.run("--quiet")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "", done.stdout
    assert fleet.head(fleet.main) != before, "--quiet also stopped it working"


def test_a_bad_flag_is_a_caller_bug_and_says_so(fleet):
    done = fleet.run("--rebase-everything")
    assert done.returncode == 2, done
    assert "usage:" in done.stderr


def test_a_git_that_cannot_answer_is_not_a_clean_tree(fleet, tmp_path):
    """`set -u` without `-e` means a failing git returns an empty string — and an
    empty `--porcelain` is exactly what a CLEAN tree looks like. A worktree whose
    git could not answer would have read as safe to rewrite, which is the one
    direction this must never fail in.
    """
    fleet.land_upstream()
    git(fleet.main, "fetch", "-q", "origin")
    before = fleet.head(fleet.main)

    # A `git` on PATH that refuses `status` and behaves normally otherwise, so
    # the sweep gets all the way to the guard under test.
    real = shutil.which("git")
    (fleet.stub_dir / "git").write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do [ "$a" = "status" ] && exit 128; done\n'
        f'exec {real} "$@"\n')
    (fleet.stub_dir / "git").chmod(0o755)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "would not say whether it is clean" in done.stdout, done.stdout
    assert fleet.head(fleet.main) == before, "a checkout git could not read was rewritten"


def test_the_holder_is_asked_once_per_worktree_not_twice(fleet):
    """`--json` carries the name AND exits with the code, so asking twice would
    be a second board round trip per held worktree — and a second ANSWER, since
    the holder can change between them."""
    a = fleet.worktree("feat/a")
    git(a, "push", "-q", "-u", "origin", "feat/a")   # or it is skipped before the board
    fleet.stub_holder(rc=3, holder="zeus/amber-otter")

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "zeus/amber-otter" in done.stdout, "the name is what makes a skip actionable"
    asked = fleet.holder_calls.read_text().splitlines()
    assert len(asked) == 2, f"two worktrees, {len(asked)} calls: {asked}"


def test_the_board_is_not_asked_about_a_worktree_that_could_not_move_anyway(fleet):
    """Detached and no-upstream are two `git rev-parse`s; the holder is a round
    trip to the board, per worktree. A machine carrying twenty of them would
    otherwise spend twenty of those establishing that nineteen were never
    candidates.

    The ordering constraint is only that nothing WRITES before the holder has
    answered — the checks above it read refs, not the tree.
    """
    fleet.worktree("feat/no-upstream")
    done = fleet.run()
    assert done.returncode == 0, done.stderr
    asked = fleet.holder_calls.read_text().splitlines()
    assert len(asked) == 1, f"the board was asked about a non-candidate: {asked}"
    assert "proj" in asked[0]


def test_a_holder_check_that_crashed_is_not_permission(fleet):
    """Listing the refusals and letting everything else fall through was
    backwards: exit 1, 2, 126, 127 or a signal are what a safety check that
    CRASHED looks like, and treating a crashed check as "nobody is there" is the
    one direction this must never fail in."""
    fleet.land_upstream()
    git(fleet.main, "fetch", "-q", "origin")
    before = fleet.head(fleet.main)

    for rc in (1, 2, 127):
        fleet.stub_holder(rc=rc)
        done = fleet.run()
        assert done.returncode == 0, done.stderr
        assert "holder check itself failed" in done.stdout, f"rc={rc}: {done.stdout}"
        assert fleet.head(fleet.main) == before, f"rc={rc} was treated as permission"


def test_an_upstream_that_was_deleted_is_named_as_such(fleet):
    """RED/GREEN. The ordinary state of a worktree left lying around after its PR
    merged and the remote branch was deleted.

    `rev-parse --abbrev-ref --symbolic-full-name '@{u}'` does three things at
    once there: it writes the fatal to stderr, writes the literal string `@{u}`
    to STDOUT, and exits non-zero. So an emptiness test on the output passes, the
    "no upstream" branch never fires, and the failure falls through to the
    catch-all guard one step later — which refuses safely and then reports "git
    would not say where it stands" about a repository that is perfectly fine.

    Nothing unsafe ever happened; the diagnosis was wrong, and the diagnosis is
    what this tool is for. Found on the first run against real checkouts.
    """
    wt = fleet.worktree("feat/landed")
    git(wt, "push", "-q", "-u", "origin", "feat/landed")
    # The remote branch goes, exactly as a merge-and-delete leaves it.
    git(fleet.main, "push", "-q", "origin", "--delete", "feat/landed")
    git(fleet.main, "fetch", "-q", "--prune", "origin")

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "its upstream is gone" in done.stdout, done.stdout
    assert "would not say where it stands" not in done.stdout, (
        "the catch-all guard answered for a case that has a real name")


def test_a_branch_that_never_had_an_upstream_still_says_so(fleet):
    """The two are worth telling apart: a branch that never had an upstream is
    somebody's local work in progress, while one whose upstream has been deleted
    is almost always finished with. The useful next move differs."""
    fleet.worktree("feat/never-pushed")
    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "no upstream, nothing to catch up to" in done.stdout, done.stdout
    assert "upstream is gone" not in done.stdout


# ------------------------------------------------- what exists nowhere else (#573)
#
# The loud line used to decide on `<branch> ^origin/<branch>` — the branch against its
# own remote ref — and that is a different question from the one it was printing an
# answer to. Measured on zeus the day #573 was filed, it named six worktrees as work
# that existed nowhere else; every one of the six was an ancestor of `origin/main` in
# its entirety. In the same sweep, five worktrees really were carrying commits no
# remote had and it said nothing about any of them.


def test_a_branch_caught_up_with_main_is_ahead_of_its_own_ref_and_not_endangered(fleet):
    """RED/GREEN, and the exact shape #573 was filed about: `feat/issue-262` on zeus.

    A PR merges, the local branch is fast-forwarded onto `origin/main` — by this very
    tool, among other things — and it is now ahead of `origin/<branch>`, which still
    points at the pre-merge tip. Every commit in that gap is on `origin/main`. Nothing
    about it is at risk and the old line called it the only copy in the world.
    """
    wt = fleet.worktree("feat/landed")
    commit(wt, "the-work")
    git(wt, "push", "-q", "-u", "origin", "feat/landed")
    fleet.land_branch("feat/landed")
    git(fleet.main, "fetch", "-q", "origin")
    git(wt, "merge", "-q", "--ff-only", "origin/main")

    naive = int(git(wt, "rev-list", "--count", "feat/landed",
                    "^origin/feat/landed").stdout.strip())
    assert naive > 0, "the near-miss has to fire, or this test proves nothing"
    assert int(git(wt, "rev-list", "--count", "feat/landed",
                   "--not", "--remotes", "--").stdout.strip()) == 0

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "nothing on it is missing from every remote" in done.stdout, done.stdout
    assert "on no remote ref" not in done.stdout, done.stdout


def test_a_branch_that_was_never_pushed_is_found_although_it_has_no_upstream(fleet):
    """The blind spot, and the worse half of the two failures.

    `origin/<branch>` cannot be compared against when it does not exist, so a branch
    nobody ever pushed was invisible — including the two largest hoards on zeus,
    `feat/qb-dash-merged` (8 commits) and `feat/qb-dash-buttons` (5). This worktree
    exits the sweep before the upstream comparison is ever reached, which is why the
    measurement is taken above it.
    """
    wt = fleet.worktree("feat/never-pushed")
    commit(wt, "only-here", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "no upstream, nothing to catch up to" in done.stdout, done.stdout
    assert "1 commit on it is on no remote ref" in done.stdout, done.stdout
    assert "19 days old" in done.stdout, done.stdout


def test_an_upstream_that_is_gone_is_not_finished_with_while_it_carries_work(fleet):
    """RED/GREEN. `fix/issue-44` on zeus: one commit on no remote anywhere, and the
    sweep telling the reader the worktree was finished with.

    The deleted upstream is precisely what made those commits unreachable from any
    remote ref, so this is the state in which "probably merged and deleted" is not
    merely unhelpful but backwards.
    """
    wt = fleet.worktree("feat/landed")
    git(wt, "push", "-q", "-u", "origin", "feat/landed")
    git(fleet.main, "push", "-q", "origin", "--delete", "feat/landed")
    git(fleet.main, "fetch", "-q", "--prune", "origin")
    commit(wt, "written-after-the-delete", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "its upstream is gone, but this worktree is not finished with" in done.stdout
    assert "so this worktree is finished with" not in done.stdout, done.stdout
    assert "if this disk failed that work is gone" in done.stdout, done.stdout


def test_work_from_this_morning_is_in_flight_and_not_a_loss(fleet):
    """AGE IS THE VERDICT, NOT THE COUNT. This sweep runs on every merge, so a line
    that says the same alarming thing every time is wallpaper inside a week — and
    something is always mid-flight on a working machine."""
    commit(fleet.main, "mine-only")

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "work in flight, which is the ordinary state" in done.stdout, done.stdout
    assert "if this disk failed" not in done.stdout, done.stdout


def test_the_line_reports_and_does_not_instruct(fleet):
    """`qb-catchup` runs unattended from a hook, and the remedy is a decision per
    branch rather than a push: several of these are abandoned experiments, pushing
    them litters the remote with branches nobody wants, and on zeus at least one
    duplicated work that had already landed by another route."""
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "Push them" not in done.stdout, done.stdout
    assert "unpushed commit" not in done.stdout, done.stdout


def test_a_remote_whose_refs_do_not_cover_every_head_refuses_the_question(fleet):
    """Codex's finding on `qb-doctor`'s equivalent row (#567), which is the same trap
    in a different language and therefore gets the same guard rather than a second
    discovery. A single-branch clone configures
    `+refs/heads/main:refs/remotes/origin/main`, so `refs/remotes/` is complete for
    `main` and empty for everything else — and `--not --remotes` would then call every
    feature branch on the server work that exists only on this disk. That is #573's
    own cry-wolf failure, arriving through the refspec instead of the comparison."""
    wt = fleet.worktree("feat/on-the-server")
    commit(wt, "pushed-and-safe")
    git(wt, "push", "-q", "origin", "feat/on-the-server")
    # Exactly the state that refspec leaves behind: the branch is on the server and
    # nothing here has a ref for it.
    git(fleet.main, "config", "remote.origin.fetch",
        "+refs/heads/main:refs/remotes/origin/main")
    git(fleet.main, "update-ref", "-d", "refs/remotes/origin/feat/on-the-server")

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "does not fetch `refs/heads/*`" in done.stdout, done.stdout
    assert "on no remote ref" not in done.stdout, "it answered a question it had refused"


def test_a_remote_with_several_refspecs_still_counts_as_covering_every_head(fleet):
    """More than one `fetch` line is ordinary — notes, tags, a second refspec — and
    the guard must find `refs/heads/*` among them rather than expect it alone.

    This is also why the guard is parameter expansion and not `git config --get-all …
    | sed … | grep -qx`: `set -o pipefail` is on and `grep -q` exits the instant it
    matches, so with enough refspec lines to fill a pipe buffer the `sed` upstream
    dies of SIGPIPE, the pipeline reports 141, and the `!` test reads a remote that
    maps every head as one that does not. Three short lines do not fill a buffer, so
    this test does NOT go red against that form — it pins the behaviour, and the
    expansion removes the dependency on how much output happened to fit.
    """
    # THE FIXTURE ALREADY CONFIGURES `refs/heads/*` AS THE FIRST FETCH LINE, so adding
    # more after it left it at index 0 and the test passed whether the loop read every
    # line or only the first — which is the one thing its name promises to distinguish.
    # Rebuilt here with the line that matters LAST.
    git(fleet.main, "config", "--unset-all", "remote.origin.fetch")
    git(fleet.main, "config", "--add", "remote.origin.fetch",
        "+refs/notes/*:refs/notes/origin/*")
    git(fleet.main, "config", "--add", "remote.origin.fetch",
        "+refs/tags/*:refs/tags/origin/*")
    git(fleet.main, "config", "--add", "remote.origin.fetch",
        "+refs/heads/*:refs/remotes/origin/*")
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "does not fetch" not in done.stdout, done.stdout
    assert "on no remote ref" in done.stdout, done.stdout


def test_a_repository_with_no_remote_says_nothing_about_stranded_work(fleet):
    """There is no elsewhere for a commit to be, so nothing here is stranded and the
    question does not arise. `qb-doctor` reaches the same conclusion in the same
    shape."""
    git(fleet.main, "remote", "remove", "origin")
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "on no remote ref" not in done.stdout, done.stdout
    assert "does not fetch" not in done.stdout, done.stdout


# --------------------------- the two tools agree on the query and the window (#573)
#
# `qb-doctor` is Python and `qb-catchup` is shell, so the logic is DUPLICATED and not
# shared: there is nothing a 5000-line Python module and a hook-budget shell script
# can import from one another, and a shell-out from the sweep would put a Python
# start-up per worktree inside a 20-second budget. What can be shared is the answer,
# and these tests are what keep the duplicate honest — because two tools disagreeing
# about "does this work exist elsewhere" is worse than either being wrong on its own.
#
# AND ON WHAT IS PINNED HERE, WHICH IS STILL NOT THE SAME AS AGREEING IN GENERAL. Pinned:
# the QUERY, the GRACE WINDOW, the age verdict both reach about one ordinary checkout, and
# — since #611 — the four refspec and ref-ownership guards this sweep grew for #573.
#
# THOSE FOUR USED TO BE `qb-catchup`-ONLY, and the note here said so. `qb-doctor`'s
# `_maps_every_head` read the SOURCE half of a refspec and nothing else, so on a negative
# refspec, a destination outside `refs/remotes/`, an orphaned ref under `refs/remotes/` or
# a `config` read that failed, the doctor ANSWERED where the sweep refused — two tools
# reaching opposite verdicts about whether the work on one disk existed anywhere else.
# #611 ported them into `_refspec_coverage` and `_unowned_remote_ref`, and
# `test_the_two_tools_refuse_the_same_configurations` below executes both against one
# checkout per row, which is the only thing that keeps two implementations in two
# languages from drifting back apart.
#
# What is still NOT pinned: everything else either tool decides. These are four named
# configurations and three named properties, not a general agreement invariant.


def test_both_tools_ask_git_the_same_question():
    """The query itself. The guards around it are pinned separately, and by execution."""
    catchup = (BIN / "qb-catchup").read_text()
    doctor = (BIN / "qb-doctor").read_text()
    assert '"$tip" --not --remotes --' in catchup, (
        "the sweep must ask `--not --remotes`, never `origin/<branch>` — and about the "
        "resolved `refs/heads/` tip, never a bare name a tag can shadow")
    assert re.search(r'"--not",\s*"--remotes"', doctor), (
        "qb-doctor's query moved; qb-catchup was written to match it")


def test_the_grace_window_agrees_with_qb_doctor():
    catchup = (BIN / "qb-catchup").read_text()
    doctor = (BIN / "qb-doctor").read_text()
    mine = re.search(r"^STRANDED_GRACE_HOURS=(\d+)$", catchup, re.M)
    theirs = re.search(r"^UNPUSHED_GRACE_HOURS = (\d+)$", doctor, re.M)
    assert mine and theirs, "one of the two constants was renamed"
    assert mine.group(1) == theirs.group(1), (
        "the sweep and the doctor would disagree about whether the same branch is a "
        "problem, which is worse than either being wrong alone")


# ------------------------------------------- fetch scope and ref trust (#573, codex)
#
# `--not --remotes` subtracts the tracking refs of EVERY remote, so the set of refs it
# trusts and the set the sweep refreshes have to be the same set. #567's Codex pass
# found that mismatch in the Python sibling — one remote fetched, all of them trusted
# — and a second pass over this script found four more ways in, all of them quiet
# rather than loud, which is the dangerous direction for a warning about lost work.


def test_a_fetch_that_failed_still_warns_about_work_that_exists_nowhere_else(fleet):
    """It used to refuse the question outright. `fetch --all` exits non-zero if ANY
    remote fails, so one permanently dead remote bought the hedge on every line of every
    merge for ever and never the signal. Warning off older refs is at worst crying wolf."""
    git(fleet.main, "remote", "set-url", "origin", str(fleet.tmp / "gone.git"))
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run(fetch=True)
    assert done.returncode == 0, done.stderr
    assert "the fetch did not complete" in done.stdout, done.stdout
    assert "1 commit on it is on no remote ref" in done.stdout, (
        "a failed fetch silenced the question it exists to ask")
    assert "19 days old" in done.stdout, done.stdout


def test_a_fetch_that_failed_withholds_the_reassurance(fleet):
    """The other half, and the whole of the rule: "finished with" and "nothing on it is
    missing" are safety claims, and off refs nobody refreshed they send someone to delete
    the only copy of something. So the reassurance hedges where the warning does not."""
    wt = fleet.worktree("feat/landed")
    git(wt, "push", "-q", "-u", "origin", "feat/landed")
    git(fleet.main, "push", "-q", "origin", "--delete", "feat/landed")
    git(fleet.main, "fetch", "-q", "--prune", "origin")
    git(fleet.main, "remote", "set-url", "origin", str(fleet.tmp / "gone.git"))

    done = fleet.run(fetch=True)
    assert done.returncode == 0, done.stderr
    assert "the fetch did not complete" in done.stdout, done.stdout
    assert "so this worktree is finished with" not in done.stdout, (
        "a safety claim was made out of refs nobody refreshed")
    assert "nothing on it is missing from every remote" not in done.stdout, done.stdout
    assert "was not established" in done.stdout, (
        "it neither reassured nor hedged, which reads as a clean answer")


def test_a_failed_fetch_does_not_claim_a_measurement_the_refspec_refused(fleet):
    """Both banners fire from their own condition, and a repository can trip both: the
    remote is unreachable AND its refspecs disqualify the question. The fetch line used
    to assert the measurement happened ("was still measured against them") directly
    above the line saying it was not asked."""
    git(fleet.main, "config", "--add", "remote.origin.fetch", "^refs/heads/private/*")
    git(fleet.main, "remote", "set-url", "origin", str(fleet.tmp / "gone.git"))
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run(fetch=True)
    assert done.returncode == 0, done.stderr
    assert "the fetch did not complete" in done.stdout, done.stdout
    assert "was still measured against them" not in done.stdout, (
        "it claimed a measurement the refspec guard had just refused")
    assert "excludes" in done.stdout, (
        "the refusal's own reason went unsaid, leaving the fetch line unexplained")


def test_a_failed_fetch_and_a_refused_question_do_not_call_a_worktree_finished_with(fleet):
    """The banner is not the verdict, and the verdict is the half that deletes things.

    The combined condition above asserts only what the run-level line says. Here the
    same repository also carries the shape that loses work — a worktree whose upstream
    is gone — so `stranded_state` never leaves `unknown` and the per-worktree line has
    to hedge. A regression that kept "finished with" under a failed fetch plus a
    refused question would leave every banner test green."""
    wt = fleet.worktree("feat/landed")
    git(wt, "push", "-q", "-u", "origin", "feat/landed")
    git(fleet.main, "push", "-q", "origin", "--delete", "feat/landed")
    git(fleet.main, "fetch", "-q", "--prune", "origin")
    git(fleet.main, "config", "--add", "remote.origin.fetch", "^refs/heads/private/*")
    git(fleet.main, "remote", "set-url", "origin", str(fleet.tmp / "gone.git"))

    done = fleet.run(fetch=True)
    assert done.returncode == 0, done.stderr
    assert "the fetch did not complete" in done.stdout, done.stdout
    assert "was still measured against them" not in done.stdout, done.stdout
    assert "so this worktree is finished with" not in done.stdout, (
        "a directory was called disposable out of a measurement nothing took")
    assert "its upstream is gone, so the branch was probably merged and deleted" in (
        done.stdout), done.stdout


def test_a_failed_fetch_still_says_what_it_did_measure_when_the_question_stood(fleet):
    """The long half of the split, and the only assertion in this file that it is ever
    printed. Every other failed-fetch test here checks an absence, so the `ASK_STRANDED=1`
    branch could be deleted outright and the suite would stay green — leaving a run that
    did measure the disk against its refs silently indistinguishable from one that did
    not. Healthy refspecs, unreachable remote."""
    git(fleet.main, "remote", "set-url", "origin", str(fleet.tmp / "gone.git"))
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run(fetch=True)
    assert done.returncode == 0, done.stderr
    assert ("the fetch did not complete, so remote-tracking refs may be older than this "
            "sweep — what exists only on this disk was still measured against them, "
            "and no worktree below is called finished with") in done.stdout, done.stdout


def test_one_unreachable_remote_does_not_silence_the_question_for_the_others(fleet):
    """One dead remote fails `--all`, and that status used to refuse the question for the
    whole run — including for the remotes that answered. It now costs the reassurance."""
    git(fleet.main, "remote", "add", "retired", str(fleet.tmp / "retired.git"))
    git(fleet.main, "config", "remote.retired.fetch",
        "+refs/heads/*:refs/remotes/retired/*")
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run(fetch=True)
    assert done.returncode == 0, done.stderr
    assert "the fetch did not complete" in done.stdout, done.stdout
    assert "1 commit on it is on no remote ref" in done.stdout, (
        "one dead remote silenced the whole question")
    assert "19 days old" in done.stdout, done.stdout


def test_a_namespace_under_refs_remotes_that_is_not_a_remote_refuses_the_question(fleet):
    """The mirror image of the refspec check. The query trusts everything under
    `refs/remotes/`; only what a configured refspec writes is ever refreshed. A ref left
    by a removed remote never self-corrects, because nothing will ever fetch it again."""
    git(fleet.main, "update-ref", "refs/remotes/ghost/main", "HEAD")
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "`refs/remotes/ghost/main` is under `refs/remotes/`" in done.stdout, done.stdout
    assert "no remote's fetch refspec writes there" in done.stdout, done.stdout
    assert "on no remote ref" not in done.stdout, done.stdout


def test_a_ref_written_directly_at_refs_remotes_is_not_missed(fleet):
    """`git update-ref refs/remotes/ghost <sha>` is a legal ref layout with no
    `<remote>/<branch>` shape to it at all, and `--not --remotes` trusts it exactly like
    any other ref under `refs/remotes/`. The enumeration used to require four
    slash-separated fields (`awk -F/ 'NF>3'`) and dropped this one on the floor — a
    permanent, self-never-correcting place for local-only commits to hide."""
    git(fleet.main, "update-ref", "refs/remotes/ghost", "HEAD")
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "`refs/remotes/ghost` is under `refs/remotes/`" in done.stdout, done.stdout
    assert "on no remote ref" not in done.stdout, done.stdout


def test_a_ref_no_refspec_writes_to_is_not_trusted_because_the_remote_name_matches(fleet):
    """OWNERSHIP IS A REFSPEC DESTINATION AND NEVER A REMOTE NAME.

    `origin` here fetches every head into `refs/remotes/origin/branches/*` — legal, and
    still under `refs/remotes/`, so the coverage check above is satisfied and the
    question is not refused for that reason. It does not write `refs/remotes/origin/old`
    and nothing here ever will. Inferring ownership from the top path segment matching a
    configured NAME trusted that ref anyway, and `--not --remotes` then subtracted a ref
    nothing refreshes — the quiet direction, and the one that loses work.
    """
    git(fleet.main, "config", "remote.origin.fetch",
        "+refs/heads/*:refs/remotes/origin/branches/*")
    git(fleet.main, "fetch", "-q", "origin")
    git(fleet.main, "symbolic-ref", "-d", "refs/remotes/origin/HEAD", check=False)
    git(fleet.main, "update-ref", "-d", "refs/remotes/origin/main")
    git(fleet.main, "update-ref", "refs/remotes/origin/old", "HEAD")
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "`refs/remotes/origin/old` is under `refs/remotes/`" in done.stdout, done.stdout
    assert "no remote's fetch refspec writes there" in done.stdout, done.stdout
    assert "on no remote ref" not in done.stdout, "it trusted a ref nothing refreshes"


def test_an_orphaned_namespace_that_merely_shares_a_prefix_is_not_trusted(fleet):
    """The looser half of the same fault. A remote name may itself contain a slash, and
    the guard used to accept any namespace some configured name merely BEGAN with: with
    `team/alice` configured, an orphaned `refs/remotes/team/bob/` — left by a removed
    remote, or written there by hand — read as covered because `team/alice` starts with
    `team/`. Nothing will ever refresh it, and asking the refspecs where they write is
    what tells the two apart."""
    git(fleet.main, "remote", "add", "team/alice", str(fleet.remote))
    git(fleet.main, "fetch", "-q", "team/alice")
    git(fleet.main, "update-ref", "refs/remotes/team/bob/main", "HEAD")
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "`refs/remotes/team/bob/main` is under `refs/remotes/`" in done.stdout, done.stdout
    assert "on no remote ref" not in done.stdout, "a prefix match hid an unrefreshed namespace"


def test_an_inventory_of_refs_that_failed_refuses_rather_than_finding_nothing(fleet):
    """The one guard in this block that used to fail OPEN. It read its refs from a
    process substitution, which `pipefail` does not reach and whose exit status nothing
    observed — so a `for-each-ref` that died on a corrupt ref or a permission yielded no
    lines, the audit became a silent no-op, and the question was answered out of refs
    that had never been checked."""
    fleet.stub_git_that_records(refuse="for-each-ref")
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "would not list the refs under `refs/remotes/`" in done.stdout, done.stdout
    assert "on no remote ref" not in done.stdout, "a failed inventory became a clean bill"


def test_a_refspec_read_that_failed_is_not_a_remote_that_maps_nothing(fleet):
    """An inspection that did not happen must not be reported as one that found nothing.
    This too read from a process substitution: a `git config` that failed left
    `maps_every_head` at 0 and came out as "`origin` does not fetch `refs/heads/*`",
    which names a configuration fault that may not exist and sends the reader to fix
    the wrong thing."""
    real = shutil.which("git")
    (fleet.stub_dir / "git").write_text(
        "#!/bin/sh\n"
        'case " $* " in *" --get-all "*) exit 4 ;; esac\n'
        f'exec {real} "$@"\n')
    (fleet.stub_dir / "git").chmod(0o755)
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "would not read `remote.origin.fetch` (exit 4)" in done.stdout, done.stdout
    assert "does not fetch" not in done.stdout, "it named a fault it had not established"
    assert "on no remote ref" not in done.stdout, done.stdout


def test_a_remote_with_no_fetch_refspec_at_all_says_which_fault_it_is(fleet):
    """The clean-but-empty answer, told apart from the failed read above. Both used to
    print the same sentence about `refs/heads/*`, and only one of them is true."""
    git(fleet.main, "config", "--unset-all", "remote.origin.fetch")
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "`origin` has no fetch refspec at all" in done.stdout, done.stdout
    assert "on no remote ref" not in done.stdout, done.stdout


def test_a_negative_refspec_refuses_the_question(fleet):
    """`^refs/heads/private/*` alongside `refs/heads/*` fetches everything except
    those, and a positive refspec elsewhere does not undo the exclusion. The branches
    it holds back are exactly the ones this would call work that exists nowhere else."""
    git(fleet.main, "config", "--add", "remote.origin.fetch", "^refs/heads/private/*")
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "excludes" in done.stdout, done.stdout
    assert "on no remote ref" not in done.stdout, done.stdout


def test_a_refspec_that_lands_outside_refs_remotes_refuses_the_question(fleet):
    """`refs/heads/*:refs/cache/*` brings back every head and puts none of it where
    `--remotes` looks — full coverage to a check that reads only the source."""
    git(fleet.main, "config", "remote.origin.fetch", "+refs/heads/*:refs/cache/origin/*")
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "which is not under `refs/remotes/`" in done.stdout, done.stdout
    assert "on no remote ref" not in done.stdout, done.stdout


def test_a_second_refspec_that_does_land_in_refs_remotes_is_coverage(fleet):
    """The two refspec faults are not the same kind of fault. An exclusion holds
    branches back whatever else is configured; a destination outside `refs/remotes/`
    only matters if nothing else brought the heads to where the question looks.
    Refusing over the first of two legal refspecs would be a false refusal."""
    git(fleet.main, "config", "remote.origin.fetch", "+refs/heads/*:refs/cache/origin/*")
    git(fleet.main, "config", "--add", "remote.origin.fetch",
        "+refs/heads/*:refs/remotes/origin/*")
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "not under `refs/remotes/`" not in done.stdout, done.stdout
    assert "on no remote ref" in done.stdout, done.stdout


def test_a_worktree_is_not_called_finished_with_when_the_question_was_refused(fleet):
    """"Finished with" is a safety claim, and a claim that was never established must
    not be made. A `git log` that failed for one worktree, or a run in which the
    question was refused for all of them, would otherwise come out as a confident
    "you can throw this away"."""
    wt = fleet.worktree("feat/landed")
    git(wt, "push", "-q", "-u", "origin", "feat/landed")
    git(fleet.main, "push", "-q", "origin", "--delete", "feat/landed")
    git(fleet.main, "fetch", "-q", "--prune", "origin")
    git(fleet.main, "update-ref", "refs/remotes/ghost/main", "HEAD")

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "was not established" in done.stdout, done.stdout
    assert "so this worktree is finished with" not in done.stdout, done.stdout


def test_a_clock_that_will_not_answer_does_not_kill_the_sweep(fleet):
    """Codex's finding, and the red run showed it fails one step earlier than either
    of us expected: an empty `date` is a unary minus inside `$(( ))`, so the old form
    did not abort — it reported "dated ahead of this clock", a confident diagnosis of
    clock skew drawn from a clock it had not read. A non-numeric `date` is the abort.
    Both are now the same honest answer: the count stands, and the age, which is the
    part that was not measured, is withheld."""
    commit(fleet.main, "mine-only", days_ago=19)
    fleet.worktree("feat/after-the-clock")
    fleet.stub_date_that_fails()

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "how old could not be read from this clock" in done.stdout, done.stdout
    assert "feat/after-the-clock" in done.stdout, "the sweep died where the clock did"
    assert "left alone, of 2 worktree(s)" in done.stdout, done.stdout


# ---------------------------------- the note is carried, not dropped (#573, round 2)
#
# `$stranded_note` is measured once per worktree, before the holder and dirty checks,
# and it used to be printed on only eight of the loop's fourteen exit paths. The six it
# was dropped on include the two states this fleet is in most of the time — a checkout
# somebody is editing, and one an agent is holding — which is to say it went silent in
# exactly the directories likeliest to hold the only copy of something.


@pytest.mark.parametrize("state,setup,line", [
    ("dirty", lambda f: (f.main / "scratch.txt").write_text("half an idea"),
     "uncommitted changes, left alone"),
    ("held", lambda f: f.stub_holder(rc=3, holder="zeus/amber-otter"),
     "held by zeus/amber-otter, left alone"),
    ("cannot tell", lambda f: f.stub_holder(rc=4),
     "cannot tell who is in it, left alone"),
])
def test_a_worktree_that_is_left_alone_still_says_what_exists_only_on_this_disk(
        fleet, state, setup, line):
    commit(fleet.main, "mine-only", days_ago=19)
    setup(fleet)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert line in done.stdout, done.stdout
    assert "if this disk failed that work is gone" in done.stdout, (
        f"the new signal was silenced in a {state} worktree, which is where it matters most")


def test_a_refused_question_hedges_rather_than_reading_as_a_clean_answer(fleet):
    """An empty note is indistinguishable from a note saying nothing was found. In a run
    where the question was refused for every worktree, "already current" and "N ahead of
    origin/x, left alone" were quietly making a safety claim nothing had established —
    and an ahead-of-upstream branch said strictly LESS than the tool used to, since the
    old line at least told you to push. Only the deleted-upstream branch hedged."""
    git(fleet.main, "update-ref", "refs/remotes/ghost/main", "HEAD")
    ahead = fleet.worktree("feat/ahead")
    git(ahead, "push", "-q", "-u", "origin", "feat/ahead")
    commit(ahead, "unshared")

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "already current" in done.stdout, done.stdout
    assert "1 ahead of" in done.stdout, done.stdout
    hedge = "whether anything on it exists only on this disk was not established"
    assert done.stdout.count(hedge) == 2, done.stdout


def test_a_per_worktree_walk_that_failed_hedges_too(fleet):
    """The other half of the same fault, and the one that is per DIRECTORY rather than
    per run: when the second walk itself fails the state is unknown, the note was empty,
    and the line printed as though the question had been asked and answered cleanly."""
    commit(fleet.main, "mine-only", days_ago=19)
    fleet.stub_git_that_records(refuse="log")

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "whether anything on it exists only on this disk was not established" in done.stdout, (
        done.stdout)
    assert "if this disk failed" not in done.stdout, done.stdout


def test_a_repository_with_no_remote_hedges_about_nothing(fleet):
    """The refusal that is NOT an unanswered question: with no remote there is no
    elsewhere for a commit to be, so the question does not arise and hedging about it
    would be noise on every line of every sweep in a scratch repo."""
    git(fleet.main, "remote", "remove", "origin")
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "was not established" not in done.stdout, done.stdout
    assert "on no remote ref" not in done.stdout, done.stdout


# ------------------------------------------ the oldest, and the cost of asking (#573)


def test_the_oldest_stranded_commit_is_the_oldest_and_not_the_last_line(fleet):
    """RED/GREEN, and the reason no other age fixture here can catch it: with a single
    stranded commit the first line and the last line of `git log` are the same line.

    Git's walk guarantees only that a child is emitted before its parent. Descending
    committer date is not promised, and it is not what a rebase, a `git am` or an
    explicit `GIT_COMMITTER_DATE` leaves behind — all of which this fleet does, and
    which this file's own `commit(..., days_ago=…)` helper does too. Here the PARENT is
    dated yesterday and its CHILD nineteen days ago, so emission is child-then-parent
    and the LAST line is the NEWER of the two. Taking it under-measures the age, and
    under-measuring reads a fortnight-old only copy as work in flight: a false green in
    the one direction this feature exists to prevent.
    """
    commit(fleet.main, "parent", days_ago=1)
    commit(fleet.main, "child", days_ago=19)

    order = git(fleet.main, "log", "--format=%ct", "main", "--not", "--remotes", "--"
                ).stdout.split()
    assert len(order) == 2 and int(order[-1]) > int(order[0]), (
        "the fixture must emit the NEWER commit last, or it cannot tell a minimum from "
        f"a last line: {order}")

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "2 commits on it are on no remote ref" in done.stdout, done.stdout
    assert "19 days old" in done.stdout, done.stdout
    assert "work in flight" not in done.stdout, done.stdout


def test_a_commit_dated_seconds_ahead_of_this_clock_is_skew_and_not_fresh_work(fleet):
    """`(now - oldest) / 60` truncates toward zero, so the whole sub-minute band of
    future skew came out as an age of 0 — which is not `< 0`, sits inside the grace
    window, and reports a clock this tool could not trust as work from this morning.
    Sub-minute is the ordinary size of skew between machines."""
    commit(fleet.main, "from-the-future", seconds_ahead=45)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "dated ahead of this clock" in done.stdout, done.stdout
    assert "work in flight" not in done.stdout, done.stdout


def test_a_clock_that_answers_with_nonsense_does_not_kill_the_sweep(fleet):
    """The other half of `stub_date_that_fails`'s docstring, and the half that ABORTS
    rather than misleads: a `date` that exits 0 and prints something non-numeric is an
    arithmetic syntax error, which under `set -u` without `set -e` leaves the variable
    unset and dies on the very next test of it, taking the rest of the sweep with it.
    The stub above only ever exercised the empty-output path."""
    commit(fleet.main, "mine-only", days_ago=19)
    fleet.worktree("feat/after-the-clock")
    fleet.stub_date_that_lies()

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "how old could not be read from this clock" in done.stdout, done.stdout
    assert "feat/after-the-clock" in done.stdout, "the sweep died where the clock did"
    assert "left alone, of 2 worktree(s)" in done.stdout, done.stdout


def test_every_worktree_is_asked_for_itself_and_not_against_a_repository_snapshot(fleet):
    """A repository-wide `rev-list --branches --not --remotes` taken once above the loop
    names every stranded tip in one walk, and it was tried — but it is a SNAPSHOT, and
    each worktree's tip is resolved fresh as the loop reaches it. A worktree that takes
    a local-only commit in between has a tip the stale set never named, falls through to
    `none`, and the sweep prints the safety claim that nothing is missing: the exact
    false negative this question exists to prevent, in the state an actively worked
    fleet is normally in. The walk it saved costs ~4ms against a 25s hook budget."""
    fleet.worktree("feat/a")
    mine = fleet.worktree("feat/mine")
    commit(mine, "only-here", days_ago=19)
    fleet.stub_git_that_records()

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert fleet.git_ran("rev-list --branches --not --remotes") == [], (
        "a repository-wide snapshot ages while the loop runs; ask per worktree instead")
    walked = fleet.git_ran("log --format=%ct")
    assert len(walked) == 3, walked
    assert "1 commit on it is on no remote ref" in done.stdout, done.stdout


def test_a_tag_sharing_a_branchs_name_does_not_get_measured_instead_of_the_branch(fleet):
    """`gitrevisions` resolves a bare `feat/x` as `refs/feat/x`, then `refs/tags/feat/x`,
    then `refs/heads/feat/x` — so a TAG WINS over the branch of the same name. Asking the
    walk about the bare name therefore measures the tag's history: point the tag at
    something the remote already has and the answer is 0, the state becomes `none`, and
    the sweep says it is finished with a directory holding the only copy of the work. The
    branch is resolved through `refs/heads/` and the walk is given the SHA."""
    mine = fleet.worktree("feat/shadowed")
    commit(mine, "only-here", days_ago=19)
    # A tag of the same name, pointing at a commit every remote already has.
    git(fleet.main, "tag", "feat/shadowed", "origin/main")

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "1 commit on it is on no remote ref" in done.stdout, (
        "the tag's history was measured, not the branch's")
    assert "so this worktree is finished with" not in done.stdout, done.stdout


def test_the_ordinary_fetching_path_reaches_the_same_verdict(fleet):
    """Every other test here runs `--no-fetch`, and the only one that fetches points at
    a URL that cannot be reached. So the path the hook uses WITHOUT `--no-fetch` — a
    fetch that succeeds, then the refspec guard, then the stranded measurement — had no
    green-path coverage at all: a fetch that wrongly set the failed flag, or a guard
    passing only because nothing had been refreshed, would both have gone unnoticed."""
    fleet.land_upstream()
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run(fetch=True)
    assert done.returncode == 0, done.stderr
    assert "the fetch did not complete" not in done.stdout, done.stdout
    assert "1 commit on it is on no remote ref" in done.stdout, done.stdout
    assert "19 days old" in done.stdout, done.stdout


# --------------------------------- the two tools have to agree, EXECUTED (#573)


def _doctor():
    """`qb-doctor` has no `.py`, so it is loaded by path exactly as its own suite does."""
    loader = importlib.machinery.SourceFileLoader("qb_doctor", str(BIN / "qb-doctor"))
    spec = importlib.util.spec_from_loader("qb_doctor", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["qb_doctor"] = module
    loader.exec_module(module)
    return module


def _doctor_unpushed(repo):
    qd = _doctor()
    common = Path(git(repo, "rev-parse", "--path-format=absolute",
                      "--git-common-dir").stdout.strip())
    host = qd.Host(repo=Path(repo), common_git_dir=common, base_url=None, token=None,
                   human_url=None, harness_bin=None, source_harness=HARNESS,
                   githooks=None, client_repo=Path(repo))
    return qd.check_unpushed(host)


@pytest.mark.parametrize("days_ago,verdict,loud", [(19, "fail", True), (0, "ok", False)])
def test_the_two_tools_reach_the_same_verdict_about_the_age_of_unpushed_work(
        fleet, days_ago, verdict, loud):
    """EXECUTING BOTH, where the two tests above only match strings in the two sources.

    A grep proves a literal is present in a file, never that it is on the live code
    path: a dead query, a branch that cannot be reached, or a second constant hardcoded
    somewhere further down would all leave those green while the tools said different
    things about the same branch in front of a user. So this runs each of them against
    one checkout and compares the answers rather than the text.

    ON THE AGE/GRACE-WINDOW PATH. The repository here has one ordinary remote fetching
    `refs/heads/*` into `refs/remotes/origin/*` and no stray refs, so nothing either tool
    guards against is in play and what is compared is the verdict about the age. The
    configurations that used to part them are the next test's business (#611).
    """
    commit(fleet.main, "mine-only", days_ago=days_ago)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    check = _doctor_unpushed(fleet.main)

    assert check.verdict == verdict, check.detail
    assert ("if this disk failed that work is gone" in done.stdout) is loud, done.stdout
    assert ("work in flight, which is the ordinary state" in done.stdout) is not loud, done.stdout
    assert ("work in flight, which is the ordinary state" in check.detail) is not loud, check.detail


def _configure(fleet, row):
    """One of #611's table rows, applied to the checkout both tools are about to read."""
    if row == "negative-refspec":
        git(fleet.main, "config", "--add", "remote.origin.fetch", "^refs/heads/private/*")
    elif row == "destination-outside-refs-remotes":
        git(fleet.main, "config", "remote.origin.fetch", "+refs/heads/*:refs/cache/origin/*")
    elif row == "orphaned-namespace":
        git(fleet.main, "update-ref", "refs/remotes/ghost/main", "HEAD")
    elif row == "no-fetch-refspec":
        git(fleet.main, "config", "--unset-all", "remote.origin.fetch")
    else:                                   # pragma: no cover - guards the parametrize list
        raise AssertionError(f"unknown row {row}")


@pytest.mark.parametrize("row,phrase", [
    ("negative-refspec", "excludes `refs/heads/private/*`"),
    ("destination-outside-refs-remotes", "which is not under `refs/remotes/`"),
    ("orphaned-namespace", "no remote's fetch refspec writes there"),
    ("no-fetch-refspec", "`origin` has no fetch refspec at all"),
])
def test_the_two_tools_refuse_the_same_configurations(fleet, row, phrase):
    """#611, and the reason it was worth doing rather than documenting.

    Each row is a configuration on which the sweep refused the question and `qb-doctor`
    answered it — the same disk, the same commits, two opposite verdicts about whether
    the work on it existed anywhere else. A reader who ran the tool that answered got a
    number computed from tracking refs it had no business trusting.

    EXECUTING BOTH, for the reason the age test above gives: a grep over two sources
    proves a literal is present in a file, never that it is on the live code path. Two
    implementations in two languages cannot share code, so this is what holds them
    together — and it fails the moment either side grows a guard the other has not.

    The commit is nineteen days old, so a guard that failed to fire would not merely
    answer: it would answer LOUDLY, which is the difference these assertions read.
    """
    _configure(fleet, row)
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run()
    check = _doctor_unpushed(fleet.main)

    assert done.returncode == 0, done.stderr
    assert phrase in done.stdout, done.stdout
    assert check.verdict == "unknown", check.detail
    assert phrase in check.detail, check.detail
    assert "on no remote ref" not in done.stdout, done.stdout


@pytest.mark.parametrize("kw,verdict", [
    (dict(days_ago=1, seconds_ahead=90), "work in flight, which is the ordinary state"),
    (dict(days_ago=1), "if this disk failed that work is gone"),
])
def test_the_grace_window_bites_at_its_own_boundary(fleet, kw, verdict):
    """The age fixtures elsewhere are this morning and nineteen days, either side of a
    window neither of them is near. `-lt` drifting to `-le`, or the minute truncation in
    `age_m` drifting by a unit, would move the boundary without a single one noticing."""
    commit(fleet.main, "mine-only", **kw)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert verdict in done.stdout, done.stdout
