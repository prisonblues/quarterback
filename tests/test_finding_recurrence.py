"""#67 reaches the board: where a finding stands relative to the fix before it,
and what the judge said when asked whether that fix's premise still holds.

Two columns because two witnesses. ``recurrence`` is mechanical — the previous
round complained about this file, the fixer wrote lines in it, this finding is on
top of what it wrote — and the panel's own replay over 36 rounds of this board's
history says it fires on about four new findings in five whether a cycle is
circling or not. ``premise_verdict`` is the judge's, and it is the half that can
see what a finding SAYS. They are stored side by side and never folded together:
the rounds where they disagree are the ones worth a human's time, and a blended
number would hide exactly those.

The properties these are grouped by are the ones #48's own suite established for
``provenance``, because this is the same class of column and the same failure
would end it:

* **The fields survive the round trip, per finding included.** A per-finding,
  per-round measurement cannot be reconstructed from anything else the board
  keeps; every round that runs while the column is dropped is simply gone (#93).
* **Null means NOT RECORDED, never "does not recur".** NULL (nobody said), ``{}``
  (the question does not arise), all-zero (it was asked and there was nothing),
  and ``"unknown"`` (asked, unplaceable) are four different statements.
* **A dropped field says so.** #93's whole lesson: an ingest that silently
  discards what it does not understand is what made the first measurement
  worthless.
* **Every malformed shape records rather than 422s.** A garbled bucket must never
  cost a run its findings.

And one that is this issue's own: **nothing may gate on any of it.** The panel
side asserts that over its stop rule; here the guarantee is narrower and still
worth pinning — the columns are readable and countable, and no read path treats a
value in them as a reason to refuse anything.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import engine

from .conftest import LAPTOP

REPO = "acme/v267repo"
AGENT = {**LAPTOP, "X-Agent-Instance": "cc67dd"}

SHA = "b1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
EARLIER = "0f1e2d3c4b5a69788796a5b4c3d2e1f001234567"


def finding(title: str, **over) -> dict:
    f = {"title": title, "severity": "P2", "file": "app/api/reviews.py",
         "line": 10, "reviewers": ["claude"]}
    return {**f, **over}


def payload(pr: int, **over) -> dict:
    body = {
        "repo": REPO,
        "pr": pr,
        "judged": True,
        "judge_model": "opus",
        "reviewers_selected": ["claude"],
        "reviewers": {"claude": {"model": "opus", "ran": True}},
        "to_fix": [],
        "dismissed": [],
        "sonar_findings": [],
    }
    return {**body, **over}


async def record(client, pr: int, **over) -> dict:
    r = await client.post("/review", json=payload(pr, **over), headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


#: The keys `record_review` merges into its response when it refused something
#: this issue's fields sent. Named rather than tested for the presence of a
#: `dropped` object, because the response is FLAT — `{**recorded, **dropped}` —
#: so `"dropped" not in got` is vacuously true on every run and asserts nothing.
DRIFT_KEYS = ("recurrence_unknown", "recurrence_counts_unusable",
              "premise_verdict_unknown", "premise_counts_unusable",
              "unreadable_fields")


def _drift(got: dict) -> dict:
    return {k: got[k] for k in DRIFT_KEYS if k in got}


async def detail(client, run_id: int) -> dict:
    r = await client.get(f"/review/{run_id}", headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------

async def test_every_new_field_survives_the_round_trip(client):
    """The point of the release. A measurement the panel computes, POSTs and the
    board discards is #93 exactly, and the per-finding half is the one nothing can
    reconstruct afterwards."""
    got = await record(
        client, 6701, round=2, cycle="c67", head_sha=SHA,
        recurrence_counts={"revisited": 1, "fix-site": 0, "elsewhere": 0,
                           "unknown": 0},
        premise_counts={"invalidates": 1, "separate": 0, "unclear": 0,
                        "not-said": 0},
        to_fix=[finding("written twice", key="aaaa000000000002",
                        new_this_round=True, recurrence="revisited",
                        recurs_of="aaaa000000000001",
                        premise_verdict="invalidates")])
    assert not _drift(got), _drift(got)
    run = await detail(client, got["id"])
    assert run["recurrence_counts"] == {"revisited": 1, "fix-site": 0,
                                        "elsewhere": 0, "unknown": 0}
    assert run["premise_counts"] == {"invalidates": 1, "separate": 0,
                                     "unclear": 0, "not-said": 0}
    [f] = run["findings"]
    assert f["recurrence"] == "revisited"
    assert f["recurs_of"] == "aaaa000000000001"
    assert f["premise_verdict"] == "invalidates"


async def test_the_measurement_rides_the_finding_history(client):
    """`GET /review/findings` is where a defect's chain across rounds is read, and
    it is the read a calibration would actually be built on — "how often does a
    finding at the fix's site turn out to be the same premise" is a question about
    the chain, not about one run."""
    await record(client, 6702, round=2, cycle="c67b", head_sha=SHA,
                 to_fix=[finding("written twice", key="bbbb000000000002",
                                 recurrence="fix-site",
                                 premise_verdict="separate")])
    r = await client.get("/review/findings", params={"repo": REPO, "pr": 6702},
                         headers=AGENT)
    assert r.status_code == 200, r.text
    [chain] = r.json()["findings"]
    [obs] = chain["observations"]
    assert obs["recurrence"] == "fix-site"
    assert obs["premise_verdict"] == "separate"
    assert obs["recurs_of"] is None


# --------------------------------------------------------------------------
# Null is not recorded, and the four states stay four
# --------------------------------------------------------------------------

async def test_a_run_from_before_this_existed_records_nulls(client):
    """Every payload the panel has ever sent until now carries none of these. The
    board must store NULL — "nobody said" — and never an empty object, which is
    the panel's way of saying the question did not arise."""
    got = await record(client, 6703, to_fix=[finding("something")])
    run = await detail(client, got["id"])
    assert run["recurrence_counts"] is None and run["premise_counts"] is None
    [f] = run["findings"]
    assert f["recurrence"] is None and f["premise_verdict"] is None


async def test_the_question_not_arising_is_not_a_zero_result(client):
    """A round 1 sends `{}`; a round that could have measured and found nothing
    sends all-zero. Collapsing them would make "there was no earlier round" and
    "the fixer was working nowhere near any of this" one number, and the second is
    the interesting one."""
    r1 = await detail(client, (await record(
        client, 6704, round=1, recurrence_counts={}, premise_counts={}))["id"])
    r2 = await detail(client, (await record(
        client, 6705, round=2, cycle="c67c",
        recurrence_counts={"revisited": 0, "fix-site": 0, "elsewhere": 0,
                           "unknown": 0}))["id"])
    assert r1["recurrence_counts"] == {} and r1["premise_counts"] == {}
    assert r2["recurrence_counts"] == {"revisited": 0, "fix-site": 0,
                                       "elsewhere": 0, "unknown": 0}


async def test_unknown_is_a_bucket_and_not_a_missing_value(client):
    """A finding the board could not place was still ASKED about. `unknown` is
    that answer; NULL is the question never being put. Two states that look alike
    and must not merge — the same rule `provenance` records."""
    got = await record(
        client, 6706, round=2, cycle="c67d",
        to_fix=[finding("placed", key="cccc000000000001", recurrence="unknown"),
                finding("not asked", key="cccc000000000002")])
    run = await detail(client, got["id"])
    by = {f["key"]: f for f in run["findings"]}
    assert by["cccc000000000001"]["recurrence"] == "unknown"
    assert by["cccc000000000002"]["recurrence"] is None


async def test_the_judge_saying_nothing_is_not_the_judge_saying_unclear(client):
    """`unclear` is the judge looking and being unable to tell. NULL is the judge
    never being asked — every round with no earlier round. Without the split, "we
    did not ask" reads as "it could not tell", which flatters the measurement in
    the direction that makes it useless."""
    got = await record(
        client, 6707, round=2, cycle="c67e",
        premise_counts={"invalidates": 0, "separate": 1, "unclear": 1,
                        "not-said": 2},
        to_fix=[finding("could not tell", key="dddd000000000001",
                        premise_verdict="unclear"),
                finding("never asked", key="dddd000000000002")])
    run = await detail(client, got["id"])
    by = {f["key"]: f for f in run["findings"]}
    assert by["dddd000000000001"]["premise_verdict"] == "unclear"
    assert by["dddd000000000002"]["premise_verdict"] is None
    assert run["premise_counts"]["not-said"] == 2


# --------------------------------------------------------------------------
# A dropped field says so
# --------------------------------------------------------------------------

async def test_an_unrecognised_bucket_is_dropped_and_named(client):
    """#93's lesson applied to the new vocabulary. `circling` is the word this
    column was very nearly called, so a producer built against an earlier draft
    would send exactly that — and it must arrive as a named drift rather than as a
    stored value no consumer can interpret."""
    got = await record(
        client, 6708, round=2, cycle="c67f",
        to_fix=[finding("x", key="eeee000000000001", recurrence="circling")])
    assert got["recurrence_unknown"] == ["circling"]
    run = await detail(client, got["id"])
    assert run["findings"][0]["recurrence"] is None


async def test_an_unrecognised_premise_verdict_is_named_under_its_own_key(client):
    """Its own response key rather than one merged "a bucket was refused" list: a
    producer misspelling a premise verdict and one misspelling a recurrence bucket
    have different bugs, and a reader told only that something was refused has to
    guess which field to go and look at."""
    got = await record(
        client, 6709, round=2, cycle="c67g",
        to_fix=[finding("x", key="ffff000000000001",
                        premise_verdict="invalidates the premise")])
    assert got["premise_verdict_unknown"] == ["invalidates the premise"]
    assert "recurrence_unknown" not in got


async def test_an_unrecognised_tally_key_is_dropped_and_named(client):
    """The other drop path. A tally is published, so it must not carry a key no
    consumer can read — and the drop is reported, or the panel and the board have
    quietly stopped agreeing about the vocabulary."""
    got = await record(client, 6710, round=2, cycle="c67h",
                       recurrence_counts={"revisited": 2, "circling": 1},
                       premise_counts={"invalidates": 1, "maybe": 4})
    assert got["recurrence_unknown"] == ["circling"]
    assert got["premise_verdict_unknown"] == ["maybe"]
    run = await detail(client, got["id"])
    assert run["recurrence_counts"] == {"revisited": 2}
    assert run["premise_counts"] == {"invalidates": 1}


async def test_an_unbelievable_count_drops_with_its_key(client):
    """Not to zero. This whole family of measurements is built on zero being a
    claim — "attribution ran and found none" — so a count that cannot be believed
    must not become one."""
    got = await record(client, 6711, round=2, cycle="c67i",
                       recurrence_counts={"revisited": -1, "elsewhere": "two",
                                          "unknown": 3})
    assert set(got["recurrence_counts_unusable"]) == {"revisited",
                                                                 "elsewhere"}
    run = await detail(client, got["id"])
    assert run["recurrence_counts"] == {"unknown": 3}


async def test_a_tally_that_loses_every_key_is_null_and_not_the_empty_object(client):
    """The emptied dict would manufacture the round-1 statement out of a payload
    whose every answer was refused — the same collapse in the other direction, and
    worse, because `{}` is a positive claim that no earlier round existed."""
    got = await record(client, 6712, round=2, cycle="c67j",
                       recurrence_counts={"circling": 1, "spiralling": 2})
    run = await detail(client, got["id"])
    assert run["recurrence_counts"] is None
    assert set(got["recurrence_unknown"]) == {"circling", "spiralling"}


async def test_a_tally_of_the_wrong_shape_entirely_says_so(client):
    """The coarsest drop and the one that was silent for `provenance` until #93's
    repair: the per-key signals can only speak about a value they could iterate,
    so a list where an object belongs produced no entries and no word."""
    got = await record(client, 6713, round=2, cycle="c67k",
                       recurrence_counts=["revisited"],
                       premise_counts=7)
    assert set(got["unreadable_fields"]) >= {"recurrence_counts",
                                                        "premise_counts"}


async def test_a_non_string_bucket_is_named_rather_than_vanishing(client):
    """`recurrence: 5` used to be the shape that left nothing at all — reading as
    a finding nobody asked about rather than as a producer sending the wrong type,
    and a type-confused sender is the likelier drift of the two."""
    got = await record(client, 6714, round=2, cycle="c67l",
                       to_fix=[finding("x", key="1111000000000001",
                                       recurrence=5)])
    assert got["recurrence_unknown"] == ["5"]


# --------------------------------------------------------------------------
# The pointer, and the one rule it has
# --------------------------------------------------------------------------

async def test_a_pointer_under_any_other_bucket_is_dropped(client):
    """`recurs_of` names the earlier finding this one stands on, and it means
    nothing under a bucket that found no such chain — evidence for a judgement
    nobody made. Dropped at the API so a producer's slip is a null column rather
    than a 500 from the CHECK behind it."""
    got = await record(
        client, 6715, round=2, cycle="c67m",
        to_fix=[finding("x", key="2222000000000001", recurrence="elsewhere",
                        recurs_of="2222000000000000")])
    run = await detail(client, got["id"])
    assert run["findings"][0]["recurs_of"] is None


async def test_the_database_refuses_the_pair_the_api_would_not_write(client):
    """The rule at the boundary as well as in the validator, which is what makes
    the class closed rather than the endpoint patched: a write path added later —
    a backfill, an admin script, the next producer — fails loudly instead of
    inventing a chain that the measurement never found."""
    got = await record(client, 6716, round=2, cycle="c67n",
                       to_fix=[finding("x", key="3333000000000001")])
    async with engine.begin() as conn:
        [(rows,)] = (await conn.execute(text(
            "SELECT count(*) FROM review_findings WHERE run_id = :run"),
            {"run": got["id"]})).all()
        assert rows == 1, "the finding did not reach the table"
        with pytest.raises(IntegrityError) as bad:
            await conn.execute(text(
                "UPDATE review_findings SET recurs_of = 'deadbeefdeadbeef' "
                "WHERE run_id = :run"), {"run": got["id"]})
    assert "ck_review_findings_recurs_of_revisited" in str(bad.value)


async def test_an_overlong_pointer_is_dropped_rather_than_cut(client):
    """A truncated key matches no finding and would sit in the column looking
    exactly like one that does — which is worse than an honest null, because the
    column exists to be resolved against a row."""
    got = await record(
        client, 6717, round=2, cycle="c67o",
        to_fix=[finding("x", key="4444000000000001", recurrence="revisited",
                        recurs_of="k" * 5000)])
    run = await detail(client, got["id"])
    assert run["findings"][0]["recurrence"] == "revisited"
    assert run["findings"][0]["recurs_of"] is None


# --------------------------------------------------------------------------
# A malformed payload never costs the run its findings
# --------------------------------------------------------------------------

async def test_a_garbled_measurement_costs_the_measurement_and_nothing_else(client):
    """This module's standing rule. The findings in a payload are worth far more
    than the bucket beside them, and a 422 loses both."""
    got = await record(
        client, 6718, round=2, cycle="c67p",
        recurrence_counts="nonsense",
        to_fix=[finding("a real defect", key="5555000000000001",
                        recurrence={"bad": "shape"},
                        premise_verdict=["separate"])])
    run = await detail(client, got["id"])
    assert len(run["findings"]) == 1
    assert run["findings"][0]["title"] == "a real defect"
    assert run["findings"][0]["recurrence"] is None
    assert run["recurrence_counts"] is None


async def test_an_ordinary_run_reports_no_drift_at_all(client):
    """The quiet path. A response that names a drop on every ordinary run trains
    a reader to ignore the field, which is how the next real one goes unnoticed."""
    got = await record(
        client, 6719, round=2, cycle="c67q",
        recurrence_counts={"revisited": 1, "fix-site": 0, "elsewhere": 0,
                           "unknown": 0},
        to_fix=[finding("x", key="6666000000000001", recurrence="revisited",
                        recurs_of="6666000000000000")])
    assert not _drift(got), json.dumps(_drift(got))


# --------------------------------------------------------------------------
# The window view
# --------------------------------------------------------------------------

async def test_the_stats_window_splits_both_ways_and_names_its_own_absence(client):
    """Both splits over the same population and the same denominator, so a reader
    can hold them against each other — which is the whole reason two questions are
    asked. `not_measured` and `not_asked` are reported rather than omitted: these
    buckets are usually a small part of a window, and four numbers read as the
    whole of it unless the remainder is named."""
    await record(
        client, 6720, round=2, cycle="c67r",
        to_fix=[finding("one", key="7777000000000001", recurrence="revisited",
                        recurs_of="7777000000000000", premise_verdict="separate"),
                finding("two", key="7777000000000002", recurrence="elsewhere",
                        premise_verdict="invalidates"),
                finding("three", key="7777000000000003")])
    r = await client.get("/review/stats", params={"repo": REPO, "days": 3650},
                         headers=AGENT)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["by_recurrence"]["revisited"] >= 1
    assert got["by_recurrence"]["elsewhere"] >= 1
    assert got["by_recurrence"]["not_measured"] >= 1
    assert got["by_premise"]["separate"] >= 1
    assert got["by_premise"]["invalidates"] >= 1
    assert got["by_premise"]["not_asked"] >= 1
    # Every word of both vocabularies is present even at zero, so a client never
    # has to guess whether a missing bucket was empty or unsupported.
    assert set(got["by_recurrence"]) >= {"revisited", "fix-site", "elsewhere",
                                         "unknown", "not_measured"}
    assert set(got["by_premise"]) >= {"invalidates", "separate", "unclear",
                                      "not_asked"}


async def test_the_run_list_carries_the_tallies_without_fetching_the_findings(client):
    """`GET /reviews` is the shape a dashboard reads, and the tallies are what let
    it show the shape of a round without walking every finding — the same argument
    `provenance_counts` makes for riding every view."""
    got = await record(client, 6721, round=2, cycle="c67s",
                       recurrence_counts={"revisited": 3},
                       premise_counts={"separate": 3})
    r = await client.get("/reviews", params={"repo": REPO}, headers=AGENT)
    assert r.status_code == 200, r.text
    [row] = [x for x in r.json() if x["id"] == got["id"]]
    assert row["recurrence_counts"] == {"revisited": 3}
    assert row["premise_counts"] == {"separate": 3}
