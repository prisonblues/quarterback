"""End-to-end board tests against a real Postgres (docker compose up -d postgres).

Runs migrations, then drives the ASGI app over an in-process httpx transport so
the NOTIFY trigger and SSE stream exercise real database machinery.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import httpx
import pytest
from httpx import ASGITransport

# Point the app at the compose-mapped Postgres before importing it.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://quarterback:quarterback@localhost:5435/quarterback",
)
os.environ.setdefault("API_TOKENS", "laptop:tok-laptop,server:tok-server")

from app.api.stream import event_stream
from app.db import engine
from app.main import app

LAPTOP = {"Authorization": "Bearer tok-laptop"}
SERVER = {"Authorization": "Bearer tok-server"}


@pytest.fixture(scope="module", autouse=True)
async def _schema():
    # Rebuild schema (+ trigger) via alembic so tests exercise the real migration.
    alembic = [sys.executable, "-m", "alembic"]
    subprocess.run([*alembic, "downgrade", "base"], check=False)
    subprocess.run([*alembic, "upgrade", "head"], check=True)
    yield
    await engine.dispose()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


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


async def test_board_hides_detail_but_flags_it(client):
    r = await client.post("/post", json={"summary": "s", "detail": "the big body"}, headers=LAPTOP)
    pid = r.json()["id"]
    row = next(p for p in (await client.get("/board", headers=LAPTOP)).json() if p["id"] == pid)
    assert "detail" not in row
    assert row["has_detail"] is True
    assert (await client.get(f"/post/{pid}", headers=LAPTOP)).json()["detail"] == "the big body"


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
