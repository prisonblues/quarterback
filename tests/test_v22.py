"""v2.2 — session registry (/sessions) + mid-session snapshot (/snapshot) + cwd."""

from __future__ import annotations

import hashlib
import uuid

import pytest

from .conftest import DESKTOP, LAPTOP, SERVER


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _find(sessions: list[dict], key: str) -> dict | None:
    return next((s for s in sessions if s["session"] == key), None)


@pytest.mark.asyncio
async def test_sessions_lists_live_then_resumable_with_size_and_cwd(client):
    sess = f"s-{uuid.uuid4()}"
    cwd = "/home/devuser/source/selfhost"

    # live: lease claimed with cwd + title + recap, no blob yet
    await client.post("/lease", json={
        "session": sess, "device": "lap", "cwd": cwd,
        "title": "Wire the board", "recap": "building v2.3 session registry",
    }, headers=LAPTOP)
    row = _find((await client.get("/sessions", headers=SERVER)).json(), sess)
    assert row is not None
    assert row["live"] is True and row["resumable"] is False
    assert row["cwd"] == cwd and row["size"] is None
    assert row["title"] == "Wire the board" and row["recap"] == "building v2.3 session registry"

    # snapshot: push a blob without releasing → size appears, still live
    jsonl = b'{"turn":1}\n{"turn":2}\n'
    sha = _sha(jsonl)
    await client.put(f"/blob/{sha}", content=jsonl, headers=LAPTOP)
    snap = await client.post("/snapshot", json={"session": sess, "blob": sha}, headers=LAPTOP)
    assert snap.status_code == 200
    row = _find((await client.get("/sessions", headers=SERVER)).json(), sess)
    assert row["live"] is True and row["resumable"] is True
    assert row["size"] == len(jsonl) and row["blob"] == sha

    # handoff: releases the lease → resumable, no longer live
    await client.post("/handoff", json={"session": sess, "blob": sha}, headers=LAPTOP)
    row = _find((await client.get("/sessions", headers=SERVER)).json(), sess)
    assert row["live"] is False and row["resumable"] is True
    assert row["cwd"] == cwd

    # the single-session endpoint (what `qb resume` reads) must expose cwd + blob
    single = (await client.get(f"/session/{sess}", headers=DESKTOP)).json()
    assert single["cwd"] == cwd and single["latest_blob"] == sha


@pytest.mark.asyncio
async def test_snapshot_keeps_the_lease(client):
    sess = f"s-{uuid.uuid4()}"
    await client.post("/lease", json={"session": sess, "device": "lap"}, headers=LAPTOP)
    jsonl = b'{"live":true}\n'
    sha = _sha(jsonl)
    await client.put(f"/blob/{sha}", content=jsonl, headers=LAPTOP)
    await client.post("/snapshot", json={"session": sess, "blob": sha}, headers=LAPTOP)

    # another device still cannot claim — snapshot did NOT release
    conflict = await client.post("/lease", json={"session": sess, "device": "zeu"}, headers=SERVER)
    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_snapshot_requires_active_lease(client):
    sess = f"s-{uuid.uuid4()}"
    jsonl = b'{}\n'
    sha = _sha(jsonl)
    await client.put(f"/blob/{sha}", content=jsonl, headers=DESKTOP)
    # no lease held → 409
    r = await client.post("/snapshot", json={"session": sess, "blob": sha}, headers=DESKTOP)
    assert r.status_code == 409
