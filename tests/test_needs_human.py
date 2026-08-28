"""#279: "a human has to look at this" becomes a class, a reason and a count.

The harness formed this judgement in four places and recorded it in none. What
this file pins is not that a boolean round-trips — it is the handful of rules
that make the number worth reading, each of which is a way the feature could ship
looking correct and measuring nothing:

* **a bare flag is refused**, at the API and at the database, because a flag ENDS
  a fix cycle and #67 is explicit that an agent must not escalate to end a cycle
  it finds tedious. A refusal is NAMED in the response, never silent — the
  failure #279 was filed about must not reappear inside its own repair;
* **the flag never collapses into ``could_not_assess``**: that field means "I
  lacked context", this one means "no context would close this", and one column
  holding both puts a grep-able question and a design decision in one bucket;
* **the declaration is attributable and falsifiable** — per reporter as well as
  per finding, counted per seat, and scored against what the record later said —
  which is what stops it being a free way out of work;
* **a refused flag reaches no scorecard**, or the published per-seat count would
  claim a declaration no row stores;
* **the waiting list's counts are not capped by its page**, because "how many
  are waiting?" answered with ``limit`` is the one answer that is never true.

Each test uses its own repo slug. ``GET /review/stats`` and
``GET /review/needs-human`` aggregate across a whole repo and the suite shares
one database, so a shared slug would make every count here depend on which other
tests had run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import engine
from app.models.review import ReviewFinding, ReviewFindingReport, ReviewRun
from app.needs_human import (
    LABEL_COLOURS,
    MAX_REASON_CHARS,
    NEEDS_HUMAN_CLASS_HELP,
    NEEDS_HUMAN_CLASSES,
    NEEDS_HUMAN_LABELS,
    needs_human_class_or_none,
    needs_human_reason_or_none,
)

from .conftest import LAPTOP

AGENT = {**LAPTOP, "X-Agent-Instance": "279abc"}

REASON = "which of the two modes this is — nobody but you can say"


def repo_of(case: str) -> str:
    return f"acme/nh-{case}"


def finding(key: str, **over) -> dict:
    f = {"title": f"finding {key}", "severity": "P2", "file": "app/api/reviews.py",
         "line": 10, "reviewers": ["claude"], "key": key}
    return {**f, **over}


async def record(client, case: str, to_fix: list[dict], **over) -> dict:
    body = {
        "repo": repo_of(case),
        "pr": 1,
        "judged": True,
        "judge_model": "opus",
        "reviewers_selected": ["claude", "codex"],
        "reviewers": {"claude": {"model": "opus", "ran": True},
                      "codex": {"model": "gpt-5", "ran": True}},
        "to_fix": to_fix,
        "dismissed": [],
        "sonar_findings": [],
    }
    r = await client.post("/review", json={**body, **over}, headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


async def chain(client, case: str) -> dict:
    r = await client.get("/review/findings",
                         params={"repo": repo_of(case), "pr": 1}, headers=LAPTOP)
    assert r.status_code == 200, r.text
    return r.json()


async def waiting(client, case: str | None = None, **params) -> dict:
    if case is not None:
        params.setdefault("repo", repo_of(case))
    r = await client.get("/review/needs-human", params=params, headers=LAPTOP)
    assert r.status_code == 200, r.text
    return r.json()


async def stats(client, case: str) -> dict:
    r = await client.get("/review/stats", params={"repo": repo_of(case)}, headers=LAPTOP)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- the vocabulary

def test_the_vocabulary_is_closed_and_every_class_carries_help_and_a_label():
    assert NEEDS_HUMAN_CLASSES == ("decision", "taste", "ui", "environment", "auth",
                                   "chore", "other")
    for c in NEEDS_HUMAN_CLASSES:
        assert NEEDS_HUMAN_CLASS_HELP[c]
        assert NEEDS_HUMAN_LABELS[c] == f"needs-human/{c}"
        # Six hex digits and no leading '#': what `gh label create --color` wants,
        # and a value it rejects would make `apply` fail on a repo rather than on
        # a test.
        assert len(LABEL_COLOURS[c]) == 6 and int(LABEL_COLOURS[c], 16) >= 0


@pytest.mark.parametrize("sent,want", [
    ("ui", "ui"), (" UI ", "ui"), ("Decision", "decision"),
    ("needs-decision", None), ("", None), ("  ", None), (None, None),
    (5, None), (["ui"], None), (True, None),
])
def test_a_class_outside_the_vocabulary_normalises_to_nothing(sent, want):
    # A value a consumer FILTERS on is never stored verbatim when it is not one
    # the consumer knows: a misspelt class would leave every by-class count while
    # still reading as a flag, which is the direction that hides the signal.
    assert needs_human_class_or_none(sent) == want


@pytest.mark.parametrize("sent,want", [
    ("  because  ", "because"), ("", None), ("   ", None), ("\t\n", None),
    (None, None), (5, None), (True, None),
])
def test_a_reason_that_says_nothing_is_not_a_reason(sent, want):
    assert needs_human_reason_or_none(sent) == want


def test_a_reason_is_bounded_because_an_authenticated_sender_is_not():
    assert len(needs_human_reason_or_none("x" * (MAX_REASON_CHARS + 500))) == MAX_REASON_CHARS


# ------------------------------------------------------------------- the storage

async def test_a_flagged_finding_stores_the_class_and_reason_per_finding_and_per_reporter(client):
    await record(client, "store", [finding(
        "k1", reviewers=["claude", "codex"],
        reported_by=[{"reviewer": "claude", "detail": "the two modes", "needs_human": True},
                     {"reviewer": "codex", "detail": "agreed"}],
        needs_human=True, needs_human_class="decision", needs_human_reason=REASON,
    )])
    c = (await chain(client, "store"))["findings"][0]
    assert (c["needs_human"], c["needs_human_class"], c["needs_human_reason"]) == (
        True, "decision", REASON)
    by = {r["reviewer"]: r for r in c["observations"][0]["reported_by"]}
    # Claude declared it; codex sent an account and said nothing, so it is not
    # credited — a group flag credited to everyone who happened to raise the
    # finding makes the member that called it and the member that didn't
    # indistinguishable, which is the whole reason this is stored per reporter.
    assert by["claude"]["needs_human"] is True
    assert by["claude"]["needs_human_class"] == "decision"
    assert by["codex"]["needs_human"] is False
    assert by["codex"]["needs_human_class"] is None


async def test_two_reporters_may_disagree_about_what_a_human_is_needed_for(client):
    await record(client, "disagree", [finding(
        "k1", reviewers=["claude", "codex"],
        reported_by=[
            {"reviewer": "claude", "needs_human": True, "needs_human_class": "ui",
             "needs_human_reason": "55 rows into a 38-row pane"},
            {"reviewer": "codex", "needs_human": True, "needs_human_class": "taste",
             "needs_human_reason": "the column heading is the wrong word"},
        ],
        needs_human=True, needs_human_class="ui", needs_human_reason="somebody has to look",
    )])
    c = (await chain(client, "disagree"))["findings"][0]
    by = {r["reviewer"]: r for r in c["observations"][0]["reported_by"]}
    # Each member keeps its own; the finding keeps the merged statement. Two
    # members agreeing a human is needed and disagreeing about what for is data,
    # not a merge conflict to be resolved by whichever row was written last.
    assert by["claude"]["needs_human_class"] == "ui"
    assert by["codex"]["needs_human_class"] == "taste"
    assert c["needs_human_class"] == "ui"


async def test_a_reporters_own_class_and_reason_are_never_split_across_two_members(client):
    # No finding-level pair at all: the donor is the first flagged reporter that
    # carries BOTH, so the stored class and reason come from one declaration.
    # Assembling them from whichever member supplied each half would record a
    # statement nobody made.
    await record(client, "donor", [finding(
        "k1", reviewers=["claude", "codex"],
        reported_by=[
            {"reviewer": "claude", "needs_human": True, "needs_human_class": "auth"},
            {"reviewer": "codex", "needs_human": True, "needs_human_class": "environment",
             "needs_human_reason": "only true on the box"},
        ],
        needs_human=True,
    )])
    c = (await chain(client, "donor"))["findings"][0]
    assert (c["needs_human_class"], c["needs_human_reason"]) == (
        "environment", "only true on the box")


# --------------------------------------------------------------- a bare flag

async def test_a_flag_with_no_reason_is_refused_and_named_rather_than_stored_bare(client):
    body = await record(client, "noreason", [finding(
        "k1", needs_human=True, needs_human_class="taste")])
    # Named, not swallowed: a refusal nobody is told about is indistinguishable
    # from a producer that never flagged.
    assert body["needs_human_refused"] == {"no_reason": 1}
    c = (await chain(client, "noreason"))["findings"][0]
    assert c["needs_human"] is False and c["needs_human_class"] is None
    # ...and the run still recorded. Ingest is best-effort for the panel: one
    # malformed field must not cost a caller its findings.
    assert body["findings"] == 1


async def test_a_flag_with_no_class_is_refused_because_a_bare_stop_routes_nowhere(client):
    body = await record(client, "noclass", [finding(
        "k1", needs_human=True, needs_human_reason=REASON)])
    assert body["needs_human_refused"] == {"no_class": 1}
    assert (await chain(client, "noclass"))["findings"][0]["needs_human"] is False


async def test_a_whitespace_only_reason_is_not_evidence(client):
    body = await record(client, "blankreason", [finding(
        "k1", needs_human=True, needs_human_class="ui", needs_human_reason="   \t ")])
    assert body["needs_human_refused"] == {"no_reason": 1}


async def test_an_unknown_class_is_reported_as_drift_and_the_flag_refused(client):
    body = await record(client, "drift", [
        finding("k1", needs_human=True, needs_human_class="needs-decision",
                needs_human_reason=REASON),
        finding("k2", reviewers=["claude"],
                reported_by=[{"reviewer": "claude", "needs_human": True,
                              "needs_human_class": "blocked",
                              "needs_human_reason": "who knows"}]),
    ])
    # Both spellings echoed — the finding's and the reporter's — because a
    # producer drifting on either is a different bug.
    assert sorted(body["needs_human_unknown"]) == ["blocked", "needs-decision"]
    assert body["needs_human_refused"] == {"no_class": 2}
    for c in (await chain(client, "drift"))["findings"]:
        assert c["needs_human"] is False


async def test_a_blank_class_is_a_malformed_value_and_not_a_silence(client):
    body = await record(client, "blankclass", [
        finding("k1", needs_human=True, needs_human_class="  ", needs_human_reason=REASON),
    ])
    # `""` is echoed: a producer sending an empty class HAS made a statement,
    # just not a usable one, and it must not read like one that said nothing.
    assert body["needs_human_unknown"] == [""]


async def test_a_long_class_spelling_is_echoed_cut_and_marked_as_cut(client):
    body = await record(client, "longclass", [
        finding("k1", needs_human=True, needs_human_class="x" * 200,
                needs_human_reason=REASON)])
    echoed = body["needs_human_unknown"][0]
    # Bounded, because an authenticated sender is not one — and MARKED, because a
    # truncated name handed back as a whole one is a drift signal that lies about
    # what arrived. The echo helper does both; a second slice on top would take
    # the mark off again.
    assert echoed.endswith("…") and len(echoed) < 200


async def test_an_ordinary_run_carries_no_needs_human_keys_at_all(client):
    body = await record(client, "quiet", [finding("k1")])
    # The response shape every existing caller already parses. A drop key on a
    # run that dropped nothing teaches its reader to ignore the key.
    assert "needs_human_refused" not in body and "needs_human_unknown" not in body


# ------------------------------------------------------------------ attribution

async def test_a_reporters_explicit_no_is_not_overturned_by_the_coarser_channel(client):
    await record(client, "explicitno", [finding(
        "k1", reviewers=["claude", "codex"],
        reported_by=[{"reviewer": "claude", "needs_human": False},
                     {"reviewer": "codex", "needs_human": True}],
        needs_human_by=["claude", "codex"],
        needs_human=True, needs_human_class="ui", needs_human_reason=REASON,
    )])
    c = (await chain(client, "explicitno"))["findings"][0]
    by = {r["reviewer"]: r["needs_human"] for r in c["observations"][0]["reported_by"]}
    # A reporter is authoritative about its own no. Reading it as "no data" would
    # manufacture a declaration it declined to make.
    assert by == {"claude": False, "codex": True}
    s = {r["reviewer"]: r["human_flagged"] for r in (await stats(client, "explicitno"))["by_model"]}
    assert s == {"claude": 0, "codex": 1}


async def test_a_finding_flagged_with_nobody_creditable_credits_every_silent_member(client):
    await record(client, "nobody", [finding(
        "k1", reviewers=["claude", "codex"],
        needs_human=True, needs_human_class="decision", needs_human_reason=REASON,
    )])
    # Over-crediting is visible and correctable; dropping the declaration is
    # neither, and would leave the finding flagged with nobody's rate moved.
    s = {r["reviewer"]: r["human_flagged"] for r in (await stats(client, "nobody"))["by_model"]}
    assert s == {"claude": 1, "codex": 1}


async def test_a_refused_flag_reaches_no_scorecard(client):
    await record(client, "refusedcard", [finding(
        "k1", reviewers=["claude"], needs_human=True, needs_human_class="ui")])
    # The published per-seat count must never claim a declaration no row stores:
    # that is the evidence rule arriving through the scorecard instead of the
    # finding, and it is the reading that makes a flag free again.
    s = {r["reviewer"]: r["human_flagged"] for r in (await stats(client, "refusedcard"))["by_model"]}
    assert s["claude"] == 0


async def test_human_flagged_counts_confirmed_findings_only(client):
    await record(client, "confirmedonly", [
        finding("k1", reviewers=["claude"], needs_human=True,
                needs_human_class="ui", needs_human_reason=REASON),
    ], dismissed=[
        finding("k2", reviewers=["claude"], needs_human=True,
                needs_human_class="ui", needs_human_reason=REASON),
    ])
    # A declaration attached to a finding the judge dismissed is not a claim
    # worth scoring — the same population `rereview_flagged` is counted over.
    s = {r["reviewer"]: r["human_flagged"]
         for r in (await stats(client, "confirmedonly"))["by_model"]}
    assert s["claude"] == 1


async def test_a_flag_is_not_could_not_assess_and_the_two_never_merge(client):
    await record(client, "notcna", [finding(
        "k1", reviewers=["claude"], needs_human=True,
        needs_human_class="decision", needs_human_reason=REASON)],
        reviewers={"claude": {"model": "opus", "ran": True,
                              "could_not_assess": ["does this module import that"]}})
    row = (await stats(client, "notcna"))["by_model"][0]
    # One says "I lacked context" — a gap a grep closes. The other says "no
    # context would close this". A feature that reported them as one number would
    # put a four-minute question and a design decision in the same bucket.
    assert row["declared_gaps_runs"] == 1
    assert row["human_flagged"] == 1


# ------------------------------------------------------- the database boundary

async def test_the_database_refuses_a_bare_flag_arriving_by_another_door(client):
    body = await record(client, "dbbare", [finding("k1")])
    async with AsyncSession(engine) as s:
        f = await s.get(ReviewFinding, (await _finding_ids(s, body["id"]))[0])
        f.needs_human = True
        # A backfill, an admin script or the next write path must not be able to
        # insert one either — the rule the API enforces, at the boundary.
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_the_database_refuses_evidence_with_no_flag_behind_it(client):
    body = await record(client, "dborphan", [finding("k1")])
    async with AsyncSession(engine) as s:
        f = await s.get(ReviewFinding, (await _finding_ids(s, body["id"]))[0])
        f.needs_human_class = "ui"
        f.needs_human_reason = REASON
        # A class and a reason on an unflagged row read exactly like a
        # declaration somebody later withdrew, and nothing in the table could
        # tell those apart.
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_the_database_refuses_a_class_outside_the_vocabulary(client):
    body = await record(client, "dbvocab", [finding("k1")])
    async with AsyncSession(engine) as s:
        f = await s.get(ReviewFinding, (await _finding_ids(s, body["id"]))[0])
        f.needs_human, f.needs_human_class, f.needs_human_reason = True, "vibes", REASON
        # It feeds a count, and an unknown value would silently leave the
        # numerator while still counting as coverage.
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_the_reporter_row_carries_the_same_two_rules(client):
    body = await record(client, "dbreport", [finding(
        "k1", reported_by=[{"reviewer": "claude", "detail": "x"}])])
    async with AsyncSession(engine) as s:
        ids = await _finding_ids(s, body["id"])
        r = await s.scalar(select(ReviewFindingReport)
                           .where(ReviewFindingReport.finding_id.in_(ids)))
        r.needs_human = True
        # Not redundant with the finding's: `/review/stats` scores THESE rows, so
        # a bare flag arriving here lands directly in a published per-seat figure.
        with pytest.raises(IntegrityError):
            await s.commit()


async def _finding_ids(session, run_id: int) -> list[int]:
    return list((await session.scalars(
        select(ReviewFinding.id).where(ReviewFinding.run_id == run_id)
        .order_by(ReviewFinding.id))).all())


# ------------------------------------------------------- what is waiting, by class

async def test_the_waiting_list_answers_by_class_and_for_how_long(client):
    await record(client, "waiting", [
        finding("k1", needs_human=True, needs_human_class="ui",
                needs_human_reason="55 rows into a 38-row pane"),
        finding("k2", needs_human=True, needs_human_class="ui",
                needs_human_reason="the chip is unreadable at that size"),
        finding("k3", needs_human=True, needs_human_class="decision",
                needs_human_reason=REASON),
        finding("k4"),
    ])
    w = await waiting(client, "waiting")
    assert w["waiting"] == 3
    assert {c: b["waiting"] for c, b in w["by_class"].items()} == {"ui": 2, "decision": 1}
    # Five `ui` checks and one `decision` is a different afternoon from six
    # decisions — which is the entire argument for the class being in the answer.
    assert [i["key"] for i in w["items"]] == ["k1", "k2", "k3"]
    assert all(i["age_seconds"] >= 0 for i in w["items"])
    assert w["items"][0]["label"] == "needs-human/ui"
    # The vocabulary is published so a producer discovers it here rather than
    # hardcoding a fourth copy of it.
    assert list(w["classes"]) == list(NEEDS_HUMAN_CLASSES)


async def test_by_class_keeps_the_vocabularys_order_not_the_databases(client):
    await record(client, "order", [
        finding("k1", needs_human=True, needs_human_class="ui", needs_human_reason=REASON),
        finding("k2", needs_human=True, needs_human_class="decision", needs_human_reason=REASON),
        finding("k3", needs_human=True, needs_human_class="auth", needs_human_reason=REASON),
    ])
    w = await waiting(client, "order")
    assert list(w["by_class"]) == ["decision", "ui", "auth"]


async def test_a_dismissed_finding_is_not_waiting_on_anybody(client):
    await record(client, "dismissed", [], dismissed=[
        finding("k1", needs_human=True, needs_human_class="ui", needs_human_reason=REASON)])
    # The judge ruled it was not a defect, so it is not work waiting on anybody.
    assert (await waiting(client, "dismissed"))["waiting"] == 0


async def test_an_unjudged_finding_is_waiting_because_nobody_ruled_on_it(client):
    await record(client, "unjudged", [
        finding("k1", needs_human=True, needs_human_class="auth", needs_human_reason=REASON)],
        judged=False)
    # Excluding these would hide every flag raised on a run whose judge was
    # skipped or crashed — precisely the rounds where a human is most likely
    # to be the only one who can settle it.
    assert (await waiting(client, "unjudged"))["waiting"] == 1


async def test_one_defect_flagged_in_three_rounds_is_one_thing_a_person_owes(client):
    for rnd in (1, 2, 3):
        await record(client, "rounds", [
            finding("k1", needs_human=True, needs_human_class="taste",
                    needs_human_reason="is `human_flagged` the right name")],
            round=rnd, cycle="c1")
    w = await waiting(client, "rounds")
    assert w["waiting"] == 1
    item = w["items"][0]
    assert item["observations"] == 3
    # `age_seconds` measures from the FIRST flag: the age of the question, not of
    # the latest time somebody restated it.
    assert item["first_flagged"] <= item["last_flagged"]


async def test_an_outcome_retires_a_waiting_item_and_include_settled_shows_it(client):
    await record(client, "settled", [
        finding("k1", needs_human=True, needs_human_class="decision", needs_human_reason=REASON)])
    r = await client.post("/review/outcomes", json={
        "repo": repo_of("settled"), "pr": 1,
        "outcomes": [{"key": "k1", "outcome": "deferred", "deferred_to": "#178"}],
    }, headers=AGENT)
    assert r.status_code in (200, 201), r.text
    # Any outcome retires it, `deferred` included: that is not the human having
    # answered, it is somebody having ACTED, and where it went is on the outcome.
    assert (await waiting(client, "settled"))["waiting"] == 0
    both = await waiting(client, "settled", include_settled=True)
    assert both["waiting"] == 0 and len(both["items"]) == 1
    assert both["items"][0]["outcome"]["deferred_to"] == "#178"


async def test_the_counts_are_over_everything_matched_and_the_list_alone_is_capped(client):
    await record(client, "cap", [
        finding(f"k{i}", needs_human=True, needs_human_class="ui",
                needs_human_reason=f"pane {i}") for i in range(5)])
    w = await waiting(client, "cap", limit=2)
    # "How many are waiting?" answered with `limit` is the one answer that is
    # never true.
    assert w["waiting"] == 5 and w["by_class"]["ui"]["waiting"] == 5
    assert w["truncated"] is True and w["listed"] == 2 and len(w["items"]) == 2


async def test_a_class_filter_narrows_the_list_and_the_counts_together(client):
    await record(client, "filter", [
        finding("k1", needs_human=True, needs_human_class="ui", needs_human_reason=REASON),
        finding("k2", needs_human=True, needs_human_class="auth", needs_human_reason=REASON),
    ])
    w = await waiting(client, "filter", **{"class": "auth"})
    assert w["waiting"] == 1 and [i["key"] for i in w["items"]] == ["k2"]


async def test_a_reclassified_defect_answers_under_its_current_class_only(client):
    # Round 2 called it `ui`; round 3 called it `taste`. The class is a question
    # about the DEFECT, so it is the newest flagged observation's — filtering rows
    # by class before grouping would return this defect under BOTH filters, each
    # with a different age, and the `ui` answer would name a class it no longer
    # carries. v2.70's "select first, classify second", one endpoint over.
    await record(client, "reclass", [
        finding("k1", needs_human=True, needs_human_class="ui",
                needs_human_reason="check the pane")], round=2, cycle="c1")
    await record(client, "reclass", [
        finding("k1", needs_human=True, needs_human_class="taste",
                needs_human_reason="the heading is the wrong word")], round=3, cycle="c1")
    assert (await waiting(client, "reclass", **{"class": "ui"}))["waiting"] == 0
    only = await waiting(client, "reclass", **{"class": "taste"})
    assert only["waiting"] == 1
    # ...and the un-narrowed answer counts it once, under the current class, with
    # both observations behind it.
    all_of_them = await waiting(client, "reclass")
    assert {c: b["waiting"] for c, b in all_of_them["by_class"].items()} == {"taste": 1}
    assert all_of_them["items"][0]["observations"] == 2


async def test_current_means_the_last_round_not_the_last_row_written(client):
    # Round 2 is recorded first and round 1 is backfilled after it, so the
    # `ui` observation has the HIGHER id and the EARLIER review clock. The
    # defect's current class is round 2's, because "current" follows the review's
    # chronology and not the board's insertion order — `GET /review/findings`
    # orders a chain the same way and the two must not disagree.
    await record(client, "backfill", [
        finding("k1", needs_human=True, needs_human_class="taste",
                needs_human_reason="the heading is the wrong word")], round=2, cycle="c1")
    late = await record(client, "backfill", [
        finding("k1", needs_human=True, needs_human_class="ui",
                needs_human_reason="check the pane")], round=1, cycle="c1")
    async with AsyncSession(engine) as db:
        run = await db.get(ReviewRun, late["id"])
        run.ts = datetime.now(UTC) - timedelta(days=3)
        await db.commit()
    w = await waiting(client, "backfill")
    assert [i["class"] for i in w["items"]] == ["taste"]
    assert list(w["by_class"]) == ["taste"]
    assert (await waiting(client, "backfill", **{"class": "ui"}))["waiting"] == 0
    # ...and the age still comes from the earliest flagged round, wherever it was
    # written.
    assert w["items"][0]["age_seconds"] > 2 * 86400


async def test_the_window_selects_defects_and_never_shortens_their_age(client):
    # The window decides which defects are in scope. It must not decide which of
    # their observations are counted: `first_flagged` is the age of the QUESTION,
    # and an age measured only inside the window is short by however long the
    # question predates it — understating exactly the oldest items, which are the
    # ones this endpoint exists to surface.
    old_run = await record(client, "window", [
        finding("k1", needs_human=True, needs_human_class="decision",
                needs_human_reason=REASON)], round=1, cycle="c1")
    # Backdated rather than waited for: the whole point is a first flag OUTSIDE
    # the window, and two runs recorded milliseconds apart cannot show it.
    long_ago = datetime.now(UTC) - timedelta(days=30)
    async with AsyncSession(engine) as db:
        run = await db.get(ReviewRun, old_run["id"])
        run.ts = long_ago
        await db.commit()
    await record(client, "window", [
        finding("k1", needs_human=True, needs_human_class="decision",
                needs_human_reason=REASON)], round=2, cycle="c1")

    narrow = await waiting(client, "window", days=7)
    assert narrow["waiting"] == 1
    item = narrow["items"][0]
    assert item["first_flagged"] == long_ago.isoformat()
    assert item["age_seconds"] > 29 * 86400
    assert item["observations"] == 2
    assert narrow["by_class"]["decision"]["oldest_age_seconds"] > 29 * 86400


async def test_an_unknown_class_filter_is_refused_rather_than_matching_nothing(client):
    r = await client.get("/review/needs-human",
                         params={"repo": repo_of("filter"), "class": "needs-decision"},
                         headers=LAPTOP)
    # An empty list reads exactly like "nothing is waiting", which is the most
    # dangerous possible answer to this endpoint's question.
    assert r.status_code == 400
    assert "needs-decision" in r.text


async def test_the_waiting_list_needs_a_reader(client):
    assert (await client.get("/review/needs-human")).status_code == 401


# ------------------------------------------------- the escalation list, and stats

async def test_a_chain_stays_flagged_when_a_later_round_stops_repeating_it(client):
    await record(client, "keys", [
        finding("k1", needs_human=True, needs_human_class="ui", needs_human_reason=REASON),
        finding("k2")], round=1, cycle="c1")
    await record(client, "keys", [finding("k1"), finding("k2")], round=2, cycle="c1")
    h = await chain(client, "keys")
    # A later round whose producer does not emit the flag has not withdrawn it —
    # it has said nothing. Withdrawal is `POST /review/outcomes`.
    assert h["needs_human_keys"] == ["k1"]
    flagged = {c["key"]: c["needs_human"] for c in h["findings"]}
    assert flagged == {"k1": True, "k2": False}


async def test_stats_reports_flags_per_reviewer_and_how_many_were_refuted(client):
    await record(client, "refuted", [
        finding("k1", reviewers=["claude"], needs_human=True, needs_human_class="taste",
                needs_human_reason="the heading is the wrong word",
                reported_by=[{"reviewer": "claude", "needs_human": True}]),
        finding("k2", reviewers=["claude"], needs_human=True, needs_human_class="ui",
                needs_human_reason="check the pane",
                reported_by=[{"reviewer": "claude", "needs_human": True}]),
    ])
    r = await client.post("/review/outcomes", json={
        "repo": repo_of("refuted"), "pr": 1,
        "outcomes": [{"key": "k1", "outcome": "refuted",
                      "note": "the heading is quoted from the spec; no judgement was owed"}],
    }, headers=AGENT)
    assert r.status_code in (200, 201), r.text
    row = next(m for m in (await stats(client, "refuted"))["by_model"]
               if m["reviewer"] == "claude")
    # The flag is a way OUT of work, so the rate at which a seat reaches for it
    # has to be readable against something. This is that something.
    assert row["human_flagged"] == 2
    assert row["human_flagged_defects"] == 2
    assert row["human_refuted"] == 1


async def test_a_caller_with_no_accounts_counts_in_human_flagged_and_in_nobodys_defects(client):
    await record(client, "noaccounts", [
        finding("k1", reviewers=["claude"], needs_human=True, needs_human_class="ui",
                needs_human_reason=REASON)])
    row = next(m for m in (await stats(client, "noaccounts"))["by_model"]
               if m["reviewer"] == "claude")
    # The two grains, and the reason all three numbers are published rather than
    # one of them. `human_flagged` counts every channel a declaration can arrive
    # by. The per-defect pair is attributed through `review_finding_reports` — the
    # row of the member that MADE the declaration — so a payload with no accounts
    # scores there for nobody. That zero means "not attributable", never "none",
    # and inferring it from `reviewers` instead would credit everyone who happened
    # to raise the finding, which is the collapse the per-reporter row exists to
    # prevent. `panel.py` sends accounts; a hand-rolled caller may not.
    assert row["human_flagged"] == 1
    assert row["human_flagged_defects"] == 0
    assert row["human_refuted"] == 0


async def test_the_scorecard_publishes_the_count_it_stores(client):
    body = await record(client, "card", [
        finding("k1", reviewers=["claude"], needs_human=True, needs_human_class="auth",
                needs_human_reason="try the credential path on a real box")])
    r = await client.get(f"/review/{body['id']}", headers=LAPTOP)
    assert r.status_code == 200, r.text
    card = next(c for c in r.json()["reviewers"] if c["name"] == "claude")
    # Stored is not shipped: a column nothing exposes is a column nothing can be
    # measured with.
    assert card["human_flagged"] == 1
