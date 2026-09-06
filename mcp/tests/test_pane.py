"""The identity this server reports, after the conversation has moved (#146, #263).

`/clear` does not restart the Claude Code CLI. It gives the same process a new
conversation, and Claude Code injects the current conversation's id into every
process it spawns from then on — so `qb-hook`, a fresh process per event, is
always right, and this server, spawned once and never again, was the one thing
that was not. Measured on this fleet 2026-09-06: `whoami` in a live session
returned key `4c8f6a8a`, a conversation whose transcript had stopped 22 hours
earlier, while the conversation actually running was `e267ef87`. Five board
claims taken from that session that day carried the dead id.

Both halves of the damage are here:

* **#146** — the hook moved to the new conversation and this did not, so one
  agent had two board identities, posting under two names and polling two
  inboxes.
* **#263** — `POST /session/end` hands back the claims stamped with the session
  it names. Claims taken through this server were stamped with the conversation
  it was launched with, so the release reached the first reset in a terminal and
  no later one.

The fix is that this server learns the current conversation from the pane file
`qb-hook` writes, per call, instead of trusting the environment it was born with.
`harness/tests/test_pane_parity.py` is what holds the two halves to one spelling
of where that file is; this suite is about what this half does with it.

Run: uv run --extra dev --extra server pytest mcp/tests/test_pane.py
"""

from __future__ import annotations

import os

import httpx
import pytest
from mcp_server import pane
from mcp_server.client import QuarterbackClient

#: A conversation, and the one it becomes after a `/clear`.
BEFORE = "4c8f6a8a-4dea-4bd9-9f1e-2a1f0b6d4c11"
AFTER = "e267ef87-b7d5-40f9-a528-a02de04c2ca9"


@pytest.fixture
def paned(tmp_path, monkeypatch):
    """A pane this process genuinely owns, with the socket to prove it.

    The socket is named for THIS pid, and this pid is trivially an ancestor of
    itself — which is the real relationship a CLI has to the hook and the MCP
    server it spawns, arranged here without a fake `/proc`.
    """
    socks = tmp_path / "cc-socks"
    socks.mkdir()
    sock = socks / f"{os.getpid()}.sock"
    sock.write_bytes(b"")
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", str(sock))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    monkeypatch.delenv("QUARTERBACK_INSTANCE", raising=False)

    class Pane:
        path = pane.pane_file()

        @staticmethod
        def holds(session: str) -> None:
            """What `qb-hook` writes on SessionStart."""
            with open(pane.pane_file(), "w", encoding="utf-8") as fh:
                fh.write(session)

    assert Pane.path, "this process should own a pane it named after itself"
    return Pane


@pytest.fixture
def unpaned(tmp_path, monkeypatch):
    """No pane at all: not Claude Code, or a socket we do not own."""
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("QUARTERBACK_INSTANCE", raising=False)


# ------------------------------------------------------------- reading a pane


def test_the_pane_file_is_what_the_session_is(paned, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", BEFORE)
    paned.holds(AFTER)
    assert pane.pane_session() == AFTER


def test_a_pane_with_no_file_yet_is_not_an_answer(paned, monkeypatch):
    """A host with no hook installed, or a session whose first SessionStart has
    not landed. `None` sends the caller to its own environment, which is exactly
    what it read before this existed — so the worst this can do is nothing."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", BEFORE)
    assert pane.pane_session() is None


def test_an_empty_or_blank_pane_file_is_not_an_answer(paned):
    for blank in ("", "  \n"):
        paned.holds(blank)
        assert pane.pane_session() is None


def test_the_conversation_is_stripped_of_the_newline_a_writer_may_add(paned):
    paned.holds(f"{AFTER}\n")
    assert pane.pane_session() == AFTER


# --------------------------------------------------- what the server reports


@pytest.fixture
def srv():
    return pytest.importorskip(
        "mcp_server.server", reason="the MCP SDK is only in the `server` extra")


def test_a_clear_no_longer_leaves_two_board_identities(srv, paned, monkeypatch):
    """#146, end to end on this side. The environment stays at the conversation
    this process was spawned with — it is frozen and there is nothing to unfreeze
    — and the answer moves anyway, because the pane moved."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", BEFORE)
    paned.holds(BEFORE)
    assert srv.resolve_session() == BEFORE
    assert srv.resolve_key() == BEFORE[:8]

    paned.holds(AFTER)          # `/clear`: the hook writes the new conversation
    assert srv.resolve_session() == AFTER
    assert srv.resolve_key() == AFTER[:8]


def test_without_a_pane_the_answer_is_this_process_own_environment(srv, unpaned,
                                                                   monkeypatch):
    """Not a degraded answer — the same answer, for as long as it is still true.
    A host with no hook, a runtime that is not Claude Code, or a session that has
    never been reset all read the environment and are right to."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", BEFORE)
    assert srv.resolve_session() == BEFORE
    assert srv.resolve_key() == BEFORE[:8]


def test_an_explicit_instance_still_wins_over_everything(srv, paned, monkeypatch):
    """`QUARTERBACK_INSTANCE` is an operator pinning an identity across restarts
    and resets, which is a thing this must not quietly undo."""
    monkeypatch.setenv("QUARTERBACK_INSTANCE", "seat-quarterback-4")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", BEFORE)
    paned.holds(AFTER)
    assert srv.resolve_key() == "seat-quarterback-4"
    # …and the SESSION still moves, which is the half that releases claims.
    assert srv.resolve_session() == AFTER


def test_a_runtime_with_no_conversation_at_all_keeps_one_nonce(srv, unpaned,
                                                               monkeypatch):
    """codex, or anything else with an MCP server and no Claude Code. One stdio
    process genuinely is one agent there, so the nonce is correct rather than a
    fallback — and it must not change between two calls, because a key that moves
    with nothing behind it is two agents."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    first = srv.resolve_key()
    assert first.startswith("p")
    assert srv.resolve_key() == first


# ------------------------------------------------ what actually goes on the wire


def _client(**kwargs) -> tuple[QuarterbackClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = QuarterbackClient("http://board.test", "tok",
                               transport=httpx.MockTransport(handler), **kwargs)
    return client, seen


def test_a_key_that_moves_is_re_read_on_every_request():
    """The client is built ONCE per MCP session — i.e. once per CLI process — so
    a key captured at construction is captured for the life of a process that
    outlives its own answer. It is a callable now, and the header is stamped per
    request rather than fixed in the client's defaults."""
    key = [BEFORE[:8]]
    client, seen = _client(key=lambda: key[0])
    client.whoami()
    key[0] = AFTER[:8]
    client.whoami()
    assert [r.headers["X-Agent-Key"] for r in seen] == [BEFORE[:8], AFTER[:8]]


def test_a_session_that_moves_is_stamped_on_the_write_that_follows_it():
    """The claim #263 could not release. A claim stamped with a conversation that
    ended hours ago is not handed back by `POST /session/end`, because that call
    names the session it is ending and the stamp does not match it."""
    session = [BEFORE]
    client, seen = _client(session=lambda: session[0])
    client.post({"type": "status", "summary": "one"})
    session[0] = AFTER
    client.post({"type": "status", "summary": "two"})
    import json
    assert [json.loads(r.content)["session"] for r in seen] == [BEFORE, AFTER]


def test_a_plain_string_key_is_still_a_key():
    """Every other caller — the board TUI, a test, a runtime with one
    conversation per process — passes a value and must keep working."""
    client, seen = _client(key="opaque-handle", session="s-1")
    client.whoami()
    assert seen[0].headers["X-Agent-Key"] == "opaque-handle"


def test_no_key_at_all_sends_no_header():
    """The bare machine name, which is also the broadcast address. A stray empty
    header would have the board designate a fresh name per launch."""
    client, seen = _client()
    client.whoami()
    assert "X-Agent-Key" not in seen[0].headers
    client_none, seen_none = _client(key=lambda: None)
    client_none.whoami()
    assert "X-Agent-Key" not in seen_none[0].headers
