"""`qb-backfill` — the collision datum recovered from the forge, and never mistaken for a review (#449).

What is under test is not a formatter. It is one claim: a row written by this tool
must carry a real file list and must be unable to read as a review, and it must say so
when the list it carries is short. Every other property here — the dry run, the
re-run, the refusals — exists to protect that one.

**The board is a real HTTP server on a real socket and `qb-backfill` is a real
process,** for the reason `test_qb_next.py` gives: what is being asserted is a
conversation between two programs, and a suite that stubbed the client would assert
everything about it except the part that matters. What is faked is the storage, which
is a dict, and `gh`, which is a script on a PATH holding nothing else — so a verdict
here does not depend on whether the developer running the suite has GitHub credentials.

Run: pytest harness/tests/test_qb_backfill.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

HARNESS = Path(__file__).resolve().parents[1]
QB_BACKFILL = HARNESS / "bin" / "qb-backfill"

REPO = "acme/widget"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40


# --------------------------------------------------------------------- the board


class Board:
    """The three calls this tool makes, over TCP, with the JSON both ends exchange.

    `runs` is what `GET /reviews` answers with, keyed by PR — the newest run first,
    exactly as the real endpoint orders it. `posted` is every payload `POST /review`
    received, which is what the assertions about "never claims a review" read.
    """

    def __init__(self, runs: dict[int, list[dict]] | None = None,
                 duplicate: bool = False, stored: int | None = None,
                 dropped: dict | None = None) -> None:
        self.lock = threading.Lock()
        self.runs = runs or {}
        #: Answer the next write with the board's duplicate-run_key refusal.
        self.duplicate = duplicate
        #: How many paths the board says it stored, overriding "all of them" — the
        #: dedup and the MAX_CHANGED_FILES cap both produce this.
        self.stored = stored
        self.dropped = dropped
        self.posted: list[dict] = []
        self.detail_reads: list[int] = []

    def reviews(self, params: dict) -> list[dict]:
        pr = int(params.get("pr", ["0"])[0])
        rows = self.runs.get(pr, [])
        return rows[: int(params.get("limit", ["50"])[0])]

    def detail(self, run_id: int) -> dict | None:
        with self.lock:
            self.detail_reads.append(run_id)
        for rows in self.runs.values():
            for r in rows:
                if r["id"] == run_id:
                    return {**r, "changed_files": r.get("_files", [])}
        return None

    def record(self, body: dict) -> dict:
        with self.lock:
            self.posted.append(body)
            if self.duplicate:
                return {"id": 1, "recorded": False, "reason": "duplicate run_key"}
            n = self.stored if self.stored is not None else len(body.get("changed_files", []))
            out = {"id": 900 + len(self.posted), "recorded": True, "findings": 0,
                   "accounts": 0, "changed_files": n}
            if self.dropped:
                out["changed_files_dropped"] = self.dropped
            return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a) -> None:  # keep pytest output readable
        pass

    def _send(self, code: int, body) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        board: Board = self.server.board
        if url.path == "/reviews":
            return self._send(200, board.reviews(parse_qs(url.query)))
        if url.path.startswith("/review/"):
            run = board.detail(int(url.path.rsplit("/", 1)[1]))
            return self._send(200, run) if run else self._send(404, {"detail": "no run"})
        self._send(404, {"detail": "not here"})

    def do_POST(self) -> None:
        url = urlparse(self.path)
        board: Board = self.server.board
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or "{}")
        if url.path == "/review":
            return self._send(201, board.record(body))
        self._send(404, {"detail": "not here"})


@pytest.fixture
def serve():
    started = []

    def _start(board: Board) -> str:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        httpd.board = board
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _start
    for httpd in started:
        httpd.shutdown()


# ------------------------------------------------------------------------ the forge


def gh_stub(tmp_path: Path, prs: list[dict], files: dict[int, list[dict]],
            *, files_fail: set[int] | None = None) -> Path:
    """A `gh` on a PATH holding nothing else, answering the two calls this tool makes.

    `files_fail` makes `gh api --paginate` exit non-zero for those PRs, which is the
    shape of a paged read that died partway — the case whose prefix must never be
    recorded.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (tmp_path / "prs.json").write_text(json.dumps(prs))
    (tmp_path / "files.json").write_text(json.dumps(
        {str(k): v for k, v in files.items()}))
    (tmp_path / "fail.json").write_text(json.dumps(sorted(files_fail or ())))
    # The interpreter by absolute path: PATH here holds `gh` and nothing else, on
    # purpose, so `env python3` in a shebang would not resolve.
    script = f'''#!{sys.executable}
import json, sys, re
root = {str(tmp_path)!r}
argv = sys.argv[1:]
if argv[:2] == ["pr", "list"]:
    print(json.dumps(json.load(open(root + "/prs.json"))))
    raise SystemExit(0)
if argv[:2] == ["api", "--paginate"]:
    pr = int(re.search(r"/pulls/(\\d+)/files", argv[2]).group(1))
    if pr in json.load(open(root + "/fail.json")):
        print("gh: HTTP 502 on page 2 of 4", file=sys.stderr)
        raise SystemExit(1)
    for f in json.load(open(root + "/files.json")).get(str(pr), []):
        print(json.dumps(f))
    raise SystemExit(0)
print("gh stub: unexpected call " + " ".join(argv), file=sys.stderr)
raise SystemExit(3)
'''
    gh = bin_dir / "gh"
    gh.write_text(script)
    gh.chmod(0o755)
    return bin_dir


def pr_meta(number: int, changed: int, *, head: str = HEAD, base: str = "main",
            title: str = "a branch", state: str = "OPEN", draft: bool = False) -> dict:
    return {"number": number, "title": title, "headRefOid": head,
            "baseRefName": base, "baseRefOid": "c" * 40, "changedFiles": changed,
            "additions": 10, "deletions": 2, "state": state, "isDraft": draft}


def paths(*names: str) -> list[dict]:
    return [{"path": n, "additions": 1, "deletions": 0} for n in names]


def run(tmp_path: Path, url: str, bin_dir: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": str(bin_dir), "QUARTERBACK_BASE_URL": url,
           "QUARTERBACK_TOKEN": "t", "QUARTERBACK_TOKEN_CMD": "",
           "QUARTERBACK_CONFIG": str(tmp_path / "nope")}
    return subprocess.run([sys.executable, str(QB_BACKFILL), "--repo", REPO, *args],
                          capture_output=True, text=True, env=env, timeout=90)


# ---------------------------------------------------------------------- the tests


def test_dry_run_writes_nothing_and_says_what_it_would_write(tmp_path, serve):
    board = Board()
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 2)], {7: paths("a.py", "b.py")})

    got = run(tmp_path, url, bin_dir)

    assert got.returncode == 0, got.stderr
    assert board.posted == []
    assert "would-record" in got.stdout
    assert "nothing was written" in got.stdout


def test_a_backfill_row_cannot_be_read_as_a_review(tmp_path, serve):
    """The claim the whole tool rests on, asserted field by field.

    `reviewed: false` and a `skip_reason` that names the backfill are what a reader
    goes on. The absences matter just as much: a single finding beside
    `reviewed: false` makes the board drop the flag to NULL
    (`ReviewIn._no_review_claims_nothing_else`), which would turn this row back into
    one nobody can vouch for — the pre-#94 state it exists to leave.
    """
    board = Board()
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 2)], {7: paths("a.py", "b.py")})

    got = run(tmp_path, url, bin_dir, "--apply")

    assert got.returncode == 0, got.stderr
    (body,) = board.posted
    assert body["reviewed"] is False
    assert "qb-backfill" in body["skip_reason"] and "#449" in body["skip_reason"]
    assert "No reviewer ran" in body["skip_reason"]
    for verdict in ("to_fix", "dismissed", "sonar_findings", "reviewers",
                    "round_stop", "judged", "stop_confident", "stopped"):
        assert verdict not in body, f"{verdict} would make this row read as a review"


def test_the_head_it_read_is_recorded_because_80_pins_on_it(tmp_path, serve):
    board = Board()
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 1, head=OTHER_HEAD)], {7: paths("a.py")})

    run(tmp_path, url, bin_dir, "--apply")

    (body,) = board.posted
    assert body["head_sha"] == OTHER_HEAD
    assert body["base"] == "main"
    assert OTHER_HEAD in body["run_key"]


def test_the_count_is_githubs_never_the_length_of_the_list(tmp_path, serve):
    """The one line that would turn every truncated list into an attested complete one.

    GitHub says nine; the paged read returned two. `changed_files_total` must be the
    nine, because `app.collisions.files_complete` compares the two and a nine that was
    derived from the two agrees with itself by construction.
    """
    board = Board()
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 9)], {7: paths("a.py", "b.py")})

    got = run(tmp_path, url, bin_dir, "--apply")

    (body,) = board.posted
    assert body["changed_files_total"] == 9
    assert len(body["changed_files"]) == 2
    assert got.returncode == 1, "a PR left unattested is not a finished job"


def test_a_short_list_is_reported_as_a_prefix_in_the_row_and_the_exit_code(tmp_path, serve):
    board = Board()
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 3000)], {7: paths("a.py")})

    got = run(tmp_path, url, bin_dir, "--apply", "--json")

    (body,) = board.posted
    assert "PREFIX" in body["skip_reason"]
    answer = json.loads(got.stdout)
    (row,) = answer["prs"]
    assert row["outcome"] == "partial"
    assert row["complete"] is False
    assert got.returncode == 1


def test_a_short_list_is_reported_in_a_dry_run_too(tmp_path, serve):
    """A dry run that called a truncated PR clean would send an operator to `--apply`
    believing the ranking was about to come back on."""
    board = Board()
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 40)], {7: paths("a.py")})

    got = run(tmp_path, url, bin_dir, "--json")

    (row,) = json.loads(got.stdout)["prs"]
    assert row["outcome"] == "partial" and row["complete"] is False
    assert board.posted == []
    assert got.returncode == 1


def test_the_boards_own_dropped_report_is_believed_over_what_was_sent(tmp_path, serve):
    """The last place a prefix could still read as whole: the board folds repeated
    paths and caps the list, so what it says it stored beats what was posted."""
    board = Board(stored=1, dropped={"over_cap": 1, "unusable": 0})
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 2)], {7: paths("a.py", "b.py")})

    got = run(tmp_path, url, bin_dir, "--apply", "--json")

    (row,) = json.loads(got.stdout)["prs"]
    assert row["outcome"] == "partial"
    assert row["complete"] is False
    assert row["files_recorded"] == 1
    assert row["board_dropped"] == {"over_cap": 1, "unusable": 0}
    assert got.returncode == 1


def test_a_paged_read_that_failed_records_nothing_at_all(tmp_path, serve):
    """A prefix produced by a network fault is indistinguishable afterwards from one
    produced by GitHub's cap, so it is not recorded at all."""
    board = Board()
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 2)], {7: paths("a.py", "b.py")},
                      files_fail={7})

    got = run(tmp_path, url, bin_dir, "--apply", "--json")

    assert board.posted == []
    (row,) = json.loads(got.stdout)["prs"]
    assert row["outcome"] == "failed"
    assert "502" in row["why"]
    assert got.returncode == 1


def test_a_second_run_on_an_unchanged_pr_moves_no_number(tmp_path, serve):
    """The board refuses the repeat on the run_key, and the tool reports it as such
    rather than as a fresh row."""
    board = Board(duplicate=True)
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 1)], {7: paths("a.py")})

    got = run(tmp_path, url, bin_dir, "--apply", "--json")

    (row,) = json.loads(got.stdout)["prs"]
    assert row["outcome"] == "duplicate"
    assert got.returncode == 0, "a duplicate is the guard working, not a failure"


def test_the_run_key_is_the_same_twice_and_different_after_a_push(tmp_path, serve):
    board = Board()
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 1)], {7: paths("a.py")})
    run(tmp_path, url, bin_dir, "--apply")
    run(tmp_path, url, bin_dir, "--apply")
    first, second = (b["run_key"] for b in board.posted)
    assert first == second

    board.posted.clear()
    pushed = gh_stub(tmp_path, [pr_meta(7, 1, head=OTHER_HEAD)], {7: paths("a.py")})
    run(tmp_path, url, pushed, "--apply")
    assert board.posted[0]["run_key"] != first


def test_a_pr_already_answered_at_this_head_is_left_alone(tmp_path, serve):
    """It must not shadow a run that already carries a complete list — including a real
    panel's, which is why the check reads the list and not `reviewed`."""
    board = Board(runs={7: [{"id": 31, "head_sha": HEAD, "base": "main",
                             "changed_files_total": 2, "reviewed": True,
                             "_files": [{"path": "a.py"}, {"path": "b.py"}]}]})
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 2)], {7: paths("a.py", "b.py")})

    got = run(tmp_path, url, bin_dir, "--apply", "--json")

    assert board.posted == []
    (row,) = json.loads(got.stdout)["prs"]
    assert row["outcome"] == "already" and row["run_id"] == 31
    assert got.returncode == 0


def test_a_prior_run_whose_list_is_a_prefix_is_not_left_alone(tmp_path, serve):
    """The row that turns the ranking off is exactly this one, so skipping it would
    make the tool useless on the population it was written for."""
    board = Board(runs={7: [{"id": 31, "head_sha": HEAD, "base": "main",
                             "changed_files_total": 9, "reviewed": True,
                             "_files": [{"path": "a.py"}]}]})
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 9)],
                      {7: paths(*[f"f{i}.py" for i in range(9)])})

    run(tmp_path, url, bin_dir, "--apply")

    assert len(board.posted) == 1


def test_a_prior_run_at_a_head_the_branch_has_left_is_not_left_alone(tmp_path, serve):
    board = Board(runs={7: [{"id": 31, "head_sha": OTHER_HEAD, "base": "main",
                             "changed_files_total": 1, "reviewed": None,
                             "_files": [{"path": "a.py"}]}]})
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 1, head=HEAD)], {7: paths("a.py")})

    run(tmp_path, url, bin_dir, "--apply")

    assert len(board.posted) == 1
    assert board.posted[0]["head_sha"] == HEAD
    assert board.detail_reads == [], "no point counting paths on a run at another head"


def test_a_pr_that_genuinely_changed_nothing_is_knowledge(tmp_path, serve):
    """`changed_files_total: 0` with no rows is complete — `files_complete` says so,
    and a zero-file PR is disjoint from everything."""
    board = Board()
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 0)], {7: []})

    got = run(tmp_path, url, bin_dir, "--apply", "--json")

    (body,) = board.posted
    assert body["changed_files_total"] == 0 and body["changed_files"] == []
    (row,) = json.loads(got.stdout)["prs"]
    assert row["outcome"] == "recorded" and row["complete"] is True
    assert got.returncode == 0


def test_an_argument_it_does_not_understand_is_not_consent(tmp_path, serve):
    board = Board()
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 1)], {7: paths("a.py")})

    got = run(tmp_path, url, bin_dir, "--apply", "--yes-really")

    assert got.returncode == 2
    assert board.posted == []


def test_a_repo_it_cannot_work_out_is_refused_rather_than_guessed(tmp_path, serve):
    """#414: a tool that cannot find the checkout must refuse, not report a confident
    nothing-to-do about a repository it never looked at."""
    board = Board()
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [], {})
    empty = tmp_path / "not-a-checkout"
    empty.mkdir()
    env = {**os.environ, "PATH": str(bin_dir), "QUARTERBACK_BASE_URL": url,
           "QUARTERBACK_TOKEN": "t", "QUARTERBACK_TOKEN_CMD": "",
           "QUARTERBACK_CONFIG": str(tmp_path / "nope")}

    got = subprocess.run([sys.executable, str(QB_BACKFILL), "--path", str(empty),
                          "--apply"], capture_output=True, text=True, env=env,
                         timeout=60)

    assert got.returncode == 2
    assert "no repository" in got.stderr and "#414" in got.stderr
    assert board.posted == []


def test_a_pr_that_is_not_open_is_named_rather_than_silently_skipped(tmp_path, serve):
    board = Board()
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 1)], {7: paths("a.py")})

    got = run(tmp_path, url, bin_dir, "--pr", "99", "--json")

    answer = json.loads(got.stdout)
    assert [r["outcome"] for r in answer["prs"]] == ["failed"]
    assert "not an open PR" in answer["prs"][0]["why"]
    assert got.returncode == 1


def test_a_pr_list_that_reached_the_limit_is_not_a_whole_repo_sweep(tmp_path, serve):
    board = Board()
    url = serve(board)
    bin_dir = gh_stub(tmp_path, [pr_meta(7, 1), pr_meta(8, 1)],
                      {7: paths("a.py"), 8: paths("b.py")})

    got = run(tmp_path, url, bin_dir, "--apply", "--limit", "2", "--json")

    assert json.loads(got.stdout)["pr_list_may_be_short"] is True
    assert "may have open PRs this run never saw" in got.stderr
    assert got.returncode == 1, "a partial sweep must not read as a whole one"
