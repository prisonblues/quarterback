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
from app.models.lease import Lease
from app.models.subagent import Subagent

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
    if existing is not None and existing.holder != holder:
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
    if row.holder != holder:
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
    device: str | None = Query(None, description="only agents on this device"),
    holder: str | None = Query(None, description="only agents held by this token name"),
) -> dict:
    """The collision index: who/what is live right now.

    ``agents`` are top-level sessions (active leases); ``subagents`` are their
    fan-out. Filter by ``cwd`` to answer "is anyone already working in this
    directory?" *before* starting, so two agents don't collide on one worktree.
    """
    now = _utcnow()
    lstmt = select(Lease).where(Lease.released_at.is_(None), Lease.expires_at > now)
    if cwd is not None:
        lstmt = lstmt.where(Lease.cwd == cwd)
    if device is not None:
        lstmt = lstmt.where(Lease.device == device)
    if holder is not None:
        lstmt = lstmt.where(Lease.holder == holder)
    leases = (await session.scalars(lstmt)).all()
    agents = [
        {
            "session": lease.session,
            "holder": lease.holder,
            "device": lease.device,
            "cwd": lease.cwd,
            "title": lease.title,
            "model": lease.model,
            "since": lease.acquired_at.isoformat(),
            "expires": lease.expires_at.isoformat(),
        }
        for lease in leases
    ]
    subs = [_subagent_view(s) for s in await active_subagents(session, now, cwd=cwd)]
    if device is not None:
        subs = [s for s in subs if s["device"] == device]
    if holder is not None:
        subs = [s for s in subs if s["holder"] == holder]
    return {"agents": agents, "subagents": subs}
