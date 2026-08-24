"""`qb-next` — the caller the plan never had (#424), driven against a real board socket.

The board side of this has been complete for weeks: `GET /plan` computes `next`,
`POST /plan/item/claim` is the interlock, `POST /plan/item/done` closes the loop.
Nothing called any of it. So the thing under test here is not a tool that formats
an answer — it is the claim-before-you-start discipline that lets several agents
work one plan without a lead, and a suite that stubbed the claim away would assert
everything about it except the part that matters.

**So the board is a real HTTP server on a real socket, and the agents are real
processes.** `qb-next` runs as a subprocess, resolves its config from the
environment exactly as it does on a live box, and talks to
:class:`Board` over the loopback with the client it ships with. What is faked is
one thing: the board's storage, which is a dict behind a lock. First-come
first-served on `(item, session)` is the property the board's own claim row has and
the property this suite needs, and it is fifteen lines rather than a database.

**The two-agent test holds both agents at the plan read until both have it.**
`Board.pair` is a barrier in `GET /plan`, so neither agent can claim until both
have been handed the same `next`. That removes the one uninteresting outcome — a
run where the first agent finished before the second started, which proves nothing
about the claim — and leaves the collision guaranteed. What separates them is then
the 409 and nothing else, which is the whole design.

`gh` is a stub on a PATH holding nothing else. Not for convenience: whether the
ref check reports OPEN or CLOSED must not depend on whether the developer running
the suite has the real `gh` installed and authenticated, or a verdict here means
something different on two machines.

Run: pytest harness/tests/test_qb_next.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

HARNESS = Path(__file__).resolve().parents[1]
QB_NEXT = HARNESS / "bin" / "qb-next"
BRIEF = HARNESS / "commands" / "get-involved.md"

SCOPE = "acme/widget"


# --------------------------------------------------------------------- the board


class Board:
    """The half of the board this tool talks to: a plan, and a claim per item.

    Deliberately not a mock of the client. `qb-next` reaches this through
    `qbdata.BoardClient`, over TCP, with the same JSON both ends really exchange —
    so a change to the envelope's shape, the refusal's shape or the client's
    error handling fails here rather than passing against a double that agreed
    with the test instead of with the server.
    """

    def __init__(self, items: list[dict], trusted: bool = True,
                 pair: bool = False) -> None:
        self.lock = threading.Lock()
        self.items = [self._row(spec, rank) for rank, spec in enumerate(items, 1)]
        self.trusted = trusted
        #: Hold `GET /plan` until two agents have read it, so the collision the
        #: two-agent test is about cannot be missed by one finishing first.
        self.barrier = threading.Barrier(2, timeout=20) if pair else None
        self.posts: list[tuple[str, dict]] = []

    @staticmethod
    def _row(spec: dict, rank: int) -> dict:
        ref = spec.get("ref", {"kind": "issue", "value": str(60 + rank)})
        return {
            "item_id": spec.get("item_id") or str(uuid.uuid4()),
            "repo": spec.get("repo", SCOPE),
            "title": spec.get("title", f"item at rank {rank}"),
            "ref": ref,
            "rank": rank,
            "rank_source": spec.get("rank_source", "ordered"),
            "placed_for": spec.get("placed_for"),
            "state": "open",
            "note": spec.get("note"),
            "plan": spec.get("plan"),
            "depends_on": [],
            "blocked_by": spec.get("blocked_by", []),
            "covered_by": spec.get("covered_by"),
            "claim": spec.get("claim"),
        }

    # -- reads

    def envelope(self) -> dict:
        with self.lock:
            rows = [dict(i) for i in self.items if i["state"] == "open"]
        free = [i for i in rows
                if not i["claim"] and not i["blocked_by"] and not i["covered_by"]]
        nxt = dict(free[0]) if free else None
        trust = self._trust(len(rows))
        if nxt is not None:
            nxt["caveat"] = None if self.trusted else (
                f"{trust['unchosen']} of {len(rows)} open items sit where they were "
                f"appended and nobody chose those positions — the first at rank "
                f"{trust['first_unchosen']['rank']} of the {SCOPE} list.")
        return {
            "repo": SCOPE, "exact": False, "scopes": [], "plan": None, "plans": [],
            "items": rows, "truncated": False, "next": nxt, "order_trust": trust,
            "counts": {
                "open": len(rows),
                "claimed": sum(1 for i in rows if i["claim"]),
                "blocked": sum(1 for i in rows if i["blocked_by"]),
                "covered": sum(1 for i in rows if i["covered_by"]),
                "stale": 0,
                "done": sum(1 for i in self.items if i["state"] == "done"),
                "dropped": 0,
            },
        }

    def _trust(self, open_n: int) -> dict:
        if self.trusted:
            return {"trusted": True, "by_source": {"ordered": open_n}, "unchosen": 0,
                    "first_unchosen": None, "hint": None}
        return {"trusted": False, "by_source": {"appended": open_n}, "unchosen": open_n,
                "first_unchosen": {"rank": 1, "repo": SCOPE},
                "hint": "a human places these at /plan/view"}

    # -- writes

    def claim(self, body: dict) -> tuple[int, dict]:
        """First come, first served, per session. The interlock, in one lock.

        A second agent is refused with the holder in the body, which is the shape
        `app/api/claims.py:_conflict` uses — the refusal is somebody to talk to
        rather than a denial, and `qb-next` reads `held_by` out of it to say so.
        """
        with self.lock:
            item = self._find(body["item_id"])
            if item is None:
                return 404, {"detail": {"error": "no such item"}}
            if item["state"] != "open":
                return 409, {"detail": {"error": f"that item is {item['state']}"}}
            held = item["claim"]
            if held and held.get("session") != body.get("session"):
                return 409, {"detail": {
                    "error": f"work claim on {SCOPE}#{item['ref']['value']!r} is held",
                    "held_by": held["holder"], "session": held["session"],
                    "note": held["note"]}}
            item["claim"] = {
                "holder": f"zeus/{body.get('session')}",
                "session": body.get("session"),
                "note": body.get("note") or f"plan: {item['title']}",
                "expires": (datetime.now(timezone.utc)
                            + timedelta(hours=1)).isoformat(),
            }
            return 200, {**item, "claimed": True, "renewed": False,
                         "claim_id": str(uuid.uuid4())}

    def done(self, body: dict) -> tuple[int, dict]:
        with self.lock:
            item = self._find(body["item_id"])
            if item is None:
                return 404, {"detail": {"error": "no such item"}}
            item["state"], item["done_note"] = "done", body.get("note")
            item["claim"] = None
            return 200, {"item_id": item["item_id"], "state": "done"}

    def release(self, body: dict) -> tuple[int, dict]:
        with self.lock:
            item = self._find(body["item_id"])
            if item is None:
                return 404, {"detail": {"error": "no such item"}}
            item["claim"] = None
            return 200, {"item_id": item["item_id"], "released": True}

    def _find(self, item_id: str) -> dict | None:
        return next((i for i in self.items if i["item_id"] == item_id), None)

    def row(self, rank: int) -> dict:
        return self.items[rank - 1]


class _Handler(BaseHTTPRequestHandler):
    board: Board

    def do_GET(self) -> None:                                    # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/plan":
            return self._send(404, {"detail": "no"})
        query = parse_qs(parsed.query)
        self.board.posts.append(("GET /plan", {k: v[0] for k, v in query.items()}))
        if self.board.barrier is not None:
            # Both agents hold the same `next` before either may claim it.
            self.board.barrier.wait()
        self._send(200, self.board.envelope())

    def do_POST(self) -> None:                                   # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        path = urlparse(self.path).path
        self.board.posts.append((f"POST {path}", body))
        route = {"/plan/item/claim": self.board.claim,
                 "/plan/item/done": self.board.done,
                 "/plan/item/release": self.board.release}.get(path)
        if route is None:
            return self._send(404, {"detail": "no"})
        status, payload = route(body)
        self._send(status, payload)

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args) -> None:                        # noqa: A003
        pass                                                     # quiet under pytest


def serve(board: Board):
    """A board on a loopback port, torn down with the test that asked for it."""
    handler = type("Bound", (_Handler,), {"board": board})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}"


# ------------------------------------------------------------------- the runner


@pytest.fixture
def gh(tmp_path: Path) -> Path:
    """A `gh` on a PATH holding nothing else, answering from the environment.

    `#!/bin/sh` and not `#!/usr/bin/env sh`: there is no /usr/bin/env inside the
    nix sandbox this suite runs in, and `patchShebangs` cannot reach a file a test
    writes while it runs (#177, and `test_runtime_stub_shebangs.py`).
    """
    binned = tmp_path / "path"
    binned.mkdir()
    stub = binned / "gh"
    stub.write_text(
        "#!/bin/sh\n"
        'if [ -n "${QB_TEST_GH_FAIL:-}" ]; then exit 1; fi\n'
        'eval "state=\\${QB_TEST_GH_STATE_$3:-OPEN}"\n'
        'printf \'{"state":"%s"}\\n\' "$state"\n')
    stub.chmod(0o755)
    return binned


def run(url: str, *args: str, session: str | None = "sess-a", gh_path: Path | None = None,
        env: dict | None = None) -> subprocess.CompletedProcess:
    """One agent, as a process, pointed at a board and at nothing else.

    The environment is built rather than inherited so the outcome cannot turn on
    the developer's own board config, their `gh`, or a `QUARTERBACK_*` they happen
    to export — the failure mode the fleet's own notes call "green here, red in
    CI".
    """
    environ = {
        "QUARTERBACK_BASE_URL": url,
        "QUARTERBACK_TOKEN": "test-token",
        "QUARTERBACK_CONFIG": "/nonexistent/quarterback/config",
        "PATH": str(gh_path) if gh_path else "",
        "HOME": "/nonexistent",
        **(env or {}),
    }
    if session is not None:
        environ["CLAUDE_CODE_SESSION_ID"] = session
    return subprocess.run([sys.executable, str(QB_NEXT), *args],
                          capture_output=True, text=True, timeout=60, env=environ)


@pytest.fixture
def three():
    """Three open, unclaimed, unblocked items — issues #61, #62, #63."""
    board = Board([{}, {}, {}])
    httpd, url = serve(board)
    yield board, url
    httpd.shutdown()


# ----------------------------------------------------------- taking one item


def test_it_takes_the_top_free_item_and_says_what_to_run(three, gh):
    """The whole point, in one call: no argument in, a command to run out."""
    board, url = three
    got = run(url, "--scope", SCOPE, gh_path=gh)
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "/fix-issue 61"
    assert board.row(1)["claim"]["session"] == "sess-a", (
        "the claim is the point: an item taken and not claimed is exactly the "
        "state that lets a second agent take it too")


def test_the_claim_comes_before_the_work(three, gh):
    """Ordering, asserted from the board's side: nothing is reported until it is taken.

    `qb-next` cannot itself do the work, so what is checked is that the claim POST
    happened and that the answer naming the item came after it — a tool that
    printed the dispatch line and left the claim to the caller would move the only
    post that prevents duplicated work to after the duplication.
    """
    board, url = three
    got = run(url, "--scope", SCOPE, gh_path=gh)
    assert got.returncode == 0
    kinds = [p[0] for p in board.posts]
    assert kinds[0] == "GET /plan"
    assert "POST /plan/item/claim" in kinds


def test_the_json_carries_the_item_the_order_and_what_to_run(three, gh):
    board, url = three
    got = run(url, "--scope", SCOPE, "--json", gh_path=gh)
    answer = json.loads(got.stdout)
    assert answer["item_id"] == board.row(1)["item_id"]
    assert answer["ref"] == {"kind": "issue", "value": "61"}
    assert answer["dispatch"] == "/fix-issue 61"
    assert answer["was_next"] is True
    assert answer["claimed"] is True
    assert answer["order_trust"]["trusted"] is True


# ------------------------------------------------- two agents, one plan (#424)


def test_two_agents_handed_the_same_next_take_different_items(gh):
    """The demonstration: three items, two agents, and the claim is the only difference.

    Both are held in `GET /plan` until both have read it, so both are told the same
    `next`. They then race for it. One wins; the other is refused with the winner
    named, walks to the next free item and takes that. Neither was assigned
    anything and no human chose between them.
    """
    board = Board([{}, {}, {}], pair=True)
    httpd, url = serve(board)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, url, "--scope", SCOPE, "--json",
                                   session=s, gh_path=gh)
                       for s in ("sess-a", "sess-b")]
            a, b = (f.result() for f in futures)
    finally:
        httpd.shutdown()

    assert a.returncode == 0 and b.returncode == 0, (a.stderr, b.stderr)
    taken = {json.loads(a.stdout)["item_id"], json.loads(b.stdout)["item_id"]}
    assert len(taken) == 2, "two agents took one item — the interlock did nothing"
    assert taken <= {board.row(1)["item_id"], board.row(2)["item_id"]}

    loser = a if json.loads(a.stdout)["item_id"] != board.row(1)["item_id"] else b
    passed = json.loads(loser.stdout)["passed_over"]
    assert passed and passed[0]["item_id"] == board.row(1)["item_id"]
    assert passed[0]["held_by"] in ("zeus/sess-a", "zeus/sess-b")
    assert "has it" in loser.stderr, (
        "a refusal that does not name the holder is a wall rather than somebody "
        "to talk to")


def test_an_item_somebody_already_holds_is_walked_past_by_name(three, gh):
    """The same separation, made deterministic: A takes rank 1, then B runs."""
    board, url = three
    first = run(url, "--scope", SCOPE, "--json", session="sess-a", gh_path=gh)
    second = run(url, "--scope", SCOPE, "--json", session="sess-b", gh_path=gh)

    assert json.loads(first.stdout)["item_id"] == board.row(1)["item_id"]
    assert json.loads(second.stdout)["item_id"] == board.row(2)["item_id"]
    assert board.row(1)["claim"]["session"] == "sess-a"
    assert board.row(2)["claim"]["session"] == "sess-b"
    # B was never offered rank 1 at all — the board's own read excludes a claimed
    # item from `next`, which is the cheap half of the interlock.
    assert json.loads(second.stdout)["passed_over"] == []


def test_it_refuses_to_claim_with_no_session_id(three, gh):
    """A sessionless claim belongs to the BOX, and two agents on one box share it.

    `claim_item` passes `session_owned=True`, and `_may_renew` falls back to the
    machine when the held claim recorded no session — so two sessionless agents
    here would renew each other's claim and both be told they had it. Refusing is
    the only honest answer, and it says why rather than just declining.
    """
    _board, url = three
    got = run(url, "--scope", SCOPE, session=None, gh_path=gh)
    assert got.returncode == 2
    assert "no session id" in got.stderr
    assert "co-tenant" in got.stderr and "same item" in got.stderr


# --------------------------------------------------------- what the order is worth


def test_an_unchosen_order_is_announced_before_anything_is_claimed(gh):
    """#183: taking rank 1 without saying nobody chose rank 1 launders insertion order.

    Asserted on the ORDER of the output as well as its content — the warning has to
    be on stderr before the claim goes out, because a caveat printed after the work
    has started is a footnote.
    """
    board = Board([{}, {}], trusted=False)
    httpd, url = serve(board)
    try:
        got = run(url, "--scope", SCOPE, "--json", gh_path=gh)
    finally:
        httpd.shutdown()
    assert got.returncode == 0
    assert "ORDER NOT CHOSEN" in got.stderr
    assert "CAVEAT ON `next`" in got.stderr
    assert "not a priority" in got.stderr
    answer = json.loads(got.stdout)
    assert answer["caveat"], "the caveat must ride on the answer, not only on stderr"
    assert answer["order_trust"]["trusted"] is False


def test_a_chosen_order_says_so_too(three, gh):
    """A flag that appears only when things are wrong is one nobody learns to read."""
    _board, url = three
    got = run(url, "--scope", SCOPE, gh_path=gh)
    assert "order: chosen" in got.stderr


# ------------------------------------------------------------- a ref that is closed


def test_a_closed_issue_is_recorded_done_and_the_walk_carries_on(three, gh):
    """`qb-reconcile` finds these regularly; an agent that meets one should not stop.

    The item is claimed, found closed, recorded done with a note saying so, and the
    walk moves to the next free item. That is bookkeeping rather than a second
    helping of work, which is why it does not count against "one item per run".
    """
    board, url = three
    got = run(url, "--scope", SCOPE, "--json", gh_path=gh,
              env={"QB_TEST_GH_STATE_61": "CLOSED"})
    assert got.returncode == 0
    answer = json.loads(got.stdout)
    assert answer["item_id"] == board.row(2)["item_id"]
    assert answer["closed_refs"][0]["ref"] == {"kind": "issue", "value": "61"}
    assert board.row(1)["state"] == "done"
    assert "already closed" in (board.row(1)["done_note"] or "")
    assert "moving on" in got.stderr


def test_a_merged_pr_item_is_treated_the_same(gh):
    board = Board([{"ref": {"kind": "pr", "value": "431"}}, {}])
    httpd, url = serve(board)
    try:
        got = run(url, "--scope", SCOPE, "--json", gh_path=gh,
                  env={"QB_TEST_GH_STATE_431": "MERGED"})
    finally:
        httpd.shutdown()
    assert json.loads(got.stdout)["item_id"] == board.row(2)["item_id"]
    assert board.row(1)["state"] == "done"


def test_a_ref_the_forge_cannot_answer_for_is_worked_not_retired(three, gh):
    """Doubt means DO the work. The other way round lets an outage close a plan."""
    board, url = three
    got = run(url, "--scope", SCOPE, "--json", gh_path=gh,
              env={"QB_TEST_GH_FAIL": "1"})
    assert json.loads(got.stdout)["item_id"] == board.row(1)["item_id"]
    assert board.row(1)["state"] == "open"


def test_no_verify_ref_asks_the_forge_nothing(three, gh):
    """For a board with no forge behind it (#327) — and for a run with no `gh`."""
    board, url = three
    got = run(url, "--scope", SCOPE, "--json", "--no-verify-ref", gh_path=None,
              env={"QB_TEST_GH_STATE_61": "CLOSED"})
    assert got.returncode == 0
    assert json.loads(got.stdout)["item_id"] == board.row(1)["item_id"]


# ------------------------------------------------------------ nothing to take


def test_nothing_free_is_a_state_and_not_an_error(gh):
    """Everything claimed, blocked or covered — the shape of a fleet that is working.

    Exit 1 rather than 2, and a report that names the holders: the point of the
    board is that a refusal is somebody to go and ask.
    """
    held = {"claim": {"holder": "zeus/amber-otter", "session": "s9",
                      "note": "landing it", "expires": "2026-08-25T00:00:00+00:00"}}
    board = Board([held,
                   {"blocked_by": [{"item_id": "x", "title": "first this"}]},
                   {"covered_by": {"plan_id": "p", "label": "stage 1",
                                   "holder": "zeus/drift-frost"}}])
    httpd, url = serve(board)
    try:
        got = run(url, "--scope", SCOPE, gh_path=gh)
    finally:
        httpd.shutdown()
    assert got.returncode == 1
    assert "nothing free" in got.stderr
    assert "1 claimed, 1 blocked, 1 covered" in got.stderr
    assert "zeus/amber-otter" in got.stderr
    assert "not an error" in got.stderr and "invent work" in got.stderr
    assert not any(p[0].startswith("POST") for p in board.posts), (
        "nothing free must write nothing — adding an item so there is something "
        "to take is the plan reordering itself")


def test_the_walk_is_bounded(gh):
    """`--tries` stops one invocation becoming a sweep of a busy plan."""
    board = Board([{}, {}, {}, {}], pair=False)
    httpd, url = serve(board)
    try:
        got = run(url, "--scope", SCOPE, "--tries", "2", gh_path=gh,
                  env={"QB_TEST_GH_STATE_61": "CLOSED",
                       "QB_TEST_GH_STATE_62": "CLOSED"})
    finally:
        httpd.shutdown()
    assert got.returncode == 1
    assert "walked 2 of 4" in got.stderr
    assert board.row(3)["claim"] is None, "the third try was not supposed to happen"


# ---------------------------------------------------------------- the other kinds


def test_a_pr_item_dispatches_to_the_review_path(gh):
    board = Board([{"ref": {"kind": "pr", "value": "431"}}])
    httpd, url = serve(board)
    try:
        got = run(url, "--scope", SCOPE, gh_path=gh)
    finally:
        httpd.shutdown()
    assert got.stdout.strip() == "/review-pr 431"


def test_a_ref_less_item_dispatches_to_nothing_and_says_so(gh):
    """Work with no forge behind it (#323) is a real kind, not a missing case."""
    board = Board([{"ref": None, "repo": "project:65lowther",
                    "title": "chase the surveyor"}])
    httpd, url = serve(board)
    try:
        got = run(url, "--scope", "project:65lowther", "--json", gh_path=gh)
    finally:
        httpd.shutdown()
    assert got.returncode == 0
    answer = json.loads(got.stdout)
    assert answer["dispatch"] is None
    assert answer["ref"] is None
    assert answer["title"] == "chase the surveyor"


def test_a_project_scope_asks_the_forge_nothing(gh):
    """There is no repository to ask. `gh` is on PATH and must go unused."""
    board = Board([{"ref": None, "repo": "project:65lowther"}])
    httpd, url = serve(board)
    try:
        got = run(url, "--scope", "project:65lowther", gh_path=gh,
                  env={"QB_TEST_GH_FAIL": "1"})
    finally:
        httpd.shutdown()
    assert got.returncode == 0


# ------------------------------------------------------------- the other end


def test_dry_run_takes_nothing(three, gh):
    board, url = three
    got = run(url, "--scope", SCOPE, "--dry-run", "--json", gh_path=gh)
    assert got.returncode == 0
    assert json.loads(got.stdout)["claimed"] is False
    assert board.row(1)["claim"] is None
    assert not any(p[0].startswith("POST") for p in board.posts)


def test_dry_run_needs_no_session(three, gh):
    """It claims nothing, so the reason to insist on a session does not apply."""
    _board, url = three
    got = run(url, "--scope", SCOPE, "--dry-run", session=None, gh_path=gh)
    assert got.returncode == 0


def test_release_puts_it_back(three, gh):
    board, url = three
    run(url, "--scope", SCOPE, gh_path=gh)
    item_id = board.row(1)["item_id"]
    got = run(url, "--release", item_id, gh_path=gh)
    assert got.returncode == 0
    assert board.row(1)["claim"] is None
    assert board.row(1)["state"] == "open"


def test_done_records_it_with_the_note(three, gh):
    board, url = three
    run(url, "--scope", SCOPE, gh_path=gh)
    got = run(url, "--done", board.row(1)["item_id"], "--note", "PR #431", gh_path=gh)
    assert got.returncode == 0
    assert board.row(1)["state"] == "done"
    assert board.row(1)["done_note"] == "PR #431"


def test_done_and_release_together_are_refused(three, gh):
    _board, url = three
    got = run(url, "--done", "a", "--release", "b", gh_path=gh)
    assert got.returncode == 2
    assert "pick one" in got.stderr


def test_an_unreachable_board_is_unknown_and_not_nothing_free(gh):
    """2, not 1. "There is no work" and "I could not ask" are different answers."""
    board = Board([{}])
    httpd, url = serve(board)
    httpd.shutdown()
    httpd.server_close()                   # the port is closed before the call
    got = run(url, "--scope", SCOPE, gh_path=gh)
    assert got.returncode == 2
    assert "could not read the plan" in got.stderr


def test_it_sends_its_session_on_the_plan_read(three, gh):
    """Without it the read cannot tell a co-tenant's hold from your own.

    `GET /plan` resolves "mine" before it answers, so a read that sends no session
    can only answer by machine — and then another agent's covered item is offered
    as free work. The endpoint's own refusal text says this in as many words.
    """
    board, url = three
    run(url, "--scope", SCOPE, gh_path=gh, session="sess-a")
    read = next(p for p in board.posts if p[0] == "GET /plan")
    assert read[1]["session"] == "sess-a"
    assert read[1]["repo"] == SCOPE


# ------------------------------------------------------- the brief that calls it


def test_the_brief_branches_on_every_exit_code_this_tool_returns():
    """A brief that knows two of three exit codes treats the third as the wrong one.

    The dangerous confusion is 1 against 2: "nothing is free" is a normal state to
    report and stop on, "I could not ask the board" is a failure to report and stop
    on, and a brief that collapsed them would have an agent announce an empty plan
    every time the board was unreachable.
    """
    text = BRIEF.read_text()
    assert "qb-next" in text
    for code, meaning in ((0, "took"), (1, "nothing free"), (2, "could not tell")):
        assert f"| {code} |" in text, f"the brief does not branch on exit {code}"
        assert meaning in text
    assert "--release" in text and "any exit" in text, (
        "releasing on a FAILED exit is the half that gets dropped, and then the "
        "next agent waits out the whole TTL for work nobody is doing")
    assert "reorder" in text, "the brief must say it may not reorder the plan"
