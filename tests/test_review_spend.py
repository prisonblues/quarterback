"""`GET /review/spend` — what review has already cost, so a ceiling can be checked (#55).

The board holds the MEASUREMENT and never the ceiling's arithmetic. That split is
the same one :mod:`app.api.dials` makes and states at length — the board stores a
dial as opaque JSON and does not know what one means — and :mod:`app.review_queue`
already refuses ``max_rounds`` for it in the same words. So these tests are about
one thing: is the number honest, and does it stay honest when the window is only
half instrumented.

Two properties carry it, and both are #15's:

* **Null is "not recorded", never "spent nothing".** A seat nobody instrumented,
  a vendor that states no figure, a run recorded before the columns existed —
  each spent real money and measured none of it. A ``0`` there is a clean budget
  made out of not having looked.
* **``tokens`` is input + output.** Cached input is a slice OF input and reasoning
  sits inside output for some vendors and beside it for others, so adding either
  double-counts precisely the seats a ceiling is trying to price.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from .conftest import LAPTOP

REPO = "acme/spend-repo"
OTHER = "acme/spend-otherrepo"
AGENT = {**LAPTOP, "X-Agent-Instance": "5555aa"}


def payload(repo: str, pr: int, reviewers: dict | None = None, **over) -> dict:
    if reviewers is None:
        reviewers = {"claude": {"model": "opus", "ran": True, "duration_ms": 1_000,
                                "input_tokens": 1_000, "output_tokens": 100}}
    return {
        "repo": repo, "pr": pr, "judged": True, "judge_model": "opus",
        "reviewers_selected": sorted(reviewers),
        "reviewers": reviewers,
        "to_fix": [], "dismissed": [], "sonar_findings": [],
        **over,
    }


async def record(client, repo: str, pr: int, **over) -> int:
    r = await client.post("/review", json=payload(repo, pr, **over), headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def backdate(run_id: int, hours: float) -> None:
    """Move a recorded run into the past. There is no API for it, deliberately —
    a client that could choose its own timestamp could put today's spend outside
    today's window."""
    from sqlalchemy import text

    from app.db import engine
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE review_runs SET ts = :ts WHERE id = :id"),
            {"ts": datetime.now(UTC) - timedelta(hours=hours), "id": run_id})


async def spend(client, **params) -> dict:
    r = await client.get("/review/spend", params=params, headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


# ---- the shape of an answer ------------------------------------------------

async def test_a_board_with_nothing_on_it_reports_counted_zeros_and_unmeasured_nulls(
        client):
    """The two halves of "nothing" that must not be folded into one another:
    nobody reviewed anything (a real zero) and nobody measured anything (a null).
    A budget checked against the second believing it was the first is a ceiling
    that never binds."""
    body = await spend(client, repo="acme/spend-neverreviewed", pr=1)
    # The repo's own two windows only. `fleet_window` is every repo on the board
    # and this suite shares one, so an absolute assertion on it would be a test
    # about which other modules ran first.
    for window in ("repo_window", "pr_total"):
        assert body[window]["runs"] == 0, window
        assert body[window]["rows"] == 0, window
        assert body[window]["measured_rows"] == 0, window
        assert body[window]["tokens"] is None, window
        assert body[window]["cost_usd"] is None, window


async def test_the_repo_and_pr_windows_are_absent_when_not_asked_for(client):
    """A caller that did not name a repo gets the fleet's number and no invented
    scope. The absence is the answer, rather than a zero that reads like one."""
    body = await spend(client)
    assert "repo_window" not in body and "pr_total" not in body
    assert "fleet_window" in body
    body = await spend(client, repo=REPO)
    assert "repo_window" in body and "pr_total" not in body


# ---- what a token is -------------------------------------------------------

async def test_tokens_are_input_plus_output_and_nothing_else(client):
    """`/review/stats`' own `billable`, and for its reasons. Cached input is a
    SLICE of input; reasoning sits inside output for some vendors and beside it
    for others. Adding either double-counts the seat being priced."""
    await record(client, "acme/spend-tokenshape", 1, reviewers={"claude": {
        "model": "opus", "ran": True, "duration_ms": 1,
        "input_tokens": 1_000, "output_tokens": 100,
        "cached_input_tokens": 900, "reasoning_tokens": 90}})
    body = await spend(client, repo="acme/spend-tokenshape", pr=1)
    assert body["repo_window"]["tokens"] == 1_100


async def test_a_run_that_recorded_no_scorecard_is_still_a_run(client):
    """The run ceiling's whole point. A round that spent its seats and then failed
    to instrument them cost what it cost, and a ceiling that only counted
    instrumented runs would be LOOSENED by the failure to instrument them."""
    await record(client, "acme/spend-norows", 1, reviewers={})
    body = await spend(client, repo="acme/spend-norows", pr=1)
    assert body["repo_window"]["runs"] == 1
    assert body["repo_window"]["rows"] == 0
    assert body["repo_window"]["tokens"] is None


async def test_a_half_instrumented_window_says_how_much_it_covers(client):
    """`rows` against `measured_rows` is what lets a client say "measured over 1 of
    2 reviewer runs — the real spend is higher" instead of quoting a sum as though
    it were complete."""
    await record(client, "acme/spend-halfmeasured", 1, reviewers={
        "claude": {"model": "opus", "ran": True, "duration_ms": 1,
                   "input_tokens": 500, "output_tokens": 50},
        "codex": {"model": "gpt", "ran": True, "duration_ms": 1}})
    body = await spend(client, repo="acme/spend-halfmeasured", pr=1)
    assert body["repo_window"]["rows"] == 2
    assert body["repo_window"]["measured_rows"] == 1
    assert body["repo_window"]["tokens"] == 550


# ---- what a window is ------------------------------------------------------

async def test_the_rolling_window_forgets_and_the_per_pr_total_does_not(client):
    """The two ceilings measure different things and the difference is the time
    bound. A per-PR ceiling is about one pull request's whole cost; a rolling
    window would let a PR reviewed daily for a fortnight stay under a ceiling it
    passed on day two."""
    old = await record(client, "acme/spend-windowed", 7)
    await backdate(old, hours=48)
    await record(client, "acme/spend-windowed", 7)

    body = await spend(client, repo="acme/spend-windowed", pr=7, hours=24)
    assert body["repo_window"]["runs"] == 1
    assert body["pr_total"]["runs"] == 2

    wider = await spend(client, repo="acme/spend-windowed", pr=7, hours=72)
    assert wider["repo_window"]["runs"] == 2


async def test_the_fleet_window_sums_every_repo_and_the_repo_window_does_not(client):
    """#55 asks for per-repo AND fleet-wide, and they are different questions: a
    repo that has spent nothing today still stops when the fleet's window is
    gone."""
    before = (await spend(client, repo=REPO, pr=11))["fleet_window"]["runs"]
    await record(client, REPO, 11)
    await record(client, OTHER, 12)
    body = await spend(client, repo=REPO, pr=11)
    assert body["repo_window"]["runs"] == 1
    assert body["fleet_window"]["runs"] == before + 2
    assert body["fleet_window"]["runs"] > body["repo_window"]["runs"]


async def test_a_repo_is_asked_about_under_one_spelling(client):
    """GitHub folds owner and repo case and preserves what you typed, so
    `Acme/SpendRepo` and `acme/spendrepo` are one repository the board would
    otherwise hold as two — and a ceiling checked against half a repo's spend is
    not a ceiling. `_asked_repo` is the fold; this is the assertion that this
    endpoint uses it."""
    await record(client, "acme/spend-casefold", 3)
    body = await spend(client, repo="Acme/Spend-CaseFold", pr=3)
    assert body["repo"] == "acme/spend-casefold"
    assert body["repo_window"]["runs"] == 1


# ---- the refusals ----------------------------------------------------------

async def test_reading_the_spend_needs_a_token(client):
    assert (await client.get("/review/spend")).status_code == 401


async def test_spend_is_not_swallowed_by_the_run_id_route(client):
    """A named path under `/review/` has to be declared BEFORE `/review/{run_id}`,
    and the failure when it is not looks nothing like a missing route: FastAPI
    matches the parameterised one and answers **422, "spend is not an integer"**.
    Measured — that is exactly what this suite returned with the endpoint removed.
    A client reading a 422 as "my query was wrong" would go and fix the query."""
    r = await client.get("/review/spend", headers=AGENT)
    assert r.status_code == 200, r.text
    assert "fleet_window" in r.json()


@pytest.mark.parametrize("hours", [0, -1, 24 * 28 + 1])
async def test_a_window_outside_the_bounds_is_refused(client, hours):
    """A ceiling is a RATE. A caller asking for a year's total has stopped asking
    a ceiling's question, and the answer is computed over every scorecard in the
    range."""
    r = await client.get("/review/spend", params={"hours": hours}, headers=AGENT)
    assert r.status_code == 422, r.text


async def test_a_pr_number_that_is_not_one_is_refused(client):
    r = await client.get("/review/spend", params={"repo": REPO, "pr": 0},
                         headers=AGENT)
    assert r.status_code == 422, r.text


# ---- the two ends, pinned against each other (#199) -------------------------

def _panel_caps():
    """`harness/loops/panel_caps.py`, imported by path.

    `test_dials.py` does this and says why: the skew this guards against is
    between a body this server PRODUCES and a body that module PARSES, so the
    test has to hold both ends. Re-describing the endpoint in the harness suite
    would pass while the two drifted, which is the failure #199 is about.
    """
    import importlib.util
    import sys
    root = Path(__file__).resolve().parent.parent / "harness" / "loops"
    sys.path.insert(0, str(root))
    name = "panel_caps_under_test"
    spec = importlib.util.spec_from_file_location(name, root / "panel_caps.py")
    mod = importlib.util.module_from_spec(spec)
    # Registered BEFORE it is executed, unlike `test_dials.py`'s equivalent. This
    # module carries `@dataclass` under `from __future__ import annotations`, and
    # `dataclasses` resolves a string annotation through `sys.modules[cls.
    # __module__]` — which is `None` for a module nothing registered, and comes
    # back as an `AttributeError` from inside the standard library.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


async def test_the_body_this_endpoint_returns_is_the_body_the_ceiling_is_checked_against(
        client, monkeypatch):
    """A real response from the real endpoint, handed to the real resolver.

    Both directions are asserted, because a ceiling that silently stopped binding
    reads exactly like a repo under its budget: the same body has to stop a run
    over its ceiling and let one under it through.
    """
    caps = _panel_caps()
    await record(client, "acme/spend-seam", 42, reviewers={"claude": {
        "model": "opus", "ran": True, "duration_ms": 1,
        "input_tokens": 4_000, "output_tokens": 400}})
    body = await spend(client, repo="acme/spend-seam", pr=42)
    monkeypatch.setenv(caps.SPEND_ENV, json.dumps(body))

    cfg = {"github": "acme/spend-seam", "name": "seam"}
    over = caps.check(cfg, {"budget": {"tokens_per_pr": 1_000}}, 42, [],
                      headless=False)
    assert over.stop, body
    assert "4,400 of 1,000 tokens" in over.refusal

    under = caps.check(cfg, {"budget": {"tokens_per_pr": 1_000_000}}, 42, [],
                       headless=False)
    assert under.stop is False


def test_the_two_ends_agree_about_the_largest_window():
    """The harness refuses a window the board would refuse, so the operator gets a
    sentence naming their key instead of a 422 from a request they never saw. Two
    numbers, one meaning — this is what keeps them equal."""
    from app.api.reviews import MAX_SPEND_WINDOW_HOURS
    assert _panel_caps().MAX_WINDOW_HOURS == MAX_SPEND_WINDOW_HOURS


def test_every_window_the_harness_reads_is_a_window_this_endpoint_returns():
    """A ceiling keyed to a window the board does not send is a ceiling that
    reports "could not be checked" for ever — visible, but never binding."""
    caps = _panel_caps()
    assert {w for w, _unit in caps.CEILINGS.values()} == {
        "repo_window", "fleet_window", "pr_total"}
