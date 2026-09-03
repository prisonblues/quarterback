"""A round that is reviewing a PR says so on the board (#253).

`GET /active` carries no work reference at all — a lease has `repo`, `branch` and
`title`, and nothing that names an issue or a PR — so every fleet surface showed
an agent three hours into reviewing #1780 exactly as it showed one that had just
opened the repo. Measured on this board while #253 was open: five live agents,
three of them reviewing, `/claims` empty, and the dashboard's AGENTS rows reading
`master`, `test` and `Panel review PR rework`.

The dashboard was never the problem. `qbdata._agent_row` has always preferred a
claim over the prompt title, and had nothing to join because nothing on the review
path claimed anything. #253's rule (from #229 and #172) is that the event must be
derived from an action that already happens rather than from a second declaration
somebody has to remember — and a round dispatching its seats is that action.

So the tests come in two halves. The first asks `hold_pr` and `release_pr` the
questions they can answer on their own — no `qb-claim`, a claim somebody else
holds, a board that would not answer, a `qb-claim` that raised. The second runs a
whole round and asserts the claim was taken BEFORE the seats and handed back
after, because the pairing is the part a later edit is most likely to undo, and a
claim that is never released is #135 arriving through a new door.

**NOT `test_panel_pr_claim.py`**, which is #550's suite for the PR's own claim — the
block carrying what the author says the change does. One file uses the word both ways
and the two suites are named apart, because the first draft of this one was written
straight over that file.

Run: pytest harness/loops/tests/test_panel_board_claim.py
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402  — `sh` is the `gh` seam
import panel_preflight  # noqa: E402  — owns its own `seat_installed` global
import panel_seats  # noqa: E402  — `hold_pr` lives here
from conftest import gh_stub  # noqa: E402

#: What `qb-claim` prints when somebody else has it, on its exit 1. The whole
#: held-case test rests on the holder reaching the note, so the double reproduces
#: the shape rather than paraphrasing it.
QB_HELD = "held by zeus/marble-bronze — panel review round 1\n"

_REAL = (panel_seats.hold_pr, panel_seats.release_pr)


@pytest.fixture(autouse=True)
def _the_real_pair(monkeypatch, pr_claims):
    """This module is the one that must NOT have the pair stubbed away.

    `conftest.pr_claims` replaces both, autouse, so no test on an enrolled
    workstation can take a live claim on a live PR. This module's whole subject is
    those two functions, and its own guard is lower down and stronger: it stubs
    `panel_seats.shutil.which` and `panel_seats.subprocess.run`, so the real
    functions execute and reach a `qb-claim` that does not exist.

    Depending on `pr_claims` explicitly rather than relying on fixture ordering,
    for the reason `test_panel_record_run` gives about the same override: it makes
    this a documented reversal of a named guard instead of a coincidence of
    declaration order.
    """
    hold, release = _REAL
    for mod in (panel, panel_seats):
        monkeypatch.setattr(mod, "hold_pr", hold)
        monkeypatch.setattr(mod, "release_pr", release)


def _cli(monkeypatch, *, present=True, returncode=0, stdout="", stderr="",
         raises=None):
    """Stub `qb-claim`/`qb-release`. Returns the dict the call is recorded into."""
    monkeypatch.setattr(panel_seats.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if present else None)
    seen: dict = {}

    def fake(args, **kw):
        seen["args"] = args
        if raises:
            raise raises
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    monkeypatch.setattr(panel_seats.subprocess, "run", fake)
    return seen


# ------------------------------------------------------------------ hold_pr itself

def test_a_taken_claim_adds_no_note_and_names_the_resource_not_a_key(monkeypatch):
    """The silent-success half — a note on every round is noise on every round.

    And the argv is the assertion that matters beside it: the KIND and the NUMBER
    go up, never a composed key. That is #172, which is what made `plan_read` and
    `claims()` disagree about one issue in the same second, and a tool composing a
    third spelling would be the same defect with a new party.
    """
    seen = _cli(monkeypatch)
    assert panel_seats.hold_pr("/tmp/acme", 1780, 2, "fix: a thing") == ""
    argv = seen["args"]
    assert argv[:3] == ["qb-claim", "pr", "1780"]
    assert "--repo-path" in argv and argv[argv.index("--repo-path") + 1] == "/tmp/acme"
    assert argv[argv.index("--note") + 1] == "panel review round 2"
    # The round's own read of the title, not a second `gh` call for the same string.
    assert "--no-gh-title" in argv
    assert argv[argv.index("--title") + 1] == "fix: a thing"


def test_the_fuse_outlasts_a_round_and_falls_well_short_of_a_worktrees(monkeypatch):
    """The two bounds that are load-bearing, and neither of them is a default.

    The board's default is one hour (`DEFAULT_TTL = 3600`), which is BELOW the
    upper end of a round — 20-40 minutes ordinarily, longer when CI or a vendor is
    slow — so a fuse at the default would expire mid-round and show the PR as free
    while four seats were still reading it. And it is far short of
    `create-worktree`'s eight hours, which covers a whole worktree's work and is
    #608's complaint about a fuse nothing can renew.

    Asserted as bounds rather than against a remembered default: the first cut of
    this test asserted `< 8 * 3600` while its comment called eight hours "the
    default", so it passed and pinned a false belief (PR #715 review).
    """
    seen = _cli(monkeypatch)
    panel_seats.hold_pr("/tmp/acme", 1780, 1)
    ttl = int(seen["args"][seen["args"].index("--ttl") + 1])
    assert ttl == panel_seats.PR_HOLD_TTL
    assert ttl > 3600, "a round outlives the board's one-hour default"
    assert ttl < 28800, "and must not become create-worktree's eight-hour fuse"


def test_a_round_with_no_title_still_asks_for_no_gh_title(monkeypatch):
    """A round that could not read a title has nothing to gain from `qb-claim`
    trying again for it — and the flag is what stops the extra API call."""
    seen = _cli(monkeypatch)
    panel_seats.hold_pr("/tmp/acme", 1780, 1)
    assert "--no-gh-title" in seen["args"]
    assert "--title" not in seen["args"]


def test_a_pr_somebody_else_holds_is_a_note_and_never_a_refusal(monkeypatch, capsys):
    """The claim is a record, not a gate. A review that would not run because a
    board could not be reached is a worse failure than a review nobody can see —
    and two panels on one PR is spend twice, which is worth a line."""
    _cli(monkeypatch, returncode=1, stdout=QB_HELD)
    note = panel_seats.hold_pr("/tmp/acme", 1780, 2)
    assert "claimed by somebody else" in note
    assert "marble-bronze" in note, "the holder is the point of the note"
    assert "ran anyway" in note
    assert note in capsys.readouterr().err


def test_a_host_without_qb_claim_says_what_is_lost_and_it_is_not_the_review(
        monkeypatch, capsys):
    _cli(monkeypatch, present=False)
    note = panel_seats.hold_pr("/tmp/acme", 1780, 1)
    assert "no `qb-claim` on this host" in note
    assert "complete and unaffected" in note
    assert "which PR this agent is on" in note
    assert note in capsys.readouterr().err


def test_a_board_that_would_not_take_it_is_told_apart_from_a_holder(monkeypatch):
    """`qb-claim` exits 2 for everything that is not a holder — an outage, a
    rotated token, a ref the board will not key. None of those is a peer, and a
    misconfiguration reported as a collision sends somebody looking for an agent
    that does not exist."""
    _cli(monkeypatch, returncode=2, stderr="qb-claim: board answered HTTP 401\n")
    note = panel_seats.hold_pr("/tmp/acme", 1780, 1)
    assert "claimed by somebody else" not in note
    assert "did not take it" in note and "401" in note


def test_claiming_never_raises_and_still_says_so(monkeypatch):
    _cli(monkeypatch, raises=subprocess.TimeoutExpired("qb-claim", 20))
    note = panel_seats.hold_pr("/tmp/acme", 1780, 1)
    assert "TimeoutExpired" in note and "no claim on PR #1780" in note


def test_a_shouting_board_cannot_push_a_page_of_markup_into_a_pr_comment(monkeypatch):
    """The note goes into `config_notes`, which `--post` publishes."""
    _cli(monkeypatch, returncode=2, stderr="<html>" + "x" * 5000)
    note = panel_seats.hold_pr("/tmp/acme", 1780, 1)
    assert len(note) < 600 and "xxx" in note


# ---------------------------------------------------------------- release_pr itself

def test_a_released_claim_is_silent(monkeypatch):
    seen = _cli(monkeypatch)
    assert panel_seats.release_pr("/tmp/acme", 1780) == ""
    assert seen["args"][:3] == ["qb-release", "pr", "1780"]


def test_nothing_to_release_is_not_a_failure(monkeypatch):
    """`qb-release` exits 0 when there was nothing live to hand back, and this
    runs on the teardown of every round — including rounds whose claim was never
    taken because the board was down."""
    _cli(monkeypatch, returncode=0)
    assert panel_seats.release_pr("/tmp/acme", 1780) == ""


def test_a_claim_left_standing_says_how_long_it_stands_for(monkeypatch, capsys):
    """The consequence is visible on a surface somebody reads: the dashboard shows
    this agent holding the PR after it stopped working on it. A reader with no note
    has to guess whether that is a stuck round or a stale claim."""
    _cli(monkeypatch, returncode=1, stdout="refused: held by another session\n")
    note = panel_seats.release_pr("/tmp/acme", 1780)
    assert "not handed back" in note
    assert "3h" in note, "the fuse length is the recovery time, so it is stated"
    assert note in capsys.readouterr().err


def test_a_host_without_qb_release_is_silent_because_it_had_no_claim_either(
        monkeypatch):
    """The same host had no `qb-claim`, so there is nothing standing to complain
    about — and a note per round about a tool this host has never had is the noise
    `_unrecorded` deliberately does not make either."""
    _cli(monkeypatch, present=False)
    assert panel_seats.release_pr("/tmp/acme", 1780) == ""


# ---------------------------------------------------------------- a whole round

META = {"title": "fix: a thing", "additions": 30, "deletions": 12,
        "baseRefName": "main", "headRefName": "h", "headRefOid": "abc",
        "files": [{"path": "a.py", "additions": 30, "deletions": 12}],
        "changedFiles": 1, "state": "OPEN", "isDraft": False}

CFG = {"github": "acme/board", "path": "/tmp/acme-board", "name": "board",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
       "review_panel": {}}


def _round(monkeypatch):
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: dict(CFG))
    monkeypatch.setattr(panel_core, "sh", gh_stub(meta=META, diff="diff"))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 5))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate",
                        lambda *a, **k: ([], None, panel.CoverageRuling()))
    monkeypatch.setattr(panel, "seat_installed", lambda name: True)
    monkeypatch.setattr(panel_preflight, "seat_installed", lambda name: True)


def test_a_round_claims_its_pr_and_hands_it_back(monkeypatch, capsys, pr_claims):
    """The pairing, which is the assertion a later edit is most likely to break.

    The claim carries the ROUND, so a cycle's rows say which round is holding the
    PR rather than only that something is; and the repo path is the checkout's,
    because `qb-claim` derives the slug from a remote and a claim keyed off the
    wrong tree is a claim on another repository's #1780.
    """
    _round(monkeypatch)
    # The pair is what conftest's guard records, and this module put the real
    # functions back — so re-stub them here, where the assertion is about the
    # ORDER of the two calls rather than about what either one says to `qb-claim`.
    seen: list[tuple] = []
    monkeypatch.setattr(panel, "hold_pr",
                        lambda p, n, r, t=None: seen.append(("hold", p, n, r, t)) or "")
    monkeypatch.setattr(panel, "release_pr",
                        lambda p, n: seen.append(("release", p, n)) or "")
    assert panel.run("board", 1780, post=False, json_out=True) == 0
    capsys.readouterr()
    assert seen == [("hold", "/tmp/acme-board", 1780, 1, "fix: a thing"),
                    ("release", "/tmp/acme-board", 1780)]


def test_the_claim_is_taken_before_a_single_seat_is_dispatched(monkeypatch, capsys):
    """#253's event is the round STARTING, and a claim taken after the seats have
    run would be a record of work that is already over — which is what the board
    already had in `POST /review` at the end."""
    _round(monkeypatch)
    order: list[str] = []
    monkeypatch.setattr(panel, "hold_pr",
                        lambda *a, **k: order.append("claim") or "")
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: (order.append("seat")
                                         or panel.ReviewerRun([], None, 5)))
    assert panel.run("board", 1780, post=False, json_out=True) == 0
    capsys.readouterr()
    assert order[0] == "claim" and "seat" in order


def test_no_record_takes_no_claim_at_all(monkeypatch, capsys):
    """`--no-record` is a caller saying this run does not go on the board, and a
    claim is a board write like any other. A preview that quietly claimed a PR
    would refuse somebody on the strength of a run nobody asked to be recorded."""
    _round(monkeypatch)
    seen: list[tuple] = []
    monkeypatch.setattr(panel, "hold_pr", lambda *a, **k: seen.append(a) or "")
    monkeypatch.setattr(panel, "release_pr", lambda *a, **k: seen.append(a) or "")
    assert panel.run("board", 1780, post=False, json_out=True, record=False) == 0
    capsys.readouterr()
    assert seen == []


def test_a_claim_that_could_not_be_taken_reaches_the_pr_comment(monkeypatch, capsys):
    """`config_notes` is the channel that already exists for a board write that
    did not land, and it is what `--post` publishes."""
    _round(monkeypatch)
    monkeypatch.setattr(panel, "hold_pr",
                        lambda *a, **k: "the board has no claim on PR #1780 — no `qb-claim`")
    assert panel.run("board", 1780, post=False, json_out=True) == 0
    notes = json.loads(capsys.readouterr().out)["config_notes"]
    assert any("no claim on PR #1780" in n for n in notes)
