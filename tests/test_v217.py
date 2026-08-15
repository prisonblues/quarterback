"""v2.17: what each panel member COST, alongside what it found.

v2.10 recorded findings and v2.13 wired up wall-clock. Neither said what a seat
spent, so the /panel leaderboard could rank a reviewer top on confirmed findings
while it was quietly the most expensive member on the panel — "which reviewer
earns its cost" was only half-answerable.

Two properties carry the whole feature and most of these tests defend one or the
other:

* **Null is "not recorded", never "spent nothing".** The panel reads usage back
  out of a pinned session after the run, and a vendor that states no figure or a
  transcript that could not be read simply sends nothing. Defaulting any of it to
  0 would average an unmeasured run in as a free one.
* **These numbers compare WITHIN a vendor only.** Different tokenizers, different
  cache semantics, and only some vendors state a cost. So the aggregate is
  grouped by (reviewer, model, effort) — the same vendor at two tiers is two
  competitors — and duration stays the cross-vendor axis.
"""

from __future__ import annotations

from .conftest import LAPTOP

REPO = "acme/v217repo"
AGENT = {**LAPTOP, "X-Agent-Instance": "ee55ff"}


def finding(**over) -> dict:
    f = {
        "severity": "P2",
        "file": "app/thing.py",
        "line": 12,
        "title": "unbounded subprocess call",
        "detail": "no timeout, so a hung CLI hangs the panel",
        "reviewers": ["claude"],
        "reason": "real",
    }
    return {**f, **over}


def payload(pr: int, **over) -> dict:
    body = {
        "repo": REPO,
        "pr": pr,
        "judged": True,
        "judge_model": "opus",
        "reviewers_selected": ["claude"],
        "reviewers": {
            "claude": {
                "model": "opus", "ran": True, "duration_ms": 41_000,
                "input_tokens": 39_217, "output_tokens": 455,
                "cached_input_tokens": 24_356, "reasoning_tokens": 450,
            },
        },
        "to_fix": [finding()],
        "dismissed": [],
        "sonar_findings": [],
    }
    return {**body, **over}


async def record(client, pr: int, **over) -> dict:
    r = await client.post("/review", json=payload(pr, **over), headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


async def detail(client, run_id: int) -> dict:
    r = await client.get(f"/review/{run_id}", headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


async def stats(client, **q) -> dict:
    params = {"repo": REPO, **q}
    r = await client.get("/review/stats", params=params, headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


def row(s: dict, reviewer: str, model: str | None = None) -> dict:
    [m] = [m for m in s["by_model"]
           if m["reviewer"] == reviewer and (model is None or m["model"] == model)]
    return m


# ---- the round trip --------------------------------------------------------

async def test_a_members_token_usage_survives_the_round_trip(client):
    d = await detail(client, (await record(client, 9401))["id"])
    [card] = [c for c in d["reviewers"] if c["name"] == "claude"]
    assert card["input_tokens"] == 39_217
    assert card["output_tokens"] == 455
    assert card["cached_input_tokens"] == 24_356
    assert card["reasoning_tokens"] == 450
    assert card["duration_ms"] == 41_000


async def test_a_panel_that_reports_no_usage_still_records(client):
    """Every field is independently optional — an older panel, or a seat whose
    session could not be read, must not start failing to record."""
    run = await record(client, 9402,
                       reviewers={"claude": {"model": "opus", "ran": True}})
    assert run["recorded"] is True


async def test_unrecorded_usage_stays_null_rather_than_zero(client):
    """The distinction the whole feature rests on. A seat that reported nothing
    must not read as a seat that cost nothing, or it wins every cost comparison
    on the strength of not having been measured."""
    run = await record(client, 9403,
                       reviewers={"claude": {"model": "opus", "ran": True}})
    [card] = [c for c in (await detail(client, run["id"]))["reviewers"]
              if c["name"] == "claude"]
    assert card["input_tokens"] is None
    assert card["output_tokens"] is None
    assert card["cost_usd"] is None


async def test_cost_is_recorded_only_where_the_vendor_states_one(client):
    """pi states a price; claude and codex don't. The board stores what it was
    told and never multiplies tokens by a price table — a run priced at today's
    rates reads wrong the moment the rates move."""
    run = await record(client, 9404, reviewers_selected=["claude", "pi"], reviewers={
        "claude": {"model": "opus", "ran": True, "input_tokens": 100, "output_tokens": 10},
        "pi": {"model": "kimi-k3", "ran": True, "input_tokens": 525,
               "output_tokens": 27, "cost_usd": 0.0017832},
    })
    cards = {c["name"]: c for c in (await detail(client, run["id"]))["reviewers"]}
    assert cards["pi"]["cost_usd"] == 0.001783
    assert cards["claude"]["cost_usd"] is None


# ---- aggregation -----------------------------------------------------------

async def test_stats_sum_tokens_per_tier_not_per_vendor(client):
    """The question these numbers answer: is the expensive tier worth it? So the
    same vendor at two tiers must land in two rows, each with its own spend."""
    await record(client, 9410, reviewers={
        "claude": {"model": "opus", "ran": True,
                   "input_tokens": 1000, "output_tokens": 100},
    })
    await record(client, 9411, reviewers={
        "claude": {"model": "sonnet", "ran": True,
                   "input_tokens": 400, "output_tokens": 40},
    })
    s = await stats(client)
    opus, sonnet = row(s, "claude", "opus"), row(s, "claude", "sonnet")
    assert opus["input_tokens"] >= 1000 and opus["output_tokens"] >= 100
    assert sonnet["input_tokens"] == 400 and sonnet["output_tokens"] == 40
    assert sonnet["total_tokens"] == 440


async def test_a_tier_nobody_measured_aggregates_to_null_not_zero(client):
    await record(client, 9412, reviewers={
        "claude": {"model": "haiku", "ran": True},
    })
    r = row(await stats(client), "claude", "haiku")
    assert r["input_tokens"] is None and r["total_tokens"] is None
    assert r["tokens_per_run"] is None and r["cost_usd"] is None
    assert r["token_runs"] == 0


async def test_a_half_measured_window_says_how_much_of_it_reported(client):
    """A sum over a partly-instrumented window is a real number about part of the
    window. Without `token_runs` a client renders it as the whole thing, and
    "tokens per run" comes out low by however many runs said nothing."""
    for pr in (9420, 9421):
        await record(client, pr, reviewers={
            "claude": {"model": "gpt-x", "ran": True,
                       "input_tokens": 500, "output_tokens": 50},
        })
    await record(client, 9422, reviewers={"claude": {"model": "gpt-x", "ran": True}})

    r = row(await stats(client), "claude", "gpt-x")
    assert r["ran"] == 3 and r["token_runs"] == 2
    assert r["total_tokens"] == 1100
    # Divided by the runs that REPORTED, not by every run in the window.
    assert r["tokens_per_run"] == 550


async def test_a_partly_stated_usage_block_counts_as_instrumented(client):
    """Each token field is independently optional, so a vendor that states only
    some of them has still been measured. Counting that row as unmeasured would
    put a zero in `token_runs` next to a non-null sum it contributed to."""
    await record(client, 9425, reviewers={
        "claude": {"model": "partialmodel", "ran": True, "cached_input_tokens": 64},
    })
    r = row(await stats(client), "claude", "partialmodel")
    assert r["cached_input_tokens"] == 64
    assert r["token_runs"] == 1


async def test_tokens_per_confirmed_is_spend_against_findings_that_survived(client):
    """Per confirmed, not per raised: a seat that raises forty and lands two has
    not earned its tokens, and dividing by `raised` would say it had."""
    await record(client, 9430, reviewers={
        "claude": {"model": "tokmodel", "ran": True,
                   "input_tokens": 900, "output_tokens": 100},
    }, to_fix=[finding(), finding(title="second thing", file="app/b.py")])
    r = row(await stats(client), "claude", "tokmodel")
    assert r["confirmed"] == 2
    assert r["tokens_per_confirmed"] == 500


async def test_cost_per_confirmed_only_where_a_cost_was_stated(client):
    # Its own model slug: stats group by (reviewer, model, effort) over the whole
    # repo, so sharing a slug with another test's run would sum the two costs.
    await record(client, 9440, reviewers_selected=["pi"], reviewers={
        "pi": {"model": "costmodel", "ran": True, "input_tokens": 10,
               "output_tokens": 2, "cost_usd": 0.5},
    }, to_fix=[finding(reviewers=["pi"])])
    r = row(await stats(client), "pi", "costmodel")
    assert r["cost_usd"] == 0.5 and r["cost_runs"] == 1
    assert r["cost_per_confirmed"] == 0.5


# ---- garbage in, a lost number rather than a lost run ----------------------

async def test_a_garbled_count_costs_the_number_not_the_whole_record(client):
    """Recording is best-effort for the panel: a review must never fail because
    the board choked on its telemetry. A count the column cannot hold, or that
    cannot be a count at all, is dropped — and a 0 is not substituted, because
    the stats would then average it in as fact."""
    run = await record(client, 9450, reviewers={
        "claude": {"model": "opus", "ran": True,
                   "input_tokens": 2 ** 40,     # wider than the INTEGER column
                   "output_tokens": -5,         # no vendor states a negative
                   "cached_input_tokens": 7},
    })
    [card] = [c for c in (await detail(client, run["id"]))["reviewers"]
              if c["name"] == "claude"]
    assert card["input_tokens"] is None
    assert card["output_tokens"] is None
    assert card["cached_input_tokens"] == 7      # the sane field is still kept


async def test_an_impossible_cost_is_refused_before_the_driver_sees_it(client):
    """NaN/Infinity arrive from a vendor that emitted a JSON non-number, and a
    figure past the column's width is not a panel run's cost. Both would take the
    whole record down at the driver if they got that far."""
    run = await record(client, 9451, reviewers={
        "claude": {"model": "opus", "ran": True, "cost_usd": 1e12},
    })
    [card] = [c for c in (await detail(client, run["id"]))["reviewers"]
              if c["name"] == "claude"]
    assert card["cost_usd"] is None


async def test_a_garbled_duration_is_dropped_the_same_way(client):
    """`duration_ms` is the same INTEGER column with the same failure mode; it
    had no guard until the token fields arrived needing one."""
    run = await record(client, 9452, reviewers={
        "claude": {"model": "opus", "ran": True, "duration_ms": 2 ** 40},
    })
    [card] = [c for c in (await detail(client, run["id"]))["reviewers"]
              if c["name"] == "claude"]
    assert card["duration_ms"] is None
