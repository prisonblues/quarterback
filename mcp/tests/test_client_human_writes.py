"""The human-gated write path — the browser vhost, the cookie, and every refusal.

``app.auth.human`` accepts a person proved at the edge, never a bearer token, and
``app/auth.py`` says the two vhosts differ in the way that matters: *"the browser
vhost has no token and the agent vhost strips ``X-Edge-Auth``"*. So a reorder is
not the same request to a different path — it is a different host, a different
credential, and a different failure mode when it is not set up.

What is worth pinning here is exactly the set a fake would get wrong: that the
bearer does NOT go to the browser vhost, that a forward-auth 302 is an error
rather than something to follow, and that "nobody is signed in" is distinguishable
from "the app said no" (#479).
"""

from __future__ import annotations

import httpx
import pytest
from mcp_server.client import QuarterbackClient

BASE = "https://board.example"
HUMAN = "https://human.example"
COOKIE = "authelia_session=abc123"


class Recorder:
    def __init__(self, response: httpx.Response | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response or httpx.Response(200, json={"reordered": 2})


def client(recorder: Recorder, *, human_url=HUMAN, cookie=COOKIE) -> QuarterbackClient:
    return QuarterbackClient(
        BASE, "tok-machine", transport=httpx.MockTransport(recorder),
        human_url=human_url, edge_cookie=cookie)


def test_a_reorder_goes_to_the_browser_vhost_and_not_the_agent_host():
    rec = Recorder()
    client(rec).plan_reorder({"repo": "acme/one", "order": ["a", "b"]})
    (req,) = rec.requests
    assert str(req.url) == f"{HUMAN}/plan/reorder"
    assert req.url.host == "human.example"


def test_the_machine_token_is_not_sent_to_the_browser_vhost():
    """The cookie authenticates this request; the bearer names this machine
    everywhere else. Sending it here hands a second vhost a credential it has no
    use for, and `human()` would not read it anyway."""
    rec = Recorder()
    client(rec).plan_reorder({"repo": "acme/one", "order": ["a"]})
    (req,) = rec.requests
    assert "authorization" not in {k.lower() for k in req.headers}
    assert req.headers["Cookie"] == COOKIE


def test_the_agent_host_still_gets_the_bearer_and_never_the_cookie():
    """The two paths must not bleed into each other — one client, two credentials."""
    rec = Recorder(httpx.Response(200, json={"ok": True}))
    c = client(rec)
    c.plan({"repo": "acme/one"})
    (req,) = rec.requests
    assert req.url.host == "board.example"
    assert req.headers["Authorization"] == "Bearer tok-machine"
    assert "cookie" not in {k.lower() for k in req.headers}


def test_no_cookie_refuses_before_the_network_and_names_the_remedy():
    rec = Recorder()
    with pytest.raises(RuntimeError) as e:
        client(rec, cookie=None).plan_reorder({"order": []})
    assert not rec.requests, "it must not spend a request to discover it has no session"
    assert "QUARTERBACK_EDGE_COOKIE" in str(e.value)
    assert HUMAN in str(e.value)


def test_no_human_url_says_it_cannot_be_derived():
    """The browser vhost is a second deployment fact, not a path on the first."""
    rec = Recorder()
    with pytest.raises(RuntimeError) as e:
        client(rec, human_url=None).plan_reorder({"order": []})
    assert not rec.requests
    assert "QUARTERBACK_HUMAN_URL" in str(e.value)


def test_a_forward_auth_bounce_is_an_error_and_is_never_followed():
    """The one way this can look like success while authenticating nobody: chase
    the 302 and Authelia's login page answers 200 with HTML."""
    rec = Recorder(httpx.Response(302, headers={"Location": f"{HUMAN}/login"}))
    with pytest.raises(RuntimeError) as e:
        client(rec).plan_reorder({"order": []})
    assert len(rec.requests) == 1, "the redirect must not be followed"
    assert "302" in str(e.value)
    assert "expired" in str(e.value)


def test_a_real_refusal_stays_an_http_error():
    """"You may not" and "you are nobody" are different answers and callers branch
    on them: only the second is a setup problem."""
    rec = Recorder(httpx.Response(403, json={"detail": "human required"}))
    with pytest.raises(httpx.HTTPStatusError):
        client(rec).plan_reorder({"order": []})


def test_item_update_uses_the_same_gate():
    rec = Recorder()
    client(rec).plan_item_update({"item_id": "x", "note": "corrected"})
    (req,) = rec.requests
    assert str(req.url) == f"{HUMAN}/plan/item/update"
    assert req.headers["Cookie"] == COOKIE


def test_closing_closes_the_human_client_only_if_one_was_opened():
    rec = Recorder()
    c = client(rec)
    c.close()          # never used the human path — must not blow up
    c2 = client(Recorder())
    c2.plan_reorder({"order": []})
    c2.close()
