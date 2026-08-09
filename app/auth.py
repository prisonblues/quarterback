from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.config import settings
from app.identity import INSTANCE_RE, compose, valid_instance

#: Header an agent uses to distinguish itself from its co-tenants on a machine.
INSTANCE_HEADER = "X-Agent-Instance"


def _match_bearer(authorization: str) -> str | None:
    """Return the agent name for a valid bearer token, else None. Constant-time."""
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return None
    for name, expected in settings.token_map.items():
        if hmac.compare_digest(presented, expected):
            return name
    return None


def identify(
    authorization: str = Header(default=""),
    instance: str = Header(default="", alias=INSTANCE_HEADER),
) -> str:
    """Resolve a request to the agent identity that made it (write paths).

    The machine half comes from *which* token authenticated, never from a
    client-supplied field. The optional ``X-Agent-Instance`` header adds the
    finer grain — one of the several agents running on that machine — giving
    ``machine/instance``. Without it the identity is the bare machine name, as
    it was before v2.9. See app.identity for why the instance needs no proof.
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
    if instance and not valid_instance(instance):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{INSTANCE_HEADER} {instance!r} must match {INSTANCE_RE.pattern}",
        )
    return compose(name, instance or None)


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
