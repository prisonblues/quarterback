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
         raises=None, results=None):
    """Stub `qb-claim`/`qb-release`. Returns the dict the calls are recorded into.

    `results` scripts CONSECUTIVE answers — `[(returncode, stdout, stderr), …]` —
    for the one path that runs `qb-claim` twice: a host whose `qb-claim` predates
    `--no-plan-item` refuses the flag, and the round asks again without it. The last
    entry repeats, so a single `returncode`/`stdout`/`stderr` still describes a tool
    that keeps saying the same thing however often it is asked.

    `seen["args"]` is the LAST argv, as it always was; `seen["argvs"]` is every one
    of them, which is what the retry test needs — the assertion there is that the
    second call dropped the flag rather than repeating it into the same refusal.
    """
    monkeypatch.setattr(panel_seats.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if present else None)
    seen: dict = {"argvs": []}
    scripted = list(results or [(returncode, stdout, stderr)])

    def fake(args, **kw):
        seen["args"] = args
        seen["argvs"].append(args)
        if raises:
            raise raises
        rc, out, err = scripted[min(len(seen["argvs"]) - 1, len(scripted) - 1)]
        return subprocess.CompletedProcess(args, rc, out, err)

    monkeypatch.setattr(panel_seats.subprocess, "run", fake)
    return seen


#: What an old `qb-claim` prints when it meets the flag: argparse's own wording,
#: which is what `_rejected_the_flag` matches on. Reproduced rather than
#: paraphrased — the whole detection rests on this being the real sentence.
STALE_TOOL = ("usage: qb-claim [-h] [--repo-path REPO_PATH] [--ttl TTL] [--note NOTE]\n"
              "qb-claim: error: unrecognized arguments: --no-plan-item\n")

#: What a board OLDER than `plan_item` answers: it discarded the field
#: (`extra="ignore"`) and wrote the rank-1 row anyway. `--json` is what puts this on
#: stdout where `_wrote_a_plan_item` can read it.
OLD_BOARD = json.dumps({
    "claim_id": "abc-123", "kind": "work", "key": "acme/widget!1780",
    "expires": "2026-09-04T18:00:00Z", "renewed": False,
    "plan_item": {"item_id": "d1", "rank": 1, "rank_source": "picked-up",
                  "title": "fix: a thing", "repo": "acme/widget"}})

#: A board that HONOURS the flag: same answer, no item.
NEW_BOARD = json.dumps({
    "claim_id": "abc-123", "kind": "work", "key": "acme/widget!1780",
    "expires": "2026-09-04T18:00:00Z", "renewed": False, "plan_item": None})


# ------------------------------------------------------------------ hold_pr itself

def test_a_taken_claim_adds_no_note_and_names_the_resource_not_a_key(monkeypatch):
    """The silent-success half — a note on every round is noise on every round.

    And the argv is the assertion that matters beside it: the KIND and the NUMBER
    go up, never a composed key. That is #172, which is what made `plan_read` and
    `claims()` disagree about one issue in the same second, and a tool composing a
    third spelling would be the same defect with a new party.
    """
    seen = _cli(monkeypatch)
    assert panel_seats.hold_pr("/tmp/acme", 1780, 2) == ("", True)
    argv = seen["args"]
    assert argv[:3] == ["qb-claim", "pr", "1780"]
    assert "--repo-path" in argv and argv[argv.index("--repo-path") + 1] == "/tmp/acme"
    assert argv[argv.index("--note") + 1] == "panel review round 2"


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


def test_a_round_holds_the_pr_without_claiming_to_have_picked_it_up(monkeypatch):
    """#722, and it is the whole of this change.

    The claim is a true exclusivity record and a false pickup. Every issue/PR claim
    writes a top-ranked plan item (#427), so a round put PR #n in at rank 1 and the
    release at the end left it there — open, unclaimed, unblocked — and `next`
    offered the following agent a review that had already happened.

    `--title` and `--no-gh-title` go with it: both exist to name the plan item, and
    a round no longer writes one. `qb-claim` reads `--no-plan-item` as implying
    `--no-gh-title`, so the saved `gh` call is not lost with the flag.

    red/green: fails on `--no-plan-item` missing from the argv.
    """
    seen = _cli(monkeypatch)
    panel_seats.hold_pr("/tmp/acme", 1780, 1)
    assert "--no-plan-item" in seen["args"]
    assert "--title" not in seen["args"], "there is no item left for a title to name"
    assert "--no-gh-title" not in seen["args"], "implied by --no-plan-item"


# ------------------------------------------------- mixed versions (#722 follow-up)

def test_an_old_board_that_ignored_the_flag_is_said_rather_than_hidden(monkeypatch, capsys):
    """New harness, old board — and it is the ordinary state of a rollout here,
    because this harness and the board deploy separately.

    `ClaimIn` takes pydantic's default `extra="ignore"`, so a board older than
    `plan_item` discards the field in silence and writes the rank-1 row the flag
    exists to prevent. The answer carries the evidence — a `plan_item` on a request
    that asked for none — and before this nothing read it: `qb-claim` exited 0, this
    returned "", and the round reported a clean claim over the exact defect #722 is
    about.

    Noted and never failed. The claim is real and it is the half that prevents
    duplicated work; nothing out here can un-write the row, so the only thing left
    to do with it is say so where a reader will meet it.

    red/green: fails on `assert note`, which was "" — the item came back and the
    round said nothing about it.
    """
    _cli(monkeypatch, returncode=0, stdout=OLD_BOARD)
    note, holding = panel_seats.hold_pr("/tmp/acme", 1780, 1)
    assert holding is True, "the claim IS ours — losing the release would be worse"
    assert note, "an ignored flag that nothing reports is the silent half of #722"
    assert "older than" in note and "rank 1" in note
    assert "#722" in note
    assert note in capsys.readouterr().err


def test_a_board_that_honoured_the_flag_says_nothing_at_all(monkeypatch):
    """The alarm must not fire on the ordinary case, or it is noise on every round —
    and `--json` returning `plan_item: null` is what the fixed board answers."""
    _cli(monkeypatch, returncode=0, stdout=NEW_BOARD)
    assert panel_seats.hold_pr("/tmp/acme", 1780, 1) == ("", True)


def test_an_unreadable_answer_is_not_an_alarm(monkeypatch):
    """`--quiet`, a truncated pipe, a `qb-claim` old enough to print the bare id:
    none of those is evidence that a row was written, and an alarm that fires when
    it cannot tell is one people learn to filter out. The cost of the miss is the
    behaviour the fleet had yesterday."""
    _cli(monkeypatch, returncode=0, stdout="abc-123\n")
    assert panel_seats.hold_pr("/tmp/acme", 1780, 1) == ("", True)


def test_an_old_qb_claim_that_rejects_the_flag_is_asked_again_without_it(
        monkeypatch, capsys):
    """New harness, old `qb-claim` — the reverse direction, and the one that used to
    fail worst.

    argparse refuses the unknown flag and exits 2, which this filed as "the board did
    not take it". So a host part-way through an upgrade ran every round UNCLAIMED and
    said only that the board had declined — silently undoing #715 a few hours after
    it shipped, on the evidence of a flag added to fix something else.

    The retry writes a plan item, which is the #722 defect. That is the right trade:
    an imperfect record somebody can see beats no record at all, which is the failure
    #715 was written to remove.

    red/green: fails on `holding is True` — the refusal was read as a board's, no
    retry happened, and the round ran with no claim.
    """
    seen = _cli(monkeypatch, results=[(2, "", STALE_TOOL), (0, NEW_BOARD, "")])
    note, holding = panel_seats.hold_pr("/tmp/acme", 1780, 1)
    assert holding is True, "the round must end up holding the PR"
    assert len(seen["argvs"]) == 2, "asked once with the flag, once without"
    assert "--no-plan-item" in seen["argvs"][0]
    assert "--no-plan-item" not in seen["argvs"][1], \
        "repeating the flag walks into the identical refusal"
    assert "predates" in note and "#722" in note
    assert note in capsys.readouterr().err


def test_a_different_unknown_argument_is_not_retried_into_the_same_refusal(monkeypatch):
    """The detection names the flag as well as argparse's wording, and that half is
    load-bearing: dropping `--no-plan-item` cannot help a host that refused
    something else, so the retry would be a second identical failure at twice the
    latency — the rule `qb-claim` states about its own retry."""
    stale = ("usage: qb-claim [-h]\n"
             "qb-claim: error: unrecognized arguments: --some-later-flag\n")
    seen = _cli(monkeypatch, returncode=2, stderr=stale)
    note, holding = panel_seats.hold_pr("/tmp/acme", 1780, 1)
    assert len(seen["argvs"]) == 1, "nothing here is retryable"
    assert holding is False
    assert "did not take it" in note


def test_a_board_refusal_is_still_not_a_stale_tool(monkeypatch):
    """Exit 2 covers an outage, a rotated token and a ref the board will not key.
    None of those is answered by dropping a flag, and reading them as a stale tool
    would hide a real misconfiguration behind a successful retry."""
    seen = _cli(monkeypatch, returncode=2, stderr="qb-claim: board answered HTTP 401\n")
    note, holding = panel_seats.hold_pr("/tmp/acme", 1780, 1)
    assert len(seen["argvs"]) == 1
    assert holding is False and "401" in note


def test_the_round_asks_for_json_because_that_is_where_the_evidence_is(monkeypatch):
    """The old-board case is detected from the board's own answer, not from
    `qb-claim`'s prose — `create-worktree` says why that matters, and the flag is
    what puts the answer on stdout."""
    seen = _cli(monkeypatch, returncode=0, stdout=NEW_BOARD)
    panel_seats.hold_pr("/tmp/acme", 1780, 1)
    assert "--json" in seen["args"]


def test_a_pr_somebody_else_holds_is_a_note_and_never_a_refusal(monkeypatch, capsys):
    """The claim is a record, not a gate. A review that would not run because a
    board could not be reached is a worse failure than a review nobody can see —
    and two panels on one PR is spend twice, which is worth a line."""
    _cli(monkeypatch, returncode=1, stderr=QB_HELD)
    note, holding = panel_seats.hold_pr("/tmp/acme", 1780, 2)
    assert holding is False, "a peer holds it, so this round has nothing to release"
    assert "claimed by somebody else" in note
    assert "marble-bronze" in note, "the holder is the point of the note"
    assert "ran anyway" in note
    assert note in capsys.readouterr().err


def test_a_host_without_qb_claim_says_what_is_lost_and_it_is_not_the_review(
        monkeypatch, capsys):
    _cli(monkeypatch, present=False)
    note, holding = panel_seats.hold_pr("/tmp/acme", 1780, 1)
    assert holding is False
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
    note, holding = panel_seats.hold_pr("/tmp/acme", 1780, 1)
    assert holding is False
    assert "claimed by somebody else" not in note
    assert "did not take it" in note and "401" in note


def test_claiming_never_raises_and_still_says_so(monkeypatch):
    _cli(monkeypatch, raises=subprocess.TimeoutExpired("qb-claim", 20))
    note, _ = panel_seats.hold_pr("/tmp/acme", 1780, 1)
    assert "TimeoutExpired" in note and "no claim on PR #1780" in note


def test_a_shouting_board_cannot_push_a_page_of_markup_into_a_pr_comment(monkeypatch):
    """The note goes into `config_notes`, which `--post` publishes."""
    _cli(monkeypatch, returncode=2, stderr="<html>" + "x" * 5000)
    note, _ = panel_seats.hold_pr("/tmp/acme", 1780, 1)
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
                        lambda p, n, r: (seen.append(("hold", p, n, r)), ("", True))[1])
    monkeypatch.setattr(panel, "release_pr",
                        lambda p, n: seen.append(("release", p, n)) or "")
    assert panel.run("board", 1780, post=False, json_out=True) == 0
    capsys.readouterr()
    assert seen == [("hold", "/tmp/acme-board", 1780, 1),
                    ("release", "/tmp/acme-board", 1780)]


def test_a_round_that_did_not_get_the_claim_does_not_hand_one_back(
        monkeypatch, capsys):
    """Found by an independent Codex review of this PR.

    `hold_pr` never gates — a round told the PR is held by a peer runs anyway — and
    the release was gated on `record` rather than on having taken anything. So the
    shorter of two overlapping rounds ended the longer one's claim on its way out,
    and `POST /claim/release` authorises at MACHINE granularity for a claim that
    names no session, which is exactly the co-tenant case. The fleet then read a
    review still in flight as finished: the claim's one job, undone by the
    mechanism that took it.

    red/green: fails with a `release` call recorded against a round that was told
    the PR belonged to somebody else.
    """
    _round(monkeypatch)
    seen: list[tuple] = []
    monkeypatch.setattr(panel, "hold_pr",
                        lambda *a, **k: ("PR #1780 is claimed by somebody else", False))
    monkeypatch.setattr(panel, "release_pr",
                        lambda p, n: seen.append(("release", p, n)) or "")
    assert panel.run("board", 1780, post=False, json_out=True) == 0
    capsys.readouterr()
    assert seen == [], "a round handed back a claim it never held"


def test_a_claim_the_board_refused_is_not_released_either(monkeypatch, capsys):
    """The other half of the same gate, and it is not the same case. A board that
    could not be reached leaves the PR genuinely unclaimed, so there is nothing to
    release — and a `qb-release` fired at it is a second failed round trip whose
    only output is a second note saying the same thing."""
    _round(monkeypatch)
    seen: list[tuple] = []
    monkeypatch.setattr(panel, "hold_pr",
                        lambda *a, **k: ("the board has no claim on PR #1780", False))
    monkeypatch.setattr(panel, "release_pr",
                        lambda p, n: seen.append(("release", p, n)) or "")
    assert panel.run("board", 1780, post=False, json_out=True) == 0
    capsys.readouterr()
    assert seen == []


def test_the_claim_is_taken_before_a_single_seat_is_dispatched(monkeypatch, capsys):
    """#253's event is the round STARTING, and a claim taken after the seats have
    run would be a record of work that is already over — which is what the board
    already had in `POST /review` at the end."""
    _round(monkeypatch)
    order: list[str] = []
    monkeypatch.setattr(panel, "hold_pr",
                        lambda *a, **k: (order.append("claim"), ("", True))[1])
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
    monkeypatch.setattr(panel, "hold_pr",
                        lambda *a, **k: (seen.append(a), ("", True))[1])
    monkeypatch.setattr(panel, "release_pr", lambda *a, **k: seen.append(a) or "")
    assert panel.run("board", 1780, post=False, json_out=True, record=False) == 0
    capsys.readouterr()
    assert seen == []


def test_a_claim_that_could_not_be_taken_reaches_the_pr_comment(monkeypatch, capsys):
    """`config_notes` is the channel that already exists for a board write that
    did not land, and it is what `--post` publishes."""
    _round(monkeypatch)
    monkeypatch.setattr(
        panel, "hold_pr",
        lambda *a, **k: ("the board has no claim on PR #1780 — no `qb-claim`", False))
    assert panel.run("board", 1780, post=False, json_out=True) == 0
    notes = json.loads(capsys.readouterr().out)["config_notes"]
    assert any("no claim on PR #1780" in n for n in notes)
