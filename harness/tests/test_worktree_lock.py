"""The worktree lock: a teardown's reading of a tree stays true until it acts (#743).

`remove-worktree` deletes a worktree, its docker stack, its database and its
local branch. It asks `worktree-holder` who is in there first, and until now that
answer was all it had — a read taken seconds to minutes before the delete landed,
with a `gh` call, a `docker compose down` and an nginx restart in between. Nothing
stopped a second teardown starting in that gap, and nothing stopped an agent being
handed the tree in it. What that produces is not a merge conflict; it is a working
directory that vanishes underneath a running process.

`harness/bin/worktree-lock` is the latch. `create-worktree` and `remove-worktree`
take it over the worktree's path and hold it for their whole run, and
`worktree-lock --enter` takes it to write the session marker that makes an agent
visible to `worktree-holder` at all.

What each test here is for, and what breaking the code does to it:

* **the key** — the one property that, if it quietly failed, would make every
  other test here pass while the lock protected nothing: two callers spelling the
  same worktree differently must land on the same lockfile. `create-worktree`
  locks a directory that does not exist yet and `remove-worktree` one that does,
  so the derivation canonicalises the PARENT rather than the target.
* **exclusion** — two real teardowns, and a teardown against a live third-party
  holder. Reverting `acquire_the_lock`'s call site turns these red.
* **death** — a killed holder frees the tree with nobody intervening. This is the
  acceptance criterion the issue proposed a TTL for; `flock` gives it for free,
  and better, because the kernel drops the lock the instant the holder dies.
* **the fail-open default** — Rich's standing steer, and the thing this change is
  most likely to break: a host with no `flock` must still tear a worktree down.
  Only `--require-lock` refuses.

Run: pytest harness/tests
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_sandbox  # noqa: E402

BIN = Path(__file__).resolve().parent.parent / "bin"
REMOVE = BIN / "remove-worktree"
LOCK = BIN / "worktree-lock"

#: `flock` and `sha256sum` are in here because the LOCKED path is the subject.
#: The suite makes them absent deliberately, one test at a time, by naming
#: `TOOLS_UNLOCKABLE` instead.
TOOLS = ("git", "bash", "sh", "awk", "sed", "grep", "tr", "cat", "head", "tail",
         "wc", "date", "basename", "dirname", "rm", "mkdir", "env", "timeout",
         "jq", "chmod", "find", "sort", "mv", "ln", "readlink", "tar", "mktemp",
         "curl", "gzip", "sleep", "tee", "flock", "sha256sum")

#: A host that cannot lock at all: no `flock`. Everything else is still here, so
#: a test using this is about the lock's absence and nothing else.
TOOLS_UNLOCKABLE = tuple(t for t in TOOLS if t != "flock")

#: The interpreter for the runtime stub below, by absolute path.
BASH = shutil.which("bash")

if shutil.which("flock") is None:  # pragma: no cover - platform guard
    pytest.skip("flock is not on this host, so the lock cannot be exercised",
                allow_module_level=True)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=False)


@pytest.fixture
def repo(tmp_path):
    main = tmp_path / "proj"
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(main)], check=True)
    git(main, "config", "user.email", "t@example.com")
    git(main, "config", "user.name", "t")
    (main / "README.md").write_text("hi\n")
    git(main, "add", "-A")
    git(main, "commit", "--quiet", "-m", "init")
    assert git(main, "checkout", "--quiet", "-b", "wip/current").returncode == 0
    return main


@pytest.fixture
def worktree(repo):
    wt = repo.parent / "proj-fix-issue-43"
    assert git(repo, "worktree", "add", "--quiet", "-b", "fix/issue-43",
               str(wt)).returncode == 0
    return wt


def holder_stub(tmp_path, code, delay=0):
    """A `worktree-holder` on PATH answering `code`, optionally slowly.

    The delay is how this suite makes a teardown take measurable time inside the
    lock without stubbing anything the teardown depends on for its result.
    """
    d = tmp_path / f"holder-stub-{code}-{delay}"
    d.mkdir(exist_ok=True)
    (d / "worktree-holder").write_text(
        f"#!/bin/sh\n{'sleep %d\n' % delay if delay else ''}exit {code}\n")
    (d / "worktree-holder").chmod(0o755)
    return d


def env_for(tmp_path, *path_extra, tools=TOOLS, **over):
    return _path_sandbox.sandbox_env(tmp_path, *path_extra, tools=tools, **over)


def run_remove(repo, tmp_path, *args, path_extra=(), tools=TOOLS, script=REMOVE,
               **over):
    return subprocess.run([str(script), *args], cwd=repo, capture_output=True,
                          text=True, env=env_for(tmp_path, *path_extra, tools=tools,
                                                 **over), check=False)


def lock_path(tmp_path, target, repo, tools=TOOLS, **over):
    """What `worktree-lock --path` says `target` keys to, given that repository.

    `--repo` is not decoration: the lockfile lives in the repository's own common
    git directory, and `create-worktree` asks this question about a worktree that
    does not exist yet and so cannot answer it from the target alone.
    """
    proc = subprocess.run([str(LOCK), "--repo", str(repo), "--path", str(target)],
                          capture_output=True, text=True, check=False,
                          env=env_for(tmp_path, tools=tools, **over))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return proc.stdout.strip()


def hold_the_lock(tmp_path, target, repo, seconds=60, tools=TOOLS, **over):
    """A live process holding `target`'s lock, through the library the scripts use.

    It `exec`s `sleep`, so the process holding the descriptor IS the process this
    returns — which is what lets the death test kill the holder and nothing else.
    """
    # The interpreter by ABSOLUTE PATH, resolved here rather than spelled
    # `/usr/bin/env bash`: there is no `/usr/bin/env` inside a nix build sandbox,
    # so a stub written at runtime with that shebang cannot exec there (#177, and
    # `test_runtime_stub_shebangs.py` is the guard that says so). `bash` and not
    # `sh`, because the library this sources uses `{fd}>` redirections.
    script = tmp_path / "hold-it"
    script.write_text(
        f"#!{BASH}\n"
        f'. "{LOCK}"\n'
        'worktree_lock_acquire "$1" 1 "$3" || exit 9\n'
        'echo ready\n'
        'exec sleep "$2"\n')
    script.chmod(0o755)
    proc = subprocess.Popen([str(script), str(target), str(seconds), str(repo)],
                            stdout=subprocess.PIPE, text=True,
                            env=env_for(tmp_path, tools=tools, **over))
    line = proc.stdout.readline()
    assert line.strip() == "ready", f"the holder never took the lock: {line!r}"
    return proc


# --------------------------------------------------------------------------
# The key. Everything else here is worthless if two callers disagree about it.

def test_a_tree_that_does_not_exist_yet_keys_the_same_lock_as_the_tree_that_does(
        repo, tmp_path):
    """`create-worktree` locks before the directory exists; `remove-worktree` after.

    Canonicalising the TARGET would give those two different answers under a
    symlinked parent, so the two sides of the race would take different locks and
    the whole mechanism would be inert while every other test still passed. The
    parent is what gets canonicalised, and the parent exists in both cases.
    """
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real)

    # The absent spellings FIRST, and one of them through the symlink — that is
    # the combination the mutant survives if only the target is canonicalised.
    # `create-worktree` is exactly this caller: a path built by string
    # concatenation onto a repo's parent, for a directory that is not there yet.
    absent = lock_path(tmp_path, real / "proj-fix-issue-43", repo)
    absent_via_symlink = lock_path(tmp_path, tmp_path / "alias" / "proj-fix-issue-43",
                                   repo)
    (real / "proj-fix-issue-43").mkdir()
    present = lock_path(tmp_path, real / "proj-fix-issue-43", repo)
    present_via_symlink = lock_path(tmp_path, tmp_path / "alias" / "proj-fix-issue-43",
                                    repo)
    trailing_slash = lock_path(tmp_path, str(real / "proj-fix-issue-43") + "/", repo)

    assert (absent == absent_via_symlink == present == present_via_symlink
            == trailing_slash), (
        "the same worktree spelled five ways took up to five different locks:\n"
        f"  absent             {absent}\n"
        f"  absent via symlink {absent_via_symlink}\n"
        f"  present            {present}\n"
        f"  present via alias  {present_via_symlink}\n"
        f"  trailing slash     {trailing_slash}")


def test_two_different_worktrees_do_not_share_a_lock(repo, tmp_path):
    """The other direction: a lock that serialised the whole box would be a bug
    that only ever shows up as everything being slow."""
    assert lock_path(tmp_path, tmp_path / "proj-a", repo) != \
           lock_path(tmp_path, tmp_path / "proj-b", repo)


def test_the_lockfile_is_named_after_the_worktree_as_well_as_hashed(repo, tmp_path):
    """A directory of bare digests is a directory nobody can debug."""
    name = Path(lock_path(tmp_path, tmp_path / "proj-fix-issue-43", repo)).name
    assert name.startswith("proj-fix-issue-43-")


def test_the_lockfile_lives_in_the_repository_not_in_the_environment(repo, worktree,
                                                                     tmp_path):
    """Where it is, said out loud, because WHERE is the whole of the last defect."""
    assert lock_path(tmp_path, worktree, repo).startswith(
        str(repo / ".git" / "quarterback" / "worktree"))


# --------------------------------------------------------------------------
# THE ROOT MUST NOT COME FROM THE CALLER. This is the class the first cut of this
# change shipped: the lock DIRECTORY was chosen from `$XDG_RUNTIME_DIR` when it
# was set and `$TMPDIR` when it was not, which is a fact about who is calling
# rather than about the tree. An interactive agent has a runtime directory and a
# systemd unit does not, so the hourly reaper and the agent it was racing computed
# the same key, the same digest, and two different files — inert for exactly the
# pairing #743 is about. Every test passed, because the harness hands both sides
# of every test one environment.
#
# So these run the two sides with DIFFERENT ambient environments on purpose.

def unattended(env):
    """The environment of a systemd unit: no runtime directory, its own TMPDIR."""
    env = dict(env)
    env.pop("XDG_RUNTIME_DIR", None)
    env["TMPDIR"] = env["HOME"] + "/private-tmp"
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    return env


def test_an_attended_and_an_unattended_caller_key_the_same_lockfile(
        repo, worktree, tmp_path):
    attended = env_for(tmp_path)
    timer = unattended(attended)

    def ask(env):
        proc = subprocess.run([str(LOCK), "--repo", str(repo), "--path", str(worktree)],
                              capture_output=True, text=True, env=env, check=False)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        return proc.stdout.strip()

    assert ask(attended) == ask(timer), (
        "an agent and a timer locked two different files for one worktree")


def test_an_unattended_teardown_is_blocked_by_an_attended_holder(
        repo, worktree, tmp_path):
    """The behaviour the test above is only a proxy for, driven end to end.

    A holder in an ordinary agent environment; a teardown run the way a systemd
    unit runs one — no `$XDG_RUNTIME_DIR`, a private `$TMPDIR` — with
    `--require-lock`, which is what `stack-reaper` passes. It must refuse.
    """
    holder = hold_the_lock(tmp_path, worktree, repo)
    try:
        proc = subprocess.run(
            [str(REMOVE), "--require-lock", "--lock-wait", "1", "fix-issue-43"],
            cwd=repo, capture_output=True, text=True, check=False,
            env=unattended(env_for(tmp_path, holder_stub(tmp_path, 0))))

        assert proc.returncode != 0, (
            f"a timer deleted a worktree an agent was holding the lock on:\n"
            f"{proc.stdout}\n{proc.stderr}")
        assert worktree.is_dir()
        assert "already holds" in proc.stderr
    finally:
        holder.kill()
        holder.wait(timeout=10)


@pytest.mark.parametrize("spelling", ["{}/", "{}//", "{}/.", "{}/./", "{}/sub/.."])
def test_dot_segments_and_extra_slashes_key_the_same_lock(repo, worktree, tmp_path,
                                                          spelling):
    """`--enter` is a CLI a slash command drives, so its argument is somebody's
    prose. `${1%/}` took one trailing slash off and nothing else."""
    (worktree / "sub").mkdir(exist_ok=True)
    plain = lock_path(tmp_path, worktree, repo)
    assert lock_path(tmp_path, spelling.format(worktree), repo) == plain


# --------------------------------------------------------------------------
# Exclusion — the first acceptance criterion.

def test_two_teardowns_of_one_worktree_cannot_interleave(repo, worktree, tmp_path):
    """Two real `remove-worktree` runs, started against the same tree.

    The first is made slow inside the lock by a `worktree-holder` that sleeps; the
    second is given one second to wait. Exactly one of them may destroy anything.
    """
    slow = subprocess.Popen(
        [str(REMOVE), "fix-issue-43"], cwd=repo, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
        env=env_for(tmp_path, holder_stub(tmp_path, 0, delay=3)))
    try:
        time.sleep(0.7)
        second = run_remove(repo, tmp_path, "--lock-wait", "1", "fix-issue-43",
                            path_extra=(holder_stub(tmp_path, 0),))
        assert second.returncode != 0, (
            f"the second teardown ran straight through the first:\n"
            f"{second.stdout}\n{second.stderr}")
        assert "already holds" in second.stderr
        assert "Nothing has been deleted" in second.stderr
    finally:
        out, err = slow.communicate(timeout=60)

    assert not worktree.exists(), (
        f"the first teardown did not finish:\n{out}\n{err}")


def test_the_lock_is_still_held_deep_into_the_teardown(repo, worktree, tmp_path):
    """Not merely across the holder check — across the destructive half too.

    `mktemp` is the backup step's first call, which is STEP 4, after the stack has
    been taken down and the nginx block removed. A lock released once the checks
    were done would leave the widest part of the window open, and this is the test
    that would still be green if it were.
    """
    real_mktemp = shutil.which("mktemp")
    stub = tmp_path / "slow-mktemp"
    stub.mkdir()
    (stub / "mktemp").write_text(f"#!/bin/sh\nsleep 3\nexec {real_mktemp} \"$@\"\n")
    (stub / "mktemp").chmod(0o755)
    (worktree / ".env").write_text("SECRET=1\n")   # something to back up

    slow = subprocess.Popen(
        [str(REMOVE), "fix-issue-43"], cwd=repo, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
        env=env_for(tmp_path, stub, holder_stub(tmp_path, 0)))
    try:
        time.sleep(1.5)          # past the checks, inside the backup
        second = run_remove(repo, tmp_path, "--lock-wait", "1", "fix-issue-43",
                            path_extra=(holder_stub(tmp_path, 0),))
        assert second.returncode != 0, (
            f"the lock was gone by the backup step:\n{second.stdout}\n{second.stderr}")
        assert "already holds" in second.stderr
    finally:
        slow.communicate(timeout=60)


def test_a_lock_held_by_a_live_process_blocks_the_teardown(repo, worktree, tmp_path):
    holder = hold_the_lock(tmp_path, worktree, repo)
    try:
        proc = run_remove(repo, tmp_path, "--lock-wait", "1", "fix-issue-43",
                          path_extra=(holder_stub(tmp_path, 0),))
        assert proc.returncode != 0
        assert worktree.is_dir(), (
            f"a locked worktree was deleted:\n{proc.stdout}\n{proc.stderr}")
        assert git(repo, "rev-parse", "--verify", "fix/issue-43").returncode == 0, \
            "the branch went even though the teardown was refused"
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_the_refusal_names_who_is_holding_it(repo, worktree, tmp_path):
    """A refusal you cannot act on is a refusal somebody works around."""
    holder = hold_the_lock(tmp_path, worktree, repo)
    try:
        proc = run_remove(repo, tmp_path, "--lock-wait", "1", "fix-issue-43",
                          path_extra=(holder_stub(tmp_path, 0),))
        assert f"pid {holder.pid}" in proc.stderr, proc.stderr
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_force_does_not_beat_the_lock(repo, worktree, tmp_path):
    """--force means "I have looked at the tree and the agent is done".

    It has never meant "run two recursive deletes over one directory at once",
    and there is no reading of the flag under which that is what the user wanted.
    """
    holder = hold_the_lock(tmp_path, worktree, repo)
    try:
        proc = run_remove(repo, tmp_path, "--force", "--lock-wait", "1",
                          "fix-issue-43")
        assert proc.returncode != 0
        assert worktree.is_dir(), f"{proc.stdout}\n{proc.stderr}"
    finally:
        holder.kill()
        holder.wait(timeout=10)


# --------------------------------------------------------------------------
# Death — the criterion the issue wanted a TTL for.

def test_a_lock_whose_holder_was_killed_does_not_block(repo, worktree, tmp_path):
    """`flock` lives on an open descriptor, so the kernel frees it when the holder
    dies. No TTL, no reaper, no window in which a dead session still owns a tree —
    which is what a TTL would have left, for the length of its term."""
    holder = hold_the_lock(tmp_path, worktree, repo)
    os.kill(holder.pid, signal.SIGKILL)
    holder.wait(timeout=10)

    proc = run_remove(repo, tmp_path, "--lock-wait", "1", "fix-issue-43",
                      path_extra=(holder_stub(tmp_path, 0),))

    assert not worktree.exists(), (
        f"a killed holder's lock outlived it:\n{proc.stdout}\n{proc.stderr}")


def test_a_lockfile_left_behind_by_a_finished_run_does_not_block(
        repo, worktree, tmp_path):
    """Lockfiles are never unlinked — unlinking one while holding a flock on it is
    how two processes come to hold the same lock. So the file from yesterday's
    teardown is still there, and it must mean nothing."""
    stale = Path(lock_path(tmp_path, worktree, repo))
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("pid 999999 · remove-worktree · zeus · since 1999-01-01 00:00:00\n")

    proc = run_remove(repo, tmp_path, "--lock-wait", "1", "fix-issue-43",
                      path_extra=(holder_stub(tmp_path, 0),))

    assert not worktree.exists(), (
        f"an abandoned lockfile blocked a teardown:\n{proc.stdout}\n{proc.stderr}")


# --------------------------------------------------------------------------
# The fail-open default, which is the thing this change must not cost.

def test_a_host_with_no_flock_still_tears_a_worktree_down(repo, worktree, tmp_path):
    """The standing steer: a coordination tool that cannot be reached must never
    make a worktree unusable. `flock` is one more such tool."""
    proc = run_remove(repo, tmp_path, "fix-issue-43", tools=TOOLS_UNLOCKABLE,
                      path_extra=(holder_stub(tmp_path, 0),))

    assert not worktree.exists(), (
        f"a host with no flock could not tear down a worktree:\n"
        f"{proc.stdout}\n{proc.stderr}")


def test_a_host_with_no_flock_says_so(repo, worktree, tmp_path):
    """Silently unlocked is how a box comes to have #743 back without anybody
    noticing."""
    proc = run_remove(repo, tmp_path, "fix-issue-43", tools=TOOLS_UNLOCKABLE,
                      path_extra=(holder_stub(tmp_path, 0),))
    assert "without a worktree lock" in proc.stderr


def test_a_host_with_no_worktree_lock_installed_still_tears_down(
        repo, worktree, tmp_path):
    """An older harness, or a partial install. Same rule."""
    lone = tmp_path / "lonely-bin"
    lone.mkdir()
    shutil.copy(REMOVE, lone / "remove-worktree")
    # The copy defeats `${0%/*}/worktree-lock`; the sandbox PATH defeats
    # `command -v`. Both, because either alone leaves the real tool reachable on
    # a host where the harness is installed (#385, #472, #528).
    proc = run_remove(repo, tmp_path, "fix-issue-43", script=lone / "remove-worktree",
                      path_extra=(holder_stub(tmp_path, 0),))

    assert not worktree.exists(), f"{proc.stdout}\n{proc.stderr}"
    assert "not installed here" in proc.stderr


def test_require_lock_refuses_a_host_that_cannot_lock(repo, worktree, tmp_path):
    """The unattended half. `--require-lock` has been named for a lock since #760
    shipped it against the holder check alone; this is the lock it names."""
    proc = run_remove(repo, tmp_path, "--require-lock", "fix-issue-43",
                      tools=TOOLS_UNLOCKABLE, path_extra=(holder_stub(tmp_path, 0),))

    assert proc.returncode != 0
    assert worktree.is_dir(), f"{proc.stdout}\n{proc.stderr}"
    assert "Could not lock" in proc.stderr
    assert git(repo, "rev-parse", "--verify", "fix/issue-43").returncode == 0


def test_qb_unattended_is_the_environment_spelling_of_it_here_too(
        repo, worktree, tmp_path):
    proc = run_remove(repo, tmp_path, "fix-issue-43", tools=TOOLS_UNLOCKABLE,
                      path_extra=(holder_stub(tmp_path, 0),), QB_UNATTENDED="1")

    assert proc.returncode != 0
    assert worktree.is_dir(), f"{proc.stdout}\n{proc.stderr}"


def test_a_locked_host_with_require_lock_still_tears_down_a_free_worktree(
        repo, worktree, tmp_path):
    """Strict must not collapse into "never delete anything" — that is the reaper
    switched off, quietly."""
    proc = run_remove(repo, tmp_path, "--require-lock", "fix-issue-43",
                      path_extra=(holder_stub(tmp_path, 0),))

    assert not worktree.exists(), f"{proc.stdout}\n{proc.stderr}"


# --------------------------------------------------------------------------
# The entry side — the second acceptance criterion.

def run_enter(tmp_path, target, *args, session="s-1111", tools=TOOLS, **over):
    return subprocess.run([str(LOCK), *args, "--enter", str(target)],
                          capture_output=True, text=True, check=False,
                          env=env_for(tmp_path, tools=tools,
                                      CLAUDE_CODE_SESSION_ID=session, **over))


def marker_of(tmp_path, session="s-1111"):
    env = env_for(tmp_path)
    return Path(env["XDG_CACHE_HOME"]) / "claude-code" / "session-cwd" / session


def test_entering_a_worktree_records_the_session_where_worktree_holder_reads_it(
        worktree, tmp_path):
    """The marker's CONTENT is the contract: `worktree-holder` compares it against
    the worktree path, so a marker holding anything else is a session that no
    teardown on this box can see."""
    proc = run_enter(tmp_path, worktree)

    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert marker_of(tmp_path).read_text() == str(worktree)


def test_entering_waits_for_a_teardown_rather_than_racing_it(repo, worktree,
                                                            tmp_path):
    """The ordering that used to be possible and now is not: the marker landing
    while a teardown is between its check and its delete."""
    holder = hold_the_lock(tmp_path, worktree, repo)
    try:
        proc = run_enter(tmp_path, worktree, "--wait", "1")
        assert proc.returncode == 5, f"{proc.stdout}\n{proc.stderr}"
        assert "locked by another process" in proc.stderr
        assert not marker_of(tmp_path).exists(), (
            "the marker was written while another process held the tree")
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_entering_a_tree_that_has_already_gone_says_so(repo, worktree, tmp_path):
    """The other half of that ordering. The teardown won; the agent must find out
    now, from a non-zero exit, rather than by its cwd evaporating later."""
    shutil.rmtree(worktree)

    proc = run_enter(tmp_path, worktree, "--wait", "1")

    assert proc.returncode == 2
    assert "not there any more" in proc.stderr
    assert not marker_of(tmp_path).exists()


def test_entering_needs_to_know_who_is_entering(worktree, tmp_path):
    env = env_for(tmp_path)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    proc = subprocess.run([str(LOCK), "--enter", str(worktree)], capture_output=True,
                          text=True, env=env, check=False)
    assert proc.returncode == 2
    assert "CLAUDE_CODE_SESSION_ID" in proc.stderr


def test_a_host_that_cannot_lock_still_records_the_session(worktree, tmp_path):
    """Fail-open here too, and for a sharper reason than elsewhere: an unwritten
    marker is a session that is invisible to every teardown on the box, which is
    strictly worse than an unlocked write."""
    proc = run_enter(tmp_path, worktree, tools=TOOLS_UNLOCKABLE)

    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert marker_of(tmp_path).read_text() == str(worktree)
    assert "without a lock" in proc.stderr


# --------------------------------------------------------------------------
# The second acceptance criterion, at the point where it is spent: an agent that
# arrives AFTER the check and BEFORE the delete.

def counting_holder_stub(tmp_path, answers):
    """A `worktree-holder` that gives a different answer on each call.

    This is the shape of the race the issue is about — the tree was free when it
    was asked and is not free when the delete lands — and it is the only way to
    put an arrival inside a single teardown deterministically.
    """
    d = tmp_path / "counting-holder"
    d.mkdir(exist_ok=True)
    seq = " ".join(str(a) for a in answers)
    (d / "worktree-holder").write_text(
        "#!/bin/sh\n"
        f'N=$(cat "{d}/n" 2>/dev/null || echo 1)\n'
        f'echo $((N + 1)) > "{d}/n"\n'
        f'set -- {seq}\n'
        'eval "code=\\${$N}"\n'
        '[ -n "$code" ] || code=0\n'
        '[ "$code" = 3 ] && echo "held by somebody" >&2\n'
        'exit "$code"\n')
    (d / "worktree-holder").chmod(0o755)
    return d


def test_an_agent_arriving_after_the_check_is_not_deleted_over(
        repo, worktree, tmp_path):
    """Free when asked, held when the delete would land. The teardown stops.

    Before this, the first answer was the only answer and stood for the whole run
    — a `gh` call, a `docker compose down` and an nginx restart later, the delete
    acted on a reading that was minutes old.
    """
    proc = run_remove(repo, tmp_path, "fix-issue-43",
                      path_extra=(counting_holder_stub(tmp_path, [0, 3]),))

    assert proc.returncode != 0
    assert worktree.is_dir(), (
        f"an agent that entered mid-teardown was deleted over:\n"
        f"{proc.stdout}\n{proc.stderr}")
    assert "arrived while" in proc.stderr
    assert git(repo, "rev-parse", "--verify", "fix/issue-43").returncode == 0, \
        "the branch was deleted even though the worktree was not"


def test_the_late_refusal_says_what_it_has_already_taken_apart(
        repo, worktree, tmp_path):
    """Aborting there is not free: the stack is down and the nginx block is gone.

    Saying so is the difference between a refusal somebody can undo and a worktree
    somebody has to work out the state of.
    """
    proc = run_remove(repo, tmp_path, "fix-issue-43",
                      path_extra=(counting_holder_stub(tmp_path, [0, 3]),))
    assert "containers are down" in proc.stderr


def test_require_lock_refuses_a_late_answer_it_cannot_read(repo, worktree, tmp_path):
    """"Could not tell" at the last gate is the same guess as at the first one."""
    proc = run_remove(repo, tmp_path, "--require-lock", "fix-issue-43",
                      path_extra=(counting_holder_stub(tmp_path, [0, 4]),))

    assert proc.returncode != 0
    assert worktree.is_dir(), f"{proc.stdout}\n{proc.stderr}"


def test_the_interactive_default_proceeds_on_a_late_could_not_tell(
        repo, worktree, tmp_path):
    """The standing steer, at the new gate as much as at the old one: a board that
    stopped answering halfway through a teardown must not strand the worktree."""
    proc = run_remove(repo, tmp_path, "fix-issue-43",
                      path_extra=(counting_holder_stub(tmp_path, [0, 4]),))

    assert not worktree.exists(), f"{proc.stdout}\n{proc.stderr}"


def test_force_still_skips_both_gates(repo, worktree, tmp_path):
    proc = run_remove(repo, tmp_path, "--force", "fix-issue-43",
                      path_extra=(counting_holder_stub(tmp_path, [3, 3]),))

    assert not worktree.exists(), f"{proc.stdout}\n{proc.stderr}"


# --------------------------------------------------------------------------
# One mechanism, in one place. The `# >>> lock` regions in both worktree scripts
# are read here, which is what the marker comments promise.

def marker_region(script, name):
    """The lines between `# >>> <name>` and `# <<< <name>`, verbatim."""
    lines = script.read_text().splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip().startswith(f"# >>> {name}")]
    ends = [i for i, ln in enumerate(lines) if ln.strip().startswith(f"# <<< {name}")]
    assert len(starts) == 1 and len(ends) == 1, (
        f"{script.name} should hold exactly one `{name}` region "
        f"(found {len(starts)} starts, {len(ends)} ends)")
    return "\n".join(lines[starts[0] + 1:ends[0]])


@pytest.mark.parametrize("script", ["remove-worktree", "create-worktree"])
def test_neither_script_works_out_the_lockfile_for_itself(script):
    """The key is derived in `worktree-lock` and nowhere else.

    Two callers each computing a path under `$XDG_RUNTIME_DIR` is the failure this
    change is arguing against everywhere else — three readers with their own idea
    of the note's shape (`app/api/claims.py:213`) — and here it would be silent:
    each script would take a lock, both would report success, and they would be
    locking different files.
    """
    block = marker_region(BIN / script, "lock")
    assert "worktree_lock_acquire" in block, (
        f"{script} does not go through the library at all")
    # CODE only. These blocks argue in prose about the environment variables the
    # root must NOT be built from, and a scan that reads the argument as the
    # offence would push the argument out of the file.
    code = "\n".join(ln for ln in block.splitlines() if not ln.strip().startswith("#"))
    for forbidden in ("XDG_RUNTIME_DIR", "sha256sum", "TMPDIR", "flock -"):
        assert forbidden not in code, (
            f"{script}'s lock block spells `{forbidden}` itself instead of asking "
            f"worktree-lock — that is a second key derivation waiting to diverge")


@pytest.mark.parametrize("script", ["remove-worktree", "create-worktree"])
def test_both_scripts_find_worktree_lock_the_same_two_ways(script):
    """`command -v`, then a sibling of `$0`. Installed by home-manager each file
    is its own flat store path, so "beside the script" is the only relationship
    there is — and a script that only tried `PATH` would silently stop locking on
    the hosts where the harness is not on it."""
    block = marker_region(BIN / script, "lock")
    assert "command -v worktree-lock" in block
    assert '${0%/*}/worktree-lock' in block


def test_the_marker_is_written_absolute_even_from_a_relative_argument(
        repo, worktree, tmp_path):
    """`worktree-holder` compares the marker's CONTENT to the worktree path.

    A relative spelling matches nothing there, so the session it names is
    invisible to every teardown on the box — the failure this command exists to
    prevent, reached by writing the marker successfully.
    """
    proc = subprocess.run([str(LOCK), "--enter", worktree.name], cwd=worktree.parent,
                          capture_output=True, text=True, check=False,
                          env=env_for(tmp_path, CLAUDE_CODE_SESSION_ID="s-1111"))

    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert marker_of(tmp_path).read_text() == str(worktree)


# --------------------------------------------------------------------------
# NO GIT CHILD MAY OUTLIVE THE HOLDER STILL HOLDING ITS LOCK.
#
# A descriptor survives fork and exec, and git runs auto-maintenance DETACHED. On
# git 2.54 both of these were traced doing it:
#
#   git fetch  -> git maintenance run --auto --quiet    --detach
#   git merge  -> git maintenance run --auto --no-quiet --detach
#
# `--detach` daemonises before deciding whether there is any work, so the child
# exists on every one of them. The first cut of this change closed the lock's
# descriptor for `git fetch` alone and asserted in a comment that everything else
# was synchronous; the `git merge --ff-only` in `create-worktree` says otherwise.
# The rule is set once, in the environment, and these pin it.

def test_the_lock_turns_off_git_auto_maintenance_for_its_holder():
    """Set where the lock is taken, so no call site has to remember."""
    lib = LOCK.read_text()
    assert "worktree_lock_quiet_git" in lib
    assert "maintenance.auto" in lib and "gc.auto" in lib
    acquire = lib.split("worktree_lock_acquire() {", 1)[1].split("\n}", 1)[0]
    assert "worktree_lock_quiet_git" in acquire, (
        "the rule is not applied where the lock is taken, so it depends on each "
        "caller remembering — which is the failure it exists to replace")


@pytest.mark.parametrize("script", ["remove-worktree", "create-worktree"])
def test_neither_script_closes_the_descriptor_by_hand_any_more(script):
    """An enumeration of "the git commands that can auto-maintain" is a list
    somebody has to keep correct, and it was already wrong once."""
    assert "{WORKTREE_LOCK_FD}>&-" not in (BIN / script).read_text(), (
        f"{script} closes the lock descriptor at a call site — that is the "
        "per-command enumeration the environment rule replaced")


def test_a_git_that_detaches_a_child_does_not_leave_the_lock_held(repo, worktree,
                                                                  tmp_path):
    """The behaviour, end to end, with a `git` that detaches the way the real one does.

    A shim rather than real git, because the real detached child usually finds no
    work and exits in milliseconds — a test built on it would be green whether or
    not the rule held. The shim spawns a background child on `fetch` and `merge`
    *unless git config says auto-maintenance is off*, which it asks the real git,
    so it honours `GIT_CONFIG_*` exactly as git does. That is the one thing being
    modelled; the leak itself — an inherited descriptor outliving its parent — is
    the kernel's behaviour and is real here.
    """
    real_git = shutil.which("git")
    shim = tmp_path / "git-shim"
    shim.mkdir()
    (shim / "git").write_text(
        "#!/bin/sh\n"
        f'REAL="{real_git}"\n'
        # The SUBCOMMAND, which is the first argument that is not an option and
        # not an option's value. `case "$1"` was wrong and silently so: the call
        # under test is `git -C <dir> fetch`, whose $1 is `-C`, and the shim then
        # modelled nothing at all while the test stayed green.
        'sub=""\n'
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        '    -C|-c|--git-dir|--work-tree) skip=1 ;;\n'
        '    -*) ;;\n'
        '    *) if [ "${skip:-0}" = 1 ]; then skip=0; else sub="$a"; break; fi ;;\n'
        '  esac\n'
        'done\n'
        'case "$sub" in\n'
        '  fetch|merge)\n'
        '    if [ "$("$REAL" config --get maintenance.auto 2>/dev/null || echo true)" '
        '!= "false" ]; then\n'
        # >/dev/null on the child, or it holds the pipe this test reads and
        # `subprocess.run` waits for it — which waits out the very leak the
        # test is arranging and then measures a lock already released.
        '      sleep 30 >/dev/null 2>&1 &\n'
        '    fi ;;\n'
        'esac\n'
        'exec "$REAL" "$@"\n')
    (shim / "git").chmod(0o755)

    holder = tmp_path / "fetch-then-go"
    holder.write_text(
        f"#!{BASH}\n"
        f'. "{LOCK}"\n'
        'worktree_lock_acquire "$1" 1 "$2" || exit 9\n'
        # Whether these SUCCEED is irrelevant — the fixture repo has no remote,
        # and the shim spawns its child before it execs the real git, exactly as
        # the real one does.
        'git -C "$1" fetch origin >/dev/null 2>&1 || true\n'
        'git -C "$1" merge --ff-only "@{u}" >/dev/null 2>&1 || true\n'
        'exit 0\n')
    holder.chmod(0o755)

    env = env_for(tmp_path, shim)
    assert subprocess.run([str(holder), str(worktree), str(repo)], env=env,
                          capture_output=True, text=True).returncode == 0

    lock = lock_path(tmp_path, worktree, repo)
    free = subprocess.run(["flock", "-n", lock, "true"], capture_output=True)
    assert free.returncode == 0, (
        "a git child outlived the holder still holding its lock — the next "
        "teardown of this worktree would refuse, naming a pid that is gone")

    # AND THE SHIM MODELS SOMETHING. The same sequence, holding the same lockfile
    # by hand so the library's rule is not applied at all, must leave the lock
    # STUCK. Without this the assertion above would be green against a shim that
    # spawned nothing — which is how its first cut passed, matching `$1` against a
    # `git -C <dir> fetch` whose first argument is `-C`.
    control = tmp_path / "control"
    control.write_text(
        f"#!{BASH}\n"
        'exec {FD}>>"$2"\n'
        'flock -x "$FD"\n'
        'git -C "$1" fetch origin >/dev/null 2>&1 || true\n'
        'exit 0\n')
    control.chmod(0o755)
    subprocess.run([str(control), str(worktree), lock], env=env,
                   capture_output=True, text=True)
    stuck = subprocess.run(["flock", "-n", lock, "true"], capture_output=True)
    assert stuck.returncode != 0, (
        "nothing held the lock after a git run with auto-maintenance left on — "
        "the shim is not modelling a detaching git, so the assertion above is "
        "empty")


# --------------------------------------------------------------------------
# When the lock IS wedged: say so accurately, and leave a way past it.

def leave_a_child_holding_it(tmp_path, target, repo):
    """A holder that exits leaving a background child with the descriptor.

    This is what a detached `git maintenance` does, and it is the state in which
    the lockfile's own record of who holds the lock is wrong.
    """
    script = tmp_path / "leak-it"
    script.write_text(
        f"#!{BASH}\n"
        f'. "{LOCK}"\n'
        'worktree_lock_acquire "$1" 1 "$2" || exit 9\n'
        # >/dev/null on the child, or it holds the pipe the caller reads and the
        # caller waits out the very leak it is arranging.
        'sleep 30 >/dev/null 2>&1 &\n'
        'echo "$$"\n')
    script.chmod(0o755)
    out = subprocess.run([str(script), str(target), str(repo)], capture_output=True,
                         text=True, env=env_for(tmp_path))
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    return int(out.stdout.strip())


def test_a_refusal_says_when_the_recorded_holder_has_already_exited(
        repo, worktree, tmp_path):
    """"pid 4711 is tearing this down right now" about a pid that exited an hour
    ago sends somebody looking for a process that is not there."""
    dead = leave_a_child_holding_it(tmp_path, worktree, repo)

    proc = run_remove(repo, tmp_path, "--lock-wait", "1", "fix-issue-43",
                      path_extra=(holder_stub(tmp_path, 0),))

    assert proc.returncode != 0
    assert worktree.is_dir()
    assert "WHICH HAS EXITED" in proc.stderr, (
        f"the refusal named pid {dead} as the live holder:\n{proc.stderr}")
    assert "lsof" in proc.stderr or "fuser" in proc.stderr


def test_ignore_lock_is_the_way_past_a_lock_nothing_will_release(
        repo, worktree, tmp_path):
    """A guard with no escape hatch gets deleted rather than obeyed — the same
    argument `--no-backup` carries. Waiting does not clear a descriptor a stray
    child is sitting on, so there has to be something that does."""
    leave_a_child_holding_it(tmp_path, worktree, repo)

    proc = run_remove(repo, tmp_path, "--ignore-lock", "--lock-wait", "1",
                      "fix-issue-43", path_extra=(holder_stub(tmp_path, 0),))

    assert not worktree.exists(), f"{proc.stdout}\n{proc.stderr}"
    assert "Ignoring the worktree lock" in proc.stderr


def test_ignore_lock_is_not_implied_by_force(repo, worktree, tmp_path):
    """--force means "the agent in there has finished". It has never meant "run
    two teardowns over one directory at once", and a live holder is exactly that."""
    holder = hold_the_lock(tmp_path, worktree, repo)
    try:
        proc = run_remove(repo, tmp_path, "--force", "--lock-wait", "1",
                          "fix-issue-43")
        assert proc.returncode != 0
        assert worktree.is_dir(), f"{proc.stdout}\n{proc.stderr}"
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_a_filesystem_that_cannot_lock_is_not_read_as_a_lock_being_held(
        repo, worktree, tmp_path):
    """`flock` spends exit 1 on two answers, and only one of them means "wait".

    A timeout is silent; a filesystem with no locking ("Function not implemented",
    "No locks available") says so. Reading the second as "somebody holds it" would
    wedge every worktree on that filesystem permanently, with `--ignore-lock` the
    only way to touch any of them. It is a "cannot lock here", which the
    interactive default proceeds through.
    """
    broken = tmp_path / "broken-flock"
    broken.mkdir()
    (broken / "flock").write_text(
        '#!/bin/sh\necho "flock: bad: Function not implemented" >&2\nexit 1\n')
    (broken / "flock").chmod(0o755)

    proc = run_remove(repo, tmp_path, "fix-issue-43",
                      path_extra=(broken, holder_stub(tmp_path, 0)))

    assert not worktree.exists(), (
        f"a filesystem that cannot lock read as a lock being held:\n"
        f"{proc.stdout}\n{proc.stderr}")
    assert "Function not implemented" in proc.stderr


def test_require_lock_still_refuses_a_filesystem_that_cannot_lock(
        repo, worktree, tmp_path):
    """It is the same answer as no flock at all, so it gets the same treatment."""
    broken = tmp_path / "broken-flock"
    broken.mkdir()
    (broken / "flock").write_text(
        '#!/bin/sh\necho "flock: bad: No locks available" >&2\nexit 1\n')
    (broken / "flock").chmod(0o755)

    proc = run_remove(repo, tmp_path, "--require-lock", "fix-issue-43",
                      path_extra=(broken, holder_stub(tmp_path, 0)))

    assert proc.returncode != 0
    assert worktree.is_dir(), f"{proc.stdout}\n{proc.stderr}"
