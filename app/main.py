from __future__ import annotations

import logging
import sys

from fastapi import FastAPI

from app.api.posts import router as posts_router
from app.api.stream import router as stream_router

# Explicit "app" logger handler — uvicorn's dictConfig at startup drops root
# handlers, so a dedicated namespace handler survives (house quirk, cf. callous).
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
_app_logger = logging.getLogger("app")
_app_logger.setLevel(logging.INFO)
_app_logger.addHandler(_handler)

app = FastAPI(title="quarterback", version="0.1.0")
app.include_router(posts_router)
app.include_router(stream_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
