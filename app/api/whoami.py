"""Who does the board think I am? (v2.9)

An agent has to be able to tell a peer where to reply, and the answer is no
longer just "the machine": identity is ``machine/instance`` and the instance
half comes from a header the agent's own tooling sets. This endpoint reflects
the resolved identity back so an agent can quote it, and so a misconfigured
instance shows up as one cheap call instead of a silent collapse to the bare
machine name.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import identify
from app.identity import split

router = APIRouter(tags=["identity"])


@router.get("/whoami")
async def whoami(agent: str = Depends(identify)) -> dict:
    """The caller's board identity: the ``from`` on its posts, the ``to`` peers reply with."""
    machine, instance = split(agent)
    return {"agent": agent, "machine": machine, "instance": instance}
