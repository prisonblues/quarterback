"""Publish/staleness reasoning for cross-device git sync (v2.8).

The board can't run git — the repos live on the devices. What it *does* hold is
every device's worktree snapshot (head + recent commits, via ``report_git``) and
the ``published`` posts announcing "this SHA is on the remote now". Comparing the
two answers the question devops work keeps getting wrong: *is my checkout stale,
and whose commit am I missing?* Pure and deterministic, like ``overlap`` — no
model, no I/O — so it's cheap to run in a hook and easy to test.
"""

from __future__ import annotations

from typing import Any

# Shortest prefix we'll treat as a commit identity (matches git's own floor for
# unambiguous abbreviations in practice, and /worktrees?has_commit=).
MIN_SHA = 7

# How far back the published line is considered. Deep enough that a device idle
# for a few days still gets an accurate count, shallow enough to stay one cheap
# indexed query.
#
# Two things read it and they must agree: ``GET /sync`` decides staleness within
# this window, and the origin poller (v2.42) suppresses a re-announcement within
# it. A poller scanning shallower than the consumer would double-announce a
# commit the consumer can still see; scanning deeper would suppress one it
# cannot. Same number, one definition.
PUBLISH_SCAN = 200


def repo_key(value: str | None) -> str | None:
    """Loose repo identity: the bare name, lowercased.

    Repo names reach the board in two shapes — ``report_git`` registers worktrees
    under the origin slug ("owner/name") while the lifecycle hook tags posts with
    the checkout's basename ("name"). Matching on the last segment lets the two
    agree without forcing either side to change.
    """
    if not value:
        return None
    return value.rstrip("/").split("/")[-1].lower() or None


def _refs(refs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [r for r in (refs or []) if isinstance(r, dict)]


def ref_value(refs: list[dict[str, Any]] | None, kind: str) -> str | None:
    """First ref of `kind`, or None."""
    for r in _refs(refs):
        if r.get("kind") == kind and r.get("value"):
            return str(r["value"])
    return None


def same_commit(a: str | None, b: str | None) -> bool:
    """True if two SHAs name the same commit, allowing either to be abbreviated."""
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    if min(len(a), len(b)) < MIN_SHA:
        return False
    return a.startswith(b) or b.startswith(a)


def has_commit(head_sha: str | None, commits: list[dict[str, Any]] | None, sha: str) -> bool:
    """Does this worktree snapshot contain `sha` (as HEAD or in its recent slice)?"""
    if same_commit(head_sha, sha):
        return True
    return any(same_commit(str(c.get("sha") or ""), sha) for c in (commits or []))


def missing_published(
    published: list[dict[str, Any]],
    head_sha: str | None,
    commits: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """The published commits this worktree lacks, newest-first.

    `published` is newest-first. We stop at the first entry the worktree *does*
    hold: publishes older than that are ancestors it must already have, even
    though they may have scrolled out of its recent-commits window. That's what
    keeps a long-lived worktree from being reported stale forever on a publish
    it merged months ago.
    """
    missing: list[dict[str, Any]] = []
    for entry in published:
        if has_commit(head_sha, commits, entry["sha"]):
            break
        missing.append(entry)
    return missing


def worktree_state(worktree: dict[str, Any], published: list[dict[str, Any]]) -> dict[str, Any]:
    """One worktree's sync verdict against the published line.

    Two independent staleness signals, because each sees what the other can't:
    ``behind_published`` is board-derived and live (a peer pushed 20 seconds ago),
    while ``behind_upstream`` is the device's own count against its tracking
    branch at last report — accurate about local state, but only as fresh as the
    last ``report_git`` and only if that device had fetched.
    """
    missing = missing_published(published, worktree.get("head_sha"), worktree.get("commits"))
    behind_upstream = worktree.get("behind") or 0
    ahead = worktree.get("ahead") or 0
    return {
        "device": worktree.get("device"),
        "path": worktree.get("path"),
        "branch": worktree.get("branch"),
        "head_sha": worktree.get("head_sha"),
        "upstream": worktree.get("upstream"),
        "remote_sha": worktree.get("remote_sha"),
        "dirty": bool(worktree.get("dirty")),
        "ahead": ahead,
        "behind_upstream": behind_upstream,
        "behind_published": len(missing),
        "missing": missing,
        "updated_at": worktree.get("updated_at"),
        "stale": bool(missing) or behind_upstream > 0,
    }


def _short(sha: str) -> str:
    return sha[:MIN_SHA]


def advice(repo: str, states: list[dict[str, Any]]) -> str | None:
    """One line an agent can act on, or None when everything is in sync.

    Written to be pasted straight into a hook's context injection, so it names
    the repo, the count, the newest missing commit and who published it.
    """
    stale = [s for s in states if s["stale"]]
    if not stale:
        return None

    first = stale[0]
    where = f"{repo}"
    if first.get("branch"):
        where += f" @ {first['branch']}"

    if first["missing"]:
        top = first["missing"][0]
        n = first["behind_published"]
        line = (
            f"{where}: {n} published commit{'s' if n != 1 else ''} you don't have "
            f"(newest {_short(top['sha'])} \"{top['summary']}\" from {top['from']}) — git pull."
        )
    else:
        n = first["behind_upstream"]
        upstream = first.get("upstream") or "the remote"
        line = (
            f"{where}: {n} commit{'s' if n != 1 else ''} behind {upstream} "
            f"at last report — git pull."
        )

    if first["dirty"]:
        line += " Working tree is dirty — commit or stash first."
    if first["ahead"]:
        n = first["ahead"]
        line += f" You also have {n} commit{'s' if n != 1 else ''} not on the remote — push them."
    if len(stale) > 1:
        line += f" ({len(stale) - 1} other worktree(s) stale too.)"
    return line
