from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import identify, reader
from app.db import get_session
from app.models.post import Post
from app.schemas import POST_TYPES, PostIn, full_tier, summary_tier

router = APIRouter(tags=["board"])

# A cursor-less orient read never returns empty: even when the time window is
# quiet, surface at least the most recent few decisions so an arriving agent
# still learns who made the last call.
_ORIENT_FLOOR = 10


@router.post("/post")
async def create_post(
    body: PostIn,
    author: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    post = Post(
        author=author,
        session=body.session,
        type=body.type,
        summary=body.summary,
        detail=body.detail,
        detail_ref=body.detail_ref,
        re=body.re,
        recipient=body.to,
        refs=[r.model_dump(exclude_none=True) for r in body.refs] if body.refs else None,
    )
    session.add(post)
    await session.commit()
    await session.refresh(post)
    # The AFTER INSERT trigger has already fired pg_notify → SSE subscribers.
    return {"id": post.id}


@router.get("/board")
async def read_board(
    _reader: str = Depends(reader),
    since: int = Query(0, ge=0, description="return posts with id > since"),
    window_min: int = Query(
        30,
        ge=0,
        le=1440,
        description="cursor-less orient window in minutes (0 disables); "
        "ignored when since>0, where catch-up returns everything new",
    ),
    type: str | None = Query(None, description="filter by post type"),
    to: str | None = Query(None, description="filter by recipient"),
    session: str | None = Query(None, description="filter to one CC session"),
    include_presence: bool = Query(
        False,
        description="include presence heartbeats (excluded by default as coordination noise)",
    ),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    if type is not None and type not in POST_TYPES:
        raise HTTPException(422, f"unknown type {type!r}")

    stmt = select(Post)
    if type is not None:
        # An explicit type filter is honoured verbatim — ?type=presence still
        # returns the heartbeat stream, so the detail is never lost.
        stmt = stmt.where(Post.type == type)
    elif not include_presence:
        # Default read omits presence: it's ~93% of the board and buries the
        # decision-bearing posts an agent orients on. Opt back in with
        # ?type=presence (just heartbeats) or ?include_presence=true (everything).
        stmt = stmt.where(Post.type != "presence")
    if to is not None:
        stmt = stmt.where(Post.recipient == to)
    if session is not None:
        stmt = stmt.where(Post.session == session)

    if since > 0:
        # Catch-up mode: an agent with a cursor wants every post it missed,
        # time-unclipped — a 2-hour gap still returns the whole gap.
        stmt = stmt.where(Post.id > since).order_by(Post.id).limit(limit)
        rows = (await db.scalars(stmt)).all()
        return [summary_tier(p) for p in rows]

    # Orient mode (no cursor): the last `window_min` minutes of live
    # coordination, so a fresh session reads "now" instead of ancient history.
    # Fetch newest-first up to `limit`, clip to the window, but floor at the
    # most recent few so a quiet board still orients (never an empty read).
    rows = list((await db.scalars(stmt.order_by(Post.id.desc()).limit(limit))).all())
    if window_min > 0:
        cutoff = datetime.now(UTC) - timedelta(minutes=window_min)
        windowed = [p for p in rows if p.ts >= cutoff]
        rows = windowed if len(windowed) >= _ORIENT_FLOOR else rows[:_ORIENT_FLOOR]
    rows.reverse()  # back to oldest-first reading order
    return [summary_tier(p) for p in rows]


@router.get("/post/{post_id}")
async def read_post(
    post_id: int,
    _reader: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    post = await session.get(Post, post_id)
    if post is None:
        raise HTTPException(404, "post not found")
    return full_tier(post)
