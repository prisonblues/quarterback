"""`qb-line` — the sensor half of #435, driven against a real board socket.

#435 asked for a driver that enumerates a repo's open PRs and ENQUEUES them. #476
supersedes exactly that half, and its argument is that a central drainer is the
shape this codebase has refused four separate times in its own docstrings. What
#476 does not supersede is #435's last paragraph:

    Blind spots are the interesting output. On a backlog assembled over weeks,
    most PRs will have no attested file list, and the report naming them is what
    turns "the order is null" into a work list.

So what is under test is a READ. The board already computes an order and already
names its blind spots — over the PRs whose agent happened to run `/fix-and-land`
step 4a, which on a real backlog is four of thirty-six. This walks the open PRs
instead of the queued ones, which is why the tiering lives here and not in
`app/api/merge_queue._blind_spots`.

**The board is a real HTTP server on a real socket and `qb-line` is a real
process**, on `test_qb_next.py`'s pattern and for its reason: the thing worth
pinning is what this tool does with the answers the board really gives — a 404, a
run that recorded nothing, a file list belonging to a commit the branch has left —
and a double that agreed with the test instead of with the server would assert none
of it.

**`gh` is a stub on a PATH holding nothing else**, so whether a PR is listed cannot
depend on whether the developer running the suite has `gh` installed and
authenticated.

Run: pytest harness/tests/test_qb_line.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

HARNESS = Path(__file__).resolve().parents[1]
QB_LINE = HARNESS / "bin" / "qb-line"

REPO = "acme/widget"
HEAD_A = "a" * 40
HEAD_B = "b" * 40


# --------------------------------------------------------------------- the board


class Board:
    """The two GETs this tool makes, and a record of every request that arrived.

    `runs` and `collisions` are keyed by PR so one board can hold a backlog with a
    different fault on every row — which is the shape the report exists to describe
    and the shape a single-PR fixture cannot produce.
    """

    def __init__(self, runs: dict[int, dict | list | None],
                 collisions: dict[int, dict | int]) -> None:
        #: A row or a LIST of rows per PR — `/reviews` is a page, and the run that
        #: answers a collisions query need not be the newest one on it.
        self.runs = runs
        #: A dict is a body; an int is a STATUS — 404 for "no run of this PR
        #: recorded a changed-file list", which is what the real endpoint raises
        #: and is the commonest answer on an old backlog.
        self.collisions = collisions
        self.requests: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def record(self, method: str, path: str, query: dict) -> None:
        with self.lock:
            self.requests.append((f"{method} {path}", query))

    @property
    def writes(self) -> list[tuple[str, dict]]:
        return [r for r in self.requests if not r[0].startswith("GET ")]


def run_row(pr: int, head: str | None = HEAD_A, reviewed: bool = True,
            skip_reason: str | None = None, run_id: int | None = None) -> dict:
    return {"id": run_id if run_id is not None else 1000 + pr, "repo": REPO, "pr": pr,
            "head_sha": head, "reviewed": reviewed, "skip_reason": skip_reason}


def collision_row(pr: int, run_id: int | None = None, recorded: int = 3,
                  total: int | None = 3, complete: bool = True,
                  collides: int = 1, disjoint: int = 2) -> dict:
    """The collisions body **exactly as the real endpoint sends it — WITH NO HEAD**.

    This is the correction that matters most in this file. A first cut of the stub
    invented a `head_sha` here, the tool read `coll["head_sha"]`, and the staleness
    test passed against a field `app/api/reviews.py` does not publish: its response
    carries `run_id`, `ts`, `pr_state`, `files_recorded`, the five classes and no
    head at all. The suite was agreeing with itself rather than with the server —
    which is the one failure this file's whole real-socket design exists to prevent,
    reintroduced through the fixture. So the keys here are the endpoint's keys, and
    which commit a run belongs to is answered where the board really answers it, in
    `/reviews`.
    """
    return {"repo": REPO, "pr": pr, "run_id": run_id if run_id is not None else 1000 + pr,
            "ts": "2026-08-26T00:00:00+00:00", "pr_state": "OPEN", "is_draft": False,
            "reviewed": True, "skip_reason": None,
            "files_recorded": recorded, "changed_files_total": total,
            "files_complete": complete,
            "counts": {"considered": collides + disjoint, "collides": collides,
                       "partial": 0, "disjoint": disjoint, "unanswerable": 0,
                       "excluded": 0},
            "scope": "PRs this board has panelled within the window"}


class _Handler(BaseHTTPRequestHandler):
    board: Board

    def do_GET(self) -> None:                                    # noqa: N802
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.board.record("GET", parsed.path, query)
        pr = int(query.get("pr", 0))
        if parsed.path == "/reviews":
            got = self.board.runs.get(pr)
            if got is None:
                return self._send(200, [])
            return self._send(200, got if isinstance(got, list) else [got])
        if parsed.path == "/review/collisions":
            got = self.board.collisions.get(pr)
            if isinstance(got, int):
                return self._send(got, {"detail": f"no run of {REPO}#{pr} recorded "
                                                  "a changed-file list"})
            if got is None:
                return self._send(404, {"detail": "nothing to compare"})
            return self._send(200, got)
        return self._send(404, {"detail": "no"})

    def do_POST(self) -> None:                                   # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.board.record("POST", urlparse(self.path).path, body)
        self._send(404, {"detail": "this tool has no business posting"})

    def _send(self, status: int, payload) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args) -> None:                        # noqa: A003
        pass


def serve(board: Board):
    handler = type("Bound", (_Handler,), {"board": board})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}"


# ------------------------------------------------------------------- the runner


def write_gh(binned: Path, prs: list[dict]) -> None:
    """A `gh` that answers `pr list` with these rows, using SHELL BUILTINS ONLY.

    PATH holds this directory and nothing else — that is the point of the stub, so
    the answer cannot depend on the developer's own `gh` — which means `cat`, `jq`
    and friends are not there either. The rows are therefore baked into the script
    and printed with `printf`, and the single-quote escape is not decoration: a PR
    title is arbitrary text arriving inside a single-quoted shell literal.
    """
    payload = json.dumps(prs).replace("'", "'\\''")
    stub = binned / "gh"
    stub.write_text(
        "#!/bin/sh\n"
        'if [ -n "${QB_TEST_GH_FAIL:-}" ]; then echo "gh: boom" >&2; exit 1; fi\n'
        f"printf '%s\\n' '{payload}'\n")
    stub.chmod(0o755)


@pytest.fixture
def gh(tmp_path: Path):
    """A PATH holding one `gh` and nothing else.

    `#!/bin/sh` and not `#!/usr/bin/env sh`: there is no /usr/bin/env inside the
    nix sandbox this suite runs in, and `patchShebangs` cannot reach a file a test
    writes while it runs (#177).
    """
    binned = tmp_path / "path"
    binned.mkdir()
    write_gh(binned, [])
    return binned


def pr_row(number: int, head: str = HEAD_A, draft: bool = False,
           title: str | None = None) -> dict:
    return {"number": number, "title": title or f"pr {number}", "isDraft": draft,
            "headRefOid": head, "baseRefName": "main"}


def run(url: str, gh_path: Path, *args: str, env: dict | None = None):
    environ = {
        "QUARTERBACK_BASE_URL": url,
        "QUARTERBACK_TOKEN": "test-token",
        "QUARTERBACK_CONFIG": "/nonexistent/quarterback/config",
        "PATH": str(gh_path),
        "HOME": "/nonexistent",
        **(env or {}),
    }
    return subprocess.run([sys.executable, str(QB_LINE), "--repo", REPO, *args],
                          capture_output=True, text=True, timeout=120, env=environ)


@pytest.fixture
def board_of(gh):
    """Build a board and a PR list together — they have to agree about the backlog."""
    binned = gh
    httpds = []

    def make(prs: list[dict], runs: dict, collisions: dict):
        write_gh(binned, prs)
        board = Board(runs, collisions)
        httpd, url = serve(board)
        httpds.append(httpd)
        return board, url, binned

    yield make
    for h in httpds:
        h.shutdown()


# ------------------------------------------------------------------ the tiers


def test_a_pr_the_board_has_never_seen_is_never_panelled(board_of):
    """The worst tier, and the one a queue-side report cannot produce at all: a PR
    that never enqueued and never panelled is in no queue row and in no rival class,
    so nothing anywhere currently names it."""
    board, url, gh_path = board_of([pr_row(7)], {}, {})
    got = run(url, gh_path, "--json")
    assert got.returncode == 0, got.stderr
    row = json.loads(got.stdout)["prs"][0]
    assert row["tier"] == "never-panelled"
    assert "run a panel round" in row["fix"]


def test_a_404_from_collisions_is_a_FINDING_and_not_a_crash(board_of):
    """The load-bearing one. `/review/collisions` raises 404 with "no run of this PR
    recorded a changed-file list — nothing to compare", and on a backlog assembled
    over weeks that is the commonest answer there is. A report that treated it as an
    error would die on the first PR it was written to describe."""
    board, url, gh_path = board_of(
        [pr_row(7)], {7: run_row(7)}, {7: 404})
    got = run(url, gh_path, "--json")
    assert got.returncode == 0, got.stderr
    row = json.loads(got.stdout)["prs"][0]
    assert row["tier"] == "no-file-list"
    assert "#94" in row["fix"]


def test_evidence_for_a_commit_the_branch_has_left_is_stale(board_of):
    """A complete file list of the wrong commit is not evidence about what would
    land. `_blind_spots` calls this `evidence-not-at-head` and says the same thing
    of a queued row; the fault does not become different because nobody queued it."""
    board, url, gh_path = board_of(
        [pr_row(7, head=HEAD_B)],
        {7: [run_row(7, head=HEAD_A, run_id=1007)]},
        {7: collision_row(7, run_id=1007)})
    row = json.loads(run(url, gh_path, "--json").stdout)["prs"][0]
    assert row["tier"] == "stale-evidence"
    assert row["run_head"] == HEAD_A and row["head"] == HEAD_B


def test_a_listless_NEWEST_run_is_blind_even_though_collisions_can_answer(board_of):
    """The correction Codex found, and the one that shapes the whole tool.

    `/review/collisions` reaches back past its window for the newest run BEARING A
    FILE LIST, so it answers happily here. The RANKER does not: `merge_queue` takes
    one unconditional `DISTINCT ON (pr) ORDER BY ts DESC` with no file-list
    predicate, because "a run that recorded no paths must come back as 0 and stay in
    the population — it is precisely the row whose absence would read as 'answered,
    and disjoint'".

    So following collisions would report this PR `orderable` while the queue counts
    it a blind spot. This report exists to say what the ranker could order, so it
    answers the ranker's question.
    """
    board, url, gh_path = board_of(
        [pr_row(7, head=HEAD_A)],
        # Newest first: the newest run bears no list, an older one does.
        {7: [run_row(7, head=HEAD_A, run_id=99),
             run_row(7, head=HEAD_A, run_id=1007)]},
        {7: collision_row(7, run_id=1007)})       # collisions answered from the OLD run
    out = json.loads(run(url, gh_path, "--json").stdout)
    assert out["prs"][0]["tier"] == "no-file-list"
    assert out["orderable"] == 0


def test_a_retargeted_pr_is_stale_even_at_the_same_commit(board_of):
    """`app.ranking.Candidate.pinned` compares head AND base, and the ranker's query
    selects `base_branch` beside `head_sha` for exactly this reason: a PR retargeted
    to another base is a different diff at the same commit."""
    board, url, gh_path = board_of(
        [pr_row(7, head=HEAD_A)],
        {7: [dict(run_row(7, head=HEAD_A, run_id=1007), base="release")]},
        {7: collision_row(7, run_id=1007)})
    row = json.loads(run(url, gh_path, "--json").stdout)["prs"][0]
    assert row["tier"] == "stale-evidence"


def test_a_run_that_recorded_no_base_is_still_orderable(board_of):
    """Deliberate, and it mirrors the ranker rather than being stricter than it.

    `app.ranking.Candidate.pinned` compares the base "only when the run recorded one
    — it is nullable for the same reason `head_sha` is, and a PR that never moved
    bases is the overwhelmingly common case". A missing base is therefore not a
    failed pin there, and must not be a blind spot here: this report's whole value is
    that its answer is the queue's answer.

    Raised as a possible false all-clear on review and kept, with the ranker's own
    sentence as the reason — the next reader will have the same instinct, and the
    place to change it is `pinned`, not this file.
    """
    board, url, gh_path = board_of(
        [pr_row(7, head=HEAD_A)],
        {7: [dict(run_row(7, head=HEAD_A, run_id=1007), base=None)]},
        {7: collision_row(7, run_id=1007)})
    assert json.loads(run(url, gh_path, "--json").stdout)["prs"][0]["tier"] == "orderable"


def test_counts_that_contradict_themselves_are_not_trusted(board_of):
    """`_blind_spots`' `inconsistent-counts`, in its own words: "the answering run
    stored more paths than its own changed-file count admits to". The queue does not
    trust such a row, so neither may this."""
    board, url, gh_path = board_of(
        [pr_row(7, head=HEAD_A)], {7: [run_row(7, head=HEAD_A, run_id=1007)]},
        {7: collision_row(7, run_id=1007, recorded=9, total=3)})
    out = json.loads(run(url, gh_path, "--json").stdout)
    assert out["prs"][0]["tier"] == "inconsistent-counts"
    assert out["blind"] == 1
    assert "re-record the run" in out["prs"][0]["fix"]


def test_evidence_whose_commit_cannot_be_established_is_NOT_called_orderable(board_of):
    """Found by Codex, and it is the sharpest defect this tool had. The collisions
    response carries no head, so a first cut fell back to the NEWEST run's — which
    calls stale evidence CURRENT whenever the newest run happens to sit at the head.
    `orderable` is the only tier here that is a safety claim, so it may not rest on a
    guess.

    The answering run falling outside the `/reviews` page is the honest version of
    the same gap: a file list exists, and which commit it describes is unknown. That
    is a blind spot with a repair, not an all-clear."""
    board, url, gh_path = board_of(
        # `gh` gave no headRefOid, so there is nothing to compare the evidence to.
        [dict(pr_row(7), headRefOid=None)],
        {7: [run_row(7, head=HEAD_A, run_id=1007)]},
        {7: collision_row(7, run_id=1007)})
    out = json.loads(run(url, gh_path, "--json").stdout)
    assert out["prs"][0]["tier"] == "head-unknown"
    assert out["blind"] == 1 and out["orderable"] == 0
    assert "could not be established" in out["prs"][0]["fix"]


def test_a_run_with_no_head_sha_at_all_is_head_unknown(board_of):
    """Every pre-v2.26 row has a null `head_sha` — `head_sha` is the column that
    version added. Comparing None against a real head and calling the result a match
    would be the same false all-clear by another route."""
    board, url, gh_path = board_of(
        [pr_row(7, head=HEAD_A)],
        {7: [run_row(7, head=None, run_id=1007)]},
        {7: collision_row(7, run_id=1007)})
    row = json.loads(run(url, gh_path, "--json").stdout)["prs"][0]
    assert row["tier"] == "head-unknown"


def test_a_prefix_list_is_a_floor_and_is_not_counted_blind(board_of):
    """GitHub caps a PR's file list at 3,000. `_blind_spots` says of this fault
    "nothing to do here", and it is a WEAKER claim rather than an absent one — a
    shared-path count computed from a prefix is a floor. Counting it blind would put
    an unfixable item on a work list whose whole purpose is to be actionable."""
    board, url, gh_path = board_of(
        [pr_row(7)], {7: run_row(7)},
        {7: collision_row(7, recorded=3000, total=4200, complete=False)})
    out = json.loads(run(url, gh_path, "--json").stdout)
    assert out["prs"][0]["tier"] == "prefix-list"
    assert out["blind"] == 0 and out["orderable"] == 1
    assert "nothing to do" in out["prs"][0]["fix"]


def test_a_complete_list_at_the_head_is_orderable(board_of):
    board, url, gh_path = board_of(
        [pr_row(7)], {7: run_row(7)}, {7: collision_row(7)})
    row = json.loads(run(url, gh_path, "--json").stdout)["prs"][0]
    assert row["tier"] == "orderable" and row["fix"] == ""
    assert row["collisions"]["collides"] == 1


# -------------------------------------------------- what the board is asked


def test_the_run_lookup_asks_for_unreviewed_runs_too(board_of):
    """A title-skipped merge records a run whose entire purpose is the changed-file
    list it carries, and `/reviews` hides exactly those by default (#94). Asking
    without `include_unreviewed` would report a PR that HAS evidence as
    never-panelled — the worst tier — and a work list with an invented item on it is
    worse than a short one."""
    board, url, gh_path = board_of(
        [pr_row(7)], {7: run_row(7, reviewed=False, skip_reason="merge title")},
        {7: collision_row(7)})
    assert json.loads(run(url, gh_path, "--json").stdout)["prs"][0]["tier"] == "orderable"
    asked = [q for path, q in board.requests if path == "GET /reviews"]
    assert asked and asked[0].get("include_unreviewed") == "true"


def test_it_writes_NOTHING(board_of):
    """#476's whole objection, asserted from the board's side rather than promised in
    a docstring. Every request this tool makes is a GET; the day somebody adds an
    enqueue to it, this fails."""
    board, url, gh_path = board_of(
        [pr_row(7), pr_row(8)], {7: run_row(7), 8: run_row(8)},
        {7: collision_row(7), 8: 404})
    assert run(url, gh_path, "--json").returncode == 0
    assert board.writes == []
    assert all(p.startswith("GET ") for p, _ in board.requests)


def test_the_payload_says_it_did_not_act(board_of):
    """This fleet's rule: a caveat a caller cannot discover from the numbers rides
    with them. A consumer must not have to infer from the absence of a queue field
    that no queue was formed."""
    board, url, gh_path = board_of([pr_row(7)], {7: run_row(7)}, {7: collision_row(7)})
    out = json.loads(run(url, gh_path, "--json").stdout)
    assert out["acted"] is False
    assert "panelled" in out["scope"]


# ------------------------------------------------------------ the whole backlog


@pytest.fixture
def backlog(board_of):
    """Five PRs, one per tier plus a second blind one — the shape #435 describes."""
    prs = [pr_row(1), pr_row(2), pr_row(3, head=HEAD_B), pr_row(4), pr_row(5)]
    runs = {1: run_row(1, run_id=1001), 2: run_row(2, run_id=1002),
            # #3's evidence sits at HEAD_A while the branch has moved to HEAD_B.
            3: [run_row(3, head=HEAD_A, run_id=1003)],
            4: run_row(4, run_id=1004)}
    collisions = {1: collision_row(1, run_id=1001), 2: 404,
                  3: collision_row(3, run_id=1003),
                  4: collision_row(4, run_id=1004, recorded=3000, total=4200,
                                   complete=False)}
    return board_of(prs, runs, collisions)


def test_the_headline_is_how_much_of_the_backlog_could_be_ORDERED(backlog):
    """The number #435 says nobody has ever seen. Not how good the order is — how
    much of a real backlog it could be computed over at all."""
    board, url, gh_path = backlog
    out = json.loads(run(url, gh_path, "--json").stdout)
    assert out["open"] == 5
    assert out["counts"] == {"never-panelled": 1, "no-file-list": 1,
                             "inconsistent-counts": 0, "stale-evidence": 1,
                             "head-unknown": 0, "prefix-list": 1, "orderable": 1}
    assert out["blind"] == 3 and out["orderable"] == 2


def test_the_report_says_the_headline_and_the_refusal_in_words(backlog):
    """The text half, because the reader is a human deciding what to go and fix."""
    board, url, gh_path = backlog
    got = run(url, gh_path)
    assert got.returncode == 0, got.stderr
    assert "2 of 5 could be ORDERED today" in got.stdout
    assert "3 blind" in got.stdout
    assert "forms no queue, enqueues nothing and merges nothing" in got.stdout


def test_an_entirely_blind_backlog_is_a_FINDING_not_a_failure(board_of):
    """The state #435 was filed about — nothing attested anywhere — has to come back
    as a report somebody can work through, with exit 0. A non-zero here would make
    the tool look broken at exactly the moment it has the most to say."""
    board, url, gh_path = board_of(
        [pr_row(1), pr_row(2)], {}, {})
    got = run(url, gh_path, "--json")
    assert got.returncode == 0, got.stderr
    out = json.loads(got.stdout)
    assert out["orderable"] == 0 and out["blind"] == 2


def test_drafts_are_listed_and_flagged_never_filtered(board_of):
    """A draft's evidence is as stale or as missing as anyone's, and hiding them
    would under-count the work list. `preland` is what refuses to LAND one; this
    reports."""
    board, url, gh_path = board_of(
        [pr_row(7, draft=True)], {7: run_row(7)}, {7: collision_row(7)})
    out = json.loads(run(url, gh_path, "--json").stdout)
    assert out["prs"][0]["draft"] is True
    assert "[draft]" in run(url, gh_path).stdout


def test_no_open_prs_is_said_plainly(board_of):
    board, url, gh_path = board_of([], {}, {})
    got = run(url, gh_path)
    assert got.returncode == 0
    assert "no open pull requests" in got.stdout


def test_a_backlog_over_the_cap_says_so_rather_than_reporting_a_prefix(board_of):
    """Found by Codex on review. The headline of this report is a FRACTION, so a
    silently truncated denominator makes it read BETTER than the truth — the one
    direction a report about unfinished work must never be wrong in.

    `gh` does not say whether more rows existed, so the tool asks for one more than
    it means to use; getting it back is the only evidence available that the answer
    is a prefix."""
    over = [pr_row(n) for n in range(1, 203)]              # 202 > PR_LIMIT
    board, url, gh_path = board_of(over, {}, {})
    out = json.loads(run(url, gh_path, "--json").stdout)
    assert out["truncated"] is True
    assert out["open"] == out["limit"] == 200, "the extra row is evidence, not data"
    assert "MORE than 200 open PRs" in run(url, gh_path).stdout


def test_a_backlog_at_the_cap_exactly_is_not_called_truncated(board_of):
    """The off-by-one that would make the warning fire on every full-but-complete
    backlog, which is how a caveat gets learned-past."""
    exact = [pr_row(n) for n in range(1, 201)]             # 200 == PR_LIMIT
    board, url, gh_path = board_of(exact, {}, {})
    out = json.loads(run(url, gh_path, "--json").stdout)
    assert out["truncated"] is False and out["open"] == 200


def test_there_is_no_preland_flag(board_of):
    """It had one, and it could not keep this tool's promise: `preland.py` fetches
    the base branch's remote-tracking ref and `announce_hold` POSTS to the board for
    a HOLD, so a sweep would write once per holding PR. Asserted rather than left to
    the docstring, because the natural thing for the next reader to do with #435's
    step 2 in front of them is add it back."""
    board, url, gh_path = board_of([pr_row(7)], {7: run_row(7)}, {7: collision_row(7)})
    got = run(url, gh_path, "--preland")
    assert got.returncode != 0
    assert "unrecognized arguments" in got.stderr


# ------------------------------------------------------------------ failures


def test_a_broken_gh_is_reported_and_not_rendered_as_an_empty_backlog(board_of):
    """`gh` failing and a repo with nothing open must never look the same. An empty
    render over a failed enumeration is this fleet's absence-vs-inability collapse,
    and here it would report a clean line over a backlog nobody managed to list."""
    board, url, gh_path = board_of([pr_row(7)], {7: run_row(7)}, {7: collision_row(7)})
    got = run(url, gh_path, env={"QB_TEST_GH_FAIL": "1"})
    assert got.returncode == 1
    assert "no open pull requests" not in got.stdout


def test_the_base_filter_reaches_gh_rather_than_being_applied_after(board_of):
    """`--base` narrows the enumeration itself. Filtering after the fact would still
    pay for a board round-trip per PR on a repo draining several bases at once."""
    board, url, gh_path = board_of([pr_row(7)], {7: run_row(7)}, {7: collision_row(7)})
    argv = gh_path / "argv"
    (gh_path / "gh").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{argv}"\n'
        'echo "[]"\n')
    (gh_path / "gh").chmod(0o755)
    assert run(url, gh_path, "--base", "test").returncode == 0
    assert "--base" in argv.read_text() and "test" in argv.read_text()
