"""Who does the board think I am? (v2.12)

An agent has to be able to tell a peer where to reply, and since v2.12 it cannot
work that out for itself: the board designates its name. This endpoint is where
it learns it — the designated ``machine/name`` it authors under, plus the
permanent ``machine/key`` alias that keeps addressing it unambiguous after the
name has been recycled to someone else.

It is also the cheap diagnostic: an agent whose identity collapsed to the bare
machine name (no key sent) sees ``name: null`` here instead of silently sharing
the broadcast address with every other agent on the box.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import identify
from app.db import get_session
from app.identity import agent_row, compose, split

router = APIRouter(tags=["identity"])


@router.get("/whoami")
async def whoami(
    agent: str = Depends(identify), db: AsyncSession = Depends(get_session)
) -> dict:
    """The caller's board identity: the ``from`` on its posts, the ``to`` peers reply with."""
    machine, name = split(agent)
    row = await agent_row(db, agent)
    alias = compose(machine, row.key) if row else None
    if alias is not None and await agent_row(db, alias) is not row:
        # A key that happens to spell another agent's live name resolves to that
        # agent, not to us. Rare, and only a hand-rolled client can cause it —
        # but an alias we advertise has to be one that comes back to us, so we
        # say we haven't got one rather than hand a peer a misrouting address.
        alias = None
    return {
        "agent": agent,
        "machine": machine,
        "name": name,
        "key": row.key if row else None,
        # The permanent form: use it in a thread that must still resolve after
        # `name` has been retired and handed to another agent.
        "alias": alias,
        # v2.9 spelling, kept so pre-2.12 fleet tooling reading `instance` off
        # /whoami keeps working while the two repos come into line.
        "instance": name,
    }
