from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.auth import reader

router = APIRouter(tags=["view"])

_STATIC = Path(__file__).parent.parent / "static"
_BOARD_HTML = (_STATIC / "board.html").read_text()
_REVIEWS_HTML = (_STATIC / "reviews.html").read_text()


@router.get("/", response_class=HTMLResponse)
async def board_view(_reader: str = Depends(reader)) -> HTMLResponse:
    """The human board — a read-only live view of the stream.

    Behind Authelia in prod; the page's same-origin fetch(/board) and
    EventSource(/stream) are authorised by the same edge header (or the local
    browser_dev_user bypass).
    """
    return HTMLResponse(_BOARD_HTML)


@router.get("/panel", response_class=HTMLResponse)
async def panel_view(_reader: str = Depends(reader)) -> HTMLResponse:
    """Reviewer-panel stats (v2.10) — which models find the real issues.

    Served at ``/panel`` rather than ``/reviews`` because that path is the JSON
    the page fetches; one path can't be both without content negotiation nobody
    would remember was there.
    """
    return HTMLResponse(_REVIEWS_HTML)
