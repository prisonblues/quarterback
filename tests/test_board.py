"""End-to-end board tests against a real Postgres (docker compose up -d postgres).

The NOTIFY trigger and SSE stream exercise real database machinery. Shared
fixtures (schema setup, client) live in conftest.py.
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from app.api.stream import event_stream
from app.db import engine

from .conftest import LAPTOP, SERVER


async def test_health_needs_no_auth(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_post_rejects_missing_and_bad_token(client):
    assert (await client.post("/post", json={"summary": "hi"})).status_code == 401
    r = await client.post("/post", json={"summary": "hi"}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_author_comes_from_token(client):
    r = await client.post("/post", json={"summary": "from laptop"}, headers=LAPTOP)
    assert r.status_code == 200
    pid = r.json()["id"]
    got = await client.get(f"/post/{pid}", headers=SERVER)
    assert got.json()["from"] == "laptop"


async def test_unknown_type_rejected(client):
    r = await client.post("/post", json={"type": "bogus", "summary": "x"}, headers=LAPTOP)
    assert r.status_code == 422


async def test_board_since_and_type_filter(client):
    base = await client.get("/board", headers=LAPTOP)
    start = base.json()[-1]["id"] if base.json() else 0

    await client.post("/post", json={"type": "note", "summary": "n1"}, headers=LAPTOP)
    fid = (
        await client.post("/post", json={"type": "finding", "summary": "f1"}, headers=SERVER)
    ).json()["id"]

    new = (await client.get("/board", params={"since": start}, headers=LAPTOP)).json()
    assert [p["id"] for p in new] == sorted(p["id"] for p in new)  # ordered
    findings = (
        await client.get("/board", params={"since": start, "type": "finding"}, headers=LAPTOP)
    ).json()
    assert [p["id"] for p in findings] == [fid]
    assert findings[0]["from"] == "server"


async def test_board_answers_a_bare_list_not_a_wrapper_object(client):
    """The shape `qb-doctor` reads, asserted where it is actually served.

    #531: the `escalations` row parsed this endpoint as `{"posts": [...], "cursor": N}` —
    the object the MCP `board_read` wrapper assembles on the way out — and so could not
    read a single post on any host. Its own stub answered an object too, so five unit
    tests passed while the row was dead.

    A unit test over the harness can only pin the annotation, and an annotation is not
    the contract: `-> list[dict]` would keep passing while the body started returning an
    object. This asserts what a client receives, and it belongs here because this is the
    file with a real app and a real database behind it.
    """
    await client.post("/post", json={"type": "note", "summary": "shape"}, headers=LAPTOP)

    body = (await client.get("/board", headers=LAPTOP)).json()

    assert isinstance(body, list), type(body)
    assert body and all(isinstance(p, dict) and "ts" in p for p in body)
    # The `type=` slice too — that is the call the escalations row makes, and the one
    # whose elements it dates itself.
    sliced = (await client.get("/board", params={"type": "note"}, headers=LAPTOP)).json()
    assert isinstance(sliced, list), type(sliced)


async def test_board_omits_presence_by_default(client):
    base = await client.get("/board", headers=LAPTOP)
    start = base.json()[-1]["id"] if base.json() else 0

    note = (
        await client.post("/post", json={"type": "note", "summary": "keep me"}, headers=LAPTOP)
    ).json()["id"]
    pres = (
        await client.post("/post", json={"type": "presence", "summary": "started"}, headers=SERVER)
    ).json()["id"]

    # Default read drops the presence heartbeat but keeps the decision-bearing note.
    default = (await client.get("/board", params={"since": start}, headers=LAPTOP)).json()
    ids = [p["id"] for p in default]
    assert note in ids
    assert pres not in ids

    # Detail is never lost: type=presence surfaces just heartbeats...
    only_presence = (
        await client.get("/board", params={"since": start, "type": "presence"}, headers=LAPTOP)
    ).json()
    assert [p["id"] for p in only_presence] == [pres]

    # ...and include_presence=true returns everything.
    everything = (
        await client.get(
            "/board", params={"since": start, "include_presence": "true"}, headers=LAPTOP
        )
    ).json()
    assert {note, pres} <= {p["id"] for p in everything}


async def test_board_hides_detail_but_flags_it(client):
    r = await client.post("/post", json={"summary": "s", "detail": "the big body"}, headers=LAPTOP)
    pid = r.json()["id"]
    row = next(p for p in (await client.get("/board", headers=LAPTOP)).json() if p["id"] == pid)
    assert "detail" not in row
    assert row["has_detail"] is True
    assert (await client.get(f"/post/{pid}", headers=LAPTOP)).json()["detail"] == "the big body"


async def _backdate(post_id: int, minutes: int):
    """Age a post so it falls outside the orient window (tests can't wait wall-clock)."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE posts SET ts = now() - make_interval(mins => :m) WHERE id = :id"),
            {"m": minutes, "id": post_id},
        )


async def test_board_orient_window_excludes_stale_but_floors_when_quiet(client):
    # An orientation read (not narrowed to a recipient) still floors when quiet.
    # Scope by type — 'stuck' is used by no other test — so posts accumulated
    # elsewhere in the suite don't interfere. A `to=` scope can't be used here:
    # that's a mailbox read, which deliberately has no floor (issue #17).
    kind = "stuck"
    old = (
        await client.post("/post", json={"type": kind, "summary": "old"}, headers=LAPTOP)
    ).json()["id"]
    await _backdate(old, 120)  # 2h ago — outside the 30-min window

    # Quiet window (nothing fresh): the floor still surfaces the last decision.
    quiet = (
        await client.get("/board", params={"type": kind, "window_min": 30}, headers=LAPTOP)
    ).json()
    assert old in [p["id"] for p in quiet]

    # Ten fresh posts fill the window past the floor → the stale post drops off.
    fresh = [
        (
            await client.post("/post", json={"type": kind, "summary": f"f{i}"}, headers=LAPTOP)
        ).json()["id"]
        for i in range(10)
    ]
    live = (
        await client.get("/board", params={"type": kind, "window_min": 30}, headers=LAPTOP)
    ).json()
    ids = [p["id"] for p in live]
    assert old not in ids
    assert set(fresh) <= set(ids)


async def test_board_inbox_read_has_no_floor_so_stale_mail_stays_hidden(client):
    # Issue #17: the floor made every ?to= poll return "the last 10 asks ever
    # sent to this machine", so each new session rediscovered days-dead mail.
    to = "wtest-inbox"
    stale = (
        await client.post(
            "/post", json={"type": "ask", "summary": "old ask", "to": to}, headers=LAPTOP
        )
    ).json()["id"]
    await _backdate(stale, 6 * 24 * 60)  # 6 days ago

    empty = (
        await client.get(
            "/board", params={"to": to, "type": "ask", "window_min": 30}, headers=LAPTOP
        )
    ).json()
    assert empty == []  # an empty inbox is the correct answer, not a reason to backfill


async def test_board_inbox_still_returns_mail_inside_the_window(client):
    # The floor going away must not cost an inbox its actual live mail.
    to = "wtest-inbox2"
    fresh = (
        await client.post(
            "/post", json={"type": "ask", "summary": "live ask", "to": to}, headers=SERVER
        )
    ).json()["id"]
    stale = (
        await client.post(
            "/post", json={"type": "ask", "summary": "old ask", "to": to}, headers=SERVER
        )
    ).json()["id"]
    await _backdate(stale, 90)  # 1.5h ago — outside the window

    got = (
        await client.get(
            "/board", params={"to": to, "type": "ask", "window_min": 30}, headers=LAPTOP
        )
    ).json()
    assert [p["id"] for p in got] == [fresh]


async def test_board_inbox_window_zero_returns_full_history(client):
    # window_min=0 disables the window — the documented knob for looking further
    # back now that the floor no longer does it by accident.
    to = "wtest-inbox3"
    stale = (
        await client.post(
            "/post", json={"type": "ask", "summary": "old ask", "to": to}, headers=LAPTOP
        )
    ).json()["id"]
    await _backdate(stale, 6 * 24 * 60)

    got = (
        await client.get(
            "/board", params={"to": to, "type": "ask", "window_min": 0}, headers=LAPTOP
        )
    ).json()
    assert [p["id"] for p in got] == [stale]


async def test_board_session_filter_has_no_floor_either(client):
    # ?session= is a lookup for one session's posts, not an orientation read.
    sess = "s-wtest-floor"
    stale = (
        await client.post(
            "/post", json={"summary": "old session post", "session": sess}, headers=LAPTOP
        )
    ).json()["id"]
    await _backdate(stale, 240)  # 4h ago

    windowed = (
        await client.get("/board", params={"session": sess, "window_min": 30}, headers=LAPTOP)
    ).json()
    assert windowed == []

    unwindowed = (
        await client.get("/board", params={"session": sess, "window_min": 0}, headers=LAPTOP)
    ).json()
    assert [p["id"] for p in unwindowed] == [stale]


async def test_board_cursor_read_ignores_window(client):
    # A catch-up read (since=cursor) returns backdated posts the window would hide.
    to = "wtest2"
    cursor = (
        await client.post("/post", json={"summary": "c0", "to": to}, headers=LAPTOP)
    ).json()["id"]
    gap = (
        await client.post("/post", json={"summary": "gap", "to": to}, headers=LAPTOP)
    ).json()["id"]
    await _backdate(gap, 240)  # 4h ago — the window would exclude it

    caught_up = (
        await client.get("/board", params={"to": to, "since": cursor}, headers=LAPTOP)
    ).json()
    assert gap in [p["id"] for p in caught_up]


async def test_suite_runs_without_the_browser_auth_bypass():
    # Asserted directly because the symptom is otherwise a hang, not a failure:
    # with a dev user configured, `reader` authenticates everyone, so the next
    # test gets a live SSE stream instead of a 401 and blocks forever on a
    # transport that buffers whole responses. conftest pins this off; a checkout
    # .env setting BROWSER_DEV_USER must not reach the suite.
    from app.config import settings

    assert not settings.browser_dev_user


async def test_stream_requires_auth(client):
    # A 401 is a complete (non-streaming) response, so it's safe over ASGITransport.
    r = await client.get("/stream")
    assert r.status_code == 401


async def test_stream_replays_backlog_then_goes_live(client):
    # Drive the stream generator in-process: httpx ASGITransport buffers the whole
    # response body, so it can't consume a live (never-ending) SSE stream. Iterating
    # the generator directly still exercises the real DB, NOTIFY trigger, and dedup.
    seed = (await client.post("/post", json={"summary": "seed"}, headers=LAPTOP)).json()["id"]

    gen = event_stream(since=seed - 1)
    try:
        # Backlog (the seed) arrives with no new post.
        first = await asyncio.wait_for(anext(gen), timeout=5)
        assert json.loads(first["data"])["id"] == seed

        # A post made while we await the next event must push live via NOTIFY.
        live_id: dict = {}

        async def make_live():
            live_id["id"] = (
                await client.post("/post", json={"summary": "live"}, headers=SERVER)
            ).json()["id"]

        post_task = asyncio.create_task(make_live())
        nxt = await asyncio.wait_for(anext(gen), timeout=5)
        await post_task

        body = json.loads(nxt["data"])
        assert body["id"] == live_id["id"]
        assert body["from"] == "server"
    finally:
        await gen.aclose()
