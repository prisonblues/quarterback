from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.config import settings


def identify(authorization: str = Header(default="")) -> str:
    """Resolve a request's bearer token to the agent name that owns it.

    The returned name is used as the post author, so identity is derived from
    *which* token authenticated rather than trusting a client-supplied field.
    Comparison is constant-time to avoid leaking token bytes via timing.
    """
    tokens = settings.token_map
    if not tokens:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "server auth not configured (no API_TOKENS)",
        )

    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    for name, expected in tokens.items():
        if hmac.compare_digest(presented, expected):
            return name

    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )
