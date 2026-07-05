from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Post(Base):
    """One append-only board entry.

    The BIGSERIAL ``id`` is the global, monotonic ordering key — the Postgres
    equivalent of cena's flock-assigned ``seq`` — used by ``?since=`` replay.
    ``author`` is the API's ``from``; ``recipient`` is the API's ``to`` (both
    are reserved words, hence the rename). Posts are immutable once written.
    """

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    author: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # Layered detail: inline blob (v1) or a content-addressed ref fetched on demand (v2 /blob).
    detail: Mapped[str | None] = mapped_column(Text)
    detail_ref: Mapped[str | None] = mapped_column(Text)
    # Thread parent (the id this post replies to) — cena's `--re`.
    re: Mapped[int | None] = mapped_column(BigInteger)
    recipient: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_posts_type", "type"),
        Index("ix_posts_recipient", "recipient"),
    )
