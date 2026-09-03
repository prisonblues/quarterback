"""#717: what a round left behind reaches the board, so a merge gate can read it.

``preland`` decides whether a pull request may be merged, and its finding clause
was ``elif confirmed: HOLD`` — any nonzero judge-confirmed count, whatever the
findings were. That contradicts ``review_panel.fix_severity_floor``, whose entire
content is that findings below it are reported and not fixed here (#165), so a
repository running a raised floor could essentially never reach READY.

The answer had existed since #42 and had no channel. ``round_stop.outstanding``
carries ``fixable`` / ``below_floor`` / ``escalated`` as separate key lists with
``handed_to`` over them, ``qb record-review`` POSTs the block, and ``ReviewIn`` is
``extra="ignore"`` — so ingest dropped it, exactly as it dropped ``head_sha``,
``unread_files``, the provenance pair and ``converged`` before it.

What is pinned here is the storage half. Three properties, in the order they get
broken:

1. **Counted, never split.** This board stores the LENGTH of each list the panel
   published and decides nothing about severity. The floors are repo dials it holds
   opaquely, and a board-side split would be a second reading of the policy that
   produced the verdict stored beside it — ``m6bc45ff1``'s argument for
   ``converged``, one column across.
2. **Whole or absent.** A disposal missing one of the three buckets a gate reads is
   refused entire and named back to the sender, because a block short one bucket
   looks complete and reads as a zero in it. NULL means "how much is owed is not
   recorded" and a consumer may never read it as zero.
3. **All-zero is a measurement.** A dry stop sends five empty lists and stores five
   zeros. That is a different fact from NULL, and collapsing the two is how a
   producer that predates the block would come to read as a clean round.

The gate half — HOLD on ``fixable + escalated``, warn on ``below_floor``,
escalations blocking at any severity — is in
``harness/loops/tests/test_preland.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from sqlalchemy import inspect as sa_inspect

# The module rather than the names off it, on `test_review_fix_pass.py`'s reason:
# the vocabularies arrive with this feature, so a `from ... import` of one turns
# the red half of every other test here into a collection error.
from app.api import reviews
from app.models.review import ReviewRun

from .conftest import LAPTOP

REPO = "acme/outstanding717"
AGENT = {**LAPTOP, "X-Agent-Instance": "e717e1"}

#: The producer's own copy of the block, read as source rather than imported — see
#: :func:`_panel_outstanding`.
PANEL_ROUNDS = (Path(__file__).resolve().parent.parent
                / "harness" / "loops" / "panel_rounds.py")


def block(**over) -> dict:
    """``round_stop.outstanding`` as the panel spells it: a mixed round with real
    work, one escalation a fixer may never touch, and two below the floor."""
    disposal = {"fixable": ["k1", "k2"], "below_floor": ["k3", "k4"],
                "escalated": ["k5"], "narrowed": [], "declined": ["k2"],
                "handed_to": "fixer", "why": "a fix pass can clear them"}
    return {**disposal, **over}


def stop(**over) -> dict:
    return {"stop": True, "reason": "dry", "confident": True, "converged": False,
            "veto": [], "outstanding": block(), **over}


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
        "cycle": "cyc-717",
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

async def test_the_disposal_survives_the_round_trip_on_both_views(client):
    """The whole of the drop this issue is about: the panel sends its disposal and
    every read path can see it afterwards.

    On the run LIST as well as the detail, and that is the load-bearing half:
    `preland` fetches `GET /reviews?repo=…&pr=…` and rules on the newest row, so a
    detail-only field would be a field it cannot reach.
    """
    got = await record(client, 7001)
    for row in (await detail(client, got["id"]), await listed(client, got["id"])):
        assert row["outstanding"] == {"fixable": 2, "below_floor": 2,
                                      "escalated": 1, "narrowed": 0, "declined": 1}
        assert row["handed_to"] == "fixer"


async def test_what_is_stored_is_a_count_and_never_a_key(client):
    """Counted, not carried. The keys belong to the findings, and a run LIST that
    serialised five hundred of these lists would be the file dump `unread_files` is
    detail-only to avoid."""
    got = await record(client, 7002)
    row = await listed(client, got["id"])
    assert all(isinstance(n, int) for n in row["outstanding"].values())
    assert "k1" not in repr(row["outstanding"])


async def test_a_dry_stop_records_five_zeros_and_not_a_silence(client):
    """All-zero is a measurement: the round ran its disposal and had nothing to
    dispose of. NULL is a producer that never sent one, and a reader that could not
    tell them apart would read every pre-#717 round as a clean one."""
    got = await record(client, 7003, to_fix=[],
                       round_stop=stop(outstanding={
                           "fixable": [], "below_floor": [], "escalated": [],
                           "narrowed": [], "declined": [], "handed_to": "nobody",
                           "why": "nothing is outstanding"}))
    row = await listed(client, got["id"])
    assert row["outstanding"] == {"fixable": 0, "below_floor": 0, "escalated": 0,
                                  "narrowed": 0, "declined": 0}
    assert row["handed_to"] == "nobody"


# ---- the three silences, kept apart ----------------------------------------

async def test_a_round_with_no_stop_record_stores_no_disposal(client):
    got = await record(client, 7010, round_stop=None)
    row = await listed(client, got["id"])
    assert row["outstanding"] is None and row["handed_to"] is None


async def test_a_producer_older_than_the_block_is_a_silence_and_not_a_drop(client):
    """Every panel before #42 sends a `round_stop` and no disposal. That is NULL with
    nothing said about it — a drop signal there would cry wolf on every such round."""
    got = await record(client, 7011,
                       round_stop={"stop": True, "reason": "dry", "confident": True,
                                   "veto": []})
    assert "outstanding_dropped" not in got
    row = await listed(client, got["id"])
    assert row["outstanding"] is None and row["handed_to"] is None


async def test_a_round_going_again_keeps_its_counts_and_hands_them_to_nobody(client):
    """`handed_to` is null mid-cycle by design (#42): the counts are true of the round
    either way and the verdict belongs to a round that is ENDING one. The row's own
    `stopped` is what tells that silence from a producer that never said."""
    got = await record(client, 7012,
                       round_stop=stop(stop=False, converged=False,
                                       outstanding=block(handed_to=None, why=None)))
    row = await listed(client, got["id"])
    assert row["outstanding"]["fixable"] == 2
    assert row["handed_to"] is None and row["stopped"] is False


# ---- refused whole ---------------------------------------------------------

async def test_a_disposal_missing_a_gated_bucket_is_refused_entire(client):
    """The sharp end of `OUTSTANDING_REQUIRED`. A block with `escalated` dropped does
    not read as a smaller remainder — it reads as a round with no escalations, which
    is the one class of finding no fix pass may touch, gone in the flattering
    direction on the field that decides whether a PR may merge."""
    short = {k: v for k, v in block().items() if k != "escalated"}
    got = await record(client, 7020, round_stop=stop(outstanding=short))
    assert "escalated" in got["outstanding_dropped"]
    assert "refused whole" in got["outstanding_dropped"]
    row = await listed(client, got["id"])
    assert row["outstanding"] is None


async def test_a_bucket_that_is_not_a_list_of_keys_refuses_the_block(client):
    """`fixable: 2` is a producer sending a count where the contract is keys. Storing
    the 2 would be this board believing a number it cannot check against anything."""
    got = await record(client, 7021, round_stop=stop(outstanding=block(fixable=2)))
    assert "fixable" in got["outstanding_dropped"]
    assert (await listed(client, got["id"]))["outstanding"] is None


async def test_a_disposal_of_the_wrong_shape_is_a_note_and_never_a_422(client):
    """This module's standing rule, on the newest field to take a value from a
    hand-rolled caller: a payload is never refused over one field, because refusing it
    loses the findings, the scorecards and the accounts along with the bad value.
    `StopIn.outstanding` is typed `Any` for exactly this."""
    got = await record(client, 7022, round_stop=stop(outstanding=[]))
    assert "not an object" in got["outstanding_dropped"]
    row = await detail(client, got["id"])
    assert row["outstanding"] is None
    # ...and the rest of the payload landed, which is what the rule is protecting.
    assert len(row["findings"]) == 1 and row["stopped"] is True


async def test_the_two_optional_buckets_do_not_cost_a_producer_its_disposal(client):
    """`narrowed` (#615) and `declined` (#665) postdate the block and no gate reads
    them, so a #42-era producer that sends neither still gets its three counted.
    Requiring them would refuse a whole disposal over two numbers nothing consults."""
    early = {k: v for k, v in block().items() if k not in ("narrowed", "declined")}
    got = await record(client, 7023, round_stop=stop(outstanding=early))
    assert "outstanding_dropped" not in got
    assert (await listed(client, got["id"]))["outstanding"] == {
        "fixable": 2, "below_floor": 2, "escalated": 1}


async def test_an_unreadable_optional_bucket_is_stored_short_and_said_out_loud(client):
    """Found by a second opinion on this PR rather than by the code it fixes.

    `narrowed: 5` is a producer sending a count where the contract is keys. The four
    readable buckets are still stored — refusing the whole disposal over a number no
    gate consults is the trade `test_the_two_optional_buckets_do_not_cost_a_producer
    _its_disposal` above makes — but the stored object is then missing a key, and a
    missing key here means "the producer sent none". Omitting it in silence would file
    a value this board refused under the shape a #42-era producer legitimately sends.
    """
    got = await record(client, 7025,
                       round_stop=stop(outstanding=block(narrowed=5, declined=None)))
    assert "narrowed" in got["outstanding_dropped"]
    assert "declined" not in got["outstanding_dropped"]
    row = await listed(client, got["id"])
    assert row["outstanding"] == {"fixable": 2, "below_floor": 2, "escalated": 1}


async def test_an_unknown_verdict_is_dropped_without_taking_the_counts_with_it(client):
    """`handed_to` is three-valued and its consumers branch on the three, so a fourth
    word would not appear as a fourth group — it would fall through every branch and
    be read as whichever the `else` is. It goes to NULL; the measurement beside it is
    a different claim and stands."""
    got = await record(client, 7024,
                       round_stop=stop(outstanding=block(handed_to="the operator")))
    row = await listed(client, got["id"])
    assert row["handed_to"] is None
    assert row["outstanding"]["fixable"] == 2


# ---- the coercers, directly ------------------------------------------------

def test_the_counts_are_lengths_and_nothing_else():
    counts, why = reviews._outstanding_or_none(block(fixable=["a"] * 9))
    assert counts["fixable"] == 9 and why == ""


def test_an_absent_block_is_a_silence_and_a_wrong_shaped_one_is_a_drop():
    """The two NULLs this coercer produces are not the same event, and only one of
    them is the sender's fault."""
    assert reviews._outstanding_or_none(None) == (None, "")
    counts, why = reviews._outstanding_or_none("fixable")
    assert counts is None and why


def test_the_second_value_answers_what_was_lost_and_not_whether_anything_was():
    """Three readings of one pair, and a caller that collapsed the middle one into
    either neighbour would either lose a whole disposal or lose a signal."""
    assert reviews._outstanding_or_none(block())[1] == ""
    counts, why = reviews._outstanding_or_none(block(declined="k2"))
    assert counts is not None and "declined" in why and "declined" not in counts
    assert reviews._outstanding_or_none(block(escalated="k5"))[0] is None


def test_an_empty_object_is_a_producer_that_sent_no_disposal():
    """`{}` does NOT survive as `{}` here, unlike the three tallies
    `_tally_or_none` serves. Those have a state meaning "the question does not
    arise"; a round always has a disposal, and `round_stop` says so with five empty
    lists — which land as five honest zeros one test up."""
    assert reviews._outstanding_or_none({})[0] is None


def test_the_verdict_is_read_against_the_panels_own_three_words():
    for word in reviews.HANDED_TO:
        assert reviews._handed_to_or_none({"handed_to": word}) == word
    for wrong in ("", "FIXER", "operator", 3, None):
        assert reviews._handed_to_or_none({"handed_to": wrong}) is None
    assert reviews._handed_to_or_none({}) is None


# ---- drift against the producer --------------------------------------------

def _panel_source() -> ast.Module:
    return ast.parse(PANEL_ROUNDS.read_text(encoding="utf-8"))


def _panel_outstanding() -> tuple[str, ...]:
    """The keys of ``round_stop``'s ``outstanding`` block, read out of the harness
    SOURCE by `ast`.

    Read rather than imported for ``tests/test_payload_key_drift.py``'s reason:
    ``harness/loops`` is installed without ``app/`` beside it, so an import here
    would be a test that only runs in a checkout — and a test that SKIPS when an
    import fails is a test that never runs anywhere, which is that file's own lesson
    and how the drift it exists to catch got in.

    Identified by the ``handed_to`` key inside it rather than by position, so a
    second dict in that file mentioning ``outstanding`` cannot be picked up instead.
    """
    found = []
    for node in ast.walk(_panel_source()):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and key.value == "outstanding"
                    and isinstance(value, ast.Dict)
                    and any(isinstance(k, ast.Constant) and k.value == "handed_to"
                            for k in value.keys)):
                found.append(tuple(k.value for k in value.keys
                                   if isinstance(k, ast.Constant)))
    assert len(found) == 1, f"read {found!r} out of the panel — scan broke?"
    return found[0]


def _panel_verdicts() -> set[str]:
    """Every word ``round_stop`` assigns to its ``handed_to`` local."""
    words = set()
    for node in ast.walk(_panel_source()):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "handed_to"
                   for t in node.targets):
            continue
        words |= {n.value for n in ast.walk(node.value)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return words


def test_the_counted_vocabulary_matches_THE_PANELS_OWN_BLOCK():
    """The drift check, and this suite is where it belongs: `harness/loops` is
    installed without `app/` beside it, so the two halves are readable at once
    nowhere else.

    A bucket the panel adds and this vocabulary does not name is a list the board
    silently declines to count, which is the shape `converged` had. Against the
    PRODUCER's own dict rather than against this file's fixture, which is the whole
    difference between a drift check and a restatement.
    """
    panel = _panel_outstanding()
    assert set(reviews.OUTSTANDING_COUNTS) | {"handed_to", "why"} == set(panel)
    # ...and the fixture is the panel's shape, so the rest of this file is exercising
    # the block the panel actually sends.
    assert set(block()) == set(panel)


def test_every_gated_bucket_is_one_the_panel_publishes():
    """The three a merge gate rules on are a subset of what is counted, and what is
    counted is a subset of what the panel sends. A required bucket the panel stopped
    sending would refuse every disposal on the board, silently, and hold every PR."""
    assert set(reviews.OUTSTANDING_REQUIRED) <= set(reviews.OUTSTANDING_COUNTS)
    assert set(reviews.OUTSTANDING_REQUIRED) <= set(_panel_outstanding())


def test_the_verdict_vocabulary_matches_the_panels_own_branches():
    assert _panel_verdicts() == set(reviews.HANDED_TO)


def test_the_columns_are_on_the_mapper_under_the_names_the_view_publishes():
    """Read off the mapper rather than off a migration, so a column renamed later
    trips this rather than the first `GET /reviews` after a deploy."""
    cols = {c.key for c in sa_inspect(ReviewRun).columns}
    assert {"outstanding_counts", "handed_to"} <= cols
