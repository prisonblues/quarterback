from __future__ import annotations

import logging
import sys

from fastapi import FastAPI

from app.api.blobs import router as blobs_router
from app.api.board_view import router as board_view_router
from app.api.claims import router as claims_router
from app.api.leases import router as leases_router
from app.api.plan import router as plan_router
from app.api.posts import router as posts_router
from app.api.reviews import router as reviews_router
from app.api.stream import router as stream_router
from app.api.subagents import router as subagents_router
from app.api.sync import router as sync_router
from app.api.whoami import router as whoami_router
from app.api.worktrees import router as worktrees_router

# Explicit "app" logger handler — uvicorn's dictConfig at startup drops root
# handlers, so a dedicated namespace handler survives.
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
_app_logger = logging.getLogger("app")
_app_logger.setLevel(logging.INFO)
_app_logger.addHandler(_handler)

app = FastAPI(title="quarterback", version="2.39.0")
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
app.include_router(plan_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
