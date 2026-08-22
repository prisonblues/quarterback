"""The board's dial layer — #305.

`review_panel.fix_severity_floor` decides which findings a fix pass may touch, and
changing it used to be a commit on a pull request reviewed by the panel that dial
configures. These pin the endpoint half of the third layer: what it stores, who may
write it, what an expiry means, and the one thing it deliberately does NOT know.

The RESOLUTION half — precedence, the narrow-only rule for `reviewers.<seat>.enabled`,
and the per-dial provenance report — lives in `harness/loops/tests/test_harness_dials.py`,
because the harness owns the vocabulary and this server does not.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, update

from app.db import async_session
from app.models.dial import DialSetting

from .conftest import LAPTOP, PINNED_SETTINGS

HUMAN = {"Remote-User": "rich", "X-Edge-Auth": PINNED_SETTINGS["HUMAN_EDGE_SECRET"]}

REPO = "prisonblues/quarterback"
FLOOR = "review_panel.fix_severity_floor"


@pytest.fixture(autouse=True)
async def _empty_board(client):
    """The suite rebuilds the schema once per session, so a dial set by one test
    is still in force in the next. Every test here starts from the state the day
    this landed: a board with no dials at all."""
    async with async_session() as s:
        await s.execute(delete(DialSetting))
        await s.commit()


async def set_dial(client, dial=FLOOR, value="P3", repo=REPO, reason="trying P3",
                   expires_at=None, headers=None):
    body = {"dial": dial, "value": value, "reason": reason}
    if repo is not None:
        body["repo"] = repo
    if expires_at is not None:
        body["expires_at"] = expires_at
    return await client.post("/dials", json=body, headers=headers or HUMAN)


async def test_a_board_with_no_dials_answers_an_empty_list(client):
    """The state every repo is in the day this lands, and the one that has to be
    indistinguishable from the state before it landed."""
    r = await client.get("/dials", params={"repo": REPO}, headers=LAPTOP)
    assert r.status_code == 200
    assert r.json()["dials"] == []


async def test_a_dial_set_is_a_dial_returned_with_its_reason_and_who_set_it(client):
    assert (await set_dial(client)).status_code == 200
    got = (await client.get("/dials", params={"repo": REPO}, headers=LAPTOP)).json()
    assert [(d["dial"], d["value"], d["scope"], d["set_by"], d["reason"])
            for d in got["dials"]] == [(FLOOR, "P3", "repo", "rich", "trying P3")]


async def test_a_fleet_dial_reaches_a_repo_and_a_repo_dial_does_not_reach_the_fleet(client):
    """Two scopes in one table. `#276`'s throttle is fleet-scoped by definition —
    the five-hour window is one number shared by every project on the subscription
    — and a floor is usually one repo's judgement."""
    await set_dial(client, dial="review_panel.max_rounds", value=1, repo=None,
                   reason="fleet-wide")
    await set_dial(client, value="P3")
    mine = (await client.get("/dials", params={"repo": REPO},
                             headers=LAPTOP)).json()["dials"]
    assert {d["dial"]: d["scope"] for d in mine} == {
        FLOOR: "repo", "review_panel.max_rounds": "fleet"}
    theirs = (await client.get("/dials", params={"repo": "someone/else"},
                               headers=LAPTOP)).json()["dials"]
    assert [d["dial"] for d in theirs] == ["review_panel.max_rounds"]


async def test_setting_a_dial_twice_replaces_it_and_says_what_it_replaced(client):
    """Moving a floor without being told what it was is how a dial gets nudged
    twice by two people who each believed they were starting from the default."""
    await set_dial(client, value="P3", reason="first")
    again = await set_dial(client, value="P4", reason="second")
    assert again.status_code == 200
    assert [(d["value"], d["reason"]) for d in again.json()["replaced"]] == [("P3", "first")]
    live = (await client.get("/dials", params={"repo": REPO},
                             headers=LAPTOP)).json()["dials"]
    assert [(d["value"], d["reason"]) for d in live] == [("P4", "second")]


async def test_an_expired_dial_is_simply_absent(client):
    """Not reported-as-expired. A resolution whose dial lapsed and one that never
    had a dial have to be indistinguishable, or the expiry is a flag somebody still
    has to clear."""
    soon = datetime.now(UTC) + timedelta(seconds=1)
    r = await set_dial(client, expires_at=soon.isoformat())
    assert r.status_code == 200
    # Rather than sleeping: reach past the API and age the row, which is the same
    # fact the clock would eventually produce.
    async with async_session() as s:
        await s.execute(update(DialSetting).values(
            expires_at=datetime.now(UTC) - timedelta(hours=1)))
        await s.commit()
    assert (await client.get("/dials", params={"repo": REPO},
                             headers=LAPTOP)).json()["dials"] == []


async def test_an_expiry_already_in_the_past_is_refused_at_the_door(client):
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    r = await set_dial(client, expires_at=past)
    assert r.status_code == 422
    assert "past" in r.json()["detail"]["error"]


async def test_clearing_a_dial_returns_the_repo_to_its_own_default(client):
    await set_dial(client)
    r = await client.post("/dials/clear", json={"dial": FLOOR, "repo": REPO},
                          headers=HUMAN)
    assert r.status_code == 200
    assert [d["value"] for d in r.json()["cleared"]] == ["P3"]
    assert (await client.get("/dials", params={"repo": REPO},
                             headers=LAPTOP)).json()["dials"] == []


async def test_clearing_a_dial_that_is_not_set_is_not_an_error(client):
    r = await client.post("/dials/clear", json={"dial": FLOOR, "repo": REPO},
                          headers=HUMAN)
    assert r.status_code == 200 and r.json()["cleared"] == []


async def test_an_agent_token_may_read_a_dial_and_may_not_set_one(client):
    """The whole security argument in one test.

    `harness_rules`' two-ref rule exists so a poisoned pull request cannot rewrite
    the rules governing its own review. Anything running while a branch under review
    is checked out — a test suite, a build step, a git hook — runs as a user whose
    machine token this board accepts. If a machine token could set a dial, that code
    could turn the `claude` seat off on the review of its own change.
    """
    assert (await client.get("/dials", headers=LAPTOP)).status_code == 200
    r = await set_dial(client, headers=LAPTOP)
    assert r.status_code == 403
    assert (await client.get("/dials", params={"repo": REPO},
                             headers=LAPTOP)).json()["dials"] == []


async def test_a_claimed_human_without_the_edge_secret_is_not_a_human(client):
    """`Remote-User` is an ordinary request header and the app cannot see who set
    it. The edge's shared secret is what makes it a method rather than a claim."""
    r = await set_dial(client, headers={**LAPTOP, "Remote-User": "rich",
                                        "X-Edge-Auth": "not-the-secret"})
    assert r.status_code == 403


async def test_reading_a_dial_still_needs_authentication(client):
    assert (await client.get("/dials")).status_code == 401


async def test_the_board_stores_a_dial_it_has_never_heard_of(client):
    """The constraint that keeps this from being a SECOND place a dial is written
    down: the harness ships the dial table, the server image carries no `harness/`
    directory, and a copy here would be the two-sources-of-truth failure arriving
    from the other end. So the name is checked for SHAPE and never for vocabulary,
    and the harness reports at every resolution anything it will not apply."""
    r = await set_dial(client, dial="review_panel.something_invented_tomorrow",
                       value=7)
    assert r.status_code == 200
    assert [d["dial"] for d in (await client.get(
        "/dials", params={"repo": REPO}, headers=LAPTOP)).json()["dials"]] == [
            "review_panel.something_invented_tomorrow"]


async def test_a_dial_name_that_is_not_a_dotted_path_is_refused(client):
    r = await set_dial(client, dial="; drop table dial_settings")
    assert r.status_code == 422
    assert "dotted path" in r.json()["detail"]["error"]


async def test_null_is_a_storable_value_and_not_the_absence_of_one(client):
    """`null` is the documented OFF SWITCH for `max_fix_growth`,
    `distant_merge_lines` and `escalate_on.premise_repeated`. A bare JSONB column
    cannot tell it from SQL NULL once an ORM has serialised Python `None` into it,
    which is why the column wraps."""
    r = await set_dial(client, dial="review_panel.max_fix_growth", value=None,
                       reason="off")
    assert r.status_code == 200
    live = (await client.get("/dials", params={"repo": REPO},
                             headers=LAPTOP)).json()["dials"]
    assert len(live) == 1 and live[0]["value"] is None


async def test_a_value_too_large_to_be_a_knob_is_refused(client):
    r = await set_dial(client, value="P" * 20_000)
    assert r.status_code == 422
    assert "too large" in r.json()["detail"]["error"]


async def test_a_repo_that_is_not_owner_slash_name_is_refused(client):
    r = await set_dial(client, repo="quarterback")
    assert r.status_code == 422
    assert "owner/name" in r.json()["detail"]["error"]


async def test_a_blank_repo_and_an_absent_one_are_the_same_fleet_scope(client):
    """A query string cannot express "absent" — `?repo=` arrives as the empty
    string — and a fleet dial writable under two keys is one that can be set twice
    and resolved once."""
    await set_dial(client, value="P1", repo=None, reason="fleet")
    r = await set_dial(client, value="P2", repo="", reason="also fleet")
    assert [d["value"] for d in r.json()["replaced"]] == ["P1"]
    assert len((await client.get("/dials", headers=LAPTOP)).json()["dials"]) == 1


async def test_a_dial_with_no_reason_is_refused(client):
    """A dial whose argument was never written down is one nobody can decide to
    remove — which is the failure `expires_at` exists for, arriving through prose."""
    r = await client.post("/dials", json={"dial": FLOOR, "value": "P3", "reason": ""},
                          headers=HUMAN)
    assert r.status_code == 422


# ------------------------------------------- what the board can see for itself

async def test_a_reason_of_nothing_but_spaces_is_a_422_and_not_a_500(client):
    """Pydantic's `min_length` counts characters and `"   "` has three, so without
    a strip the natural mistake reached the database's own constraint and came back
    as a server error instead of an answer naming the field."""
    r = await set_dial(client, reason="   ")
    assert r.status_code == 422
    assert "reason" in r.json()["detail"]["error"]


async def test_a_value_postgres_cannot_store_is_refused_where_it_is_typed(client):
    """`json.dumps` emits the JavaScript literals `NaN` and `Infinity` by default
    and JSONB refuses both, so a float dial set to one passed every check and failed
    at the commit."""
    # A raw body, because httpx refuses to ENCODE `inf` — which is the point: the
    # only way it reaches the endpoint is from a client whose encoder is Python's
    # default one, and Python's default one emits it.
    r = await client.post(
        "/dials",
        content=('{"dial": "review_panel.max_fix_growth", "value": Infinity, '
                 f'"reason": "no ceiling", "repo": "{REPO}"}}'),
        headers={**HUMAN, "Content-Type": "application/json"})
    assert r.status_code == 422
    assert (await client.get("/dials", params={"repo": REPO},
                             headers=LAPTOP)).json()["dials"] == []


# ------------------------------- the two ends, pinned against each other (#199)

def _harness_rules():
    """`harness/loops/harness_rules.py`, imported by path.

    The harness is not a package and is not on this suite's path, and it is
    deliberately imported HERE rather than the endpoint being re-described in the
    harness suite: the skew this guards against is between a body this server
    PRODUCES and a body that resolver PARSES, so the test has to hold both.
    """
    import importlib.util
    import sys
    root = Path(__file__).resolve().parent.parent / "harness" / "loops"
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location(
        "harness_rules_under_test", root / "harness_rules.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_the_body_this_endpoint_returns_is_the_body_the_resolver_parses(
        client, monkeypatch):
    """Client/server skew is #199, and a settings channel is where it would hurt
    most: a board reporting a floor as in force while every box's resolver quietly
    ignored the shape it arrived in is precisely the disagreement #305 exists to end.

    So this takes a REAL response from the real endpoint and hands it to the real
    resolver, rather than either side asserting against a hand-written literal.
    """
    hr = _harness_rules()
    await set_dial(client, value="P3", reason="trying P3", repo=REPO)
    await set_dial(client, dial="review_panel.max_rounds", value=2, repo=None,
                   reason="fleet-wide")
    await set_dial(client, dial="reviewers.pi.enabled", value=False, repo=REPO,
                   reason="metered, and not worth it here")
    body = (await client.get("/dials", params={"repo": REPO},
                             headers=LAPTOP)).json()

    monkeypatch.setenv(hr.DIALS_ENV, json.dumps(body))
    dials, _where, problems, unreadable = hr.board_dials(REPO)
    assert (problems, unreadable) == ([], False)
    assert {k: (v["value"], v["scope"]) for k, v in dials.items()} == {
        FLOOR: ("P3", "repo"),
        "review_panel.max_rounds": (2, "fleet"),
        "reviewers.pi.enabled": (False, "repo"),
    }
    assert dials[FLOOR]["reason"] == "trying P3"
    assert dials[FLOOR]["set_by"] == "rich"


async def test_an_expired_row_never_reaches_the_resolver_from_either_end(client,
                                                                        monkeypatch):
    """Both ends filter, and neither relies on the other doing it: the endpoint
    because a client that had to filter could forget to, and the resolver because
    `$QUARTERBACK_DIALS` is a hand-written body with no server in front of it."""
    hr = _harness_rules()
    await set_dial(client)
    async with async_session() as s:
        await s.execute(update(DialSetting).values(
            expires_at=datetime.now(UTC) - timedelta(hours=1)))
        await s.commit()
    body = (await client.get("/dials", params={"repo": REPO},
                             headers=LAPTOP)).json()
    assert body["dials"] == []

    # And the same row, handed straight to the resolver as though no server had
    # filtered it.
    raw = {"dials": [{"dial": FLOOR, "value": "P3", "scope": "repo",
                      "reason": "r", "set_by": "rich",
                      "expires_at": (datetime.now(UTC)
                                     - timedelta(hours=1)).isoformat()}]}
    monkeypatch.setenv(hr.DIALS_ENV, json.dumps(raw))
    assert hr.board_dials(REPO)[0] == {}
