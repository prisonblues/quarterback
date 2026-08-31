"""#643: the dials a round ran under, stored beside the verdict they produced.

`review_panel` is the fifth key the panel has been sending into `ReviewIn`'s
`extra="ignore"` and the board discarding without a word. It is the #165/#297
dial set **as applied** — a repo whose `fix_severity_floor` was rejected reads
here the floor that actually ran — and it is what `converged` was decided under:
two of that flag's conjuncts are cut at `cleared_floor`, which is
`round_trigger_floor` or `fix_severity_floor` depending on whether a budget is in
force. All three are in this object, and none of them was on the row.

So the pairing these tests pin is a round's verdict and its policy on the same
record. `tests/test_review_convergence.py` covers the verdict; this covers the
policy, and the two are deliberately separate files because a board-side
derivation of the first from the second is exactly what m6bc45ff1 argues must not
be built.

The other half is what this board REFUSES to do with the object: it stores it and
does not read a dial out of it. `app/api/dials.py` makes that case — a second
place that knew what `review_panel.max_rounds` meant is the drift #305 exists to
end — so a dial name is never checked and a dial value is never coerced. What is
left to refuse is shape and size, and both refuse the whole object, because half
a policy record is not a smaller policy but one no round ran under.
"""

from __future__ import annotations

# The module, not the names off it: `MAX_DIALS_CHARS` arrives with this feature, so
# a `from ... import` of it turns the red half of every OTHER test in this file into
# a collection error, which demonstrates nothing about the behaviour they pin.
from app.api import reviews

from .conftest import LAPTOP

REPO = "acme/dials643"
AGENT = {**LAPTOP, "X-Agent-Instance": "d643d6"}

#: What `panel_seats.Dials.as_dict()` sends, spelled as it spells it.
DIALS = {
    "fixer_may_defer": True,
    "file_deferral_issues": "any",
    "fix_severity_floor": "P4",
    "round_trigger_floor": "P2",
    "low_severity_fix_lines": 40,
    "unrefereed_line_weight": 2,
    "max_fix_growth": 1.5,
    "max_fix_growth_chars": None,
    "max_fix_guard_lines": 250,
    "reviewer_scope": "increment",
    "next_door_days": 30,
    "require_failing_test": False,
    "max_rounds": 5,
}


def payload(pr: int, **over) -> dict:
    body = {
        "repo": REPO,
        "pr": pr,
        "pr_title": f"feat: thing {pr}",
        "base": "main",
        "reviewed": True,
        "judged": True,
        "judge_model": "opus",
        "reviewers_selected": ["claude"],
        "reviewers": {"claude": {"model": "sonnet", "ran": True}},
        "review_panel": DIALS,
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


async def test_the_dials_survive_the_round_trip_verbatim(client):
    """The whole point: what the panel applied is readable off the run afterwards.

    Asserted as one equality against the object the panel sends rather than key by
    key, because the board's contract here is "verbatim" — a test that checked
    three interesting dials would pass a board that silently dropped the other
    nine, which is the failure this issue is about.
    """
    posted = await record(client, 1)
    run = await detail(client, posted["id"])
    assert run["review_panel"] == DIALS


async def test_the_floors_converged_was_decided_under_are_on_the_row(client):
    """The reason this key and not another (#626).

    A below-floor policy stop is `stopped`, `stop_confident` and NOT `converged`.
    Which findings were below the floor depends on `cleared_floor`, and a reader
    handed the verdict alone cannot tell a round that converged at a P4 floor from
    one that converged at P1. Both facts now sit on the same fetch.
    """
    posted = await record(client, 2, round_stop={
        "stop": True, "reason": "nothing above the fix floor", "confident": True,
        "converged": False, "veto": []})
    run = await detail(client, posted["id"])
    assert run["converged"] is False and run["stop_confident"] is True
    assert run["review_panel"]["fix_severity_floor"] == "P4"
    assert run["review_panel"]["round_trigger_floor"] == "P2"
    assert run["review_panel"]["low_severity_fix_lines"] == 40


async def test_an_absent_dial_set_is_null_and_an_empty_one_is_not(client):
    """Three states, on the rule every neighbouring field here follows.

    NULL is not an edge case on this column: the panel sends it on EVERY skip and
    every pre-flight refusal, deliberately, because those paths resolve a review
    policy and never apply one. Folding `{}` into it would make "this round ran
    under no dials it could name" indistinguishable from "this round applied no
    policy at all".
    """
    absent = await detail(client, (await record(client, 3, review_panel=None))["id"])
    assert absent["review_panel"] is None
    missing = payload(4)
    del missing["review_panel"]
    r = await client.post("/review", json=missing, headers=AGENT)
    assert r.status_code == 201, r.text
    assert (await detail(client, r.json()["id"]))["review_panel"] is None
    empty = await detail(client, (await record(client, 5, review_panel={}))["id"])
    assert empty["review_panel"] == {}


async def test_a_dial_this_board_has_never_heard_of_is_stored_anyway(client):
    """Opaque on purpose — the board must not learn the vocabulary (#305).

    Every other coercer on this model reads its value against a shared vocabulary
    and drops what is not in it. This one has no vocabulary and must not grow one:
    a dial added to the panel would otherwise be dropped here until somebody
    edited a tuple in this repository, which is the drift in the other direction.
    """
    posted = await record(client, 6, review_panel={**DIALS, "quorum_of_the_future": 3})
    run = await detail(client, posted["id"])
    assert run["review_panel"]["quorum_of_the_future"] == 3


async def test_a_dial_set_that_is_not_an_object_is_null_and_says_so(client):
    """`review_panel: "P2"` is a producer sending the wrong shape.

    Without a signal it would land on the NULL that means "this round applied no
    policy" — a true statement about a skip and a false one about a reviewed
    round, and the sender would have no way to tell which the board recorded.
    """
    posted = await record(client, 7, review_panel="P2")
    assert "review_panel" in posted["unreadable_fields"]
    assert (await detail(client, posted["id"]))["review_panel"] is None


async def test_an_oversized_dial_set_is_refused_whole_and_named(client):
    """Refused, not trimmed — and the refusal has its own key.

    Trimming is right for `changed_files`, where a shorter list is still a true
    list. It is wrong here: a reader checking a round's verdict against six of its
    twelve dials would be checking it against a policy that never ran. And the
    reason is reported under `review_panel_dropped` rather than folded into
    `unreadable_fields`, because an object refused for its SIZE is a different
    sender fault from one refused for its shape and wants a different fix.
    """
    huge = {f"dial_{n}": "x" * 64 for n in range(reviews.MAX_DIALS_CHARS // 32)}
    posted = await record(client, 8, review_panel=huge)
    assert "over the" in posted["review_panel_dropped"]
    assert "review_panel" not in posted.get("unreadable_fields", [])
    assert (await detail(client, posted["id"]))["review_panel"] is None


async def test_a_stored_dial_set_is_not_reported_as_dropped(client):
    """The negative half: the drop signal must not fire on the ordinary payload.

    A `review_panel_dropped` that were always set would make every one of the
    assertions above pass while telling every real sender its policy record had
    been refused.
    """
    posted = await record(client, 9)
    assert "review_panel_dropped" not in posted
    assert "review_panel" not in posted.get("unreadable_fields", [])


async def test_the_dial_set_is_not_carried_on_the_run_list(client):
    """Detail only, on `unread_files`' rule.

    Ingest bounds one of these at `MAX_DIALS_CHARS`; `GET /reviews?limit=500`
    would serialise five hundred of them. One run's policy is a fair payload, a
    page of policies is a config dump — and the list view already carries
    `converged`, so a caller that wants the pair fetches the round.
    """
    await record(client, 10)
    r = await client.get("/reviews", params={"repo": REPO}, headers=AGENT)
    assert r.status_code == 200, r.text
    runs = r.json()
    assert runs and all("review_panel" not in run for run in runs)


async def test_a_value_postgres_cannot_store_is_refused_at_ingest(client):
    """The 500 this column would otherwise have introduced (found by Codex).

    Python's JSON reader accepts the non-standard `NaN`, `Infinity` and
    `-Infinity` literals, so starlette parses a body carrying one and hands
    `ReviewIn` a float that Postgres will not take in JSONB. Every check in this
    module passed and the refusal happened at INSERT — a 500 on a panel round
    that had done nothing wrong, which is the opposite of "a dropped field says
    so". `json.dumps(..., allow_nan=False)` moves the refusal to where the sender
    can be told about it.
    """
    r = await client.post(
        "/review",
        content=b'{"repo": "acme/dials643", "pr": 11, "reviewed": true, '
                b'"review_panel": {"max_fix_growth": NaN}}',
        headers={**AGENT, "content-type": "application/json"})
    assert r.status_code == 201, r.text
    assert "NaN" in r.json()["review_panel_dropped"]
    assert (await detail(client, r.json()["id"]))["review_panel"] is None


async def test_a_nul_inside_a_dial_value_is_refused_at_ingest(client):
    """The same class, and it survives the `allow_nan` guard.

    Postgres refuses `\\u0000` inside a JSONB string — the one escape sequence its
    JSON type cannot represent — so a dial value carrying a NUL is another 500 at
    INSERT rather than a drop the sender is told about.
    """
    posted = await record(client, 12, review_panel={**DIALS, "reviewer_scope": "pr\x00x"})
    assert "NUL" in posted["review_panel_dropped"]
    assert (await detail(client, posted["id"]))["review_panel"] is None
