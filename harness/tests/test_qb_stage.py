"""Tests for qb-stage, which says the workflow stage to the bar and to the board.

The field is cosmetic, which is exactly why the failure modes matter more than
the happy path: a status marker that can fail a review, refuse a legal token, or
write outside its directory has cost more than it was ever worth. So the happy
path gets one test and the ways it must NOT misbehave get the rest.

Since #262 there is a second consumer — the board, so the rest of the fleet can
see how far along a session is — and it doubles the ways this can misbehave
rather than adding one. A report that blocked, hung, or failed the caller would
break the local marker that has always worked, in exchange for a column. So the
board half is tested almost entirely through what it must NOT do: not delay, not
speak when there is nothing to say, and not turn an unconfigured or unreachable
board into anybody's problem.

Run: pytest harness/tests
"""

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

QB_STAGE = Path(__file__).resolve().parent.parent / "bin" / "qb-stage"

#: How long a backgrounded report is given to arrive before a test calls it lost.
#: Generous, because it is only ever waited out by a test that is already failing.
ARRIVAL_TIMEOUT = 10.0


class Board:
    """A stub board recording what `POST /lease/stage` was sent.

    ``delay`` is the fail-open case that matters most: a board that accepts the
    connection and then thinks about it for a minute cannot be distinguished from
    a working one by any timeout the caller might pick, so the caller must not be
    waiting on it at all.
    """

    def __init__(self, status=200, delay=0.0):
        self.requests = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if delay:
                    time.sleep(delay)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode() if length else ""
                with outer.lock:
                    outer.requests.append({
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "body": raw,
                    })
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):  # keep pytest output readable
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def wait(self, n=1, timeout=ARRIVAL_TIMEOUT):
        """The first ``n`` requests, or fail saying how many actually turned up."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.requests) >= n:
                    return list(self.requests)
            time.sleep(0.02)
        with self.lock:
            raise AssertionError(f"expected {n} report(s), got {len(self.requests)}")

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def board():
    made = []

    def make(**kw):
        b = Board(**kw)
        made.append(b)
        return b

    yield make
    for b in made:
        b.stop()


@pytest.fixture
def run(tmp_path):
    """Invoke qb-stage with an isolated marker dir and a fixed session id.

    **No board unless a test asks for one.** The inherited environment on a real
    machine names the real board, so a fixture that passed it through would have
    this suite posting `ABCDEF` to the fleet every time somebody ran it. Pointing
    `QUARTERBACK_CONFIG` at a path that does not exist is the unconfigured-host
    case, which is also the case most of these tests are about.
    """

    def _run(*args, session_id="11111111-2222-3333-4444-555555555555",
             board_url=None, token="tok-test", **kw):
        env = {**os.environ, "QB_SESSION_STAGE_DIR": str(tmp_path / "stage")}
        env.pop("QUARTERBACK_BASE_URL", None)
        env.pop("QUARTERBACK_TOKEN", None)
        env.pop("QUARTERBACK_TOKEN_CMD", None)
        env["QUARTERBACK_CONFIG"] = str(tmp_path / "no-such-config")
        if board_url:
            env["QUARTERBACK_BASE_URL"] = board_url
            env["QUARTERBACK_TOKEN"] = token
        if session_id is None:
            env.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            env["CLAUDE_CODE_SESSION_ID"] = session_id
        return subprocess.run(
            [str(QB_STAGE), *args], env=env, capture_output=True, text=True, **kw
        )

    _run.dir = tmp_path / "stage"
    _run.session = "11111111-2222-3333-4444-555555555555"
    return _run


def test_a_stage_is_recorded_under_the_session_id(run):
    assert run("R1F").returncode == 0
    marker = run.dir / "11111111-2222-3333-4444-555555555555"
    assert marker.read_text() == "R1F"


def test_the_marker_carries_no_trailing_newline(run):
    """The statusline renders the contents verbatim into a one-line bar."""
    run("F0")
    assert (run.dir / "11111111-2222-3333-4444-555555555555").read_text() == "F0"


def test_a_later_stage_replaces_the_earlier_one(run):
    """A session moves R1 -> R1F -> R2; the bar shows where it is, not a history."""
    for stage in ("R1", "R1F", "R2"):
        run(stage)
    assert run("--show").stdout == "R2"


def test_clear_removes_the_marker(run):
    run("R2F")
    assert run("--clear").returncode == 0
    assert run("--show").stdout == ""


def test_clearing_a_stage_that_was_never_set_is_not_an_error(run):
    """/drop-worktree clears unconditionally; it must not care whether one existed."""
    assert run("--clear").returncode == 0


@pytest.mark.parametrize("stage", ["F0", "R1", "R1F", "R12F", "A", "ABCDEF"])
def test_well_formed_stages_are_accepted(run, stage):
    """The shape is checked, not the vocabulary — a new stage needs no edit here."""
    assert run(stage).returncode == 0, run(stage).stderr


@pytest.mark.parametrize(
    "stage",
    [
        "ABCDEFG",       # seven characters: past what the bar has room for
        "R1 F",          # a space would split the field
        "R1F;whoami",    # punctuation, in case a caller interpolates
        "R1/../../x",    # separators, which a filename must never carry
        "",              # an empty argument is a caller bug, not a clear
    ],
)
def test_malformed_stages_are_refused_loudly(run, stage):
    """Exit 2, because a typo here is a caller bug and silence would hide it."""
    result = run(stage)
    assert result.returncode == 2
    assert not (run.dir / "11111111-2222-3333-4444-555555555555").exists()


def test_no_session_id_is_silent_success(run):
    """A loop under systemd has no session and nobody watching a bar. Telemetry
    that can fail the thing it reports on is worse than no telemetry."""
    result = run("R1", session_id=None)
    assert result.returncode == 0
    assert result.stderr == ""
    assert not run.dir.exists()


@pytest.mark.parametrize("session_id", ["../../evil", "a/b", "..", "with space"])
def test_a_session_id_that_could_escape_the_directory_is_refused(run, session_id):
    """The id becomes a filename. Refused rather than sanitised: a mangled id
    would write a marker that no reader ever looks for."""
    result = run("R1", session_id=session_id)
    assert result.returncode == 2
    assert "session id" in result.stderr


def test_show_on_a_fresh_session_prints_nothing(run):
    result = run("--show")
    assert result.returncode == 0
    assert result.stdout == ""


def test_help_works_without_a_session(run):
    """Documentation must not depend on being inside a Claude Code session."""
    result = run("--help", session_id=None)
    assert result.returncode == 0
    assert "qb-stage <stage>" in result.stdout


def test_the_marker_dir_is_created_on_demand(run):
    """First use of a fresh machine must not need a mkdir from the caller."""
    assert not run.dir.exists()
    run("F0")
    assert run.dir.is_dir()


# ---- and the fleet: the board half (#262) -----------------------------------


def test_a_stage_is_reported_to_the_board(run, board):
    """The half the marker file cannot do.

    Cross-machine that file is not there to read and same-machine nothing read
    it, so a stage that is only written locally is invisible to every surface a
    fleet is looked at through.
    """
    b = board()
    assert run("R1F", board_url=b.url).returncode == 0

    (req,) = b.wait()
    assert req["path"] == "/lease/stage"
    assert req["auth"] == "Bearer tok-test"
    assert json.loads(req["body"]) == {"session": run.session, "stage": "R1F"}


def test_clearing_tells_the_board_to_clear_too(run, board):
    """A lease still advertising `R2` after the work landed is worse than a blank one."""
    b = board()
    run("R2", board_url=b.url)
    run("--clear", board_url=b.url)

    reports = b.wait(2)
    assert json.loads(reports[-1]["body"]) == {"session": run.session, "stage": None}


def test_clearing_reports_even_when_no_marker_was_there(run, board):
    """The board is a second store and can hold a stage this cache does not —
    a session resumed on another box, a cache cleared by hand. `/drop-worktree`
    clears unconditionally and the report has to follow it."""
    b = board()
    assert run("--clear", board_url=b.url).returncode == 0
    (req,) = b.wait()
    assert json.loads(req["body"])["stage"] is None


def test_show_is_a_read_and_says_nothing_to_anybody(run, board):
    """`--show` is what the statusline calls on every render. A report there
    would turn a status bar into a write path several times a second."""
    b = board()
    run("R1", board_url=b.url)
    b.wait(1)
    run("--show", board_url=b.url)
    time.sleep(0.3)
    assert len(b.requests) == 1


def test_a_malformed_stage_is_not_reported(run, board):
    """It is refused locally, so there is nothing to say and nobody to say it to."""
    b = board()
    assert run("R1 F", board_url=b.url).returncode == 2
    time.sleep(0.3)
    assert b.requests == []


def test_no_session_means_no_report(run, board):
    """A stage belongs to a session's lease. With no session there is no lease
    to put one on, and the board would have nothing to key it by."""
    b = board()
    assert run("R1", board_url=b.url, session_id=None).returncode == 0
    time.sleep(0.3)
    assert b.requests == []


def test_a_session_id_that_could_escape_is_not_reported_either(run, board):
    """Refused before it reaches either store. The id is interpolated into JSON
    as well as into a path, so the same check defends both."""
    b = board()
    assert run("R1", board_url=b.url, session_id="a/b").returncode == 2
    time.sleep(0.3)
    assert b.requests == []


# ---- fail open: the board is never allowed to be the caller's problem --------


def test_an_unconfigured_machine_still_records_the_stage(run):
    """No board is a normal state, not a failure. The bar has always worked
    without one and it must go on working without one."""
    result = run("R1")
    assert result.returncode == 0
    assert result.stderr == ""
    assert (run.dir / run.session).read_text() == "R1"


def test_an_unreachable_board_is_silent_and_harmless(run, tmp_path):
    """A refused connection must not reach the caller as an exit code, a message
    on stderr, or a missing marker. There is nothing a skill could do about it."""
    result = run("R1", board_url="http://127.0.0.1:1")
    assert result.returncode == 0
    assert result.stderr == ""
    assert (run.dir / run.session).read_text() == "R1"


def test_a_board_that_refuses_the_report_is_still_not_the_callers_problem(run, board):
    """500 is the same answer as unreachable, from here: there is no retry worth
    making for a status field and no caller that could act on the difference."""
    b = board(status=500)
    result = run("R1", board_url=b.url)
    assert result.returncode == 0
    assert result.stderr == ""
    assert (run.dir / run.session).read_text() == "R1"
    b.wait(1)


def test_a_slow_board_does_not_delay_the_caller(run, board):
    """The one that decides whether this belongs in qb-stage at all.

    `qb-stage` is called from the middle of a workflow, several times a session.
    A board thinking for five seconds must cost the caller none of them, which is
    why the report — and the config resolution before it, since a token command
    is an arbitrary program — happens in a backgrounded subshell.
    """
    b = board(delay=5.0)
    started = time.monotonic()
    result = run("R1", board_url=b.url)
    elapsed = time.monotonic() - started

    assert result.returncode == 0
    assert elapsed < 2.0, f"qb-stage waited {elapsed:.1f}s on a board that was still thinking"
    assert (run.dir / run.session).read_text() == "R1"


def test_the_marker_is_written_even_when_the_report_is_in_flight(run, board):
    """The local half is what a person is looking at; it must not queue behind
    the remote half. Asserted at the moment the process exits, before the
    report can possibly have completed."""
    b = board(delay=3.0)
    run("R2F", board_url=b.url)
    assert (run.dir / run.session).read_text() == "R2F"


def _token_stub(tmp_path, seconds=30):
    """A credential source that never answers, and can be found by its own path."""
    stub = tmp_path / "slow-token-stub"
    # `#!/bin/sh`, never `#!/usr/bin/env` — see the note in the test below (#177).
    stub.write_text(f"#!/bin/sh\nsleep {seconds}\necho tok-test\n")
    stub.chmod(0o755)
    return stub


def _stub_env(run, tmp_path, board_url, stub):
    env = {k: v for k, v in os.environ.items() if not k.startswith("QUARTERBACK_")}
    # No inherited token, or `board_config` would short-circuit the command these
    # tests exist to run and their assertions would pass without proving it.
    env.update({
        "QB_SESSION_STAGE_DIR": str(run.dir),
        "QUARTERBACK_CONFIG": str(tmp_path / "no-such-config"),
        "QUARTERBACK_BASE_URL": board_url,
        "QUARTERBACK_TOKEN_CMD": str(stub),
        "CLAUDE_CODE_SESSION_ID": run.session,
    })
    return env


def _stub_running(stub) -> bool:
    return subprocess.run(["pgrep", "-f", str(stub)], capture_output=True).returncode == 0


def test_a_hanging_token_command_does_not_outlive_its_bound(run, board, tmp_path):
    """The orphan the test above would otherwise leave behind.

    `curl --max-time` bounds curl and nothing else, so an unbounded credential
    source leaves a detached subshell per call that nobody is watching — the one
    failure mode a fail-open path can still accumulate. The bound is on the token
    command because that is the part that can wait forever.
    """
    b = board()
    stub = _token_stub(tmp_path)
    subprocess.run([str(QB_STAGE), "R1"], env=_stub_env(run, tmp_path, b.url, stub),
                   capture_output=True, text=True, check=False)
    assert _stub_running(stub), "the token command should have been started at all"

    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if not _stub_running(stub):
            return
        time.sleep(0.25)
    subprocess.run(["pkill", "-f", str(stub)], capture_output=True)
    raise AssertionError("a hanging token command was left running past its bound")


def test_a_token_command_that_hangs_does_not_hang_the_caller(run, board, tmp_path):
    """QUARTERBACK_TOKEN_CMD is an arbitrary program — a `pass` lookup, a gpg
    agent — and one waiting on a passphrase would block forever with nobody at a
    terminal to answer it. Hence stdin closed, and hence the resolution itself
    inside the background subshell rather than in front of it."""
    b = board()
    # `#!/bin/sh`, never `#!/usr/bin/env` — there is no `/usr/bin/env` inside a nix
    # build sandbox and `patchShebangs` cannot reach a file a test writes while it
    # runs (#177). The stub body is POSIX, so the plain path is all it needs.
    stub = _token_stub(tmp_path, seconds=10)

    started = time.monotonic()
    proc = subprocess.run([str(QB_STAGE), "R1"], env=_stub_env(run, tmp_path, b.url, stub),
                          capture_output=True, text=True)
    elapsed = time.monotonic() - started

    assert proc.returncode == 0
    assert elapsed < 2.0, f"qb-stage waited {elapsed:.1f}s on a token command"
    assert (run.dir / run.session).read_text() == "R1"
    subprocess.run(["pkill", "-f", str(stub)], capture_output=True)
