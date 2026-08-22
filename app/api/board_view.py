from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.auth import reader

router = APIRouter(tags=["view"])

_STATIC = Path(__file__).parent.parent / "static"
# Explicit encoding: these pages carry em-dashes and curly quotes, and
# `read_text()` without one reads them in whatever the container's locale
# happens to be — which is how a page renders fine in dev and mojibakes in prod.
_BOARD_HTML = (_STATIC / "board.html").read_text(encoding="utf-8")
_REVIEWS_HTML = (_STATIC / "reviews.html").read_text(encoding="utf-8")
_PLAN_HTML = (_STATIC / "plan.html").read_text(encoding="utf-8")


@router.get("/", response_class=HTMLResponse)
async def board_view(_reader: str = Depends(reader)) -> HTMLResponse:
    """The human board — the live view of the stream, and where a person answers it.

    Behind Authelia in prod; the page's same-origin fetch(/board) and
    EventSource(/stream) are authorised by the same edge header (or the local
    browser_dev_user bypass).

    Read-only until issue #108, and that was the gap it is about: an ``ask``
    addressed to a person was a post nobody was watching and nothing could reply
    to. The page now asks ``/whoami`` who is looking, and shows the composer only
    when the answer is a person — reads and writes are proved differently here
    (``reader`` accepts a bare ``Remote-User``; :func:`app.auth.author` demands the
    edge secret with it), so a viewer who may look is not automatically one who
    may write. Everything it posts goes through ``POST /post`` like any agent's.
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


@router.get("/plan/view", response_class=HTMLResponse)
async def plan_view(_reader: str = Depends(reader)) -> HTMLResponse:
    """The plan (v2.39) — and the place a human reorders it.

    Under ``/plan/`` rather than beside ``/panel`` because ``/plan`` itself is
    the JSON this page fetches. It matters more here than for the panel: the
    reorder, drop and declare-a-scope controls are the *only* way into the
    human-only endpoints from a browser, since :func:`app.auth.human` refuses a
    bearer token, and the edge identity this page carries is what makes them a
    person's decision.
    """
    return HTMLResponse(_PLAN_HTML)
