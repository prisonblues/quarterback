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
import subprocess
import threading
from collections.abc import Iterable, Iterator

import httpx

#: The header a delegated agent presents (#478). Must match
#: ``app.auth.ELEVATED_HEADER``; the two live in different repos' worth of
#: distance, so the name is stated once on each side and the tests pin it.
ELEVATED_HEADER = "X-Agent-Elevated"


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
        elevated: str | None = None,
        elevated_cmd: str | None = None,
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
        # This machine's DELEGATED credential (#478), for the narrow set of writes
        # `app.auth.delegated` names. It goes to the ordinary agent host beside the
        # bearer — it is a client-supplied credential like the bearer, not an
        # edge-injected proof like `Remote-User`, so the edge neither supplies it
        # nor strips it and no vhost change is involved.
        #
        # Resolved LAZILY and never here: the command is usually `op read`, which
        # can prompt, and this client is constructed once per MCP session on every
        # session start — to serve two tools a session will probably never call.
        self._elevated = elevated or None
        self._elevated_cmd = elevated_cmd or None
        # Serialises credential RESOLUTION only — never a request. Two delegated
        # calls that both need a fetch would otherwise run the command twice, and
        # the command is `op read`, which can prompt. See `_resolve_elevated`.
        self._elevated_lock = threading.Lock()
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

    # -------------------------------------------------------------- delegated

    #: What to tell a caller with no credential. Long because it is the whole
    #: remedy: this fails on every unprovisioned box, and a bare 403 would send
    #: somebody to the board's auth code instead of to their own config.
    NO_CREDENTIAL = (
        "this call needs a delegated credential and this host has none. Set "
        "QUARTERBACK_ELEVATED_TOKEN, or QUARTERBACK_ELEVATED_TOKEN_CMD to a "
        "command that prints it (the fleet resolves it from 1Password, per "
        "machine). It is not the board bearer and not a person's session."
    )

    def _resolve_elevated(self, *, stale: str | None = None) -> str | None:
        """This machine's delegated secret — the cached one, or a freshly fetched one.

        ``stale`` is the value the caller just had refused, and passing it asks for
        a secret **different from that one**. That is a compare-and-swap on the
        cache rather than an unconditional re-fetch, and it is what makes a
        concurrent retry cheap: if another call has already rotated the cache, this
        one gets the new value without running the command a second time.

        Same *reasoning* as ``QUARTERBACK_TOKEN_REFRESH_CMD`` — "the cached copy is
        exactly what is stale" — but not the same mechanism, and there is no
        ``_REFRESH_CMD`` of its own: this re-runs the ONE command it has, which is
        enough because `op read` goes to the store every time.

        **Everything that touches ``self._elevated`` happens under the lock, and
        nothing else does.** The lock is held across the subprocess, deliberately:
        the cost of serialising is one caller waiting, and the cost of not doing so
        is two concurrent `op read` invocations — which on a box using the 1Password
        desktop integration is two authorisation prompts for one logical fetch.
        """
        with self._elevated_lock:
            current = self._elevated
            # Warm, or somebody else already replaced the value this caller was
            # refused. Either way there is nothing to fetch.
            if current and current != stale:
                return current
            if not self._elevated_cmd:
                # Nothing to re-derive from, so an operator-configured literal is
                # never discarded: dropping it would leave this client permanently
                # credential-less until the process restarts, turning one bad
                # request into every later one.
                return current
            try:
                done = subprocess.run(self._elevated_cmd, shell=True, check=False,
                                      capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
                # UnicodeDecodeError because `text=True` decodes the command's
                # stdout: a secret store that emits a stray byte would otherwise
                # raise out of a credential lookup rather than reporting "no
                # credential".
                done = None
            # The EXIT CODE decides, not the presence of output. `op` prints
            # diagnostics to stdout on some failures, so a non-zero run that wrote
            # something would otherwise be adopted as a credential — and the
            # symptom would be a 403 nobody could explain, from a value that never
            # was one.
            value = ""
            if done is not None and done.returncode == 0:
                # First line only, exactly as `qb_resolve_token` trims: a store
                # that prints a warning after the value must not put it in a
                # header.
                value = done.stdout.split("\n", 1)[0].strip()
            if value:
                self._elevated = value
            elif stale is not None:
                # The fetch produced nothing AND the cached value has already been
                # refused once. Keeping it would let the next call sail past the
                # "have I got one" check and replay a rejected secret; dropping it
                # turns that into the actionable "no credential" refusal instead of
                # a second 403.
                self._elevated = None
            return self._elevated

    def _delegated_post(self, path: str, body: dict) -> dict:
        """POST to the agent host, carrying this machine's delegated credential.

        The same host and the same bearer as every other call — only one extra
        header. A missing credential is refused BEFORE the request, because that
        is one setup step rather than an answer about what was asked.
        """
        secret = self._resolve_elevated()
        if not secret:
            raise RuntimeError(self.NO_CREDENTIAL)
        # Held in a LOCAL for the rest of this call. Reading `self._elevated` again
        # at send time is the race this method used to have: a concurrent call
        # clearing the cache between resolve and send left this request going out
        # with an empty header — a 403 that looks like a wrong secret and is
        # actually a missing one.
        resp = self._send_delegated(path, body, secret)
        # One retry, and only for the 403 that is actually about the credential.
        # A 403 here can equally be the board refusing the ACT — dropping an item,
        # writing an exemption marker — and re-reading 1Password to ask again is
        # both useless and misleading: it turns a clear "you may not" into two
        # identical refusals with a secret-store round trip between them. The
        # board names the header in the credential case (see `delegated()`), so
        # that is what to match on.
        if (resp.status_code == 403 and self._elevated_cmd
                and ELEVATED_HEADER in resp.text):
            fresh = self._resolve_elevated(stale=secret)
            # Only when it is actually different. `_resolve_elevated` returns the
            # same value when there was nothing fresher, and replaying it would
            # spend a second request to be refused identically.
            if fresh and fresh != secret:
                resp = self._send_delegated(path, body, fresh)
        resp.raise_for_status()
        return resp.json()

    def _send_delegated(self, path: str, body: dict,
                        secret: str) -> httpx.Response:
        """One POST with the secret it is GIVEN — never one it reads off `self`.

        The parameter is the fix for #498's first item rather than a tidy-up: a
        request must carry the credential its caller resolved, not whatever the
        shared cache happens to hold by the time the header is built.
        """
        return self._http.post(self._url(path), json=body,
                               headers={ELEVATED_HEADER: secret})

    def plan_reorder(self, body: dict) -> dict:
        """``POST /plan/reorder`` — put an order into force. Delegated (#478)."""
        return self._delegated_post("/plan/reorder", body)

    def plan_item_update(self, body: dict) -> dict:
        """``POST /plan/item/update`` — retitle, move, re-reason, drop. Delegated."""
        return self._delegated_post("/plan/item/update", body)

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

    def end_session(self, session: str, reason: str) -> dict:
        """End a session: release its lease and its claims, saying why (#277)."""
        resp = self._http.post(self._url("/session/end"),
                               json={"session": session, "reason": reason})
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

    # -- the landing graph (#294) ---------------------------------------

    def landing(self, params: dict) -> dict:
        resp = self._http.get(self._url("/landing"),
                              params={k: v for k, v in params.items() if v is not None})
        resp.raise_for_status()
        return resp.json()

    def landing_write(self, path: str, body: dict) -> dict:
        """gate / clear / mind / unmind.

        The session is stamped on ``mind`` only, and that is the whole expiry
        design rather than a detail: a watch lives while its holder's presence
        does, so a watch that named no session has nothing but its TTL to lean
        on. ``unmind`` is a machine-level act (you may stand down a watch your
        own box set) and takes none — an argument an endpoint ignores is an
        argument that will one day mean something else.
        """
        if path == "mind" and self._session and not body.get("session"):
            body = {**body, "session": self._session}
        resp = self._http.post(self._url(f"/landing/{path}"), json=body)
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

    def blockers(self, params: dict) -> dict:
        """``GET /blockers`` — the queue of questions a human owes an answer to."""
        resp = self._http.get(self._url("/blockers"), params=params)
        resp.raise_for_status()
        return resp.json()

    def blocker_write(self, path: str, body: dict) -> dict:
        """``POST /blockers`` and ``/blockers/resolve`` (#328).

        Ordinary agent auth. Resolving is NOT a second credential: the endpoint
        takes `author` and the caller's identity decides whether the call was an
        answer or a withdrawal, so an agent needs nothing extra to take back its
        own question and cannot get anything extra by asking.
        """
        resp = self._http.post(self._url(f"/blockers{path}"), json=body)
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
