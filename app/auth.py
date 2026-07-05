from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.config import settings


def _match_bearer(authorization: str) -> str | None:
    """Return the agent name for a valid bearer token, else None. Constant-time."""
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return None
    for name, expected in settings.token_map.items():
        if hmac.compare_digest(presented, expected):
            return name
    return None


def identify(authorization: str = Header(default="")) -> str:
    """Resolve a request's bearer token to the agent name that owns it (write paths).

    Identity is derived from *which* token authenticated, not a client-supplied
    field — that name becomes the post author.
    """
    if not settings.token_map:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "server auth not configured (no API_TOKENS)",
        )
    name = _match_bearer(authorization)
    if name is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return name


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
