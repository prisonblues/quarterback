from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import identify, reader
from app.db import get_session
from app.models.worktree import Worktree

router = APIRouter(tags=["worktree"])


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
    """
    await session.execute(delete(Worktree).where(Worktree.device == body.device))
    for wt in body.worktrees:
        session.add(
            Worktree(
                device=body.device,
                path=wt.path,
                repo=wt.repo,
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
    repo: str | None = Query(None),
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
        stmt = stmt.where(Worktree.repo == repo)
    if branch is not None:
        stmt = stmt.where(Worktree.branch == branch)
    stmt = stmt.order_by(Worktree.device, Worktree.path)

    rows: list[Worktree] = list((await session.scalars(stmt)).all())
    if has_commit is not None:
        rows = [w for w in rows if _has_commit(w, has_commit)]
    return [_view(w) for w in rows]
