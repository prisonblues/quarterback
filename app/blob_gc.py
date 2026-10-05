"""Collect unreachable transcript/detail blobs with a 24-hour reuse grace."""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session, engine
from app.models.blob import Blob
from app.models.post import Post
from app.models.session import SessionRecord

logger = logging.getLogger(__name__)
GRACE = timedelta(hours=24)
INTERVAL = 3600
BATCH_SIZE = 100
# Database-local transaction lock; shared by uploads/reference writers, exclusive
# for GC. Acquire BEFORE checking a blob and hold through the pointer's commit.
GC_LOCK = 0x5142424C4F42


async def lock_blob_writes(session: AsyncSession) -> None:
    await session.execute(select(func.pg_advisory_xact_lock_shared(GC_LOCK)))


async def touch_blob(session: AsyncSession, sha: str) -> None:
    await session.execute(
        update(Blob).where(Blob.sha == sha).values(last_used_at=func.clock_timestamp())
    )


async def collect_blobs(*, dry_run: bool = False) -> dict[str, int]:
    """Delete in small committed batches; never load blob content into Python.

    The lock and candidate query are separate statements so READ COMMITTED sees
    reference writers that committed while we waited for the exclusive lock.
    Multiple workers/collectors are safe; writers proceed between batches.
    """
    counts = {"blobs": 0, "bytes": 0}
    while True:
        async with async_session() as session, session.begin():
            await session.execute(select(func.pg_advisory_xact_lock(GC_LOCK)))
            eligible = (
                select(Blob.sha, Blob.size)
                .where(
                    Blob.last_used_at < func.clock_timestamp() - GRACE,
                    ~select(SessionRecord.session).where(
                        SessionRecord.latest_blob == Blob.sha
                    ).exists(),
                    ~select(Post.id).where(func.lower(Post.detail_ref) == Blob.sha).exists(),
                )
            )
            if dry_run:
                rows = (await session.execute(eligible)).all()
            else:
                rows = (await session.execute(eligible.order_by(Blob.sha).limit(BATCH_SIZE))).all()
                if rows:
                    await session.execute(delete(Blob).where(Blob.sha.in_([r.sha for r in rows])))
            counts["blobs"] += len(rows)
            counts["bytes"] += sum(r.size for r in rows)
        if dry_run or len(rows) < BATCH_SIZE:
            return counts
        await asyncio.sleep(0)  # let shutdown and waiting writers run


async def run_blob_gc() -> None:
    """One pass at startup, then hourly; a failed pass retries next hour."""
    while True:
        try:
            counts = await collect_blobs()
            logger.info("blob GC deleted %s blobs (%s logical bytes)", counts["blobs"], counts["bytes"])
        except Exception:
            logger.exception("blob GC failed; retrying in one hour")
        await asyncio.sleep(INTERVAL)


async def _main(dry_run: bool) -> None:
    try:
        counts = await collect_blobs(dry_run=dry_run)
        print(f"{'eligible' if dry_run else 'deleted'}: {counts['blobs']} blobs, {counts['bytes']} logical bytes")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report eligible blobs without deleting")
    asyncio.run(_main(parser.parse_args().dry_run))
