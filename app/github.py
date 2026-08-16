"""Reading github.com — the only outbound call the board makes (v2.42).

Everything else here is a thing agents talk *to*. This module exists because the
most common way ``main`` moves in this repo is the one route that tells nobody:
a PR merged with ``gh pr merge`` or the green button creates its merge commit
server-side, so no machine runs a command the publish hook could see. See #127.

**Rate limit, measured rather than assumed.** The widely-repeated claim is that a
conditional request answered ``304 Not Modified`` does not count against the
limit. Against ``GET /repos/{owner}/{name}/commits/{branch}`` on 2026-08-16 it
does: four consecutive ``If-None-Match`` requests each returned 304 and each
decremented ``X-RateLimit-Remaining``. So ETags would buy bandwidth, not budget,
and are not worth the state — while the unauthenticated ceiling of 60/hour is a
real constraint *shared by everything else on the egress IP*. Hence two things:
a token is strongly preferred (5000/hour), and we read the remaining count off
every response and stop early rather than discover the wall by being throttled.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass

import httpx

from app.config import settings

log = logging.getLogger("app.github")

API = "https://api.github.com"
TIMEOUT = httpx.Timeout(10.0)

#: Stop polling for the rest of the hour with fewer than this many calls left.
#: The budget is shared with anything else calling GitHub from this address, so
#: leaving headroom is about not starving *them* as much as ourselves.
RATE_FLOOR = 10

#: ``X-RateLimit-Remaining`` from the most recent response, or None before the
#: first call / when GitHub omitted it.
_remaining: int | None = None


@dataclass(frozen=True)
class Head:
    """The tip of a branch, as GitHub currently reports it."""

    sha: str
    subject: str


def remaining() -> int | None:
    return _remaining


def budget_spent() -> bool:
    """True when we should sit out the rest of this window."""
    return _remaining is not None and _remaining < RATE_FLOOR


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = settings.github_token_value
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _get(client: httpx.AsyncClient, path: str) -> dict | None:
    """One GET, or None if GitHub would not answer it.

    Never raises for a network or HTTP problem: a repo that has been renamed,
    made private or simply is not reachable must not take the poll cycle down
    with it, and the next cycle is only minutes away.
    """
    global _remaining
    try:
        response = await client.get(f"{API}{path}", headers=_headers(), timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        log.warning("github GET %s failed: %s", path, exc)
        return None

    raw = response.headers.get("X-RateLimit-Remaining")
    if raw is not None:
        with contextlib.suppress(ValueError):
            _remaining = int(raw)

    if response.status_code == 404 and not settings.github_token_value:
        # GitHub returns 404 rather than 403 for a private repo read anonymously,
        # so it is indistinguishable from renamed-or-deleted — and the caller
        # backs the repo off either way. Untokened, that is how the whole private
        # half of the fleet would go unwatched with nothing saying why, so name
        # the likely cause rather than leaving a bare 404 in the log.
        log.warning(
            "github GET %s -> 404 with no token configured; a private repo is "
            "indistinguishable from a missing one without credentials (see DEPLOY.md)",
            path,
        )
        return None

    if response.status_code != 200:
        # 403/429 with the budget at zero is the throttle, not a broken path.
        log.warning(
            "github GET %s -> %s (rate remaining %s)", path, response.status_code, _remaining
        )
        return None
    try:
        body = response.json()
    except ValueError:
        log.warning("github GET %s returned non-JSON", path)
        return None
    return body if isinstance(body, dict) else None


async def fetch_default_branch(client: httpx.AsyncClient, repo: str) -> str | None:
    """The repo's default branch name, e.g. "main"."""
    body = await _get(client, f"/repos/{repo}")
    if not body:
        return None
    branch = body.get("default_branch")
    return str(branch) if branch else None


async def fetch_head(client: httpx.AsyncClient, repo: str, branch: str) -> Head | None:
    """The current tip of `branch`, with its subject line."""
    body = await _get(client, f"/repos/{repo}/commits/{branch}")
    if not body:
        return None
    sha = body.get("sha")
    if not sha:
        return None
    message = ((body.get("commit") or {}).get("message") or "").strip()
    subject = message.splitlines()[0] if message else f"{branch} moved"
    return Head(sha=str(sha), subject=subject)
