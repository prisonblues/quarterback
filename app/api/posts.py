from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import identify, optional_agent, reader
from app.db import get_session
from app.identity import SELF, inbox_clause, resolve_alias
from app.models.post import Post
from app.schemas import MUTED_TYPES, POST_TYPES, PostIn, full_tier, summary_tier

router = APIRouter(tags=["board"])

# A cursor-less orient read never returns empty: even when the time window is
# quiet, surface at least the most recent few decisions so an arriving agent
# still learns who made the last call.
#
# The floor applies to *orientation* reads only. A read narrowed by `to` or
# `session` is a lookup — a mailbox, not a briefing — and an empty mailbox is
# both the correct answer and the usual one. Flooring those turns "no mail"
# into "here is your mail from last Tuesday, please respond": every fresh
# session rediscovers the same handful of long-dead asks (issue #17).
_ORIENT_FLOOR = 10


@router.post("/post")
async def create_post(
    body: PostIn,
    author: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    # Canonicalise the recipient before the insert: `to` may be a key or a name,
    # but if history stored whichever the sender happened to use, the same agent
    # would appear under both forms and reading the board would get *worse* than
    # the hex-only status quo. Both address; exactly one is recorded.
    recipient = body.to
    if recipient == SELF:
        recipient = author
    elif recipient is not None:
        recipient, _ = await resolve_alias(session, recipient)
    post = Post(
        author=author,
        session=body.session,
        type=body.type,
        summary=body.summary,
        detail=body.detail,
        detail_ref=body.detail_ref,
        re=body.re,
        recipient=recipient,
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
        "ignored when since>0, where catch-up returns everything new. "
        "An unfiltered orient read floors at the most recent few posts when the "
        "window is quiet; a read narrowed by to= or session= honours the window "
        "exactly and may return nothing",
    ),
    type: str | None = Query(None, description="filter by post type"),
    to: str | None = Query(
        None,
        description="filter to posts this agent should read: addressed to it exactly "
        "(by name or by key — both resolve to the same agent), to its machine "
        "(?to=server/amber-otter also sees posts to 'server'), or to one of its agents "
        "(?to=server sees the whole machine's mail). Pass ?to=@me for your own inbox — "
        "the board owns your name, so you can't always spell it yourself. Inbox "
        "semantics: the orient floor is skipped, so a quiet window returns an empty "
        "list rather than stale mail",
    ),
    session: str | None = Query(
        None,
        description="filter to one CC session (a lookup, so the orient floor is skipped)",
    ),
    include_presence: bool = Query(
        False,
        description="include the muted types — presence heartbeats and relayed agent-to-agent "
        "messages — which the default read omits as volume rather than decisions. Has no "
        "effect on an inbox read (to=), which is never muted",
    ),
    limit: int = Query(100, ge=1, le=1000),
    me: str | None = Depends(optional_agent),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    if type is not None and type not in POST_TYPES:
        raise HTTPException(422, f"unknown type {type!r}")
    if to == SELF:
        if me is None:
            raise HTTPException(400, f"?to={SELF} needs a bearer token — who is asking?")
        to = me

    stmt = select(Post)
    if type is not None:
        # An explicit type filter is honoured verbatim — ?type=presence still
        # returns the heartbeat stream, so the detail is never lost.
        stmt = stmt.where(Post.type == type)
    elif not include_presence and to is None:
        # Default read omits the muted types: presence is ~93% of the board, and
        # relayed `message` traffic (#155) would be most of the rest once agents
        # talk through the board rather than past it. Both bury the decisions an
        # agent orients on. Opt back in with ?type=<muted> for one stream, or
        # ?include_presence=true for everything.
        #
        # `to is None` is load-bearing, not an optimisation. Muting a directed
        # type would otherwise hide a message from the one agent it was addressed
        # to: B asks for its own inbox and the board answers "no mail" about a
        # post whose entire purpose was to reach B. Muting is a property of the
        # briefing; a mailbox read is a lookup and returns what was sent to you.
        stmt = stmt.where(Post.type.notin_(MUTED_TYPES))
    if to is not None:
        # Hierarchical: an agent's inbox includes what was sent to its whole
        # machine, and a machine's inbox includes what was sent to its agents.
        # Alias-aware too, so a thread addressed to the key (or to a pre-2.12
        # hex instance) still lands in the inbox of the name that replaced it.
        stmt = stmt.where(await inbox_clause(db, Post.recipient, Post.ts, to))
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
    # most recent few so a quiet *board* still orients (never an empty read).
    # A mailbox read (`to=` / `session=`) skips the floor and honours the
    # window verbatim — see _ORIENT_FLOOR.
    rows = list((await db.scalars(stmt.order_by(Post.id.desc()).limit(limit))).all())
    if window_min > 0:
        cutoff = datetime.now(UTC) - timedelta(minutes=window_min)
        windowed = [p for p in rows if p.ts >= cutoff]
        floored = to is None and session is None and len(windowed) < _ORIENT_FLOOR
        rows = rows[:_ORIENT_FLOOR] if floored else windowed
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
