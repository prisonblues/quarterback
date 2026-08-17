"""The machine-local half — the part a browser cannot do.

Every *board* mutation is reachable from the browser board. What is not is
anything needing a process on this machine: pulling the checkout an advisory
names, cherry-picking a SHA ``find_commit`` located. That is a browser-sandbox
limit rather than a UI one, and it is why issue #110 asks for a client that *is*
a local process.

**Refusals are the feature here, not the actions.** Rewriting somebody else's
checkout out from under them is the failure this exists to prevent, so each
action asks three questions first and a "could not tell" counts as a no:

1. is the tree dirty, mid-sequencer, or carrying commits that exist nowhere else?
2. is another live agent holding this worktree? (``worktree-holder``, exit 3)
3. could we even ask? (exit 4 — a down board must not read as "free")

**These checks narrow a race; they do not close it.** Nothing here reserves the
checkout, and deliberately so: ``worktree-holder`` says of itself that it is
ADVISORY, NEVER A LOCK, and a lock taken by this client would wedge whatever
live session it locked out. An agent can still arrive between the last check and
the git write. What that buys is the ordering below — the slow, network-bound
holder question is asked *last*, immediately before git writes, so the window it
leaves open is as small as a client without a lock can make it. The failure
being prevented is the *silent* rewrite of somebody else's checkout, which is
the one this ordering actually catches.

Issue #83 will land the same rebase-on-publish behaviour as a first-class
operation. When it does, :func:`pull` should call it instead of running the
git itself; the checks below are deliberately separate, named functions so the
refusals survive that swap rather than being re-derived inside it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT = 120

#: worktree-holder's documented exit codes.
_HELD = 3
_CANNOT_TELL = 4

#: A git object name and nothing else, applied with `fullmatch` because the point
#: is to *exclude* the rest of git's revision grammar: `HEAD~10`, `:/subject`,
#: `main@{yesterday}` and `--upload-pack=…` are all things a board post's `commit`
#: ref can carry, and all things that would make `cherry-pick` pick something
#: nobody asked for. 7 is git's own minimum unambiguous prefix, 40 a full SHA-1.
_SHA = re.compile(r"[0-9a-fA-F]{7,40}")

#: What git leaves in the git dir while an interrupted multi-step operation is
#: still open. Checked by path because there is no porcelain that reports it, and
#: this is where git's own `wt_status` looks.
_SEQUENCER_MARKERS = (
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "MERGE_HEAD",
    "rebase-merge",
    "rebase-apply",
)


@dataclass(frozen=True)
class Outcome:
    """What happened, and whether the caller may treat it as done."""

    ok: bool
    message: str

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.ok


def _git(path: str, *args: str) -> subprocess.CompletedProcess:
    """Run git, and turn a failure to *launch* it into a failed run.

    A missing git, an unreadable directory, or a `git pull` that hangs past the
    timeout would otherwise raise out of a check and up through the Textual
    worker that called it — so the one place designed to say "refusing, and
    here is why" would instead say nothing at all. Every caller already handles
    a non-zero return code; this makes those the only outcome there is.
    """
    try:
        return subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args, 1, "", f"git {args[0]} timed out after {_GIT_TIMEOUT}s"
        )
    except OSError as e:
        return subprocess.CompletedProcess(args, 1, "", f"could not run git: {e}")


def _first_line(text: str, fallback: str) -> str:
    line = (text or "").strip().splitlines()
    return line[0] if line else fallback


def check_free(path: str) -> Outcome:
    """Is this worktree free of other live agents?

    Delegates to ``worktree-holder`` rather than re-asking ``/active`` here: the
    board alone cannot answer it (a lease records the directory the agent was
    *launched* in, which for the worktree workflow is the main checkout), and
    that script is where the marker/board union already lives.
    """
    binary = shutil.which("worktree-holder")
    if binary is None:
        return Outcome(
            False,
            "could not tell whether anyone is in this worktree "
            "(worktree-holder is not on PATH) — refusing",
        )
    try:
        # Run once, not `--quiet` then again for the message: the script asks the
        # board, and asking it twice to render one refusal doubles the latency of
        # the answer a user is waiting on. Its contract puts the all-clear on
        # stdout and every warning on stderr, so one call has both.
        proc = subprocess.run([binary, path], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return Outcome(False, f"could not run worktree-holder ({e}) — refusing")
    if proc.returncode == _HELD:
        # The WHOLE warning, not its first line. worktree-holder leads with a
        # generic "<path> is held by another live agent" and puts who, on what
        # branch, for how long, on the lines after it — so taking one line threw
        # away the only part of the refusal a person can act on.
        who = (proc.stderr or "").strip() or "another live agent (no detail given)"
        return Outcome(False, f"refusing — {who}")
    if proc.returncode == _CANNOT_TELL:
        return Outcome(False, "could not tell whether anyone is here (board unreachable) — refusing")
    if proc.returncode != 0:
        return Outcome(False, f"worktree-holder failed (exit {proc.returncode}) — refusing")
    return Outcome(True, "nobody else is live here")


def check_clean(path: str) -> Outcome:
    """Refuse on a dirty tree — a pull over uncommitted work is somebody's afternoon."""
    proc = _git(path, "status", "--porcelain")
    if proc.returncode != 0:
        return Outcome(False, f"not a git checkout ({_first_line(proc.stderr, 'git failed')})")
    changed = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if changed:
        return Outcome(False, f"working tree is dirty ({len(changed)} path(s)) — refusing")
    return Outcome(True, "clean")


def check_no_unpushed(path: str) -> Outcome:
    """Refuse when the checkout holds commits its upstream does not.

    No upstream is *not* a pass. A branch that tracks nothing has commits that
    exist on exactly one disk, which is the case this check exists for.
    """
    upstream = _git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode != 0:
        return Outcome(False, "branch has no upstream, so local commits exist nowhere else — refusing")
    ahead = _git(path, "rev-list", "--count", "@{u}..HEAD")
    if ahead.returncode != 0:
        return Outcome(False, f"could not count unpushed commits ({_first_line(ahead.stderr, '?')})")
    try:
        # Not `int(x or 0)`: a git that exits 0 having printed nothing (or a
        # locale-mangled number) would raise out of the background worker, and an
        # exception there is the one outcome the caller cannot turn into a message.
        count = int(ahead.stdout.strip())
    except ValueError:
        got = " ".join(ahead.stdout.split())[:60]
        return Outcome(False, f"could not read git's unpushed count (got {got!r}) — refusing")
    if count:
        return Outcome(False, f"{count} unpushed commit(s) here — refusing")
    return Outcome(True, "nothing unpushed")


def _git_dir(path: str) -> Path | None:
    """The checkout's git dir, or None if `path` is not one.

    Resolved rather than assumed ``<path>/.git``: in a linked worktree that is a
    file pointing elsewhere, and the sequencer state lives at the far end of it.
    """
    proc = _git(path, "rev-parse", "--absolute-git-dir")
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return Path(out) if out else None


def check_not_mid_operation(path: str) -> Outcome:
    """Refuse a checkout that is part-way through a cherry-pick, revert, merge or rebase.

    A tree can be clean and still be mid-sequencer — a rebase paused on `edit` is
    exactly that — so :func:`check_clean` does not cover this. Writing into one is
    worse than writing into a dirty tree: whoever left it there has state that only
    `--continue` or `--abort` can resolve, and neither is ours to choose.
    """
    git_dir = _git_dir(path)
    if git_dir is None:
        return Outcome(False, "not a git checkout")
    for marker in _SEQUENCER_MARKERS:
        if (git_dir / marker).exists():
            return Outcome(False, f"a {marker} operation is already in progress here — refusing")
    return Outcome(True, "no operation in progress")


def pull(path: str) -> Outcome:
    """Fast-forward this machine's checkout at `path`.

    Fast-forward only, and that is a consequence of the checks rather than a
    separate policy: with no unpushed commits and a clean tree there is nothing
    for a merge or a rebase to do that a fast-forward cannot, and `--ff-only`
    fails loudly instead of inventing a merge commit if that stops being true
    between the check and the pull.

    The local checks run first and :func:`check_free` last, immediately before the
    write: it is the slow one (it asks the board) and the only one whose answer can
    go stale in a way that matters, so it gets the shortest gap to the git call.
    That narrows the race, it does not remove it — see the module docstring; there
    is no reservation here and by design cannot be one.
    """
    for check in (check_clean, check_no_unpushed, check_free):
        outcome = check(path)
        if not outcome.ok:
            return outcome
    proc = _git(path, "pull", "--ff-only")
    if proc.returncode != 0:
        return Outcome(False, f"git pull --ff-only failed: {_first_line(proc.stderr, 'unknown error')}")
    return Outcome(True, _first_line(proc.stdout, "already up to date"))


def cherry_pick(path: str, sha: str) -> Outcome:
    """Cherry-pick `sha` into the checkout at `path`.

    Unpushed commits are not a refusal here, unlike :func:`pull` — adding a
    commit on top of local work is the ordinary case, and the thing being
    protected against is writing into a checkout somebody else is using.

    Check order matches :func:`pull`'s and for the same reason: the local checks
    are cheap and stable, :func:`check_free` is neither, so it is asked last and
    the git write follows it immediately. The gap is small, not absent.
    """
    if not _SHA.fullmatch(sha or ""):
        return Outcome(
            False,
            f"{sha!r} is not a commit SHA — this takes 7 to 40 hex characters and "
            "nothing else (no HEAD~n, no branch names, no flags)",
        )
    # Mid-operation before dirty: a stopped cherry-pick is *also* a dirty tree, and
    # "you are part-way through a cherry-pick" is the half of that a person can act on.
    for check in (check_not_mid_operation, check_clean, check_free):
        outcome = check(path)
        if not outcome.ok:
            return outcome
    have = _git(path, "cat-file", "-e", f"{sha}^{{commit}}")
    if have.returncode != 0:
        fetch = _git(path, "fetch", "--all", "--quiet")
        if fetch.returncode != 0 or _git(path, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
            return Outcome(False, f"{sha[:12]} is not in this checkout, even after a fetch")
    # `--`, even though `sha` is already known to be hex: the separator is what
    # makes that a property of the call rather than of the validator staying right.
    proc = _git(path, "cherry-pick", "--", sha)
    if proc.returncode != 0:
        # Leave no half-applied pick behind: an abandoned CHERRY_PICK_HEAD is a
        # trap for whoever opens this checkout next, and they did not ask for it.
        # Only ours, though — `check_not_mid_operation` above established there was
        # no sequencer state here before this call, so anything present now is this
        # call's, and `--abort` cannot be discarding somebody else's afternoon.
        if not check_not_mid_operation(path).ok:
            _git(path, "cherry-pick", "--abort")
        return Outcome(False, f"cherry-pick failed: {_first_line(proc.stderr, 'conflict')}")
    return Outcome(True, f"cherry-picked {sha[:12]}")


def local_worktrees(registered: list[dict], device: str) -> list[dict]:
    """This device's registered worktrees that still exist on disk.

    The registry is a snapshot somebody pushed, so a directory in it may have
    been removed since. Offering an action against a path that is gone is worse
    than not offering it.
    """
    return [
        w
        for w in registered
        if w.get("device") == device and w.get("path") and Path(w["path"]).is_dir()
    ]
