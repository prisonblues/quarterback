"""`qb-stash` — a stash that belongs to one worktree, pinned against real git.

The shared `refs/stash` is what makes `git stash` unsafe in this harness (#210),
and refusing it is only half an answer: agents and humans reach for a stash
because they need one. `qb-stash` is the other half — `git stash create` for the
snapshot, `refs/worktree/*` for the storage, which is the one ref namespace git
keeps per worktree.

The two things worth pinning are the isolation (a sibling can neither see nor pop
an entry) and the two limits `git stash create` imposes — no pathspec, no
untracked files — because a tool that quietly dropped either would be a worse
data-loss bug than the one it replaces.

Run: pytest harness/tests/test_qb_stash.py
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

QB_STASH = Path(__file__).resolve().parents[1] / "bin" / "qb-stash"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def env(home: Path) -> dict:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
            "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
            "GIT_CONFIG_SYSTEM": str(home / ".gitconfig-system")}


def git(cwd: Path, *args, home: Path, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env=env(home), check=check)


def qb(cwd: Path, *args, home: Path, check=True):
    return subprocess.run([str(QB_STASH), *args], cwd=cwd, capture_output=True,
                          text=True, env=env(home), check=check)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    (h / ".gitconfig").write_text("")
    (h / ".gitconfig-system").write_text("")
    return h


@pytest.fixture
def repo(tmp_path, home):
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True, env=env(home))
    git(main, "config", "user.email", "t@example.com", home=home)
    git(main, "config", "user.name", "T", home=home)
    (main / "f.txt").write_text("v1\n")
    git(main, "add", "f.txt", home=home)
    git(main, "commit", "-qm", "init", home=home)
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", str(wt), "-b", "side", home=home)
    return main, wt


def test_an_entry_is_invisible_to_a_sibling_worktree(repo, home):
    """The whole point. `refs/worktree/*` is per-worktree where `refs/stash` is
    not, so there is nothing for a sibling to list, and nothing to pop."""
    main, wt = repo
    (wt / "f.txt").write_text("side work\n")
    qb(wt, "push", "-m", "side work", home=home)

    assert "side work" in qb(wt, "list", home=home).stdout
    assert qb(main, "list", home=home).stdout.strip() == ""
    assert git(main, "stash", "list", home=home).stdout.strip() == "", (
        "qb-stash put something on the SHARED stack, which is the bug it exists "
        "to avoid")


def test_push_reverts_the_tree_and_pop_puts_it_back(repo, home):
    main, wt = repo
    (wt / "f.txt").write_text("side work\n")
    qb(wt, "push", "-m", "parked", home=home)
    assert (wt / "f.txt").read_text() == "v1\n"
    assert git(wt, "status", "--porcelain", home=home).stdout.strip() == ""

    qb(wt, "pop", home=home)
    assert (wt / "f.txt").read_text() == "side work\n"
    assert qb(wt, "list", home=home).stdout.strip() == ""


def test_the_staged_and_unstaged_split_survives_a_round_trip(repo, home):
    """Hand-recovery of the trees lost to #210 was lossy in exactly this way: a
    staged addition came back unstaged, which is easy to miss and easy to commit
    wrong. `--index` is what stops it."""
    main, wt = repo
    (wt / "s.txt").write_text("staged\n")
    git(wt, "add", "s.txt", home=home)
    (wt / "f.txt").write_text("unstaged\n")
    before = git(wt, "status", "--porcelain", home=home).stdout

    qb(wt, "push", "-m", "split", home=home)
    assert git(wt, "status", "--porcelain", home=home).stdout.strip() == ""
    qb(wt, "pop", "--index", home=home)
    assert git(wt, "status", "--porcelain", home=home).stdout == before


def test_untracked_files_are_left_in_the_tree_and_said_so(repo, home):
    """`git stash create` has no `-u` and ignores untracked files. Removing them
    anyway would destroy them; staying silent would let an agent believe they were
    saved. So they stay, and the omission is printed."""
    main, wt = repo
    (wt / "f.txt").write_text("tracked change\n")
    (wt / "new.py").write_text("brand new\n")
    r = qb(wt, "push", "-m", "mixed", home=home)

    assert (wt / "new.py").read_text() == "brand new\n"
    assert "new.py" in r.stderr and "NOT saved" in r.stderr
    assert (wt / "f.txt").read_text() == "v1\n"


def test_push_refuses_a_pathspec_rather_than_ignoring_it(repo, home):
    """A pathspec silently widened to the whole tree is the shape of a data-loss
    bug: `push -- f.txt` would revert everything else too."""
    main, wt = repo
    (wt / "f.txt").write_text("x\n")
    r = qb(wt, "push", "f.txt", home=home, check=False)
    assert r.returncode != 0
    assert "pathspec" in r.stderr
    assert (wt / "f.txt").read_text() == "x\n"


def test_push_refuses_the_untracked_flags_rather_than_ignoring_them(repo, home):
    main, wt = repo
    (wt / "f.txt").write_text("x\n")
    for flag in ("-u", "--include-untracked", "-a"):
        r = qb(wt, "push", flag, home=home, check=False)
        assert r.returncode != 0, flag
        assert "not supported" in r.stderr, flag
    assert (wt / "f.txt").read_text() == "x\n"


def test_push_with_nothing_to_save_says_so_and_creates_no_entry(repo, home):
    main, wt = repo
    r = qb(wt, "push", home=home)
    assert "No local changes" in r.stdout
    assert qb(wt, "list", home=home).stdout.strip() == ""


def test_pop_keeps_the_entry_when_the_apply_conflicts(repo, home):
    """A conflicted pop that also dropped the entry is how a recoverable mess
    becomes an unrecoverable one."""
    main, wt = repo
    (wt / "f.txt").write_text("parked\n")
    qb(wt, "push", "-m", "parked", home=home)
    (wt / "f.txt").write_text("something else since\n")

    r = qb(wt, "pop", home=home, check=False)
    assert r.returncode != 0
    assert "kept" in r.stderr
    assert "parked" in qb(wt, "list", home=home).stdout


def test_entries_are_addressable_by_index_newest_first(repo, home):
    main, wt = repo
    for n in ("first", "second", "third"):
        (wt / "f.txt").write_text(n + "\n")
        qb(wt, "push", "-m", n, home=home)
    listing = qb(wt, "list", home=home).stdout.splitlines()
    assert len(listing) == 3
    assert "third" in listing[0] and "first" in listing[2]

    qb(wt, "apply", "2", home=home)
    assert (wt / "f.txt").read_text() == "first\n"


def test_a_missing_entry_is_an_error_not_a_silent_no_op(repo, home):
    main, wt = repo
    r = qb(wt, "pop", "7", home=home, check=False)
    assert r.returncode != 0
    assert "no qb-stash entry" in r.stderr


def test_drop_and_clear_remove_entries(repo, home):
    main, wt = repo
    for n in ("a", "b"):
        (wt / "f.txt").write_text(n + "\n")
        qb(wt, "push", "-m", n, home=home)
    qb(wt, "drop", "0", home=home)
    assert len(qb(wt, "list", home=home).stdout.splitlines()) == 1
    qb(wt, "clear", home=home)
    assert qb(wt, "list", home=home).stdout.strip() == ""


def test_two_pushes_in_the_same_second_are_two_entries(repo, home):
    """The first draft named entries `<epoch>-<pid>`, so a second push inside the
    same second wrote the SAME ref and silently replaced the first — a data-loss
    bug inside the tool built to prevent one. `test_entries_are_addressable...`
    caught it as an ordering failure; this names it."""
    main, wt = repo
    for n in ("one", "two", "three"):
        (wt / "f.txt").write_text(n + "\n")
        qb(wt, "push", "-m", n, home=home)
    refs = git(wt, "for-each-ref", "--format=%(refname)", "refs/worktree/qb-stash/",
               home=home).stdout.split()
    assert len(refs) == 3, f"entries collided: {refs}"


def test_it_works_under_the_shared_stash_guard(repo, home):
    """`qb-stash` is what the guard's message tells people to use, so it had
    better not trip over it. `git stash create` mints the commit without writing
    `refs/stash`, and `refs/worktree/*` is waved straight through."""
    main, wt = repo
    qb_hooks = QB_STASH.parent / "qb-hooks"
    subprocess.run([str(qb_hooks), "install", "--repo", str(main)],
                   capture_output=True, text=True, env=env(home), check=True)
    (wt / "f.txt").write_text("guarded work\n")
    qb(wt, "push", "-m", "under the guard", home=home)
    assert (wt / "f.txt").read_text() == "v1\n"
    qb(wt, "pop", home=home)
    assert (wt / "f.txt").read_text() == "guarded work\n"


def test_push_refuses_mid_merge_rather_than_discarding_the_merge(repo, home):
    """The revert `push` does is `reset --hard`, which would take the in-progress
    merge with it."""
    main, wt = repo
    git(wt, "checkout", "-q", "-b", "other", home=home)
    (wt / "f.txt").write_text("theirs\n")
    git(wt, "commit", "-qam", "theirs", home=home)
    git(wt, "checkout", "-q", "side", home=home)
    (wt / "f.txt").write_text("ours\n")
    git(wt, "commit", "-qam", "ours", home=home)
    git(wt, "merge", "other", home=home, check=False)

    r = qb(wt, "push", "-m", "nope", home=home, check=False)
    assert r.returncode != 0
    assert "in progress" in r.stderr
    assert (Path(git(wt, "rev-parse", "--absolute-git-dir", home=home).stdout.strip())
            / "MERGE_HEAD").exists(), "the merge state was discarded anyway"
