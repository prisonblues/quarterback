"""HTTP client for the quarterback board API.

Wraps the coordination endpoints with bearer-token auth and consistent error
handling. The token names the machine; ``key`` is this agent's own opaque
handle, which the board maps to the short name it records as the author of every
post (see ``app.identity`` on the server). The client does not derive a display
identity — that was the bug: a name derived here has to agree with every other
process that names the same agent, across repos that don't ship together.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator

import httpx


def _decode_frame(data: list[str]) -> dict | None:
    """The frame's payload, or None if it is not something a consumer can read.

    Every caller reaches straight for ``.get`` on what this yields, so a frame
    that parses to null, a list or a scalar is as unusable as one that does not
    parse at all — and is dropped by the same rule rather than crashing a tail
    several hours in.
    """
    try:
        event = json.loads("\n".join(data))
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def sse_events(lines: Iterable[str]) -> Iterator[dict]:
    """Decode an SSE line stream into the JSON object of each ``data:`` field.

    Only ``data`` is read. ``/stream`` sets ``id:`` to the same post id the
    payload already carries, and tracking both invites the two disagreeing about
    where the cursor is. Comment lines (``: ping``, sse-starlette's keep-alive)
    and payloads that don't parse are skipped rather than raised: one malformed
    frame must not end a tail that has been running for a day.
    """
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if data:
                event = _decode_frame(data)
                if event is not None:
                    yield event
                data = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field == "data":
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        # A connection dropped after the payload but before its blank line still
        # left a whole post behind; discarding it cost the cursor that post and
        # the reader the line. A genuinely truncated payload does not parse, so
        # it is dropped here anyway.
        event = _decode_frame(data)
        if event is not None:
            yield event


class QuarterbackClient:
    """HTTP wrapper around the quarterback board API."""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        key: str | None = None,
        requested_name: str | None = None,
        session: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = session
        # No token ⇒ no header at all, rather than a "Bearer " that authenticates
        # nothing. That is the tokenless client the board TUI starts with on a
        # host that has no credential: every authed call 401s, and ``health()``
        # — which the server guards with no dependency — still answers.
        headers = {}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        if key:
            headers["X-Agent-Key"] = key
        if requested_name:
            headers["X-Agent-Name"] = requested_name
        # A TRANSPORT is injected, never a client, and this is round 2's third
        # P2. The parameter used to take an httpx.Client and call
        # `.headers.update()` on it — which mutates an object the caller owns, so
        # constructing a second QuarterbackClient over one shared client
        # overwrote the first one's bearer. Round 1 was right that an injected
        # client must still authenticate and wrong about where to put the
        # headers.
        #
        # Taking the transport removes the question rather than answering it: a
        # client is only ever passed for its transport anyway (a test's
        # MockTransport, a proxy's), the httpx.Client is then always ours to
        # configure, and two QuarterbackClients over one transport hold their own
        # credentials — which is the property that was actually wanted.
        self._http = httpx.Client(timeout=30, headers=headers, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def whoami(self) -> dict:
        resp = self._http.get(self._url("/whoami"))
        resp.raise_for_status()
        return resp.json()

    def post(self, body: dict) -> dict:
        # Stamp the session here rather than at each call site, so anything that
        # posts — board_post, publish, whatever comes next — is attributable to
        # the agent that wrote it without having to remember to say so.
        if self._session and "session" not in body:
            body = {**body, "session": self._session}
        resp = self._http.post(self._url("/post"), json=body)
        resp.raise_for_status()
        return resp.json()

    def board(self, params: dict) -> list[dict]:
        resp = self._http.get(self._url("/board"), params=params)
        resp.raise_for_status()
        return resp.json()

    def board_head(self, params: dict) -> tuple[list[dict], int | None]:
        """A page of the board, and where the board actually ends.

        The second half comes from the ``X-Board-Head`` response header, so it is
        the newest id on the WHOLE board even when this request was filtered down
        to one type or one recipient — which is the thing a filtered body cannot
        tell you, and the thing a tail needs to pick a stream cursor without
        asking twice (#173).

        ``None`` when the header is absent or unreadable, which is a board older
        than this client rather than an error: the fleet is deployed by pushing to
        `main`, so a client can very ordinarily be newer than the board it talks
        to. Callers fall back to the behaviour they had before the header existed.
        """
        resp = self._http.get(self._url("/board"), params=params)
        resp.raise_for_status()
        raw = resp.headers.get("X-Board-Head")
        try:
            head = int(raw) if raw is not None else None
        except ValueError:
            head = None
        return resp.json(), head

    def get_post(self, post_id: int) -> dict:
        resp = self._http.get(self._url(f"/post/{post_id}"))
        resp.raise_for_status()
        return resp.json()

    def stream(self, since: int = 0, read_timeout: float = 90.0) -> Iterator[dict]:
        """Yield summary-tier posts from ``/stream``: the backlog after ``since``,
        then live ones as they land, forever.

        ``read_timeout`` is a liveness check, not a patience limit. The board
        sends a keep-alive comment every few seconds, so a read that stalls for
        this long means the connection is dead rather than the board being quiet
        — the caller gets an ``httpx.ReadTimeout`` it can reconnect on instead of
        waiting on a socket nothing will ever write to again.
        """
        timeout = httpx.Timeout(read_timeout, connect=10.0)
        with self._http.stream(
            "GET", self._url("/stream"), params={"since": since}, timeout=timeout
        ) as resp:
            resp.raise_for_status()
            yield from sse_events(resp.iter_lines())

    # -- read-only views the terminal board renders (v2.40) -------------

    def health(self) -> dict:
        """The one endpoint that needs no auth — up/down on a host with no token."""
        resp = self._http.get(self._url("/health"), timeout=10.0)
        resp.raise_for_status()
        return resp.json()

    def sessions(self, limit: int = 50) -> list[dict]:
        resp = self._http.get(self._url("/sessions"), params={"limit": limit})
        resp.raise_for_status()
        return resp.json()

    def review_stats(self, params: dict) -> dict:
        resp = self._http.get(
            self._url("/review/stats"),
            params={k: v for k, v in params.items() if v is not None},
        )
        resp.raise_for_status()
        return resp.json()

    # -- blobs (v2) ----------------------------------------------------

    def put_blob(self, content: bytes) -> dict:
        sha = hashlib.sha256(content).hexdigest()
        resp = self._http.put(self._url(f"/blob/{sha}"), content=content)
        resp.raise_for_status()
        return resp.json()

    def get_blob(self, sha: str) -> bytes:
        resp = self._http.get(self._url(f"/blob/{sha}"))
        resp.raise_for_status()
        return resp.content

    # -- leases / handoff (v2) -----------------------------------------

    def lease(self, body: dict) -> dict:
        resp = self._http.post(self._url("/lease"), json=body)
        resp.raise_for_status()
        return resp.json()

    def renew_lease(self, lease_id: str) -> dict:
        resp = self._http.post(self._url("/lease/renew"), json={"lease_id": lease_id})
        resp.raise_for_status()
        return resp.json()

    def release_lease(self, lease_id: str) -> dict:
        resp = self._http.post(self._url("/lease/release"), json={"lease_id": lease_id})
        resp.raise_for_status()
        return resp.json()

    # -- resource claims + release allocation (v2.31) -------------------

    def claim(self, body: dict) -> dict:
        resp = self._http.post(self._url("/claim"), json=body)
        resp.raise_for_status()
        return resp.json()

    def renew_claim(self, claim_id: str, session: str | None = None) -> dict:
        resp = self._http.post(self._url("/claim/renew"),
                               json={"claim_id": claim_id, "session": session})
        resp.raise_for_status()
        return resp.json()

    def release_claim(self, claim_id: str, session: str | None = None) -> dict:
        resp = self._http.post(self._url("/claim/release"),
                               json={"claim_id": claim_id, "session": session})
        resp.raise_for_status()
        return resp.json()

    def claims(self, params: dict) -> dict:
        resp = self._http.get(self._url("/claims"),
                              params={k: v for k, v in params.items() if v is not None})
        resp.raise_for_status()
        return resp.json()

    def claim_held(self, params: dict) -> dict:
        resp = self._http.get(self._url("/claim/held"),
                              params={k: v for k, v in params.items() if v is not None})
        resp.raise_for_status()
        return resp.json()

    # -- the landing queue (v2.61, #227) --------------------------------

    def merge_queue(self, params: dict) -> dict:
        resp = self._http.get(self._url("/merge-queue"),
                              params={k: v for k, v in params.items() if v is not None})
        resp.raise_for_status()
        return resp.json()

    def merge_queue_write(self, path: str, body: dict) -> dict:
        """Enqueue or leave.

        The session is stamped on ``enqueue`` for the reason :meth:`plan_verb`
        gives, one step sharper: everyone behind a queue head needs to know
        *which agent on that box* to ask about it, and a machine name on a box
        running four agents does not say. ``leave`` records the machine and the
        reason and takes no session, so nothing is stamped onto it — an argument
        the endpoint ignores is an argument that will one day mean something
        else.
        """
        if path == "enqueue" and self._session and not body.get("session"):
            body = {**body, "session": self._session}
        resp = self._http.post(self._url(f"/merge-queue/{path}"), json=body)
        resp.raise_for_status()
        return resp.json()

    # -- the plan (v2.39; a plan became a row of its own in #172) ------

    def plans(self, params: dict) -> dict:
        return self._plan_read("/plans", params)

    def plan_submit(self, body: dict) -> dict:
        if self._session and not body.get("session"):
            body = {**body, "session": self._session}
        resp = self._http.post(self._url("/plan/submit"), json=body)
        resp.raise_for_status()
        return resp.json()

    def plan_verb(self, path: str, body: dict) -> dict:
        """One of the whole-PLAN verbs (claim / release / done).

        The session is stamped here for the reason :meth:`plan_item` gives: a
        claim whose holder cannot be reached is half a claim.
        """
        if self._session and not body.get("session"):
            body = {**body, "session": self._session}
        resp = self._http.post(self._url(f"/plan/{path}"), json=body)
        resp.raise_for_status()
        return resp.json()

    def plan(self, params: dict) -> dict:
        return self._plan_read("/plan", params)

    def _plan_read(self, path: str, params: dict) -> dict:
        """A plan read, carrying this session so `covered_by` can be exact.

        Stamped here for the reason :meth:`plan_item` stamps it on a write: a plan
        claim is owned by the session, so a read that names no session can only be
        answered by MACHINE — and on a box running several agents that tells a
        co-tenant its neighbour's held plan is free.
        """
        if self._session and not params.get("session"):
            params = {**params, "session": self._session}
        resp = self._http.get(self._url(path),
                              params={k: v for k, v in params.items() if v is not None})
        resp.raise_for_status()
        return resp.json()

    def plan_add(self, body: dict) -> dict:
        resp = self._http.post(self._url("/plan/item"), json=body)
        resp.raise_for_status()
        return resp.json()

    def plan_item(self, path: str, body: dict) -> dict:
        """One of the per-item verbs (claim / release / done / depends).

        The session is stamped here rather than at each call site, for the same
        reason ``post`` does it: a claim whose holder cannot be reached is half a
        claim, and an agent that forgot to pass its own id should not be the
        reason the next agent has nobody to ask.
        """
        if self._session and not body.get("session"):
            body = {**body, "session": self._session}
        resp = self._http.post(self._url(f"/plan/item/{path}"), json=body)
        resp.raise_for_status()
        return resp.json()

    def plan_order(self, params: dict) -> dict:
        resp = self._http.get(self._url("/plan/order"),
                              params={k: v for k, v in params.items() if v is not None})
        resp.raise_for_status()
        return resp.json()

    def handoff(self, session: str, blob: str) -> dict:
        resp = self._http.post(self._url("/handoff"), json={"session": session, "blob": blob})
        resp.raise_for_status()
        return resp.json()

    def session_state(self, session: str) -> dict:
        resp = self._http.get(self._url(f"/session/{session}"))
        resp.raise_for_status()
        return resp.json()

    # -- worktree registry (v2.1) --------------------------------------

    def put_worktrees(self, body: dict) -> dict:
        resp = self._http.put(self._url("/worktrees"), json=body)
        resp.raise_for_status()
        return resp.json()

    def get_worktrees(self, params: dict) -> list[dict]:
        resp = self._http.get(self._url("/worktrees"), params=params)
        resp.raise_for_status()
        return resp.json()

    # -- publish / sync advisories (v2.8) ------------------------------

    def sync(self, params: dict) -> dict:
        resp = self._http.get(self._url("/sync"), params=params)
        resp.raise_for_status()
        return resp.json()

    # -- coordination: collision index + sub-agents (v2.6) -------------

    def active(self, params: dict) -> dict:
        resp = self._http.get(self._url("/active"), params=params)
        resp.raise_for_status()
        return resp.json()

    def overlap(self, params: dict) -> dict:
        resp = self._http.get(self._url("/overlap"), params=params)
        resp.raise_for_status()
        return resp.json()

    def subagent_start(self, body: dict) -> dict:
        resp = self._http.post(self._url("/subagent"), json=body)
        resp.raise_for_status()
        return resp.json()

    def subagent_end(self, body: dict) -> dict:
        resp = self._http.post(self._url("/subagent/end"), json=body)
        resp.raise_for_status()
        return resp.json()
