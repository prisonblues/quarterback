"""v2.15: rounds, and what a run could not see.

Two runs of one PR used to be two unrelated records — nothing said which was the
re-review of the other's fix, what this round found that the last had not, or
what stopped the loop. And a run reported only what was *found*: a reviewer given
a prefix of the diff, one that never ran, and one with nothing to say all wrote
the same zero into the record.

These tests pin what makes a review reviewable: the round metadata survives the
round trip, a declaration is attributed to the member that made it (not to
everyone who happened to raise the same finding), "never asked" stays distinct
from "nothing to declare", and a re-review flag is checked against what the
following round actually found rather than taken on trust.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from app.api.reviews import _derive_key
from app.db import engine

from .conftest import LAPTOP

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness" / "loops"))
# `panel_core` is deliberately NOT imported here: `sh` has lived there since #129, and the
# one test that patches it goes through `panel.panel_core` so the object it patches is the
# object `panel.run()` calls. A second import binds the same module under a second name and
# reads as if there were a choice about which to patch, which is the mistake #129 fixed.
import panel

REPO = "acme/v215repo"
AGENT = {**LAPTOP, "X-Agent-Instance": "d14d14"}


@pytest.fixture(autouse=True)
def _every_seat_installed(monkeypatch):
    """This box has every seat, for every test in this module (#222).

    `budgets` is built from the seats the HOST can actually run, so a panel round
    driven from here otherwise depends on which vendor CLIs the machine happens to
    carry: on a CI runner, which carries none, `claude` and `codex` get no budget
    at all, and an assertion that codex's 40-char budget CUT the diff fails with
    `assert False is True` — while `fake_review` has them running perfectly
    happily. That pairing (a seat that ran with no budget) is a doubles artefact
    and not a state production can reach, because `run_seat` refuses an absent seat
    before it can run; the doubles replace `review_llm` wholesale and so never
    reach that refusal.

    A fixture rather than a line inside one helper: this is the only module in the
    app suite that drives `panel.run()`, and the next test written here inherits
    the pin instead of rediscovering the failure on CI. It is scoped to this module
    for the same reason `harness/loops/tests` does not make it package-wide — a
    test whose subject is a seat's ABSENCE must not be silently pinned to the
    opposite. Nothing here has that subject; if something does, it overrides this
    the way `test_panel_absent_seat.py` does.

    No `raising=False`: the attribute is guaranteed to exist for the pin to have a
    purpose, and tolerating its absence is how a rename turns it into a silent
    no-op that hands this module back to the host's PATH.
    """
    monkeypatch.setattr(panel, "seat_installed", lambda name: True)


def payload(pr: int, **over) -> dict:
    body = {
        "repo": REPO,
        "pr": pr,
        "judged": True,
        "judge_model": "opus",
        "reviewers_selected": ["claude", "codex"],
        "reviewers": {
            "claude": {"model": "sonnet", "ran": True, "could_not_assess": []},
            "codex": {"model": "gpt-5.6", "ran": True, "truncated": True,
                      "max_diff_chars": 60_000,
                      "could_not_assess": ["the migration, which the diff omits"]},
        },
        "round": 1,
        # Every panel run mints one, and the re-review check now REQUIRES it: the
        # positional fallback it replaced credited one cycle's round 2 to another
        # cycle's round 1 whenever two agents looped the same PR.
        "cycle": "cyc-1",
        "new_findings": 1,
        "round_stop": {"stop": False, "reason": "1 finding(s) no earlier round raised",
                       "confident": False, "veto": ["codex saw 60,000 of 118,402 diff chars"]},
        "coverage_note": "codex is right that the migration is unread",
        "to_fix": [{
            "severity": "P2", "file": "app/sync.py", "line": 40,
            "title": "half-stale node after the early return",
            "reviewers": ["claude", "codex"],
            "reason": "real",
            "needs_rereview": True,
            "rereview_by": ["codex"],
            "new_this_round": True,
        }],
        "dismissed": [],
        "sonar_findings": [],
    }
    return {**body, **over}


async def record(client, pr: int, **over) -> int:
    r = await client.post("/review", json=payload(pr, **over), headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def detail(client, run_id: int) -> dict:
    r = await client.get(f"/review/{run_id}", headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


def card(run: dict, name: str) -> dict:
    return next(c for c in run["reviewers"] if c["name"] == name)


# ---- the round survives the round trip -------------------------------------

async def test_a_run_records_where_it_sat_in_the_cycle(client):
    run = await detail(client, await record(client, 6100))
    assert run["round"] == 1
    assert run["new_findings"] == 1
    assert run["stop_reason"] == "1 finding(s) no earlier round raised"
    assert run["stop_confident"] is False
    assert run["coverage_note"] == "codex is right that the migration is unread"


async def test_a_stop_that_was_not_convergence_is_recorded_as_such(client):
    """The whole point of the column: a counter reading zero while a reviewer read
    half the diff is a fact about the panel, not about the code."""
    run = await detail(client, await record(
        client, 6101, round=2, new_findings=0,
        round_stop={"stop": True, "reason": "dry — nothing raised that an earlier round had not",
                    "confident": False, "veto": ["codex saw 60,000 of 118,402 diff chars"]}))
    assert run["round"] == 2 and run["stop_reason"].startswith("dry")
    assert run["stopped"] is True
    assert run["stop_confident"] is False
    # ...and WHY it was not convergence, which is the question the column exists
    # to answer. "not convergence" with the reasons dropped answers half of it.
    assert run["stop_veto"] == ["codex saw 60,000 of 118,402 diff chars"]


async def test_a_round_that_said_go_again_is_not_recorded_as_a_stop(client):
    """`stop: false` used to be parsed and dropped, leaving the reason string as
    the only signal — and it reads as a reason to CONTINUE ("1 finding(s) no
    earlier round raised"), so the board labelled a running cycle finished."""
    run = await detail(client, await record(client, 6104))   # the default: stop false
    assert run["stopped"] is False
    assert run["stop_reason"] == "1 finding(s) no earlier round raised"


async def test_an_older_payload_is_a_first_round_that_declared_nothing(client):
    """Recorded exactly as before: round 1, and NULL rather than zero everywhere
    the panel was never asked — a pre-v2.15 run must not read as earned-clean."""
    body = {k: v for k, v in payload(6102).items()
            if k not in ("round", "cycle", "new_findings", "round_stop", "coverage_note")}
    body["reviewers"] = {"claude": {"model": "sonnet", "ran": True}}
    body["to_fix"] = [{"severity": "P3", "file": "a.py", "title": "x",
                       "reviewers": ["claude"], "reason": "real"}]
    r = await client.post("/review", json=body, headers=AGENT)
    assert r.status_code == 201
    run = await detail(client, r.json()["id"])
    assert run["round"] == 1
    assert run["new_findings"] is None and run["stop_reason"] is None
    # `stop_veto` NULL, not []: "no panel ever said" is not "the stopping rule ran
    # and vetoed nothing", and the read path keeps them apart as ingest does.
    assert run["stopped"] is None and run["stop_veto"] is None
    assert run["stop_confident"] is None and run["coverage_note"] is None
    assert run["cycle"] is None
    assert card(run, "claude")["could_not_assess"] is None
    assert card(run, "claude")["unstructured"] is None
    assert run["findings"][0]["needs_rereview"] is False
    assert run["findings"][0]["new_this_round"] is None


async def test_a_flat_stop_reason_is_accepted_without_the_nested_verdict(client):
    run = await detail(client, await record(
        client, 6103, round_stop=None, stop_reason="round cap (2) reached"))
    assert run["stop_reason"] == "round cap (2) reached"
    # Nothing claimed about confidence, or about whether it stopped at all — the
    # flat field carries a reason and nothing else, and guessing True from it is
    # how a round that meant "go again" would read as finished.
    assert run["stop_confident"] is None and run["stopped"] is None


# ---- coverage declarations -------------------------------------------------

async def test_nothing_to_declare_is_not_the_same_as_never_asked(client):
    """A finding count reports "clean" and "I could not tell" as the same zero.
    This is the column that separates them, so its own empty states must not
    collapse either."""
    run = await detail(client, await record(client, 6110))
    assert card(run, "claude")["could_not_assess"] == []
    assert card(run, "codex")["could_not_assess"] == ["the migration, which the diff omits"]


async def test_a_reply_that_did_not_parse_is_not_a_reviewer_that_was_never_asked(client):
    """An unparsed reply loses everything the member might have declared, so it
    lands on `could_not_assess: null` — the same cell as a pre-v2.15 reviewer that
    was never asked. That is the NULL/[] collapse this release exists to prevent,
    one level up: a coverage failure the honesty stats could not see."""
    run = await detail(client, await record(client, 6112, reviewers={
        "claude": {"model": "sonnet", "ran": True, "could_not_assess": []},
        "codex": {"model": "gpt-5.6", "ran": True, "unstructured": True},
    }))
    codex = card(run, "codex")
    assert codex["unstructured"] is True and codex["could_not_assess"] is None
    # ...and it is distinguishable from the member that simply had nothing to say.
    assert card(run, "claude")["unstructured"] is None
    s = (await client.get(f"/review/stats?repo={REPO}", headers=AGENT)).json()
    rows = {(m["reviewer"], m["model"]): m for m in s["by_model"]}
    assert rows[("codex", "gpt-5.6")]["unstructured_runs"] >= 1
    assert rows[("claude", "sonnet")]["unstructured_runs"] == 0


async def test_truncation_is_visible_on_the_row_it_affected(client):
    run = await detail(client, await record(client, 6111))
    codex = card(run, "codex")
    assert codex["truncated"] is True and codex["max_diff_chars"] == 60_000
    assert card(run, "claude")["truncated"] is None


# ---- the re-review declaration, and who made it ----------------------------

async def test_a_flag_is_credited_to_the_member_that_made_it(client):
    """Not to everyone who raised the finding: the declaration's accuracy is per
    reviewer, and crediting the group makes the honest member and the quiet one
    indistinguishable on exactly the statistic that separates them."""
    run = await detail(client, await record(client, 6120))
    assert card(run, "codex")["rereview_flagged"] == 1
    assert card(run, "claude")["rereview_flagged"] == 0
    assert run["findings"][0]["needs_rereview"] is True


async def test_a_reporters_own_flag_wins_over_the_panels_attribution(client):
    """``reported_by`` is the finer grain, per REPORTER: a row carrying an explicit
    flag is the record for that member, and a row that omits the key has declared
    nothing, so the coarser ``rereview_by`` still speaks for it. Dropping it for
    every member that merely sent an account lost the attribution outright."""
    run = await detail(client, await record(client, 6121, to_fix=[{
        "severity": "P1", "file": "app/db.py", "title": "session leak",
        "reason": "real",
        "rereview_by": ["claude", "pi"],
        "reported_by": [
            {"reviewer": "claude", "severity": "P1", "account": "leaks on the error path"},
            {"reviewer": "codex", "severity": "P2", "account": "same, plus the retry",
             "needs_rereview": True},
            {"reviewer": "pi", "severity": "P2", "account": "no need to re-read it",
             "needs_rereview": False},
        ],
    }]))
    assert card(run, "codex")["rereview_flagged"] == 1
    # claude sent an account but no flag of its own, so `rereview_by` fills in.
    assert card(run, "claude")["rereview_flagged"] == 1
    # pi said false in its own words, and that is not overturned by the coarser list.
    assert card(run, "pi")["rereview_flagged"] == 0
    flags = {r["reviewer"]: r["needs_rereview"] for r in run["findings"][0]["reported_by"]}
    assert flags == {"claude": True, "codex": True, "pi": False}


async def test_a_reporters_silence_is_not_treated_as_missing_data(client):
    """A member that sent an account is authoritative about itself, including its
    `false`. Filling that in from the coarser list would manufacture a
    declaration it did not make — and honesty per reviewer is exactly the
    statistic that ruins."""
    run = await detail(client, await record(client, 6123, to_fix=[{
        "severity": "P2", "file": "a.py", "title": "structural", "reason": "real",
        "reviewers": ["claude", "codex"],
        "rereview_by": ["claude", "codex"],
        "reported_by": [
            {"reviewer": "claude", "account": "not structural at all",
             "needs_rereview": False},
        ],
    }]))
    assert card(run, "claude")["rereview_flagged"] == 0
    # codex sent no account, so the panel's attribution still speaks for it.
    assert card(run, "codex")["rereview_flagged"] == 1


async def test_an_unattributed_flag_credits_everyone_rather_than_vanishing(client):
    """A panel that merges before it can send per-reporter accounts still made the
    declaration. Over-crediting is visible and correctable; dropping it is not."""
    run = await detail(client, await record(client, 6122, to_fix=[{
        "severity": "P2", "file": "a.py", "title": "structural", "reason": "real",
        "reviewers": ["claude", "codex"], "needs_rereview": True,
    }]))
    assert card(run, "claude")["rereview_flagged"] == 1
    assert card(run, "codex")["rereview_flagged"] == 1


# ---- the accuracy check on the declaration ---------------------------------

async def _two_rounds(client, pr: int, second_file: str):
    """Round 1 flags app/sync.py for re-reading; round 2 raises something new in
    `second_file`."""
    await record(client, pr)
    await record(client, pr, round=2, new_findings=1,
                 round_stop={"stop": False, "reason": "1 finding(s) no earlier round raised",
                             "confident": False, "veto": []},
                 to_fix=[{"severity": "P2", "file": second_file,
                          "title": "dual-keyed node the mirror created",
                          "reviewers": ["claude"], "reason": "real",
                          "new_this_round": True}])
    r = await client.get(f"/review/findings?repo={REPO}&pr={pr}", headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_flag_the_next_round_vindicated_is_recorded_as_a_hit(client):
    """The declaration that would have predicted the round the workflow never
    ran: a structural fix in app/sync.py, and the round after it finds a defect
    there that did not exist until the fix was written."""
    h = await _two_rounds(client, 6130, "app/sync.py")
    first, second = h["runs"]
    assert first["rereview_flagged"] == 1 and first["rereview_hit"] is True
    assert second["round"] == 2 and second["rereview_flagged"] == 0
    # The last round said "go again", so the cycle has not stopped — its reason is
    # a reason to continue, and reporting it as `stopped` called it finished.
    assert h["stopped"] is False
    assert h["stop_reason"].startswith("1 finding") and h["stop_confident"] is False


async def test_a_flag_nothing_followed_up_on_is_recorded_as_a_miss(client):
    """A wrong declaration is data too — honesty per reviewer needs the misses as
    much as the hits, and the declarer cannot mark its own homework."""
    h = await _two_rounds(client, 6131, "app/other.py")
    assert h["runs"][0]["rereview_hit"] is False


async def test_a_finding_the_judge_threw_out_is_not_the_flag_being_borne_out(client):
    """`rereview_hit` is the accuracy check on a declaration the declarer cannot
    mark itself. Letting a false positive the judge dismissed count as the flagged
    fix having gone wrong is the one thing that makes the number uninformative."""
    await record(client, 6134)
    await record(client, 6134, round=2, new_findings=0, to_fix=[],
                 dismissed=[{"severity": "P3", "file": "app/sync.py",
                             "title": "the mirror is redundant", "reviewers": ["claude"],
                             "reason": "not a defect"}])
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6134", headers=AGENT)).json()
    assert h["runs"][0]["rereview_flagged"] == 1
    assert h["runs"][0]["rereview_hit"] is False


async def test_a_later_cycle_is_not_the_answer_to_an_earlier_rounds_flag(client):
    """A standalone `/panel` read, or a new cycle restarting at round 1, lands in
    the next slot by position. Crediting it as the re-review of the earlier round
    attributes one cycle's findings to another cycle's declaration, and this
    number is presented as an honesty measure."""
    await record(client, 6135)                      # round 1, flags app/sync.py
    await record(client, 6135, round=1, new_findings=1,
                 to_fix=[{"severity": "P2", "file": "app/sync.py",
                          "title": "a wholly separate review of the same file",
                          "reviewers": ["claude"], "reason": "real"}])
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6135", headers=AGENT)).json()
    assert [r["round"] for r in h["runs"]] == [1, 1]
    assert h["runs"][0]["rereview_flagged"] == 1
    # A round 1 is nobody's round 2 — unanswered, not vindicated.
    assert h["runs"][0]["rereview_hit"] is None


async def test_one_cycles_re_review_is_not_credited_to_anothers_declaration(client):
    """Two agents looping the same PR interleave: A-r1, B-r1, A-r2. Position plus
    "round is one more" credits B's declaration with A's re-review, and this number
    is published as an honesty measure per reviewer — a wrong attribution there is
    worse than none. The stored cycle id is what makes it a join."""
    await record(client, 6136, cycle="cycle-A")                       # A round 1
    await record(client, 6136, cycle="cycle-B", to_fix=[{             # B round 1
        "severity": "P2", "file": "app/other.py", "title": "b's own finding",
        "reviewers": ["claude"], "reason": "real", "needs_rereview": True,
        "new_this_round": True}])
    await record(client, 6136, cycle="cycle-A", round=2, new_findings=1, to_fix=[{
        "severity": "P2", "file": "app/sync.py", "title": "dual-keyed node",
        "reviewers": ["claude"], "reason": "real", "new_this_round": True}])
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6136", headers=AGENT)).json()
    a1, b1, a2 = h["runs"]
    assert a2["cycle"] == "cycle-A" and a2["round"] == 2
    # A's round 2 answers A's round 1 even though B's run sits between them...
    assert a1["rereview_hit"] is True
    # ...and B's declaration is unanswered, not vindicated by somebody else's round.
    assert b1["rereview_flagged"] == 1 and b1["rereview_hit"] is None


async def test_a_pr_running_one_cycle_still_summarises_its_stop_state(client):
    """The control for the four below: nothing about #44 costs the ordinary PR its
    summary, which is what the endpoint is for."""
    await record(client, 6150, cycle="cycle-A")
    await record(client, 6150, cycle="cycle-A", round=2, new_findings=0,
                 round_stop={"stop": True, "reason": "dry — nothing raised that an "
                             "earlier round had not", "confident": True, "veto": []})
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6150", headers=AGENT)).json()
    assert h["cycles"] == 1
    assert h["stopped"] is True and h["stop_confident"] is True
    assert h["stop_reason"].startswith("dry") and h["stop_veto"] == []


async def test_two_cycles_on_one_pr_do_not_summarise_each_other(client):
    """#44: `stopped`, `stop_reason`, `stop_confident` and `stop_veto` came from
    `runs[-1]` whatever cycle it belonged to, so B's last round decided how A read.
    Here A is explicitly still going and B has stopped, confidently — the old
    summary reported this PR as a confident convergence, and the reader has no way
    to tell that the round which said so belongs to somebody else's loop.

    The per-finding join in the same response has refused this inference since
    cycles became a stored fact. A summary that contradicts the rows underneath it
    is worse than an absent one."""
    await record(client, 6151, cycle="cycle-A")   # A is going again: stop False
    await record(client, 6151, cycle="cycle-B", round=1, new_findings=0, to_fix=[],
                 round_stop={"stop": True, "reason": "dry — nothing raised that an "
                             "earlier round had not", "confident": True, "veto": []})
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6151", headers=AGENT)).json()
    assert h["cycles"] == 2
    assert h["stopped"] is None and h["stop_reason"] is None
    assert h["stop_confident"] is None
    # NULL, not []: the same distinction `GET /review/{id}` draws. [] would say the
    # stopping rule ran and vetoed nothing.
    assert h["stop_veto"] is None
    # Only the SUMMARY is withheld. The rounds and the chains are per-run facts and
    # are unaffected — withholding them would answer less than the record supports.
    assert h["rounds"] == 2 and len(h["runs"]) == 2
    # BOTH values, because the point of this test is the disagreement between them:
    # A is explicitly still going while B has confidently stopped. Asserting only
    # B's leaves the test passing if A's per-run `stopped` regressed to True or
    # null, which is the half that made the old summary a lie.
    assert h["runs"][0]["cycle"] == "cycle-A" and h["runs"][0]["stopped"] is False
    assert h["runs"][1]["cycle"] == "cycle-B" and h["runs"][1]["stopped"] is True


async def test_a_review_only_run_neither_ends_a_cycle_nor_hides_one(client):
    """A standalone `/panel` read carries no cycle, so it never ended the cycle
    running around it. That premise cuts one way only: the run is SKIPPED, not
    counted as a rival. It cannot supply the ending, and it cannot veto the ending
    of a loop it was no part of.

    The rule that shipped first counted it as a bucket of its own and so nulled the
    summary here — which made the common shape (any PR read once outside its loop)
    unattributable for as long as that read stayed in the window, on the strength
    of a run that ended nothing. Three panel rounds raised it before it changed.

    The cycle-less run is NEWEST here on purpose: that is the arrangement where
    `runs[-1]` is the wrong run, so this pins the ending coming from cycle A's own
    last round rather than from the read that happened to land after it."""
    await record(client, 6152, cycle="cycle-A", round=1)
    await record(client, 6152, cycle="cycle-A", round=2, new_findings=0,
                 round_stop={"stop": True, "reason": "dry — nothing raised that an "
                             "earlier round had not", "confident": True, "veto": []})
    await record(client, 6152, cycle=None, round=1, new_findings=0, to_fix=[],
                 round_stop={"stop": True, "reason": "one-shot read", "confident": False,
                             "veto": ["a one-shot read is not a cycle"]})
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6152", headers=AGENT)).json()
    assert h["cycles"] == 1, "one loop ran here, and one is what it says"
    assert h["stopped"] is True
    # Cycle A's ending, NOT the cycle-less run's — which is newer, and says
    # something different about both the reason and the confidence.
    assert h["stop_reason"].startswith("dry"), "the read that ended nothing did not speak"
    assert h["stop_confident"] is True and h["stop_veto"] == []
    # The run is still reported, just not as an ending: withholding it would answer
    # less than the record supports.
    assert h["rounds"] == 3 and h["runs"][-1]["cycle"] is None


async def test_history_recorded_before_cycles_existed_still_summarises(client):
    """Every run predating the cycle column has a null one, so a window of them is
    one bucket and reads exactly as it always did. Treating null as "unknown, and
    therefore ambiguous" would have withheld the summary from all of the archive to
    describe a case that cannot arise in it."""
    await record(client, 6153, cycle=None)
    await record(client, 6153, cycle=None, round=2, new_findings=0,
                 round_stop={"stop": True, "reason": "dry", "confident": True, "veto": []})
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6153", headers=AGENT)).json()
    assert h["cycles"] == 0, "no cycle ran here, and zero is a real answer"
    assert h["stopped"] is True and h["stop_reason"] == "dry"


async def test_narrowing_the_window_to_one_cycle_brings_the_summary_back(client):
    """What a caller does about a null summary, and the reason nulling is not a
    dead end: `limit` decides the window, so a window of one cycle summarises. The
    docstring says so, which makes it a promise worth pinning."""
    await record(client, 6154, cycle="cycle-A")
    await record(client, 6154, cycle="cycle-B", round=1, new_findings=0, to_fix=[],
                 round_stop={"stop": True, "reason": "dry", "confident": True, "veto": []})
    wide = (await client.get(f"/review/findings?repo={REPO}&pr=6154",
                             headers=AGENT)).json()
    assert wide["cycles"] == 2 and wide["stopped"] is None
    narrow = (await client.get(f"/review/findings?repo={REPO}&pr=6154&limit=1",
                               headers=AGENT)).json()
    assert narrow["cycles"] == 1 and narrow["stopped"] is True
    # ...and it says so: the window it summarises is not the PR's whole history.
    assert narrow["truncated"] is True


async def test_narrowing_the_window_costs_the_finding_history_it_narrows(client):
    """The other half of that remedy, which the docstring used to present as a
    clean escape hatch and now presents as a trade.

    `limit` is ONE window: the same runs that decide whether there is a summary
    decide `first_run`, the `gone` status and what counts as new. So the response
    that got its summary back is a response with less history in it — here cycle
    A's round and the chain it opened are simply not in the answer any more.

    And the escape only ever reaches one direction. `limit` trims from the OLD end,
    so the summary it recovers is always the NEWEST bucket's; no value of it
    recovers cycle A's ending, which is the one a reader of cycle A wants."""
    await record(client, 6155, cycle="cycle-A")
    await record(client, 6155, cycle="cycle-B", round=1, new_findings=0, to_fix=[
        {"severity": "P3", "file": "app/late.py", "line": 4, "title": "B's own find",
         "reviewers": ["claude"], "reason": "real"}],
        round_stop={"stop": True, "reason": "dry", "confident": True, "veto": []})
    wide = (await client.get(f"/review/findings?repo={REPO}&pr=6155",
                             headers=AGENT)).json()
    narrow = (await client.get(f"/review/findings?repo={REPO}&pr=6155&limit=1",
                               headers=AGENT)).json()
    assert wide["stopped"] is None and narrow["stopped"] is True
    # What it cost: A's round, and the chain that only A ever raised.
    assert wide["rounds"] == 2 and narrow["rounds"] == 1
    assert len(narrow["findings"]) < len(wide["findings"])
    assert not [c for c in narrow["findings"] if c["file"] == "app/sync.py"]
    # There is no `limit` that summarises cycle A instead: the window only ever
    # trims the old end, so A's ending is unreachable from this endpoint.
    for lim in (1, 2, 50):
        h = (await client.get(f"/review/findings?repo={REPO}&pr=6155&limit={lim}",
                              headers=AGENT)).json()
        assert h["stopped"] is not False, "no window reports cycle A's own ending"


async def test_two_real_cycles_stay_unattributable_whichever_one_ends_newest(client):
    """The rule counts distinct cycles and never consults position, and this is
    where that has to hold: interleave the two loops so the OLDER cycle owns the
    newest round. Any rule reaching for `runs[-1]`, or for "the newest cycle forms
    a contiguous tail", reports an ending here. There isn't one to report."""
    await record(client, 6170, cycle="cycle-A", round=1)
    await record(client, 6170, cycle="cycle-B", round=1, new_findings=0, to_fix=[],
                 round_stop={"stop": True, "reason": "dry", "confident": True, "veto": []})
    await record(client, 6170, cycle="cycle-A", round=2, new_findings=0,
                 round_stop={"stop": True, "reason": "dry", "confident": True, "veto": []})
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6170", headers=AGENT)).json()
    assert h["cycles"] == 2, "two loops, interleaved, still two"
    assert h["stopped"] is None and h["stop_veto"] is None, (
        "adjacency is not attribution — the count of distinct cycles decides")
    assert h["rounds"] == 3


async def test_cycles_is_a_count_and_not_a_boolean_in_disguise(client):
    """Three buckets read as three. Nothing downstream should be able to get away
    with treating the field as "1 or many"."""
    await record(client, 6171, cycle="cycle-A")
    await record(client, 6171, cycle="cycle-B", round=1)
    await record(client, 6171, cycle="cycle-C", round=1)
    await record(client, 6171, cycle=None, round=1, new_findings=0, to_fix=[])
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6171", headers=AGENT)).json()
    assert h["cycles"] == 3, "three loops ran; the cycle-less read is not a fourth"
    assert h["stopped"] is None


async def test_one_bucket_in_the_window_is_not_one_bucket_in_the_pr(client):
    """The summary is a claim about the WINDOW. An older cycle that falls outside
    `limit` leaves a window holding one bucket, so the endpoint summarises — and
    what it summarises is the newest cycle's ending, not the PR's.

    That is not nulled away, because `limit`-narrowing is the documented way to
    recover a summary and it truncates on purpose. It is `truncated` that carries
    the scope, which is why the docstring no longer calls `cycles` the single
    field answering "can I trust the summary": read together or not at all."""
    await record(client, 6172, cycle="cycle-A", round=1)          # older, falls outside
    await record(client, 6172, cycle="cycle-B", round=1, new_findings=0, to_fix=[],
                 round_stop={"stop": True, "reason": "dry", "confident": True, "veto": []})
    wide = (await client.get(f"/review/findings?repo={REPO}&pr=6172", headers=AGENT)).json()
    assert wide["cycles"] == 2 and wide["stopped"] is None, "both cycles visible: no claim"
    narrow = (await client.get(f"/review/findings?repo={REPO}&pr=6172&limit=1",
                               headers=AGENT)).json()
    assert narrow["cycles"] == 1, "cycle A is outside the window, so it is not counted"
    assert narrow["stopped"] is True, "and the window's one bucket summarises"
    # The pair is the contract: a summary with `truncated` set describes the
    # window. Either field alone reads as a statement about the PR, and is not.
    assert narrow["truncated"] is True


async def test_an_attributable_run_that_recorded_no_veto_answer_reads_null(client):
    """The three-state rule has to hold hardest in the branch that DOES summarise.
    The window is one cycle, so the summary speaks for exactly one run — and that
    run recorded no `round_stop` at all, so its `stop_veto` is NULL. `or []` here
    reported the opposite of the truth about the very run the summary rests on:
    `[]` is "the stopping rule ran and vetoed nothing", and nothing ran.

    `GET /review/{id}` has always returned it raw; this endpoint now agrees rather
    than contradicting its sibling about one stored row."""
    await record(client, 6160, cycle="cycle-A", round_stop=None)
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6160", headers=AGENT)).json()
    assert h["cycles"] == 1, "one bucket, so this window is summarisable"
    assert h["stop_veto"] is None, "NULL, not [] — that round recorded no veto answer"
    # The other three are null for the same reason, and were already.
    assert h["stopped"] is None and h["stop_reason"] is None
    assert h["stop_confident"] is None


async def test_the_per_run_rows_carry_a_null_veto_unaltered(client):
    """`runs[]` is what the docstring, the README and the CHANGELOG all point
    callers at as the better answer, on the stated promise that each round's own
    four ride there UNALTERED at any window size. An `or []` made that promise
    false for a round with no recorded veto — and made one stored row read
    differently through this endpoint than through `GET /review/{id}`, which is the
    disagreement `test_a_nested_stop_that_did_not_say_records_no_stop` pins from
    the other side."""
    run_id = await record(client, 6161, cycle="cycle-A", round_stop=None)
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6161", headers=AGENT)).json()
    row = next(r for r in h["runs"] if r["id"] == run_id)
    assert row["stop_veto"] is None, "unaltered means NULL stays NULL"
    # ...and the two endpoints agree about the same row, which is the point.
    assert (await detail(client, run_id))["stop_veto"] is None


async def test_a_recorded_empty_veto_is_still_an_empty_list(client):
    """The other half of the distinction, or the fix above would just be a new way
    of losing information: a round that DID run its stopping rule and vetoed
    nothing recorded `[]`, and `[]` is what it must read as — through the summary
    and through the per-run row alike."""
    run_id = await record(client, 6162, cycle="cycle-A",
                          round_stop={"stop": True, "reason": "dry", "confident": True,
                                      "veto": []})
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6162", headers=AGENT)).json()
    assert h["stop_veto"] == [], "the rule ran and vetoed nothing — not null"
    assert next(r for r in h["runs"] if r["id"] == run_id)["stop_veto"] == []


# ---- the page's half of #44 -------------------------------------------------
# There is no JS runner in this repo (no package.json, no jest/vitest config), so
# these grep the file that ships, the way `test_reviewer_cost.py` already does for
# its coverage markers. Crude, and the rendering itself remains unexercised — but
# it is the only thing standing between a re-edit and the two claims the API's
# comments rest on. Synchronous and fixture-free: they read a file that ships,
# and nothing about them wants a database.

def test_the_page_never_reads_the_summary_stop_veto_unguarded():
    """The load-bearing safety argument for making a list field nullable is that
    every consumer reads it as `stop_veto || []`, and until now that was enforced
    by prose in a Python comment about a JavaScript file.

    reviews.html is the only consumer of `GET /review/findings` in the repo, and
    the summary's `stop_veto` is null whenever the window holds more than one
    bucket. A future `for (const v of h.stop_veto)` would throw on exactly the
    mixed window this feature creates, so the count is pinned: one guard, and two
    mentions inside the ternary that guard opens. Any new use fails here and has to
    be re-read rather than discovered in a browser."""
    page = (Path(__file__).resolve().parents[1] / "app/static/reviews.html").read_text(
        encoding="utf-8")
    assert page.count("(h.stop_veto || [])") == 1, "the summary's veto list has one guard"
    assert page.count("h.stop_veto") == 3, (
        "the guard, plus `.join` and `.length` in its true arm — a fourth mention "
        "is a read this test has not seen and cannot vouch for")


def test_the_page_never_reads_a_PER_RUN_stop_veto_unguarded():
    """`runs[].stop_veto` became nullable in the same change and for the same
    reason, and it is a SECOND field under a different name: the audit above
    reasons only about the summary, so a row renderer doing `r.stop_veto.length`
    was invisible to it. This is that field's own pin.

    Verified rather than assumed: the page reads a per-run veto in three places
    (the run card's count, its title, and the expanded round's list), and every one
    of them opens with `(r.stop_veto || [])` and short-circuits on `.length` before
    anything calls `.join`/`.map` — so a null row renders as no vetoes rather than
    throwing. `harness/loops/preland.py` is NOT a consumer of either field: it
    rules on rows from `GET /reviews`, a different endpoint."""
    page = (Path(__file__).resolve().parents[1] / "app/static/reviews.html").read_text(
        encoding="utf-8")
    assert page.count("(r.stop_veto || [])") == 2, (
        "both entry points to a per-run veto are guarded — the run card and the "
        "expanded round")
    # Every mention is inside a guard or inside the arm one opened. A new read
    # outside them lands here rather than in a browser on a pre-cycle row.
    assert page.count("r.stop_veto") == 5, (
        "two guards, plus `.join`/`.length` in the card's arm and `.map` in the "
        "round's — a sixth mention is a read this test has not seen")


def test_the_page_reads_the_cycle_count_before_the_stop_state():
    """The rendering branch the API tests above justify, pinned as far as a repo
    with no DOM harness can pin it.

    Order is the substance: with two cycles `h.stopped` is null, and null is not
    `false`, so a check placed after the `h.stopped === false` arm would fall
    through and print nothing — the blank the API's comment says must not happen.

    Every marker is checked for existence before it is used, so a reformat of the
    file fails here with a sentence about what broke rather than an `IndexError`
    from `split(...)[1]` — which is the whole point of a test that exists to be
    read by whoever next edits the page."""
    page = (Path(__file__).resolve().parents[1] / "app/static/reviews.html").read_text(
        encoding="utf-8")
    for marker in ("const ending = ", "box.innerHTML", "h.cycles > 1",
                   "h.stopped === false"):
        assert marker in page, f"the page no longer contains {marker!r} — this test " \
                               "pins the ending ternary and cannot find it"
    ending = page.split("const ending = ", 1)[1].split("box.innerHTML", 1)[0]
    assert ending.index("h.cycles > 1") < ending.index("h.stopped === false"), (
        "the cycle arm must come first: `h.stopped` is null here, not false")
    mixed = ending.split("h.stopped === false", 1)[0]
    # Said as loudly as the other endings that report something wrong with the
    # review, not dimmed to the weight of the incidental veto count beside it.
    # Defensible because the arm is now RARE: it takes two real cycles, not one
    # stray cycle-less read.
    assert "var(--warn)" in mixed and 'class="dim"' not in mixed
    # Escaped like every other server value on this line, however integral `len()`
    # makes it — the convention is what stops the next value being interpolated raw.
    assert "esc(h.cycles)" in mixed and "${h.cycles}" not in mixed
    # `cycles` counts cycles, so the copy is allowed to say so plainly. It was not
    # allowed to when the same field counted buckets.
    assert "cycles ran here" in mixed


async def test_a_finding_older_than_the_window_is_not_new_inside_it(client):
    """"New" used to be first appearance within the traced window, so a round that
    fell outside `limit` made a long-standing finding read as fresh — falsely
    vindicating the re-review flag pointing at its file. The panel already computed
    the answer against the real baseline, and now that is what counts."""
    await record(client, 6137)                                  # round 1 flags app/sync.py
    await record(client, 6137, round=2, new_findings=0, to_fix=[{
        "severity": "P2", "file": "app/sync.py", "title": "the same defect again",
        "reviewers": ["claude"], "reason": "real",
        # First time this KEY appears in the window, but the panel's baseline says
        # an earlier round already raised it.
        "new_this_round": False}])
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6137", headers=AGENT)).json()
    assert h["runs"][0]["rereview_hit"] is False


async def test_a_cycle_less_pair_is_unanswered_rather_than_guessed_at(client):
    """The positional fallback took the adjacent run whenever the cycle was null
    and its round was one more. A-r1 followed by B-r2 — two agents, two cycles,
    nobody naming one — then credited B's findings as the answer to A's
    declaration. This number is published as an honesty measure, so an unknown
    attribution is null, not a guess. Nothing real is lost: the flag column and the
    cycle id shipped in the same release."""
    await record(client, 6138, cycle=None)
    await record(client, 6138, cycle=None, round=2, new_findings=1,
                 to_fix=[{"severity": "P2", "file": "app/sync.py",
                          "title": "somebody else's finding", "reviewers": ["claude"],
                          "reason": "real", "new_this_round": True}])
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6138", headers=AGENT)).json()
    assert h["runs"][0]["rereview_flagged"] == 1
    assert h["runs"][0]["rereview_hit"] is None


async def test_a_short_path_and_the_full_one_score_as_the_same_file(client):
    """Reviewers spell paths differently and the judge takes whichever spelling it
    likes per round — `panel.py::_same_file` exists for exactly that. Scoring the
    re-review flag on exact string equality made an honest declaration about
    `sync.py` a miss against the next round's `app/sync.py`, and that error only
    ever runs one way: against the reviewer."""
    await record(client, 6145, to_fix=[{
        "severity": "P2", "file": "sync.py", "title": "half-stale node",
        "reason": "real", "needs_rereview": True, "new_this_round": True,
        "reported_by": [{"reviewer": "codex", "account": "a", "needs_rereview": True}]}])
    await record(client, 6145, round=2, new_findings=1, to_fix=[{
        "severity": "P2", "file": "app/sync.py", "title": "the early return again",
        "reviewers": ["claude"], "reason": "real", "new_this_round": True}])
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6145", headers=AGENT)).json()
    assert h["runs"][0]["rereview_hit"] is True
    assert h["runs"][0]["rereview_by_reviewer"]["codex"]["hit"] is True


async def test_two_files_that_merely_share_a_basename_are_not_one_file(client):
    """The suffix rule is a path suffix, not a basename match — `api/tests/x.py`
    and `web/tests/x.py` are two files, and crediting one flag for the other would
    be the same over-scoring in the opposite direction."""
    await record(client, 6146, to_fix=[{
        "severity": "P2", "file": "api/tests/x.py", "title": "half-stale node",
        "reviewers": ["codex"], "reason": "real",
        "needs_rereview": True, "rereview_by": ["codex"], "new_this_round": True}])
    await record(client, 6146, round=2, new_findings=1, to_fix=[{
        "severity": "P2", "file": "web/tests/x.py", "title": "somewhere else",
        "reviewers": ["claude"], "reason": "real", "new_this_round": True}])
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6146", headers=AGENT)).json()
    assert h["runs"][0]["rereview_hit"] is False


async def test_a_nested_stop_that_did_not_say_records_no_stop(client):
    """The flat path's rule, stated in `record_review`: a caller that says nothing
    about whether the cycle stopped records NULL, not a guessed True. `stop`
    defaulting to True made `round_stop: {"reason": ..., "veto": [...]}` render a
    running cycle as finished."""
    run = await detail(client, await record(client, 6147, round_stop={
        "reason": "1 finding(s) no earlier round raised",
        "veto": ["codex saw 60,000 of 118,402 diff chars"]}))
    assert run["stopped"] is None
    assert run["stop_reason"] == "1 finding(s) no earlier round raised"
    assert run["stop_veto"] == ["codex saw 60,000 of 118,402 diff chars"]


async def test_a_declaration_that_is_not_a_phrase_is_dropped_not_stringified(client):
    """`str(x)` on a dict stored the Python repr `"{'area': 'the migration'}"` as
    something a reviewer declared, and `/panel` then printed it verbatim. The
    helper's contract is that anything unusable becomes nothing."""
    run = await detail(client, await record(client, 6148, reviewers={
        "codex": {"model": "gpt-5.6", "ran": True,
                  "could_not_assess": [{"area": "the migration"}, "the migration"]}}))
    assert card(run, "codex")["could_not_assess"] == ["the migration"]


async def test_a_flag_the_judge_never_ruled_on_is_not_a_scorable_prediction(client):
    """The two halves of one measurement used different populations. `flagged`
    counted every verdict including `dismissed`, so a declaration attached to a
    finding the judge threw out inflated `rereview_flagged` and put its file where
    it could only register as a miss — scoring a reviewer as having predicted
    wrongly when it had made no scorable prediction at all."""
    await record(client, 6139, judged=False, to_fix=[{
        "severity": "P2", "file": "app/sync.py", "title": "nobody ruled on this",
        "reviewers": ["codex"], "reason": "unjudged", "needs_rereview": True,
        "rereview_by": ["codex"]}])
    await record(client, 6139, round=2, new_findings=1, to_fix=[{
        "severity": "P2", "file": "app/sync.py", "title": "something new here",
        "reviewers": ["claude"], "reason": "real", "new_this_round": True}])
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6139", headers=AGENT)).json()
    assert h["runs"][0]["rereview_flagged"] == 0
    assert h["runs"][0]["rereview_hit"] is None
    # ...and the scorecard counts the same population, so the detail table and the
    # history block printed under it cannot publish two different numbers under one
    # name. A flag on a finding nobody ruled on is scored nowhere.
    run = await detail(client, h["runs"][0]["id"])
    assert card(run, "codex")["rereview_flagged"] == 0


async def test_an_unjudged_finding_does_not_vindicate_the_flag_that_pointed_at_it(client):
    """The docstring says only findings that survived the judge count as new, and
    the page repeats it, but the predicate admitted `unjudged` — so a round 2 whose
    judge crashed vindicated every flag round 1 aimed at those files, on the
    strength of findings nobody ruled on."""
    await record(client, 6141)                     # round 1 flags app/sync.py
    await record(client, 6141, round=2, new_findings=1, judged=False, to_fix=[{
        "severity": "P2", "file": "app/sync.py", "title": "the judge died on this one",
        "reviewers": ["claude"], "reason": "unjudged", "new_this_round": True}])
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6141", headers=AGENT)).json()
    assert h["runs"][0]["rereview_flagged"] == 1
    assert h["runs"][0]["rereview_hit"] is False


async def test_the_re_review_check_is_answered_per_member_that_made_it(client):
    """Run-level, the honest and the quiet member are indistinguishable on exactly
    the statistic that separates them: two members flagging different files get one
    boolean between them. The declaration rides on the reporter's own row, so who
    was borne out is answerable."""
    await record(client, 6142, to_fix=[
        {"severity": "P2", "file": "app/sync.py", "title": "the mirror is structural",
         "reason": "real", "new_this_round": True,
         "reported_by": [
             {"reviewer": "codex", "severity": "P2", "account": "read the result",
              "needs_rereview": True},
             {"reviewer": "claude", "severity": "P2", "account": "no need"}]},
        {"severity": "P3", "file": "app/other.py", "title": "and this one too",
         "reason": "real", "new_this_round": True,
         "reported_by": [{"reviewer": "claude", "severity": "P3", "account": "structural",
                          "needs_rereview": True}]},
    ])
    await record(client, 6142, round=2, new_findings=1, to_fix=[{
        "severity": "P2", "file": "app/sync.py", "title": "dual-keyed node",
        "reviewers": ["claude"], "reason": "real", "new_this_round": True}])
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6142", headers=AGENT)).json()
    r1 = h["runs"][0]
    assert r1["rereview_flagged"] == 2 and r1["rereview_hit"] is True
    # codex pointed at app/sync.py and the next round found something there;
    # claude pointed at app/other.py and it did not. One run, two answers.
    assert r1["rereview_by_reviewer"] == {
        "codex": {"flagged": 1, "hit": True},
        "claude": {"flagged": 1, "hit": False},
    }


async def test_a_flag_with_no_round_after_it_is_unanswered_not_wrong(client):
    """None, not False: nobody looked. Scoring an unrun round as a miss would
    punish the reviewer for the workflow stopping."""
    await record(client, 6132)
    h = (await client.get(f"/review/findings?repo={REPO}&pr=6132", headers=AGENT)).json()
    assert h["runs"][0]["rereview_flagged"] == 1
    assert h["runs"][0]["rereview_hit"] is None


async def test_a_chain_carries_the_declaration_it_was_given(client):
    h = await _two_rounds(client, 6133, "app/sync.py")
    flagged = [c for c in h["findings"] if c["needs_rereview"]]
    assert [c["file"] for c in flagged] == ["app/sync.py"]


async def test_a_finding_level_flag_credits_the_members_that_sent_no_account(client):
    """`reported_by` present, nobody's own flag set, no `rereview_by` — the
    fallback was gated on there being no accounts at all, so the flag was stored on
    the finding while no reviewer's `rereview_flagged` counted it and the two views
    of one declaration disagreed. A member that sent no account is not authoritative
    about its own silence, so it is the one to credit."""
    run = await detail(client, await record(client, 6125, to_fix=[{
        "severity": "P2", "file": "a.py", "title": "structural", "reason": "real",
        "reviewers": ["claude", "codex"], "needs_rereview": True,
        "reported_by": [{"reviewer": "claude", "account": "not structural",
                         "needs_rereview": False}],
    }]))
    assert run["findings"][0]["needs_rereview"] is True
    assert card(run, "codex")["rereview_flagged"] == 1
    # claude said false about itself, and that stands.
    assert card(run, "claude")["rereview_flagged"] == 0


async def test_a_flag_every_reporter_denied_is_recorded_and_credited_to_nobody(client):
    """The remaining asymmetry, stated rather than papered over: every credited
    member sent an account and every one said false, so there is nobody left to
    credit and filling it in would manufacture a declaration. The finding keeps the
    caller's flag — a caller contradicting itself, recorded rather than resolved."""
    run = await detail(client, await record(client, 6126, to_fix=[{
        "severity": "P2", "file": "a.py", "title": "structural", "reason": "real",
        "reviewers": ["claude"], "needs_rereview": True,
        "reported_by": [{"reviewer": "claude", "account": "no", "needs_rereview": False}],
    }]))
    assert run["findings"][0]["needs_rereview"] is True
    assert card(run, "claude")["rereview_flagged"] == 0


async def test_a_flag_naming_only_unknown_members_credits_someone(client):
    """`rereview_by: ["gemini"]` on a finding credited to codex — a retired member,
    a typo, a reviewer merged out. The filtered attribution comes back empty, and
    the fallback used to be skipped because `rereview_by` was non-empty: the flag
    was stored with nobody credited and nothing tallied, which is exactly the
    silent drop the fallback exists to prevent."""
    run = await detail(client, await record(client, 6124, to_fix=[{
        "severity": "P2", "file": "a.py", "title": "structural", "reason": "real",
        "reviewers": ["codex"], "needs_rereview": True, "rereview_by": ["gemini"],
    }]))
    assert run["findings"][0]["needs_rereview"] is True
    assert card(run, "codex")["rereview_flagged"] == 1


# ---- best-effort ingest ----------------------------------------------------

async def test_a_garbled_round_costs_the_number_not_the_whole_record(client):
    """This module's rule is that a review must never fail because the board
    choked (see `_line_or_none`). A `round: 0` or `new_findings: -1` from a
    hand-rolled caller used to 422 the payload, losing the findings, the
    scorecards and the accounts along with the bad integer."""
    r = await client.post("/review", json=payload(6150, round=0, new_findings=-1),
                          headers=AGENT)
    assert r.status_code == 201, r.text
    run = await detail(client, r.json()["id"])
    assert run["round"] == 1               # rounds are numbered from 1
    assert run["new_findings"] is None     # "the panel did not say", not "none"
    assert len(run["findings"]) == 1       # ...and the review itself survived


async def test_a_fractional_count_is_not_believed_rather_than_truncated(client):
    """`int(1.9)` is 1. Silently changing a caller's meaning is a different failure
    from the documented "a value that cannot be believed becomes None", and this is
    the helper that defines that policy."""
    r = await client.post("/review", json=payload(6151, round=2.7, new_findings=1.9),
                          headers=AGENT)
    assert r.status_code == 201, r.text
    run = await detail(client, r.json()["id"])
    assert run["round"] == 1 and run["new_findings"] is None


async def test_a_declaration_spelled_as_a_string_costs_nothing(client):
    """This module's rule is that ingest is best-effort: a hand-rolled caller must
    not lose its findings, its scorecards and its accounts to one badly-spelled
    field. `could_not_assess` and `veto` were the only strictly-typed ones, and a
    bare string is exactly the shape `panel.py::_str_list` tolerates on the way
    in."""
    r = await client.post("/review", json=payload(
        6152,
        # Its own repo: the leaderboard aggregates per repo, and a coerced
        # declaration here would otherwise show up as one claude really made.
        repo=f"{REPO}-coercion",
        reviewers={"claude": {"model": "sonnet", "ran": True,
                              "could_not_assess": "the migration"},
                   "codex": {"model": "gpt-5.6", "ran": True, "could_not_assess": 7}},
        round_stop={"stop": True, "reason": "capped", "confident": False,
                    "veto": "round cap (2) reached"}), headers=AGENT)
    assert r.status_code == 201, r.text
    run = await detail(client, r.json()["id"])
    assert card(run, "claude")["could_not_assess"] == ["the migration"]
    # An unreadable shape is "no declaration obtained" — NULL — not [], which
    # would claim the member was asked and had nothing to say.
    assert card(run, "codex")["could_not_assess"] is None
    assert run["stop_veto"] == ["round cap (2) reached"]
    assert len(run["findings"]) == 1


async def test_an_empty_veto_is_not_the_same_as_no_panel_having_said(client):
    """`veto or None` collapsed "the panel ran the stopping rule and found nothing
    to veto" onto "no panel ever said" — the same NULL/[] collapse this release
    argues at length must not happen to `could_not_assess`, one field over."""
    ran = await record(client, 6153, round_stop={
        "stop": True, "reason": "dry", "confident": True, "veto": []})
    async with engine.connect() as conn:
        stored = await conn.scalar(
            text("SELECT stop_veto FROM review_runs WHERE id = :i"), {"i": ran})
    assert stored == []
    # ...and a run whose caller sent no verdict at all still stores NULL.
    plain = await record(client, 6154, round_stop=None)
    async with engine.connect() as conn:
        assert await conn.scalar(
            text("SELECT stop_veto FROM review_runs WHERE id = :i"), {"i": plain}) is None


async def test_a_duplicated_name_in_rereview_by_is_one_declaration(client):
    """`rereview_by: ["codex", "codex"]` — trivially produced by a caller merging
    two reviewer lists — tallied two flags for one finding, and nothing behind it
    catches the repeat: these names create no report rows for the (finding,
    reviewer) constraint to reject."""
    run = await detail(client, await record(client, 6155, to_fix=[{
        "severity": "P2", "file": "a.py", "title": "structural", "reason": "real",
        "reviewers": ["codex"], "needs_rereview": True,
        "rereview_by": ["codex", "codex"]}]))
    assert card(run, "codex")["rereview_flagged"] == 1


# ---- the stats side --------------------------------------------------------

async def test_coverage_counters_reach_the_leaderboard(client):
    await record(client, 6140)
    s = (await client.get(f"/review/stats?repo={REPO}", headers=AGENT)).json()
    # Keyed by (reviewer, model) like the leaderboard itself groups: the same
    # vendor at two tiers is two competitors, and a run that recorded no model
    # for it is a third row rather than the same one.
    rows = {(m["reviewer"], m["model"]): m for m in s["by_model"]}
    codex = rows[("codex", "gpt-5.6")]
    assert codex["truncated_runs"] >= 1
    assert codex["declared_gaps_runs"] >= 1
    assert codex["rereview_flagged"] >= 1
    # claude declared [] — asked, nothing to say — which is not a declared gap.
    claude = rows[("claude", "sonnet")]
    assert claude["declared_gaps_runs"] == 0
    assert claude["truncated_runs"] == 0


# ---- the two halves agree on what a defect IS ------------------------------

KEY_CASES = (("app/sync.py", "half-stale node after the early return"),
             ("a.py", "Unicode dash — survives the strip!"),
             (None, ""),
             ("x.py", "   spaced   out   "),
             ("b.py", "MiXeD CaSe 42 and_underscores"))


def test_the_panel_and_the_board_derive_the_same_defect_key():
    """The panel now sends `key` so the local round-over-round diff and the
    board's cross-run chains are provably the same identity. They are two
    implementations of one recipe (a third lives in migration 0012's SQL), so
    drift between them is silent: the round diff would say "new" about a finding
    the chain says is old, and only one of the two is on screen."""
    for file, title in KEY_CASES:
        # The panel keys a defect off the reporters' own titles, so the recipe is
        # fed a reviewer's report rather than a bare string.
        reports = [panel.Finding("codex", "P2", file or "", 7, title, "detail")]
        # Compared against the board's whole ingest path, not against
        # `_derive_key` alone: `_prepare` substitutes "(untitled)" for a missing
        # title BEFORE deriving, and migration 0012 keys off the column that
        # substitution wrote. A test that skips it measures a call the board
        # never makes, and reports drift where there is none — which is exactly
        # how someone "fixes" the panel into producing a key no other
        # implementation can reach.
        assert panel._defect_key(file or "", reports) == _derive_key(file, title or "(untitled)")


async def test_the_migrations_sql_derives_the_same_defect_key_too():
    """The third implementation, and the one nobody had checked: migration 0012
    backfills every pre-v2.11 finding's key in SQL. If its regexp, its `btrim` or
    its `substr` disagrees with the Python by one character, the old rows join no
    chain — silently, because a key that links nothing looks exactly like a defect
    that was only ever seen once. Run against the live database, since the answer
    depends on Postgres' `lower`/`regexp_replace`, not on our reading of them."""
    spec = importlib.util.spec_from_file_location(
        "_m0012",
        Path(__file__).resolve().parents[1] / "migrations/versions/0012_review_finding_reports.py",
    )
    m0012 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m0012)
    stmt = text(f"SELECT {m0012._KEY_SQL} FROM "
                "(VALUES (cast(:file as text), cast(:title as text))) AS t(file, title)")
    async with engine.connect() as conn:
        for file, title in KEY_CASES:
            got = await conn.scalar(stmt, {"file": file, "title": title})
            assert got == _derive_key(file, title), (file, title)


# ---- the two halves, end to end --------------------------------------------

PANEL_CFG = {
    "github": "acme/e2e",
    "path": "/tmp/acme-e2e",
    # The filename that supplied the baseline, which is what `review_refusal` reads
    # to tell a CONFIGURED repo from one running on built-in defaults. This fixture
    # stands in for a configured repo — without the key the panel correctly refuses
    # to review, and this test's whole subject (the payload the board reads back)
    # is never produced.
    "_rules_baseline": ".harness-rules.sample",
    # codex's cap is the subject of the round below: it has to CUT the 268-char
    # diff (that is what `reviewers.codex.truncated` asserts) without putting the
    # round past the pre-flight refusal threshold, which is 3x the tightest seat
    # ceiling (#138). 120 is 2.2x — truncated, comfortably short of a refusal.
    #
    # It was 40 — 6.7x — and that is #239's whole mechanism rather than a typo.
    # `seat_ceilings` used to resolve "is this seat here" for ITSELF instead of
    # taking the round's snapshot, so the verdict read the real PATH while
    # `_every_seat_installed` above pinned the budgets to a box carrying
    # everything. On a machine with no `codex` binary the two disagreed in the
    # direction that hid the problem: the budgets gave codex a 40-char cap and the
    # verdict, seeing no codex on PATH, weighed the diff against no ceiling at all
    # and let the round run. With a codex installed the verdict saw the cap and
    # refused, and this test failed — passing or failing on whether a vendor binary
    # was installed, which is exactly what #239 is titled for.
    #
    # The panel now hands the verdict the round's own snapshot, so there is ONE
    # answer to which seats exist and this test's outcome no longer depends on the
    # host. That answer is the pinned one — codex is here, with its configured cap
    # — and at 40 it refuses the round on every box rather than on some of them. So
    # the cap moves to a value that means what this fixture always intended.
    "reviewers": {"claude": {"enabled": True, "model": "sonnet"},
                  "codex": {"enabled": True, "model": "gpt-5.6", "max_diff_chars": 120}},
    "review_panel": {},
}
DIFF = "diff --git a/app/sync.py b/app/sync.py\n@@ -1,1 +1,2 @@\n+mirror = {}\n" + "x" * 200


def _fake_sh(args, **kw):
    if args[:3] == ["gh", "pr", "view"]:
        return json.dumps({"title": "feat: mirror", "additions": 20, "deletions": 2,
                           "baseRefName": "main", "headRefName": "feat/x",
                           "headRefOid": "abc123"})
    return DIFF


def _panel_round(monkeypatch, tmp_path, round_no, title, baseline=()):
    """One panel run with every process it would spawn replaced — the reviewers,
    the judge, the CI check — so what is under test is the payload the panel
    builds, not the CLIs."""
    def fake_review(name, model, prompt, effort="", code_tree=None, budget_usd=None):
        if name == "codex":
            # Blind and kernel-capped: codex cannot be given read tools at all
            # (its only read path is its shell), and it stands in here for the
            # seat whose truncation is the box's rather than a budget's — so this
            # row exercises every one of #113's new reviewer columns at once.
            return panel.ReviewerRun(
                [panel.Finding("codex", "P2", "app/sync.py", 12, title,
                               "detail", needs_rereview=True)],
                None, 900, ["the migration, which the diff omits"],
                code_blind=True)
        # claude answered in the old bare-array shape: it declared NOTHING, which
        # is None all the way to the column — not [], which would say it was asked
        # and had no gap. It DID read the code, which is what makes its declaring
        # nothing a fact about the round.
        return panel.ReviewerRun([], None, 800, None, code_blind=False)

    def fake_adjudicate(clusters, diff, model, pr, budget=None, coverage=None, ci="", **_kw):  # **_kw: code_tree/budget_usd since #113
        """The judge confirms what it was shown, keeping every reporter's own
        report on the record — which is where the per-reviewer declarations the
        board scores live."""
        reports = [f for grp in clusters for f in grp]
        return ([panel.Canonical(id=panel._finding_id(pr, 1), severity="P2",
                                 file="app/sync.py", line=12, synthesis=title,
                                 verdict="confirmed", detail="detail",
                                 reported_by=reports, rationale="real")],
                None,
                panel.CoverageRuling("codex is right that the migration is unread"))

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: PANEL_CFG)
    # Patched through `panel` rather than on a separately-imported panel_core:
    # `run()` calls `panel_core.sh(...)`, and this guarantees the object being
    # patched is the one it resolves. A second import of the same module name is
    # normally the same object — but "normally" is how the doubles in this split
    # went silently inert once, and a stub that does not apply here spawns a real
    # `gh` against a repo that does not exist (#129).
    monkeypatch.setattr(panel.panel_core, "sh", _fake_sh)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", fake_adjudicate)
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("e2e", 77, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=2) == 0
    return str(out), json.loads(out.read_text())


async def test_a_real_panel_payload_records_and_reads_back(client, monkeypatch, tmp_path):
    """The board takes `panel.py --json` as-is, so the two can only stay in step if
    something exercises the actual payload. A renamed field otherwise fails
    silently into a NULL column — the run records, nothing errors, and the column
    the whole release exists for is empty."""
    r1_path, r1 = _panel_round(monkeypatch, tmp_path, 1, "half-stale node")
    # A round HAPPENED, asserted before anything about its contents. A pre-flight
    # refusal (#138) produces a payload too, with `reviewed: false` and every
    # finding-shaped key empty — so every assertion below would fail one by one,
    # naming the field it read rather than the reason there was nothing in it. That
    # is how this test spent a release reporting "new_findings 0" while the actual
    # answer was "the panel refused the round and said so at length" (#239).
    assert r1["reviewed"] is True, r1["skip_reason"]
    assert r1["preflight"]["verdict"] == "run", r1["preflight"]["reason"]
    assert r1["round"] == 1 and r1["new_findings"] == 1
    assert r1["round_stop"]["stop"] is False
    # codex's budget (40 chars) cut a longer diff: measured, not declared.
    assert r1["reviewers"]["codex"]["truncated"] is True
    assert r1["reviewers"]["codex"]["could_not_assess"] == \
        ["the migration, which the diff omits"]
    assert r1["reviewers"]["claude"]["could_not_assess"] is None
    assert r1["to_fix"][0]["needs_rereview"] is True
    assert r1["to_fix"][0]["rereview_by"] == ["codex"]

    posted = await client.post("/review", json=r1, headers=AGENT)
    assert posted.status_code == 201, posted.text
    run = await detail(client, posted.json()["id"])
    assert run["round"] == 1 and run["new_findings"] == 1
    assert run["coverage_note"] == "codex is right that the migration is unread"
    assert card(run, "codex")["could_not_assess"] == ["the migration, which the diff omits"]
    assert card(run, "claude")["could_not_assess"] is None
    assert card(run, "codex")["rereview_flagged"] == 1
    assert run["findings"][0]["new_this_round"] is True

    # #113's columns, asserted on the way BACK OUT of the database — which is the
    # half that was missing. The panel has sent `absent` since v2.32 and ingest
    # dropped it, because `ReviewerIn` inherits pydantic's `extra="ignore"`; that
    # is the same silent drop this file's v2.26 note records for `head_sha` and
    # `unread_files` (#93). A test that only checked the payload would have passed
    # throughout, which is precisely how four fields went missing last time.
    assert card(run, "codex")["code_blind"] is True
    assert card(run, "claude")["code_blind"] is False, \
        "a seat that read the code must not read back as blind"
    # The round-level setting is kept apart from the per-seat answer on purpose: a
    # round with the setting ON and every seat blind is a configuration doing
    # nothing, and only the difference between the two shows it.
    assert run["code_access"] is True

    # Round 2 raises the SAME finding again, against round 1 as its baseline.
    _, r2 = _panel_round(monkeypatch, tmp_path, 2, "half-stale node", baseline=[r1_path])
    assert r2["new_findings"] == 0
    assert r2["to_fix"][0]["new_this_round"] is False
    # ...but a P2 is still confirmed, so the cycle is not done — and at the cap
    # that is recorded as running out, not as convergence.
    assert r2["round_stop"]["stop"] is True and r2["round_stop"]["confident"] is False
    assert "round cap (2)" in r2["round_stop"]["reason"]

    # Both rounds belong to one cycle, inherited from round 1's payload — so the
    # board can join them without guessing from adjacency.
    assert r2["cycle"] == r1["cycle"]

    assert (await client.post("/review", json=r2, headers=AGENT)).status_code == 201
    h = (await client.get("/review/findings?repo=acme/e2e&pr=77", headers=AGENT)).json()
    assert h["rounds"] == 2
    assert {r["cycle"] for r in h["runs"]} == {r1["cycle"]}
    assert h["stopped"] is True and h["stop_reason"].startswith("round cap (2)")
    # The panel's veto list survives to the board, which is the only place a
    # reader can find out WHY the stop was not convergence.
    assert h["stop_veto"] and any("still outstanding" in v for v in h["stop_veto"])
    # One defect, two observations — not two defects.
    assert len(h["findings"]) == 1 and h["findings"][0]["runs_seen"] == 2
    assert h["stop_confident"] is False
