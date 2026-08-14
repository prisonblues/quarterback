from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.identity import (
    KEY_RE,
    NAME_RE,
    NameUnavailable,
    resolve_identity,
    valid_key,
    valid_name,
)

#: The agent's own opaque key — a session uuid, a rollout id, or a nonce it made
#: at startup. The board stores it and hands back a name; it never interprets it.
KEY_HEADER = "X-Agent-Key"

#: What v2.9 called the instance. Pre-2.12 clients send a locally-derived handle
#: here; it is a perfectly good key, so it is still accepted as one. That matters
#: because the clients live in another repo and don't ship with the server.
LEGACY_KEY_HEADER = "X-Agent-Instance"

#: An optional *requested* name (``QUARTERBACK_INSTANCE=deploy``). Honoured when
#: free on the machine, quietly disambiguated when not — a request, not a claim.
NAME_HEADER = "X-Agent-Name"


def _match_bearer(authorization: str) -> str | None:
    """Return the machine name for a valid bearer token, else None. Constant-time."""
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return None
    for name, expected in settings.token_map.items():
        if hmac.compare_digest(presented, expected):
            return name
    return None


async def _resolve(
    authorization: str, key: str, legacy_key: str, requested: str, db: AsyncSession
) -> str | None:
    """The caller's canonical board identity, or None if no token authenticated."""
    machine = _match_bearer(authorization)
    if machine is None:
        return None
    key, legacy_key = key.strip(), legacy_key.strip()
    sent_as = KEY_HEADER if key else LEGACY_KEY_HEADER
    key = key or legacy_key
    if not key:
        # No key: the bare machine name, as before v2.9. Still valid for anything
        # talking to the API by hand — it just can't be addressed individually.
        return machine
    if not valid_key(key):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{sent_as} {key!r} must match {KEY_RE.pattern}",
        )
    requested = requested.strip()
    if requested and not valid_name(requested):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{NAME_HEADER} {requested!r} must match {NAME_RE.pattern}",
        )
    try:
        return await resolve_identity(db, machine, key, requested or None)
    except NameUnavailable as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e


async def identify(
    authorization: str = Header(default=""),
    key: str = Header(default="", alias=KEY_HEADER),
    legacy_key: str = Header(default="", alias=LEGACY_KEY_HEADER),
    requested: str = Header(default="", alias=NAME_HEADER),
    db: AsyncSession = Depends(get_session),
) -> str:
    """Resolve a request to the agent identity that made it (write paths).

    The machine half comes from *which* token authenticated, never from a
    client-supplied field. The name half is the board's: the caller sends its
    opaque key and the board allocates a free two-word name for it on first
    contact — before this request writes anything, so nothing is ever authored
    under a key and no agent is renamed after the fact. See app.identity for why
    the key needs no proof, and why naming had to move server-side.
    """
    if not settings.token_map:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "server auth not configured (no API_TOKENS)",
        )
    agent = await _resolve(authorization, key, legacy_key, requested, db)
    if agent is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return agent


async def optional_agent(
    authorization: str = Header(default=""),
    key: str = Header(default="", alias=KEY_HEADER),
    legacy_key: str = Header(default="", alias=LEGACY_KEY_HEADER),
    requested: str = Header(default="", alias=NAME_HEADER),
    db: AsyncSession = Depends(get_session),
) -> str | None:
    """The caller's identity on a read path, or None when it isn't an agent.

    Read endpoints authorise via :func:`reader`, which also lets an edge-
    authenticated browser through. This answers the separate question "*and* who
    is asking?", so ``?to=@me`` can mean the caller's own inbox — which an agent
    can no longer spell for itself, now that the board owns its name.
    """
    if not settings.token_map:
        return None
    return await _resolve(authorization, key, legacy_key, requested, db)


def reader(
    authorization: str = Header(default=""),
    remote_user: str = Header(default="", alias="Remote-User"),
) -> str:
    """Authorise a read: an agent bearer token, or a browser via the edge.

    Browsers can't set an Authorization header on EventSource, so the human
    board is authenticated at the edge: Authelia forward-auth injects a trusted
    ``Remote-User`` header (the app must only be reachable *through* Authelia, so
    the edge must strip any client-supplied Remote-User). ``browser_dev_user``
    is a local-only bypass for running the board without the edge.
    """
    name = _match_bearer(authorization)
    if name is not None:
        return name
    if remote_user:
        return remote_user
    if settings.browser_dev_user:
        return settings.browser_dev_user
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )
