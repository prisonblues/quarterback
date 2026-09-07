"""#773: the refutation set — what a pull request has already disproved, read back.

The board has recorded a ``refuted`` outcome since v2.37 and never handed one back
to a round. So a seat that raised a false positive raised it again, the judge ruled
on it again, and the fixer either re-refuted it — which #616 measured as costing
MORE than complying — or complied with something that was wrong. The loop's
cheapest path was to comply with a finding nobody believed.

``GET /review/refutations`` is the read that ends that, and this file is its
contract. It is organised around the five ways of being wrong that read as working:

1. **The reason is the payload.** mergeCraft's fix-side brief is the rule —
   *"Record the reason, not the verdict"* — because a reusable reason is the only
   thing a later reviewer can check against the code, and a verdict is an appeal to
   authority. A reasonless refutation cannot be stored at all — the database
   refuses it, asserted by trying the write rather than by reading the DDL — and
   would not be published if it were.
2. **Two writers, kept apart.** The outcome table and the ledger both say
   ``refuted`` and they are different facts — ``(refuted, judge)`` against a fixer's
   terminal decision — so ``source`` distinguishes them and the outcome wins where
   both speak.
3. **A retraction really retracts.** An outcome amended away from ``refuted``, and
   a ledger promotion back to ``raised``, both take the row out of the set. The
   ledger half is the newest-entry-then-test ordering, and written the obvious way
   round (filter first, pick after) a promoted finding stays refuted for ever.
4. **The blast radius is one PR.** Both parameters are required, matched with
   ``==``, and no parameter widens it. A wrong entry silently suppresses a real
   finding later, which is why promoting one to repo-wide is not built here.
5. **Locality, published rather than inferred.** ``locatable`` is what the harness
   matches on (#771), and a refutation naming no line can be READ and can never
   BIND — so the row says which it is instead of leaving a consumer to guess from a
   null.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api.review_refutations import MAX_REFUTATIONS, _reason_of
from app.db import engine

from .conftest import LAPTOP

AGENT = {**LAPTOP, "X-Agent-Instance": "cc773a"}

#: The file the worked example lives in, from #616's own instance: a P2 about a
#: preview whose empty glossary was the intended behaviour.
PREVIEW = "app/legislation_routes.py"

#: The refutation that pays for the whole feature, in the form the brief asks for —
#: the general fact, not the verdict. `"the finding was wrong"` would be useless to
#: the next round; this can be checked with one grep.
WHY = ("html_preview is the free teaser — legislation_routes builds it inside "
       "`if not viewer_has_full_content_access():`, so an empty definitions panel "
       "is the intended behaviour")


def repo_of(case: str) -> str:
    return f"acme/refuted-{case}"


def finding(key: str, **over) -> dict:
    f = {"title": f"finding {key}", "severity": "P2", "file": PREVIEW,
         "line": 118, "reviewers": ["claude"], "key": key}
    return {**f, **over}


async def record(client, case: str, keys: list[str], *, pr: int = 1, **over) -> dict:
    body = {
        "repo": repo_of(case), "pr": pr,
        "judged": True, "judge_model": "opus",
        "reviewers_selected": ["claude"],
        "reviewers": {"claude": {"model": "opus", "ran": True}},
        "to_fix": [finding(k) for k in keys],
        "dismissed": [], "sonar_findings": [],
    }
    r = await client.post("/review", json={**body, **over}, headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


async def outcome(client, case: str, items: list[dict], *, pr: int = 1) -> dict:
    r = await client.post("/review/outcomes",
                          json={"repo": repo_of(case), "pr": pr, "outcomes": items},
                          headers=AGENT)
    assert r.status_code in (200, 201), r.text
    return r.json()


async def ledger(client, case: str, rnd: int, entries: list[dict], *, pr: int = 1) -> dict:
    r = await client.post(
        "/review/ledger",
        json={"repo": repo_of(case), "pr": pr, "round": rnd, "entries": entries},
        headers=AGENT)
    assert r.status_code in (200, 201), r.text
    return r.json()


async def refutations(client, case: str, *, pr: int = 1, **params) -> dict:
    r = await client.get("/review/refutations",
                         params={"repo": repo_of(case), "pr": pr, **params},
                         headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


def by_key(body: dict) -> dict[str, dict]:
    return {r["key"]: r for r in body["refutations"]}


# ---- 1. the reason, not the verdict ----------------------------------------


async def test_a_refuted_finding_comes_back_with_the_reason_and_the_place(client):
    """#616's instance, replayed. Round 1 confirms the preview P2, the fixer proves
    it wrong, and the next round can be told so — with the sentence that disproves
    it and the file and line it stood at, which is what the matcher needs."""
    await record(client, "case", ["preview-glossary"])
    await outcome(client, "case", [
        {"key": "preview-glossary", "outcome": "refuted", "note": WHY,
         "attested_by": "rich"}])

    body = await refutations(client, "case")
    assert body["considered"] == 1 and body["dropped"] == 0
    row = body["refutations"][0]
    assert row["key"] == "preview-glossary"
    assert row["reason"] == WHY
    assert row["source"] == "outcome"
    # The locality the harness matches on — from the finding's newest observation,
    # because the key is derived WITHOUT the line and says nothing about where it is.
    assert (row["file"], row["line"], row["severity"]) == (PREVIEW, 118, "P2")
    assert row["locatable"] is True
    # A claim, carried beside proof of who recorded it — never merged with it.
    assert row["attested_by"] == "rich" and row["set_by"]


async def test_a_reasonless_refutation_cannot_be_stored_and_would_not_be_shown(client):
    """Two guards, and it is worth saying which is which.

    The near one is the database: ``ck_review_finding_outcomes_refuted_note``
    refuses a ``refuted`` row with no note, so the reasonless refutation cannot be
    written even by going round the API — asserted by trying the write, on
    ``ck_review_findings_recurs_of_revisited``'s lesson one table over, because a
    constraint checked by reading its own DDL passes whatever it does.

    The far one is :func:`app.api.review_refutations._reason_of`, which withholds a
    row whose reason is empty or whitespace. It is unreachable through either door
    today and it stays, because what it prevents is the worst failure available: a
    bare "this was refuted" is binding text with no argument under it — exactly the
    *"docstring finding was wrong"* form the whole feature exists to refuse — and
    the harness suppresses on a row's PRESENCE, not on its prose, so a placeholder
    would bind as hard as an argument.
    """
    assert _reason_of("   ") == "" and _reason_of(None) == ""
    assert _reason_of("  make lint  only checks src/ ") == "make lint only checks src/"

    await record(client, "bare", ["k1", "k2"])
    await outcome(client, "bare", [{"key": "k1", "outcome": "refuted", "note": WHY}])
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO review_finding_outcomes "
                     "(repo, pr, finding_key, outcome, note, set_by) "
                     "VALUES (:repo, 1, 'k2', 'refuted', NULL, 'laptop/x')"),
                {"repo": repo_of("bare")})

    assert list(by_key(await refutations(client, "bare"))) == ["k1"]


async def test_the_contract_and_the_scope_ride_on_the_wire(client):
    """Two properties a consumer must implement and neither of which can be
    inferred from the rows: match by locality and not by wording (#771), and this
    binds one pull request and nothing else."""
    await record(client, "contract", ["k1"])
    body = await refutations(client, "contract")
    assert "this pull request only" in body["scope"]
    assert "LOCALITY" in body["contract"] and "never by wording" in body["contract"]


# ---- 2. two writers, kept apart --------------------------------------------


async def test_a_ledger_refutation_names_the_writer_that_made_it(client):
    """``(refuted, judge)`` and ``(refuted, human)`` are different facts, and
    :mod:`app.finding_lifecycle` exists so that they cannot be pooled. A consumer
    deciding how much weight to give a suppression reads this field."""
    await record(client, "ledger", ["dropped-by-judge"])
    await ledger(client, "ledger", 2, [
        {"key": "dropped-by-judge", "state": "refuted", "source": "judge",
         "reason": "the guard is unreachable: the caller validates one frame up"}])

    row = by_key(await refutations(client, "ledger"))["dropped-by-judge"]
    assert row["source"] == "ledger:judge"
    assert row["round"] == 2
    assert row["reason"].startswith("the guard is unreachable")


async def test_the_terminal_outcome_wins_where_both_writers_speak(client):
    """A ledger row is one round's observation; an outcome is the terminal record
    of what somebody found out by acting on the defect. Two rows for one key would
    be one defect refuted twice, and the merge has to prefer the settled one."""
    await record(client, "both", ["k1"])
    await ledger(client, "both", 2, [
        {"key": "k1", "state": "refuted", "source": "judge",
         "reason": "the round's own reading"}])
    await outcome(client, "both", [
        {"key": "k1", "outcome": "refuted", "note": WHY}])

    rows = (await refutations(client, "both"))["refutations"]
    assert len(rows) == 1, "one defect came back as two refutations"
    assert rows[0]["source"] == "outcome" and rows[0]["reason"] == WHY


async def test_a_judges_dismissal_is_not_a_refutation(client):
    """The temptation is real and it is declined. A ``dismissed`` verdict is a
    ruling made inside one round on that round's material, by a party that may not
    have been able to read the code — and nothing ever required it to state a
    REUSABLE reason. Binding it would carry a judge's context failure forward for
    the life of the PR with no sentence anybody could check it against."""
    await record(client, "dismissed", [],
                 dismissed=[finding("thrown-out", reason="not supported by the diff")])
    body = await refutations(client, "dismissed")
    assert body["refutations"] == [] and body["considered"] == 0


# ---- 3. a retraction really retracts ---------------------------------------


async def test_an_outcome_amended_away_from_refuted_leaves_the_set(client):
    """The retraction path, and it needs no machinery of its own: the row is
    selected on its CURRENT outcome, so a fixer who decides the refutation was
    wrong and records ``fixed`` instead stops suppressing anything."""
    await record(client, "amend", ["k1"])
    await outcome(client, "amend", [{"key": "k1", "outcome": "refuted", "note": WHY}])
    assert list(by_key(await refutations(client, "amend"))) == ["k1"]

    await outcome(client, "amend", [{"key": "k1", "outcome": "fixed"}])
    assert (await refutations(client, "amend"))["refutations"] == []


async def test_a_promoted_finding_stops_being_refuted(client):
    """The ordering test, and the reason the ledger read picks the newest entry
    BEFORE it tests the state.

    Written the obvious way round — ``WHERE state = 'refuted'`` and then the pick —
    a finding refuted in round 2 and returned to the board later would stay refuted
    for ever, and the ledger's own mechanism for "this is coming back" would be
    invisible to the one consumer that acts on it.
    """
    await record(client, "promote", ["k1"])
    await ledger(client, "promote", 2, [
        {"key": "k1", "state": "refuted", "source": "judge",
         "reason": "not reachable from any caller"}])
    assert list(by_key(await refutations(client, "promote"))) == ["k1"]

    await ledger(client, "promote", 3, [
        {"key": "k1", "state": "deferred", "source": "panel",
         "reason": "parked: the caller landed and it is reachable after all"}])
    assert (await refutations(client, "promote"))["refutations"] == []


# ---- 4. one pull request ---------------------------------------------------


async def test_a_refutation_binds_its_own_pr_and_no_other(client):
    """The safety valve, and it is an absent capability rather than a default
    somebody can flip: a wrong entry silently suppresses a real finding later, so
    nothing here widens the scope even when two PRs share a file and a key."""
    await record(client, "scope", ["shared-key"], pr=1)
    await record(client, "scope", ["shared-key"], pr=2)
    await outcome(client, "scope", [
        {"key": "shared-key", "outcome": "refuted", "note": WHY}], pr=1)

    assert list(by_key(await refutations(client, "scope", pr=1))) == ["shared-key"]
    assert (await refutations(client, "scope", pr=2))["refutations"] == []


async def test_the_cap_announces_itself(client):
    """A trimmed answer that does not say it was trimmed reads as the whole one —
    and here the whole one is a suppression set, so a silent trim would silently
    stop binding."""
    keys = [f"k{i}" for i in range(4)]
    await record(client, "cap", keys)
    await outcome(client, "cap",
                  [{"key": k, "outcome": "refuted", "note": WHY} for k in keys])

    body = await refutations(client, "cap", limit=2)
    assert len(body["refutations"]) == 2
    assert body["considered"] == 4 and body["dropped"] == 2


async def test_the_limit_is_far_above_any_prompts_share(client):
    """The two caps are different promises. `panel_core.REFUTED_MAX` bounds a
    reviewer's ATTENTION at 8; this bounds the set that also suppresses a re-raise,
    and cutting that at 8 would make what the loop remembers depend on how much a
    prompt could afford."""
    assert MAX_REFUTATIONS >= 100

    r = await client.get("/review/refutations",
                         params={"repo": repo_of("bound"), "pr": 1,
                                 "limit": MAX_REFUTATIONS + 1},
                         headers=AGENT)
    assert r.status_code == 422


# ---- 5. locatable, published rather than inferred --------------------------


async def test_a_refutation_with_no_line_can_be_read_but_never_matched(client):
    """`panel_locality.same_finding` refuses a finding that names no line, against
    everything, including another finding that names no line — on a 9,000-line file
    path equality would declare every unplaced finding to be every other one.

    So the row still carries its reason, which a reviewer can read, and says on its
    face that it cannot bind a matcher. The alternative — leaving a consumer to
    derive it from a null check — is the same rule computed twice, and they disagree
    the first time a line arrives as 0.
    """
    await record(client, "unplaced", [])
    await record(client, "unplaced", [],
                 to_fix=[finding("no-line", file="CLAUDE.md", line=None)])
    await outcome(client, "unplaced", [
        {"key": "no-line", "outcome": "refuted",
         "note": "the paragraph is generated from the rules file"}])

    row = by_key(await refutations(client, "unplaced"))["no-line"]
    assert row["file"] == "CLAUDE.md" and row["line"] is None
    assert row["locatable"] is False
    assert row["reason"].startswith("the paragraph is generated")


async def test_the_newest_observation_supplies_the_place(client):
    """A finding's line moves when the fix above it lands, and ``finding_key`` is
    derived deliberately WITHOUT the line so that the identity does not move with
    it. The set therefore reports where the defect was LAST seen, which is the
    position a matcher on the current round's findings has to meet."""
    await record(client, "moved", [])
    await record(client, "moved", [], to_fix=[finding("drifts", line=118)])
    await record(client, "moved", [], to_fix=[finding("drifts", line=141)])
    await outcome(client, "moved", [
        {"key": "drifts", "outcome": "refuted", "note": WHY}])

    assert by_key(await refutations(client, "moved"))["drifts"]["line"] == 141


async def test_rows_that_can_bind_come_first_and_the_order_is_stable(client):
    """A truncated read must lose the rows that could only ever have been read
    before it loses the ones that also bind a matcher — the cap is the one place a
    read can silently change what the loop enforces.

    Stability is asserted for its own reason: an operator diffing two rounds'
    prompts should see the findings move, not the ordering."""
    await record(client, "order", [])
    await record(client, "order", [], to_fix=[
        finding("placed", line=42),
        finding("unplaced-a", file="README.md", line=None),
        finding("unplaced-b", file="docs/x.md", line=None)])
    await outcome(client, "order", [
        {"key": k, "outcome": "refuted", "note": WHY}
        for k in ("unplaced-a", "placed", "unplaced-b")])

    first = [r["key"] for r in (await refutations(client, "order"))["refutations"]]
    assert first[0] == "placed"
    assert [r["key"] for r in (await refutations(client, "order"))["refutations"]] == first


@pytest.mark.parametrize("spelling", ["ACME/Refuted-Fold", "acme/refuted-fold"])
async def test_the_repo_spelling_is_folded_on_the_way_out(client, spelling):
    """`finding_key` is scoped by (repo, pr) and every read here compares the column
    with ``==``, so a caller's capital would answer "nothing has been refuted" — the
    flattering direction, and #326's exact defect one endpoint over."""
    await record(client, "fold", ["k1"])
    await outcome(client, "fold", [{"key": "k1", "outcome": "refuted", "note": WHY}])

    r = await client.get("/review/refutations",
                         params={"repo": spelling, "pr": 1}, headers=AGENT)
    assert r.status_code == 200, r.text
    assert [x["key"] for x in r.json()["refutations"]] == ["k1"]
