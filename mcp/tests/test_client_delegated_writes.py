"""The delegated write path — one extra header, the same host, the same bearer.

`app.auth.delegated` authorises a NAMED set of writes for an agent holding its
machine's own credential. It is not a way to be a person, and the shape of the
request is what makes that true: this goes to the ordinary agent host with the
ordinary bearer, carrying `X-Agent-Elevated` beside it. Nothing here forges
`Remote-User`, nothing asks the edge for anything, and no vhost changes.

Pinned here: the header goes out only on the two delegated calls; a rotated
secret is re-read once on a 403; and "this host has no credential" is separated
from "the board said no", because only the first is a setup step.
"""

from __future__ import annotations

import os

import httpx
import pytest
from mcp_server.client import ELEVATED_HEADER, QuarterbackClient

BASE = "https://board.example"
# Deliberately low-entropy and obviously not a credential: gitleaks runs on this
# repo's pre-commit hook and a realistic-looking fixture trips it on every commit.
SECRET = "test-not-a-real-secret"


class Recorder:
    def __init__(self, response: httpx.Response | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response or httpx.Response(200, json={"reordered": 2})


def client(rec: Recorder, *, secret=SECRET, cmd=None) -> QuarterbackClient:
    return QuarterbackClient(BASE, "tok-machine", transport=httpx.MockTransport(rec),
                             elevated=secret, elevated_cmd=cmd)


def test_a_reorder_goes_to_the_ordinary_agent_host():
    """Not a second vhost. The credential is client-supplied like the bearer, so
    the edge neither injects nor strips it and `qb.fo.ls` is the right address."""
    rec = Recorder()
    client(rec).plan_reorder({"repo": "acme/one", "order": ["a", "b"]})
    (req,) = rec.requests
    assert str(req.url) == f"{BASE}/plan/reorder"


def test_it_carries_both_the_bearer_and_the_delegated_secret():
    """The bearer says which machine; the secret says that machine may do this.
    `delegated()` looks the secret up BY the machine, so dropping either half is
    not a weaker request, it is a different one that cannot be authorised."""
    rec = Recorder()
    client(rec).plan_reorder({"order": ["a"]})
    (req,) = rec.requests
    assert req.headers["Authorization"] == "Bearer tok-machine"
    assert req.headers[ELEVATED_HEADER] == SECRET


def test_ordinary_calls_do_not_carry_the_delegated_secret():
    """A credential sent on every request is a credential in every log and every
    proxy. It rides only the two calls that need it."""
    rec = Recorder(httpx.Response(200, json={"ok": True}))
    client(rec).plan({"repo": "acme/one"})
    (req,) = rec.requests
    assert ELEVATED_HEADER not in req.headers
    assert req.headers["Authorization"] == "Bearer tok-machine"


def test_no_credential_refuses_before_the_network_and_names_the_remedy():
    rec = Recorder()
    with pytest.raises(RuntimeError) as e:
        client(rec, secret=None).plan_reorder({"order": []})
    assert not rec.requests, "it must not spend a request to discover it has none"
    assert "QUARTERBACK_ELEVATED_TOKEN" in str(e.value)


def test_a_board_refusal_stays_an_http_error():
    """"Your secret is wrong" and "this host has none" are different answers and
    only one of them is a setup step."""
    rec = Recorder(httpx.Response(403, json={"detail": "does not match"}))
    with pytest.raises(httpx.HTTPStatusError):
        client(rec).plan_reorder({"order": []})


def test_item_update_uses_the_same_credential():
    rec = Recorder()
    client(rec).plan_item_update({"item_id": "x", "note": "corrected"})
    (req,) = rec.requests
    assert str(req.url) == f"{BASE}/plan/item/update"
    assert req.headers[ELEVATED_HEADER] == SECRET


# --------------------------------------------------------- the secret's own life

def test_the_command_is_not_run_until_a_delegated_write_is_attempted():
    """This client is constructed once per MCP session on EVERY session start and
    the command is usually `op read`, which can prompt. Resolving eagerly would
    put a credential prompt in front of every agent that starts, to serve two
    tools it will probably never call."""
    marker = "/tmp/qb-elev-ran-never"
    if os.path.exists(marker):
        os.remove(marker)
    QuarterbackClient(BASE, "t", elevated_cmd=f"touch {marker}; echo s").close()
    assert not os.path.exists(marker), "constructing the client must run nothing"


def test_the_command_supplies_the_secret():
    rec = Recorder()
    client(rec, secret=None, cmd="echo from-the-store").plan_reorder({"order": ["a"]})
    assert rec.requests[0].headers[ELEVATED_HEADER] == "from-the-store"


def test_only_the_first_line_of_the_command_is_used():
    """A store that prints a warning after the value must not put it in a header —
    `qb_resolve_token` trims the same way."""
    rec = Recorder()
    client(rec, secret=None,
           cmd="printf 'good\\nWARNING: rate limited\\n'").plan_reorder({"order": ["a"]})
    assert rec.requests[0].headers[ELEVATED_HEADER] == "good"


def test_a_command_that_fails_is_not_an_exception_it_is_no_credential():
    """`op` not installed, or not signed in. Same situation as having none, and it
    must arrive as the actionable refusal rather than an OSError from a client."""
    rec = Recorder()
    with pytest.raises(RuntimeError) as e:
        client(rec, secret=None, cmd="definitely-not-a-real-binary-xyz").plan_reorder(
            {"order": []})
    assert "QUARTERBACK_ELEVATED_TOKEN" in str(e.value)


def test_a_403_re_reads_the_command_and_retries_once():
    """A rotated secret's first symptom is a write that worked yesterday, and
    asking the store again is the whole remedy."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(ELEVATED_HEADER, ""))
        if len(seen) == 1:
            return httpx.Response(403, json={"detail": "does not match"})
        return httpx.Response(200, json={"reordered": 1})

    counter = "/tmp/qb-elev-n"
    if os.path.exists(counter):
        os.remove(counter)
    script = (f"n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); "
              f"echo $n > {counter}; echo v$n")
    c = QuarterbackClient(BASE, "t", transport=httpx.MockTransport(handler),
                          elevated_cmd=script)
    assert c.plan_reorder({"order": ["a"]}) == {"reordered": 1}
    assert seen == ["v1", "v2"], seen
    os.remove(counter)


def test_a_literal_secret_is_not_retried_because_there_is_nowhere_fresher():
    rec = Recorder(httpx.Response(403, json={"detail": "nope"}))
    with pytest.raises(httpx.HTTPStatusError):
        client(rec).plan_reorder({"order": []})
    assert len(rec.requests) == 1
