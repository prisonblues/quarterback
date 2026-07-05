from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.auth import reader

router = APIRouter(tags=["view"])

_BOARD_HTML = (Path(__file__).parent.parent / "static" / "board.html").read_text()


@router.get("/", response_class=HTMLResponse)
async def board_view(_reader: str = Depends(reader)) -> HTMLResponse:
    """The human board — a read-only live view of the stream.

    Behind Authelia in prod; the page's same-origin fetch(/board) and
    EventSource(/stream) are authorised by the same edge header (or the local
    browser_dev_user bypass).
    """
    return HTMLResponse(_BOARD_HTML)
