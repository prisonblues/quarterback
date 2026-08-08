"""Local git context gathering for the MCP server.

Runs read-only git commands in the agent's own checkout (the MCP server is local,
so it *can* see the repo — the quarterback server on atlas cannot). Produces the
snapshot that report_git registers with the board.
"""

from __future__ import annotations

import re
import subprocess

US = "\x1f"  # unit separator between %H and %s in git log output


def _git(repo_path: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _git_rc(repo_path: str, *args: str) -> int:
    """Run git for its exit status only (predicates like merge-base --is-ancestor)."""
    return subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True,
        text=True,
    ).returncode


def _try(repo_path: str, *args: str) -> str | None:
    try:
        return _git(repo_path, *args).strip() or None
    except subprocess.CalledProcessError:
        return None


def upstream_of(repo_path: str) -> str | None:
    """The tracking branch (e.g. "origin/main"), or None if the branch tracks nothing."""
    return _try(repo_path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")


def upstream_contains(repo_path: str, sha: str) -> bool | None:
    """Is `sha` already on the tracking branch? None when there's nothing to compare to.

    Reads the local remote-tracking ref — no network. That's exactly right after
    a push (push updates the ref), which is when we ask.
    """
    up = upstream_of(repo_path)
    if not up:
        return None
    rc = _git_rc(repo_path, "merge-base", "--is-ancestor", sha, up)
    if rc == 0:
        return True
    return False if rc == 1 else None  # anything else (128: bad ref) is "can't tell"


def sync_state(worktree_path: str) -> dict:
    """Tracking-branch facts for one worktree: upstream, remote_sha, ahead/behind, dirty.

    All best-effort — a worktree with no upstream, or a git that errors, reports
    None rather than failing the snapshot. ahead/behind come from the *local*
    remote-tracking ref, so they're only as fresh as the last fetch; that's why
    the board also compares against published commits, which are live.
    """
    dirty = _try(worktree_path, "status", "--porcelain", "--untracked-files=no")
    state: dict = {
        "upstream": None,
        "remote_sha": None,
        "ahead": None,
        "behind": None,
        # `_try` collapses a clean tree (empty output) to None, so re-derive the
        # boolean from whether git ran at all.
        "dirty": None if _git_rc(worktree_path, "status", "--porcelain") != 0 else bool(dirty),
    }
    up = upstream_of(worktree_path)
    if not up:
        return state
    state["upstream"] = up
    state["remote_sha"] = _try(worktree_path, "rev-parse", up)
    counts = _try(worktree_path, "rev-list", "--left-right", "--count", f"{up}...HEAD")
    if counts:
        parts = counts.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            # left = commits only on the upstream (behind), right = only local (ahead)
            state["behind"], state["ahead"] = int(parts[0]), int(parts[1])
    return state


def repo_slug(repo_path: str) -> str | None:
    """owner/name from the origin remote, or None if not a GitHub-style remote."""
    try:
        url = _git(repo_path, "config", "--get", "remote.origin.url").strip()
    except subprocess.CalledProcessError:
        return None
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def head_context(repo_path: str) -> dict:
    """Where am I: {toplevel, branch, head, subject} for the checkout at repo_path."""
    return {
        "toplevel": _try(repo_path, "rev-parse", "--show-toplevel"),
        "branch": _try(repo_path, "branch", "--show-current"),
        "head": _try(repo_path, "rev-parse", "HEAD"),
        "subject": _try(repo_path, "log", "-1", "--format=%s"),
    }


def recent_shas(worktree_path: str, depth: int) -> list[str]:
    """The last `depth` commit SHAs, newest first — a checkout's "what I have"."""
    out = _try(worktree_path, "log", f"-n{depth}", "--format=%H")
    return out.splitlines() if out else []


def _recent_commits(worktree_path: str, depth: int) -> list[dict]:
    try:
        out = _git(worktree_path, "log", f"-n{depth}", f"--format=%H{US}%s")
    except subprocess.CalledProcessError:
        return []
    commits = []
    for line in out.splitlines():
        if US in line:
            sha, subject = line.split(US, 1)
            commits.append({"sha": sha, "subject": subject})
    return commits


def gather_worktrees(repo_path: str, commit_depth: int = 15) -> tuple[str | None, list[dict]]:
    """Return (repo_slug, worktrees) for the repo containing repo_path.

    Each worktree: {path, repo, branch, head, commits:[{sha, subject}]} plus the
    ``sync_state`` keys (upstream, remote_sha, ahead, behind, dirty). Parses
    ``git worktree list --porcelain`` — blank-line-separated records of
    ``worktree <path>`` / ``HEAD <sha>`` / ``branch refs/heads/<name>`` (or
    ``detached``).
    """
    slug = repo_slug(repo_path)
    out = _git(repo_path, "worktree", "list", "--porcelain")

    worktrees: list[dict] = []
    cur: dict = {}

    def flush():
        if cur.get("path"):
            worktrees.append(
                {
                    "path": cur["path"],
                    "repo": slug,
                    "branch": cur.get("branch"),
                    "head": cur.get("head"),
                    "commits": _recent_commits(cur["path"], commit_depth),
                    **sync_state(cur["path"]),
                }
            )

    for line in out.splitlines():
        if not line.strip():
            flush()
            cur = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            cur["path"] = value
        elif key == "HEAD":
            cur["head"] = value
        elif key == "branch":
            cur["branch"] = value.removeprefix("refs/heads/")
        elif key == "detached":
            cur["branch"] = None
    flush()
    return slug, worktrees
