"""v2.29: a round records what it was judged AGAINST, not just what it read.

v2.26 gave a run its ``head_sha`` and left the other end of that range as a
branch *name*. An empty **To fix** list is only true relative to a base, so
without the base end nothing can ask whether the base moved since the review.

**The release is two columns because the obvious one field cannot do the job**,
and that is what most of this file pins. #98 proposed storing GitHub's
``baseRefOid`` and comparing it later against the PR's current ``baseRefOid``;
``baseRefOid`` is the merge base, and a merge base is a common ancestor, so
commits landing on the base branch cannot move it. A check built on it alone can
only ever answer "unmoved". Both ends are therefore stored and they are never
interchangeable:

* ``merge_base`` — what the reviewed diff was built FROM. Moves when the PR
  merges its base in or is rebased: the branch acting.
* ``base_sha`` — the base branch's tip at review time, the end that moves on its
  own, and the only one a staleness check can rest on.

The properties below are the ones a later consumer (#96) would be broken by:
they survive the round trip, they stay distinct, NULL still means *not
recorded*, a garbled value is refused AND named back, and nothing here draws a
verdict from the two disagreeing — which is the ordinary state of most PRs.
"""

from __future__ import annotations

import json
import logging

from .conftest import LAPTOP

REPO = "acme/v229repo"
AGENT = {**LAPTOP, "X-Agent-Instance": "ee55ff"}

#: Three distinct commit ids, spelled out rather than generated: the whole point
#: of the release is that these ends are not each other, and a test whose
#: fixtures could coincide would pass on an implementation that copied one into
#: the other.
HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
MERGE_BASE = "b2c3d4e5f60718293a4b5c6d7e8f90123456789a"
BASE_TIP = "c3d4e5f60718293a4b5c6d7e8f90123456789ab2"


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


async def detail(client, run_id: int) -> dict:
    r = await client.get(f"/review/{run_id}", headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------- both ends survive, and differ

async def test_both_ends_of_the_range_reach_the_board(client):
    """The release in one assertion. Before it, `base` was a branch name and the
    left-hand side of the diff the seats actually read was named nowhere."""
    run = await record(client, 9401, base="main", head_sha=HEAD,
                       merge_base=MERGE_BASE, base_sha=BASE_TIP,
                       to_fix=[finding("something")])
    d = await detail(client, run["id"])
    assert d["base"] == "main"
    assert d["head_sha"] == HEAD
    assert d["merge_base"] == MERGE_BASE
    assert d["base_sha"] == BASE_TIP


async def test_the_merge_base_is_not_stored_as_the_base_tip(client):
    """The defect this release exists to prevent, asserted directly rather than
    implied. `baseRefOid` is the merge base and does not move when the base
    branch does — PR #87 held one value across ten commits of `main`. An
    implementation that let either field stand in for the other would give a
    pre-land check a comparison that can only answer "unmoved"."""
    run = await record(client, 9402, merge_base=MERGE_BASE, base_sha=BASE_TIP)
    d = await detail(client, run["id"])
    assert d["merge_base"] != d["base_sha"]
    assert d["merge_base"] == MERGE_BASE and d["base_sha"] == BASE_TIP


async def test_one_end_alone_does_not_backfill_the_other(client):
    """A run that could read the merge base and not the base tip is the shape the
    skip path and a `gh` failure both produce. The missing end must stay missing:
    a merge base silently promoted to a base tip is the broken check again, this
    time with the board rather than the panel telling the lie."""
    run = await record(client, 9403, merge_base=MERGE_BASE)
    d = await detail(client, run["id"])
    assert d["merge_base"] == MERGE_BASE
    assert d["base_sha"] is None, "no tip was sent, so none was recorded"

    other = await record(client, 9404, base_sha=BASE_TIP)
    assert (await detail(client, other["id"]))["merge_base"] is None


async def test_the_base_ends_ride_the_findings_view(client):
    """`GET /review/findings` is where a defect is traced to the fix that caused
    it, and a trace is only replayable against a base. Two strings per run, which
    is why they ride the list view where the path lists deliberately do not."""
    run = await record(client, 9405, head_sha=HEAD, merge_base=MERGE_BASE,
                       base_sha=BASE_TIP, to_fix=[finding("a regression")])
    r = await client.get("/review/findings", params={"repo": REPO, "pr": 9405},
                         headers=AGENT)
    assert r.status_code == 200, r.text
    row = r.json()["runs"][0]
    assert row["id"] == run["id"]
    assert row["head_sha"] == HEAD
    assert row["merge_base"] == MERGE_BASE
    assert row["base_sha"] == BASE_TIP


async def test_the_base_ends_ride_the_run_list(client):
    """`GET /reviews` is where a caller scans runs to decide which to look at, and
    the entire point of these two fields is comparing them against the repo's
    current base without fetching each run one at a time."""
    run = await record(client, 9406, merge_base=MERGE_BASE, base_sha=BASE_TIP)
    r = await client.get("/reviews", params={"repo": REPO, "pr": 9406},
                         headers=AGENT)
    assert r.status_code == 200, r.text
    row = next(x for x in r.json() if x["id"] == run["id"])
    assert row["merge_base"] == MERGE_BASE
    assert row["base_sha"] == BASE_TIP


# ------------------------------------------- null is NOT RECORDED, never "no base"

async def test_a_pre_v229_run_records_nulls(client):
    """Every run before this release said nothing about either end. Reading that
    as "reviewed against nothing" — or worse, as a base that has not moved —
    is the collapse v2.26 set the rule against."""
    run = await record(client, 9410, head_sha=HEAD, to_fix=[finding("x")])
    d = await detail(client, run["id"])
    assert d["merge_base"] is None
    assert d["base_sha"] is None


async def test_a_base_end_is_stored_lowercase_so_it_joins(client):
    """These columns exist to be resolved — against the repo, and against the base
    branch's tip at land time. A sha that differs from itself by case joins
    nothing, and `head_sha` already holds to this rule."""
    run = await record(client, 9411,
                       merge_base="  " + MERGE_BASE.upper() + "  ",
                       base_sha=BASE_TIP.upper())
    d = await detail(client, run["id"])
    assert d["merge_base"] == MERGE_BASE
    assert d["base_sha"] == BASE_TIP


# ------------------------------------------------------ a dropped field says so

async def test_a_dropped_base_end_is_named_back_separately(client):
    """A run sent `"main"` and a run sent nothing both store NULL, and only the
    response tells the sender which. Named per field rather than as one "a commit
    id was refused" flag: a producer with a good head and a garbled base has one
    bug, and a reader told only that something was refused has to guess where."""
    run = await record(client, 9420, head_sha=HEAD,
                       merge_base="origin/main", base_sha="HEAD~1")
    assert run["merge_base_dropped"] == "origin/main"
    assert run["base_sha_dropped"] == "HEAD~1"
    assert "head_sha_dropped" not in run, "the head was fine and must not be implicated"

    silent = await record(client, 9421)
    assert "merge_base_dropped" not in silent
    assert "base_sha_dropped" not in silent


async def test_a_garbled_base_costs_the_base_and_nothing_else(client):
    """This module's standing rule: a malformed field records rather than 422s,
    and never costs the run its findings. Telemetry that can fail a review which
    already happened is worse than no telemetry."""
    for bad in ("main", "HEAD", 42, ["a" * 40], "", "zzzz" * 10, None):
        run = await record(client, 9422, merge_base=bad, base_sha=bad,
                           to_fix=[finding("still here")])
        d = await detail(client, run["id"])
        assert d["merge_base"] is None, bad
        assert d["base_sha"] is None, bad
        assert [f["title"] for f in d["findings"]] == ["still here"], bad


async def test_a_base_drop_is_logged_and_not_only_returned(client, caplog):
    """The response is read by whoever made the request, and `qb record-review`
    prints only the run id — so without the log the evidence is gone the moment
    the response is parsed, and #65's drift check has nothing left to read."""
    with caplog.at_level(logging.WARNING, logger="app.review"):
        run = await record(client, 9423, merge_base="main", base_sha="main~2")
    assert len(caplog.records) == 1, "one line per run, not one per dropped field"
    logged = json.loads(caplog.records[0].getMessage().split(": ", 1)[1])
    assert logged["run"] == run["id"]
    assert logged["merge_base_dropped"] == "main"
    assert logged["base_sha_dropped"] == "main~2"


# --------------------------------------- the two disagreeing is not a verdict

async def test_a_base_that_moved_is_recorded_and_not_judged(client):
    """`base_sha != merge_base` is the ordinary state of every PR whose base
    gained a commit after it forked, so this release stamps the pair and draws no
    conclusion. Whether a moved base makes a review stale is #96's verdict, and
    #98's asymmetry — proving staleness is cheap, proving freshness is not — is
    the consumer's to keep. Nothing here may pre-empt it with a flag."""
    run = await record(client, 9430, head_sha=HEAD, merge_base=MERGE_BASE,
                       base_sha=BASE_TIP, to_fix=[])
    d = await detail(client, run["id"])
    assert not any("stale" in k for k in d), "the verdict belongs to #96, not to ingest"
    assert d["merge_base"] == MERGE_BASE and d["base_sha"] == BASE_TIP
