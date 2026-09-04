"""#732: the rungs and the floors a round stopped on reach the board.

``panel_rounds.round_stop`` returns 24 keys. ``StopIn`` declared 6. The other
eighteen were discarded by pydantic's default ``extra="ignore"`` on a
``populate_by_name=True`` model with no ``extra=`` — the same drop that took
``head_sha``, ``unread_files``, the provenance pair, ``converged`` (#626) and
``outstanding`` (#717), one tier further in than any of them.

``tests/test_payload_key_drift.py`` is what now fails when a nineteenth key is
nested, and it is where each of the eighteen is written down as either a field or
an exemption. **This module is the storage half**: that the twelve which became
fields actually survive a POST and come back out, in the shape the columns
promise.

Four properties, in the order they get broken:

1. **The three scalars ride every view and the nine objects do not.** ``preland``
   and any calibration read ``GET /reviews``; a floor and two counts are cheap
   enough to go there, and nine measurement blocks are the file dump ``rules`` and
   ``fix_pass`` are deferred to avoid.
2. **Counted, never carried.** ``new_below_trigger_floor`` arrives as a list of
   finding keys and is stored as its length, on ``outstanding_counts``' rule:
   ``len()`` of a list cannot disagree with that list, and the keys are on the
   round's own findings.
3. **NULL is never zero.** "No new finding fell under the floor" and "this
   producer does not measure it" are opposite readings of the same round, and only
   one of them argues for lowering the floor.
4. **Verbatim, uninterpreted, and never a 422.** Nothing here reads a rung's name
   or a floor's spelling, and a rung of the wrong shape costs that rung and never
   the run — this module's standing rule, on the newest fields to take a value
   from a hand-rolled caller.

The drift half — that ``STOP_RUNGS`` and ``StopIn``'s fields are the same nine the
panel actually nests — is in ``tests/test_payload_key_drift.py``, where both
halves are readable at once.
"""

from __future__ import annotations

# The module rather than the names off it, on `test_review_outstanding.py`'s
# reason: the vocabulary arrives with this feature, so a `from ... import` of one
# turns the red half of every other test here into a collection error.
from app.api import reviews

from .conftest import LAPTOP

REPO = "acme/rungs732"
AGENT = {**LAPTOP, "X-Agent-Instance": "e732e1"}


def rungs(**over) -> dict:
    """The nine measurement blocks as `round_stop` spells them, shortened to the
    fields that carry the argument: a measurement, and the `over`/`fired` split
    that keeps "the number crossed a limit" apart from "this is why the cycle
    stopped"."""
    blocks = {
        "fix_injection": {"rate": 0.4, "limit": 0.3, "over": True, "fired": False},
        "revert": {"kind": "ok", "range": "abc123..def456", "offered": False},
        "excision": {"kind": "ok", "count": 0, "why": None},
        "new_findings_not_falling": {"counts": [4, 4, 4], "streak": 3,
                                     "over": True, "fired": True},
        "unrefereed_fix": {"lines": 40, "armed": False, "over": False,
                           "fired": False},
        "guard_churn": {"lines": 12, "limit": None, "armed": False,
                        "over": False, "fired": False},
        "fix_budget": {"spent": 30, "limit": 40, "within": True, "breach": None,
                       "brief": None, "fired": False},
        "fix_surface": {"files": ["app/x.py"], "count": 1},
        "premises": {"limit": 2, "declared": 1, "repeated": [],
                     "undecidable": [], "undecidable_brake": False,
                     "wired": True, "stamped": 1, "retroactive": [],
                     "undeclared_rounds": []},
    }
    return {**blocks, **over}


def stop(**over) -> dict:
    """A `round_stop` carrying everything #732 bound, plus the six keys it
    deliberately did not."""
    verdict = {
        "stop": True, "reason": "no new findings", "confident": True,
        "converged": False, "veto": [],
        "outstanding": {"fixable": ["k1"], "below_floor": ["k2", "k3"],
                        "escalated": [], "narrowed": [], "declined": [],
                        "handed_to": "fixer", "why": "a fix pass can clear it"},
        # The three #732 stores as columns of their own.
        "cleared_floor": "P3",
        "new_below_trigger_floor": ["k4", "k5"],
        "repeated_below_trigger_floor": ["k6"],
        # ...the six it deliberately does not, sent anyway because the panel does
        # and a payload that omitted them would not be the payload under test.
        "escalated_outstanding": [], "declined_outstanding": [], "narrowed": [],
        "round": 2, "max_rounds": 3, "trigger_floor": "P3",
    }
    return {**verdict, **rungs(), **over}


def payload(pr: int, **over) -> dict:
    body = {
        "repo": REPO,
        "pr": pr,
        "pr_title": f"feat: thing {pr}",
        "base": "main",
        "reviewed": True,
        "judged": True,
        "judge_model": "opus",
        "round": 2,
        "cycle": "cyc-732",
        "reviewers_selected": ["claude"],
        "reviewers": {"claude": {"model": "sonnet", "ran": True}},
        "to_fix": [
            {"severity": "P2", "file": "app/x.py", "title": "off-by-one",
             "reviewers": ["claude"], "reason": "confirmed in diff"},
        ],
        "round_stop": stop(),
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


async def listed(client, run_id: int) -> dict:
    r = await client.get("/reviews", params={"repo": REPO}, headers=AGENT)
    assert r.status_code == 200, r.text
    return next(row for row in r.json() if row["id"] == run_id)


# ---- the round trip --------------------------------------------------------

async def test_the_rungs_survive_the_round_trip_verbatim(client):
    """The whole of the drop this issue is about: the panel publishes nine
    measurement blocks and, until now, every one of them hit the floor.

    Verbatim and uninterpreted, on `review_panel`'s terms. Nothing on this board
    reads a rung's name, compares a limit against a rate, or collapses a block's
    `over` onto its `fired` — that pair is the distinction `round_stop` publishes
    them for, and a stored copy that lost it would be worse than no copy.
    """
    got = await record(client, 7301)
    assert got.get("stop_rungs_dropped", "") == ""
    row = await detail(client, got["id"])
    assert row["stop_rungs"] == rungs()
    # The `over`/`fired` split survives on the one rung where they disagree, which
    # is the reading a consumer would otherwise get wrong about a clean round.
    assert row["stop_rungs"]["fix_injection"] == {
        "rate": 0.4, "limit": 0.3, "over": True, "fired": False}


async def test_the_floors_ride_every_view_and_the_rungs_ride_the_detail(client):
    """The cut this endpoint makes, and it is `fix_pass`' exactly: the scalars a
    POPULATION is calibrated on ride the run list, and the objects behind them ride
    the detail.

    #710 calibrates a trigger floor by asking "how many findings did this floor
    turn away across thousands of rounds", which detail-only would make one fetch
    per run to answer. Nine measurement blocks on a `GET /reviews?limit=500` would
    be the file dump `unread_files` is kept off the list view to avoid.
    """
    got = await record(client, 7302)
    for row in (await detail(client, got["id"]), await listed(client, got["id"])):
        assert row["cleared_floor"] == "P3"
        assert row["new_below_trigger_floor"] == 2
        assert row["repeated_below_trigger_floor"] == 1
    assert "stop_rungs" not in await listed(client, got["id"])
    assert "stop_rungs" in await detail(client, got["id"])


async def test_what_is_stored_of_a_below_floor_list_is_a_count_and_never_a_key(client):
    """Counted, not carried, on `outstanding_counts`' rule. The keys belong to the
    findings and are already on the round's own rows; a run list that serialised
    five hundred of these lists would be a file dump."""
    got = await record(client, 7303)
    row = await listed(client, got["id"])
    assert row["new_below_trigger_floor"] == 2
    assert "k4" not in repr(row)


async def test_an_empty_below_floor_list_is_a_zero_and_not_a_silence(client):
    """A round whose trigger floor turned nothing away measured that. NULL is a
    producer that never measured it, and a reader that could not tell them apart
    would read a floor holding back half a repo's findings as one holding back
    none."""
    got = await record(client, 7304,
                       round_stop=stop(new_below_trigger_floor=[],
                                       repeated_below_trigger_floor=[]))
    row = await listed(client, got["id"])
    assert row["new_below_trigger_floor"] == 0
    assert row["repeated_below_trigger_floor"] == 0


# ---- the silences, kept apart ----------------------------------------------

async def test_a_round_with_no_stop_record_stores_no_rung_and_no_floor(client):
    got = await record(client, 7310, round_stop=None)
    row = await detail(client, got["id"])
    assert row["stop_rungs"] is None and row["cleared_floor"] is None
    assert row["new_below_trigger_floor"] is None
    assert row["repeated_below_trigger_floor"] is None


async def test_a_producer_older_than_the_rungs_is_a_silence_and_not_a_drop(client):
    """Every panel before these keys sends a `round_stop` and no rung. That is NULL
    with nothing said about it — a drop signal there would cry wolf on every such
    round, which is what makes the signal worth having when it does fire."""
    got = await record(client, 7311,
                       round_stop={"stop": True, "reason": "dry", "confident": True,
                                   "veto": []})
    assert "stop_rungs_dropped" not in got
    row = await detail(client, got["id"])
    assert row["stop_rungs"] is None and row["cleared_floor"] is None


# ---- refused, and said out loud --------------------------------------------

async def test_a_rung_of_the_wrong_shape_costs_that_rung_and_says_so(client):
    """`guard_churn: 4` is one bug in one rung. Refusing the other eight over it
    would lose eight true measurements to report one false one — so the set is
    stored short and the sender is told which rung is missing from it.

    Named rather than dropped in silence, on `_outstanding_or_none`'s rule: an
    absent rung means "the producer sent none", so omitting a refused one would
    file it under the shape a producer too old to send that rung legitimately
    sends.
    """
    got = await record(client, 7320, round_stop=stop(guard_churn=4))
    assert "guard_churn" in got["stop_rungs_dropped"]
    row = await detail(client, got["id"])
    assert "guard_churn" not in row["stop_rungs"]
    assert row["stop_rungs"]["fix_budget"]["spent"] == 30


async def test_a_rung_set_over_the_cap_is_refused_whole_and_named(client):
    """The sharp end of `MAX_STOP_RUNGS_CHARS`. Refused entire rather than trimmed,
    on `_opaque_or_none`'s rule and this block's own reason: a rung set short a
    rung does not read as a smaller measurement, it reads as a round on which that
    rung did not fire — the flattering direction, on the eight fields #712 wants a
    cycle's series of."""
    huge = {"files": ["app/f%d.py" % n for n in range(20000)]}
    got = await record(client, 7321, round_stop=stop(fix_surface=huge))
    assert "over the" in got["stop_rungs_dropped"]
    assert "refused whole" in got["stop_rungs_dropped"]
    row = await detail(client, got["id"])
    assert row["stop_rungs"] is None
    # ...and the rest of the payload landed, which is what the rule is protecting.
    assert len(row["findings"]) == 1 and row["stopped"] is True


async def test_a_below_floor_count_where_the_contract_is_keys_is_null(client):
    """`new_below_trigger_floor: 3` is a producer sending a count where the
    contract is a list of finding keys. Storing the 3 would be this board believing
    a number it cannot check against anything — and the value it would be believed
    as is the one a calibration reads."""
    got = await record(client, 7322, round_stop=stop(new_below_trigger_floor=3))
    row = await listed(client, got["id"])
    assert row["new_below_trigger_floor"] is None
    # ...and its sibling, which arrived correctly, is unaffected.
    assert row["repeated_below_trigger_floor"] == 1


async def test_a_floor_or_a_count_this_board_cannot_read_is_named_back(client):
    """Raised by a second opinion on this branch, and it is this module's own rule
    turned on its newest fields: a dropped field says so.

    Without this, `cleared_floor: 7` and `new_below_trigger_floor: 3` stored NULL —
    the same value a producer too old to send either records — so a panel whose
    payload this board disbelieved would be told nothing, on the three fields a
    trigger-floor calibration reads. A round silently absent from a denominator is
    worse than one visibly absent from it.

    In `unreadable_fields` rather than a signal of their own, on `_word_or_none`'s
    rule: one word and two key lists, one way each to get them wrong, one remedy.
    Dotted, because a sender has to know which of the payload's two tiers this
    board was looking at.
    """
    got = await record(client, 7324,
                       round_stop=stop(cleared_floor=7, new_below_trigger_floor=3,
                                       repeated_below_trigger_floor="k6"))
    assert set(got["unreadable_fields"]) >= {
        "round_stop.cleared_floor",
        "round_stop.new_below_trigger_floor",
        "round_stop.repeated_below_trigger_floor"}
    row = await listed(client, got["id"])
    assert row["cleared_floor"] is None
    assert row["new_below_trigger_floor"] is None
    assert row["repeated_below_trigger_floor"] is None


async def test_a_floor_a_producer_never_sent_is_not_named_as_unreadable(client):
    """The other half, and the one that keeps the signal worth reading: a producer
    too old to nest these three is a silence, not a fault. A check that cried wolf
    on every pre-#732 round would be switched off within the week."""
    got = await record(client, 7325,
                       round_stop={"stop": True, "reason": "dry", "confident": True,
                                   "veto": []})
    assert not [f for f in got.get("unreadable_fields", [])
                if f.startswith("round_stop.")]


async def test_a_rung_block_of_the_wrong_shape_is_never_a_422(client):
    """This module's standing rule, on the newest fields to take a value from a
    hand-rolled caller: a payload is never refused over one field, because refusing
    it loses the findings, the scorecards and the accounts along with the bad
    value. Every rung on `StopIn` is typed `Any` for exactly this."""
    got = await record(client, 7323,
                       round_stop=stop(premises="two", fix_surface=[],
                                       cleared_floor=7))
    row = await detail(client, got["id"])
    assert row["cleared_floor"] is None
    assert "premises" not in row["stop_rungs"] and "fix_surface" not in row["stop_rungs"]
    assert len(row["findings"]) == 1 and row["stopped"] is True


async def test_the_six_keys_this_board_does_not_bind_change_nothing(client):
    """`escalated_outstanding`, `declined_outstanding`, `narrowed`, `round`,
    `max_rounds` and `trigger_floor` are sent on every real payload and are
    dropped on purpose — each is either the same value under a second name or a
    dial `review_panel` already holds.

    Pinned here because "decided to drop" has to stay a decision rather than
    becoming an accident: a round that sends all six stores exactly what a round
    that sends none does, and the value each of them duplicates is on the row from
    its own source.
    """
    without = {k: v for k, v in stop().items()
               if k not in ("escalated_outstanding", "declined_outstanding",
                            "narrowed", "round", "max_rounds", "trigger_floor")}
    full = await listed(client, (await record(client, 7330))["id"])
    bare = await listed(client, (await record(client, 7331,
                                             round_stop=without))["id"])
    for key in ("cleared_floor", "new_below_trigger_floor",
                "repeated_below_trigger_floor", "outstanding", "handed_to"):
        assert full[key] == bare[key]
    # `round` comes off the TOP level, which is why the nested copy is exempt.
    assert full["round"] == bare["round"] == 2


# ---- the coercers, directly ------------------------------------------------

def test_the_rungs_are_read_against_the_panels_own_nine_names():
    stored, why = reviews._stop_rungs_or_none(stop())
    assert why == ""
    assert set(stored) == set(reviews.STOP_RUNGS)


def test_a_key_that_is_not_a_rung_is_not_carried_into_the_column():
    """The column's contract is the nine blocks `STOP_RUNGS` names. A verdict field
    beside them — `stop`, `veto`, `outstanding` — has its own column and must not
    be stored a second time here, free to disagree with it."""
    stored, _ = reviews._stop_rungs_or_none(stop())
    assert "stop" not in stored and "outstanding" not in stored


def test_an_absent_block_is_a_silence_and_a_wrong_shaped_one_is_a_drop():
    """The two NULLs this coercer produces are not the same event, and only one of
    them is the sender's fault."""
    assert reviews._stop_rungs_or_none(None) == (None, "")
    assert reviews._stop_rungs_or_none({"stop": True}) == (None, "")
    stored, why = reviews._stop_rungs_or_none({"guard_churn": 4})
    assert stored is None and "guard_churn" in why


def test_the_second_value_answers_what_was_lost_and_not_whether_anything_was():
    """Three readings of one pair, on `_outstanding_or_none`'s contract: stored
    whole, stored short with a sentence, and refused entire."""
    assert reviews._stop_rungs_or_none(stop())[1] == ""
    stored, why = reviews._stop_rungs_or_none(stop(revert="ok"))
    assert stored is not None and "revert" in why and "revert" not in stored
    over = stop(fix_surface={"files": ["x" * reviews.MAX_STOP_RUNGS_CHARS]})
    assert reviews._stop_rungs_or_none(over)[0] is None


def test_a_below_floor_list_is_counted_and_anything_else_is_nobody_said():
    assert reviews._below_floor_count(["a", "b", "c"]) == 3
    assert reviews._below_floor_count([]) == 0
    for wrong in (3, "k1", {"n": 3}, None):
        assert reviews._below_floor_count(wrong) is None


def test_the_floor_is_stored_verbatim_against_no_vocabulary():
    """A severity floor is a repo dial this board holds opaquely. A spelling it has
    not met stores as itself, so a consumer grouping on it gets an extra group it
    can SEE — where a vocabulary test would silently store NULL, "the panel did not
    say", about a round that did."""
    for word in ("P1", "P4", "none", "P0"):
        assert reviews.StopIn(cleared_floor=word).cleared_floor == word
    for wrong in (7, "", "   ", None):
        assert reviews.StopIn(cleared_floor=wrong).cleared_floor is None
