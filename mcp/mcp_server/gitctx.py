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


def repo_slug(repo_path: str) -> str | None:
    """owner/name from the origin remote, or None if not a GitHub-style remote."""
    try:
        url = _git(repo_path, "config", "--get", "remote.origin.url").strip()
    except subprocess.CalledProcessError:
        return None
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


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

    Each worktree: {path, repo, branch, head, commits:[{sha, subject}]}. Parses
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
