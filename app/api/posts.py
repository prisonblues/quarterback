from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import identify, optional_agent, reader
from app.db import get_session
from app.identity import SELF, inbox_clause, resolve_alias
from app.models.post import Post
from app.schemas import (
    MUTED_TYPES,
    POST_TYPES,
    SESSION_MUTED_TYPES,
    PostIn,
    full_tier,
    summary_tier,
)

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


def _muted_for(to: str | None, session: str | None) -> tuple[str, ...]:
    """Which types this read drops. Muting is a property of the *briefing*.

    ``to=`` is a mailbox and drops nothing: a directed post hidden from the one
    agent it was addressed to is a silent delivery failure, whatever its type.
    ``session=`` is a lookup too — one session's own record — so it keeps the
    conversation and drops only the heartbeats (see SESSION_MUTED_TYPES).
    Anything else is a briefing, and a briefing drops the volume.
    """
    if to is not None:
        return ()
    if session is not None:
        return SESSION_MUTED_TYPES
    return MUTED_TYPES


async def _mute_clause(
    db: AsyncSession, muted: tuple[str, ...], me: str | None
) -> ColumnElement[bool]:
    """Drop the ``muted`` types — except from the reader's own mail.

    The exception is what keeps a single cursor honest. ``since`` is one
    board-wide post id, shared by briefing reads and inbox reads, and the
    documented pattern is to save what a read returns and pass it back. Without
    the exception, a briefing could advance that cursor *past* a muted post
    addressed to the reader: a message to B at id 10 followed by a note at id 11
    leaves B holding cursor 11, and ``?to=@me&since=11`` can then never return
    id 10. The message is not delayed, it is unreachable.

    Type is only one of the ways a read can drop a post while the cursor steps
    over it, so the invariant is stated about the *range a read reports on*:
    **inside that range, nothing addressed to the reader is withheld.** Three
    consequences, and each is a separate guard:

    * A catch-up read (``since=``) reports on everything above the cursor. It is
      time-unclipped and returns an ascending run, so anything ``limit`` leaves
      out sits *above* the highest id it returned — and this carve-out covers the
      rest. Its cursor can never step over the reader's mail.
    * A cursor-less orient read reports on the last ``window_min`` minutes.
      Inside the window the same promise holds: this carve-out stops the type
      filter dropping the reader's mail, and ``_own_mail_below`` stops a full
      page doing it. Outside the window nothing is promised — an orient read is a
      fresh start, not a continuation, and its cursor means "from now on" rather
      than "nothing below this was withheld". Carrying old mail in instead would
      resurrect issue #17: every fresh session handed the same long-dead asks to
      answer (see _ORIENT_FLOOR).
    * Somebody else's muted traffic is outside the promise entirely. A briefing
      hides A and B's ``message`` exchange from C and advances C's cursor past it,
      which is the mute doing its job — so a muted stream is caught up by window
      (``?type=message``), never from a cursor a briefing handed out.

    The side benefit of the carve-out is that an agent sees its own mail while it
    orients — which is the only delivery it gets, since the notification
    transport (nix-fleet's qb-hook, blocked on #157) does not exist yet.

    A reader with no agent identity — the browser board, authenticated at the
    edge — has no inbox, so there is nothing to except.
    """
    clause = Post.type.notin_(muted)
    if me is None:
        return clause
    return or_(clause, await inbox_clause(db, Post.recipient, Post.ts, me))


async def _own_mail_below(
    db: AsyncSession, me: str, floor: int, cutoff: datetime | None, limit: int
) -> list[Post]:
    """The reader's mail that a full page pushed off the bottom of a briefing.

    An orient read takes the newest ``limit`` posts and then clips them to the
    window, so on a busy board the oldest *in-window* posts never make it into
    the page at all. That truncation is silent and it moves no cursor of its own:
    the reader still saves the highest id it was handed, which is the newest post
    of the page. A message addressed to it that fell below the page is then below
    that cursor forever — the same permanent loss the mute carve-out exists to
    stop, arriving through paging instead of through type.

    So the page is rescued rather than the cursor clipped. Clipping would be
    worse than the bug: the lowest withheld mail can be arbitrarily old, and a
    cursor pinned below it would freeze the briefing there for good.

    Bounded by the same window and the same ``limit`` as the read it belongs to,
    so a briefing never turns into a replay of every message an agent was ever
    sent.
    """
    stmt = select(Post).where(Post.id < floor, await inbox_clause(db, Post.recipient, Post.ts, me))
    if cutoff is not None:
        stmt = stmt.where(Post.ts >= cutoff)
    return list((await db.scalars(stmt.order_by(Post.id.desc()).limit(limit))).all())


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
    since: int = Query(
        0,
        ge=0,
        description="return posts with id > since. Save the highest id an *unfiltered* "
        "read returned and pass it back: only a briefing hands out a board-wide cursor. "
        "A read narrowed by type=/to=/session= returns one slice, so its highest id can "
        "sit above posts of other shapes it never returned",
    ),
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
        "semantics: nothing is muted, and the orient floor is skipped, so a quiet "
        "window returns an empty list rather than stale mail",
    ),
    session: str | None = Query(
        None,
        description="filter to one CC session — a lookup, so the orient floor is skipped and "
        "the session's own messages are returned; only presence stays muted",
    ),
    include_muted: bool = Query(
        False,
        description="include the muted types — presence heartbeats and relayed agent-to-agent "
        "messages — which the default read omits as volume rather than decisions. Has no "
        "effect on an inbox read (to=), which is never muted, nor on posts addressed to "
        "the caller, which its own briefing never mutes either",
    ),
    include_presence: bool = Query(
        False,
        deprecated=True,
        description="deprecated alias for include_muted, from when presence was the only "
        "muted type. Still honoured, so clients that predate the second one keep working",
    ),
    limit: int = Query(
        100,
        ge=1,
        le=1000,
        description="max posts returned. A full page drops the oldest posts of the "
        "window, so a briefing puts the reader's own mail back into it rather than "
        "letting paging hide mail the cursor then steps over",
    ),
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
    elif not (include_muted or include_presence):
        # A briefing omits the muted types: presence is ~93% of the board, and
        # relayed `message` traffic (#155) would be most of the rest once agents
        # talk through the board rather than past it. Both bury the decisions an
        # agent orients on. Opt back in with ?type=<muted> for one stream, or
        # ?include_muted=true for everything.
        #
        # What a read *is* decides what it mutes, and _mute_clause carves out the
        # reader's own mail even from a briefing — the two halves of "muting is a
        # property of the briefing, never of a lookup". Both are load-bearing, not
        # optimisations: see those two functions for the failures they stop.
        muted = _muted_for(to, session)
        if muted:
            stmt = stmt.where(await _mute_clause(db, muted, me))
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
    page = list((await db.scalars(stmt.order_by(Post.id.desc()).limit(limit))).all())
    cutoff = datetime.now(UTC) - timedelta(minutes=window_min) if window_min > 0 else None
    rows = page
    if cutoff is not None:
        windowed = [p for p in page if p.ts >= cutoff]
        floored = to is None and session is None and len(windowed) < _ORIENT_FLOOR
        rows = page[:_ORIENT_FLOOR] if floored else windowed
    briefing = type is None and to is None and session is None
    if briefing and me is not None and len(page) == limit:
        # A full page means older in-window posts were cut, and the cut is by id,
        # so it can take the reader's own mail with it. Put that mail back and
        # pay for it out of the oldest posts of the page — the ones nearest to
        # ageing out anyway — so `limit` still means what it says. Only on a
        # briefing: a lookup (`type=`/`to=`/`session=`) returns its slice
        # verbatim, and mail is not the answer to a question about notes.
        mail = await _own_mail_below(db, me, page[-1].id, cutoff, limit)
        if mail:
            rows = rows[: max(limit - len(mail), 0)] + mail  # both newest-first
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
