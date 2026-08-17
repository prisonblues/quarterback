from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import reader
from app.db import get_session
from app.models.post import Post
from app.models.worktree import Worktree
from app.sync import MIN_SHA, advice, ref_value, repo_key, worktree_state

router = APIRouter(tags=["sync"])

# How far back the published line is considered. Deep enough that a device idle
# for a few days still gets an accurate count, shallow enough to stay one cheap
# indexed query.
_PUBLISH_SCAN = 200


def _published_entries(posts: list[Post], repo: str) -> list[dict]:
    """`published` posts for this repo as {sha, from, branch, ...}, newest-first.

    One entry per commit. The same SHA can legitimately be announced more than
    once — a local push fires the lifecycle hook while CI announces the same
    commit on merge — and counting it twice would report a checkout as two
    commits behind when it is one. The first (newest) announcement wins.
    """
    want = repo_key(repo)
    entries: list[dict] = []
    seen: set[str] = set()
    for p in posts:
        if repo_key(ref_value(p.refs, "repo")) != want:
            continue
        sha = ref_value(p.refs, "commit")
        if not sha:
            continue  # a publish that names no commit can't be compared against
        # Announcements may abbreviate differently, so key on a common prefix.
        key = sha.lower()[:MIN_SHA]
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "id": p.id,
                "sha": sha,
                "from": p.author,
                "branch": ref_value(p.refs, "branch"),
                "summary": p.summary,
                "ts": p.ts.isoformat(),
            }
        )
    return entries


def _on_branch(entries: list[dict], branch: str | None) -> list[dict]:
    """Publishes relevant to one branch line.

    A publish that names no branch is taken to be on whatever line we're asking
    about — a hand-written post shouldn't vanish for omitting a ref. A worktree
    with no branch (detached) is compared against everything, since we can't tell
    which line it belongs to.
    """
    if branch is None:
        return entries
    return [e for e in entries if e["branch"] in (None, branch)]


@router.get("/sync")
async def sync_status(
    _reader: str = Depends(reader),
    repo: str = Query(..., min_length=1, description="repo name or owner/name"),
    branch: str | None = Query(None, description="restrict to one branch line"),
    device: str | None = Query(None, description="restrict to one device's worktrees"),
    path: str | None = Query(None, description="restrict to one worktree path"),
    have: str | None = Query(
        None, description="caller's own recent SHAs, newest first, comma-separated"
    ),
    dirty: bool | None = Query(None, description="caller's working tree has uncommitted changes"),
    ahead: int | None = Query(None, ge=0, description="caller's commits not on its upstream"),
    behind: int | None = Query(None, ge=0, description="caller's upstream commits it lacks"),
    limit: int = Query(_PUBLISH_SCAN, ge=1, le=1000, description="published posts to consider"),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Is this repo in sync across the fleet — and what should I pull?

    Compares checkouts against the ``published`` line (the commits peers have
    announced as pushed) and returns a per-worktree verdict plus one actionable
    ``advice`` line.

    Two ways to ask about *your* checkout. Pass ``have`` (your recent SHAs) and
    the answer is about you, whether or not you've ever registered — that's the
    form the lifecycle hook uses, since it can't assume ``report_git`` has run.
    Otherwise scope with device/path to ask about a registered worktree.
    """
    # A client that sends `branch=` (detached HEAD, unset variable) means "no
    # branch", not "the branch named empty string" — which would match nothing.
    branch = branch or None
    posts = list(
        (
            await db.scalars(
                select(Post).where(Post.type == "published").order_by(Post.id.desc()).limit(limit)
            )
        ).all()
    )
    all_published = _published_entries(posts, repo)

    stmt = select(Worktree).where(Worktree.repo.is_not(None))
    if device is not None:
        stmt = stmt.where(Worktree.device == device)
    if path is not None:
        stmt = stmt.where(Worktree.path == path)
    if branch is not None:
        stmt = stmt.where(Worktree.branch == branch)
    rows = list((await db.scalars(stmt.order_by(Worktree.device, Worktree.path))).all())

    want = repo_key(repo)
    states = [
        worktree_state(
            {
                "device": w.device,
                "path": w.path,
                "branch": w.branch,
                "head_sha": w.head_sha,
                "commits": w.commits,
                "upstream": w.upstream,
                "remote_sha": w.remote_sha,
                "ahead": w.ahead,
                "behind": w.behind,
                "dirty": w.dirty,
                "updated_at": w.updated_at.isoformat(),
            },
            # Each worktree is judged against its own branch line: a feature
            # worktree isn't stale for lacking a publish onto main.
            _on_branch(all_published, branch or w.branch),
        )
        for w in rows
        if repo_key(w.repo) == want
    ]
    # Stale first, so a caller that reads only the head of the list sees the
    # worktree that needs action.
    states.sort(key=lambda s: (not s["stale"], s["device"] or "", s["path"] or ""))

    caller = None
    if have:
        shas = [s for s in (part.strip() for part in have.split(",")) if s]
        caller = worktree_state(
            {
                "device": _reader,
                "path": path,
                "branch": branch,
                "head_sha": shas[0] if shas else None,
                "commits": [{"sha": s} for s in shas],
                "ahead": ahead,
                "behind": behind,
                "dirty": dirty,
            },
            _on_branch(all_published, branch),
        )

    # The caller asked about itself: answer about itself, not about the fleet.
    subject = [caller] if caller else states
    on_branch = _on_branch(all_published, branch)
    # Can we answer at all? Every verdict below is a comparison against the
    # published line; with nothing on it there is nothing to compare against, and
    # `stale: false` stops meaning "you're current" and starts meaning "we didn't
    # look". Callers need those two apart — see the advice fallback below.
    comparable = bool(on_branch)
    line = advice(repo, subject)
    if caller is None and not states and (device is not None or path is not None):
        # Scoped to a specific checkout that the board has never seen. Silence
        # here would read as "you're in sync", which is the one answer we can't
        # support — say what's actually missing instead.
        line = (
            f"{repo}: this worktree isn't registered with the board, so staleness "
            f"can't be checked — run report_git, or pass `have`."
        )
    elif line is None and not comparable and caller is not None and behind is None:
        # Nothing published for this repo *and* the caller has no upstream to fall
        # back on (detached, or a branch that has never been pushed). Both sources
        # are absent, so the honest answer is "unknown", not silence.
        #
        # Deliberately narrow: with `behind` present we still hold the weak local
        # signal and stay quiet, because this line reaches an agent through the
        # hook's context injection on every session. A repo that will never run
        # CI must not nag once a session forever — the case worth breaking
        # silence for is the one where we hold no signal at all.
        line = (
            f"{repo}: nothing has been published to the board for this repo, and "
            f"this checkout reports no upstream — staleness is unknown, not clear."
        )

    return {
        "repo": repo,
        "branch": branch,
        "published": on_branch,
        "worktrees": states,
        "caller": caller,
        "registered": bool(states),
        "comparable": comparable,
        "stale": any(s["stale"] for s in subject),
        "advice": line,
    }
