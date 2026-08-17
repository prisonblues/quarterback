from __future__ import annotations

import asyncio
import json

import asyncpg
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from app.auth import reader
from app.config import settings
from app.db import async_session
from app.models.post import Post
from app.schemas import summary_tier

router = APIRouter(tags=["board"])

CHANNEL = "quarterback_posts"


async def event_stream(since: int):
    """Yield SSE events: replay the backlog since `since`, then stream live.

    Ordering guarantee: we LISTEN *before* reading the backlog, so any post
    committed during replay is also queued as a live NOTIFY. Each event's id is
    tracked and monotonic, so the replay/live overlap is de-duplicated rather
    than dropped.

    **The stream carries every type, muted ones included — deliberately.** This is
    the raw tail, not a briefing. Its consumers are the human board (a monitor,
    which asks /board for ?include_muted=1 for the same reason) and #110's
    `qb board --follow`, and neither wants the server deciding what it may see.
    Filtering here would also mean filtering `presence`, which this stream has
    always carried, to enforce a `message` mute that belongs to /board's briefing —
    a surface the stream does not serve. A client that wants less filters on `type`
    as it reads, which is the only end that knows which client it is.
    """
    queue: asyncio.Queue[str] = asyncio.Queue()
    conn = await asyncpg.connect(settings.asyncpg_dsn)

    def on_notify(_c, _pid, _channel, payload: str) -> None:
        queue.put_nowait(payload)

    await conn.add_listener(CHANNEL, on_notify)
    try:
        last_id = since

        # 1. Replay the backlog since the cursor.
        async with async_session() as session:
            rows = (
                await session.scalars(select(Post).where(Post.id > since).order_by(Post.id))
            ).all()
        for p in rows:
            last_id = p.id
            yield {"event": "post", "id": str(p.id), "data": json.dumps(summary_tier(p))}

        # 2. Go live. NOTIFY payloads are already summary-tier JSON (see trigger).
        while True:
            payload = await queue.get()
            pid = json.loads(payload).get("id")
            if pid is None or pid <= last_id:
                continue  # dropped a replay/live duplicate
            last_id = pid
            yield {"event": "post", "id": str(pid), "data": payload}
    finally:
        await conn.remove_listener(CHANNEL, on_notify)
        await conn.close()


@router.get("/stream")
async def stream(
    _reader: str = Depends(reader),
    since: int = Query(0, ge=0, description="replay posts with id > since before going live"),
) -> EventSourceResponse:
    """Server-Sent Events feed of summary-tier posts."""
    return EventSourceResponse(event_stream(since))
