"""#626: the share of cycles that end in a confident dry round.

The convergence epic (#621) is judged on one number and nothing reported it.
#631 shipped the number — `round_stop.converged`, computed once in the panel
FROM `confident` so the two cannot disagree — and put it in the round's JSON
payload only. `qb record-review` POSTed it, `ReviewIn` is `extra="ignore"`, and
ingest dropped it: the same silent drop `app/api/reviews.py`'s own v2.26 note
records for `head_sha` and the provenance pair, in the same file.

These tests pin the two halves of the repair. **Storage**: `converged` survives
the round trip, keeps its three states apart, and cannot be claimed by a round
whose own stop record refuses it. **Aggregation**: `GET /review/convergence`
counts CYCLES and not rounds, keeps the undecided population out of the ratio
rather than in its denominator, and splits by repo and PR shape.

The case that decides the whole design is
`test_a_below_floor_policy_stop_is_confident_and_not_converged`: a stop under
the cleared floor is `stopped`, `stop_confident` AND `converged: false`, so
`converged` is strictly stronger than anything already stored and a board-side
derivation over `stopped + stop_confident + outstanding` would call it a clean
finish. That is why this is a column.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from app.api.reviews import _cycle_ending, _pr_kind, _pr_size
from app.db import engine

from .conftest import LAPTOP

AGENT = {**LAPTOP, "X-Agent-Instance": "c626c6"}


def payload(repo: str, pr: int, **over) -> dict:
    """A judged one-seat round with one confirmed finding, unless overridden."""
    body = {
        "repo": repo,
        "pr": pr,
        "pr_title": f"feat: thing {pr}",
        "base": "main",
        "changed_lines": 120,
        "judged": True,
        "judge_model": "opus",
        "reviewers_selected": ["claude"],
        "reviewers": {"claude": {"model": "sonnet", "ran": True}},
        "to_fix": [
            {"severity": "P2", "file": "app/x.py", "title": "off-by-one",
             "reviewers": ["claude"], "reason": "confirmed in diff"},
        ],
    }
    return {**body, **over}


def stop(*, stopped=True, confident=True, converged=True, reason="dry") -> dict:
    return {"stop": stopped, "reason": reason, "confident": confident,
            "converged": converged, "veto": []}


async def record(client, repo: str, pr: int, **over) -> dict:
    r = await client.post("/review", json=payload(repo, pr, **over), headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


async def convergence(client, repo: str) -> dict:
    r = await client.get(f"/review/convergence?repo={repo}", headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


# ---- the pure classifiers --------------------------------------------------

def test_a_pr_kind_is_read_off_a_conventional_subject():
    """Scope and the breaking marker are discarded; the kind is not."""
    assert _pr_kind("feat(panel): a round can end") == "feat"
    assert _pr_kind("refactor!: split the module") == "refactor"
    assert _pr_kind("FIX: shouting is still a fix") == "fix"


def test_no_title_and_an_unclassifiable_title_are_different_answers():
    """`unknown` is 'nobody said' and `other` is 'said, and not one of ours'.

    Folding the first into the second files rows nobody classified under a
    bucket that claims they were — the collapse this whole issue is about, one
    field over.
    """
    assert _pr_kind(None) == "unknown"
    assert _pr_kind("   ") == "unknown"
    assert _pr_kind("Bump actions/checkout from 4 to 5") == "other"
    assert _pr_kind("wibble: not a kind we know") == "other"


def test_a_size_band_is_never_guessed_from_a_missing_count():
    """A run with no `changed_lines` is `unknown`, not `xs` — the smallest band
    is where a convergence rate looks best, and every pre-column run is NULL."""
    assert _pr_size(None) == "unknown"
    assert _pr_size(0) == "xs"
    assert _pr_size(49) == "xs"
    assert _pr_size(50) == "s"
    assert _pr_size(600) == "l"
    assert _pr_size(20_000) == "xl"


def test_an_unanswered_round_is_unmeasured_and_never_a_failure():
    """The precedence, at the grain it is actually decided at."""
    assert _cycle_ending(True, True) == "converged"
    assert _cycle_ending(True, False) == "unconverged"
    # Went again: still running, or abandoned. Nothing here can tell those apart
    # and neither is an ending.
    assert _cycle_ending(False, False) == "open"
    # The panel never said. Every round recorded before the column, #631's own
    # included — they sent the field and ingest dropped it.
    assert _cycle_ending(True, None) == "unmeasured"
    assert _cycle_ending(None, None) == "unmeasured"


# ---- storage ---------------------------------------------------------------

async def test_a_clean_finish_survives_the_round_trip(client):
    """The whole of the drop this issue is about: the panel sends `converged`
    and every read path can see it afterwards."""
    repo = "acme/c626-roundtrip"
    run = await record(client, repo, 6001, cycle="cyc-1", round=1,
                       to_fix=[], round_stop=stop())

    detail = (await client.get(f"/review/{run['id']}", headers=AGENT)).json()
    assert detail["converged"] is True
    assert detail["stop_confident"] is True

    listed = (await client.get(f"/reviews?repo={repo}", headers=AGENT)).json()
    assert listed[0]["converged"] is True

    hist = (await client.get(f"/review/findings?repo={repo}&pr=6001",
                             headers=AGENT)).json()
    # Both grains: the cycle summary and the round's own row. The summary
    # reports the newest round of the newest cycle; the row is what a caller
    # whose summary came back unattributable has to fall back on.
    assert hist["converged"] is True
    assert hist["runs"][0]["converged"] is True


async def test_a_below_floor_policy_stop_is_confident_and_not_converged(client):
    """The case that makes this a column rather than a derivation (#165).

    The cycle stopped, the stop was earned, and real findings are outstanding
    that this repo's policy says are reported and not fixed here. Every field a
    board-side derivation could reach says clean finish; the panel says it is
    not one, and the panel is right.
    """
    repo = "acme/c626-belowfloor"
    run = await record(
        client, repo, 6002, cycle="cyc-1", round=2,
        round_stop=stop(converged=False,
                        reason="reported, not fixed here: 3 findings under the "
                               "P2 cleared floor"),
    )
    detail = (await client.get(f"/review/{run['id']}", headers=AGENT)).json()
    assert detail["stopped"] is True
    assert detail["stop_confident"] is True
    assert detail["converged"] is False

    agg = await convergence(client, repo)
    assert agg["overall"]["unconverged"] == 1
    assert agg["overall"]["converged"] == 0
    assert agg["overall"]["rate"] == 0.0


async def test_a_producer_that_never_says_stays_null(client):
    """A `round_stop` with no `converged` in it is NULL, not False.

    `confident` may default to False because a payload that never said must not
    buy a landing. Nothing gates on `converged`; its only reader is a rate, and
    defaulting it would put every round from a producer too old to send the
    field into the denominator as a failure to converge.
    """
    repo = "acme/c626-silent"
    run = await record(client, repo, 6003, cycle="cyc-1", to_fix=[],
                       round_stop={"stop": True, "reason": "dry",
                                   "confident": True, "veto": []})
    detail = (await client.get(f"/review/{run['id']}", headers=AGENT)).json()
    assert detail["stop_confident"] is True
    assert detail["converged"] is None

    agg = await convergence(client, repo)
    assert agg["overall"]["unmeasured"] == 1
    assert agg["overall"]["decided"] == 0
    # Not 0.0. A window with nothing decided has no convergence rate, and 0.0 is
    # the claim "this repo converges never".
    assert agg["overall"]["rate"] is None


async def test_a_flat_stop_reason_cannot_claim_a_clean_finish(client):
    """The flat path carries a reason and nothing else, so it says nothing here."""
    repo = "acme/c626-flat"
    run = await record(client, repo, 6004, cycle="cyc-1", to_fix=[],
                       stop_reason="dry")
    detail = (await client.get(f"/review/{run['id']}", headers=AGENT)).json()
    assert detail["stop_reason"] == "dry"
    assert detail["converged"] is None


async def test_converged_cannot_outrun_the_stop_it_was_built_from(client):
    """`converged: true` beside a round that never stopped is not believed.

    The panel builds `converged` from `confident`, which is built from `stop`,
    so this pair is not something the panel can emit. It is coerced rather than
    422'd — refusing the payload would lose the findings and the scorecards with
    it — and the drop is named in the response.
    """
    repo = "acme/c626-outrun"
    r = await client.post(
        "/review",
        json=payload(repo, 6005, cycle="cyc-1",
                     round_stop=stop(stopped=False, confident=False,
                                     converged=True, reason="go again")),
        headers=AGENT,
    )
    assert r.status_code == 201, r.text
    assert "converged_dropped" in r.json()

    detail = (await client.get(f"/review/{r.json()['id']}", headers=AGENT)).json()
    # False, not NULL: this payload's stopping rule DID answer, in a way its own
    # evidence refuses.
    assert detail["converged"] is False
    assert detail["stopped"] is False


async def test_an_unconfident_stop_cannot_claim_a_clean_finish(client):
    """The other half of the same pair: a vetoed stop is not a clean finish."""
    repo = "acme/c626-veto"
    r = await client.post(
        "/review",
        json=payload(repo, 6006, cycle="cyc-1", to_fix=[],
                     round_stop={"stop": True, "reason": "dry", "confident": False,
                                 "converged": True,
                                 "veto": ["codex saw 60,000 of 118,402 diff chars"]}),
        headers=AGENT,
    )
    assert r.status_code == 201, r.text
    assert "converged_dropped" in r.json()
    detail = (await client.get(f"/review/{r.json()['id']}", headers=AGENT)).json()
    assert detail["converged"] is False
    assert detail["stop_veto"]


async def test_a_run_that_reviewed_nothing_loses_both_claims(client):
    """`reviewed: false` takes the confidence, and the clean finish with it.

    The two validators run in declaration order for exactly this payload: the
    first revokes `confident`, and a `converged` check that ran before it would
    read a confidence about to be taken away and let the clean finish rest on it.
    """
    repo = "acme/c626-noreview"
    r = await client.post(
        "/review",
        json=payload(repo, 6007, cycle="cyc-1", to_fix=[], reviewed=False,
                     skip_reason="title matches skip pattern /^Merge /",
                     round_stop=stop()),
        headers=AGENT,
    )
    assert r.status_code == 201, r.text
    recorded = r.json()
    assert "stop_confidence_dropped" in recorded
    assert "converged_dropped" in recorded
    detail = (await client.get(f"/review/{r.json()['id']}", headers=AGENT)).json()
    assert detail["stop_confident"] is False
    assert detail["converged"] is False


async def test_the_boundary_refuses_a_clean_finish_the_stop_does_not_support(client):
    """The CHECK, exercised past the endpoint that coerces it away.

    The API is not the only writer. This is the constraint's whole purpose and
    the only way to reach it is to bypass ingest, which is what this does.
    """
    async with engine.begin() as conn:
        with pytest.raises(Exception) as exc:
            await conn.execute(text(
                "INSERT INTO review_runs (author, repo, pr, round, stopped, "
                "stop_confident, converged) VALUES "
                "('laptop/x', 'acme/c626-check', 1, 1, true, false, true)"
            ))
    assert "ck_review_runs_converged_implies_earned_stop" in str(exc.value)


async def test_the_boundary_allows_a_clean_finish_a_stop_does_support(client):
    """...and the constraint is not merely refusing everything."""
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO review_runs (author, repo, pr, round, stopped, "
            "stop_confident, converged) VALUES "
            "('laptop/x', 'acme/c626-check-ok', 1, 1, true, true, true)"
        ))


# ---- aggregation -----------------------------------------------------------

async def test_the_rate_is_over_cycles_and_not_over_rounds(client):
    """A four-round cycle that converges is ONE converged cycle.

    Counting rounds would weight a repo's convergence by how long its loops take
    — heaviest on exactly the pull requests convergence is the question about —
    and a cycle whose first three rounds went again would report 25%.
    """
    repo = "acme/c626-cycles"
    for rnd in (1, 2, 3):
        await record(client, repo, 6010, cycle="cyc-a", round=rnd,
                     round_stop=stop(stopped=False, confident=False,
                                     converged=False, reason="go again"))
    await record(client, repo, 6010, cycle="cyc-a", round=4, to_fix=[],
                 round_stop=stop())

    agg = await convergence(client, repo)
    assert agg["overall"]["cycles"] == 1
    assert agg["overall"]["converged"] == 1
    assert agg["overall"]["decided"] == 1
    assert agg["overall"]["rate"] == 1.0
    # ...and the round it converged AT, which is the successor to this issue's
    # original headline: a cap of 6 buys nothing if nothing ever converges at 5.
    # Keyed `final_round`: it is the terminal round's NUMBER, which is not the
    # count of rounds the cycle took wherever one went unrecorded.
    assert [r["final_round"] for r in agg["by_rounds"]] == [4]


async def test_two_cycles_on_one_pr_are_two_cycles(client):
    """A `--new-cycle` re-run is a second loop, and its ending is its own.

    Two agents can loop the same PR, and a positional rule credits one cycle's
    ending to the other. The cycle id is what makes this a join.
    """
    repo = "acme/c626-twocycles"
    await record(client, repo, 6011, cycle="cyc-a", round=1, to_fix=[],
                 round_stop=stop())
    await record(client, repo, 6011, cycle="cyc-b", round=1,
                 round_stop=stop(converged=False, reason="capped"))

    agg = await convergence(client, repo)
    assert agg["overall"]["cycles"] == 2
    assert agg["overall"]["converged"] == 1
    assert agg["overall"]["unconverged"] == 1
    assert agg["overall"]["rate"] == 0.5


async def test_a_cycle_still_going_is_not_a_failure_to_converge(client):
    """`open` is its own bucket and is outside the ratio.

    A cycle mid-flight has no ending. Counting it would report every in-flight
    loop as a failure while it is in flight, so a busy fleet would read as a
    diverging one.
    """
    repo = "acme/c626-open"
    await record(client, repo, 6012, cycle="cyc-a", round=1,
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="1 P2 outstanding"))
    await record(client, repo, 6013, cycle="cyc-b", round=1, to_fix=[],
                 round_stop=stop())

    agg = await convergence(client, repo)
    assert agg["overall"]["open"] == 1
    assert agg["overall"]["converged"] == 1
    assert agg["overall"]["decided"] == 1
    assert agg["overall"]["cycles"] == 2
    assert agg["overall"]["rate"] == 1.0


async def test_the_terminal_round_is_the_highest_round_not_the_latest_record(
        client):
    """Ordered on the panel's own round numbering, `ts` only as a tie-break.

    A re-review recorded late — a retry, a board that was down and caught up —
    has a later `ts` than the round after it, and taking the newest record would
    read that cycle's ending off its round 1.
    """
    repo = "acme/c626-order"
    await record(client, repo, 6014, cycle="cyc-a", round=2, to_fix=[],
                 round_stop=stop())
    # Recorded afterwards, and it is round 1: the cycle converged at round 2.
    await record(client, repo, 6014, cycle="cyc-a", round=1,
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="go again"))

    agg = await convergence(client, repo)
    assert agg["overall"]["converged"] == 1
    assert agg["overall"]["open"] == 0


async def test_runs_with_no_cycle_are_excluded_and_counted(client):
    """A one-shot read is not a loop that could converge — and saying so beats
    answering `cycles: 0`, which is what an empty board answers too."""
    repo = "acme/c626-nocycle"
    await record(client, repo, 6015, to_fix=[], round_stop=stop())

    agg = await convergence(client, repo)
    assert agg["overall"]["cycles"] == 0
    assert agg["window"]["runs"] == 1
    assert agg["window"]["runs_without_cycle"] == 1


async def test_a_title_skipped_merge_does_not_end_its_cycle(client):
    """A run that reviewed nothing inherits the cycle id and has no stop fields.

    Left in, it becomes the newest round of the cycle and supplies its ending
    from a stopping rule that never ran — turning a converged cycle into an
    `unmeasured` one because somebody later panelled a merge commit.
    """
    repo = "acme/c626-skipped"
    await record(client, repo, 6016, cycle="cyc-a", round=1, to_fix=[],
                 round_stop=stop())
    await record(client, repo, 6016, cycle="cyc-a", round=2, to_fix=[],
                 reviewed=False, skip_reason="title matches skip pattern /^Merge /")

    agg = await convergence(client, repo)
    assert agg["overall"]["converged"] == 1
    assert agg["overall"]["unmeasured"] == 0


async def test_cycles_split_by_pr_shape(client):
    """Size and kind, the two shape signals the board actually holds."""
    repo = "acme/c626-shape"
    await record(client, repo, 6020, cycle="c1", to_fix=[], changed_lines=30,
                 pr_title="fix: one line", round_stop=stop())
    await record(client, repo, 6021, cycle="c2", changed_lines=4000,
                 pr_title="refactor: move the world",
                 round_stop=stop(converged=False, reason="capped"))

    agg = await convergence(client, repo)
    assert {r["size"]: r["rate"] for r in agg["by_size"]} == {"xs": 1.0, "xl": 0.0}
    assert {r["kind"]: r["rate"] for r in agg["by_kind"]} == {"fix": 1.0,
                                                             "refactor": 0.0}
    assert {(r["size"], r["kind"]) for r in agg["by_shape"]} == {
        ("xs", "fix"), ("xl", "refactor")}
    # The band grid is published in BAND order, not alphabetically: `l, m, s,
    # unknown, xl, xs` is the wrong order for the one axis a reader scans down.
    assert [r["size"] for r in agg["by_size"]] == ["xs", "xl"]


async def test_repos_are_reported_apart(client):
    """The first split the issue asks for — an unfiltered window still separates
    them, because a fleet rate over several repos answers about none of them."""
    for repo, converged in (("acme/c626-repo-a", True), ("acme/c626-repo-b", False)):
        await record(client, repo, 6030, cycle="c1", to_fix=[],
                     round_stop=stop(converged=converged,
                                     reason="dry" if converged else "capped"))

    r = await client.get("/review/convergence", headers=AGENT)
    assert r.status_code == 200, r.text
    rows = {x["repo"]: x for x in r.json()["by_repo"]}
    assert rows["acme/c626-repo-a"]["rate"] == 1.0
    assert rows["acme/c626-repo-b"]["rate"] == 0.0


async def test_marginal_by_round_reports_its_own_coverage(client):
    """This issue's original headline, and the coverage marker it needs.

    `new_findings` is nullable and NULL is "the panel did not say", so the sum
    covers whatever fraction of the population was instrumented. Read against
    `rounds` it is low by however many rounds said nothing.
    """
    repo = "acme/c626-marginal"
    await record(client, repo, 6040, cycle="c1", round=1, new_findings=7,
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="go again"))
    await record(client, repo, 6040, cycle="c1", round=2, to_fix=[],
                 new_findings=0, round_stop=stop())
    # Same cycle's shape, from a producer that never reported the count.
    await record(client, repo, 6041, cycle="c2", round=1, to_fix=[],
                 round_stop=stop())

    rows = {r["round"]: r for r in (await convergence(client, repo))
            ["marginal_by_round"]}
    assert rows[1]["rounds"] == 2
    assert rows[1]["new_findings"] == 7
    assert rows[1]["new_findings_runs"] == 1
    assert rows[1]["converged"] == 1
    assert rows[2]["new_findings"] == 0
    assert rows[2]["converged"] == 1


async def test_the_window_is_echoed_back_as_it_was_applied(client):
    """A response that filtered on one spelling and reported another leaves a
    caller unable to tell which repository it was answered about (#326)."""
    await record(client, "acme/c626-echo", 6050, cycle="c1", to_fix=[],
                 round_stop=stop())
    r = await client.get("/review/convergence?repo=ACME/C626-Echo", headers=AGENT)
    assert r.status_code == 200, r.text
    assert r.json()["window"]["repo"] == "acme/c626-echo"
    assert r.json()["overall"]["converged"] == 1


def test_the_page_drops_the_tile_rather_than_rendering_an_unmeasured_zero():
    """The page's half of the same rule, and the one that is easiest to undo.

    `rate` is null wherever nothing was decided, and JavaScript renders null as
    "0%" the moment a `== null` guard becomes a truthiness test. That would put
    "0% of cycles end cleanly" at the top of the panel page on every window made
    of rounds recorded before the column existed — which is every window today.

    There is no JS runner here, so this greps the file that ships. Crude, and it
    is the only thing standing between a re-edit and the most flattering possible
    rendering of "not measured" appearing as its opposite.
    """
    page = (Path(__file__).resolve().parents[1] / "app/static/reviews.html").read_text()
    assert "c.overall.rate == null" in page, "the tile must test the rate for null"
    # And the whole view must survive a board too old to serve the endpoint: the
    # two `ok` guards above it cover /review/stats and /reviews and would let a
    # 404 here throw on `.json()`.
    assert "convRes.ok ?" in page, "an absent endpoint must not fail the page"


async def test_the_endpoint_is_behind_the_read_token(client):
    """A read like every other on this router — the population it summarises is
    which pull requests this fleet cannot finish reviewing."""
    assert (await client.get("/review/convergence")).status_code == 401


# ---- what the review of #641 turned up -------------------------------------

def test_a_degenerate_title_is_classified_without_backtracking():
    """`_PR_KIND_RE` has three `\\s*` in a row and an optional group between two
    of them, so *m* spaces split across the stars at each of *n* give-backs from
    `[a-z]+` is cubic. `pr_title` is `Text` with no length cap at the model or at
    `ReviewIn`, so one write token can store a title that makes this run for
    seconds — durably, and on the event loop.

    Asserted on behaviour rather than on the pattern: the answer is still
    `other`, and it arrives at once. The bound is generous because a wall clock
    on a loaded box is not a benchmark; the pre-fix number was 12.5s for the same
    input and no plausible machine noise reaches it.
    """
    import time
    for size in (2_000, 4_000):
        degenerate = "a" * size + " " * size
        began = time.monotonic()
        assert _pr_kind(degenerate) == "other"
        assert time.monotonic() - began < 1.0, f"backtracked on {size} chars"


def test_the_bound_on_the_title_does_not_cost_a_title_that_classifies():
    """The cheap fix is a stricter regex, and it loses these. The cap is on the
    INPUT for that reason: every subject that classified before still does."""
    assert _pr_kind("feat (x) ! : spaced") == "feat"
    assert _pr_kind("  fix(api)!: leading space") == "fix"
    # ...and a kind pushed past the scan window is `other`, not a wrong kind.
    assert _pr_kind(" " * 200 + "feat: too far in") == "other"


async def test_the_endpoint_stays_fast_with_a_degenerate_title_stored(client):
    """End to end, because the regex runs SYNCHRONOUSLY inside an async endpoint:
    a slow classifier does not block one request, it blocks the worker. The panel
    page fires this on every load, with no filters."""
    import time
    repo = "acme/c626-redos"
    await record(client, repo, 6060, cycle="c1", to_fix=[],
                 pr_title="a" * 4_000 + " " * 4_000, round_stop=stop())
    began = time.monotonic()
    agg = await convergence(client, repo)
    assert time.monotonic() - began < 5.0, "the endpoint backtracked on a title"
    assert {r["kind"] for r in agg["by_kind"]} == {"other"}


def test_a_changed_line_count_that_cannot_be_one_is_unknown():
    """`POST /review` accepts `changed_lines: -5` — nothing bounds the field — and
    a negative count fell through the band loop keeping its initialised value,
    landing in `xs`. That is the band where a convergence rate looks best, and
    the same argument the NULL case already makes applies whole: nobody credibly
    said how big this was."""
    assert _pr_size(-1) == "unknown"
    assert _pr_size(-5000) == "unknown"
    assert _pr_size(0) == "xs"


async def test_a_cycle_the_budget_ended_is_counted_and_not_filed_open(client):
    """The bias that matters, because #637 fits a threshold to this number.

    A caps refusal reviews nothing and records `stop: True, confident: False,
    converged: False`. It used to be excluded by `reviewed IS NOT FALSE`, so the
    cycle's terminal round was the last round that said "go again" and the whole
    cycle was `open` — filed as maybe-still-running, outside the denominator, in
    the direction that flatters. It is an ending, and it is `unconverged`, which
    is what `preland` and the review queue have always called it.
    """
    repo = "acme/c626-budget"
    await record(client, repo, 6061, cycle="c1", round=1,
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="go again"))
    await record(client, repo, 6061, cycle="c1", round=2, to_fix=[],
                 reviewed=False,
                 skip_reason="refused: 1,200,000 of 1,000,000 tokens",
                 round_stop={"stop": True, "confident": False, "converged": False,
                             "reason": "repo spend ceiling reached",
                             "veto": ["1,200,000 of 1,000,000 tokens"]})

    agg = await convergence(client, repo)
    assert agg["overall"]["unconverged"] == 1
    assert agg["overall"]["open"] == 0
    assert agg["overall"]["decided"] == 1
    assert agg["overall"]["rate"] == 0.0


async def test_a_title_skipped_merge_is_still_not_the_cycles_ending(client):
    """The other half of the same widening, and the one it must not cost.

    A merge panelled under a title skip inherits the cycle id and reaches no
    stopping rule, so its `round_stop` is None and `converged` is NULL. The
    clause added for budget stops keys on a convergence verdict having been
    PUBLISHED, so this row stays excluded and the cycle keeps the ending its last
    real round gave it.
    """
    repo = "acme/c626-skipmerge"
    await record(client, repo, 6062, cycle="c1", round=1, to_fix=[],
                 round_stop=stop())
    await record(client, repo, 6062, cycle="c1", round=2, to_fix=[],
                 reviewed=False, skip_reason="title matches skip pattern /^Merge /")

    agg = await convergence(client, repo)
    assert agg["overall"]["converged"] == 1
    assert agg["overall"]["unmeasured"] == 0
    assert agg["overall"]["cycles"] == 1


async def test_a_converged_round_a_later_round_ran_past_is_not_a_marginal_win(
        client):
    """`marginal_by_round.converged` counted every round with `converged IS TRUE`,
    on the reasoning that a cycle does not continue past a clean finish.

    True of today's panel and enforced by nothing — #617 exists because rounds
    have run past a cycle's end. Counted on the TERMINAL round only, "how many
    cycles ended at round N having converged" is true by construction rather than
    by an invariant a producer can change without knowing.
    """
    repo = "acme/c626-pastend"
    await record(client, repo, 6063, cycle="c1", round=1, to_fix=[],
                 round_stop=stop())
    await record(client, repo, 6063, cycle="c1", round=2,
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="go again"))

    rows = {r["round"]: r for r in (await convergence(client, repo))
            ["marginal_by_round"]}
    # Round 1 converged and round 2 ran anyway. The cycle did not end converged,
    # and round 1 is not where it ended.
    assert rows[1]["converged"] == 0
    assert rows[1]["rounds"] == 1
    assert rows[2]["converged"] == 0
    # The coverage marker moves with its numerator: both are over terminal rounds.
    assert rows[1]["converged_runs"] == 0
    assert rows[2]["converged_runs"] == 1


async def test_the_window_is_bounded_by_default_and_says_so(client):
    """This endpoint pulls one row per CYCLE and classifies each in Python on the
    event loop — 1.32s unfiltered over 200k rows against 0.05s for
    `/review/stats`, growing linearly. The panel page calls it with no filters.

    So it defaults to a lookback where the other `/review/*` reads default to all
    time, and the boundary it applied is in `window.since`: a bounded answer says
    it is bounded.
    """
    repo = "acme/c626-window"
    await record(client, repo, 6064, cycle="c1", to_fix=[], round_stop=stop())

    r = await client.get(f"/review/convergence?repo={repo}", headers=AGENT)
    assert r.status_code == 200, r.text
    assert r.json()["window"]["since"] is not None, "an unbounded default scan"
    # Older than the default window: present on the board, outside the answer.
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO review_runs (author, repo, pr, round, cycle, ts, "
            "stopped, stop_confident, converged) VALUES "
            "('laptop/x', :repo, 9, 1, 'ancient', now() - interval '400 days', "
            "true, true, true)"), {"repo": repo})
    assert (await convergence(client, repo))["overall"]["cycles"] == 1
    wide = await client.get(f"/review/convergence?repo={repo}&days=3650",
                            headers=AGENT)
    assert wide.json()["overall"]["cycles"] == 2, "days= no longer widens it"


def test_the_page_says_which_window_the_tile_was_answered_over():
    """The page's "all time" is not this endpoint's, and the tile has to say so.

    `/review/stats` defaults to the whole table and this one defaults to a
    lookback, so a reader who picks "all time" gets a header reading "since
    <first run>" over a tile counted across a narrower window. No JS runner here
    either, so this greps the file that ships — the same crude guard the null-rate
    rule gets, for the same reason.
    """
    page = (Path(__file__).resolve().parents[1] / "app/static/reviews.html").read_text()
    assert 'window from ' in page, "the tile must name the window it was bounded to"


# ---- #637: the population `escalate_on.fix_injection` is fitted to -----------


async def injection(client, repo: str) -> dict:
    return {r["round"]: r for r in (await convergence(client, repo))
            ["injection_by_round"]}


async def test_injection_by_round_publishes_the_rate_the_rule_actually_reads(
        client):
    """#637 needed the per-ROUND attribution rate and the board published sums.

    `/review/stats` adds the four `provenance_counts` buckets across a window per
    reviewer, and a ratio of sums is not the distribution of per-round ratios: the
    pooled figure weights a 44-finding round like a 4-finding one and
    `escalate_on.fix_injection` weights them alike too — one round each, one
    verdict each. So a recalibration had to pull `/reviews` and divide by hand,
    which is why the instruction to re-measure stood for as long as it did.

    Two rounds, deliberately lopsided: 1/9 over a big round and 3/3 over a small
    one. The pooled rate is 4/12 and the median of the two per-round rates is
    0.5555, and the two answers point opposite ways about a cut at 0.5.
    """
    repo = "acme/c637-grain"
    await record(client, repo, 6370, cycle="c1", round=1, to_fix=[],
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="go again"))
    await record(client, repo, 6370, cycle="c1", round=2,
                 provenance_counts={"introduced": 1, "missed": 8},
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="go again"))
    await record(client, repo, 6370, cycle="c1", round=3,
                 provenance_counts={"introduced": 3},
                 round_stop=stop(converged=False, confident=False,
                                 reason="a stop, not convergence"))

    rows = await injection(client, repo)
    assert rows[2]["rated_runs"] == 1
    assert rows[2]["rate_median"] == pytest.approx(1 / 9, abs=1e-4)
    assert rows[3]["rate_median"] == 1.0
    # The pooled ratio is published beside the distribution and is not it.
    assert rows[2]["introduced"] == 1 and rows[2]["new"] == 9
    assert rows[2]["pooled_rate"] == pytest.approx(1 / 9, abs=1e-4)


async def test_the_distribution_is_over_rounds_and_not_over_findings(client):
    """Two rounds numbered 2, one big and clean, one small and all-injected.

    Pooled they are 3 of 23 — 13%, comfortably under any threshold anyone has
    proposed. Per round they are 0.0 and 1.0, and one of those two rounds is a
    cycle the rule ends. The quantiles have to see both.
    """
    repo = "acme/c637-spread"
    for pr, counts in ((6371, {"introduced": 0, "missed": 20}),
                       (6372, {"introduced": 3})):
        await record(client, repo, pr, cycle=f"c{pr}", round=1, to_fix=[],
                     round_stop=stop(stopped=False, confident=False,
                                     converged=False, reason="go again"))
        await record(client, repo, pr, cycle=f"c{pr}", round=2,
                     provenance_counts=counts,
                     round_stop=stop(converged=False, confident=False,
                                     reason="a stop, not convergence"))

    row = (await injection(client, repo))[2]
    assert row["pooled_rate"] == pytest.approx(3 / 23, abs=1e-4)
    assert row["rate_min"] == 0.0
    assert row["rate_max"] == 1.0
    assert row["rate_median"] == 0.5, "the median of 0.0 and 1.0"
    assert row["rate_p25"] == 0.25 and row["rate_p75"] == 0.75


async def test_a_round_that_was_never_asked_to_attribute_is_not_a_rate_of_zero(
        client):
    """`{}` is "the question did not arise" and NULL is "nobody said".

    Neither is `0.0`, which is a claim about a fix pass, and a round 1 has no fix
    pass in front of it. Counted in `rounds`, kept out of `attributed_runs` and
    out of every rate — `percentile_cont` ignores the NULLs the divisor produces,
    which is what stops "attribution did not arise" reading as "the fix pass
    introduced nothing" and dragging a median down to it.
    """
    repo = "acme/c637-notasked"
    # Round 1: no tally at all. Round 2: `{}`, which the panel sends where the
    # fix range could not be read. Round 3: a real rate of 1.0.
    await record(client, repo, 6373, cycle="c1", round=1, to_fix=[],
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="go again"))
    await record(client, repo, 6373, cycle="c1", round=2, provenance_counts={},
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="go again"))
    await record(client, repo, 6373, cycle="c1", round=3,
                 provenance_counts={"introduced": 5},
                 round_stop=stop(converged=False, confident=False,
                                 reason="a stop, not convergence"))

    rows = await injection(client, repo)
    assert rows[1]["rounds"] == 1
    assert rows[1]["attributed_runs"] == 0 and rows[1]["rated_runs"] == 0
    assert rows[1]["rate_median"] is None, "an unmeasured round is not a 0.0"
    assert rows[1]["pooled_rate"] is None and rows[1]["introduced"] is None
    assert rows[2]["attributed_runs"] == 0, "`{}` is not attribution running"
    assert rows[2]["rate_median"] is None
    assert rows[3]["rated_runs"] == 1 and rows[3]["rate_median"] == 1.0


async def test_a_tally_that_ran_and_summed_to_zero_is_covered_but_unrated(
        client):
    """All-zero is not `{}`: attribution ran and placed nothing.

    So it counts as coverage and still has no rate — a denominator of zero is not
    a rate of zero — and the two markers are published apart for exactly this row.
    """
    repo = "acme/c637-zero"
    await record(client, repo, 6374, cycle="c1", round=1, to_fix=[],
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="go again"))
    await record(client, repo, 6374, cycle="c1", round=2,
                 provenance_counts={"introduced": 0, "missed": 0,
                                    "missed-unread": 0, "unknown": 0},
                 round_stop=stop(converged=False, confident=False,
                                 reason="a stop, not convergence"))

    row = (await injection(client, repo))[2]
    assert row["attributed_runs"] == 1, "the panel did attribute"
    assert row["rated_runs"] == 0, "over nothing"
    assert row["rate_median"] is None and row["pooled_rate"] is None


async def test_the_unattributable_buckets_depress_the_rate(client):
    """`unknown` and `missed-unread` sit in the denominator, on the panel's rule.

    `panel_rounds.injection_state` puts them there because they push the rate
    DOWN, and a round the harness could not place must not be the round that ends
    a cycle. A board that divided by `introduced + missed` alone would publish
    1.0 for this round and the panel would compute 0.25 for the same one.
    """
    repo = "acme/c637-buckets"
    await record(client, repo, 6375, cycle="c1", round=1, to_fix=[],
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="go again"))
    await record(client, repo, 6375, cycle="c1", round=2,
                 provenance_counts={"introduced": 1, "missed": 0,
                                    "missed-unread": 2, "unknown": 1},
                 round_stop=stop(converged=False, confident=False,
                                 reason="a stop, not convergence"))

    row = (await injection(client, repo))[2]
    assert row["new"] == 4, "all four buckets, not the two that name a verdict"
    assert row["rate_median"] == 0.25


async def test_the_rounds_a_raised_cap_buys_are_in_the_distribution(client):
    """Off the table and not off `ranked`, and this is the row that decides it.

    `escalate_on.fix_injection` fires on any round from 2 onward. Measured over
    each cycle's TERMINAL round only, the mid-cycle rounds vanish — and those are
    precisely the ones `max_rounds: 6` buys and the ones a recalibration for a cap
    of 6 is about. This cycle ends at round 4; rounds 2 and 3 are the evidence.
    """
    repo = "acme/c637-midcycle"
    await record(client, repo, 6376, cycle="c1", round=1, to_fix=[],
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="go again"))
    for rnd, counts in ((2, {"introduced": 4, "missed": 4}),
                        (3, {"introduced": 6, "missed": 2})):
        await record(client, repo, 6376, cycle="c1", round=rnd,
                     provenance_counts=counts,
                     round_stop=stop(stopped=False, confident=False,
                                     converged=False, reason="go again"))
    await record(client, repo, 6376, cycle="c1", round=4,
                 provenance_counts={"introduced": 1, "missed": 7},
                 round_stop=stop(converged=False, confident=False,
                                 reason="a stop, not convergence"))

    rows = await injection(client, repo)
    assert rows[2]["rate_median"] == 0.5
    assert rows[3]["rate_median"] == 0.75
    assert rows[4]["rate_median"] == 0.125


async def test_a_round_outside_any_cycle_is_not_in_the_population(client):
    """A one-shot `/panel` read has no fix pass before it and no cycle around it.

    `marginal_by_round` is restricted to `cycle IS NOT NULL` and this is over the
    same population, so the two are comparable row for row. A standalone read
    pooled in would report an attribution rate for a round that could not have one.
    """
    repo = "acme/c637-nocycle"
    await record(client, repo, 6377, round=2,
                 provenance_counts={"introduced": 9},
                 round_stop=stop(converged=False, confident=False,
                                 reason="a stop, not convergence"))

    assert (await injection(client, repo)) == {}


async def test_dial_runs_says_how_many_rounds_can_name_their_own_threshold(
        client):
    """The coverage marker #637 turned on, and it read 1 of 38 on 2026-09-02.

    `rules` is the only field on the row that records
    `escalate_on.fix_injection` (#305/#647) — `review_panel` is
    `panel_seats.Dials.as_dict()` and `escalate_on` is not in it. A threshold
    fitted across rounds that cannot say what they ran under is fitted across a
    denominator that moved underneath it, so the count is published beside the
    distribution.

    Presence only. Reading the dial OUT of here would make this board a second
    interpreter of a repo's dials, which `app/api/dials.py` refuses at length.
    """
    repo = "acme/c637-dials"
    await record(client, repo, 6378, cycle="c1", round=1, to_fix=[],
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="go again"))
    await record(client, repo, 6378, cycle="c1", round=2,
                 provenance_counts={"introduced": 3, "missed": 3},
                 round_stop=stop(converged=False, confident=False,
                                 reason="a stop, not convergence"))
    await record(client, repo, 6379, cycle="c2", round=1, to_fix=[],
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="go again"))
    await record(client, repo, 6379, cycle="c2", round=2,
                 provenance_counts={"introduced": 3, "missed": 3},
                 rules={"dials": {"review_panel.escalate_on.fix_injection": {
                     "layer": "defaults", "value": 0.5,
                     "source": "harness_rules.DEFAULTS"}}},
                 round_stop=stop(converged=False, confident=False,
                                 reason="a stop, not convergence"))

    row = (await injection(client, repo))[2]
    assert row["rated_runs"] == 2, "both rounds are in the distribution"
    assert row["dial_runs"] == 1, "only one of them can say what it ran under"


async def test_the_board_applies_no_threshold_and_no_minimum_denominator(client):
    """The line this endpoint does not cross, pinned rather than described.

    `escalate_on.fix_injection` is a repo dial and
    `panel_rounds.FIX_INJECTION_MIN_NEW` is a harness constant. A board that
    published `over: true` would be interpreting the first, and one that dropped
    this two-finding round would be applying the second — and either makes this
    file a second implementation of a rule it can then disagree with the panel
    about, which is what `m6bc45ff1` refuses for `converged`.

    A rate over two findings is not a rate; publishing it anyway is honest, and
    the consumer holding the floor applies the floor.
    """
    repo = "acme/c637-noverdict"
    await record(client, repo, 6380, cycle="c1", round=1, to_fix=[],
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="go again"))
    await record(client, repo, 6380, cycle="c1", round=2,
                 provenance_counts={"introduced": 2},
                 round_stop=stop(converged=False, confident=False,
                                 reason="a stop, not convergence"))

    row = (await injection(client, repo))[2]
    assert row["rated_runs"] == 1 and row["rate_median"] == 1.0
    assert not any(k in row for k in ("over", "fired", "limit", "min_new")), \
        "the board publishes the population, never the verdict"


async def test_a_round_that_said_nothing_about_its_dials_is_not_counted(client):
    """The trap under `dial_runs`, and it is a wrong number rather than a nit.

    A Python `None` bound to a JSONB column stores the jsonb scalar `null`, which
    is not SQL NULL. So `rules IS NOT NULL` is true of every round that never sent
    a rules record, and the marker would have read 38 of 38 on the fleet where the
    honest answer was 1 — a coverage marker asserting full coverage of the one
    thing nothing covered. `jsonb_typeof(...) = 'object'` is the test, the same
    one `provenance_runs` uses one rung up the type ladder for `{}`.
    """
    repo = "acme/c637-jsonnull"
    await record(client, repo, 6381, cycle="c1", round=1, to_fix=[],
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="go again"))
    await record(client, repo, 6381, cycle="c1", round=2,
                 provenance_counts={"introduced": 3, "missed": 3},
                 round_stop=stop(converged=False, confident=False,
                                 reason="a stop, not convergence"))

    async with engine.begin() as conn:
        held = (await conn.execute(text(
            "SELECT jsonb_typeof(rules) FROM review_runs "
            "WHERE repo = :repo AND round = 2"), {"repo": repo})).scalar()
    assert held == "null", "the premise: the column holds jsonb null, not SQL NULL"
    assert (await injection(client, repo))[2]["dial_runs"] == 0


async def test_dial_runs_is_counted_over_the_population_it_annotates(client):
    """A coverage marker over a different population than its numerator is worse
    than none — `converged_runs` says so one query up, and this is the same trap.

    A round 1 names its dials and has no rate, so unfiltered `dial_runs` would
    report coverage for a round that is not in the distribution it sits beside.
    Filtered on the same `rated` predicate as every figure in the row, round 1
    reads `rated_runs: 0` and `dial_runs: 0` together, which is the only pair of
    numbers a reader can divide.
    """
    repo = "acme/c637-marker"
    rules = {"dials": {"review_panel.escalate_on.fix_injection":
                       {"layer": "defaults", "value": 0.5}}}
    await record(client, repo, 6382, cycle="c1", round=1, to_fix=[], rules=rules,
                 round_stop=stop(stopped=False, confident=False, converged=False,
                                 reason="go again"))
    await record(client, repo, 6382, cycle="c1", round=2, rules=rules,
                 provenance_counts={"introduced": 3, "missed": 1},
                 round_stop=stop(converged=False, confident=False,
                                 reason="a stop, not convergence"))

    rows = await injection(client, repo)
    assert rows[1]["rated_runs"] == 0 and rows[1]["dial_runs"] == 0, \
        "a round with no rate cannot be coverage for the rate"
    assert rows[2]["rated_runs"] == 1 and rows[2]["dial_runs"] == 1


async def test_an_empty_rules_record_is_not_coverage_for_a_threshold(client):
    """`{}` names no dial, so it cannot say what the threshold was.

    `_attribution_ran`'s rule one field over: a non-empty OBJECT, because a
    present-but-empty record and a record naming every dial are opposite answers
    to the question the marker asks. Raised by an independent reviewer on this
    diff — the first cut tested `jsonb_typeof(...) = 'object'` alone and counted
    the empty record as coverage.

    An upper bound remains, and the test says which side it errs on: a record
    carrying `{"dials": {}}` is counted, because telling THAT apart means knowing
    what `dials` holds and then what `escalate_on.fix_injection` is, which is the
    vocabulary this board refuses. Emptiness needs no vocabulary; naming a dial
    does.
    """
    repo = "acme/c637-emptyrules"
    for pr, rules in ((6383, {}), (6384, {"dials": {}}),
                      (6385, {"dials": {"review_panel.max_rounds":
                                        {"layer": "defaults", "value": 6}}})):
        await record(client, repo, pr, cycle=f"c{pr}", round=1, to_fix=[],
                     round_stop=stop(stopped=False, confident=False,
                                     converged=False, reason="go again"))
        await record(client, repo, pr, cycle=f"c{pr}", round=2, rules=rules,
                     provenance_counts={"introduced": 2, "missed": 2},
                     round_stop=stop(converged=False, confident=False,
                                     reason="a stop, not convergence"))

    row = (await injection(client, repo))[2]
    assert row["rated_runs"] == 3
    assert row["dial_runs"] == 2, "`{}` is not coverage; `{'dials': {}}` is, and " \
        "that is the upper bound this marker is documented to be"
