"""`qb-end` — the stop verb as a CLI (#277).

There were three ways to start a session on this fleet and none to end one, so
what stood in for ending was the TTL. That is a floor, not a report: an expired
lease says *nobody renewed*, which is the identical row whether the work
finished, the pane was closed, or the agent is thinking hard (#252).

This is the half a script and a button call. Its two callers — `qb-hook` on
SessionEnd and `qb-seat-click` before it kills a pane — both ignore the exit code
by design (a ✕ closes the pane whether or not the board hears), so the codes are
for a human and for these tests. What they must get right is the same split
`qb-claim` states: a REFUSAL is permanent and a caller should stop; an OUTAGE is a
thing you wait out, and collapsing them is how "the board refused this" gets
reported as "try again later" forever.

Run: pytest harness/tests
"""

import json
import os
import subprocess
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
END = BIN / "qb-end"


def run(*args, board: str | None = "http://b", answer: dict | None = None,
        status: int = 200, tmp_path: Path = None, session_env: str | None = None):
    """Run `qb-end` against a stubbed board.

    Stubbed the way `test_qb_claim.py` stubs one, and for the same reason: a COPY
    of the script beside a stub `qbdata.py`. The script inserts its own directory
    at the front of `sys.path` — which is how an installed harness finds the
    module beside it in `$out/bin` — so PYTHONPATH cannot shadow the real one and
    the copy is the only seam there is.
    """
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    copied = stub / END.name
    copied.write_bytes(END.read_bytes())
    (stub / "qbdata.py").write_text(f"""
import json, urllib.error, io

BOARD = {board!r}
STATUS = {status!r}
BODY = json.loads({json.dumps(answer if answer is not None else {})!r})


class _Client:
    def post(self, path, body):
        # Recorded on stdout of the stub's own making would collide with the
        # script's; the assertions that care about the request read it here.
        print(json.dumps({{"path": path, "body": body}}), file=__import__("sys").stderr)
        if STATUS != 200:
            raise urllib.error.HTTPError(
                "http://board" + path, STATUS, "nope", {{}},
                io.BytesIO(json.dumps({{"detail": BODY}}).encode()))
        return BODY


def board_client():
    if BOARD is None:
        raise RuntimeError("no board configured (QUARTERBACK_BASE_URL is unset)")
    return _Client(), None
""")
    env = {**os.environ}
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    if session_env is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session_env
    return subprocess.run([sys.executable, str(copied), *args],
                          capture_output=True, text=True, env=env)


RECORDED, REFUSED, UNKNOWN = 0, 1, 2


def _sent(got) -> dict:
    """The request body the stub saw."""
    line = next(ln for ln in got.stderr.splitlines() if ln.startswith('{"path"'))
    return json.loads(line)


def test_a_clean_ending_is_zero_and_names_what_it_handed_back(tmp_path):
    got = run("s-1", "--reason", "killed", tmp_path=tmp_path,
              answer={"ended": True, "lease_was": "released",
                      "released_claims": [{"kind": "merge", "key": "acme/w:main"}]})
    assert got.returncode == RECORDED
    assert "ended: s-1" in got.stderr and "killed" in got.stderr
    assert "released merge/acme/w:main" in got.stderr
    assert _sent(got) == {"path": "/session/end",
                          "body": {"session": "s-1", "reason": "killed"}}


def test_ending_something_already_ended_is_still_zero(tmp_path):
    """Two callers race here by design — a hook and a human on the ✕ — and the
    second must not read as broken because the first won."""
    got = run("s-2", tmp_path=tmp_path,
              answer={"ended": False, "lease_was": "already ended",
                      "released_claims": []})
    assert got.returncode == RECORDED
    assert "already: s-2 was already ended" in got.stderr


def test_the_default_reason_is_finished(tmp_path):
    got = run("s-3", tmp_path=tmp_path, answer={"ended": True, "lease_was": "released"})
    assert _sent(got)["body"]["reason"] == "finished"


def test_the_session_defaults_to_this_claude_code_session(tmp_path):
    """So a hook, a skill or a human in a pane can call it with no argument at
    all — the id it would have to pass is already in the environment."""
    got = run(tmp_path=tmp_path, session_env="sid-from-env",
              answer={"ended": True, "lease_was": "released"})
    assert got.returncode == RECORDED
    assert _sent(got)["body"]["session"] == "sid-from-env"


def test_no_session_anywhere_is_unknown_and_says_which_thing_was_missing(tmp_path):
    got = run(tmp_path=tmp_path)
    assert got.returncode == UNKNOWN
    assert "CLAUDE_CODE_SESSION_ID" in got.stderr


def test_a_reason_the_board_would_not_take_never_leaves_the_process(tmp_path):
    """argparse refuses it, so a typo costs nothing and names the choices. The
    board's vocabulary is closed because the field is branched on, and a client
    that posted free text would be asking for a 422 it could only report."""
    got = run("s-4", "--reason", "gave-up", tmp_path=tmp_path)
    assert got.returncode == 2  # argparse's own usage exit
    assert "context_reset" in got.stderr


def test_the_board_refusing_is_one_and_says_it_is_not_an_outage(tmp_path):
    """A session is ended by the box it runs on. Asking again changes nothing,
    and a caller told "cannot tell" would retry a permanent refusal forever."""
    got = run("s-5", tmp_path=tmp_path, status=403,
              answer={"error": "that session is leased by another machine"})
    assert got.returncode == REFUSED
    assert "refused:" in got.stderr
    assert "another machine" in got.stderr


def test_an_unconfigured_host_is_unknown_rather_than_a_crash(tmp_path):
    """The commonest state of a box that is not on the fleet, and it must be a
    quiet 2 — this is called from a hook that has to carry on regardless."""
    got = run("s-6", board=None, tmp_path=tmp_path)
    assert got.returncode == UNKNOWN
    assert "could not reach the board" in got.stderr


def test_a_board_error_is_unknown_not_refused(tmp_path):
    """A 500 is the board being unwell, which is a thing you wait out."""
    got = run("s-7", tmp_path=tmp_path, status=500, answer={"error": "boom"})
    assert got.returncode == UNKNOWN
    assert "HTTP 500" in got.stderr


def test_quiet_says_nothing_on_either_stream(tmp_path):
    """`--quiet` is the stronger promise and wins over `--json`: the ✕ on the seat
    bar calls this from a `run-shell -b`, where anything written is either
    discarded or lands on somebody's terminal."""
    got = run("s-8", "--quiet", "--json", tmp_path=tmp_path,
              answer={"ended": True, "lease_was": "released"})
    assert got.returncode == RECORDED
    assert got.stdout == ""
    assert not [ln for ln in got.stderr.splitlines() if not ln.startswith('{"path"')]


def test_json_puts_the_boards_whole_answer_on_stdout(tmp_path):
    answer = {"ended": True, "lease_was": "released", "reason": "finished",
              "released_claims": []}
    got = run("s-9", "--json", tmp_path=tmp_path, answer=answer)
    assert json.loads(got.stdout) == answer


def test_claims_it_would_not_release_are_reported_rather_than_swallowed(tmp_path):
    """Reading `released_claims` as "everything is let go" is the mistake, so a
    claim the board left alone has to be visible from here."""
    got = run("s-10", tmp_path=tmp_path,
              answer={"ended": True, "lease_was": "released", "released_claims": [],
                      "refused_claims": [{"kind": "merge", "key": "acme/w:main"}]})
    assert got.returncode == RECORDED
    assert "held by another machine and were left alone" in got.stderr
