from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app import origin
from app.api.blobs import router as blobs_router
from app.api.board_view import router as board_view_router
from app.api.claims import router as claims_router
from app.api.leases import router as leases_router
from app.api.posts import router as posts_router
from app.api.reviews import router as reviews_router
from app.api.stream import router as stream_router
from app.api.subagents import router as subagents_router
from app.api.sync import router as sync_router
from app.api.whoami import router as whoami_router
from app.api.worktrees import router as worktrees_router
from app.config import settings

# Explicit "app" logger handler — uvicorn's dictConfig at startup drops root
# handlers, so a dedicated namespace handler survives.
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
_app_logger = logging.getLogger("app")
_app_logger.setLevel(logging.INFO)
_app_logger.addHandler(_handler)

@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Run the origin watch alongside the app (v2.34, #127).

    Safe as an in-process task only because the deploy is a single container —
    see the note at Dockerfile:16. Two replicas would poll twice; they would not
    double-post (``already_announced`` is checked against the board, not against
    process memory), but that check is a narrowing, not a lock. Add a leader
    election here before scaling, at the same time as the migration lock.

    The task is not started under the test suite: httpx's ASGITransport does not
    run lifespan events, and ``GITHUB_POLL_SECONDS`` is pinned to 0 regardless.
    """
    task: asyncio.Task | None = None
    if settings.github_poll_seconds > 0:
        task = asyncio.create_task(origin.run(settings.github_poll_seconds))
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="quarterback", version="2.34.0", lifespan=lifespan)
app.include_router(whoami_router)
app.include_router(posts_router)
app.include_router(stream_router)
app.include_router(blobs_router)
app.include_router(leases_router)
app.include_router(subagents_router)
app.include_router(reviews_router)
app.include_router(worktrees_router)
app.include_router(sync_router)
app.include_router(claims_router)
app.include_router(board_view_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
