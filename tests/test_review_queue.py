"""#273: the review queue, derived from state rather than accumulated from events.

The failure this pins is not that a panel run is wrong — the panel works. It is
that nothing starts one, and no reader anywhere reports the depth or the age of
what is waiting. On 2026-08-20 six of eight open PRs had never been panelled and
the newest round on the board was two and a half days old; the "two and a half
days" had to be reconstructed by hand from timestamps, because nothing recorded
it.

The properties under test are the ones that separate this from #54's arrival
watcher and from #227's landing queue:

* **A backlog with no arrival event still comes back in full.** Nothing is
  enqueued and no event is observed, so a queue asked for the first time today
  answers about PRs opened last week. A watcher that starts empty is what #273
  exists to refuse.
* **"Panelled once" is not terminal.** A PR carrying 37 confirmed findings at its
  current head is `unresolved`, not `ready`.
* **Precedence is the correctness.** A PR that is both CONFLICTING and reviewed
  at a head it has moved past comes back `blocked`, not `stale`, so no round is
  spent on a branch that will not merge (#271) — while `review_state` still says
  what review alone thinks of it, so a blocked PR that has never been panelled is
  legible as such.
* **The drainer cannot exempt anything.** Only an open plan item carrying the
  marker takes a PR out of the line, silence is not exemption, and the exemption
  comes back visible and ageing rather than hidden.
* **Idle and broken do not look alike** (#244). Every entry carries every hold,
  and a queue with nothing drainable says why in a sentence.
* **It writes nothing.** It is the thing a drainer would consult, and a reader
  that could exempt or claim its own awkward entries has no depth worth reading.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.api.reviews import _derive_key
from app.db import async_session
from app.models.merge_queue import MergeQueueEntry
from app.models.plan_item import PlanItem
from app.models.resource_lease import ResourceLease
from app.review_queue import (
    Exemption,
    Held,
    Landing,
    LastRun,
    NeedsHuman,
    PullRequest,
    classify,
    drainable,
    exempting,
    idle_reason,
    same_commit,
)

from .conftest import LAPTOP, PINNED_SETTINGS, SERVER

REPO = "acme/drainrepo"
AGENT = {**LAPTOP, "X-Agent-Instance": "d27327"}
#: A person, as the edge proves one. Since #335 an exemption is a human write,
#: so the suite that reads exemptions has to be able to make one.
HUMAN = {"Remote-User": "rich", "X-Edge-Auth": PINNED_SETTINGS["HUMAN_EDGE_SECRET"]}
OTHER = {**SERVER, "X-Agent-Instance": "d27328"}

SHA_A = "a" * 40
SHA_B = "b" * 40

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
OPENED = NOW - timedelta(days=5)
RAN = NOW - timedelta(days=2, hours=12)


def pr(**over) -> PullRequest:
    base = {"number": 1, "head": SHA_A, "mergeable": "MERGEABLE", "opened": OPENED,
            "title": "a pull request"}
    return PullRequest(**{**base, **over})


def run(**over) -> LastRun:
    base = {"run_id": 1, "ts": RAN, "round": 1, "head_sha": SHA_A, "stopped": False,
            "stop_reason": "3 finding(s) no earlier round raised",
            "stop_confident": False, "stop_veto": None, "pr_state": "OPEN",
            "ci_status": None, "confirmed": 3, "outstanding": 3, "cleared": 0}
    return LastRun(**{**base, **over})


def holds(verdict) -> set[str]:
    return {h["code"] for h in verdict.holds}


# ---------------------------------------------------------------------------
# the derivation, with no database anywhere near it
# ---------------------------------------------------------------------------

def test_a_pr_with_no_round_is_unreviewed_and_aged_from_its_opening():
    """The six PRs of the issue's table: no arrival was observed for any of them."""
    v = classify(pr(), run=None, now=NOW)
    assert (v.state, v.action) == ("unreviewed", "review")
    assert v.since == OPENED and v.since_basis == "pr_opened"
    assert v.age_is_upper_bound is False
    assert drainable(v) is True


def test_a_conflicting_branch_is_blocked_rather_than_reviewed():
    """#271: a round on a branch that will not merge is a round bought twice."""
    v = classify(pr(mergeable="CONFLICTING"), run=None, now=NOW)
    assert (v.state, v.action) == ("blocked", "integrate")
    assert holds(v) == {"conflicting"}
    assert drainable(v) is False


def test_a_blocked_pr_still_says_what_review_thinks_of_it():
    """`state` decides what may be done; `review_state` keeps what review knows.

    #190 was CONFLICTING *and* reviewed at a head it had moved past. It must come
    back `blocked` so no round is spent on it — and it must still be possible to
    read that a round is owed once the conflict clears, which is what a single
    collapsed state column throws away.
    """
    v = classify(pr(mergeable="CONFLICTING", head=SHA_B), run=run(head_sha=SHA_A),
                 now=NOW)
    assert v.state == "blocked"
    assert (v.review_state, v.review_action) == ("stale", "re-review")
    assert "once it merges clean it is stale" in v.reason

    never = classify(pr(mergeable="CONFLICTING"), run=None, now=NOW)
    assert never.state == "blocked" and never.review_state == "unreviewed"


def test_a_moved_head_is_stale_and_its_age_is_an_upper_bound():
    v = classify(pr(head=SHA_B), run=run(head_sha=SHA_A), now=NOW)
    assert (v.state, v.action) == ("stale", "re-review")
    assert v.since == RAN and v.since_basis == "last_run"
    # Nothing records WHEN the head moved; it can only have been at or after the
    # round, so the wait reported is the longest it could have been.
    assert v.age_is_upper_bound is True


def test_a_round_that_never_said_which_commit_it_read_cannot_be_shown_current():
    v = classify(pr(), run=run(head_sha=None), now=NOW)
    assert v.state == "stale"
    assert "did not record which commit" in v.reason


def test_outstanding_findings_at_the_current_head_buy_a_fix_pass_not_a_round():
    """#188: 37 confirmed findings, reviewed at its current head, going nowhere."""
    v = classify(pr(), run=run(confirmed=37, outstanding=37, stopped=True,
                               stop_reason="round cap (2) reached"), now=NOW)
    assert (v.state, v.action) == ("unresolved", "fix")
    assert "37 confirmed finding(s) outstanding" in v.reason


def test_a_cycle_that_did_not_converge_buys_another_round():
    v = classify(pr(), run=run(confirmed=0, outstanding=0, stopped=False,
                               stop_reason="1 finding(s) no earlier round raised"),
                 now=NOW)
    assert (v.state, v.action) == ("unconverged", "review")
    assert "did not converge" in v.reason


def test_a_round_that_never_said_whether_it_converged_is_not_read_as_converged():
    """`stopped is None` is "the panel did not say", which is neither True nor
    False — and reading it as True lands a PR on a round that made no claim."""
    v = classify(pr(), run=run(confirmed=0, outstanding=0, stopped=None,
                               stop_reason=None), now=NOW)
    assert v.state == "unconverged"
    assert "never said whether it converged" in v.reason


def test_a_converged_clean_round_leaves_this_queue_for_the_landing_one():
    v = classify(pr(), run=run(confirmed=2, outstanding=0, cleared=2, stopped=True,
                               stop_confident=True, stop_reason="dry"), now=NOW)
    assert (v.state, v.action) == ("ready", "land")
    # Not drainable HERE. Landing is preland's verdict plus the merge claim.
    assert drainable(v) is False


def test_an_unearned_clean_stop_is_not_convergence_and_buys_another_round():
    """`stop_confident` exists so a clean verdict can be told from an EARNED one.

    A reviewer truncated out of half the diff raises nothing, and a counter
    reading zero then says nothing about the code. Reading that as convergence
    hands the landing queue a round which had already said it was not evidence —
    and `StopIn.confident` defaults to False precisely so a payload that never
    said cannot buy a landing.
    """
    v = classify(pr(), run=run(confirmed=0, outstanding=0, stopped=True,
                               stop_confident=False, stop_reason="dry",
                               stop_veto=["codex saw 60,000 of 118,402 diff chars"]),
                 now=NOW)
    assert (v.state, v.action) == ("unconverged", "review")
    assert "60,000 of 118,402" in v.reason


def test_a_round_that_never_recorded_its_confidence_does_not_buy_a_landing():
    """NULL is "nobody said", not a claim of confidence — and `StopIn.confident`
    defaults to False so a payload written through the API cannot say nothing and
    be read generously. A row written around it must not do better."""
    v = classify(pr(), run=run(confirmed=0, outstanding=0, stopped=True,
                               stop_confident=None, stop_reason="dry"), now=NOW)
    assert v.state == "unconverged"
    assert "did not record whether the stop was earned" in v.reason


def test_a_landing_entry_about_a_commit_the_pr_has_left_has_expired():
    """An entry is a claim about a COMMIT, and `ready_sha` exists so a readiness
    cannot outlive the thing it was asserted about. Matching on the PR number
    alone hands #227 a PR three pushes past the one it was told about."""
    entered = NOW - timedelta(hours=3)
    stale = Landing(verdict="ready", ready=False, position=1, entered=entered,
                    head_sha=SHA_B, at_head=False)
    v = classify(pr(head=SHA_A), run=run(confirmed=0, outstanding=0, stopped=True,
                                         stop_confident=True),
                 landing=stale, now=NOW)
    assert v.state == "ready"
    # Aged from the ROUND, not from a place in a line taken at another commit.
    assert v.since_basis == "last_run" and v.since == RAN


def test_a_ready_pr_in_the_landing_queue_is_aged_from_its_place_in_it():
    entered = NOW - timedelta(hours=3)
    v = classify(pr(), run=run(confirmed=0, outstanding=0, stopped=True,
                               stop_confident=True),
                 landing=Landing(verdict="ready", ready=True, position=2,
                                 entered=entered), now=NOW)
    assert v.state == "ready" and v.since_basis == "queue_entered"
    assert v.since == entered
    assert "position 2" in v.reason and "at the head of the landing queue" in v.reason

    # A PR in the line but not at its head is still #227's, and the reason says
    # which of the two it is rather than reading alike.
    queued = classify(pr(), run=run(confirmed=0, outstanding=0, stopped=True,
                                    stop_confident=True),
                      landing=Landing(verdict="queued", ready=False, position=3,
                                      entered=entered, head_sha=SHA_A, at_head=True),
                      now=NOW)
    assert queued.state == "ready"
    assert "waiting its turn" in queued.reason and "verdict queued" in queued.reason


def test_the_plan_outranks_every_state_the_queue_can_derive():
    """The exemption authority is deliberately somewhere the drainer cannot reach."""
    ex = Exemption(item_id="item-1", title="parked pending #300", note="review: exempt",
                   added_by="zeus/agent", updated=NOW - timedelta(days=14), rank=3)
    v = classify(pr(mergeable="CONFLICTING"), run=None, exemption=ex,
                 needs_human=NeedsHuman(waiting=2, since=NOW - timedelta(days=1)),
                 now=NOW)
    assert (v.state, v.action) == ("exempt", "none")
    assert v.since_basis == "plan_item_updated"
    assert holds(v) == {"exempt"}
    # An exemption nobody has revisited in a fortnight should LOOK a fortnight old.
    assert v.since == NOW - timedelta(days=14)


def test_an_escalation_outranks_a_conflict_because_a_human_is_owed_an_answer():
    flagged = NOW - timedelta(days=3)
    v = classify(pr(mergeable="CONFLICTING"), run=None,
                 needs_human=NeedsHuman(waiting=2, since=flagged), now=NOW)
    assert (v.state, v.action) == ("escalated", "answer")
    assert holds(v) == {"escalated"}
    # The age of the QUESTION, not of the round that last restated it (#279).
    assert v.since == flagged and v.since_basis == "needs_human_first_flagged"
    # The classes and reasons are not paraphrased here; the entry points at their
    # one authority instead.
    assert "review/needs-human" in v.reason


def test_a_caller_may_add_an_escalation_the_board_has_no_record_of():
    """Additive only. An agent whose own round raised a flag must not be able to
    decide the flag is not there, so the caller's field can add and never remove."""
    v = classify(pr(escalated=True), run=None, now=NOW)
    assert v.state == "escalated"
    assert "the board has no record of" in v.reason
    # And the board's own record wins the reason when it has one.
    both = classify(pr(escalated=True), run=None,
                    needs_human=NeedsHuman(waiting=1, since=NOW), now=NOW)
    assert "1 defect(s) waiting on a human" in both.reason


def test_every_reason_it_cannot_be_acted_on_is_reported_not_just_the_first():
    """#244: a reader that saw one hold would act the moment that hold cleared."""
    v = classify(
        pr(mergeable="UNKNOWN", draft=True, head=None),
        run=None,
        held=Held(holder="zeus/otter", session="s1", note="fixing round 2",
                  expires=NOW + timedelta(hours=1)),
        now=NOW,
    )
    assert holds(v) == {"mergeable-unknown", "draft", "claimed", "no-head"}
    assert v.state == "unreviewed"  # the holds do not rewrite what it is waiting for
    assert drainable(v) is False


def test_the_round_cap_is_the_callers_number_and_only_bites_on_a_round():
    capped = classify(pr(), run=run(confirmed=0, outstanding=0, stopped=False),
                      rounds=5, max_rounds=5, now=NOW)
    assert "round-cap" in holds(capped)
    # A fix pass is not a round, so the cap does not stand in its way.
    fixing = classify(pr(), run=run(), rounds=5, max_rounds=5, now=NOW)
    assert holds(fixing) == set()
    # Nor is a re-review: the head moved, so the next round opens a NEW cycle at
    # round 1, and a cap counted across cycles would refuse every one of them.
    rereview = classify(pr(head=SHA_B), run=run(head_sha=SHA_A), rounds=9,
                        max_rounds=2, now=NOW)
    assert rereview.state == "stale" and holds(rereview) == set()
    # No cap sent, no cap applied — the board does not know what a dial means.
    assert holds(classify(pr(), run=run(confirmed=0, outstanding=0, stopped=False),
                          rounds=99, now=NOW)) == set()


@pytest.mark.parametrize("note,expected", [
    ("review: exempt", True),
    ("review:exempt", True),
    ("Review : Exempt — parked behind #300", True),
    (None, False),
    ("", False),
    ("needs a review", False),
    ("waiting on a review exemption decision", False),
    ("pre-review: exemptions are for later", False),
])
def test_only_a_deliberate_marker_exempts_a_pr(note, expected):
    """Silence is not exemption, and neither is prose that happens to mention it."""
    assert exempting(note) is expected


@pytest.mark.parametrize("a,b,expected", [
    (SHA_A, SHA_A, True),
    (SHA_A, SHA_B, False),
    (SHA_A, SHA_A[:12], True),
    (SHA_A, SHA_B[:12], False),
    (None, SHA_A, None),
    (SHA_A, None, None),
    (SHA_A, "abc", None),      # too short to be evidence of anything
    ("A" * 40, SHA_A, True),   # case is not a difference between commits
])
def test_two_commits_are_compared_or_declared_incomparable_never_guessed(a, b, expected):
    assert same_commit(a, b) is expected


def test_an_idle_queue_says_why_it_is_idle():
    assert idle_reason([]) == "no open pull requests were supplied for this repo"
    assert idle_reason([{"drainable": True, "holds": [], "state": "unreviewed"}]) is None
    said = idle_reason([
        {"drainable": False, "holds": [{"code": "conflicting", "detail": ""}],
         "state": "blocked"},
        {"drainable": False, "holds": [{"code": "conflicting", "detail": ""},
                                       {"code": "claimed", "detail": ""}],
         "state": "blocked"},
        {"drainable": False, "holds": [], "state": "ready"},
    ])
    assert said is not None
    assert "all 3 open PR(s) are held" in said
    assert "2 conflicting" in said and "1 claimed" in said and "1 ready" in said


# ---------------------------------------------------------------------------
# the endpoint
# ---------------------------------------------------------------------------

FINDING = {"severity": "P2", "file": "app/sync.py",
           "title": "half-stale node after the early return",
           "reviewers": ["claude"], "reason": "real"}


def review_payload(pr_number: int, findings: int = 3, **over) -> dict:
    to_fix = [{**FINDING, "title": f"{FINDING['title']} {i}"} for i in range(findings)]
    body = {
        "repo": REPO,
        "pr": pr_number,
        "judged": True,
        "judge_model": "opus",
        "head_sha": SHA_A,
        "reviewers_selected": ["claude"],
        "reviewers": {"claude": {"model": "opus", "ran": True}},
        "round": 1,
        "cycle": f"cyc-{pr_number}",
        "new_findings": findings,
        "round_stop": {"stop": False, "reason": f"{findings} finding(s) no earlier round raised"},
        "to_fix": to_fix,
        "dismissed": [],
        "sonar_findings": [],
    }
    return {**body, **over}


async def record(client, pr_number: int, findings: int = 3, **over) -> int:
    r = await client.post("/review", json=review_payload(pr_number, findings, **over),
                          headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def ask(client, prs: list[dict], *, repo: str = REPO, headers=AGENT,
              **over) -> dict:
    r = await client.post("/review-queue", json={"repo": repo, "prs": prs, **over},
                          headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def entry(queue: dict, number: int) -> dict:
    return next(e for e in queue["entries"] if e["pr"] == number)


def snapshot(number: int, **over) -> dict:
    base = {"number": number, "headRefOid": SHA_A, "mergeable": "MERGEABLE",
            "createdAt": OPENED.isoformat(), "title": f"pr {number}", "isDraft": False}
    return {**base, **over}


async def test_a_backlog_comes_back_in_full_with_no_event_ever_observed(client):
    """The whole argument against #54: nothing enqueued these, and they are here.

    No panel has run, nothing has been posted about them, and the board learns
    they exist only from the snapshot in this very call — which is what "derived
    from state" buys. An arrival watcher started today would report zero.
    """
    q = await ask(client, [snapshot(n) for n in (7301, 7302, 7303)])
    assert q["counts"]["open"] == 3
    assert q["counts"]["drainable"] == 3
    assert q["counts"]["by_state"]["unreviewed"] == 3
    assert all(e["next_action"] == "review" and e["rounds"] == 0
               for e in q["entries"])
    assert q["idle_reason"] is None
    assert q["oldest"]["pr"] == 7301 and q["oldest"]["age_seconds"] > 0


async def test_the_issues_own_table_reproduces_end_to_end(client):
    """One queue, four verbs, from real rows: blocked / stale / unresolved / first."""
    await record(client, 7310, findings=37, head_sha=SHA_A, round=2,
                 round_stop={"stop": True, "reason": "round cap (2) reached"})
    await record(client, 7311, findings=43, head_sha=SHA_A, round=2,
                 round_stop={"stop": True, "reason": "round cap (2) reached"})

    q = await ask(client, [
        snapshot(7310),                                       # reviewed at its head
        snapshot(7311, headRefOid=SHA_B, mergeable="CONFLICTING"),  # moved AND conflicting
        snapshot(7312),                                       # never panelled
    ])
    unresolved = entry(q, 7310)
    assert unresolved["state"] == "unresolved" and unresolved["next_action"] == "fix"
    assert unresolved["last_run"]["confirmed"] == 37
    assert unresolved["last_run"]["outstanding"] == 37
    assert unresolved["since_basis"] == "last_run"

    blocked = entry(q, 7311)
    assert blocked["state"] == "blocked" and blocked["next_action"] == "integrate"
    assert blocked["review_state"] == "stale"      # not lost, just outranked
    assert blocked["drainable"] is False

    first = entry(q, 7312)
    assert first["state"] == "unreviewed" and first["rounds"] == 0

    assert q["counts"]["drainable"] == 2           # the conflicting one is not
    assert q["counts"]["by_next_action"]["integrate"] == 1


async def test_rounds_are_counted_and_the_newest_is_the_one_that_speaks(client):
    await record(client, 7320, findings=2, head_sha=SHA_B, round=1)
    await record(client, 7320, findings=1, head_sha=SHA_A, round=2,
                 round_stop={"stop": True, "reason": "dry", "confident": True})
    q = await ask(client, [snapshot(7320)])
    e = entry(q, 7320)
    assert e["rounds"] == 2
    assert e["last_run"]["round"] == 2 and e["last_run"]["head_sha"] == SHA_A
    assert e["state"] == "unresolved"


async def test_recorded_outcomes_clear_findings_and_move_the_pr_off_unresolved(client):
    """Nothing was counting the wait, and nothing was counting the clearing either."""
    await record(client, 7330, findings=2, head_sha=SHA_A, round=2,
                 round_stop={"stop": True, "reason": "round cap (2) reached",
                             "confident": True})
    before = entry(await ask(client, [snapshot(7330)]), 7330)
    assert before["state"] == "unresolved" and before["last_run"]["outstanding"] == 2

    keys = [_derive_key(FINDING["file"], f"{FINDING['title']} {i}") for i in range(2)]
    r = await client.post("/review/outcomes", headers=AGENT, json={
        "repo": REPO, "pr": 7330,
        "outcomes": [{"key": k, "outcome": "fixed"} for k in keys]})
    assert r.status_code in (200, 201), r.text

    after = entry(await ask(client, [snapshot(7330)]), 7330)
    assert after["last_run"]["confirmed"] == 2      # the round still said what it said
    assert after["last_run"]["cleared"] == 2
    assert after["last_run"]["outstanding"] == 0
    assert after["state"] == "ready"                # stopped, and nothing outstanding


async def test_a_narrowed_finding_clears_the_queue_the_way_a_fixed_one_does(client):
    """#615's fifth outcome, and the half of it the queue decides.

    `narrowed` says the finding is real and this pass repaired it at the point it
    was raised, with the general form left for another change. That is a FIX, so it
    clears for the plainest reason of the five: the code changed and the finding as
    raised is answered. Left out of the clearing set the queue would hold a PR open
    on a defect somebody has already repaired — which is exactly the pressure the
    word exists to remove, since a fixer that cannot say "fixed here, not
    everywhere" fixes the class instead, and the class-wide fix is what edits files
    nobody reviewed.
    """
    await record(client, 7331, findings=2, head_sha=SHA_A, round=2,
                 round_stop={"stop": True, "reason": "round cap (2) reached",
                             "confident": True})
    before = entry(await ask(client, [snapshot(7331)]), 7331)
    assert before["state"] == "unresolved" and before["last_run"]["outstanding"] == 2

    keys = [_derive_key(FINDING["file"], f"{FINDING['title']} {i}") for i in range(2)]
    r = await client.post("/review/outcomes", headers=AGENT, json={
        "repo": REPO, "pr": 7331,
        "outcomes": [{"key": k, "outcome": "narrowed",
                      "note": "the general form is a rule over every caller"}
                     for k in keys]})
    assert r.status_code in (200, 201), r.text
    assert r.json()["recorded"] == keys

    after = entry(await ask(client, [snapshot(7331)]), 7331)
    assert after["last_run"]["confirmed"] == 2      # the round still said what it said
    assert after["last_run"]["cleared"] == 2
    assert after["last_run"]["outstanding"] == 0
    assert after["state"] == "ready"


async def test_an_exempting_plan_item_takes_a_pr_out_of_the_line_and_says_which(client):
    """Written as an AGENT until #335, and that was the hole this suite could not see.

    The queue was careful that its reader never writes an exemption, and the
    marker was then left on `POST /plan/item`, which every agent may call — so
    the worker held the authority the drainer had been denied. The refusal is
    tested in `test_review_exemption.py`; what this one now pins is the other
    half, that a marker a PERSON wrote still takes the PR out of the line
    exactly as before. Nothing about reading an exemption changed.
    """
    r = await client.post("/plan/item", headers=AGENT, json={
        "title": "PR 7340 is parked", "repo": REPO, "ref_kind": "pr",
        "ref_value": "7340"})
    assert r.status_code == 200, r.text
    item_id = r.json()["item_id"]
    r = await client.post("/plan/item/update", headers=HUMAN, json={
        "item_id": item_id,
        "note": "review: exempt — waiting on the upstream release, do not spend rounds"})
    assert r.status_code == 200, r.text

    q = await ask(client, [snapshot(7340), snapshot(7341)])
    ex = entry(q, 7340)
    assert ex["state"] == "exempt" and ex["next_action"] == "none"
    assert ex["exemption"]["item_id"] == item_id
    assert "upstream release" in ex["exemption"]["note"]
    assert ex["exemption"]["added_by"]          # who exempted it is visible
    assert ex["exemption"]["stale_seconds"] >= 0
    assert ex["drainable"] is False

    # Silence is not exemption: a PR the plan says nothing about is drainable.
    assert entry(q, 7341)["plan_item"] is None
    assert entry(q, 7341)["drainable"] is True


async def test_a_plan_item_without_the_marker_is_reported_but_does_not_exempt(client):
    r = await client.post("/plan/item", headers=AGENT, json={
        "title": "PR 7350 needs a second opinion", "repo": REPO, "ref_kind": "pr",
        "ref_value": "7350", "note": "worth a review before we decide"})
    assert r.status_code == 200, r.text

    e = entry(await ask(client, [snapshot(7350)]), 7350)
    assert e["state"] == "unreviewed" and e["drainable"] is True
    assert e["plan_item"] is not None and e["plan_item"]["exempts"] is False
    assert e["exemption"] is None


async def test_a_pr_somebody_already_holds_is_reported_held_not_offered(client):
    r = await client.post("/claim", headers=OTHER, json={
        "ref": {"kind": "pr", "repo": REPO, "value": "7360"},
        "note": "running round 2"})
    assert r.status_code in (200, 201), r.text

    e = entry(await ask(client, [snapshot(7360)]), 7360)
    assert e["drainable"] is False
    assert [h["code"] for h in e["holds"]] == ["claimed"]
    assert e["claim"]["holder"]
    assert "running round 2" in e["holds"][0]["detail"]


async def test_a_pr_in_the_landing_queue_is_handed_to_it_by_name(client):
    await record(client, 7370, findings=0, head_sha=SHA_A, round=2, new_findings=0,
                 round_stop={"stop": True, "reason": "dry", "confident": True})
    r = await client.post("/merge-queue/enqueue", headers=AGENT, json={
        "repo": REPO, "base": "main", "pr": 7370, "head": SHA_A, "verdict": "ready"})
    assert r.status_code == 200, r.text

    e = entry(await ask(client, [snapshot(7370)]), 7370)
    assert e["state"] == "ready" and e["next_action"] == "land"
    assert e["landing"]["position"] == 1 and e["landing"]["ready"] is True
    assert e["landing"]["at_head"] is True
    assert e["since_basis"] == "queue_entered"
    assert e["drainable"] is False

    # The PR is pushed. The entry still exists and is still first in the line,
    # and it is no longer a statement about this PR.
    moved = entry(await ask(client, [snapshot(7370, headRefOid=SHA_B)]), 7370)
    assert moved["landing"]["at_head"] is False
    assert moved["landing"]["ready"] is False
    assert moved["state"] == "stale", "the round is behind the head too"


async def test_a_draft_is_in_the_queue_and_is_not_offered_for_a_round(client):
    e = entry(await ask(client, [snapshot(7380, isDraft=True)]), 7380)
    assert e["state"] == "unreviewed"
    assert [h["code"] for h in e["holds"]] == ["draft"]
    assert e["drainable"] is False


async def test_a_queue_with_nothing_drainable_says_so_rather_than_going_quiet(client):
    q = await ask(client, [snapshot(7390, mergeable="CONFLICTING"),
                           snapshot(7391, mergeable="CONFLICTING")])
    assert q["counts"]["drainable"] == 0
    assert q["oldest"] is None
    assert q["oldest_held"]["pr"] in (7390, 7391)
    assert "2 conflicting" in q["idle_reason"]
    assert q["counts"]["by_hold"]["conflicting"] == 2


async def test_an_empty_repo_is_an_empty_queue_with_a_reason(client):
    q = await ask(client, [])
    assert q["counts"]["open"] == 0 and q["counts"]["drainable"] == 0
    assert q["idle_reason"] == "no open pull requests were supplied for this repo"
    assert q["oldest"] is None and q["oldest_held"] is None


async def test_a_defect_waiting_on_a_human_escalates_the_pr_by_itself(client):
    """Derived, not taken on trust. Before #279 "a human has to look at this" was
    a judgement formed in four places and recorded in none, so a queue could only
    ask the caller — and an agent whose own round raised the flag would have been
    the one deciding whether to honour it."""
    await record(client, 7400, findings=0, head_sha=SHA_A, to_fix=[{
        **FINDING, "title": "which of these three shapes is right",
        "needs_human": True, "needs_human_class": "decision",
        "needs_human_reason": "no diff answers this; it is a product call"}])

    q = await ask(client, [snapshot(7400)])
    e = entry(q, 7400)
    assert e["state"] == "escalated" and e["next_action"] == "answer"
    assert e["needs_human"]["waiting"] == 1
    assert e["needs_human"]["detail"].endswith("pr=7400")
    assert REPO in e["needs_human"]["detail"]
    assert e["drainable"] is False
    assert [h["code"] for h in e["holds"]] == ["escalated"]
    assert e["since_basis"] == "needs_human_first_flagged"
    assert q["counts"]["waiting_on_a_human"] == 1


async def test_an_outcome_recorded_against_it_retires_the_escalation(client):
    """#279's own rule, and nothing subtler: any outcome retires it, `deferred`
    included — that is somebody having ACTED, not the human having answered."""
    await record(client, 7402, findings=0, head_sha=SHA_A, to_fix=[{
        **FINDING, "title": "does this look right on a real screen",
        "needs_human": True, "needs_human_class": "ui",
        "needs_human_reason": "a screenshot is the only evidence"}])
    before = entry(await ask(client, [snapshot(7402)]), 7402)
    assert before["state"] == "escalated"

    key = _derive_key(FINDING["file"], "does this look right on a real screen")
    r = await client.post("/review/outcomes", headers=AGENT, json={
        "repo": REPO, "pr": 7402,
        "outcomes": [{"key": key, "outcome": "deferred", "deferred_to": "#400"}]})
    assert r.status_code in (200, 201), r.text

    after = entry(await ask(client, [snapshot(7402)]), 7402)
    assert after["needs_human"] is None
    assert after["state"] != "escalated"


async def test_a_caller_may_still_add_an_escalation_the_board_never_heard_of(client):
    q = await ask(client, [snapshot(7404, escalated=True)])
    e = entry(q, 7404)
    assert e["state"] == "escalated" and e["needs_human"] is None
    assert "the board has no record of" in e["reason"]


async def test_the_callers_round_cap_holds_a_pr_that_has_had_its_rounds(client):
    await record(client, 7410, findings=0, head_sha=SHA_A, round=1, new_findings=0,
                 round_stop={"stop": False, "reason": "a reviewer was truncated"})
    uncapped = entry(await ask(client, [snapshot(7410)]), 7410)
    assert uncapped["state"] == "unconverged" and uncapped["drainable"] is True

    capped = entry(await ask(client, [snapshot(7410)], max_rounds=1), 7410)
    assert capped["drainable"] is False
    assert [h["code"] for h in capped["holds"]] == ["round-cap"]


async def test_githubs_own_z_suffixed_timestamp_is_read_as_a_time(client):
    """`gh` spells UTC with a `Z`, and the age is the number this issue is about."""
    q = await ask(client, [snapshot(7480, createdAt="2026-01-01T00:00:00Z")])
    e = entry(q, 7480)
    assert e["opened"].startswith("2026-01-01T00:00:00")
    assert e["age_seconds"] > 86_400, "a real wait, not a zero from an unparsed field"


async def test_the_repo_is_folded_the_way_every_other_key_is_folded(client):
    """`Acme/DrainRepo` and `acme/drainrepo` are one repository everywhere else."""
    await record(client, 7420, findings=1, head_sha=SHA_A)
    q = await ask(client, [snapshot(7420)], repo="Acme/DrainRepo")
    assert q["repo"] == REPO
    assert entry(q, 7420)["rounds"] == 1


async def test_the_same_pr_twice_is_refused_rather_than_answered_twice(client):
    r = await client.post("/review-queue", headers=AGENT, json={
        "repo": REPO, "prs": [snapshot(7430), snapshot(7430)]})
    assert r.status_code == 422
    assert "7430" in str(r.json()["detail"])


async def test_a_repo_that_is_not_owner_slash_name_is_refused(client):
    r = await client.post("/review-queue", headers=AGENT,
                          json={"repo": "drainrepo", "prs": []})
    assert r.status_code == 422


async def test_reading_the_queue_needs_a_token(client):
    r = await client.post("/review-queue", json={"repo": REPO, "prs": []})
    assert r.status_code == 401


async def test_the_queue_writes_nothing(client):
    """A reader that could exempt or claim its own entries has no depth worth reading."""
    async def counts() -> tuple[int, int, int]:
        async with async_session() as s:
            return (
                await s.scalar(select(func.count()).select_from(PlanItem)),
                await s.scalar(select(func.count()).select_from(ResourceLease)),
                await s.scalar(select(func.count()).select_from(MergeQueueEntry)),
            )

    before = await counts()
    await ask(client, [snapshot(7440), snapshot(7441, mergeable="CONFLICTING")])
    await ask(client, [snapshot(7440)])
    assert await counts() == before


async def test_the_response_publishes_its_vocabulary_and_refuses_to_own_the_order(client):
    q = await ask(client, [snapshot(7450), snapshot(7451)])
    assert [e["pr"] for e in q["entries"]] == [7450, 7451]
    assert "NOT a work order" in q["ordering"]
    assert "unreviewed" in q["vocabulary"]["states"]
    assert "re-review" in q["vocabulary"]["next_actions"]
    assert "conflicting" in q["vocabulary"]["holds"]


async def test_the_rounds_ci_and_pr_state_are_reported_but_decide_nothing(client):
    """#324: a red run followed by an approval-gated one reports as no checks.

    So no state here is derived from CI. The reading is still published, because
    `app.ordering.Candidate` reads it and an orderer should not need a second
    query — but it is published as what it is: what one panel run saw, at a
    commit, at a time.
    """
    await record(client, 7460, findings=1, head_sha=SHA_A, ci_status="FAIL",
                 pr_state="OPEN")
    e = entry(await ask(client, [snapshot(7460)]), 7460)
    assert e["last_run"]["ci_status"] == "FAIL"
    assert e["last_run"]["pr_state"] == "OPEN"
    # Red CI does not make it blocked, drainable-or-not, or anything else: the
    # state is what review alone says.
    assert e["state"] == "unresolved" and e["drainable"] is True


async def test_an_entry_carries_everything_the_ordering_rules_read(client):
    """#232's `order()` takes candidates, not plan rows, so a queue can reuse it.

    That reuse is only free while every field `Candidate` reads is already on an
    entry. This pins the projection, so a rule that grows a new input fails here
    rather than in whoever tries to order this queue.
    """
    from app.ordering import Candidate

    await record(client, 7470, findings=1, head_sha=SHA_A, ci_status="PASS")
    e = entry(await ask(client, [snapshot(7470)]), 7470)
    reads = {f for f in Candidate.__dataclass_fields__
             if f not in {"key", "depends_on", "blocked", "collides_with", "evidence",
                          "idle_days"}}
    available = {
        "pr_state": e["last_run"]["pr_state"],
        "draft": e["draft"],
        "ci": e["last_run"]["ci_status"],
        "outstanding_findings": e["last_run"]["outstanding"],
    }
    assert reads == set(available), f"Candidate grew a field the queue does not carry: {reads}"
    assert available["ci"] == "PASS" and available["outstanding_findings"] == 1
