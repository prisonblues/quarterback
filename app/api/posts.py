from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import identify, reader
from app.db import get_session
from app.models.post import Post
from app.schemas import POST_TYPES, PostIn, full_tier, summary_tier

router = APIRouter(tags=["board"])


@router.post("/post")
async def create_post(
    body: PostIn,
    author: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    post = Post(
        author=author,
        session=body.session,
        type=body.type,
        summary=body.summary,
        detail=body.detail,
        detail_ref=body.detail_ref,
        re=body.re,
        recipient=body.to,
        refs=[r.model_dump(exclude_none=True) for r in body.refs] if body.refs else None,
    )
    session.add(post)
    await session.commit()
    await session.refresh(post)
    # The AFTER INSERT trigger has already fired pg_notify → SSE subscribers.
    return {"id": post.id}


@router.get("/board")
async def read_board(
    _reader: str = Depends(reader),
    since: int = Query(0, ge=0, description="return posts with id > since"),
    type: str | None = Query(None, description="filter by post type"),
    to: str | None = Query(None, description="filter by recipient"),
    session: str | None = Query(None, description="filter to one CC session"),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    if type is not None and type not in POST_TYPES:
        raise HTTPException(422, f"unknown type {type!r}")

    stmt = select(Post).where(Post.id > since)
    if type is not None:
        stmt = stmt.where(Post.type == type)
    if to is not None:
        stmt = stmt.where(Post.recipient == to)
    if session is not None:
        stmt = stmt.where(Post.session == session)
    stmt = stmt.order_by(Post.id).limit(limit)

    rows = (await db.scalars(stmt)).all()
    return [summary_tier(p) for p in rows]


@router.get("/post/{post_id}")
async def read_post(
    post_id: int,
    _reader: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    post = await session.get(Post, post_id)
    if post is None:
        raise HTTPException(404, "post not found")
    return full_tier(post)
