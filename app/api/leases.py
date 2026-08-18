from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.subagents import active_subagents_by_session
from app.auth import identify, reader
from app.db import get_session
from app.identity import retire, same_machine
from app.models.blob import Blob
from app.models.lease import Lease
from app.models.session import SessionRecord
from app.schemas import CWD_MAX

router = APIRouter(tags=["lease"])


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _lease_view(lease: Lease) -> dict:
    return {
        "lease_id": str(lease.id),
        "session": lease.session,
        "device": lease.device,
        "holder": lease.holder,
        "expires": lease.expires_at.isoformat(),
    }


async def _active_lease(session: AsyncSession, sess_key: str, now: datetime) -> Lease | None:
    """The single active lease on a session, or None. Active = unreleased and unexpired."""
    stmt = (
        select(Lease)
        .where(
            Lease.session == sess_key,
            Lease.released_at.is_(None),
            Lease.expires_at > now,
        )
        .order_by(Lease.expires_at.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def _retire_if_idle(session: AsyncSession, holder: str, now: datetime) -> None:
    """Free ``holder``'s shortname once its last live lease is gone.

    An agent can hold several sessions at once, and ending one is not the end of
    it. Retiring on the first release would hand its name away mid-life — the
    rename this whole design exists to avoid — and split the rest of its work
    across two identities.
    """
    still_working = await session.scalar(
        select(Lease.id)
        .where(Lease.holder == holder, Lease.released_at.is_(None), Lease.expires_at > now)
        .limit(1)
    )
    if still_working is None:
        await retire(session, holder)


async def _record_blob(
    session: AsyncSession, sess_key: str, blob_sha: str,
    holder: str, fields: dict, now: datetime,
) -> None:
    """Upsert the durable sessions pointer (shared by /handoff and /snapshot).

    ``fields`` carries optional metadata (device/cwd/title/recap); a None value is
    inserted but never overwrites an existing value on conflict.
    """
    base = {"latest_blob": blob_sha, "holder": holder, "updated_at": now}
    set_ = {**base, **{k: v for k, v in fields.items() if v is not None}}
    await session.execute(
        pg_insert(SessionRecord)
        .values(session=sess_key, **base, **fields)
        .on_conflict_do_update(index_elements=[SessionRecord.session], set_=set_)
    )


class LeaseIn(BaseModel):
    session: str = Field(min_length=1)
    device: str = Field(min_length=1)
    ttl: int = Field(default=300, ge=1, le=86400)
    cwd: str | None = Field(default=None, max_length=CWD_MAX)  # project dir (peer `--resume`)
    repo: str | None = None     # git repo name (topic-overlap match)
    branch: str | None = None   # git branch (finer overlap signal)
    title: str | None = None    # CC ai-title
    recap: str | None = None    # compact-summary head / last prompt
    model: str | None = None    # model id from last assistant msg


class RenewIn(BaseModel):
    lease_id: uuid.UUID


class ReleaseIn(BaseModel):
    lease_id: uuid.UUID


class HandoffIn(BaseModel):
    session: str = Field(min_length=1)
    blob: str = Field(min_length=1, description="sha of the JSONL blob already PUT to /blob")
    cwd: str | None = Field(default=None, max_length=CWD_MAX)
    title: str | None = None
    recap: str | None = None
    model: str | None = None


class SnapshotIn(BaseModel):
    """Update a live session's latest blob WITHOUT releasing the lease — the
    mid-session freshness path (Stop hook), so a peer can pull a current
    transcript. Contrast /handoff, which also releases."""
    session: str = Field(min_length=1)
    blob: str = Field(min_length=1, description="sha of the JSONL blob already PUT to /blob")
    cwd: str | None = Field(default=None, max_length=CWD_MAX)
    title: str | None = None
    recap: str | None = None
    model: str | None = None


@router.post("/lease")
async def acquire_lease(
    body: LeaseIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Claim a session, or renew if you already hold it.

    409 if a *different* device holds an active lease — that device must crash
    (lease lapses) or hand off before this one can take over. "Different" is
    judged at machine granularity: a lease belongs to the box, so a session
    reclaimed by another agent on the same machine is a renew, not a conflict.
    """
    now = _utcnow()
    active = await _active_lease(session, body.session, now)
    if active is not None and (
        not same_machine(active.holder, holder) or active.device != body.device
    ):
        raise HTTPException(
            409,
            detail={
                "error": "session is leased by another device",
                "held_by": active.holder,
                "device": active.device,
                "expires": active.expires_at.isoformat(),
            },
        )

    if active is not None:
        # Same device re-claiming — treat as a renew. Take the caller's identity:
        # a lease claimed before the holder had an instance (or by the machine
        # itself) upgrades to the live agent's address on the next heartbeat.
        active.holder = holder
        active.ttl_seconds = body.ttl
        active.expires_at = now + timedelta(seconds=body.ttl)
        if body.cwd:
            active.cwd = body.cwd
        if body.repo:
            active.repo = body.repo
        if body.branch:
            active.branch = body.branch
        if body.title:
            active.title = body.title
        if body.recap:
            active.recap = body.recap
        if body.model:
            active.model = body.model
        await session.commit()
        return {**_lease_view(active), "renewed": True}

    lease = Lease(
        session=body.session,
        device=body.device,
        holder=holder,
        ttl_seconds=body.ttl,
        expires_at=now + timedelta(seconds=body.ttl),
        cwd=body.cwd,
        repo=body.repo,
        branch=body.branch,
        title=body.title,
        recap=body.recap,
        model=body.model,
    )
    session.add(lease)
    await session.commit()
    await session.refresh(lease)
    return {**_lease_view(lease), "renewed": False}


@router.post("/lease/renew")
async def renew_lease(
    body: RenewIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    lease = await session.get(Lease, body.lease_id)
    if lease is None:
        raise HTTPException(404, "lease not found")
    if not same_machine(lease.holder, holder):
        raise HTTPException(403, "not your lease")
    if lease.released_at is not None:
        raise HTTPException(409, "lease already released; re-acquire via POST /lease")
    now = _utcnow()
    if lease.expires_at <= now:
        raise HTTPException(409, "lease expired; re-acquire via POST /lease")
    lease.expires_at = now + timedelta(seconds=lease.ttl_seconds)
    await session.commit()
    return _lease_view(lease)


@router.post("/lease/release")
async def release_lease(
    body: ReleaseIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    lease = await session.get(Lease, body.lease_id)
    if lease is None:
        raise HTTPException(404, "lease not found")
    if not same_machine(lease.holder, holder):
        raise HTTPException(403, "not your lease")
    if lease.released_at is None:
        now = _utcnow()
        lease.released_at = now
        # Releasing is SessionEnd: that agent is going. Free *its* shortname —
        # the holder's, not the caller's, since a co-tenant may release on its
        # behalf — keeping the name on everything it authored (identity.retire),
        # so the live space recycles without rewriting the past.
        #
        # Only while the lease is still live. `holder` is a name, and names
        # recycle, so a belated release of a lease that lapsed weeks ago would
        # otherwise unname whichever agent inherited it since. A lapsed lease
        # already gave up its claim; there is nothing left here to retire.
        if lease.expires_at > now:
            await _retire_if_idle(session, lease.holder, now)
        await session.commit()
    return {"lease_id": str(lease.id), "released": True}


@router.post("/handoff")
async def handoff(
    body: HandoffIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record the session's latest JSONL blob and release your lease.

    Requires that you hold the active lease and that the blob has already been
    PUT — the sessions row is the durable pointer a peer pulls after claiming.
    """
    now = _utcnow()
    active = await _active_lease(session, body.session, now)
    if active is None or not same_machine(active.holder, holder):
        raise HTTPException(409, "you do not hold an active lease on this session")
    if await session.get(Blob, body.blob.lower()) is None:
        raise HTTPException(400, "unknown blob; PUT it to /blob/<sha> first")

    await _record_blob(session, body.session, body.blob.lower(), holder, {
        "device": active.device,
        "cwd": body.cwd or active.cwd,
        "title": body.title or active.title,
        "recap": body.recap or active.recap,
        "model": body.model or active.model,
    }, now)
    active.released_at = now
    await _retire_if_idle(session, active.holder, now)  # handoff releases — that agent is done
    await session.commit()
    return {
        "session": body.session,
        "latest_blob": body.blob.lower(),
        "released_lease": str(active.id),
    }


@router.post("/snapshot")
async def snapshot(
    body: SnapshotIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Update a live session's latest blob without releasing the lease.

    The mid-session freshness path (Stop hook): a peer can pull a current
    transcript. Requires you hold the active lease and the blob is already PUT.
    """
    now = _utcnow()
    active = await _active_lease(session, body.session, now)
    if active is None or not same_machine(active.holder, holder):
        raise HTTPException(409, "you do not hold an active lease on this session")
    if await session.get(Blob, body.blob.lower()) is None:
        raise HTTPException(400, "unknown blob; PUT it to /blob/<sha> first")
    await _record_blob(session, body.session, body.blob.lower(), holder, {
        "device": active.device,
        "cwd": body.cwd or active.cwd,
        "title": body.title or active.title,
        "recap": body.recap or active.recap,
        "model": body.model or active.model,
    }, now)
    await session.commit()
    return {"session": body.session, "latest_blob": body.blob.lower()}


@router.get("/sessions")
async def list_sessions(
    _reader: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict]:
    """All known sessions — live (held now) and resumable (handed off) — with
    freshness and transcript size, for the board + `qb sessions`/`qb resume`."""
    now = _utcnow()
    records = (
        await session.scalars(
            select(SessionRecord).order_by(SessionRecord.updated_at.desc()).limit(limit)
        )
    ).all()
    active = (
        await session.scalars(
            select(Lease).where(Lease.released_at.is_(None), Lease.expires_at > now)
        )
    ).all()

    shas = {r.latest_blob for r in records if r.latest_blob}
    sizes: dict[str, int] = {}
    if shas:
        rows = await session.execute(select(Blob.sha, Blob.size).where(Blob.sha.in_(shas)))
        sizes = dict(rows.all())

    live = {lease.session: lease for lease in active}
    out: dict[str, dict] = {}
    for r in records:
        lv = live.get(r.session)
        out[r.session] = {
            "session": r.session,
            "cwd": r.cwd,
            "title": (lv.title if lv else None) or r.title,
            "recap": (lv.recap if lv else None) or r.recap,
            "model": (lv.model if lv else None) or r.model,
            "device": (lv.device if lv else r.device),
            "holder": r.holder,
            "updated_at": r.updated_at.isoformat(),
            "blob": r.latest_blob,
            "size": sizes.get(r.latest_blob) if r.latest_blob else None,
            "live": lv is not None,
            "resumable": r.latest_blob is not None,
        }
    for lease in active:  # live sessions not yet handed off (no record)
        out.setdefault(lease.session, {
            "session": lease.session,
            "cwd": lease.cwd,
            "title": lease.title,
            "recap": lease.recap,
            "model": lease.model,
            "device": lease.device,
            "holder": lease.holder,
            "updated_at": lease.acquired_at.isoformat(),
            "blob": None,
            "size": None,
            "live": True,
            "resumable": False,
        })

    subs_by_session = await active_subagents_by_session(session, now)
    for s in out.values():
        s["subagents"] = subs_by_session.get(s["session"], [])

    return sorted(out.values(), key=lambda s: (s["live"], s["updated_at"]), reverse=True)[:limit]


@router.get("/session/{session_key}")
async def get_session_state(
    session_key: str,
    _reader: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Peer discovery: the latest handed-off blob plus any active lease.

    A peer claims (POST /lease) then GETs ``latest_blob`` from /blob to resume.
    """
    now = _utcnow()
    record = await session.get(SessionRecord, session_key)
    active = await _active_lease(session, session_key, now)
    if record is None and active is None:
        raise HTTPException(404, "unknown session")
    return {
        "session": session_key,
        "latest_blob": record.latest_blob if record else None,
        "cwd": (record.cwd if record else None) or (active.cwd if active else None),
        "title": (record.title if record else None) or (active.title if active else None),
        "recap": (record.recap if record else None) or (active.recap if active else None),
        "model": (record.model if record else None) or (active.model if active else None),
        "device": record.device if record else None,
        "holder": record.holder if record else None,
        "updated_at": record.updated_at.isoformat() if record else None,
        "active_lease": _lease_view(active) if active else None,
    }
