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


def lock_path(tmp_path, target, tools=TOOLS, **over):
    """What `worktree-lock --path` says `target` keys to, in this sandbox."""
    proc = subprocess.run([str(LOCK), "--path", str(target)], capture_output=True,
                          text=True, check=False,
                          env=env_for(tmp_path, tools=tools, **over))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return proc.stdout.strip()


def hold_the_lock(tmp_path, target, seconds=60, tools=TOOLS, **over):
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
        'worktree_lock_acquire "$1" 1 || exit 9\n'
        'echo ready\n'
        'exec sleep "$2"\n')
    script.chmod(0o755)
    proc = subprocess.Popen([str(script), str(target), str(seconds)],
                            stdout=subprocess.PIPE, text=True,
                            env=env_for(tmp_path, tools=tools, **over))
    line = proc.stdout.readline()
    assert line.strip() == "ready", f"the holder never took the lock: {line!r}"
    return proc


# --------------------------------------------------------------------------
# The key. Everything else here is worthless if two callers disagree about it.

def test_a_tree_that_does_not_exist_yet_keys_the_same_lock_as_the_tree_that_does(
        tmp_path):
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
    absent = lock_path(tmp_path, real / "proj-fix-issue-43")
    absent_via_symlink = lock_path(tmp_path, tmp_path / "alias" / "proj-fix-issue-43")
    (real / "proj-fix-issue-43").mkdir()
    present = lock_path(tmp_path, real / "proj-fix-issue-43")
    present_via_symlink = lock_path(tmp_path, tmp_path / "alias" / "proj-fix-issue-43")
    trailing_slash = lock_path(tmp_path, str(real / "proj-fix-issue-43") + "/")

    assert (absent == absent_via_symlink == present == present_via_symlink
            == trailing_slash), (
        "the same worktree spelled five ways took up to five different locks:\n"
        f"  absent             {absent}\n"
        f"  absent via symlink {absent_via_symlink}\n"
        f"  present            {present}\n"
        f"  present via alias  {present_via_symlink}\n"
        f"  trailing slash     {trailing_slash}")


def test_two_different_worktrees_do_not_share_a_lock(tmp_path):
    """The other direction: a lock that serialised the whole box would be a bug
    that only ever shows up as everything being slow."""
    assert lock_path(tmp_path, tmp_path / "proj-a") != \
           lock_path(tmp_path, tmp_path / "proj-b")


def test_the_lockfile_is_named_after_the_worktree_as_well_as_hashed(tmp_path):
    """A directory of bare digests is a directory nobody can debug."""
    assert Path(lock_path(tmp_path, tmp_path / "proj-fix-issue-43")).name.startswith(
        "proj-fix-issue-43-")


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
    holder = hold_the_lock(tmp_path, worktree)
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
    holder = hold_the_lock(tmp_path, worktree)
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
    holder = hold_the_lock(tmp_path, worktree)
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
    holder = hold_the_lock(tmp_path, worktree)
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
    stale = Path(lock_path(tmp_path, worktree))
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


def test_entering_waits_for_a_teardown_rather_than_racing_it(worktree, tmp_path):
    """The ordering that used to be possible and now is not: the marker landing
    while a teardown is between its check and its delete."""
    holder = hold_the_lock(tmp_path, worktree)
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
    for forbidden in ("XDG_RUNTIME_DIR", "sha256sum", "TMPDIR", "flock -"):
        assert forbidden not in block, (
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
