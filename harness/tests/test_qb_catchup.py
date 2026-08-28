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

import os
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
CATCHUP = BIN / "qb-catchup"


def git(where, *args, check=True):
    return subprocess.run(["git", "-C", str(where), *args],
                          capture_output=True, text=True, check=check)


def commit(where, name, text="x", days_ago=0):
    """A commit, optionally dated into the past.

    `days_ago` exists because the verdict this tool now reaches turns on AGE and not
    on count (#573): the same two commits are the ordinary state of work this morning
    and a single point of failure a fortnight later, and only a dated fixture can tell
    those two apart.
    """
    (Path(where) / name).write_text(text)
    git(where, "add", name)
    env = None
    if days_ago:
        when = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S%z")
        env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(["git", "-C", str(where), "-c", "user.email=t@e", "-c", "user.name=T",
                    "commit", "-qm", name],
                   capture_output=True, text=True, check=True, env=env)


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
    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "on no remote ref" in done.stdout, done.stdout
    assert "19 days old" in done.stdout, done.stdout
    assert "if this disk failed that work is gone" in done.stdout, "the loud part is the point"
    assert fleet.head(fleet.main) == git(fleet.main, "rev-parse", "HEAD").stdout.strip()


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
    git(fleet.main, "config", "--add", "remote.origin.fetch",
        "+refs/heads/*:refs/remotes/origin/*")
    git(fleet.main, "config", "--add", "remote.origin.fetch",
        "+refs/notes/*:refs/notes/origin/*")
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


# ------------------------------------------------ the two tools have to agree (#573)
#
# `qb-doctor` is Python and `qb-catchup` is shell, so the logic is DUPLICATED and not
# shared: there is nothing a 5000-line Python module and a hook-budget shell script
# can import from one another, and a shell-out from the sweep would put a Python
# start-up per worktree inside a 20-second budget. What can be shared is the answer,
# and these two tests are what keep the duplicate honest — because two tools
# disagreeing about "does this work exist elsewhere" is worse than either being wrong
# on its own.


def test_both_tools_ask_git_the_same_question():
    catchup = (BIN / "qb-catchup").read_text()
    doctor = (BIN / "qb-doctor").read_text()
    assert '"$branch" --not --remotes --' in catchup, (
        "the sweep must ask `--not --remotes`, never `origin/<branch>`")
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


def test_a_fetch_that_failed_refuses_the_question_rather_than_answering_from_stale_refs(fleet):
    """The sweep still runs — acting on what is already here is the point — but a
    question measured against refs nobody refreshed is not answered."""
    git(fleet.main, "remote", "set-url", "origin", str(fleet.tmp / "gone.git"))
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run(fetch=True)
    assert done.returncode == 0, done.stderr
    assert "the fetch did not complete" in done.stdout, done.stdout
    assert "on no remote ref" not in done.stdout, "it answered out of a snapshot nobody refreshed"


def test_a_namespace_under_refs_remotes_that_is_not_a_remote_refuses_the_question(fleet):
    """The mirror image of the refspec check. The query trusts everything under
    `refs/remotes/`; only configured remotes are refreshed. A namespace left by a
    removed remote never self-corrects, because nothing will ever fetch it again."""
    git(fleet.main, "update-ref", "refs/remotes/ghost/main", "HEAD")
    commit(fleet.main, "mine-only", days_ago=19)

    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "`refs/remotes/ghost/` belongs to no configured remote" in done.stdout, done.stdout
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
