"""v2.37: a finding's outcome, after somebody acted on it.

The judge rules once, at review time, with no more access to the answer than the
reviewer it is ruling on — and the leaderboard was built on that ruling alone. On
PR #64 three of six judge-confirmed P2s were plainly wrong and are still in the
board as confirmed; #32 r2 produced the opposite case, a confirmed finding
refuted by a real transcript, recorded nowhere.

What this file pins is the shape that makes the number honest rather than the
happy path:

* the outcome is per DEFECT, so a defect raised in three rounds is three
  observations and one outcome — the multiplication that would weight one
  refutation by how long the fix loop ran;
* ``refuted`` cannot be recorded without the reasoning, because a bare
  contradiction of the judge is the same confident-assertion-with-nothing-behind-
  it that this release exists to measure;
* the verdict and the outcome never merge, and are allowed to disagree;
* a change of answer is visible (``revisions``/``prior_outcome``) and a repeat
  never erases the evidence for the answer it repeats;
* an unattested refutation is recorded and NAMED, never silently counted — the
  API cannot tell a fixer from a reviewer, so #77's self-grading rule is
  published rather than pretended.

Each test uses its own repo. ``GET /review/stats`` aggregates a whole repo and
the suite shares one database, so a shared slug would make every count here
depend on which other tests had run — the assertions would drift as the file
grows and would be repaired by loosening them, which is how a suite stops
measuring anything.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import IntegrityError

from app.api import reviews

from .conftest import LAPTOP

AGENT = {**LAPTOP, "X-Agent-Instance": "77aabb"}


def repo_of(case: str) -> str:
    return f"acme/v237-{case}"


def finding(key: str, **over) -> dict:
    f = {"title": f"finding {key}", "severity": "P2", "file": "app/api/reviews.py",
         "line": 10, "reviewers": ["claude"], "key": key}
    return {**f, **over}


async def record(client, case: str, **over) -> dict:
    body = {
        "repo": repo_of(case),
        "pr": 1,
        "judged": True,
        "judge_model": "opus",
        "reviewers_selected": ["claude"],
        "reviewers": {"claude": {"model": "opus", "ran": True}},
        "to_fix": [],
        "dismissed": [],
        "sonar_findings": [],
    }
    r = await client.post("/review", json={**body, **over}, headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


async def post_outcomes(client, case: str, items: list[dict], **over):
    return await client.post(
        "/review/outcomes",
        json={"repo": repo_of(case), "pr": 1, "outcomes": items, **over},
        headers=AGENT,
    )


async def outcomes(client, case: str, items: list[dict], expect: int | None = None,
                   **over) -> dict:
    """POST a batch and return the body.

    The status code is part of the contract — 201 created, 200 updated, 422 when
    nothing was accepted — so it is asserted rather than ignored: `expect` pins
    one, and the default accepts either success code, because most tests here are
    about the body and would otherwise re-encode the code in every call.
    """
    r = await post_outcomes(client, case, items, **over)
    if expect is not None:
        assert r.status_code == expect, r.text
    else:
        assert r.status_code in (200, 201), r.text
    return r.json()


async def chains(client, case: str) -> dict[str, dict]:
    r = await client.get(f"/review/findings?repo={repo_of(case)}&pr=1", headers=AGENT)
    assert r.status_code == 200, r.text
    return {c["key"]: c for c in r.json()["findings"]}


async def stats(client, case: str) -> dict:
    r = await client.get(f"/review/stats?repo={repo_of(case)}", headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


def row(s: dict, reviewer: str = "claude") -> dict:
    return next(m for m in s["by_model"] if m["reviewer"] == reviewer)


# ------------------------------------------------------- the record, and its guards

async def test_outcome_records_and_reads_back_on_the_chain(client):
    """The whole point in one pass: a confirmed finding, later refuted, and both
    statements survive side by side."""
    await record(client, "basic", to_fix=[finding("k1"), finding("k2")])
    res = await outcomes(client, "basic", [
        {"key": "k1", "outcome": "fixed"},
        {"key": "k2", "outcome": "refuted",
         "note": "install -m 0755 bin/* globs; the new script IS installed",
         "attested_by": "rich"},
    ])
    assert res["recorded"] == ["k1", "k2"]
    assert res["rejected"] == []
    assert res["unattested_refutations"] == []

    c = await chains(client, "basic")
    # The judge said confirmed and the fixer said refuted, and NEITHER is folded
    # into the other — the disagreement is the measurement.
    assert c["k2"]["observations"][0]["verdict"] == "confirmed"
    assert c["k2"]["outcome"]["outcome"] == "refuted"
    assert c["k2"]["outcome"]["note"].startswith("install -m 0755")
    assert c["k2"]["outcome"]["attested_by"] == "rich"
    assert c["k2"]["outcome"]["revisions"] == 0
    assert c["k2"]["outcome"]["prior_outcome"] is None
    # `set_by` comes from the token, never from the payload.
    assert c["k2"]["outcome"]["set_by"].startswith("laptop/")
    assert c["k1"]["outcome"]["outcome"] == "fixed"


async def test_status_and_outcome_are_independent(client):
    """``status`` is what the reviews support; ``outcome`` is what somebody found
    out. A chain can read ``gone`` — raised earlier, not raised in the latest run
    — while the finding was in fact refuted, and that pair is the case #77 exists
    for. Folding one into the other would hide exactly it."""
    await record(client, "status", to_fix=[finding("g1")])
    await record(client, "status", to_fix=[], round=2)
    await outcomes(client, "status", [
        {"key": "g1", "outcome": "refuted", "note": "the condition it assumed is false"}])
    c = await chains(client, "status")
    assert c["g1"]["status"] == "gone"
    assert c["g1"]["outcome"]["outcome"] == "refuted"


async def test_run_detail_carries_the_outcome_and_null_where_nobody_said(client):
    run = await record(client, "detail", to_fix=[finding("d1"), finding("d2")])
    await outcomes(client, "detail", [{"key": "d1", "outcome": "deferred",
                                       "deferred_to": "prisonblues/quarterback#132"}])
    r = await client.get(f"/review/{run['id']}", headers=AGENT)
    assert r.status_code == 200, r.text
    by_key = {f["key"]: f for f in r.json()["findings"]}
    assert by_key["d1"]["outcome"]["outcome"] == "deferred"
    assert by_key["d1"]["outcome"]["deferred_to"] == "prisonblues/quarterback#132"
    # Nobody has said, which is not an outcome of "nothing happened".
    assert by_key["d2"]["outcome"] is None


async def test_refuted_needs_its_reasoning(client):
    """The refutation IS the evidence. Recording ``refuted`` as a bare flag would
    put a confident contradiction of the judge into a published precision figure
    with nothing behind it — which is what the confirmed findings on PR #64 were."""
    await record(client, "note", to_fix=[finding("r1"), finding("r2")])
    res = await outcomes(client, "note", [
        {"key": "r1", "outcome": "refuted"},
        {"key": "r2", "outcome": "fixed"},
    ])
    assert res["recorded"] == ["r2"]
    assert [x["key"] for x in res["rejected"]] == ["r1"]
    assert "the evidence" in res["rejected"][0]["reason"]
    # One bad item does not cost the batch its good ones.
    c = await chains(client, "note")
    assert c["r1"]["outcome"] is None
    assert c["r2"]["outcome"]["outcome"] == "fixed"


async def test_repeating_a_refutation_does_not_require_retyping_it(client):
    """A loop that has the key and not the prose must be able to re-report, and
    the note it does not resend must survive: an omitted field on a repeat of the
    SAME answer is "nothing to add", never "clear what is there"."""
    await record(client, "repeat", to_fix=[finding("n1")])
    await outcomes(client, "repeat", [
        {"key": "n1", "outcome": "refuted", "note": "line 34 is the last help line"}])
    res = await outcomes(client, "repeat", [{"key": "n1", "outcome": "refuted"}])
    assert res["unchanged"] == ["n1"] and res["rejected"] == []
    c = await chains(client, "repeat")
    assert c["n1"]["outcome"]["note"] == "line 34 is the last help line"
    assert c["n1"]["outcome"]["revisions"] == 0


async def test_changing_the_answer_is_visible_and_drops_the_old_reasoning(client):
    """A terminal state that moves is legitimate; one that moves quietly is how an
    after-the-fact precision figure improves without anybody deciding to. And the
    old note explained the old answer, so it must not survive under the new one."""
    await record(client, "change", to_fix=[finding("m1")])
    await outcomes(client, "change", [{"key": "m1", "outcome": "deferred",
                                       "deferred_to": "prisonblues/quarterback#74"}])
    res = await outcomes(client, "change", [{"key": "m1", "outcome": "fixed"}])
    assert res["changed"] == [{"key": "m1", "from": "deferred", "to": "fixed"}]
    c = await chains(client, "change")
    assert c["m1"]["outcome"]["outcome"] == "fixed"
    assert c["m1"]["outcome"]["revisions"] == 1
    assert c["m1"]["outcome"]["prior_outcome"] == "deferred"
    assert c["m1"]["outcome"]["deferred_to"] is None
    assert c["m1"]["outcome"]["updated_at"] is not None


async def test_rejections_are_itemised_and_named(client):
    """Four ways to be wrong in one payload, each named, and the good item still
    lands: a fix pass reporting a round's findings must not lose eleven of them to
    one typo."""
    await record(client, "reject", to_fix=[finding("x1"), finding("x2"), finding("x3")])
    res = await outcomes(client, "reject", [
        {"key": "x1", "outcome": "regressed"},
        {"key": "ghost", "outcome": "fixed"},
        {"key": "x1", "outcome": "fixed"},
        {"key": "x2", "outcome": "fixed", "deferred_to": "acme/repo#1"},
        {"key": "x3", "outcome": "fixed"},
    ], expect=201)
    assert res["recorded"] == ["x3"]
    reasons = [r["reason"] for r in res["rejected"]]
    assert len(reasons) == 4
    assert any("unknown outcome" in r for r in reasons)
    assert any("no finding with this key" in r for r in reasons)
    assert any("once per request" in r for r in reasons)
    assert any("only meaningful on a deferred outcome" in r for r in reasons)


async def test_a_key_may_be_reported_once_per_request(client):
    """A payload that says two things about one defect is a caller contradicting
    itself, and picking either is a guess — so the later entry is refused whether
    or not the first was accepted. It used to be refused only when the first had
    been ACCEPTED, so a key whose first entry was rejected had its duplicates told
    "the first entry was kept" when nothing had been kept for it at all."""
    await record(client, "dupe", to_fix=[finding("d1")])
    res = await outcomes(client, "dupe", [
        {"key": "d1", "outcome": "regressed"},
        {"key": "d1", "outcome": "fixed"},
    ], expect=422)
    assert res["recorded"] == []
    assert "unknown outcome" in res["rejected"][0]["reason"]
    assert "once per request" in res["rejected"][1]["reason"]


async def test_the_status_code_agrees_with_the_body(client):
    """A shell pipeline built around `qb` — a curl wrapper — checks the status and
    nothing else, so "201 Created" over a body of twelve rejections is the
    response lying about what it did."""
    await record(client, "codes", to_fix=[finding("c1")])
    created = await post_outcomes(client, "codes", [{"key": "c1", "outcome": "fixed"}])
    assert created.status_code == 201
    updated = await post_outcomes(client, "codes", [{"key": "c1", "outcome": "fixed"}])
    assert updated.status_code == 200
    nothing = await post_outcomes(client, "codes", [{"key": "ghost", "outcome": "fixed"}])
    assert nothing.status_code == 422
    assert nothing.json()["rejected"][0]["reason"] == "no finding with this key on this PR"


async def test_superseded_needs_the_key_that_replaced_it(client):
    """The replacing key is the entire content of a `superseded` outcome, so it is
    required the way a note is required for `refuted` — a bare `superseded`
    records "replaced by something"."""
    await record(client, "supersede-bare", to_fix=[finding("b1"), finding("b2")])
    res = await outcomes(client, "supersede-bare", [
        {"key": "b1", "outcome": "superseded"}], expect=422)
    assert "superseded needs superseded_by" in res["rejected"][0]["reason"]


async def test_superseded_by_must_name_another_finding_on_this_pr(client):
    await record(client, "supersede", to_fix=[finding("s1"), finding("s2")])
    res = await outcomes(client, "supersede", [
        {"key": "s1", "outcome": "superseded", "superseded_by": "s1"},
        {"key": "s2", "outcome": "superseded", "superseded_by": "elsewhere"},
    ], expect=422)
    assert res["recorded"] == []
    assert any("itself" in r["reason"] for r in res["rejected"])
    assert any("names no finding on this PR" in r["reason"] for r in res["rejected"])

    ok = await outcomes(client, "supersede", [
        {"key": "s1", "outcome": "superseded", "superseded_by": "s2"}])
    assert ok["recorded"] == ["s1"]
    c = await chains(client, "supersede")
    assert c["s1"]["outcome"]["superseded_by"] == "s2"


async def test_unattested_refutations_are_named_back_not_refused(client):
    """#77's rule is that an agent must not grade its own findings unattended, and
    this API cannot tell a fixer from a reviewer. Refusing would leave the
    refutation in prose where nothing counts it; counting it silently would be the
    self-grading loop. So it is recorded, named back, and split in the stats."""
    await record(client, "attest", to_fix=[finding("u1"), finding("u2")])
    res = await outcomes(client, "attest", [
        {"key": "u1", "outcome": "refuted", "note": "not a defect: it globs"},
        {"key": "u2", "outcome": "refuted", "note": "checked the transcript",
         "attested_by": "rich"},
    ])
    assert res["recorded"] == ["u1", "u2"]
    assert res["unattested_refutations"] == ["u1"]

    # Re-reporting a signed-off refutation without resending the attestation must
    # not report it as unattested: the enrich rule keeps what is stored, so
    # reading only the payload would have the response contradict the row it just
    # wrote — and in the direction that cries wolf.
    again = await outcomes(client, "attest", [
        {"key": "u2", "outcome": "refuted"}])
    assert again["unattested_refutations"] == []
    c = await chains(client, "attest")
    assert c["u2"]["outcome"]["attested_by"] == "rich"

    s = await stats(client, "attest")
    assert s["by_outcome"]["refuted"] == 2
    assert s["by_outcome_attested"]["refuted"] == 1
    m = row(s)
    assert m["outcome"]["refuted"] == 2
    assert m["outcome_attested"]["refuted"] == 1


# ------------------------------------------------------------------- the statistic

async def test_precision_after_is_fixed_over_fixed_plus_refuted(client):
    """The number the release exists to publish, beside the judgement-time one —
    and ``deferred``/``superseded`` are decisions about what to do next, so they
    stay out of the ratio or "we never got to it" reads as "it was real"."""
    await record(client, "ratio", to_fix=[finding(k) for k in ("p1", "p2", "p3", "p4")])
    await outcomes(client, "ratio", [
        {"key": "p1", "outcome": "fixed"},
        {"key": "p2", "outcome": "fixed"},
        {"key": "p3", "outcome": "refuted", "note": "the condition it assumed is false"},
        {"key": "p4", "outcome": "deferred"},
    ])
    m = row(await stats(client, "ratio"))
    assert m["outcome"] == {"fixed": 2, "refuted": 1, "deferred": 1, "superseded": 0}
    assert m["precision_after"] == round(2 / 3, 3)
    # Judgement-time precision is untouched: the two are published together
    # because the GAP between them is the measurement.
    assert m["precision"] == 1.0


async def test_an_outcome_is_counted_once_however_many_rounds_raised_it(client):
    """The grain that makes the number honest. A defect raised in three rounds is
    three observations and one thing that happened to it; counting per observation
    would weight one refutation by how long the fix loop ran — heaviest on exactly
    the PRs a reviewer's reliability is the question for."""
    for rnd in (1, 2, 3):
        await record(client, "grain", to_fix=[finding("rep")], round=rnd)
    await outcomes(client, "grain", [
        {"key": "rep", "outcome": "refuted",
         "note": "three rounds, one defect, one refutation"}])
    m = row(await stats(client, "grain"))
    assert m["confirmed"] == 3           # observations
    assert m["outcome"]["refuted"] == 1  # defects
    assert m["confirmed_defects"] == 1
    assert m["outcomes_recorded"] == 1
    assert m["precision_after"] == 0.0


async def test_coverage_is_published_because_nobody_has_to_record_an_outcome(client):
    """Counters that read zero mean "unrecorded" far more often than they mean
    "nothing happened" — the same trap ``provenance_runs`` exists for. So the
    denominator ships beside the counts, and a member nobody has scored gets
    ``precision_after: null`` rather than a flattering 0 or 1."""
    await record(client, "coverage",
                 to_fix=[finding("c1"), finding("c2"),
                         finding("c3", reviewers=["codex"])],
                 reviewers_selected=["claude", "codex"],
                 reviewers={"claude": {"model": "opus", "ran": True},
                            "codex": {"model": "gpt-5", "ran": True}})
    await outcomes(client, "coverage", [{"key": "c1", "outcome": "fixed"}])
    s = await stats(client, "coverage")
    m = row(s)
    assert m["outcomes_recorded"] == 1
    assert m["confirmed_defects"] == 2
    assert m["outcome"]["refuted"] == 0
    # A member nobody has scored at all: zeros, and no ratio invented from them.
    other = row(s, "codex")
    assert other["outcomes_recorded"] == 0
    assert other["confirmed_defects"] == 1
    assert other["precision_after"] is None


async def test_window_counts_a_defect_once_however_many_seats_raised_it(client):
    """``by_outcome`` is per defect across the window; summing ``by_model`` would
    double-count every finding two seats agreed on — the reason ``by_provenance``
    is computed the same way."""
    await record(
        client, "window",
        to_fix=[finding("shared", reviewers=["claude", "codex"]), finding("solo")],
        reviewers_selected=["claude", "codex"],
        reviewers={"claude": {"model": "opus", "ran": True},
                   "codex": {"model": "gpt-5", "ran": True}},
    )
    await outcomes(client, "window", [
        {"key": "shared", "outcome": "fixed"},
        {"key": "solo", "outcome": "fixed"},
    ])
    s = await stats(client, "window")
    assert row(s)["outcome"]["fixed"] == 2
    assert row(s, "codex")["outcome"]["fixed"] == 1
    # Three member-level fixes, two defects.
    assert s["by_outcome"]["fixed"] == 2
    assert s["by_outcome"]["not_recorded"] == 0


async def test_confirmed_defects_nobody_ruled_on_are_reported_not_omitted(client):
    """``not_recorded`` under its own name, for the same reason
    ``by_provenance.not_attributed`` is: the four buckets are a small part of the
    window until fix passes start recording, and a page shown only them would
    present a handful as the whole picture."""
    await record(client, "unruled", to_fix=[finding("q1"), finding("q2")])
    s = await stats(client, "unruled")
    assert s["by_outcome"]["not_recorded"] == 2
    assert s["by_outcome"]["fixed"] == 0
    assert row(s)["precision_after"] is None


async def test_dismissed_findings_are_outside_the_measure(client):
    """The population is confirmed findings, like every other quality figure on
    that page: a dismissed finding was not a defect, so what happened to it says
    nothing about the reviewer that raised it. Stored all the same — the record is
    wider than the statistic, deliberately."""
    await record(client, "dismissed", to_fix=[finding("keep")],
                 dismissed=[finding("drop")])
    res = await outcomes(client, "dismissed", [
        {"key": "keep", "outcome": "fixed"},
        {"key": "drop", "outcome": "refuted", "note": "the judge already threw it out"},
    ])
    assert res["recorded"] == ["keep", "drop"]
    c = await chains(client, "dismissed")
    assert c["drop"]["outcome"]["outcome"] == "refuted"

    s = await stats(client, "dismissed")
    assert row(s)["outcome"] == {"fixed": 1, "refuted": 0, "deferred": 0, "superseded": 0}
    assert s["by_outcome"]["refuted"] == 0
    assert row(s)["confirmed_defects"] == 1


async def test_a_key_that_names_no_finding_is_refused_and_an_empty_batch_is_a_422(client):
    await record(client, "empty", to_fix=[finding("e1")])
    res = await outcomes(client, "empty", [{"key": "ghost", "outcome": "fixed"}], expect=422)
    assert res["recorded"] == []
    assert res["rejected"][0]["reason"] == "no finding with this key on this PR"

    r = await client.post("/review/outcomes",
                          json={"repo": repo_of("empty"), "pr": 1, "outcomes": []},
                          headers=AGENT)
    assert r.status_code == 422


async def test_two_writers_recording_one_defect_at_once_leave_one_row(client):
    """v2.31 shipped a race-based feature with a sequential suite and its own panel
    found eight P1s in it. The race here is not exotic: the commonest second writer
    is the SAME agent retrying after its client timed out on a request the board
    had already accepted."""
    await record(client, "race", to_fix=[finding("w1")])
    both = await asyncio.gather(
        client.post("/review/outcomes",
                    json={"repo": repo_of("race"), "pr": 1,
                          "outcomes": [{"key": "w1", "outcome": "fixed"}]},
                    headers=AGENT),
        client.post("/review/outcomes",
                    json={"repo": repo_of("race"), "pr": 1,
                          "outcomes": [{"key": "w1", "outcome": "fixed"}]},
                    headers=AGENT),
    )
    # 201 for whichever inserted, 200 for whichever found the row already there —
    # in either order, since that is what "concurrent" means.
    assert sorted(r.status_code for r in both) == [200, 201], [r.text for r in both]
    # One defect, one outcome — whichever way the two interleaved.
    c = await chains(client, "race")
    assert c["w1"]["outcome"]["outcome"] == "fixed"
    assert c["w1"]["outcome"]["revisions"] == 0
    assert row(await stats(client, "race"))["outcome"]["fixed"] == 1


def unique_violation() -> IntegrityError:
    """What asyncpg raises when two writers insert the same key — SQLSTATE and
    all, because the SQLSTATE is what the handler now discriminates on."""
    orig = Exception("duplicate key value violates unique constraint")
    orig.sqlstate = "23505"
    return IntegrityError("insert", {}, orig)


async def test_a_lost_insert_race_is_retried_once_and_then_reported(client, monkeypatch):
    """The retry is what makes the case above ordinary rather than a 500, so it is
    pinned directly: one failure is absorbed, two are reported as contention
    instead of being retried forever — a request that keeps retrying itself hides
    whatever it is actually hitting."""
    real = reviews._apply_outcomes
    calls = {"n": 0}

    async def flaky(session, body, author):
        calls["n"] += 1
        if calls["n"] == 1:
            raise unique_violation()
        return await real(session, body, author)

    await record(client, "retry", to_fix=[finding("t1")])
    monkeypatch.setattr(reviews, "_apply_outcomes", flaky)
    res = await outcomes(client, "retry", [{"key": "t1", "outcome": "fixed"}])
    assert res["recorded"] == ["t1"] and calls["n"] == 2

    async def always(session, body, author):
        raise unique_violation()

    monkeypatch.setattr(reviews, "_apply_outcomes", always)
    r = await client.post("/review/outcomes",
                          json={"repo": repo_of("retry"), "pr": 1,
                                "outcomes": [{"key": "t1", "outcome": "fixed"}]},
                          headers=AGENT)
    assert r.status_code == 409
    assert "in flight" in r.json()["detail"]


async def test_only_a_unique_violation_is_treated_as_contention(client, monkeypatch):
    """A CHECK or NOT NULL violation is deterministic: retrying builds the same
    invalid row, and reporting it as "another writer got there first; retry" sends
    the caller round a loop over a bug in this service while hiding it from the
    logs. Only 23505 is somebody else."""
    calls = {"n": 0}

    async def check_violation(session, body, author):
        calls["n"] += 1
        orig = Exception('violates check constraint "ck_review_finding_outcomes_vocabulary"')
        orig.sqlstate = "23514"
        raise IntegrityError("insert", {}, orig)

    await record(client, "sqlstate", to_fix=[finding("v1")])
    monkeypatch.setattr(reviews, "_apply_outcomes", check_violation)
    with pytest.raises(IntegrityError):
        await client.post("/review/outcomes",
                          json={"repo": repo_of("sqlstate"), "pr": 1,
                                "outcomes": [{"key": "v1", "outcome": "fixed"}]},
                          headers=AGENT)
    assert calls["n"] == 1, "a deterministic integrity error must not be retried"


async def test_a_rewritten_note_is_a_revision_and_is_named(client):
    """The hole under "a repeat only enriches": it also let a repeat REPLACE the
    stored note — the evidence for a refutation — while reporting `unchanged` and
    leaving `revisions` at zero, so the row afterwards was indistinguishable from
    one recorded that way. Filling an empty field is enrichment; overwriting a
    stored one is a change, and changes are visible here by construction."""
    await record(client, "amend", to_fix=[finding("a1")])
    await outcomes(client, "amend", [
        {"key": "a1", "outcome": "refuted", "note": "the condition it assumed is false"}])
    res = await outcomes(client, "amend", [
        {"key": "a1", "outcome": "refuted", "note": "actually: the glob covers it"}])
    assert res["unchanged"] == []
    assert res["amended"] == [{"key": "a1", "fields": ["note"], "outcome": "refuted"}]
    c = await chains(client, "amend")
    assert c["a1"]["outcome"]["note"] == "actually: the glob covers it"
    assert c["a1"]["outcome"]["revisions"] == 1
    # The answer did not move, so `prior_outcome` — which is about the ANSWER —
    # stays empty. `revisions` is what says the record was edited.
    assert c["a1"]["outcome"]["prior_outcome"] is None


async def test_an_attestation_can_be_retracted_and_cannot_be_added_silently(client):
    """Two halves of one rule. An explicit null CLEARS (a mistaken attestation is
    retractable without flipping the outcome twice to do it, which would fabricate
    two revisions); and adding an attestation to somebody else's unattended
    refutation is an amendment, named, not a quiet edit."""
    await record(client, "retract", to_fix=[finding("k1")])
    await outcomes(client, "retract", [
        {"key": "k1", "outcome": "refuted", "note": "not a defect"}])
    added = await outcomes(client, "retract", [
        {"key": "k1", "outcome": "refuted", "attested_by": "rich"}])
    assert added["amended"] == []          # it FILLED an empty field
    assert added["unchanged"] == ["k1"]
    assert added["unattested_refutations"] == []

    dropped = await outcomes(client, "retract", [
        {"key": "k1", "outcome": "refuted", "attested_by": None}])
    assert dropped["amended"] == [{"key": "k1", "fields": ["attested_by"],
                                   "outcome": "refuted"}]
    assert dropped["unattested_refutations"] == ["k1"]
    c = await chains(client, "retract")
    assert c["k1"]["outcome"]["attested_by"] is None
    assert c["k1"]["outcome"]["note"] == "not a defect"


async def test_refuted_cannot_have_its_note_cleared_out_from_under_it(client):
    """The clearing rule must not become a hole in the evidence rule: an explicit
    null note on a `refuted` row would otherwise pass the check on the strength of
    the note it is in the act of deleting."""
    await record(client, "clearnote", to_fix=[finding("n2")])
    await outcomes(client, "clearnote", [
        {"key": "n2", "outcome": "refuted", "note": "the transcript carries it"}])
    res = await outcomes(client, "clearnote", [
        {"key": "n2", "outcome": "refuted", "note": None}], expect=422)
    assert "refuted needs a note" in res["rejected"][0]["reason"]
    c = await chains(client, "clearnote")
    assert c["n2"]["outcome"]["note"] == "the transcript carries it"


async def test_a_no_op_repeat_steals_no_authorship_but_a_fill_records_its_author(client):
    """`set_by` names who is responsible for the row's CURRENT content. Two ways
    to get that wrong, and this pins both ends:

    * it used to be overwritten on every repeat, so the author of a refutation
      became whoever last re-reported it — and `session`, which is how a peer
      reaches that agent, was nulled by any repeat that omitted it;
    * but a repeat that FILLS an empty field is adding content, and leaving the
      author alone then filed a signoff claim under the name of the agent that did
      not make it — in the one field that exists to say who is claiming.
    """
    sid = "1111aaaa-2222-3333-4444-555555555555"
    await record(client, "author", to_fix=[finding("s1")])
    await outcomes(client, "author", [{"key": "s1", "outcome": "refuted", "note": "not real"}],
                   session=sid)
    before = (await chains(client, "author"))["s1"]["outcome"]
    assert before["session"] == sid

    # A genuine no-op: nothing moves, including the provenance pair.
    await outcomes(client, "author", [{"key": "s1", "outcome": "refuted"}])
    after = (await chains(client, "author"))["s1"]["outcome"]
    assert after["set_by"] == before["set_by"]
    assert after["session"] == sid

    # A fill IS a change, so it records who made it — and the session travels
    # with the identity rather than being updated on its own.
    await outcomes(client, "author", [{"key": "s1", "outcome": "refuted",
                                       "attested_by": "rich"}], session="2222bbbb")
    filled = (await chains(client, "author"))["s1"]["outcome"]
    assert filled["attested_by"] == "rich"
    assert filled["session"] == "2222bbbb"


async def test_an_over_long_value_is_refused_not_quietly_trimmed(client):
    """Silently slicing a note to the cap loses whichever sentence was last, which
    on a refutation is usually the conclusion — and tells the caller it recorded
    fine. This endpoint refuses and says which field."""
    await record(client, "bounds", to_fix=[finding("b1"), finding("b2")])
    res = await outcomes(client, "bounds", [
        {"key": "b1", "outcome": "refuted", "note": "x" * 4001},
        {"key": "b2", "outcome": "fixed"},
    ], expect=201)
    assert res["recorded"] == ["b2"]
    assert "note over 4000 characters" in res["rejected"][0]["reason"]
    c = await chains(client, "bounds")
    assert c["b1"]["outcome"] is None


async def test_a_misspelled_field_is_rejected_rather_than_dropped(client):
    """`extra="forbid"`, unlike the panel ingest: a dropped `attestedBy` silently
    downgrades a signed-off refutation to unattended in a published figure, and a
    rejection here costs one item rather than the batch."""
    await record(client, "typo", to_fix=[finding("t1"), finding("t2")])
    res = await outcomes(client, "typo", [
        {"key": "t1", "outcome": "refuted", "note": "n", "attestedBy": "rich"},
        {"key": "t2", "outcome": "fixed"},
    ], expect=201)
    assert res["recorded"] == ["t2"]
    assert "attestedBy" in res["rejected"][0]["reason"]


async def test_one_malformed_item_does_not_cost_the_batch(client):
    """The reason `outcomes` is a list of raw objects: FastAPI validates a typed
    list whole, so an item with no `key` would 422 the request and lose every
    valid sibling — the opposite of what this endpoint promises."""
    await record(client, "malformed", to_fix=[finding("m1")])
    res = await outcomes(client, "malformed", [
        {"outcome": "fixed"},
        "not an object",
        {"key": "   ", "outcome": "fixed"},
        {"key": "m1", "outcome": "fixed"},
    ], expect=201)
    assert res["recorded"] == ["m1"]
    reasons = [r["reason"] for r in res["rejected"]]
    assert len(reasons) == 3
    assert any("key" in r for r in reasons)
    assert any("not an object" in r for r in reasons)
    # A blank key says it was blank, rather than "no finding with this key",
    # which points the caller at keys that were never the problem.
    assert any("blank" in r.lower() for r in reasons)


async def test_the_cap_is_named_rather_than_422ing_the_batch(client):
    """A caller batching a long fix loop past the cap keeps the first
    MAX_OUTCOMES rows and is told what was dropped, rather than losing all 501."""
    await record(client, "cap", to_fix=[finding(f"c{i}") for i in range(3)])
    over = reviews.MAX_OUTCOMES + 1
    items = [{"key": "c0", "outcome": "fixed"}]
    items += [{"key": f"filler{i}", "outcome": "fixed"} for i in range(over - 1)]
    res = await outcomes(client, "cap", items, expect=201)
    assert res["recorded"] == ["c0"]
    assert any("over the" in r["reason"] and "cap" in r["reason"] for r in res["rejected"])


async def test_the_repo_is_taken_by_either_name_and_trimmed(client):
    """`github` is what the panel calls the slug, so it is accepted here as it is
    on the ingest path — and an untrimmed repo matched nothing, which came back as
    "no finding with this key", pointing the caller at keys that were fine."""
    await record(client, "alias", to_fix=[finding("a1")])
    r = await client.post("/review/outcomes",
                          json={"github": f"  {repo_of('alias')}  ", "pr": 1,
                                "outcomes": [{"key": "a1", "outcome": "fixed"}]},
                          headers=AGENT)
    assert r.status_code == 201, r.text
    assert r.json()["recorded"] == ["a1"]


async def test_an_outcome_does_not_leak_across_prs_of_one_repo(client):
    """`finding_key` identifies a defect WITHIN a PR, which is why the row is
    keyed on all three columns — the same key on PR 2 is a different defect."""
    await record(client, "prscope", to_fix=[finding("same")])
    r = await client.post("/review", json={
        "repo": repo_of("prscope"), "pr": 2, "judged": True, "judge_model": "opus",
        "reviewers_selected": ["claude"], "reviewers": {"claude": {"model": "opus", "ran": True}},
        "to_fix": [finding("same")], "dismissed": [], "sonar_findings": [],
    }, headers=AGENT)
    assert r.status_code == 201, r.text
    await outcomes(client, "prscope", [{"key": "same", "outcome": "fixed"}])

    pr2 = await client.get(f"/review/findings?repo={repo_of('prscope')}&pr=2", headers=AGENT)
    assert pr2.status_code == 200
    assert pr2.json()["findings"][0]["outcome"] is None


async def test_the_database_refuses_a_bare_refutation_too(client):
    """The evidence rule at the boundary, for writers that are not this API — a
    backfill, an admin script, the next write path. Both halves of the CHECK
    matter: a CHECK passes when its expression is NULL, so the trim test alone
    would let a null note through, which is the row it exists to refuse."""
    from sqlalchemy import text

    from app.db import engine

    # One transaction per attempt: a failed statement aborts its transaction, so
    # a loop inside one `begin()` fails the second case on the FIRST case's
    # rollback state rather than on the constraint.
    for note in ("NULL", "''", "'   '"):
        with pytest.raises(IntegrityError) as caught:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "INSERT INTO review_finding_outcomes "
                    "(repo, pr, finding_key, outcome, note, set_by) VALUES "
                    f"('acme/direct', 1, 'k', 'refuted', {note}, 'laptop/hand')"))
        assert "ck_review_finding_outcomes_refuted_note" in str(caught.value)


async def test_recording_requires_a_writer_token(client):
    """Same rule as every other write on this router: reading the board is one
    thing, writing a verdict about somebody's findings onto it is another."""
    r = await client.post("/review/outcomes",
                          json={"repo": repo_of("auth"), "pr": 1,
                                "outcomes": [{"key": "k1", "outcome": "fixed"}]})
    assert r.status_code == 401
