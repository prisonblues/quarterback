"""Argument handling, the tokenless path, and the refusal that guards a resume."""

from __future__ import annotations

import io

import httpx
import pytest
from mcp_server.board.__main__ import (
    _client,
    _project_dir,
    _report_health,
    _strip_verb,
    build_parser,
    do_resume,
    resolve_recipient,
)


class Cfg:
    base_url = "https://board.example"
    token = "tok"
    config_path = "/home/rich/.config/quarterback/config"


def parse(*argv):
    return build_parser().parse_args(_strip_verb(list(argv)))


def test_a_leading_board_verb_is_accepted_and_dropped():
    """All `qb` needs is `board) exec qb-board "$@"` — in either repo."""
    assert parse("board", "--follow").follow is True
    assert parse("--follow").follow is True
    # ...and only a *leading* one, so `--to board` still means what it says.
    assert parse("--to", "board").to == "board"


def test_defaults_match_the_documented_behaviour():
    args = parse()
    assert args.follow is False
    assert args.lines == 20
    assert args.presence is False  # hidden by default, as GET /board already decided
    assert args.colour is None  # decided by whether stdout is a tty
    assert args.repo == "."


def test_types_accumulate():
    assert parse("-t", "ask", "-t", "finding").types == ["ask", "finding"]


def test_colour_flags_are_mutually_exclusive():
    assert parse("--no-color").colour is False
    assert parse("--color").colour is True
    with pytest.raises(SystemExit):
        parse("--color", "--no-color")


def test_project_dir_matches_the_substitution_claude_code_makes():
    # Getting this wrong writes the transcript somewhere --resume never looks,
    # and it fails silently.
    assert _project_dir("/home/rich/source/quarterback") == "-home-rich-source-quarterback"
    assert _project_dir("/home/rich/a.b/c") == "-home-rich-a-b-c"


# -- the tokenless path ------------------------------------------------


class HealthyClient:
    def health(self):
        return {"status": "ok"}


class DownClient:
    def health(self):
        raise httpx.ConnectError("no route to host")


def test_with_no_token_a_healthy_board_is_reported_as_up():
    err = io.StringIO()
    assert _report_health(HealthyClient(), Cfg(), err) == 1
    assert "is up" in err.getvalue() and "no token" in err.getvalue()


def test_with_no_token_a_dead_board_is_reported_as_down():
    err = io.StringIO()
    assert _report_health(DownClient(), Cfg(), err) == 1
    assert "DOWN" in err.getvalue()


# -- resume ------------------------------------------------------------


class SessionClient:
    def __init__(self, state):
        self._state = state

    def session_state(self, session):
        return self._state

    def get_blob(self, sha):  # pragma: no cover - not reached in these tests
        return b""


def test_resuming_a_session_another_device_still_holds_is_refused():
    """Two machines resuming one session both write transcripts; the second wins."""
    err = io.StringIO()
    client = SessionClient(
        {"latest_blob": "abc", "cwd": "/tmp", "active_lease": {"holder": "atlas/x"}}
    )
    assert do_resume(client, "s1", err) == 1
    assert "atlas/x" in err.getvalue() and "refusing" in err.getvalue()


def test_a_session_with_no_transcript_cannot_be_resumed():
    err = io.StringIO()
    assert do_resume(SessionClient({"latest_blob": None, "cwd": "/tmp"}), "s1", err) == 1
    assert "no transcript" in err.getvalue()


def test_a_session_with_no_recorded_cwd_says_what_to_run_by_hand():
    err = io.StringIO()
    assert do_resume(SessionClient({"latest_blob": "abc", "cwd": None}), "s1", err) == 1
    assert "claude --resume s1" in err.getvalue()


def test_an_unknown_session_is_reported_rather_than_raised():
    class Broken:
        def session_state(self, session):
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", "https://b/x"), response=httpx.Response(404)
            )

    err = io.StringIO()
    assert do_resume(Broken(), "s1", err) == 1
    assert "could not read session" in err.getvalue()


def test_a_checkout_missing_on_this_machine_is_named_rather_than_execd(tmp_path, monkeypatch):
    class Blobby(SessionClient):
        def get_blob(self, sha):
            return b'{"ok": true}\n'

    # HOME redirected: the transcript really is written, and it must land in the
    # test's directory rather than in the developer's ~/.claude/projects.
    monkeypatch.setenv("HOME", str(tmp_path))
    err = io.StringIO()
    cwd = tmp_path / "absent"
    client = Blobby({"latest_blob": "abc", "cwd": str(cwd), "active_lease": None})
    assert do_resume(client, "s1", err) == 1
    assert "not on this machine" in err.getvalue()
    # The pull happened first and is reported: the transcript is here even though
    # the checkout is not, which is what the "clone it, then --resume" line means.
    written = tmp_path / ".claude" / "projects" / _project_dir(str(cwd)) / "s1.jsonl"
    assert written.read_bytes() == b'{"ok": true}\n'


# -- identity ----------------------------------------------------------


def headers(client):
    return dict(client._http.headers)


def test_by_default_the_client_sends_no_agent_key():
    """No key ⇒ the bare machine name, which is also the broadcast address.

    Sending one would have the board designate a fresh two-word name per launch —
    a name that never finishes and so is never recycled — and make `?to=@me` the
    inbox of an identity a second old, rather than this machine's whole mail.
    """
    h = headers(_client(Cfg(), {}))
    assert "x-agent-key" not in h and "x-agent-name" not in h
    assert h["authorization"] == "Bearer tok"


def test_quarterback_instance_gives_the_client_a_stable_identity():
    h = headers(_client(Cfg(), {"QUARTERBACK_INSTANCE": "deploy"}))
    assert h["x-agent-key"] == "deploy" and h["x-agent-name"] == "deploy"


def test_an_instance_label_the_board_would_refuse_as_a_name_is_still_sent_as_a_key():
    """Better than 400ing the session's first request over a capital letter."""
    h = headers(_client(Cfg(), {"QUARTERBACK_INSTANCE": "Rich_Laptop"}))
    assert h["x-agent-key"] == "Rich_Laptop"
    assert "x-agent-name" not in h


def test_a_tokenless_client_sends_no_authorization_header():
    class NoToken(Cfg):
        token = None

    assert "authorization" not in headers(_client(NoToken(), {}))


# -- @me ---------------------------------------------------------------


class WhoClient:
    def __init__(self, agent=None, boom=False):
        self._agent, self._boom = agent, boom

    def whoami(self):
        if self._boom:
            raise httpx.ConnectError("no route")
        return {"agent": self._agent}


def test_at_me_is_resolved_once_so_both_halves_of_the_tail_agree():
    """`/stream` takes no recipient filter, so the live half has nothing to
    compare a server-side spelling against."""
    assert resolve_recipient(WhoClient("zeus/fern-nectar"), "@me", io.StringIO()) == (
        "zeus/fern-nectar"
    )


def test_an_ordinary_recipient_is_passed_through_untouched():
    assert resolve_recipient(WhoClient(), "zeus", io.StringIO()) == "zeus"
    assert resolve_recipient(WhoClient(), None, io.StringIO()) is None


def test_an_unresolvable_at_me_shows_everything_rather_than_nothing():
    err = io.StringIO()
    assert resolve_recipient(WhoClient(boom=True), "@me", err) is None
    assert "could not resolve @me" in err.getvalue()
