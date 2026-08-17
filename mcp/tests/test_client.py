"""The HTTP client, driven over a real transport.

``httpx.MockTransport`` rather than a hand-written fake: the parts worth testing
here are the ones a fake would have to reimplement to be wrong about — which
headers go out, how query parameters are encoded, when ``raise_for_status``
fires, and whether a stream is closed when the reader walks away.
"""

from __future__ import annotations

import json

import httpx
import pytest
from mcp_server.client import QuarterbackClient

BASE = "https://board.example"


class Recorder:
    """A transport handler that keeps the requests and answers from a script."""

    def __init__(self, response: httpx.Response | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response or httpx.Response(200, json={"ok": True})

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def make_client(handler: Recorder, token: str = "tok", **kw) -> QuarterbackClient:
    return QuarterbackClient(
        f"{BASE}/", token, transport=httpx.MockTransport(handler), **kw
    )


# -- identity ----------------------------------------------------------


def test_the_token_key_and_requested_name_all_travel_as_headers():
    handler = Recorder()
    make_client(handler, key="opaque-handle", requested_name="zeus/f5ca7491").whoami()
    assert handler.last.headers["authorization"] == "Bearer tok"
    assert handler.last.headers["x-agent-key"] == "opaque-handle"
    assert handler.last.headers["x-agent-name"] == "zeus/f5ca7491"


def test_no_token_sends_no_authorization_header_at_all():
    """Rather than a "Bearer " that authenticates nothing.

    That is the tokenless client the terminal board starts with on a host with no
    credential: everything else 401s, and health() still answers.
    """
    handler = Recorder()
    make_client(handler, token="").health()
    assert "authorization" not in handler.last.headers


def test_an_absent_key_and_name_are_omitted_rather_than_sent_empty():
    handler = Recorder()
    make_client(handler).whoami()
    assert "x-agent-key" not in handler.last.headers
    assert "x-agent-name" not in handler.last.headers


def test_a_session_is_stamped_on_every_post():
    """So that anything which posts is attributable without remembering to say so."""
    handler = Recorder()
    make_client(handler, session="abcd1234").post({"type": "status", "summary": "hi"})
    assert json.loads(handler.last.content)["session"] == "abcd1234"


def test_an_explicit_session_in_the_body_wins_over_the_clients_own():
    handler = Recorder()
    make_client(handler, session="abcd1234").post({"summary": "hi", "session": "other"})
    assert json.loads(handler.last.content)["session"] == "other"


# -- requests ----------------------------------------------------------


def test_the_base_url_keeps_no_trailing_slash():
    handler = Recorder()
    make_client(handler).health()
    assert str(handler.last.url) == f"{BASE}/health"


def test_sessions_sends_its_limit():
    handler = Recorder(httpx.Response(200, json=[]))
    make_client(handler).sessions(limit=7)
    assert handler.last.url.params["limit"] == "7"


def test_review_stats_drops_the_parameters_that_are_none():
    """An unset filter must be absent, not the string "None" for the board to parse."""
    handler = Recorder()
    make_client(handler).review_stats({"repo": "quarterback", "since": None, "days": 7})
    assert dict(handler.last.url.params) == {"repo": "quarterback", "days": "7"}


def test_board_passes_its_parameters_through():
    handler = Recorder(httpx.Response(200, json=[]))
    make_client(handler).board({"limit": 1, "window_min": 0, "include_presence": True})
    assert dict(handler.last.url.params) == {
        "limit": "1", "window_min": "0", "include_presence": "true",
    }


def test_health_asks_for_a_shorter_timeout_than_the_rest():
    """Up-or-down on an unreachable host should not hang for the default patience."""
    handler = Recorder()
    make_client(handler).health()
    assert handler.last.extensions["timeout"]["read"] == 10.0


# -- errors ------------------------------------------------------------


@pytest.mark.parametrize("code", [400, 401, 404, 422, 503])
def test_an_error_status_raises_rather_than_returning_a_body(code):
    handler = Recorder(httpx.Response(code, json={"detail": "no"}))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        make_client(handler).board({"limit": 1})
    assert caught.value.response.status_code == code


def test_an_error_status_on_the_stream_raises_before_any_frame_is_yielded():
    """The tail's whole retry policy is decided from this exception."""
    handler = Recorder(httpx.Response(404, text="nope"))
    with pytest.raises(httpx.HTTPStatusError):
        list(make_client(handler).stream(since=1))


# -- streaming ---------------------------------------------------------


def sse_body(*posts: str) -> bytes:
    return "".join(f"data: {p}\n\n" for p in posts).encode()


def test_stream_sends_its_cursor_and_decodes_the_frames():
    handler = Recorder(httpx.Response(200, content=sse_body('{"id": 5}', '{"id": 6}')))
    events = list(make_client(handler).stream(since=4))
    assert handler.last.url.params["since"] == "4"
    assert events == [{"id": 5}, {"id": 6}]


def test_stream_skips_keep_alive_comments():
    body = b': ping\n\ndata: {"id": 9}\n\n'
    handler = Recorder(httpx.Response(200, content=body))
    assert list(make_client(handler).stream()) == [{"id": 9}]


def test_the_read_timeout_is_a_liveness_check_not_a_patience_limit():
    """The board pings every few seconds, so a long silence means a dead socket."""
    handler = Recorder(httpx.Response(200, content=sse_body('{"id": 1}')))
    list(make_client(handler).stream(read_timeout=45.0))
    assert handler.last.extensions["timeout"] == {
        "connect": 10.0, "read": 45.0, "write": 45.0, "pool": 45.0,
    }


class ClosingStream(httpx.SyncByteStream):
    """A body that reports whether the response was closed after being read."""

    def __init__(self, content: bytes) -> None:
        self._content, self.closed = content, False

    def __iter__(self):
        yield self._content

    def close(self) -> None:
        self.closed = True


def test_a_reader_that_stops_early_still_closes_the_connection():
    """A tail abandoning a stream must not leak the socket it was reading."""
    body = ClosingStream(sse_body('{"id": 1}', '{"id": 2}'))
    handler = Recorder(httpx.Response(200, stream=body))
    events = make_client(handler).stream()
    assert next(events) == {"id": 1}
    events.close()
    assert body.closed


def test_two_clients_over_one_transport_keep_their_own_credentials():
    """A transport is injected, never a client, and this is why.

    The parameter used to take an httpx.Client and call `.headers.update()` on
    it — mutating an object the caller owns. Two QuarterbackClients built over
    one shared client therefore ended up with ONE Authorization header, the
    second overwriting the first, and the first went on making requests it
    believed were authenticated as itself. Silent, and wrong in the direction
    that matters: it authenticates as somebody else rather than failing.
    """
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"agent": "x"})

    shared = httpx.MockTransport(handler)
    first = QuarterbackClient(f"{BASE}/", "token-one", transport=shared)
    second = QuarterbackClient(f"{BASE}/", "token-two", transport=shared)

    first.whoami()
    second.whoami()
    first.whoami()

    assert seen == ["Bearer token-one", "Bearer token-two", "Bearer token-one"], (
        "constructing the second client changed what the first one sends"
    )
