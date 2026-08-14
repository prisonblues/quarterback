from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentName(Base):
    """The board-designated shortname for one agent on one machine (v2.12).

    The client sends a stable opaque ``key`` — a session id, a rollout id, or a
    nonce a one-shot process makes at startup; the board does not interpret it —
    and the board allocates a two-word ``name`` that is free on that machine.
    Both spell the same agent: ``zeus/amber-otter`` is whoever holds that name
    now, ``zeus/ed49425c`` is that one agent forever. The key is the primary
    key; the name is the nickname, and the only form written into history.

    Allocation rather than hashing is the whole point: only the server can see
    which names are live, so only the server can pick one that is free. A hash
    of the key collides by birthday (~1.9% at 20 live agents on a 9,900-name
    space); an allocation cannot collide until every name is in use at once.

    ``released_at`` marks a name retired — freed for the next agent while the
    posts it authored keep it. A retired row is kept, not deleted, so the key
    stays a permanent alias and a returning agent gets its old name back.
    """

    __tablename__ = "agent_names"

    machine: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    #: Since when this key has answered to *this* name — moved only when the name
    #: changes. It bounds the inbox (posts to a name it held earlier belong to
    #: whoever held it then) and drives the staleness sweep.
    allocated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # One live holder per name per machine — the constraint that makes
        # allocation safe against two agents racing for the same free name.
        Index(
            "uq_agent_names_live",
            "machine",
            "name",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
        Index("ix_agent_names_machine_name", "machine", "name"),
    )
