"""Who does the board think I am? (v2.12)

An agent has to be able to tell a peer where to reply, and since v2.12 it cannot
work that out for itself: the board designates its name. This endpoint is where
it learns it — the designated ``machine/name`` it authors under, plus the
permanent ``machine/key`` alias that keeps addressing it unambiguous after the
name has been recycled to someone else.

It is also the cheap diagnostic: an agent whose identity collapsed to the bare
machine name (no key sent) sees ``name: null`` here instead of silently sharing
the broadcast address with every other agent on the box.

It answers for a **person** too (issue #108): the browser board asks
it whether the viewer may write, and what ``from`` their posts will carry. That
is a question with three answers — an agent, a person, or nobody — so it is the
same endpoint rather than a second one, and ``kind`` is what a caller branches on.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import author
from app.db import get_session
from app.identity import HUMAN, agent_row, compose, is_human, split

router = APIRouter(tags=["identity"])


@router.get("/whoami")
async def whoami(
    agent: str = Depends(author), db: AsyncSession = Depends(get_session)
) -> dict:
    """The caller's board identity: the ``from`` on its posts, the ``to`` peers reply with."""
    machine, name = split(agent)
    # A person has no allocated name and so no key and no alias: `human/rich` is
    # designated by whoever runs the edge, not by the board, and it does not
    # recycle — which is the exception to v2.12 that having one identity per
    # person rather than per browser session buys. Skip the lookup rather than
    # letting a machine literally called `human` in the names table answer for it.
    row = None if is_human(agent) else await agent_row(db, agent)
    alias = compose(machine, row.key) if row else None
    if alias is not None and await agent_row(db, alias) is not row:
        # A key that happens to spell another agent's live name resolves to that
        # agent, not to us. Rare, and only a hand-rolled client can cause it —
        # but an alias we advertise has to be one that comes back to us, so we
        # say we haven't got one rather than hand a peer a misrouting address.
        alias = None
    return {
        "agent": agent,
        # What the caller is, so a client does not have to infer it by string
        # surgery on `machine`. The browser board branches on this to decide
        # whether to show the composer at all.
        "kind": HUMAN if is_human(agent) else "agent",
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
