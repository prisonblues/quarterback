"""`qb-status` — the pane's answer, the agent's answer, and the gap (#277 step 2).

Before this, "is that session alive" had one answer and it was wrong in whichever
direction it was taken from. tmux knows whether a PROCESS is there; the board
knows what the AGENT last said about itself; and #263 and #252 are both records of
the two disagreeing while nothing was able to say so.

What this suite is really about:

* **THE TABLE IS THE FEATURE.** Every combination of the two sources is a
  different fault with a different remedy, and each one is exercised here — a
  crashed seat, a `/clear` that left the pane running, a long turn, an agent the
  board never heard of. A case nobody has thought of should be a missing row, and
  a missing row is a KeyError rather than a plausible-looking verdict.
* **"GONE" IS NEVER GUESSED FROM AN ABSENCE OF TMUX.** Run outside a multiplexer
  the pane source says *cannot tell*, which is #244's rule: a governor that cannot
  read its input must not report clear.
* **A 404 IS AN ANSWER AND AN OUTAGE IS NOT.** Taken off `HTTPError.code`, never
  matched in a message — a session id with "404" in it is not a missing session.

Stubbed the way `test_qb_admit.py` stubs one: a COPY of the script beside a stub
`qbdata.py`, plus a `tmux` on PATH that replays canned panes.

Run: pytest harness/tests/test_qb_status.py
"""

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
STATUS = BIN / "qb-status"

ALIVE, FINISHED, GONE, UNKNOWN = 0, 1, 2, 3

SESSION = "24e8ee23-1111-2222-3333-444455556666"

#: The order `qb-status` asks tmux for, restated so the canned panes below line up
#: with it — and pinned against the script by
#: :func:`test_the_pane_fields_are_the_ones_this_suite_replays`, because a field
#: inserted in the script and not here would silently shift every canned answer by
#: one and the suite would keep passing about the wrong columns.
FIELDS = ("pane_id", "@qb_session", "@qb_seat", "@qb_spawn", "@qb_spawn_ended",
          "@qb_state", "pane_dead", "pane_current_command", "session_name")


def pane(session: str = SESSION, **over) -> str:
    row = {"pane_id": "%3", "@qb_session": session, "@qb_seat": "", "@qb_spawn": "",
           "@qb_spawn_ended": "", "@qb_state": "", "pane_dead": "0",
           "pane_current_command": "node", "session_name": "seats"}
    row.update(over)
    return "\t".join(row[f] for f in FIELDS)


def sandbox(tmp_path: Path, *, panes: list[str] | None = "none",
            lease: dict | None = None, ended: dict | None = None,
            record: bool = True, session_error: int | None = None,
            active_error: bool = False, no_client: bool = False,
            agent: str | None = None) -> dict:
    """A copy of `qb-status` whose two sources both answer to this test.

    `panes=None` means tmux exists and knows of no pane; `panes="none"` means
    there is no tmux at all, which is a different answer and the one most easily
    got wrong.
    """
    if lease and lease.get("state_at") == FRESH:
        lease = {**lease,
                 "state_at": (datetime.now(UTC) - timedelta(seconds=30)).isoformat()}
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    copied = stub / STATUS.name
    copied.write_bytes(STATUS.read_bytes())
    (stub / "qbdata.py").write_text(f"""
import json
import urllib.error

RECORD = {record!r}
LEASE = json.loads({json.dumps(lease)!r})
ENDED = json.loads({json.dumps(ended)!r})
SESSION_ERROR = {session_error!r}
ACTIVE_ERROR = {active_error!r}


class _Config:
    agent = {agent!r}


def _http(code):
    return urllib.error.HTTPError("http://board", code, "nope", {{}}, None)


class _Client:
    def get(self, path, params=None):
        if path.startswith("/session/"):
            if SESSION_ERROR:
                raise _http(SESSION_ERROR)
            if not RECORD:
                raise _http(404)
            return {{"session": path.rsplit("/", 1)[-1], "ended": ENDED}}
        if path == "/active":
            if ACTIVE_ERROR:
                raise RuntimeError("the board's live list is unreadable")
            return {{"agents": [LEASE] if LEASE else []}}
        raise AssertionError("qb-status asked for " + path)


def board_client():
    if {no_client!r}:
        raise RuntimeError("no board configured (QUARTERBACK_BASE_URL is unset)")
    return _Client(), _Config()
""")

    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    if panes != "none":
        canned = tmp_path / "panes.txt"
        canned.write_text("\n".join(panes or []) + ("\n" if panes else ""))
        # `#!/bin/sh`, never `#!/usr/bin/env` — #177, and there is no
        # `/usr/bin/env` inside a nix build sandbox.
        (tools / "tmux").write_text(f"#!/bin/sh\ncat {canned}\n")
        (tools / "tmux").chmod(0o755)
    return {"script": copied, "tools": tools, "tmux": panes != "none"}


def run(box: dict, *args: str, session: str | None = SESSION):
    """`qb-status` in the sandbox. $CLAUDE_CODE_SESSION_ID is cleared and the
    session passed as an argument instead, so a test never reads the identity of
    whatever is running the suite; `session=None` is how the no-session-anywhere
    case is reached."""
    env = {**os.environ, "PATH": f"{box['tools']}{os.pathsep}{os.environ['PATH']}"}
    env.pop("TMUX", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    if box["tmux"]:
        env["TMUX"] = "/tmp/fake,1,0"
    positional = [] if args or session is None else [session]
    return subprocess.run([sys.executable, str(box["script"]), *positional, *args],
                          capture_output=True, text=True, env=env)


#: A lease whose beacon moved a moment ago. Substituted by :func:`sandbox` at the
#: moment the test runs, not written down and not computed at import: a fixed
#: timestamp is either in the past and goes stale (turning every "fresh" test into
#: a staleness test the day it crosses eight minutes) or in the future and
#: exercises clock skew instead of freshness — and one computed at import drifts
#: by however long the suite takes to reach the test, which is 93 seconds in the
#: nix sandbox and was enough to break this once already.
FRESH = "<fresh>"
LIVE = {"session": SESSION, "holder": "zeus/seat-quarterback-1", "state": "working",
        "state_at": FRESH, "expires": "2999-01-01T01:00:00+00:00"}
ENDING = {"reason": "finished", "at": "2026-08-22T10:00:00+00:00", "holder": "zeus/x"}


# ------------------------------------------------- pane running, board agreeing

def test_a_running_pane_with_a_fresh_lease_is_alive_and_says_nothing_else(tmp_path):
    got = run(sandbox(tmp_path, panes=[pane()], lease=LIVE))
    assert got.returncode == ALIVE
    assert "alive:" in got.stderr
    assert "⚠" not in got.stderr, "there is no disagreement to report"


def test_both_sources_are_always_reported_separately(tmp_path):
    """The whole ask of step 2: `status` reports the pane's answer and the agent's
    answer SEPARATELY. A single merged verdict is the thing that could not tell a
    long turn from a corpse."""
    got = run(sandbox(tmp_path, panes=[pane()], lease=LIVE))
    assert re.search(r"pane:\s+running", got.stderr)
    assert re.search(r"agent:\s+leased", got.stderr)


# ------------------------------------------------------------ the disagreements

def test_a_running_pane_whose_session_ended_is_263s_shape(tmp_path):
    """A `/clear` or a supersede: the pane lives on and this conversation does
    not. Before #277 that was indistinguishable from a healthy seat."""
    got = run(sandbox(tmp_path, panes=[pane()],
                      ended={"reason": "context_reset", "at": "t", "holder": "zeus/x"}))
    assert got.returncode == ALIVE
    assert "#263" in got.stderr


def test_a_running_pane_with_a_lapsed_lease_says_the_board_stopped_counting(tmp_path):
    got = run(sandbox(tmp_path, panes=[pane()]))
    assert got.returncode == ALIVE
    assert "lapsed" in got.stderr
    assert "stopped counting" in got.stderr


def test_a_running_pane_the_board_never_heard_of_is_named_as_that(tmp_path):
    got = run(sandbox(tmp_path, panes=[pane()], record=False))
    assert got.returncode == ALIVE
    assert "never heard of" in got.stderr


def test_a_pane_gone_with_a_live_lease_is_a_crashed_seat(tmp_path):
    """The case #266 is about from the other end: the board will show it working
    until the TTL runs out, and `qb-end --reason killed` is the report nothing
    made."""
    got = run(sandbox(tmp_path, panes=[], lease=LIVE))
    assert got.returncode == GONE
    assert "crashed or killed" in got.stderr
    assert "qb-end" in got.stderr


def test_a_stale_state_reads_as_a_long_turn_and_not_a_stall(tmp_path):
    """#252, in the one sentence that stops a reader concluding "stuck" from a
    timestamp: the beacon moves at turn boundaries only."""
    stale = {**LIVE, "state_at": "2020-01-01T00:00:00+00:00"}
    got = run(sandbox(tmp_path, panes=[pane()], lease=stale))
    assert got.returncode == ALIVE
    assert "#252" in got.stderr
    assert "long turn, not a stall" in got.stderr


def test_a_fresh_state_is_not_called_a_long_turn(tmp_path):
    got = run(sandbox(tmp_path, panes=[pane()], lease=LIVE))
    assert "#252" not in got.stderr
    # A range, not a second: FRESH is stamped when this module is imported and the
    # suite reaches here later, so pinning the exact figure is a test that goes red
    # on a slow box rather than on a defect.
    assert re.search(r"working as of 0m\d\ds ago", got.stderr), \
        "the state's age is what a reader judges it by"


def test_a_state_stamped_in_the_future_is_clock_skew_and_not_a_negative_age(tmp_path):
    """Two clocks, and one of them is the board's. A negative age reads as a bug in
    this tool and sends a reader looking in the wrong place."""
    ahead = {**LIVE, "state_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
    got = run(sandbox(tmp_path, panes=[pane()], lease=ahead))
    assert got.returncode == ALIVE
    agent_line = got.stderr.split("agent:")[1].split("\n")[0]
    assert "just now" in agent_line
    assert "ago" not in agent_line, f"a negative age was rendered: {agent_line}"


# ---------------------------------------------------------------- the endings

def test_a_pane_gone_and_an_ending_reported_is_finished(tmp_path):
    got = run(sandbox(tmp_path, panes=[], ended=ENDING))
    assert got.returncode == FINISHED
    assert "finished" in got.stderr


def test_a_pane_gone_and_a_lapsed_lease_is_gone_not_finished(tmp_path):
    """The distinction v2.77 exists to draw. An expired lease says *nobody
    renewed*, which is not *the work finished* — reading it as one is how a
    dashboard reported a crash as a success."""
    got = run(sandbox(tmp_path, panes=[]))
    assert got.returncode == GONE


def test_an_exited_spawn_whose_ending_nobody_reported_is_gone(tmp_path):
    """The window a `qb-start` spawn leaves behind, read correctly: the agent has
    gone and something should have said so."""
    got = run(sandbox(tmp_path, panes=[pane(**{"@qb_spawn": "/fix-issue",
                                               "@qb_spawn_ended": "1"})], lease=LIVE))
    assert got.returncode == GONE
    assert "exited" in got.stderr
    assert "TTL" in got.stderr


def test_a_dead_pane_is_exited_too(tmp_path):
    got = run(sandbox(tmp_path, panes=[pane(pane_dead="1")], ended=ENDING))
    assert got.returncode == FINISHED


# ------------------------------------------- an inability is never a fact (#244)

def test_no_tmux_is_cannot_tell_and_never_gone(tmp_path):
    """Reporting a session gone because this process could not see a pane would be
    an inability dressed as a fact — the same class of mistake as reading an
    expired lease as a finished one."""
    got = run(sandbox(tmp_path, panes="none", lease=LIVE))
    assert got.returncode == ALIVE
    assert "unknown" in got.stderr
    assert "not inside tmux" in got.stderr
    assert "no pane was checked" in got.stderr


def test_no_tmux_and_a_lapsed_lease_is_unknown_rather_than_gone(tmp_path):
    got = run(sandbox(tmp_path, panes="none"))
    assert got.returncode == UNKNOWN


def test_no_board_is_unknown_and_does_not_become_a_verdict(tmp_path):
    got = run(sandbox(tmp_path, panes=[], no_client=True))
    assert got.returncode == UNKNOWN
    assert "could not be asked" in got.stderr


def test_a_running_pane_still_reads_alive_when_the_board_is_down(tmp_path):
    """One source failing does not take the other's answer with it — a pane that
    is running is a fact whatever the board can be asked."""
    got = run(sandbox(tmp_path, panes=[pane()], no_client=True))
    assert got.returncode == ALIVE
    assert "nothing here is a report about the agent" in got.stderr


def test_a_404_means_no_such_session_and_a_500_means_cannot_tell(tmp_path):
    assert run(sandbox(tmp_path, panes=[], record=False)).returncode == GONE
    outage = run(sandbox(tmp_path, panes=[], session_error=503))
    assert outage.returncode == UNKNOWN
    assert "could not be asked" in outage.stderr


def test_a_session_id_containing_404_is_not_a_missing_session(tmp_path):
    """Taken off `HTTPError.code`, never matched in a message. A status tool that
    mistook an outage for "no such session" would be making #244's mistake in the
    one command written to stop it."""
    got = run(sandbox(tmp_path, panes=[], session_error=503),
              "404b1234-4040-4040-4040-404040404040")
    assert got.returncode == UNKNOWN


def test_an_unreadable_live_list_is_unknown_too(tmp_path):
    got = run(sandbox(tmp_path, panes=[], active_error=True))
    assert got.returncode == UNKNOWN


# ----------------------------------------------------------------- the plumbing

def test_the_session_defaults_to_this_one_and_says_so_when_there_is_none(tmp_path):
    got = run(sandbox(tmp_path, panes=[]), session=None)
    assert got.returncode == UNKNOWN
    assert "$CLAUDE_CODE_SESSION_ID is unset" in got.stderr


def test_only_the_named_session_is_looked_at(tmp_path):
    """`list-panes -a` is the whole tmux server, so a pane belonging to another
    session must not answer for this one."""
    other = pane(session="99999999-0000-0000-0000-000000000000")
    got = run(sandbox(tmp_path, panes=[other], lease=LIVE))
    assert "absent" in got.stderr


def test_json_carries_both_sources_and_the_disagreement(tmp_path):
    got = run(sandbox(tmp_path, panes=[], lease=LIVE), SESSION, "--json")
    answer = json.loads(got.stdout)
    assert answer["verdict"] == "gone"
    assert answer["pane"]["verdict"] == "absent"
    assert answer["agent"]["verdict"] == "leased"
    assert "crashed" in answer["disagreement"]


def test_json_says_null_rather_than_omitting_an_agreement(tmp_path):
    """A caller branching on `disagreement` should not have to know whether the
    key is there."""
    got = run(sandbox(tmp_path, panes=[pane()], lease=LIVE), SESSION, "--json")
    assert json.loads(got.stdout)["disagreement"] is None


def test_quiet_wins_over_json_and_prints_nothing_at_all(tmp_path):
    got = run(sandbox(tmp_path, panes=[pane()], lease=LIVE), SESSION, "--json", "-q")
    assert (got.stdout, got.stderr) == ("", "")
    assert got.returncode == ALIVE


def test_it_never_writes_to_the_board(tmp_path):
    """It observes and nothing else: no process signalled, no pane closed, no lease
    touched. The stub client has no `post` at all, so a write is an AttributeError
    rather than a review comment."""
    assert "post" not in STATUS.read_text().split('"""', 2)[2]


def test_a_running_pane_wins_over_a_finished_one_with_the_same_session(tmp_path):
    """One session id can be on two panes at once: `qb-start` leaves its window open
    after the agent exits, so resuming that session in a new pane leaves the corpse
    behind wearing the same stamp. Taking the first match let `list-panes` order
    decide the verdict."""
    corpse = pane(**{"@qb_spawn": "/fix-issue", "@qb_spawn_ended": "1",
                     "pane_id": "%1"})
    alive = pane(pane_id="%2")
    got = run(sandbox(tmp_path, panes=[corpse, alive], lease=LIVE))
    assert got.returncode == ALIVE, got.stderr
    assert "%2" in got.stderr


def test_a_dead_pane_beside_a_dead_pane_is_still_exited(tmp_path):
    """The preference is for a RUNNING pane, not for the last one listed: with
    nothing running, the first match is still the answer."""
    got = run(sandbox(tmp_path, panes=[pane(**{"@qb_spawn_ended": "1"}),
                                       pane(pane_dead="1", pane_id="%4")],
                      ended=ENDING))
    assert got.returncode == FINISHED


# --------------------------------------- a pane this box cannot see is not gone

def test_a_lease_on_another_machine_is_not_a_crashed_seat(tmp_path):
    """The pane source can only ever see THIS tmux server, so "no pane carries
    that session" says nothing at all about a session leased by another machine.
    Calling that `gone` was this tool asserting something it had not looked at."""
    got = run(sandbox(tmp_path, panes=[], lease={**LIVE, "holder": "laptop/seat-1"},
                      agent="zeus"))
    assert got.returncode == UNKNOWN
    assert "held on laptop" in got.stderr
    assert "Ask laptop" in got.stderr


def test_a_lease_on_this_machine_with_no_pane_is_still_a_crashed_seat(tmp_path):
    got = run(sandbox(tmp_path, panes=[], lease={**LIVE, "holder": "zeus/seat-1"},
                      agent="zeus"))
    assert got.returncode == GONE
    assert "crashed or killed" in got.stderr


def test_a_machine_this_box_cannot_name_never_downgrades_the_verdict(tmp_path):
    """`cfg.agent` is a GUESS — the board's machine name comes from the token map
    and need not match what this box calls itself — so it is only ever used to say
    "elsewhere" when the two DISAGREE, never to confirm that a lease is local."""
    got = run(sandbox(tmp_path, panes=[], lease={**LIVE, "holder": "laptop/seat-1"},
                      agent=None))
    assert got.returncode == GONE


def test_a_running_pane_is_unaffected_by_whose_machine_the_lease_names(tmp_path):
    """Only the ABSENT row is a blind spot. A pane that is here carrying that
    session is a fact, whatever the board thinks about which box it is on."""
    got = run(sandbox(tmp_path, panes=[pane()], lease={**LIVE, "holder": "laptop/x"},
                      agent="zeus"))
    assert got.returncode == ALIVE


# ------------------------------------------------------------------- no drift

def test_the_pane_fields_are_the_ones_this_suite_replays():
    """A field inserted in the script and not here shifts every canned answer by
    one column, and the suite would keep passing about the wrong thing."""
    body = STATUS.read_text()
    block = body.split("fields = (", 1)[1].split(")", 1)[0]
    assert tuple(re.findall(r'"([^"]+)"', block)) == FIELDS


def test_every_pair_of_verdicts_has_a_row():
    """A missing row is a KeyError, which is the right failure — but only if every
    verdict either source can produce is in the product."""
    body = STATUS.read_text()
    table = body.split("TABLE: dict[tuple[str, str], tuple[int, str]] = {", 1)[1]
    table = table.split("\n}", 1)[0]
    rows = set(re.findall(r'\("([a-z]+)",\s*"([a-z]+)"\)', table))
    panes = {"running", "exited", "absent", "unknown"}
    agents = {"leased", "ended", "lapsed", "never", "unknown"}
    assert rows == {(p, a) for p in panes for a in agents}


@pytest.mark.parametrize("verdict", ["running", "exited", "absent", "unknown"])
def test_the_pane_verdicts_are_the_ones_the_table_expects(verdict):
    assert f'"verdict": "{verdict}"' in STATUS.read_text()


@pytest.mark.parametrize("verdict", ["leased", "ended", "lapsed", "never", "unknown"])
def test_the_agent_verdicts_are_the_ones_the_table_expects(verdict):
    assert f'"verdict": "{verdict}"' in STATUS.read_text()
