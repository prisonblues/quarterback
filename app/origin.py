"""Watching origin, so a merge nobody pushed still reaches the board (v2.42, #127).

The publish reflex is a ``PostToolUse`` hook on ``Bash``: it sees a ``git push``,
asks git whether the push landed, and posts ``published``. That is a good design
and it is complete for a local push. ``gh pr merge`` and the green button in the
GitHub UI do no local push — the merge commit is created server-side — so the
board never hears about the route it most often moves by. Two consumers quietly
under-report as a result: ``GET /sync``'s staleness advisory, whose whole premise
is "a peer published something you don't have", and #83's rebase-on-published.

So the board asks GitHub directly. This is the board's first periodic mechanism;
everything else here expires lazily at request time (see the "No reaper" note in
``models/resource_lease.py``). It earns the exception because there is no request
to hang it off: the whole point is that *nothing local happens* when a PR is
merged on github.com.

**Attribution.** #127 is explicit that whatever emits this must not sign the
merge as whoever noticed it, and a poller is a noticer. These posts are authored
``github`` — a fact reported by GitHub, not a claim by an agent — parallel to the
existing ``ci`` author, which is likewise a name rather than a person.

**Announcing what we find, rather than seeding quietly.** On first sight of a
repo we announce the current head if the board has not already got it, instead of
recording it silently and only reporting later movement. Seeding would be tidier
and would miss exactly the case this module exists for: a PR merged while the
board was down or before it watched the repo. The cost of announcing is nil,
because ``missing_published`` only counts a commit against a worktree that
actually lacks it — a checkout already holding the head is not made stale by
hearing about it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from app import github
from app.config import settings
from app.db import async_session
from app.models.post import Post
from app.models.worktree import Worktree
from app.sync import PUBLISH_SCAN, ref_value, repo_key, same_commit

log = logging.getLogger("app.origin")

#: The author on a poller-emitted publish. Not an agent, deliberately.
POLLER_AUTHOR = "github"

#: Let the app finish coming up before the first cycle, so a crash-loop cannot
#: turn into a request-per-restart against GitHub.
STARTUP_DELAY = 15

#: Default branch names, learned once per process. A repo's default branch
#: changes about never, and re-asking each cycle would double our call count for
#: an answer that does not move.
_default_branches: dict[str, str] = {}

#: Cycles still to skip for a repo GitHub would not answer for, and the failure
#: count behind it. Without this a dead registration costs a call *every cycle,
#: for ever*: a smoke test against the real API spent 17 of an anonymous budget
#: of 60 on repos that no longer exist, and would have done so again minutes
#: later. Backoff rather than a permanent blacklist, so a rename, a private
#: window or an outage recovers on its own.
_cooldown: dict[str, int] = {}
_failures: dict[str, int] = {}

#: Cap on the backoff, in cycles. At the default 300s interval this is ~5 hours
#: between retries of a repo that has gone for good.
MAX_COOLDOWN = 60

#: Ignore a registration nobody has refreshed in this long. `report_git` rewrites
#: a device's rows every time it runs, so a row this old belongs to a checkout
#: that is gone or a machine that has not been on in weeks — and the advisory it
#: would feed has nobody to read it. Generous, because being wrong here means
#: silently not watching a repo somebody does care about.
STALE_REGISTRATION_DAYS = 30


async def watched_repos(db) -> list[str]:
    """Repos to poll: the "owner/name" slugs `report_git` has recently registered.

    ``worktrees.repo`` holds whatever ``repo_slug()`` made of the origin remote,
    which is ``owner/name`` for a GitHub remote and NULL otherwise. Anything
    without a slash cannot be addressed on the API, so it is not ours to poll.

    Registrations older than ``STALE_REGISTRATION_DAYS`` are dropped. Every repo
    in this list costs a GitHub call per cycle out of a budget that is shared and,
    unauthenticated, small — so a checkout nobody has reported in a month should
    not be spending it.
    """
    cutoff = datetime.now(UTC) - timedelta(days=STALE_REGISTRATION_DAYS)
    rows = await db.execute(
        select(Worktree.repo)
        .distinct()
        .where(Worktree.repo.is_not(None), Worktree.updated_at >= cutoff)
    )
    return sorted({repo for (repo,) in rows if repo and "/" in repo})


def _note_failure(repo: str) -> None:
    """Back a repo off after GitHub declines to answer for it."""
    _failures[repo] = _failures.get(repo, 0) + 1
    _cooldown[repo] = min(2 ** (_failures[repo] - 1), MAX_COOLDOWN)


def _note_success(repo: str) -> None:
    _failures.pop(repo, None)
    _cooldown.pop(repo, None)


def _cooling_off(repo: str) -> bool:
    """True if this repo is being skipped this cycle; decrements its counter."""
    left = _cooldown.get(repo, 0)
    if left <= 0:
        return False
    _cooldown[repo] = left - 1
    return True


async def already_announced(db, repo: str, sha: str) -> bool:
    """Has this commit already been announced for this repo?

    Scans the same window ``GET /sync`` reads, because that is the only window in
    which a duplicate could be observed. Guards three real cases: CI announced
    the merge first (it usually does), an agent pulled and published it, or this
    process restarted and is re-reading a head it already reported.
    """
    rows = (
        await db.execute(
            select(Post)
            .where(Post.type == "published")
            .order_by(Post.id.desc())
            .limit(PUBLISH_SCAN)
        )
    ).scalars().all()

    want = repo_key(repo)
    for post in rows:
        if repo_key(ref_value(post.refs, "repo")) != want:
            continue
        if same_commit(ref_value(post.refs, "commit"), sha):
            return True
    return False


async def _default_branch(client: httpx.AsyncClient, repo: str) -> str | None:
    cached = _default_branches.get(repo)
    if cached:
        return cached
    branch = await github.fetch_default_branch(client, repo)
    if branch:
        _default_branches[repo] = branch
    return branch


async def poll_repo(db, client: httpx.AsyncClient, repo: str) -> str | None:
    """Check one repo's default branch; announce its head if that is news.

    Returns the SHA announced, or None when there was nothing to say.
    """
    branch = await _default_branch(client, repo)
    if not branch:
        _note_failure(repo)
        return None

    head = await github.fetch_head(client, repo, branch)
    if not head:
        # The branch we cached may have been renamed out from under us; drop it
        # so the next attempt re-asks rather than retrying a dead path.
        _default_branches.pop(repo, None)
        _note_failure(repo)
        return None

    _note_success(repo)
    if await already_announced(db, repo, head.sha):
        return None

    db.add(
        Post(
            author=POLLER_AUTHOR,
            type="published",
            summary=head.subject,
            refs=[
                {"kind": "repo", "value": repo},
                {"kind": "commit", "value": head.sha, "repo": repo},
                {"kind": "branch", "value": branch},
            ],
        )
    )
    await db.commit()
    log.info("origin: %s@%s -> %s (%s)", repo, branch, head.sha[:7], head.subject)
    return head.sha


async def poll_cycle(db, client: httpx.AsyncClient) -> list[str]:
    """One pass over every watched repo. Returns the SHAs announced."""
    if github.budget_spent():
        log.warning("origin: skipping cycle, GitHub budget low (%s left)", github.remaining())
        return []

    announced: list[str] = []
    for repo in await watched_repos(db):
        if _cooling_off(repo):
            continue
        if github.budget_spent():
            log.warning("origin: stopping cycle early, GitHub budget low")
            break
        try:
            sha = await poll_repo(db, client, repo)
        except Exception:
            # One bad repo must not cost the others their turn.
            log.exception("origin: polling %s failed", repo)
            await db.rollback()
            continue
        if sha:
            announced.append(sha)
    return announced


async def run(interval: int) -> None:
    """The poll loop. Cancelled by the lifespan on shutdown."""
    auth = "authenticated" if settings.github_token_value else "anonymous (60/hour, shared IP)"
    log.info("origin: watching every %ss, %s", interval, auth)
    await asyncio.sleep(STARTUP_DELAY)
    while True:
        try:
            async with httpx.AsyncClient() as client, async_session() as db:
                await poll_cycle(db, client)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The loop outlives any one failure; a dead poller is a silent one.
            log.exception("origin: poll cycle failed")
        await asyncio.sleep(interval)
