"""#647: what a stored count was measured against, on the same row as the count.

`provenance_counts` has been stored since v2.26 and rides every view. Four of the
twenty-five keys `tests/test_payload_key_drift.py` listed as dropped-on-purpose are
the WORKING behind it, and nothing on the row said which:

* `fix_range_source` (#512) — `increment`, `compare` or `reconstructed`. The three
  are not one measurement: the increment drops a base-branch merge's files.
* `provenance_restored` (#559) — what the round declined to attribute because the
  cycle had already seen it. The filter that MOVES `introduced`.
* `rules` (#305) — which layer supplied each dial. `review_panel` is
  `Dials.as_dict()`, twelve settings, and `escalate_on` is not among them, so this
  is the only field on the row that records the threshold a round ran under.
* `scope` / `since_sha` — what the round actually reviewed, which is what makes the
  stored `diff_chars` comparable across a cycle's rounds.

So the pairing these tests pin is a stored NUMBER and its denominator on one
record. `tests/test_review_panel_dials.py` covers the dial values #643 stored; this
covers the provenance, and the split between the two files is the split between the
two issues rather than an arrangement of convenience.

The other half is what this board refuses to do with them. The two objects are
stored opaquely on `_opaque_or_none`'s terms — `app/api/dials.py` argues why a
second place that knew what a dial meant is the drift #305 exists to end, and a
board that read a key out of `provenance_restored` would be a second implementation
of #559's filter. The three scalars are stored against no vocabulary either, which
is the one decision here that runs against a neighbouring precedent (`pr_state`
coerces anything outside its set to NULL) and `reviews._word_or_none` argues at
length.
"""

from __future__ import annotations

# The module, not the names off it, for `test_review_panel_dials.py`'s reason: the
# bounds arrive with this feature, so a `from ... import` of one turns the red half
# of every other test in this file into a collection error.
from app.api import reviews

from .conftest import LAPTOP

REPO = "acme/prov647"
AGENT = {**LAPTOP, "X-Agent-Instance": "d647d7"}

ANCHOR = "a" * 40
HEAD = "b" * 40

#: What `panel_core.rules_record(cfg)` sends, spelled as it spells it and cut to
#: four dials. `escalate_on.fix_injection` is here on purpose: it is the dial #637
#: recalibrates, it is NOT in `review_panel`, and this is the only place a round
#: records the value it ran under.
RULES = {
    "from": "`.harness-rules.sample` on origin/main",
    "baseline": ".harness-rules.sample",
    "unreadable": False,
    "dials_from": "https://qb.fo.ls",
    "dials_unreadable": False,
    "dials": {
        "review_panel.escalate_on.fix_injection": {
            "value": 0.5, "layer": "defaults", "source": "harness_rules.DEFAULTS"},
        "review_panel.max_rounds": {
            "value": 6, "layer": "board", "source": "board", "scope": "repo",
            "reason": "the cycle needs headroom while #637 recalibrates",
            "set_by": "rich", "expires_at": "2026-12-31T00:00:00+00:00"},
        "review_panel.fix_severity_floor": {
            "value": "P4", "layer": "sample", "source": ".harness-rules.sample"},
        "reviewers.claude.model": {
            "value": "opus", "layer": "overlay", "source": ".harness-rules"},
    },
}

#: The six characters an escape sequence SPELLS, and one real NUL. Named
#: rather than written inline because a real NUL in a test body is invisible
#: in a diff, and the whole point of the pair is that they are different.
ESCAPED_NUL = "\\" + "u0000"
REAL_NUL = chr(0)

#: `panel_scope`'s block, spelled as it spells it. `files` is a COUNT and not a
#: list of paths, which is what keeps this field small enough to bound tightly.
RESTORED = {"count": 12, "files": 3, "rounds": [1, 2], "unread": [], "why": None}


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
        "head_sha": HEAD,
        "provenance_counts": {"introduced": 4, "missed": 1},
        "rules": RULES,
        "provenance_restored": RESTORED,
        "fix_range_source": "increment",
        "scope": "increment",
        "since_sha": ANCHOR,
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


async def test_the_provenance_survives_the_round_trip_verbatim(client):
    """The whole point: what the round measured against is readable off it after.

    The two objects asserted as one equality each rather than key by key, because
    the board's contract is "verbatim" — a test that checked three interesting keys
    would pass a board that silently dropped the rest, which is the failure this
    issue is about.
    """
    run = await detail(client, (await record(client, 1))["id"])
    assert run["rules"] == RULES
    assert run["provenance_restored"] == RESTORED
    assert run["fix_range_source"] == "increment"
    assert run["scope"] == "increment"
    assert run["since_sha"] == ANCHOR


async def test_the_threshold_the_round_ran_under_is_on_the_row(client):
    """The key with the deadline, asserted by name (#637).

    `review_panel` is `Dials.as_dict()` — the twelve #165/#297 settings — and
    `escalate_on` is not among them, so before this column no round recorded the
    `fix_injection` threshold it ran under. A recalibration against a population
    that does not say what the dial was set to during each round is guesswork with
    extra steps, and the round payload that carried it is a temp file on whatever
    host ran the panel.
    """
    run = await detail(client, (await record(client, 2))["id"])
    dial = run["rules"]["dials"]["review_panel.escalate_on.fix_injection"]
    assert dial["value"] == 0.5
    assert dial["layer"] == "defaults"


async def test_a_board_dial_keeps_its_reason_and_its_expiry(client):
    """#305's other half: a dial in force whose argument nobody can read is a dial
    nobody can decide to remove.

    Asserted separately from the round trip above because it is the part that makes
    `rules` bigger than `review_panel` by two orders — the reason field is bounded
    at `MAX_REASON` by this same board — and therefore the part a size bound would
    be tempted to trim.
    """
    run = await detail(client, (await record(client, 3))["id"])
    dial = run["rules"]["dials"]["review_panel.max_rounds"]
    assert dial["layer"] == "board" and dial["set_by"] == "rich"
    assert "recalibrates" in dial["reason"]
    assert dial["expires_at"] == "2026-12-31T00:00:00+00:00"


async def test_the_three_scalars_ride_the_run_list(client):
    """A recalibration reads a POPULATION, and these are what it slices it on.

    The rule `merge_base` and `base_sha` already state: a scalar whose whole point
    is cross-run comparison must not cost one fetch per run. `diff_chars` is on this
    view and is scope-dependent, so a caller reading the list without `scope` is
    comparing a whole PR against a commit range and cannot tell.
    """
    await record(client, 4)
    r = await client.get("/reviews", params={"repo": REPO}, headers=AGENT)
    assert r.status_code == 200, r.text
    runs = r.json()
    assert runs
    row = next(run for run in runs if run["pr"] == 4)
    assert row["scope"] == "increment"
    assert row["since_sha"] == ANCHOR
    assert row["fix_range_source"] == "increment"


async def test_the_two_objects_are_not_carried_on_the_run_list(client):
    """Detail only, on `review_panel`'s rule and `unread_files`' before it.

    `rules` is one entry per review dial — fifty-two on this repository — and
    ingest bounds it at `MAX_RULES_CHARS`, so `GET /reviews?limit=500` would
    serialise five hundred configuration records. The three scalars above are what
    a page of runs needs; the objects are what one run needs.
    """
    await record(client, 5)
    r = await client.get("/reviews", params={"repo": REPO}, headers=AGENT)
    assert r.status_code == 200, r.text
    runs = r.json()
    assert runs and all("rules" not in run for run in runs)
    assert all("provenance_restored" not in run for run in runs)


async def test_an_absent_block_is_null_and_an_empty_one_is_not(client):
    """Three states, on the rule every neighbouring field here follows.

    NULL is not an edge case on either column. `provenance_restored` is null on
    every round outside a cycle and on round 2, whose only prior round IS the
    anchor; `rules` is null on every round recorded before this column. Folding
    `{}` into that would make "the panel resolved a policy and named no layers"
    indistinguishable from "no panel ever said".
    """
    absent = await detail(
        client, (await record(client, 6, rules=None, provenance_restored=None))["id"])
    assert absent["rules"] is None and absent["provenance_restored"] is None
    missing = payload(7)
    del missing["rules"], missing["provenance_restored"]
    r = await client.post("/review", json=missing, headers=AGENT)
    assert r.status_code == 201, r.text
    got = await detail(client, r.json()["id"])
    assert got["rules"] is None and got["provenance_restored"] is None
    empty = await detail(
        client, (await record(client, 8, rules={}, provenance_restored={}))["id"])
    assert empty["rules"] == {} and empty["provenance_restored"] == {}


async def test_a_dial_path_this_board_has_never_heard_of_is_stored_anyway(client):
    """Opaque on purpose — the board must not learn the vocabulary (#305).

    Sharper for `rules` than for `review_panel`: interpreting a layer would mean
    learning the resolution ORDER too, and a dial the panel adds must land here
    without anybody editing a tuple in this repository.
    """
    grown = {**RULES, "dials": {**RULES["dials"],
                                "review_panel.quorum_of_the_future": {
                                    "value": 3, "layer": "board", "source": "board",
                                    "scope": "fleet", "reason": "why not",
                                    "set_by": "nobody", "expires_at": None}}}
    run = await detail(client, (await record(client, 9, rules=grown))["id"])
    assert run["rules"]["dials"]["review_panel.quorum_of_the_future"]["value"] == 3


async def test_a_fix_range_source_this_board_has_not_heard_of_is_stored_verbatim(client):
    """The decision that runs against `pr_state`'s precedent, pinned as behaviour.

    `pr_state` coerces anything outside `PR_STATES` to NULL, and rightly: GitHub's
    states are a foreign, stable set and a variant spelling reclassifies a PR in the
    direction that hides work. Neither half holds here. #512 published two sources
    and #504 added `reconstructed`, so a frozen set on this board would have dropped
    the third on the release that introduced it — #647's own bug one layer down —
    and an unrecognised value forms its own group in a consumer's `GROUP BY` rather
    than being folded into one it cannot see.
    """
    posted = await record(client, 10, fix_range_source="tarball", scope="worktree")
    run = await detail(client, posted["id"])
    assert run["fix_range_source"] == "tarball"
    assert run["scope"] == "worktree"
    assert "fix_range_source" not in posted.get("unreadable_fields", [])


async def test_a_block_that_is_not_an_object_is_null_and_says_so(client):
    """`rules: "sample"` is a producer sending the wrong shape.

    Without a signal it would land on the NULL that means "the panel did not say",
    which is a claim about the payload's AGE rather than about this round — and the
    sender would have no way to tell which the board recorded.
    """
    posted = await record(client, 11, rules="sample", provenance_restored=[12])
    assert "rules" in posted["unreadable_fields"]
    assert "provenance_restored" in posted["unreadable_fields"]
    run = await detail(client, posted["id"])
    assert run["rules"] is None and run["provenance_restored"] is None


async def test_a_word_field_that_is_not_a_word_is_null_and_says_so(client):
    """One signal for all three ways to get a one-word field wrong.

    A non-string, a blank and an over-long value are the same sender fault wanting
    the same fix, which is why these are named in `unreadable_fields` and have no
    `*_dropped` key of their own — unlike the objects, where a wrong shape and an
    oversized record want different repairs.

    Refused rather than truncated: a truncated `increment` is `incre`, which is not
    a shorter true answer but a different false one.
    """
    posted = await record(client, 12, scope=7,
                          fix_range_source="x" * (reviews.MAX_SCOPE_CHARS + 1))
    assert "scope" in posted["unreadable_fields"]
    assert "fix_range_source" in posted["unreadable_fields"]
    run = await detail(client, posted["id"])
    assert run["scope"] is None and run["fix_range_source"] is None


async def test_a_blank_scope_is_not_stored_as_a_scope(client):
    """`scope: "  "` is not a word either, and it is the one of the three that
    would otherwise round-trip: a whitespace string is a `str`, so only the strip
    catches it, and a stored `"  "` would sit in a consumer's `GROUP BY` as a
    fourth scope nobody can look up."""
    posted = await record(client, 13, scope="  ")
    assert "scope" in posted["unreadable_fields"]
    assert (await detail(client, posted["id"]))["scope"] is None


async def test_an_oversized_rules_record_is_refused_whole_and_named(client):
    """Refused, not trimmed — and the refusal has its own key.

    Trimming is right for `changed_files`, where a shorter list is still a true
    list. It is wrong here: `rules` is a mapping from dial to source, so half of it
    leaves a reader with dials whose layer is absent and no way to tell that from a
    dial the resolver never saw. The reason is reported under `rules_dropped` rather
    than folded into `unreadable_fields`, because an object refused for its SIZE is a
    different sender fault from one refused for its shape.
    """
    huge = {**RULES, "dials": {f"review_panel.dial_{n}": {"value": "x" * 64}
                               for n in range(reviews.MAX_RULES_CHARS // 32)}}
    posted = await record(client, 14, rules=huge)
    assert "over the" in posted["rules_dropped"]
    assert "rules" not in posted.get("unreadable_fields", [])
    assert (await detail(client, posted["id"]))["rules"] is None


async def test_an_oversized_restored_block_is_refused_whole_and_named(client):
    """The same, on its own key and at its own bound.

    Two constants and not one: `rules` is bounded two orders higher, so a single
    "one of your blocks was too big" would send a producer to the wrong field with
    the wrong idea of the limit it broke.
    """
    huge = {**RESTORED, "why": "x" * (reviews.MAX_RESTORED_CHARS + 1)}
    posted = await record(client, 15, provenance_restored=huge)
    assert "over the" in posted["provenance_restored_dropped"]
    assert "rules_dropped" not in posted
    assert (await detail(client, posted["id"]))["provenance_restored"] is None


async def test_a_stored_payload_is_not_reported_as_dropped(client):
    """The negative half: none of the five drop signals fires on an ordinary round.

    A `rules_dropped` that were always set would make every assertion above pass
    while telling every real sender its provenance record had been refused.
    """
    posted = await record(client, 16)
    for key in ("rules_dropped", "provenance_restored_dropped", "since_sha_dropped"):
        assert key not in posted, key
    assert not posted.get("unreadable_fields")


async def test_a_garbled_anchor_is_dropped_and_named(client):
    """`since_sha` goes through `_sha_or_none` with the other three commit ids.

    Named on its own rather than under one "a commit id was refused" flag, for
    `base_sha_dropped`'s reason and one more: a round whose `scope` says `increment`
    and whose anchor the board silently refused says it reviewed an increment of
    nothing in particular.
    """
    posted = await record(client, 17, since_sha="HEAD~1")
    assert posted["since_sha_dropped"] == "HEAD~1"
    run = await detail(client, posted["id"])
    assert run["since_sha"] is None and run["scope"] == "increment"


async def test_the_anchor_is_normalised_like_every_other_commit_id(client):
    """One rule for both ends of the range, which is why they share a validator.

    Under increment scope the round's target is `since_sha...head_sha`. A head end
    lower-cased on the way in and an anchor stored as sent would make that range
    compare a normalised value against a raw one at read time.
    """
    posted = await record(client, 18, since_sha="  " + ANCHOR.upper() + "  ")
    run = await detail(client, posted["id"])
    assert run["since_sha"] == ANCHOR
    assert "since_sha_dropped" not in posted


async def test_a_value_postgres_cannot_store_is_refused_at_ingest(client):
    """The 500 these columns would otherwise have introduced.

    Python's JSON reader accepts the non-standard `NaN`, `Infinity` and `-Infinity`
    literals, so starlette parses a body carrying one and hands `ReviewIn` a float
    Postgres will not take in JSONB. Every check in the module passed and the
    refusal happened at INSERT — a 500 on a panel round that had done nothing wrong.
    Found on `review_panel` in #643; the shared coercer is what carries the fix onto
    these two rather than it having to be found again.
    """
    r = await client.post(
        "/review",
        content=b'{"repo": "acme/prov647", "pr": 19, "reviewed": true, '
                b'"rules": {"dials": {"a": {"value": NaN}}}}',
        headers={**AGENT, "content-type": "application/json"})
    assert r.status_code == 201, r.text
    assert "NaN" in r.json()["rules_dropped"]
    assert (await detail(client, r.json()["id"]))["rules"] is None


async def test_a_nul_inside_a_restored_reason_is_refused_at_ingest(client):
    """The same class on the other column, and it survives the `allow_nan` guard.

    Postgres refuses `\\u0000` inside a JSONB string — the one escape sequence its
    JSON type cannot represent — so a `why` carrying a NUL is another 500 at INSERT
    rather than a drop the sender is told about.
    """
    posted = await record(client, 20,
                          provenance_restored={**RESTORED, "why": "no checkout\x00"})
    assert "NUL" in posted["provenance_restored_dropped"]
    assert (await detail(client, posted["id"]))["provenance_restored"] is None


async def test_a_reason_that_quotes_an_escape_sequence_is_not_a_nul(client):
    """The NUL check must not fire on a string that merely SPELLS one (Codex).

    #643 looked for the six characters of the escape in `json.dumps`' output,
    which cannot tell a real NUL from a string containing that text: the dump
    writes a real NUL as those six characters and writes the literal text as
    seven, with the backslash doubled — and the seven contain the six.

    It matters here rather than being a curiosity. `rules` carries board-dial
    reasons written by people, and a reason quoting an escape sequence would have
    had the WHOLE record refused: fifty-two dials' provenance lost, permanently,
    to a NUL that was never there. The refusal is whole-object by design, which is
    exactly what makes a false positive expensive.
    """
    reason = "the panel logged a literal " + ESCAPED_NUL + " and we raised it"
    quoted = {**RULES, "dials": {**RULES["dials"], "review_panel.max_rounds": {
        **RULES["dials"]["review_panel.max_rounds"], "reason": reason}}}
    posted = await record(client, 21, rules=quoted)
    assert "rules_dropped" not in posted
    run = await detail(client, posted["id"])
    assert run["rules"] == quoted


async def test_a_nul_in_a_dial_path_is_refused(client):
    """...and the walk reaches KEYS, which is the dump scan's only real advantage.

    A dial path carrying a NUL fails at INSERT exactly as a value does, so the
    check that replaced the scan has to look at both.
    """
    posted = await record(client, 22,
                          rules={**RULES, "dials": {"a" + REAL_NUL + "b": {"value": 1}}})
    assert "NUL" in posted["rules_dropped"]
    assert (await detail(client, posted["id"]))["rules"] is None


def test_the_largest_column_is_deferred_and_the_scalars_are_not():
    """The read-path defence, asserted where it is actually made.

    `test_the_two_objects_are_not_carried_on_the_run_list` above only proves the
    JSON response does not carry `rules`. That is not the whole cost, and
    `unread_files` is the precedent: its first cut published a count computed as
    `len(r.unread_files)` in Python, so Postgres still shipped every path of every
    row and only the serialisation was saved. `rules` is bounded two orders above
    `review_panel`, which makes a `GET /reviews?limit=500` that fetched it the same
    mistake at a hundred times the size.

    The three scalars are asserted NOT deferred in the same breath, because they
    ride `_run_view` — a deferred column read there raises `MissingGreenlet` under
    async SQLAlchemy, so getting this backwards breaks the list view outright
    rather than merely slowing it.
    """
    from app.models.review import ReviewRun

    mapper = ReviewRun.__mapper__
    assert mapper.attrs["rules"].deferred, (
        "review_runs.rules is the largest column on this table and no list view "
        "publishes it — undeferring it makes GET /reviews fetch every one")
    for name in ("scope", "since_sha", "fix_range_source", "provenance_restored"):
        assert not mapper.attrs[name].deferred, name
