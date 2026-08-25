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
import shutil
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
CATCHUP = BIN / "qb-catchup"


def git(where, *args, check=True):
    return subprocess.run(["git", "-C", str(where), *args],
                          capture_output=True, text=True, check=check)


def commit(where, name, text="x"):
    (Path(where) / name).write_text(text)
    git(where, "add", name)
    git(where, "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-qm", name)


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

        def run(self, *args, cwd=None):
            env = dict(os.environ)
            env["PATH"] = f"{stub_dir}:{env['PATH']}"
            return subprocess.run(
                [str(CATCHUP), "-C", str(cwd or self.main), "--no-fetch", *args],
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


def test_unpushed_commits_are_left_alone_and_said_loudly(fleet):
    """The state #45 was actually in, and the one thing here that cannot be
    reconstructed from the remote."""
    commit(fleet.main, "mine-only")
    done = fleet.run()
    assert done.returncode == 0, done.stderr
    assert "unpushed" in done.stdout, done.stdout
    assert "exist nowhere else" in done.stdout, "the loud part is the point"


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
