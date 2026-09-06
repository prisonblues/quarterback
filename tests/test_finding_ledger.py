"""#772: a finding has a life between rounds, and the board records it.

A finding used to exist inside a round and nowhere else. What this file pins is
the part of the repair that is easy to get subtly wrong rather than the happy
path:

* ``unpaid`` and ``deferred`` are different rows with different sources, so "a
  budget ran out before anybody looked" and "somebody looked and parked it" can
  never be counted as one population — the whole reason the vocabulary is not one
  ``skipped`` bucket;
* a promotion keeps the finding's ORIGINAL round, so a deferred finding returning
  in round 5 does not read as fresh damage in round 5's injection rate;
* coming back is allowed and coming back SILENTLY is not: the ordinary panel
  writer cannot move a parked finding to ``raised``, and is told which door to
  use;
* every state that is a refusal costs a reason, at the API and at the database;
* a retry confirms rather than doubles, and a writer that rewrites its own word
  inside one round is named for it.

Each test uses its own repo and PR. The suite shares one database and this
endpoint aggregates per (repo, pr), so a shared slug would make every assertion
here depend on which other tests had run.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.api import reviews
from app.finding_lifecycle import (
    LIFECYCLE_SOURCES,
    LIFECYCLE_STATES,
    OPEN_STATES,
    OUTCOME_STATES,
    PROMOTABLE_STATES,
    REASON_REQUIRED_STATES,
)

from .conftest import LAPTOP

AGENT = {**LAPTOP, "X-Agent-Instance": "772abc"}


def repo_of(case: str) -> str:
    return f"acme/ledger-{case}"


def finding(key: str, **over) -> dict:
    f = {"title": f"finding {key}", "severity": "P2", "file": "app/api/reviews.py",
         "line": 10, "reviewers": ["claude"], "key": key}
    return {**f, **over}


async def record(client, case: str, keys: list[str], *, pr: int = 1, **over) -> dict:
    body = {
        "repo": repo_of(case),
        "pr": pr,
        "judged": True,
        "judge_model": "opus",
        "reviewers_selected": ["claude"],
        "reviewers": {"claude": {"model": "opus", "ran": True}},
        "to_fix": [finding(k) for k in keys],
        "dismissed": [],
        "sonar_findings": [],
    }
    r = await client.post("/review", json={**body, **over}, headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


async def post_ledger(client, case: str, rnd: int, entries: list[dict], *, pr: int = 1,
                      **over):
    return await client.post(
        "/review/ledger",
        json={"repo": repo_of(case), "pr": pr, "round": rnd, "entries": entries, **over},
        headers=AGENT,
    )


async def ledger(client, case: str, rnd: int, entries: list[dict], expect: int | None = None,
                 *, pr: int = 1, **over) -> dict:
    r = await post_ledger(client, case, rnd, entries, pr=pr, **over)
    if expect is not None:
        assert r.status_code == expect, r.text
    else:
        assert r.status_code in (200, 201), r.text
    return r.json()


async def read(client, case: str, *, pr: int = 1, query: str = "") -> dict:
    r = await client.get(f"/review/ledger?repo={repo_of(case)}&pr={pr}{query}",
                         headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


def by_key(body: dict) -> dict[str, dict]:
    return {f["key"]: f for f in body["findings"]}


# ------------------------------------------------------------- the vocabulary

def test_the_ledger_states_extend_the_outcome_vocabulary_rather_than_shadow_it():
    """One vocabulary with three more members, not two vocabularies.

    ``OUTCOMES`` is now DEFINED as the shared tuple rather than copied beside it,
    so a sixth outcome becomes a sixth lifecycle state on the commit that adds it.
    A parallel vocabulary is how a state ends up spelled two ways and counted in
    neither — the drift this board has already paid for over ``provenance``.
    """
    assert reviews.OUTCOMES == OUTCOME_STATES
    assert set(OUTCOME_STATES) < set(LIFECYCLE_STATES)
    assert set(LIFECYCLE_STATES) - set(OUTCOME_STATES) == {"raised", "unpaid", "escalated"}


def test_nobody_looked_and_somebody_looked_are_separate_states():
    """The single distinction the whole feature exists for, asserted on the
    vocabulary itself so it cannot be collapsed by a later tidy-up.

    ``unpaid`` is open work — nobody was paid to look at it, so it is still owed —
    while ``deferred`` is a decision and is not. Both may come back.
    """
    assert "unpaid" in OPEN_STATES and "deferred" not in OPEN_STATES
    assert {"deferred", "unpaid"} == PROMOTABLE_STATES
    # Everything but the two states that ARE the loop working owes a reason.
    assert set(LIFECYCLE_STATES) - REASON_REQUIRED_STATES == {"raised", "fixed"}


async def test_the_vocabulary_is_published_so_a_producer_need_not_keep_a_copy(client):
    r = await client.get("/review/ledger/vocabulary", headers=AGENT)
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["states"] == list(LIFECYCLE_STATES)
    assert v["sources"] == list(LIFECYCLE_SOURCES)
    assert v["promotion_source"] == "promotion"


# ------------------------------------------------------------ record and read

async def test_a_round_records_where_each_finding_stands_and_it_reads_back(client):
    """The whole point in one pass: three findings, three different states, and
    each one's round and reason survive the round trip."""
    await record(client, "basic", ["k1", "k2", "k3"])
    res = await ledger(client, "basic", 1, [
        {"key": "k1", "state": "raised", "source": "panel"},
        {"key": "k2", "state": "deferred", "source": "judge",
         "reason": "below the cleared floor; reported not fixed here"},
        {"key": "k3", "state": "unpaid", "source": "budget",
         "reason": "verification budget exhausted after 8 findings"},
    ], expect=201)
    assert sorted(res["recorded"]) == ["k1", "k2", "k3"]

    got = by_key(await read(client, "basic"))
    assert got["k1"]["state"] == "raised" and got["k1"]["source"] == "panel"
    assert got["k2"]["state"] == "deferred" and got["k2"]["source"] == "judge"
    assert got["k3"]["state"] == "unpaid" and got["k3"]["source"] == "budget"
    # Every row is round-stamped, and on its first appearance the two round
    # columns agree.
    assert all(f["round"] == 1 and f["origin_round"] == 1 for f in got.values())
    assert got["k3"]["reason"].startswith("verification budget")

    body = await read(client, "basic")
    # The three lists a round actually consumes. `unpaid` is open work and
    # `deferred` is not, and they are published apart so a reader can tell "the
    # budget is too small" from "somebody made a call".
    assert sorted(body["open_keys"]) == ["k1", "k3"]
    assert body["deferred_keys"] == ["k2"]
    assert body["unpaid_keys"] == ["k3"]
    assert body["last_round"] == 1


async def test_two_sources_may_speak_about_one_finding_in_one_round(client):
    """A round is not one writer. The panel raises, the judge rules, and both
    words are kept — the newest is the finding's state and the other stays in the
    history rather than being overwritten."""
    await record(client, "twovoices", ["k1"])
    await ledger(client, "twovoices", 1, [{"key": "k1", "state": "raised", "source": "panel"}])
    await ledger(client, "twovoices", 1, [
        {"key": "k1", "state": "refuted", "source": "judge",
         "reason": "install -m 0755 bin/* does glob; the finding is wrong"},
    ])
    got = by_key(await read(client, "twovoices"))["k1"]
    assert got["state"] == "refuted" and got["source"] == "judge"
    assert [(e["source"], e["state"]) for e in got["history"]] == [
        ("panel", "raised"), ("judge", "refuted")]


# --------------------------------------------------------------- the promotion

async def test_a_promoted_finding_keeps_its_original_round(client):
    """The requirement the injection rate turns on.

    A finding deferred in round 2 and promoted in round 5 is five rounds into the
    cycle and TWO rounds old. Read off ``round`` it looks like fresh damage in
    round 5, which is what inflates ``fix_injection`` and stops cycles that were
    converging; ``origin_round`` is what says otherwise, and the board carries it
    forward so no producer can get it wrong.
    """
    await record(client, "promote", ["k1"])
    await ledger(client, "promote", 2, [
        {"key": "k1", "state": "deferred", "source": "judge",
         "reason": "parked: the fix wants a decision about the cache key"},
    ])
    res = await ledger(client, "promote", 5, [
        {"key": "k1", "state": "raised", "source": "promotion",
         "reason": "round 5's diff touches app/cache.py again"},
    ], expect=201)
    assert res["promoted"] == [{"key": "k1", "from": "deferred", "origin_round": 2}]

    got = by_key(await read(client, "promote"))["k1"]
    assert got["state"] == "raised"
    assert got["round"] == 5
    assert got["origin_round"] == 2
    assert got["rounds_open"] == 3
    # The audit trail: why it came back, on the row rather than in a report.
    assert "touches app/cache.py" in got["reason"]
    assert [e["state"] for e in got["history"]] == ["deferred", "raised"]


async def test_an_unpaid_finding_may_be_promoted_too(client):
    """``unpaid`` comes back for the same reason ``deferred`` does: a finding the
    budget skipped once must not be skipped for ever by nobody's decision."""
    await record(client, "promoteunpaid", ["k1"])
    await ledger(client, "promoteunpaid", 1, [
        {"key": "k1", "state": "unpaid", "source": "budget",
         "reason": "over the verification budget"},
    ])
    res = await ledger(client, "promoteunpaid", 2, [
        {"key": "k1", "state": "raised", "source": "promotion",
         "reason": "budget raised for round 2"},
    ])
    assert res["promoted"] == [{"key": "k1", "from": "unpaid", "origin_round": 1}]


async def test_coming_back_silently_is_refused_and_the_caller_is_told_the_door(client):
    """THE failure this table exists to end, refused at the door.

    A deferred finding re-raised by the ordinary panel writer is indistinguishable
    from fresh damage. Coming back is fine — coming back with no reason and no
    trail is not — so the rejection names the source that costs a reason rather
    than simply saying no.
    """
    await record(client, "silent", ["k1"])
    await ledger(client, "silent", 1, [
        {"key": "k1", "state": "deferred", "source": "judge", "reason": "parked"},
    ])
    res = await ledger(client, "silent", 2, [
        {"key": "k1", "state": "raised", "source": "panel"},
    ], expect=422)
    assert res["recorded"] == []
    assert "promotion" in res["rejected"][0]["reason"]
    assert "deferred" in res["rejected"][0]["reason"]


async def test_a_settled_finding_is_not_reopened_by_a_ledger_edit(client):
    """Only a parked finding comes back. A refuted one that reappears is a NEW
    observation and the round that raises it says so — letting a promotion reopen
    it would overturn somebody's recorded decision with none of the evidence the
    outcome table demands for the same move."""
    await record(client, "settled", ["k1"])
    await ledger(client, "settled", 1, [
        {"key": "k1", "state": "refuted", "source": "judge",
         "reason": "the constant is exported; the finding misread the file"},
    ])
    res = await ledger(client, "settled", 2, [
        {"key": "k1", "state": "raised", "source": "promotion", "reason": "it is back"},
    ], expect=422)
    assert "cannot promote from refuted" in res["rejected"][0]["reason"]


async def test_a_promotion_of_a_finding_with_no_ledger_state_is_refused(client):
    await record(client, "nothing", ["k1"])
    res = await ledger(client, "nothing", 1, [
        {"key": "k1", "state": "raised", "source": "promotion", "reason": "back"},
    ], expect=422)
    assert "nothing to promote" in res["rejected"][0]["reason"]


async def test_a_promotion_returns_a_finding_to_raised_and_to_nothing_else(client):
    await record(client, "promotestate", ["k1"])
    await ledger(client, "promotestate", 1, [
        {"key": "k1", "state": "deferred", "source": "judge", "reason": "parked"},
    ])
    res = await ledger(client, "promotestate", 2, [
        {"key": "k1", "state": "fixed", "source": "promotion", "reason": "?"},
    ], expect=422)
    assert "returns a finding to 'raised'" in res["rejected"][0]["reason"]


# ------------------------------------------------------------ the named reason

@pytest.mark.parametrize("state", sorted(REASON_REQUIRED_STATES))
async def test_every_state_that_is_a_refusal_costs_a_reason(client, state):
    """A bare skip is a confident assertion with nothing behind it, and here it
    lands in the number that decides whether a cycle converged."""
    case = f"reason-{state}"
    await record(client, case, ["k1", "k2"])
    entry = {"key": "k1", "state": state, "source": "human"}
    if state == "superseded":
        entry["superseded_by"] = "k2"
    res = await ledger(client, case, 1, [entry], expect=422)
    assert res["recorded"] == []
    assert "reason" in res["rejected"][0]["reason"]


async def test_raised_and_fixed_are_the_two_states_that_owe_nothing(client):
    """The loop working. Requiring a reason for these would tax the ordinary case
    and teach producers to write "fixed it" in a field that exists for arguments."""
    await record(client, "nofuss", ["k1", "k2"])
    res = await ledger(client, "nofuss", 1, [
        {"key": "k1", "state": "raised", "source": "panel"},
        {"key": "k2", "state": "fixed", "source": "fix-pass"},
    ], expect=201)
    assert sorted(res["recorded"]) == ["k1", "k2"]


async def test_the_database_refuses_a_bare_skip_even_when_the_api_is_bypassed(client):
    """The rule at the boundary, not only at ingest — for a backfill, an admin
    script or the next write path. ``btrim`` with an explicit character set,
    because single-argument ``btrim`` strips ordinary spaces only and a reason of
    one tab satisfied it."""
    from app.db import async_session
    from app.models.review import ReviewFindingLedger

    async with async_session() as s:
        s.add(ReviewFindingLedger(
            repo="acme/ck", pr=1, finding_key="k1", state="deferred",
            source="human", reason="\t", round=1, origin_round=1, set_by="laptop/x",
        ))
        with pytest.raises(IntegrityError):
            await s.commit()


# ---------------------------------------------- rejection is itemised, not fatal

async def test_one_bad_entry_costs_its_own_row_and_not_the_batch(client):
    """A round reporting twelve findings must not lose eleven of them to one
    typo. The reason is named back, never merely counted."""
    await record(client, "itemised", ["k1", "k2"])
    res = await ledger(client, "itemised", 1, [
        {"key": "k1", "state": "raised", "source": "panel"},
        {"key": "k2", "state": "parked", "source": "panel"},
        {"key": "nope", "state": "raised", "source": "panel"},
        {"key": "k2", "state": "raised", "source": "wizard"},
    ], expect=201)
    assert res["recorded"] == ["k1"]
    # Three rejections, each naming its own fault — the response is what a caller
    # reads instead of diffing its own payload against the board.
    why = [r["reason"] for r in res["rejected"]]
    assert len(why) == 3
    assert "unknown state" in why[0]
    assert "no finding with this key on this PR" in why[1]
    assert "unknown source" in why[2]


async def test_a_key_the_board_has_no_observation_of_is_refused_with_the_remedy(client):
    """The rule that keeps the board out of the identity business while still
    refusing a state with no evidence under it: the board does not mint keys and
    does not check their shape, only that some run of this PR recorded one."""
    await record(client, "unknownkey", ["k1"])
    res = await ledger(client, "unknownkey", 1, [
        {"key": "invented", "state": "raised", "source": "panel"},
    ], expect=422)
    assert "POST /review for the round before its ledger" in res["rejected"][0]["reason"]


async def test_a_key_may_be_spoken_about_once_per_source_per_request(client):
    await record(client, "dupe", ["k1"])
    res = await ledger(client, "dupe", 1, [
        {"key": "k1", "state": "raised", "source": "panel"},
        {"key": "k1", "state": "fixed", "source": "panel"},
    ], expect=201)
    assert res["recorded"] == ["k1"]
    assert "already appears earlier in this payload" in res["rejected"][0]["reason"]


async def test_superseded_names_a_real_finding_and_never_itself(client):
    await record(client, "supersede", ["k1", "k2"])
    res = await ledger(client, "supersede", 1, [
        {"key": "k1", "state": "superseded", "source": "human", "reason": "merged into k2",
         "superseded_by": "k1"},
        {"key": "k2", "state": "superseded", "source": "human", "reason": "gone",
         "superseded_by": "ghost"},
    ], expect=422)
    reasons = {r["key"]: r["reason"] for r in res["rejected"]}
    assert "names this finding itself" in reasons["k1"]
    assert "names no finding on this PR" in reasons["k2"]


async def test_a_run_id_from_another_pull_request_is_refused_outright(client):
    """The request's field, not an entry's: a ledger row pointing at another PR's
    round is a join that silently answers about the wrong loop."""
    await record(client, "runid", ["k1"])
    other = await record(client, "runid", ["k9"], pr=2)
    r = await post_ledger(client, "runid", 1,
                          [{"key": "k1", "state": "raised", "source": "panel"}],
                          run_id=other["id"])
    assert r.status_code == 422, r.text
    assert "is not a run of" in r.json()["detail"]


# ------------------------------------------------------------- repeats and edits

async def test_a_retry_confirms_rather_than_doubles(client):
    """The commonest race here is one agent's own timed-out retry, not two
    agents. Under an append-only design it would double the round's ledger and
    every count over it."""
    await record(client, "retry", ["k1"])
    entry = {"key": "k1", "state": "deferred", "source": "judge", "reason": "parked"}
    first = await ledger(client, "retry", 1, [entry], expect=201)
    again = await ledger(client, "retry", 1, [entry], expect=200)
    assert first["recorded"] == ["k1"]
    assert again["recorded"] == [] and again["unchanged"] == ["k1"]

    got = by_key(await read(client, "retry"))["k1"]
    assert len(got["history"]) == 1
    # A no-op steals no authorship and moves no timestamp: "when was this last
    # decided" must not answer "just now" for a decision taken last week.
    assert got["revisions"] == 0 and got["updated_at"] is None


async def test_a_writer_that_changes_its_own_word_inside_a_round_is_named_for_it(client):
    """A state that moves is legitimate; a state that moves silently is not."""
    await record(client, "moved", ["k1"])
    await ledger(client, "moved", 1, [{"key": "k1", "state": "raised", "source": "panel"}])
    res = await ledger(client, "moved", 1, [
        {"key": "k1", "state": "deferred", "source": "panel", "reason": "under the floor"},
    ], expect=200)
    assert res["changed"] == [{"key": "k1", "source": "panel",
                               "from": "raised", "to": "deferred"}]
    got = by_key(await read(client, "moved"))["k1"]
    assert got["revisions"] == 1 and got["prior_state"] == "raised"


async def test_a_rewritten_reason_under_an_unchanged_state_is_counted_and_named(client):
    """The reason IS the evidence for a skip, and a reason quietly rewritten
    improves an after-the-fact convergence figure by the same route a rewritten
    refutation improves a precision one."""
    await record(client, "amend", ["k1"])
    await ledger(client, "amend", 1, [
        {"key": "k1", "state": "deferred", "source": "judge", "reason": "parked"},
    ])
    res = await ledger(client, "amend", 1, [
        {"key": "k1", "state": "deferred", "source": "judge", "reason": "actually, capped"},
    ], expect=200)
    assert res["amended"] == [{"key": "k1", "source": "judge", "state": "deferred",
                               "filled": [], "rewrote": ["reason"]}]
    assert by_key(await read(client, "amend"))["k1"]["revisions"] == 1


async def test_one_spelling_of_a_repository_however_the_caller_types_it(client):
    """The unique constraint is only as good as the spelling it is on: `Acme/X`
    beside `acme/x` is two ledgers for one defect."""
    await record(client, "fold", ["k1"])
    r = await client.post(
        "/review/ledger",
        json={"repo": f"  {repo_of('fold').upper()} ", "pr": 1, "round": 1,
              "entries": [{"key": "k1", "state": "raised", "source": "panel"}]},
        headers=AGENT)
    assert r.status_code == 201, r.text
    assert r.json()["repo"] == repo_of("fold")
    assert len(by_key(await read(client, "fold"))) == 1


async def test_the_read_can_be_narrowed_to_one_state(client):
    await record(client, "filter", ["k1", "k2"])
    await ledger(client, "filter", 1, [
        {"key": "k1", "state": "raised", "source": "panel"},
        {"key": "k2", "state": "unpaid", "source": "budget", "reason": "over budget"},
    ])
    only = await read(client, "filter", query="&state=unpaid")
    assert [f["key"] for f in only["findings"]] == ["k2"]
    r = await client.get(f"/review/ledger?repo={repo_of('filter')}&pr=1&state=parked",
                         headers=AGENT)
    assert r.status_code == 400, r.text


async def test_an_empty_ledger_is_a_real_answer_and_not_an_error(client):
    body = await read(client, "empty")
    assert body["findings"] == [] and body["entries"] == 0
    assert body["last_round"] == 0 and body["truncated"] is False


async def test_the_ledger_path_is_not_swallowed_by_the_run_detail_route(client):
    """A route-order regression, pinned because it is invisible in review.

    ``app/api/reviews.py`` ends with ``GET /review/{run_id}``, a catch-all one
    segment deep, and Starlette matches routes in registration order. Included
    after it, every ``GET /review/ledger`` came back 422 — "ledger is not a valid
    integer" — while the POST looked fine, because the catch-all is GET only. The
    ledger router is registered FIRST in ``app/main.py`` for this reason, and a
    reordering of those two lines fails here rather than in a harness at 3am.
    """
    r = await client.get("/review/ledger?repo=acme/route-order&pr=1", headers=AGENT)
    assert r.status_code == 200, r.text
    assert r.json()["repo"] == "acme/route-order"


async def test_a_caller_cannot_set_the_origin_round_itself(client):
    """It is derived from the board's own rows and never sent.

    A producer could get it wrong on a retry, on a resumed cycle, or by not
    having hydrated first, and the failure would be silent and in the flattering
    direction: a promoted finding acquiring a fresh round is exactly the "looks
    like new damage" defect this table exists to end. ``extra="forbid"`` is what
    makes the mistake a named rejection rather than a value quietly ignored.
    """
    await record(client, "originsent", ["k1"])
    res = await ledger(client, "originsent", 3, [
        {"key": "k1", "state": "raised", "source": "panel", "origin_round": 1},
    ], expect=422)
    assert "origin_round" in res["rejected"][0]["reason"]
