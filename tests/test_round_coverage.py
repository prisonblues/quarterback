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
    "reviewers": {"claude": {"enabled": True, "model": "sonnet"},
                  "codex": {"enabled": True, "model": "gpt-5.6", "max_diff_chars": 40}},
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
    def fake_review(name, model, prompt, effort=""):
        if name == "codex":
            return panel.ReviewerRun(
                [panel.Finding("codex", "P2", "app/sync.py", 12, title,
                               "detail", needs_rereview=True)],
                None, 900, ["the migration, which the diff omits"])
        # claude answered in the old bare-array shape: it declared NOTHING, which
        # is None all the way to the column — not [], which would say it was asked
        # and had no gap.
        return panel.ReviewerRun([], None, 800, None)

    def fake_adjudicate(clusters, diff, model, pr, budget=None, coverage=None, ci=""):
        """The judge confirms what it was shown, keeping every reporter's own
        report on the record — which is where the per-reviewer declarations the
        board scores live."""
        reports = [f for grp in clusters for f in grp]
        return ([panel.Canonical(id=panel._finding_id(pr, 1), severity="P2",
                                 file="app/sync.py", line=12, synthesis=title,
                                 verdict="confirmed", detail="detail",
                                 reported_by=reports, rationale="real")],
                None, "codex is right that the migration is unread")

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
