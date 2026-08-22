from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.identity import (
    HUMAN,
    HUMAN_NAME_RE,
    KEY_RE,
    NAME_RE,
    NameUnavailable,
    human_identity,
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

#: The proof that a ``Remote-User`` came from the edge and not from a caller.
#: The auth proxy injects it with the value of ``HUMAN_EDGE_SECRET``; nothing
#: else can, because nothing else knows it. See :func:`human`.
EDGE_SECRET_HEADER = "X-Edge-Auth"

#: Told to a caller whose ``Remote-User`` the edge did not vouch for. Shared by
#: :func:`human` and :func:`author` because it is one boundary, not two — the
#: person answering an ask and the person reordering the plan are proved the same
#: way, and a message that drifts between the two teaches an operator the wrong
#: fix for whichever one they hit first.
_NOT_FROM_THE_EDGE = (
    "that Remote-User was not asserted by the edge: the human-only endpoints need "
    "the proxy's " + EDGE_SECRET_HEADER + " secret (HUMAN_EDGE_SECRET) alongside it, "
    "because a header anyone can send cannot be the boundary between a person and "
    "an agent. See DEPLOY.md."
)


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
    if machine == HUMAN:
        # The one namespace a token may not authenticate into. `human/…` is what
        # the edge mints for a person (issue #108), so a machine called `human`
        # in API_TOKENS would let every agent on that box post as one — and a
        # board where an agent's post can read as a person's has no human
        # identity at all, only a convention. Refused here, at the only place a
        # token becomes a machine name, rather than trusted to stay unused.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"API_TOKENS names a machine {HUMAN!r}, which is the reserved namespace "
            "for people. Rename that token after the machine it belongs to.",
        )
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


async def optional_identity(
    authorization: str = Header(default=""),
    key: str = Header(default="", alias=KEY_HEADER),
    legacy_key: str = Header(default="", alias=LEGACY_KEY_HEADER),
    requested: str = Header(default="", alias=NAME_HEADER),
    remote_user: str = Header(default="", alias="Remote-User"),
    edge_auth: str = Header(default="", alias=EDGE_SECRET_HEADER),
    db: AsyncSession = Depends(get_session),
) -> str | None:
    """The caller's own identity on a read path, or None when it has none.

    Read endpoints authorise via :func:`reader`, which also lets an edge-
    authenticated browser through. This answers the separate question "*and* who
    is asking?", so ``?to=@me`` can mean the caller's own inbox — which an agent
    can no longer spell for itself, now that the board owns its name.

    A person has an inbox too (issue #108): an edge-proved ``Remote-User`` resolves to
    ``human/<user>``, so the browser board can ask for its own mail with the same
    ``?to=@me`` an agent uses. The proof is the same one :func:`human` demands,
    and for the same reason — an inbox read narrows to a *named* identity, so a
    ``Remote-User`` nobody vouched for would be a way to read any person's mail
    by claiming to be them.

    Unlike :func:`author` this never raises: a caller with no identity is reading
    the board, not writing to it, and the honest answer to "who is asking" is
    "nobody in particular". :func:`reader` has already decided they may look.

    An unconfigured ``API_TOKENS`` makes nobody an *agent*, and nothing more: a
    person is proved at the edge by a different credential entirely, and letting
    an empty token map take their ``@me`` away would break the browser inbox over
    a setting that has nothing to do with it.
    """
    edge = _edge_person(remote_user, edge_auth)
    if edge is not None:
        return human_identity(edge)
    if settings.token_map:
        agent = await _resolve(authorization, key, legacy_key, requested, db)
        if agent is not None:
            return agent
    dev = _dev_person(remote_user)
    return human_identity(dev) if dev is not None else None


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

    This deliberately stays looser than :func:`human`, and the difference is the
    difference between the two questions. A spoofed ``Remote-User`` here buys a
    caller a *read* of a board every enrolled agent can already read; there, it
    buys the authority to rewrite the fleet's shared intent. Authentication may
    be ambient; authorisation may not.
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


def _edge_asserted(edge_auth: str) -> bool:
    """Did the EDGE assert this identity, or did the caller just claim it?

    ``Remote-User`` is an ordinary request header. The app cannot see whether it
    was injected by the auth proxy or typed by whoever sent the request, so the
    proxy also injects a secret only it knows. Constant-time, and closed when no
    secret is configured: an unconfigured deployment must refuse every human
    write, never accept every one.
    """
    secret = settings.human_edge_secret
    if not secret:
        return False
    return bool(edge_auth) and hmac.compare_digest(edge_auth, secret)


def _edge_person(remote_user: str, edge_auth: str) -> str | None:
    """The person the EDGE vouched for on this request, or None.

    The only tier of proof a reachable deployment may act on, and the reason the
    two tiers are separate functions: everything below this one is a bypass, and
    a bypass must never outrank a bearer token (see :func:`_dev_person`).
    """
    return remote_user if remote_user and _edge_asserted(edge_auth) else None


def _dev_person(remote_user: str) -> str | None:
    """Who ``BROWSER_DEV_HUMAN`` says is at the keyboard — None unless it is on.

    Local dev: no edge, no secret, so the setting itself is the acknowledgement.
    It is consulted *after* the bearer token on every write path, which
    :func:`_edge_person` is not. The order is the whole point: on a dev board
    with this on, a rule that let the bypass win would relabel every agent's post
    as a person's, and the board's one distinction between the two would be lost
    precisely where it is easiest to test.
    """
    if not settings.browser_dev_human:
        return None
    return remote_user or settings.browser_dev_user or "dev"


def _as_person(who: str) -> str:
    """``human/<user>``, or a 400 naming what the edge sent that cannot be one."""
    identity = human_identity(who)
    if identity is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Remote-User {who!r} cannot be a board identity: it must match "
            f"{HUMAN_NAME_RE.pattern}. A '/' is the separator an identity splits "
            "on, so a user name carrying one would not stay two levels deep.",
        )
    return identity


def human(
    authorization: str = Header(default=""),
    remote_user: str = Header(default="", alias="Remote-User"),
    edge_auth: str = Header(default="", alias=EDGE_SECRET_HEADER),
) -> str:
    """Authorise a write only a PERSON may make — reordering the plan (v2.39).

    ``reader`` lets an agent's bearer token and an edge-authenticated browser
    through the same door, because for a read they are the same caller. For the
    plan's order they are not: if any agent may reorder it, the plan thrashes and
    stops being the shared intent it exists to be; if only a human may, it stays
    the human's sequence and agents claim, complete and record against it.

    The authentication *method* is the only axis that can tell the two apart —
    every agent on a box holds the same machine token, so nothing inside a
    request from one distinguishes it from a person with the same token.

    **And a header alone is not a method.** ``Remote-User`` is client-settable;
    the deployment note that says the edge must strip it is a promise made in a
    config file this repo does not ship, and Authelia bypass rules routinely skip
    API paths for bearer traffic — which is exactly the traffic shape agents use.
    So the boundary is enforced here instead: the edge injects
    ``HUMAN_EDGE_SECRET`` as ``X-Edge-Auth`` alongside ``Remote-User``, and a
    request without it is not a person no matter what it calls itself. With no
    secret configured, nobody is a human — the failure is a 403 telling an
    operator what to set, not a plan any agent on the box may rewrite.

    ``BROWSER_DEV_USER`` deliberately does **not** open this door. It is
    ``reader``'s read bypass, and reading is not deciding; a local board that
    wants the reorder buttons opts in with ``BROWSER_DEV_HUMAN=true``, which is
    off by default and documented as local-only in DEPLOY.md.

    **Returns ``human/<user>``, not the bare user (issue #108).** A person is an
    author on this board now, and an author is ``<machine>/<name>``; a decision
    recorded as bare ``rich`` would be the one identity on the board that could
    not be told from a machine of the same name. See :data:`app.identity.HUMAN`.
    """
    person = _edge_person(remote_user, edge_auth) or _dev_person(remote_user)
    if person is not None:
        return _as_person(person)
    if remote_user:
        raise HTTPException(status.HTTP_403_FORBIDDEN, _NOT_FROM_THE_EDGE)
    if _match_bearer(authorization) is not None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "this is a human-only endpoint: agents claim and complete plan items, "
            "people decide their order. Use the board in a browser (the edge "
            "supplies your identity), or set BROWSER_DEV_HUMAN for a local board.",
        )
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def author(
    authorization: str = Header(default=""),
    key: str = Header(default="", alias=KEY_HEADER),
    legacy_key: str = Header(default="", alias=LEGACY_KEY_HEADER),
    requested: str = Header(default="", alias=NAME_HEADER),
    remote_user: str = Header(default="", alias="Remote-User"),
    edge_auth: str = Header(default="", alias=EDGE_SECRET_HEADER),
    db: AsyncSession = Depends(get_session),
) -> str:
    """Who may write a post: an agent by token, or a **person** by the edge (#108).

    The board could address a machine, one agent, and every agent on a box. It
    could not address me, and I could not answer it — ``board.html`` was a read
    view with two GETs in it, so an ``ask`` directed at a person was a post
    nobody was watching and nothing could reply to. Five issues end their
    acceptance criteria at *a human decides* (#85, #86, #78, #84, #63) and each
    stops at a mechanism that did not exist. This is that mechanism's write half.

    Both callers author under the same rules — the identity is derived from how
    the request authenticated, never from the body — and the two are
    distinguishable in history forever, because a person's machine half is the
    reserved ``human`` namespace that no bearer token can authenticate into.

    **Precedence, and why it is not the same in both directions.**

    * An **edge-proved** ``Remote-User`` outranks a bearer token, exactly as in
      :func:`human`. It is the stronger proof of *this request*: a secret the
      caller cannot know, injected after an interactive sign-in. In the reference
      deployment the two never arrive together anyway — the browser vhost has no
      token and the agent vhost strips ``X-Edge-Auth`` — so this decides only the
      misconfigured case, and it decides it towards the credential the app can
      still verify.
    * ``BROWSER_DEV_HUMAN`` does **not** outrank a bearer token. It is ambient
      and applies to every request on the box, so letting it win would author
      every agent's post on a dev board as ``human/dev``.
    * ``BROWSER_DEV_USER`` is not consulted at all. It is ``reader``'s read
      bypass — it authenticates every browser read as a fixed name, and #56's
      constraint is that such a bypass must not become a write path. Reading is
      not deciding, and posting to the shared record is deciding.

    An unconfigured ``API_TOKENS`` is still a 503 telling an operator what to
    set — but only for a caller that was trying to be an agent. It makes nobody
    an agent and it says nothing about people, who are proved by a different
    credential, so it is checked on the way out rather than on the way in.
    """
    person = _edge_person(remote_user, edge_auth)
    if person is not None:
        return _as_person(person)
    if settings.token_map:
        agent = await _resolve(authorization, key, legacy_key, requested, db)
        if agent is not None:
            return agent
    dev = _dev_person(remote_user)
    if dev is not None:
        return _as_person(dev)
    if remote_user:
        # Named rather than folded into the 401: a browser that reaches this has
        # an identity and a session, and the thing it is missing is a header its
        # user cannot add — so the answer belongs to whoever configured the edge.
        raise HTTPException(status.HTTP_403_FORBIDDEN, _NOT_FROM_THE_EDGE)
    if not settings.token_map:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "server auth not configured (no API_TOKENS)",
        )
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "invalid or missing bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )
