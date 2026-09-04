"""The cross-worktree discovery index: who has which checkout, and which commit.

## One repository, one stored spelling (#350)

``worktrees.repo`` is a **repository**, not a label. It is written from
``mcp_server.gitctx.repo_slug`` — ``owner/name`` read off ``remote.origin.url``,
or NULL where the remote is not one — so unlike ``leases.repo`` (a *report*, from
a heartbeat that may only know a bare name; see :mod:`app.repomatch`) it can and
should be canonical. It was not: the column stored whatever the device sent
and ``GET /worktrees?repo=`` compared it with ``==``, so a device whose remote is
spelled ``PrisonBlues/Quarterback`` registered a repository the board held apart
from the same one registered as ``prisonblues/quarterback``.

The fix is #326's, which #349 settled for the review tables: fold on the write
through :func:`app.claimkey.canonical_repo`, hold it there with a CHECK
constraint, and canonicalise the caller's string at the read so the two halves
meet. Migration ``0034`` folds the rows written before it.

## Why the read still accepts a bare name, when nothing else does

``GET /worktrees?repo=`` and ``GET /sync?repo=`` read one column and disagreed
about what "the same repo" means: ``/sync`` folds through
:func:`app.sync.repo_key` (basename, lower-cased) while this endpoint compared
exactly. That is not a stylistic difference — the board TUI locates a checkout to
cherry-pick into by passing the ``repo`` ref off a *post*, and the lifecycle hook
tagged posts with the checkout's **basename**. So the one caller in the tree that
uses this filter has only ever been able to spell the bare name, and this
endpoint has been answering it with ``[]`` — the false-clean that #326 is about,
rendered as "no registered checkout of quarterback on zeus".

#714 makes the hook report ``owner/name`` where it can, so new posts carry the
qualified spelling and hit the exact tier. The bare-name tier does not retire with
it: a checkout whose remote is not a GitHub one has no other name, every post
already on the board carries the old one, and a hook rolls out across a fleet
rather than at an instant.

So the filter is two-tier and the tiers cannot be confused with each other:
``owner/name`` is canonicalised and matched exactly, and a **bare name** — the
one spelling ``REPO_RE`` refuses precisely because it is ambiguous — is matched
by basename, which is the answer to an ambiguous question that names everything
it could mean. It widens a read; it never widens the column, which stays
``owner/name`` because nothing but ``canonical_repo`` can write it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import identify, reader
from app.claimkey import BadRef, canonical_repo
from app.db import get_session
from app.models.worktree import Worktree
from app.repomatch import asked_repo, canonical_clause

router = APIRouter(tags=["worktree"])


def _repo_where(stmt: Select, repo: str) -> Select:
    """Narrow to one repository, however the caller spelled it — see the module docstring.

    ``owner/name`` is folded and compared against the column, which is indexed and
    exact. A bare name is compared against the column's basename, the same rule
    ``/sync`` applies to the same column, so the two endpoints finally agree about
    what counts as the same repo. Anything that is neither is a 422 rather than an
    empty list: a clone URL or a path answered with ``[]`` reads as "nothing is
    registered" when it means "I could not tell what you asked about".

    The rule itself moved to :mod:`app.repomatch` with #714, unchanged, when a
    fourth read needed it and had written none — the exact-then-basename tiering
    here, and the gate that refuses a third spelling rather than answering it.
    Three copies of a two-tier rule is two copies that can drift.
    """
    return stmt.where(canonical_clause(Worktree.repo, asked_repo(repo)))


def _canonical_repo_or_none(repo: str | None) -> str | None:
    """The repo a device is registering, folded — or None where it has no remote.

    ``repo_slug`` returns None for a checkout whose origin is not a GitHub-style
    remote, and a blank string is the same fact arriving from an older client, so
    both become NULL: a row whose repo is ``''`` would be a repository no query
    can name, and ``/sync`` selects on ``repo IS NOT NULL`` to mean "has one".
    """
    if repo is None or not repo.strip():
        return None
    try:
        return canonical_repo(repo)
    except BadRef as e:
        raise HTTPException(422, detail={"error": str(e), "repo": repo}) from e


class Commit(BaseModel):
    sha: str = Field(min_length=1)
    subject: str = ""


class WorktreeIn(BaseModel):
    path: str = Field(min_length=1)
    repo: str | None = None
    branch: str | None = None
    head: str | None = None
    commits: list[Commit] = Field(default_factory=list)
    # Sync state (v2.8) — optional so an older MCP server still registers cleanly.
    upstream: str | None = None
    remote_sha: str | None = None
    ahead: int | None = Field(default=None, ge=0)
    behind: int | None = Field(default=None, ge=0)
    dirty: bool | None = None


class WorktreesIn(BaseModel):
    device: str = Field(min_length=1)
    worktrees: list[WorktreeIn]


def _view(w: Worktree) -> dict:
    return {
        "device": w.device,
        "path": w.path,
        "repo": w.repo,
        "branch": w.branch,
        "head_sha": w.head_sha,
        "commits": w.commits or [],
        "upstream": w.upstream,
        "remote_sha": w.remote_sha,
        "ahead": w.ahead,
        "behind": w.behind,
        "dirty": w.dirty,
        "updated_at": w.updated_at.isoformat(),
    }


def _has_commit(w: Worktree, sha: str) -> bool:
    sha = sha.lower()
    if w.head_sha and w.head_sha.lower().startswith(sha):
        return True
    return any(str(c.get("sha", "")).lower().startswith(sha) for c in w.commits or [])


@router.put("/worktrees")
async def register_worktrees(
    body: WorktreesIn,
    _author: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Replace a device's registered worktrees with a fresh snapshot.

    A full replace (not merge) so worktrees removed on the device disappear here
    too, keeping cross-worktree discovery from surfacing stale entries.

    Every ``repo`` is folded through :func:`app.claimkey.canonical_repo` first, so
    a remote spelled with capitals registers under the one spelling every read
    looks for. A value that is not ``owner/name`` is a 422 for the whole snapshot
    rather than a row stored under a second identity — the same refusal the claim,
    plan and review writes make, and the reason it can be this strict is that the
    only client derives the value from ``remote.origin.url`` and already sends
    nothing at all when it cannot.
    """
    repos = [_canonical_repo_or_none(wt.repo) for wt in body.worktrees]
    await session.execute(delete(Worktree).where(Worktree.device == body.device))
    for wt, repo in zip(body.worktrees, repos, strict=True):
        session.add(
            Worktree(
                device=body.device,
                path=wt.path,
                repo=repo,
                branch=wt.branch,
                head_sha=wt.head,
                commits=[c.model_dump() for c in wt.commits] or None,
                upstream=wt.upstream,
                remote_sha=wt.remote_sha,
                ahead=wt.ahead,
                behind=wt.behind,
                dirty=wt.dirty,
            )
        )
    await session.commit()
    return {"device": body.device, "count": len(body.worktrees)}


@router.get("/worktrees")
async def list_worktrees(
    _reader: str = Depends(reader),
    device: str | None = Query(None),
    repo: str | None = Query(
        None, description="owner/name, or the bare name — see the module docstring"
    ),
    branch: str | None = Query(None),
    has_commit: str | None = Query(
        None, min_length=7, description="find worktrees holding this sha"
    ),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    stmt = select(Worktree)
    if device is not None:
        stmt = stmt.where(Worktree.device == device)
    if repo is not None:
        stmt = _repo_where(stmt, repo)
    if branch is not None:
        stmt = stmt.where(Worktree.branch == branch)
    stmt = stmt.order_by(Worktree.device, Worktree.path)

    rows: list[Worktree] = list((await session.scalars(stmt)).all())
    if has_commit is not None:
        rows = [w for w in rows if _has_commit(w, has_commit)]
    return [_view(w) for w in rows]
