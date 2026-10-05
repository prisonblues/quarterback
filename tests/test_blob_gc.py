"""#819: only unreachable blobs outside the grace period are collected."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update

from app import blob_gc
from app.db import async_session
from app.models.blob import Blob
from app.models.session import SessionRecord

from .conftest import LAPTOP


async def upload(client, content):
    sha = hashlib.sha256(content).hexdigest()
    response = await client.put(f"/blob/{sha}", content=content, headers=LAPTOP)
    assert response.status_code == 200
    return sha


async def age(sha, hours=25):
    async with async_session() as db:
        await db.execute(update(Blob).where(Blob.sha == sha).values(
            last_used_at=datetime.now(UTC) - timedelta(hours=hours)))
        await db.commit()


async def exists(sha):
    async with async_session() as db:
        return await db.scalar(select(Blob.sha).where(Blob.sha == sha)) is not None


async def test_gc_deletes_old_orphans_but_keeps_references_and_recent_uploads(client):
    orphan = await upload(client, b"gc-orphan")
    referenced = await upload(client, b"gc-referenced")
    detail = await upload(client, b"gc-detail")
    recent = await upload(client, b"gc-recent")
    await client.post("/lease", json={"session": "gc-live", "device": "lap"}, headers=LAPTOP)
    assert (await client.post("/handoff", json={"session": "gc-live", "blob": referenced},
                              headers=LAPTOP)).status_code == 200
    assert (await client.post("/post", json={"type": "note", "summary": "gc detail",
                                            "detail_ref": detail.upper()}, headers=LAPTOP)).status_code == 200
    for sha in (orphan, referenced, detail):
        await age(sha)
    preview = await blob_gc.collect_blobs(dry_run=True)
    assert preview["blobs"] >= 1
    assert await exists(orphan)
    await blob_gc.collect_blobs()
    assert not await exists(orphan)
    for sha in (referenced, detail, recent):
        assert await exists(sha)
    assert (await blob_gc.collect_blobs())["blobs"] == 0


async def test_reput_old_blob_renews_grace_without_changing_creation_or_content(client):
    content = b"gc-reput"
    sha = await upload(client, content)
    await age(sha)
    async with async_session() as db:
        created = (await db.get(Blob, sha)).created_at
    response = await client.put(f"/blob/{sha}", content=content, headers=LAPTOP)
    assert response.json()["created"] is False
    async with async_session() as db:
        blob = await db.get(Blob, sha)
        assert blob.last_used_at > datetime.now(UTC) - timedelta(minutes=1)
        assert blob.created_at == created
        assert blob.content == content
    await blob_gc.collect_blobs()
    assert await exists(sha)


@pytest.mark.parametrize("endpoint", ["snapshot", "handoff"])
async def test_supersession_starts_grace_for_peer_with_old_pointer(client, endpoint):
    key = "gc-supersede-" + endpoint
    old = await upload(client, key.encode())
    new = await upload(client, (key + "-new").encode())
    await client.post("/lease", json={"session": key, "device": "lap"}, headers=LAPTOP)
    assert (await client.post("/snapshot", json={"session": key, "blob": old}, headers=LAPTOP)).status_code == 200
    await age(old)
    assert (await client.post("/" + endpoint, json={"session": key, "blob": new}, headers=LAPTOP)).status_code == 200
    await blob_gc.collect_blobs()
    assert await exists(old)  # peer read the old pointer just before supersession
    await age(old)
    await blob_gc.collect_blobs()
    assert not await exists(old)
    assert await exists(new)


async def test_gc_batches_and_reports_logical_bytes(client, monkeypatch):
    await blob_gc.collect_blobs()
    contents = [b"batch-a", b"batch-b", b"batch-c"]
    shas = [await upload(client, content) for content in contents]
    for sha in shas:
        await age(sha)
    monkeypatch.setattr(blob_gc, "BATCH_SIZE", 2)
    assert await blob_gc.collect_blobs() == {"blobs": 3, "bytes": sum(map(len, contents))}
    assert all([not await exists(sha) for sha in shas])


async def test_gc_waits_for_uncommitted_reference_and_rechecks_after_commit(client):
    sha = await upload(client, b"gc-race")
    await age(sha)
    async with async_session() as writer:
        await blob_gc.lock_blob_writes(writer)
        writer.add(SessionRecord(session="gc-race", latest_blob=sha))
        await writer.flush()
        task = asyncio.create_task(blob_gc.collect_blobs())
        try:
            # Observe the actual lock wait, rather than treating a short sleep as proof.
            for _ in range(100):
                async with async_session() as observer:
                    waiting = await observer.scalar(select(func.count()).select_from(
                        text("pg_locks")
                    ).where(text(
                        "locktype = 'advisory' AND NOT granted AND objid = 1112297282"
                    )))
                if waiting:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("collector never waited for the blob writer")
            assert not task.done()
            await writer.commit()
            await asyncio.wait_for(task, 5)
        finally:
            await writer.rollback()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert await exists(sha)


async def test_periodic_gc_retries_after_failure(monkeypatch):
    calls = []

    async def collect():
        calls.append("collect")
        if calls.count("collect") == 1:
            raise RuntimeError("database unavailable")
        return {"blobs": 0, "bytes": 0}

    async def sleep(seconds):
        assert seconds == 3600
        calls.append("sleep")
        if calls.count("sleep") == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(blob_gc, "collect_blobs", collect)
    monkeypatch.setattr(blob_gc.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await blob_gc.run_blob_gc()
    assert calls == ["collect", "sleep", "collect", "sleep"]


async def test_gc_keeps_blob_just_inside_grace(client):
    sha = await upload(client, b"gc-near-boundary")
    await age(sha, hours=23.999)
    await blob_gc.collect_blobs()
    assert await exists(sha)


async def test_app_lifespan_starts_and_cancels_collector(monkeypatch):
    from app.main import app

    started = asyncio.Event()
    stopped = asyncio.Event()

    async def collector():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(blob_gc, "run_blob_gc", collector)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), 1)
    assert stopped.is_set()


@pytest.mark.parametrize("endpoint", ["snapshot", "handoff", "put", "post"])
async def test_blob_writers_wait_for_collector_transaction(client, endpoint):
    sha = await upload(client, ("gc-writer-" + endpoint).encode())
    key = "gc-writer-" + endpoint
    await client.post("/lease", json={"session": key, "device": "lap"}, headers=LAPTOP)
    async with async_session() as collector:
        await collector.execute(select(func.pg_advisory_xact_lock(blob_gc.GC_LOCK)))
        if endpoint == "put":
            request = client.put(f"/blob/{sha}", content=key.encode(), headers=LAPTOP)
        elif endpoint == "post":
            request = client.post("/post", json={"type": "note", "summary": key,
                                                "detail_ref": sha}, headers=LAPTOP)
        else:
            request = client.post("/" + endpoint, json={"session": key, "blob": sha}, headers=LAPTOP)
        task = asyncio.create_task(request)
        try:
            for _ in range(100):
                async with async_session() as observer:
                    waiting = await observer.scalar(text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                        "AND NOT granted AND objid = :key"
                    ), {"key": blob_gc.GC_LOCK & 0xffffffff})
                if waiting:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail(f"{endpoint} never waited for collector")
            assert not task.done()
            await collector.commit()
            assert (await asyncio.wait_for(task, 5)).status_code == 200
        finally:
            await collector.rollback()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_concurrent_sessions_can_swap_blob_pointers(client):
    a = await upload(client, b"gc-swap-a")
    b = await upload(client, b"gc-swap-b")
    for key, sha in (("gc-swap-a", a), ("gc-swap-b", b)):
        await client.post("/lease", json={"session": key, "device": "lap"}, headers=LAPTOP)
        assert (await client.post("/snapshot", json={"session": key, "blob": sha},
                                  headers=LAPTOP)).status_code == 200
    results = await asyncio.wait_for(asyncio.gather(
        client.post("/snapshot", json={"session": "gc-swap-a", "blob": b}, headers=LAPTOP),
        client.post("/snapshot", json={"session": "gc-swap-b", "blob": a}, headers=LAPTOP),
    ), 5)
    assert all(r.status_code == 200 for r in results)
    async with async_session() as db:
        assert (await db.get(SessionRecord, "gc-swap-a")).latest_blob == b
        assert (await db.get(SessionRecord, "gc-swap-b")).latest_blob == a
