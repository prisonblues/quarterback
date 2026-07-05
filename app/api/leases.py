from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import identify
from app.db import get_session
from app.models.blob import Blob
from app.models.lease import Lease
from app.models.session import SessionRecord

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


class LeaseIn(BaseModel):
    session: str = Field(min_length=1)
    device: str = Field(min_length=1)
    ttl: int = Field(default=300, ge=1, le=86400)


class RenewIn(BaseModel):
    lease_id: uuid.UUID


class ReleaseIn(BaseModel):
    lease_id: uuid.UUID


class HandoffIn(BaseModel):
    session: str = Field(min_length=1)
    blob: str = Field(min_length=1, description="sha of the JSONL blob already PUT to /blob")


@router.post("/lease")
async def acquire_lease(
    body: LeaseIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Claim a session, or renew if you already hold it.

    409 if a *different* device holds an active lease — that device must crash
    (lease lapses) or hand off before this one can take over.
    """
    now = _utcnow()
    active = await _active_lease(session, body.session, now)
    if active is not None and (active.holder != holder or active.device != body.device):
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
        # Same device re-claiming — treat as a renew.
        active.ttl_seconds = body.ttl
        active.expires_at = now + timedelta(seconds=body.ttl)
        await session.commit()
        return {**_lease_view(active), "renewed": True}

    lease = Lease(
        session=body.session,
        device=body.device,
        holder=holder,
        ttl_seconds=body.ttl,
        expires_at=now + timedelta(seconds=body.ttl),
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
    if lease.holder != holder:
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
    if lease.holder != holder:
        raise HTTPException(403, "not your lease")
    if lease.released_at is None:
        lease.released_at = _utcnow()
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
    if active is None or active.holder != holder:
        raise HTTPException(409, "you do not hold an active lease on this session")
    if await session.get(Blob, body.blob.lower()) is None:
        raise HTTPException(400, "unknown blob; PUT it to /blob/<sha> first")

    await session.execute(
        pg_insert(SessionRecord)
        .values(
            session=body.session,
            latest_blob=body.blob.lower(),
            device=active.device,
            holder=holder,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[SessionRecord.session],
            set_={
                "latest_blob": body.blob.lower(),
                "device": active.device,
                "holder": holder,
                "updated_at": now,
            },
        )
    )
    active.released_at = now
    await session.commit()
    return {
        "session": body.session,
        "latest_blob": body.blob.lower(),
        "released_lease": str(active.id),
    }


@router.get("/session/{session_key}")
async def get_session_state(
    session_key: str,
    _holder: str = Depends(identify),
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
        "device": record.device if record else None,
        "holder": record.holder if record else None,
        "updated_at": record.updated_at.isoformat() if record else None,
        "active_lease": _lease_view(active) if active else None,
    }
