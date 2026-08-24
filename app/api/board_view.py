from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, Response

from app.auth import reader

router = APIRouter(tags=["view"])

_STATIC = Path(__file__).parent.parent / "static"
# Explicit encoding: these pages carry em-dashes and curly quotes, and
# `read_text()` without one reads them in whatever the container's locale
# happens to be — which is how a page renders fine in dev and mojibakes in prod.
_BOARD_HTML = (_STATIC / "board.html").read_text(encoding="utf-8")
_REVIEWS_HTML = (_STATIC / "reviews.html").read_text(encoding="utf-8")
_PLAN_HTML = (_STATIC / "plan.html").read_text(encoding="utf-8")
_FLEET_HTML = (_STATIC / "fleet.html").read_text(encoding="utf-8")
_PRS_HTML = (_STATIC / "prs.html").read_text(encoding="utf-8")
# Read here for the same reason the pages are: an asset the build failed to ship
# becomes a startup crash instead of a silent 404 on a page that would then just
# quietly have no drag. See app/static/vendor/README.md for the pin.
_SORTABLE_JS = (_STATIC / "vendor" / "sortable.min.js").read_text(encoding="utf-8")


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


@router.get("/vendor/sortable.min.js")
async def sortable_js(_reader: str = Depends(reader)) -> Response:
    """SortableJS, vendored — the drag half of reordering the plan (#388).

    **This is the first served asset in the app**, and it is a route rather than a
    ``StaticFiles`` mount on purpose. There was no static serving here at all: the
    four pages above are ``read_text()`` at import and returned as strings, and a
    mount would have introduced a second way in, with its own auth boundary to
    reason about, for one file. This is one more handler in the shape of the ones
    beside it, so ``Depends(reader)`` means here exactly what it means there — and
    the browser sends the same edge identity for a ``<script src>`` as for the page
    that asked for it.

    Reading it at import is the load-bearing part. A vendored file the build failed
    to ship would otherwise 404 at runtime and leave ``/plan/view`` looking healthy
    with no drag on it — #169's pattern, and the reason the page's other assets are
    read the same way. Here it is a startup crash instead.

    ``/vendor/`` rather than ``/static/`` because the directory is what it is: code
    fetched from somewhere else and checked in byte for byte. The version, source
    URL and checksum are in ``app/static/vendor/README.md`` and pinned by
    ``tests/test_plan_page.py``, so "which Sortable is this" has an answer.
    """
    # An hour: long enough that browsing the board does not re-fetch 45KB over a
    # phone connection, short enough that a deploy is in force the same session.
    # Not `immutable` — that wants a version in the path, and a version in the path
    # is a second place the page and the route have to agree about.
    #
    # `private`, not `public`, although the bytes are a public MIT library: this
    # response came back from behind `reader`, and a shared cache that kept it would
    # be answering an unauthenticated request with something the edge had authorised.
    # Nothing is leaked by this particular file, but the header is a rule about the
    # response's provenance rather than about its contents, and the only cache that
    # matters here is the phone's own.
    return Response(_SORTABLE_JS, media_type="application/javascript",
                    headers={"Cache-Control": "private, max-age=3600"})


@router.get("/fleet", response_class=HTMLResponse)
async def fleet_view(_reader: str = Depends(reader)) -> HTMLResponse:
    """What every agent is doing, from a phone (#378) — and how much of it is known.

    At ``/fleet`` rather than under one of the endpoints it reads, because it
    reads three: ``GET /active`` for who holds a lease, ``GET /sessions`` for who
    ever pushed a transcript and how their last lease ended, and ``GET /claims``
    for what is still spoken for. No one of those paths is the page's, so none of
    them can be its parent the way ``/plan`` is ``/plan/view``'s.

    **It never concludes "gone" from an absent lease.** ``/active`` lists only
    leases inside their TTL, a lease is renewed once per *prompt*, and one prompt
    can be an hour of autonomous work — so a busy agent leaves ``/active``
    precisely while it is busiest (#252). Where the board cannot separate a long
    turn from a stall, the row says which two readings it cannot separate, in the
    wording ``qb-reconcile`` already uses for the same ambiguity. Where somebody
    *reported* an ending (#277) it says so, and that row is a different one.

    One write reaches it, and it is ``POST /session/end``: the verb a person
    actually needs from a phone when something has gone wrong. There is
    deliberately no spawn button — ``qb-start`` is off by default per machine
    (#360), and a phone is the worst place to reason about whether a box opted
    in.
    """
    return HTMLResponse(_FLEET_HTML)


@router.get("/prs", response_class=HTMLResponse)
async def prs_view(_reader: str = Depends(reader)) -> HTMLResponse:
    """Where each pull request has got to: its round, what it waits on, its place in the line (#395).

    At ``/prs`` because it reads three JSON paths and owns none of them: ``GET
    /reviews`` for the newest panel round on each PR, ``GET /review/needs-human``
    for the defects a person still owes an answer about, and ``GET /merge-queue``
    for the line to land. All three have been served, tested and documented for
    releases and **no page or pane had ever called one of them** — which is the
    whole of #395: the board was holding the answers to "what round is it on" and
    "is it blocked by anything" and no screen asked.

    Not on ``/panel``. #57 argues that page is a research instrument answering
    "which reviewer earns its cost", and that "what is running and what is it
    waiting on" is a different question that should not dilute it. This is that
    other page.

    **Every number on it is a memory, and it says so.** ``pr_state`` and
    ``ci_status`` are what the panel saw *at run time* — :class:`app.models.review.ReviewRun`
    is explicit that a PR merged after its final round still reads ``OPEN`` — so
    each is drawn with the round and the age it came from rather than as a live
    reading. A merge-queue ``verdict`` is testimony pinned to a ``ready_sha``, and
    a head that has moved since retires it; the row says which commit was judged.

    **An empty line is not a drained one.** Nothing enqueues automatically
    (#258), so a queue with no entries cannot be distinguished from a queue
    nobody feeds — and the page names that ambiguity in place of a clean zero,
    the way ``/fleet`` names the two readings of an absent lease rather than
    picking one.

    **``round`` is not a stage.** #262's ``stage`` is where a *session* is in its
    own loop; ``ReviewRun.round`` is how many panel rounds this *pull request*
    has been through. Different objects, different questions, and this page draws
    only the second one — a blank cell that could be read as either is worse than
    two honest columns.
    """
    return HTMLResponse(_PRS_HTML)
