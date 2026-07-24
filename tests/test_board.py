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

from .conftest import LAPTOP, ZEUS


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
    got = await client.get(f"/post/{pid}", headers=ZEUS)
    assert got.json()["from"] == "laptop"


async def test_unknown_type_rejected(client):
    r = await client.post("/post", json={"type": "bogus", "summary": "x"}, headers=LAPTOP)
    assert r.status_code == 422


async def test_board_since_and_type_filter(client):
    base = await client.get("/board", headers=LAPTOP)
    start = base.json()[-1]["id"] if base.json() else 0

    await client.post("/post", json={"type": "note", "summary": "n1"}, headers=LAPTOP)
    fid = (
        await client.post("/post", json={"type": "finding", "summary": "f1"}, headers=ZEUS)
    ).json()["id"]

    new = (await client.get("/board", params={"since": start}, headers=LAPTOP)).json()
    assert [p["id"] for p in new] == sorted(p["id"] for p in new)  # ordered
    findings = (
        await client.get("/board", params={"since": start, "type": "finding"}, headers=LAPTOP)
    ).json()
    assert [p["id"] for p in findings] == [fid]
    assert findings[0]["from"] == "zeus"


async def test_board_omits_presence_by_default(client):
    base = await client.get("/board", headers=LAPTOP)
    start = base.json()[-1]["id"] if base.json() else 0

    note = (
        await client.post("/post", json={"type": "note", "summary": "keep me"}, headers=LAPTOP)
    ).json()["id"]
    pres = (
        await client.post("/post", json={"type": "presence", "summary": "started"}, headers=ZEUS)
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
    # Scope to a private recipient so posts accumulated by other tests don't interfere.
    to = "wtest"
    old = (
        await client.post("/post", json={"summary": "old", "to": to}, headers=LAPTOP)
    ).json()["id"]
    await _backdate(old, 120)  # 2h ago — outside the 30-min window

    # Quiet window (nothing fresh): the floor still surfaces the last decision.
    quiet = (await client.get("/board", params={"to": to, "window_min": 30}, headers=LAPTOP)).json()
    assert old in [p["id"] for p in quiet]

    # Ten fresh posts fill the window past the floor → the stale post drops off.
    fresh = [
        (
            await client.post("/post", json={"summary": f"f{i}", "to": to}, headers=LAPTOP)
        ).json()["id"]
        for i in range(10)
    ]
    live = (await client.get("/board", params={"to": to, "window_min": 30}, headers=LAPTOP)).json()
    ids = [p["id"] for p in live]
    assert old not in ids
    assert set(fresh) <= set(ids)


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
                await client.post("/post", json={"summary": "live"}, headers=ZEUS)
            ).json()["id"]

        post_task = asyncio.create_task(make_live())
        nxt = await asyncio.wait_for(anext(gen), timeout=5)
        await post_task

        body = json.loads(nxt["data"])
        assert body["id"] == live_id["id"]
        assert body["from"] == "zeus"
    finally:
        await gen.aclose()
