from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import identify
from app.db import get_session
from app.models.blob import Blob

router = APIRouter(tags=["blob"])

MAX_BLOB_BYTES = 64 * 1024 * 1024  # 64 MB guard for session JSONL / detail blobs


@router.put("/blob/{sha}")
async def put_blob(
    sha: str,
    request: Request,
    _author: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Store a content-addressed blob. Idempotent: re-PUTting the same sha is a no-op.

    The body's sha256 must equal the path ``sha`` — the server verifies it so a
    corrupted upload can't masquerade under a good hash.
    """
    body = await request.body()
    if len(body) > MAX_BLOB_BYTES:
        raise HTTPException(413, f"blob exceeds {MAX_BLOB_BYTES} bytes")

    actual = hashlib.sha256(body).hexdigest()
    if actual != sha.lower():
        raise HTTPException(400, f"sha mismatch: body hashes to {actual}")

    stmt = (
        pg_insert(Blob)
        .values(sha=actual, content=body, size=len(body))
        .on_conflict_do_nothing(index_elements=[Blob.sha])
    )
    result = await session.execute(stmt)
    await session.commit()
    return {"sha": actual, "size": len(body), "created": result.rowcount > 0}


@router.get("/blob/{sha}")
async def get_blob(
    sha: str,
    _author: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> Response:
    blob = await session.get(Blob, sha.lower())
    if blob is None:
        raise HTTPException(404, "blob not found")
    return Response(content=blob.content, media_type="application/octet-stream")
