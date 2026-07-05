from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, LargeBinary, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Blob(Base):
    """Content-addressed blob: session JSONL or an oversized post detail.

    The primary key is the caller-asserted sha256 of the content, verified on
    write, so storage is idempotent and de-duplicated. A post's ``detail_ref``
    points here.
    """

    __tablename__ = "blobs"

    sha: Mapped[str] = mapped_column(Text, primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
