"""Sub-agent visibility + the collision index (v2.6).

Two coordination gaps this closes:

- **Sub-agents are invisible.** The Task/Agent tool runs inside the parent
  session and fires no lifecycle hooks, so leases/presence never see a fan-out.
  ``POST /subagent`` (+ ``/subagent/end``) register them as current-state rows —
  never posts — so they show up without adding board noise.
- **No "who's live in this dir?" query.** ``GET /active`` folds active leases
  (top-level agents) and live sub-agents into one answer, filterable by ``cwd``,
  so an agent can check a worktree for occupants before diving in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import identify, reader
from app.db import get_session
from app.identity import address_clause, addressed_to, same_machine
from app.models.lease import Lease
from app.models.post import Post
from app.models.subagent import Subagent
from app.overlap import overlap_score

router = APIRouter(tags=["coordination"])


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SubagentIn(BaseModel):
    parent_session: str = Field(min_length=1)
    agent_id: str = Field(min_length=1, description="unique per sub-agent within the parent")
    label: str | None = None  # e.g. "Explore: board frontend"
    cwd: str | None = None
    device: str | None = None
    ttl: int = Field(default=900, ge=1, le=86400)


class SubagentEndIn(BaseModel):
    parent_session: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)


def _subagent_view(s: Subagent) -> dict:
    return {
        "parent_session": s.parent_session,
        "agent_id": s.agent_id,
        "label": s.label,
        "cwd": s.cwd,
        "device": s.device,
        "holder": s.holder,
        "since": s.started_at.isoformat(),
        "expires": s.expires_at.isoformat(),
    }


async def active_subagents(
    session: AsyncSession, now: datetime, cwd: str | None = None
) -> list[Subagent]:
    """Live sub-agents (unended, unexpired), optionally scoped to a working dir."""
    stmt = select(Subagent).where(Subagent.ended_at.is_(None), Subagent.expires_at > now)
    if cwd is not None:
        stmt = stmt.where(Subagent.cwd == cwd)
    return list((await session.scalars(stmt)).all())


async def active_subagents_by_session(
    session: AsyncSession, now: datetime
) -> dict[str, list[dict]]:
    """Live sub-agents grouped by parent session — for the /sessions cards."""
    grouped: dict[str, list[dict]] = {}
    for s in await active_subagents(session, now):
        grouped.setdefault(s.parent_session, []).append(_subagent_view(s))
    return grouped


@router.post("/subagent")
async def register_subagent(
    body: SubagentIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Register (or renew) a live sub-agent under its parent session.

    Called by a Task/Agent-tool PreToolUse hook on spawn. Upserts on
    ``(parent_session, agent_id)``: re-registering renews the TTL and clears any
    prior end (``started_at`` is preserved). Never writes to the posts log.

    409 if the key already exists under a *different* holder — a token may only
    manage its own sub-agents (mirrors the lease ownership model).
    """
    now = _utcnow()
    existing = await session.scalar(
        select(Subagent).where(
            Subagent.parent_session == body.parent_session,
            Subagent.agent_id == body.agent_id,
        )
    )
    if existing is not None and not same_machine(existing.holder, holder):
        raise HTTPException(
            409,
            detail={
                "error": "sub-agent registered by another holder",
                "held_by": existing.holder,
            },
        )
    values = {
        "parent_session": body.parent_session,
        "agent_id": body.agent_id,
        "label": body.label,
        "cwd": body.cwd,
        "device": body.device,
        "holder": holder,
        "expires_at": now + timedelta(seconds=body.ttl),
        "ended_at": None,
    }
    # On conflict, refresh everything but the identity keys and started_at (a
    # renew must not reset when the sub-agent first appeared).
    set_ = {k: v for k, v in values.items() if k not in ("parent_session", "agent_id")}
    await session.execute(
        pg_insert(Subagent)
        .values(**values)
        .on_conflict_do_update(constraint="uq_subagent_parent_agent", set_=set_)
    )
    await session.commit()
    row = await session.scalar(
        select(Subagent).where(
            Subagent.parent_session == body.parent_session,
            Subagent.agent_id == body.agent_id,
        )
    )
    return _subagent_view(row)


@router.post("/subagent/end")
async def end_subagent(
    body: SubagentEndIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Mark a sub-agent finished (idempotent). Called by a PostToolUse hook.

    403 if the sub-agent belongs to another holder (mirrors lease release).
    """
    now = _utcnow()
    row = await session.scalar(
        select(Subagent).where(
            Subagent.parent_session == body.parent_session,
            Subagent.agent_id == body.agent_id,
        )
    )
    if row is None:
        return {"ended": False, "reason": "unknown subagent"}
    if not same_machine(row.holder, holder):
        raise HTTPException(403, "not your subagent")
    if row.ended_at is None:
        row.ended_at = now
        await session.commit()
    return {"ended": True, "parent_session": body.parent_session, "agent_id": body.agent_id}


@router.get("/active")
async def list_active(
    _reader: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
    cwd: str | None = Query(None, description="only agents live in this working dir"),
    repo: str | None = Query(None, description="only agents live in this git repo"),
    device: str | None = Query(None, description="only agents on this device"),
    holder: str | None = Query(
        None,
        description="only agents held by this identity; a bare machine name "
        "(?holder=zeus) matches every agent instance on it",
    ),
    mine: str | None = Query(
        None,
        description="the caller's own session id; entries owned by it are tagged own=true",
    ),
    peers_only: bool = Query(
        False,
        description="exclude the caller's own lease and its sub-agents entirely "
        "(requires `mine`) — the genuine-peers view, so an agent's own fan-out "
        "never reads as a collision",
    ),
) -> dict:
    """The collision index: who/what is live right now.

    ``agents`` are top-level sessions (active leases); ``subagents`` are their
    fan-out. Filter by ``cwd``/``repo`` to answer "is anyone already working
    here?" *before* starting, so two agents don't collide.

    Pass ``mine=<your session>`` to tag your own entries ``own=true`` (so a
    reader can signpost "yours" rather than mistaking its own sub-agents for
    peers); add ``peers_only=true`` to drop them from the result altogether.
    """
    now = _utcnow()
    lstmt = select(Lease).where(Lease.released_at.is_(None), Lease.expires_at > now)
    if cwd is not None:
        lstmt = lstmt.where(Lease.cwd == cwd)
    if repo is not None:
        lstmt = lstmt.where(Lease.repo == repo)
    if device is not None:
        lstmt = lstmt.where(Lease.device == device)
    if holder is not None:
        lstmt = lstmt.where(address_clause(Lease.holder, holder))
    leases = (await session.scalars(lstmt)).all()
    if peers_only and mine is not None:
        leases = [ln for ln in leases if ln.session != mine]
    agents = [
        {
            "session": lease.session,
            "holder": lease.holder,
            "device": lease.device,
            "cwd": lease.cwd,
            "repo": lease.repo,
            "branch": lease.branch,
            "title": lease.title,
            "model": lease.model,
            "since": lease.acquired_at.isoformat(),
            "expires": lease.expires_at.isoformat(),
            "own": mine is not None and lease.session == mine,
        }
        for lease in leases
    ]
    subs = [_subagent_view(s) for s in await active_subagents(session, now, cwd=cwd)]
    if device is not None:
        subs = [s for s in subs if s["device"] == device]
    if holder is not None:
        subs = [s for s in subs if addressed_to(s["holder"], holder)]
    for s in subs:
        s["own"] = mine is not None and s["parent_session"] == mine
    if peers_only and mine is not None:
        subs = [s for s in subs if not s["own"]]
    return {"agents": agents, "subagents": subs}


@router.get("/overlap")
async def find_overlap(
    _reader: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
    mine: str = Query(..., description="the caller's own session id (always excluded)"),
    repo: str | None = Query(None, description="restrict to peers live in this git repo"),
    subject: str | None = Query(
        None, description="the caller's title+recap; ranks peers by textual overlap with it"
    ),
    min_score: float = Query(0.12, ge=0.0, le=1.0, description="drop peers below this overlap"),
    limit: int = Query(5, ge=1, le=50),
) -> dict:
    """Self-discovery: which *other* live sessions are on the same problem as me?

    A genuine peer is a top-level agent (active lease) that is **not me and not
    my own sub-agent**, in the same ``repo``, whose session subject overlaps mine
    (see app.overlap). Each peer comes back with its latest board post so the
    caller can open a directed ``ask`` that threads onto it (``to``/``re``) —
    turning a silent collision into a conversation.

    ``subject`` present ⇒ rank by overlap and drop peers below ``min_score``.
    ``subject`` absent ⇒ every same-repo peer is returned (repo alone is the
    signal), score null.
    """
    now = _utcnow()
    lstmt = select(Lease).where(
        Lease.released_at.is_(None), Lease.expires_at > now, Lease.session != mine
    )
    if repo is not None:
        lstmt = lstmt.where(Lease.repo == repo)
    leases = (await session.scalars(lstmt)).all()

    scored: list[tuple[float | None, Lease]] = []
    for lease in leases:
        if subject:
            peer_subject = " ".join(filter(None, (lease.title, lease.recap)))
            score = overlap_score(subject, peer_subject)
            if score < min_score:
                continue
            scored.append((score, lease))
        else:
            scored.append((None, lease))
    # Highest overlap first; unscored (repo-only) peers keep lease order.
    scored.sort(key=lambda t: (t[0] is not None, t[0] or 0.0), reverse=True)

    peers = []
    for score, lease in scored[:limit]:
        last = await session.scalar(
            select(Post)
            .where(Post.session == lease.session, Post.type != "presence")
            .order_by(Post.id.desc())
            .limit(1)
        )
        peers.append({
            "session": lease.session,
            "holder": lease.holder,
            "device": lease.device,
            "repo": lease.repo,
            "branch": lease.branch,
            "title": lease.title,
            "recap": lease.recap,
            "since": lease.acquired_at.isoformat(),
            "score": round(score, 3) if score is not None else None,
            "last_post_id": last.id if last else None,
            "last_post_summary": last.summary if last else None,
        })
    return {"peers": peers}
