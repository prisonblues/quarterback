"""What this server believes about WHEN it is (#146, #263).

An MCP server is spawned once and its environment freezes there. `/clear` gives
the terminal a new conversation with a new session id, and this process is not
respawned — so from that moment it holds two beliefs that have quietly stopped
being true:

* the key it is known by, which the hook derived the same way and immediately
  stopped agreeing with, giving one agent two names, two leases and two
  inboxes (#146);
* the session it stamps on every post and every claim, which named a context
  that no longer existed — and no ending could reach those claims, because
  `/session/end` releases by session key and the key was wrong (#263).

The fix is one file per pane, written by `qb-hook` (the half that sees the reset)
and read here. These tests are about the reading.

Skipped without the `server` extra: importing the server imports the MCP SDK, and
a tail-only install deliberately has none (see `test_package_contract.py`).

Run: uv run --extra dev --extra server pytest mcp/tests/test_server_identity.py
"""

from __future__ import annotations

import pytest
from mcp_server.client import QuarterbackClient

pytest.importorskip("mcp", reason="the MCP SDK is only in the `server` extra")

from mcp_server import server  # noqa: E402

PANE_ENV = {"CLAUDE_CODE_MESSAGING_SOCKET": "/run/user/1000/cc-socks/4242.sock"}
PANE = "cli-4242"
SPAWNED_WITH = "aaaaaaaa-1111-4111-8111-111111111111"
CLEARED_TO = "bbbbbbbb-2222-4222-8222-222222222222"


@pytest.fixture
def paned(tmp_path, monkeypatch):
    """A server process in a Claude Code pane, with a runtime dir of its own."""
    for name in ("QUARTERBACK_INSTANCE", "CLAUDE_CODE_CHILD_SESSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SPAWNED_WITH)
    for key, value in PANE_ENV.items():
        monkeypatch.setenv(key, value)
    return tmp_path


# -- the pane -----------------------------------------------------------


def test_the_pane_is_the_cli_process_that_a_clear_does_not_restart(paned):
    assert server.resolve_pane() == PANE


def test_a_named_pane_wins_and_is_left_exactly_as_it_was(paned, monkeypatch):
    """`qb-seats` sets QUARTERBACK_INSTANCE on every seat, which is why a seat
    never saw #146 — and pinning identity to the seat is the behaviour this
    change exists to preserve, not to replace."""
    monkeypatch.setenv("QUARTERBACK_INSTANCE", "seat-quarterback-4")
    assert server.resolve_pane() == "seat-quarterback-4"


def test_a_child_session_has_no_pane_of_its_own(paned, monkeypatch):
    """A Task sub-agent is its own conversation sharing its parent's CLI process.
    Joining the parent's pane would make it read the parent's conversation and
    post as its parent."""
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    assert server.resolve_pane() is None


def test_a_runtime_with_no_pane_signal_has_no_pane(paned, monkeypatch):
    """Codex, a bare stdio launch, an older Claude Code. There is nothing to join
    and the environment is all there is — which is what shipped."""
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET")
    assert server.resolve_pane() is None


# -- the conversation ---------------------------------------------------


def test_the_session_follows_the_pane_rather_than_this_process(paned):
    """#263 in one assertion. The conversation moved; this process did not."""
    assert server.resolve_session() == SPAWNED_WITH

    (paned / f"qb-conv-{PANE}").write_text(CLEARED_TO)
    assert server.resolve_session() == CLEARED_TO


def test_the_environment_is_still_the_fallback(paned):
    """No hook, no runtime dir, no pointer: behave as before rather than lose the
    stamp altogether. A post with session=null cannot be grouped under its author
    and `peers` cannot offer a `last_post_id` to reply onto."""
    assert not (paned / f"qb-conv-{PANE}").exists()
    assert server.resolve_session() == SPAWNED_WITH


def test_an_empty_pointer_is_not_an_answer(paned):
    """A truncated write — the hook is fail-open and never checks — must not
    blank the session on every call that follows it."""
    (paned / f"qb-conv-{PANE}").write_text("  \n")
    assert server.resolve_session() == SPAWNED_WITH


def test_a_child_session_stamps_its_own_conversation(paned, monkeypatch):
    """It shares the pane's directory and must not read the pane's pointer."""
    (paned / f"qb-conv-{PANE}").write_text(CLEARED_TO)
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cccccccc-3333-4333-8333-333333333333")
    assert server.resolve_session() == "cccccccc-3333-4333-8333-333333333333"


# -- the key this server publishes for the hook to adopt ----------------


def test_the_key_is_written_where_qb_hook_looks_for_it(paned):
    server.publish_pane_key("aaaaaaaa")
    assert (paned / f"qb-pane-{PANE}").read_text() == "aaaaaaaa"


def test_a_named_pane_needs_no_pointer(paned, monkeypatch):
    """Both halves already read the name out of the environment."""
    monkeypatch.setenv("QUARTERBACK_INSTANCE", "seat-quarterback-4")
    server.publish_pane_key("seat-quarterback-4")
    assert not (paned / "qb-pane-seat-quarterback-4").exists()


def test_an_unwritable_runtime_dir_is_not_an_error(paned, monkeypatch):
    """Best-effort by contract: the hook stays on its old fallback rather than
    this process failing to start."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(paned / "nope"))
    server.publish_pane_key("aaaaaaaa")  # must not raise


# -- the client asks afresh ---------------------------------------------


def test_the_client_stamps_the_conversation_making_the_call():
    """The client is constructed once and outlives the conversation it was
    constructed for. A session captured then is a value that silently stops being
    true, and every claim stamped after that names a context that is gone."""
    current = ["first"]
    client = QuarterbackClient("http://board.test", "tok", session=lambda: current[0])
    assert client._session == "first"
    current[0] = "second"
    assert client._session == "second"


def test_a_plain_string_session_still_works():
    """Callers with nothing to re-read are unaffected."""
    client = QuarterbackClient("http://board.test", "tok", session="fixed")
    assert client._session == "fixed"
