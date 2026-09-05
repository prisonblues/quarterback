"""The backup `remove-worktree` takes before it deletes a worktree (#743).

Teardown removes the directory, the docker stack, the database and the local
branch, and the tarball it writes first is the only thing standing between a
mistake and a recoverable one. It had three holes, and they compounded:

* `git status --porcelain` with no `--ignored` cannot see the ignored files,
  which in a real worktree are `.env`, `data/`, `CLAUDE.local.md`,
  `.worktree-port` and `.claude/` — the half that is in no other copy. What it
  DID archive was tracked-and-modified files, every one of which is either
  committed elsewhere or trivially remade.
* the failure branch was `|| echo "Backup failed"` with no `exit`, so the
  recursive delete on the next lines ran anyway. The one run where the backup
  matters is the one where it was skipped.
* `substr($0, 4)` on the porcelain line handles a space and nothing else. git
  QUOTES and C-escapes a path holding a quote, a newline or a non-ASCII byte, so
  `café.txt` reached tar as the eleven literal characters `caf\\303\\251.txt`
  and was silently dropped from the archive.

These drive the real script against real throwaway repos: the subject is what
git actually emits for a rename, a deletion and a non-ASCII name, and no mock of
mine would have predicted the `-z` rename record.

Run: pytest harness/tests
"""

import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_sandbox  # noqa: E402

BIN = Path(__file__).resolve().parent.parent / "bin"
REMOVE = BIN / "remove-worktree"

#: As `test_remove_worktree_branch_guard.py`'s list, plus what the backup itself
#: reaches for: `tar` to write the archive, `mktemp` for the NUL-delimited file
#: list, and `curl` so `worktree-holder` gets as far as its "no board
#: configured" answer rather than stopping one step earlier at "no curl". A name
#: MISSING here reads to the script as "that tool is broken", which the test
#: would then report as the behaviour of the guard.
TOOLS = ("git", "bash", "sh", "awk", "sed", "grep", "tr", "cat", "head", "tail",
         "wc", "date", "basename", "dirname", "rm", "mkdir", "env", "timeout",
         "jq", "chmod", "find", "sort", "mv", "ln", "readlink", "tar", "mktemp",
         "curl", "gzip")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=False)


@pytest.fixture
def repo(tmp_path):
    """A project that ignores `.env` and `data/`, left off its default branch.

    The `.gitignore` is the fixture's whole point: an ignored file is the thing
    the old backup could not see, and a repo without one passes just as happily
    with `--ignored` removed again.
    """
    main = tmp_path / "proj"
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(main)], check=True)
    git(main, "config", "user.email", "t@example.com")
    git(main, "config", "user.name", "t")
    (main / ".gitignore").write_text(".env\ndata/\n")
    (main / "README.md").write_text("hi\n")
    (main / "doomed.txt").write_text("bye\n")
    git(main, "add", "-A")
    git(main, "commit", "--quiet", "-m", "init")
    # Off main, so a worktree is free to take it and the teardown's own default
    # branch guard is not what is under test here.
    assert git(main, "checkout", "--quiet", "-b", "wip/current").returncode == 0
    return main


@pytest.fixture
def worktree(repo):
    """A linked worktree at the path `remove-worktree fix-issue-43` derives."""
    wt = repo.parent / "proj-fix-issue-43"
    assert git(repo, "worktree", "add", "--quiet", "-b", "fix/issue-43",
               str(wt)).returncode == 0
    return wt


def run_remove(repo, tmp_path, *args, path_extra=(), **over):
    """The real script, with a `PATH` and a `HOME` this test owns.

    Both halves matter and neither substitutes for the other — see
    `_path_sandbox` and #528. Here the credential half is doing real work: with
    no board reachable, `worktree-holder` answers "could not tell", which is the
    permissive default these backup tests want out of the way.
    """
    env = _path_sandbox.sandbox_env(tmp_path, *path_extra, tools=TOOLS, **over)
    return subprocess.run([str(REMOVE), *args], cwd=repo, capture_output=True,
                          text=True, env=env, check=False)


def backups(repo):
    return sorted(repo.parent.glob("proj-fix-issue-43-backup-*.tar.gz"))


def archived(repo):
    """Every member name in the one backup this teardown wrote."""
    found = backups(repo)
    assert len(found) == 1, f"expected exactly one backup, got {found}"
    with tarfile.open(found[0]) as tf:
        return set(tf.getnames())


def test_an_ignored_file_is_in_the_backup(repo, worktree, tmp_path):
    """`.env` and `data/` are the files whose loss actually costs something."""
    (worktree / ".env").write_text("SECRET=1\n")
    (worktree / "data").mkdir()
    (worktree / "data" / "blob.bin").write_text("payload\n")

    proc = run_remove(repo, tmp_path, "fix-issue-43")

    assert not worktree.exists(), f"teardown did not finish:\n{proc.stdout}"
    names = archived(repo)
    assert ".env" in names, f"the ignored .env was not backed up: {names}"
    assert "data/blob.bin" in names, f"the ignored data/ was not backed up: {names}"


def test_a_path_needing_quoting_is_collected(repo, worktree, tmp_path):
    """git quotes these on the porcelain line; `-z` is what makes them real.

    Three shapes in one archive, because they fail for one reason: a non-ASCII
    byte (C-escaped to `\\303\\251`), a double quote (which makes git wrap the
    WHOLE path in quotes), and a plain space (the only one the old `substr`
    handled).
    """
    (worktree / "café.txt").write_text("accent\n")
    (worktree / 'we"ird.txt').write_text("quote\n")
    (worktree / "sp ace.txt").write_text("space\n")

    proc = run_remove(repo, tmp_path, "fix-issue-43")

    assert not worktree.exists(), f"teardown did not finish:\n{proc.stdout}"
    names = archived(repo)
    for want in ("café.txt", 'we"ird.txt', "sp ace.txt"):
        assert want in names, f"{want!r} missing from the backup: {names}"
    # And the escaped spelling is not in there under a name nobody can find.
    assert not [n for n in names if "\\303" in n], names


def test_a_renamed_file_contributes_only_its_new_name(repo, worktree, tmp_path):
    """Under `-z` a rename is two records, and the second is a bare old path.

    Read as a status line it would lose its first three characters, so the
    archive would carry a file called `DME.md` — or fail to stat one, which
    (now that a failed backup aborts) would refuse the teardown outright.
    """
    assert git(worktree, "mv", "README.md", "RENAMED.md").returncode == 0

    proc = run_remove(repo, tmp_path, "fix-issue-43")

    assert not worktree.exists(), f"teardown did not finish:\n{proc.stdout}"
    names = archived(repo)
    assert "RENAMED.md" in names, names
    assert "README.md" not in names, names


def test_a_deleted_file_does_not_fail_the_backup(repo, worktree, tmp_path):
    """`tar -T` fails the whole archive on one entry it cannot stat.

    A `git rm` is an ordinary state for a worktree being torn down, and now that
    a failed backup aborts, leaving deletions in the list would refuse those
    teardowns. A guard that fires on the safe case is a guard somebody disables.
    """
    assert git(worktree, "rm", "--quiet", "doomed.txt").returncode == 0
    (worktree / ".env").write_text("SECRET=1\n")

    proc = run_remove(repo, tmp_path, "fix-issue-43")

    assert not worktree.exists(), (
        f"a deleted file blocked the teardown:\n{proc.stdout}\n{proc.stderr}")
    assert ".env" in archived(repo)


def test_a_failed_backup_aborts_the_teardown(repo, worktree, tmp_path):
    """The whole point: no archive, no delete.

    `tar` is stubbed rather than the filesystem broken, because the failure
    being modelled is "the archive did not get written" and that has many
    causes — full disk, an unarchivable file, a tar that is not GNU. The script
    cannot tell them apart and must not have to.
    """
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "tar").write_text("#!/bin/sh\nexit 2\n")
    (stub / "tar").chmod(0o755)
    (worktree / ".env").write_text("SECRET=1\n")

    proc = run_remove(repo, tmp_path, "fix-issue-43", path_extra=(stub,))

    assert proc.returncode != 0, proc.stdout
    assert worktree.is_dir(), (
        f"the worktree was deleted after the backup failed — #743's whole "
        f"point:\n{proc.stdout}\n{proc.stderr}")
    assert (worktree / ".env").read_text() == "SECRET=1\n"
    assert "Backup" in proc.stderr and "failed" in proc.stderr
    # No half-written tarball left behind to be mistaken for a good one.
    assert backups(repo) == []
    # And the branch — the other thing a teardown destroys — survives with it.
    assert git(repo, "rev-parse", "--verify", "fix/issue-43").returncode == 0


def test_no_backup_is_the_escape_hatch_for_a_backup_that_cannot_succeed(
        repo, worktree, tmp_path):
    """Aborting on failure would otherwise make a worktree un-removable.

    A tree whose contents cannot be archived at all — an unwritable parent, a
    file tar refuses — would be stuck forever behind a guard with no way past
    it, and the way past a guard has to be a flag rather than a hand-edit.
    """
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "tar").write_text("#!/bin/sh\nexit 2\n")
    (stub / "tar").chmod(0o755)
    (worktree / ".env").write_text("SECRET=1\n")

    proc = run_remove(repo, tmp_path, "--no-backup", "fix-issue-43",
                      path_extra=(stub,))

    assert not worktree.exists(), f"{proc.stdout}\n{proc.stderr}"
    assert backups(repo) == []


def test_a_clean_worktree_writes_no_backup_at_all(repo, worktree, tmp_path):
    """`--ignored` must not turn every teardown into a tarball.

    Nothing dirty and nothing ignored means nothing to lose, and an archive per
    teardown is how a directory fills with files nobody ever reads.
    """
    proc = run_remove(repo, tmp_path, "fix-issue-43")

    assert not worktree.exists(), f"{proc.stdout}\n{proc.stderr}"
    assert backups(repo) == []
