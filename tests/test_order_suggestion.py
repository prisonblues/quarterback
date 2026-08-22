"""#232's deterministic slice: an order the rules derive, and the part they cannot.

#232 asks for one agent that owns the plan's order and is told what its last
orders cost. The half that needs no agent is the half tested here — and it is the
half that has to exist first, because "build the agent, then measure it" has
nothing to measure against: every ordering opinion this fleet ever formed was
spoken in a session and lost with it.

So the properties under test are the ones that make a machine-proposed order safe
to publish beside a human-owned one:

* **It never writes the live sequence.** A suggestion is a read; putting it into
  force stays ``POST /plan/reorder``, which is human-only. That is what lets this
  ship while #183 is unsettled — an agent that may silently rewrite the sequence
  is an agent with human privileges.
* **It says which parts it derived and which it could not.** An order whose
  derived and judged halves are indistinguishable gets trusted uniformly, usually
  too much. ``basis`` is per item and the ambiguous set is a field.
* **No placement is chosen by a coin.** Ties fall back to the order in force, so a
  scope where no rule fires comes back untouched — and every crossing no rule
  ordered is labelled ``displaced`` at both ends rather than happening in silence,
  because applying a rule to a pair with something between them has to shift it.
* **Absent evidence is named, never treated as good news.** #101's failure mode
  exactly: a rival "read by a caller as answered, and disjoint". Most of a plan
  references issues and has no review state at all, and the response says so.
* **One PR is answered for by its newest run, full stop.** #101 found the same
  defect twice — a predicate composed in front of the newest-run selection
  resurrecting a stale one — so the selection here takes no predicate about a
  run's state and every reading is taken afterwards.
* **Every proposal is recorded with its evidence.** That is the prediction side
  of the ledger; the outcome side is #232's remaining half and is absent rather
  than stubbed.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, update

import app.api.plan as plan_api
from app.api.plan import EVIDENCE_STALE_DAYS, STALE_DAYS
from app.db import async_session
from app.models.order_proposal import OrderProposal
from app.models.plan_item import PlanItem
from app.models.review import ReviewRun
from app.ordering import (
    RULES_VERSION,
    Candidate,
    moves_between,
    rule_inputs,
    suggest_order,
)

from .conftest import LAPTOP, PINNED_SETTINGS

#: Fleet-scope (repo-less) plan items this module creates, so that it can take
#: them out again. Every other scope here is namespaced by a made-up repo name and
#: is invisible to anybody else; ``repo=None`` is the one scope that cannot be,
#: and a repo read WIDENS into it by design — so a fleet row left behind is three
#: extra items in every other suite's "what is next" assertion. It is not
#: hypothetical: it broke seven tests in `test_plan_items.py`, none of which
#: mention this file.
_FLEET: list[str] = []


@pytest.fixture(autouse=True)
async def _leave_the_fleet_scope_as_it_was(client):
    """Delete this module's fleet-scope rows after every test that made one.

    By id, not by predicate, for the plan items — deleting "every repo-less item"
    would take another suite's rows with it and move the breakage rather than fix
    it. The proposals go by scope because the ledger is this release's table and
    nothing else writes to it yet, and a proposal whose item is gone is a row no
    later read can interpret.
    """
    yield
    if not _FLEET:
        return
    ids = [uuid.UUID(i) for i in _FLEET]
    _FLEET.clear()
    async with async_session() as s:
        await s.execute(delete(OrderProposal).where(OrderProposal.repo.is_(None)))
        await s.execute(delete(PlanItem).where(PlanItem.id.in_(ids)))
        await s.commit()


#: A person, as the edge proves it — the identity header AND the secret only the
#: proxy knows. Needed because the only way a suggestion becomes the order is a
#: human-only endpoint, and a test that never tries it has not shown that.
HUMAN = {"Remote-User": "rich", "X-Edge-Auth": PINNED_SETTINGS["HUMAN_EDGE_SECRET"]}
AGENT = {**LAPTOP, "X-Agent-Instance": "ord01"}

#: A real-shaped commit id: ingest drops one that could not be resolved later
#: (`_sha_or_none`), so a short made-up token would have been stored as NULL and
#: this test would have asserted nothing about provenance.
HEAD_SHA = "c0ffee1c0ffee1c0ffee1c0ffee1c0ffee1c0ffe"


# --- the rules, against literals ------------------------------------------
#
# No database in this half. The rules are a pure function precisely so that every
# one of them can be stated as "these facts produce this order", which is the
# only form in which a rule can be argued with.


def keys(result) -> list[str]:
    return list(result.suggested_order)


def basis(result) -> dict[str, str]:
    return {p.key: p.basis for p in result.placements}


def separating(result, key: str) -> set[str]:
    return {r.rule for p in result.placements if p.key == key for r in p.reasons if r.separating}


def test_nothing_moves_when_no_rule_separates_anything():
    """The guarantee the whole feature rests on: a tie keeps the order in force.

    Three items the rules cannot tell apart come back in the order they arrived,
    every one of them labelled ``ambiguous``, and every one of them named as
    interchangeable with the others. A caller can therefore read `moves` as a
    list of things a rule asked for — never as what the coin came down on.
    """
    r = suggest_order([Candidate(key="a"), Candidate(key="b"), Candidate(key="c")])
    assert keys(r) == ["a", "b", "c"]
    assert r.changed is False and r.moves() == ()
    assert set(basis(r).values()) == {"ambiguous"}
    assert r.ambiguous == (("a", "b", "c"),)
    assert r.counts()["derived"] == 0
    assert r.counts()["interchangeable"] == 3
    # And nothing is displaced either, because nothing moved: the displacement note
    # exists for a move a rule elsewhere forced, and no rule fired here.
    assert not [x for p in r.placements for x in p.reasons if x.rule == "displaced"]


def test_a_single_item_is_unopposed_and_not_ambiguous():
    """Two different facts, and folding them would overstate the remainder.

    "Nothing separated this from its peer" and "there was no peer" both leave the
    rules with nothing to say, and only the first is a position a model could
    have an opinion about.
    """
    r = suggest_order([Candidate(key="only")])
    assert basis(r) == {"only": "unopposed"}
    assert r.ambiguous == () and r.counts()["ambiguous"] == 0


def test_an_item_waiting_on_an_open_blocker_sinks_and_the_reason_is_a_constraint():
    """The plan's own ``next`` already skips a blocked item, so sinking it asserts
    nothing new — which is what makes this a constraint rather than a policy."""
    r = suggest_order([Candidate(key="blocked", blocked=True),
                       Candidate(key="free1"), Candidate(key="free2")])
    assert keys(r) == ["free1", "free2", "blocked"]
    assert basis(r)["blocked"] == "constraint"
    assert separating(r, "free1") == {"blocked"}


def test_a_second_item_in_a_sunk_bucket_still_says_what_sank_it():
    """A bucket sinks as a bucket, and every member of it was put there by the same
    rule. The first version looked for a separation only against the item emitted
    before and the best rival still available, so the SECOND blocked item — tied
    with the first, with nothing un-emitted left to compare against — came back
    `ambiguous` with no reason at all, while the bucket rule had demonstrably put it
    below the workable one. Under-counted in `counts.derived` and unexplained in the
    entry (Codex, review pass five)."""
    r = suggest_order([Candidate(key="blocked1", blocked=True),
                       Candidate(key="blocked2", blocked=True),
                       Candidate(key="free")])
    assert keys(r) == ["free", "blocked1", "blocked2"]
    assert basis(r) == {"free": "constraint", "blocked1": "constraint",
                        "blocked2": "constraint"}
    assert separating(r, "blocked2") == {"blocked"}
    assert r.counts()["derived"] == 3 and r.counts()["ambiguous"] == 0


def test_a_dependency_is_repaired_and_labelled_a_constraint():
    """Topological repair asserts nothing (#183) — it removes a contradiction. An
    item cannot be above the thing it waits on, whatever any preference says."""
    r = suggest_order([Candidate(key="after", depends_on=("before",), blocked=True),
                       Candidate(key="before")])
    assert keys(r) == ["before", "after"]
    assert basis(r)["after"] == "constraint"
    assert "dependency" in separating(r, "after")


def test_a_dependency_outranks_every_preference():
    """The one rule enforced structurally rather than by a sort key. ``before`` is
    finished and would otherwise sink to the bottom; it cannot, because something
    still waits on it."""
    r = suggest_order([
        Candidate(key="after", depends_on=("before",), blocked=True, ci="FAIL"),
        Candidate(key="before", pr_state="MERGED"),
        Candidate(key="other"),
    ])
    assert keys(r) == ["other", "before", "after"]
    # And no reason claims otherwise. `after` sorts ABOVE `before` on the bucket
    # rule and the edge overrode it, so a separation reported for that pair would
    # be a sentence contradicting the list it is attached to — which is what
    # comparing every pair rather than every EARLIER pair would produce.
    details = [x.detail for p in r.placements if p.key == "before" for x in p.reasons]
    assert not [d for d in details if "placed after after" in d], details


def test_finished_work_sinks_below_work_that_is_merely_waiting():
    """Blocked work becomes workable; finished work never does. The bucket order is
    stated in the module and asserted here so it cannot drift silently."""
    r = suggest_order([Candidate(key="merged", pr_state="MERGED"),
                       Candidate(key="waiting", blocked=True),
                       Candidate(key="free")])
    assert keys(r) == ["free", "waiting", "merged"]
    # And the label distinguishes them: a blocker is a row in this database, a
    # merged PR is what a panel saw the last time it ran.
    assert basis(r)["waiting"] == "constraint"
    assert basis(r)["merged"] == "preference"


def test_a_closed_pr_counts_as_finished_too():
    r = suggest_order([Candidate(key="closed", pr_state="closed"), Candidate(key="open")])
    assert keys(r) == ["open", "closed"]


def test_red_ci_rises_because_the_work_is_already_identified():
    """The sign of this rule is the opposite of a landing queue's, and deliberately:
    red CI sinks a PR that is trying to merge and raises a plan item, because in a
    plan it is work that exists, is known, and is holding something up."""
    r = suggest_order([Candidate(key="green", ci="PASS"), Candidate(key="red", ci="FAIL")])
    assert keys(r) == ["red", "green"]
    assert basis(r)["red"] == "preference"
    assert separating(r, "red") == {"open_work"}


def test_a_confirmed_finding_nobody_has_answered_rises():
    r = suggest_order([Candidate(key="clean", outstanding_findings=0),
                       Candidate(key="open_findings", outstanding_findings=2)])
    assert keys(r) == ["open_findings", "clean"]


def test_an_answered_finding_does_not_rise():
    """``outstanding_findings`` is the count nobody has recorded an outcome for, so
    a round with three confirmed findings and three outcomes orders nothing."""
    r = suggest_order([Candidate(key="a", outstanding_findings=0),
                       Candidate(key="b", outstanding_findings=0)])
    assert keys(r) == ["a", "b"] and set(basis(r).values()) == {"ambiguous"}


def test_an_unrecognised_ci_string_is_neither_green_nor_red():
    """``ci_status`` is a free-text column an authenticated sender fills, and
    ``review_ci`` reports five words of which only two are compared. A rule that
    fired on a typo would be a rule firing on noise.

    The PASS candidate is what makes this bite: without it, "everything unknown
    rises" and "nothing unknown rises" produce the same order, and the test passes
    against a rule that treats any non-PASS string as red."""
    r = suggest_order([Candidate(key="green", ci="PASS"), Candidate(key="typo", ci="failed"),
                       Candidate(key="dunno", ci="unknown"), Candidate(key="silent", ci=None)])
    assert keys(r) == ["green", "typo", "dunno", "silent"]
    assert set(basis(r).values()) == {"ambiguous"}


def test_staleness_rises_only_once_it_crosses_the_threshold():
    """A graded rule with a stated threshold, so most ties stay ties. Age used as a
    plain tiebreak would separate almost everything and leave the ambiguous set —
    the output this slice exists to produce — permanently empty."""
    r = suggest_order([Candidate(key="fresh", idle_days=1.0),
                       Candidate(key="old", idle_days=20.0)], stale_days=14.0)
    assert keys(r) == ["old", "fresh"]
    assert separating(r, "old") == {"stale"}
    within = suggest_order([Candidate(key="a", idle_days=1.0),
                            Candidate(key="b", idle_days=13.9)], stale_days=14.0)
    assert keys(within) == ["a", "b"] and set(basis(within).values()) == {"ambiguous"}


def test_open_work_outranks_staleness():
    r = suggest_order([Candidate(key="stale", idle_days=99.0),
                       Candidate(key="red", ci="FAIL")], stale_days=14.0)
    assert keys(r) == ["red", "stale"]


def test_overlap_breaks_a_tie_only_when_the_query_has_actually_been_run():
    """Overlap is a refinement, not a prerequisite — which is what lets this ship
    while #101 is open. With it absent the pair stays ambiguous and nothing else
    about the order changes."""
    pair = [
        Candidate(key="pending", collides_with=("ready",), ci="PENDING",
                  outstanding_findings=0, draft=False, pr_state="OPEN"),
        Candidate(key="ready", collides_with=("pending",), ci="PASS",
                  outstanding_findings=0, draft=False, pr_state="OPEN"),
    ]
    unknown = suggest_order(pair)
    assert keys(unknown) == ["pending", "ready"]
    assert set(basis(unknown).values()) == {"ambiguous"}

    known = suggest_order(pair, overlap_known=True)
    assert keys(known) == ["ready", "pending"]
    assert basis(known)["ready"] == "preference"
    # Recorded on the one it demoted as well: a refinement that explains only the
    # winner is half a record, and the loser is what somebody will argue with.
    assert "overlap" in separating(known, "pending")


def test_overlap_never_moves_an_item_the_collision_is_not_about():
    """The first version of this rule readiness-sorted the whole tied group as soon
    as any pair in it collided, so a third item sharing no file with either could
    be moved — and came back labelled `overlap`, a reason that was not true of it.
    Codex found it on review; this is the case that holds it shut."""
    r = suggest_order([
        Candidate(key="bystander"),
        Candidate(key="slow", collides_with=("fast",), ci="PENDING",
                  outstanding_findings=0, draft=False, pr_state="OPEN"),
        Candidate(key="fast", collides_with=("slow",), ci="PASS",
                  outstanding_findings=0, draft=False, pr_state="OPEN"),
    ], overlap_known=True)
    # The bystander is at the head and stays there: no overlap fact is about it, so
    # the refinement has nothing to say and the active order stands.
    assert keys(r) == ["bystander", "fast", "slow"]
    assert separating(r, "bystander") == set()
    assert basis(r)["bystander"] == "ambiguous"
    # And the two that DO collide are separated, at the step where the readier one
    # reaches the head.
    assert "overlap" in separating(r, "fast")
    assert "overlap" in separating(r, "slow")


def test_overlap_never_reaches_across_two_disconnected_collisions():
    """The same defect as the test above, one level in — and it arrived as the fix
    for that one, which is why the rule is now stated pairwise rather than narrowed
    again (#67).

    Two independent pairs in one tied group: A collides with A', B with B', and
    nothing crosses between them. Asking "which member of this group is readiest"
    lets B' jump the head of the A pair on a fact about the B pair. The question
    the data can answer is about a PAIR, so that is the only question asked.
    """
    r = suggest_order([
        Candidate(key="a_slow", collides_with=("a_fast",)),
        Candidate(key="b_fast", collides_with=("b_slow",), ci="PASS",
                  outstanding_findings=0, draft=False, pr_state="OPEN"),
        Candidate(key="a_fast", collides_with=("a_slow",), ci="PASS",
                  outstanding_findings=0, draft=False, pr_state="OPEN"),
        Candidate(key="b_slow", collides_with=("b_fast",)),
    ], overlap_known=True)
    # a_fast overtakes a_slow, and b_fast keeps the lead it already had over
    # b_slow — both claims about a pair that actually collides.
    assert keys(r) == ["a_fast", "a_slow", "b_fast", "b_slow"]
    # The ungrounded move is the one that does NOT happen: b_fast is the readiest
    # item in the group, and it stays behind a_slow, which it shares no file with.
    assert keys(r).index("a_slow") < keys(r).index("b_fast")
    for placement in r.placements:
        for reason in placement.reasons:
            if reason.rule == "overlap" and "placed after" in reason.detail:
                # Every overlap claim names a peer this item actually collides with.
                other = reason.detail.split("placed after ")[1].split(" —")[0]
                assert other.split("_")[0] == placement.key.split("_")[0], reason.detail


def test_a_confirmed_overlap_placement_is_derived_and_not_left_in_the_remainder():
    """The rule decided this head; it merely agreed with the order already in force.
    Reporting that as ``ambiguous`` would put a question the rules have answered
    into the remainder a model is asked about."""
    r = suggest_order([
        Candidate(key="fast", collides_with=("slow",), ci="PASS",
                  outstanding_findings=0, draft=False, pr_state="OPEN"),
        Candidate(key="slow", collides_with=("fast",)),
    ], overlap_known=True)
    assert keys(r) == ["fast", "slow"] and r.changed is False
    assert basis(r) == {"fast": "preference", "slow": "preference"}
    assert r.ambiguous == ()


def test_a_one_directional_collision_still_counts():
    """``collides_with`` comes from a caller's query, and a query that reported the
    relation one way round would otherwise make the order depend on which of the
    pair happened to be asked about."""
    r = suggest_order([
        Candidate(key="slow"),
        Candidate(key="fast", collides_with=("slow",), ci="PASS",
                  outstanding_findings=0, draft=False, pr_state="OPEN"),
    ], overlap_known=True)
    assert keys(r) == ["fast", "slow"]


def test_an_item_a_rule_displaces_says_so_instead_of_moving_silently():
    """The third and last instance of "a pair fact expressed over a set", and the
    one that is not fixable by narrowing: `slow, bystander, fast` with a rule that
    puts `fast` before `slow` has no answer in which only that pair inverts.

    So the guarantee is the exact one — no placement is chosen by a coin — and the
    bystander's move is labelled rather than left as an unexplained entry in
    `moves`, which in a proposal whose selling point is stated reasons is the one
    row a reader cannot check. Found by Codex on the third review pass.
    """
    r = suggest_order([
        Candidate(key="slow", collides_with=("fast",)),
        Candidate(key="bystander"),
        Candidate(key="fast", collides_with=("slow",), ci="PASS",
                  outstanding_findings=0, draft=False, pr_state="OPEN"),
    ], overlap_known=True)
    assert keys(r).index("fast") < keys(r).index("slow"), "the rule has to be applied"
    moved = {m["key"] for m in r.moves()}
    assert "bystander" in moved, "the fixture must actually displace it"
    note = next(x for p in r.placements if p.key == "bystander"
                for x in p.reasons if x.rule == "displaced")
    assert note.separating is False
    assert "fast" in note.detail and "does not compare them" in note.detail
    # Its basis is unchanged: nothing derived its position, and saying otherwise
    # would move it out of the remainder a model is asked about.
    assert basis(r)["bystander"] == "ambiguous"


def test_a_crossing_is_reported_even_when_the_item_has_a_rule_of_its_own():
    """The note is per PAIR, not per item. A first version suppressed it whenever
    the item had any separating reason, so an item pinned by a dependency edge and
    then crossed by an unrelated overlap promotion reported the dependency and said
    nothing about the crossing — two different claims, one of them missing.

    `bystander` precedes `waiter`, which is why it has a `dependency` reason; that
    reason says nothing about `fast` going past it. Codex, review pass four.
    """
    r = suggest_order([
        Candidate(key="slow", collides_with=("fast",)),
        Candidate(key="bystander"),
        Candidate(key="fast", collides_with=("slow",), ci="PASS",
                  outstanding_findings=0, draft=False, pr_state="OPEN"),
        Candidate(key="waiter", depends_on=("bystander",), blocked=True),
    ], overlap_known=True)
    assert keys(r) == ["fast", "slow", "bystander", "waiter"]
    rules = {x.rule for p in r.placements if p.key == "bystander" for x in p.reasons}
    assert "dependency" in rules, "the fixture must give it a rule of its own"
    assert "displaced" in rules, "and the crossing must still be reported"
    # Both ends of an unexplained inversion say so, not just the one that lost.
    assert "displaced" in {x.rule for p in r.placements if p.key == "fast" for x in p.reasons}


def test_a_crossing_a_rule_ordered_is_not_reported_as_displacement():
    """The note is for inversions nothing accounts for. A blocked item sinking below
    every workable one inverts several pairs and the bucket rule ordered every one
    of them, so a note there would be noise on the ordinary case — and noise is how
    a real one goes unread."""
    r = suggest_order([Candidate(key="blocked", blocked=True),
                       Candidate(key="free1"), Candidate(key="free2")])
    assert keys(r) == ["free1", "free2", "blocked"] and r.changed is True
    assert not [x for p in r.placements for x in p.reasons if x.rule == "displaced"]
    # Nor is a dependency repair, and TRANSITIVELY: `first` crosses `last` with no
    # direct edge between them, and the walk ordered that pair just as firmly as the
    # two it does have an edge for. The chain carries no `blocked` flag on purpose —
    # with one, the bucket rule would separate the three and the reachability test
    # below would never be reached, which is how the first version of this
    # assertion passed against direct edges only.
    chain = suggest_order([Candidate(key="last", depends_on=("mid",)),
                           Candidate(key="mid", depends_on=("first",)),
                           Candidate(key="first")])
    assert keys(chain) == ["first", "mid", "last"]
    assert not [x for p in chain.placements for x in p.reasons if x.rule == "displaced"]


def test_overlap_says_nothing_about_two_items_that_share_no_file():
    r = suggest_order([
        Candidate(key="a", ci="PENDING", outstanding_findings=0, draft=False, pr_state="OPEN"),
        Candidate(key="b", ci="PASS", outstanding_findings=0, draft=False, pr_state="OPEN"),
    ], overlap_known=True)
    assert keys(r) == ["a", "b"] and set(basis(r).values()) == {"ambiguous"}


#: A candidate the overlap refinement WOULD promote: every clause of "closer to
#: landing" positively known. Each case below removes exactly one of them.
def _ready_pair(**missing) -> list[Candidate]:
    ready = {"ci": "PASS", "outstanding_findings": 0, "draft": False, "pr_state": "OPEN"}
    return [Candidate(key="rival", collides_with=("nearly",)),
            Candidate(key="nearly", collides_with=("rival",), **{**ready, **missing})]


def test_readiness_is_never_inferred_from_a_missing_fact():
    """The overlap refinement claims one item is closer to landing, and a fact
    nobody recorded is not evidence for that.

    One case per clause, because a single case proves only that *something* stopped
    the promotion. The first draft of this test left three of the four clauses free
    to be deleted with the test still passing — the fixture happened to be missing
    the finding count as well, so it was that clause doing the work every time.
    """
    promoted = suggest_order(_ready_pair(), overlap_known=True)
    assert keys(promoted) == ["nearly", "rival"], "the fixture must be promotable at all"
    for clause in ("ci", "outstanding_findings", "draft", "pr_state"):
        held = suggest_order(_ready_pair(**{clause: None}), overlap_known=True)
        assert keys(held) == ["rival", "nearly"], f"{clause}=None was read as ready"
    # An unrecognised status is not green either, for the same reason it is not red.
    typo = suggest_order(_ready_pair(ci="passed"), overlap_known=True)
    assert keys(typo) == ["rival", "nearly"]


def test_the_inputs_distinguish_unasked_overlap_from_no_overlap():
    """NULL is not ``[]`` — the distinction ``unread_files`` and ``stop_veto``
    already keep in ``review_runs``, and here it is the difference between "these
    two are unrelated" and "#101 is open so nobody asked"."""
    c = Candidate(key="a", collides_with=())
    assert rule_inputs(c, 14.0, overlap_known=False)["collides_with"] is None
    assert rule_inputs(c, 14.0, overlap_known=True)["collides_with"] == []


def test_a_dependency_cycle_is_reported_and_never_repaired():
    """Every repair is a guess about which edge was the wrong one. The plan API
    refuses a cycle at write time so a human decides that, and if one arrives here
    anyway the members keep the order in force and say what happened."""
    r = suggest_order([Candidate(key="a", depends_on=("b",)),
                       Candidate(key="b", depends_on=("a",)),
                       Candidate(key="free")])
    assert set(r.cycles) == {"a", "b"}
    assert keys(r)[0] == "free" and keys(r)[1:] == ["a", "b"]
    assert basis(r)["a"] == basis(r)["b"] == "unresolved"
    assert r.counts()["derived"] == 0


def test_an_edge_pointing_outside_the_set_is_not_an_edge_this_walk_can_honour():
    """It reaches the rules as ``blocked`` instead, which is the whole of what can
    be said about a dependency whose other end is not being ordered."""
    r = suggest_order([Candidate(key="a", depends_on=("elsewhere",), blocked=True),
                       Candidate(key="b")])
    assert keys(r) == ["b", "a"] and r.cycles == ()


def test_a_duplicate_key_is_refused_rather_than_silently_dropped():
    """A proposal that loses an item is worse than no proposal."""
    try:
        suggest_order([Candidate(key="a"), Candidate(key="a")])
    except ValueError as e:
        assert "duplicate" in str(e)
    else:  # pragma: no cover - the assertion is the point
        raise AssertionError("a duplicate key was accepted")


def test_the_digest_ignores_a_clock_tick_and_notices_a_fact():
    """The digest is what stops a cron floor filling the ledger with copies, so it
    is computed over the QUANTISED inputs: the rules cannot see anything finer
    than the staleness threshold, and a digest over ``idle_days`` would change
    every second and dedupe nothing."""
    a = suggest_order([Candidate(key="x", idle_days=1.0)], stale_days=14.0)
    b = suggest_order([Candidate(key="x", idle_days=2.5)], stale_days=14.0)
    crossed = suggest_order([Candidate(key="x", idle_days=30.0)], stale_days=14.0)
    assert a.inputs_digest == b.inputs_digest
    assert a.inputs_digest != crossed.inputs_digest


def test_the_digest_covers_the_sequence_it_was_proposed_against():
    """The same facts against a different incumbent order are a different
    proposal — otherwise a human reorder would look to the ledger like nothing
    had happened."""
    one = suggest_order([Candidate(key="a"), Candidate(key="b")])
    other = suggest_order([Candidate(key="b"), Candidate(key="a")])
    assert one.inputs_digest != other.inputs_digest


def test_moves_reports_an_item_present_in_only_one_of_the_two_orders():
    """Cannot happen for an ordering this module produced, and a stored row read
    back months later is data: a reader gets "these lists disagree about which
    items exist" rather than a KeyError."""
    m = {d["key"]: d for d in moves_between(["a", "b"], ["b", "c"])}
    assert m["a"] == {"key": "a", "from": 0, "to": None, "delta": None}
    assert m["c"] == {"key": "c", "from": None, "to": 1, "delta": None}


# --- the endpoints --------------------------------------------------------


async def add(client, repo: str, title: str, **over) -> dict:
    r = await client.post("/plan/item", json={"repo": repo, "title": title, **over},
                          headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


async def add_fleet(client, title: str, **over) -> dict:
    """A repo-less item, registered for cleanup — see :data:`_FLEET`."""
    r = await client.post("/plan/item", json={"title": title, **over}, headers=AGENT)
    assert r.status_code == 200, r.text
    _FLEET.append(r.json()["item_id"])
    return r.json()


async def order(client, repo: str) -> dict:
    r = await client.get("/plan/order", params={"repo": repo}, headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


async def propose(client, repo: str, **body) -> dict:
    r = await client.post("/plan/order-proposal", json={"repo": repo, **body}, headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


async def review(client, repo: str, pr: int, **over) -> dict:
    body = {
        "repo": repo, "pr": pr, "judged": True, "judge_model": "opus",
        "reviewers_selected": ["claude"],
        "reviewers": {"claude": {"model": "opus", "ran": True}},
        "to_fix": [], "dismissed": [], "sonar_findings": [],
    }
    r = await client.post("/review", json={**body, **over}, headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


def finding(key: str, **over) -> dict:
    return {"title": f"finding {key}", "severity": "P2", "file": "app/api/plan.py",
            "line": 10, "reviewers": ["claude"], "key": key, **over}


async def age_item(item_id: str, days: float) -> None:
    async with async_session() as s:
        await s.execute(update(PlanItem).where(PlanItem.id == uuid.UUID(item_id))
                        .values(updated_at=datetime.now(UTC) - timedelta(days=days)))
        await s.commit()


async def age_run(run_id: int, days: float) -> None:
    async with async_session() as s:
        await s.execute(update(ReviewRun).where(ReviewRun.id == run_id)
                        .values(ts=datetime.now(UTC) - timedelta(days=days)))
        await s.commit()


def entry(body: dict, item_id: str) -> dict:
    return next(e for e in body["entries"] if e["key"] == item_id)


async def test_a_suggestion_is_a_read_and_never_touches_the_live_order(client):
    """The property that makes this safe to ship before #183 is settled: it cannot
    thrash the plan, because it does not write it."""
    repo = "acme/ord-readonly"
    first = await add(client, repo, "first")
    second = await add(client, repo, "second", depends_on=[first["item_id"]])
    body = await order(client, repo)
    assert body["suggested_order"] == [first["item_id"], second["item_id"]]

    live = await client.get("/plan", params={"repo": repo, "exact": True}, headers=AGENT)
    assert [i["item_id"] for i in live.json()["items"]] == [first["item_id"], second["item_id"]]
    assert [i["rank"] for i in live.json()["items"]] == [1, 2]


async def test_the_suggested_order_is_applied_by_a_human_and_by_nobody_else(client):
    """``suggested_order`` is shaped exactly like ``POST /plan/reorder``'s ``order``
    so that applying it is one call — and the payload says whose call it is. An
    agent that could apply it would be an agent with human privileges, which is
    what #232 says a planner must not be."""
    repo = "acme/ord-apply"
    a = await add(client, repo, "a")
    b = await add(client, repo, "b")
    await add(client, repo, "c", depends_on=[b["item_id"]])
    body = await order(client, repo)
    assert body["apply"] == {"endpoint": "POST /plan/reorder", "human_only": True,
                            "body": {"repo": repo, "order": body["suggested_order"]}}

    refused = await client.post("/plan/reorder", json=body["apply"]["body"], headers=AGENT)
    assert refused.status_code == 403, refused.text

    applied = await client.post("/plan/reorder", json=body["apply"]["body"], headers=HUMAN)
    assert applied.status_code == 200, applied.text
    assert [i["item_id"] for i in applied.json()["items"]] == body["suggested_order"]
    # And once it is in force the suggestion is a no-op, which is the shape a
    # steady state should have.
    assert (await order(client, repo))["changed"] is False
    assert a["item_id"] in body["suggested_order"]


async def test_an_entry_says_whether_anybody_chose_the_rank_it_would_move(client):
    """A suggested move against a position a human decided is a different
    proposition from one against a position `plan_add` merely appended to — and
    this endpoint's own argument is that a reader must be able to tell derived
    from judged (#183)."""
    repo = "acme/ord-ranksource"
    first = await add(client, repo, "appended, nobody chose it")
    placed = await add(client, repo, "placed", before=first["item_id"])
    body = await order(client, repo)
    assert entry(body, placed["item_id"])["rank_source"] == "placed"
    assert entry(body, first["item_id"])["rank_source"] == "appended"

    await client.post("/plan/reorder",
                      json={"repo": repo, "order": [first["item_id"], placed["item_id"]]},
                      headers=HUMAN)
    after = await order(client, repo)
    assert {entry(after, i["item_id"])["rank_source"]
            for i in (first, placed)} == {"ordered"}


async def test_one_pr_is_answered_for_by_its_newest_run_and_no_other(client):
    """#101's finding, applied here: any predicate placed in front of the
    newest-run selection resurrects a stale run. The older run says the PR is red;
    the newest says it is green, and green is the answer."""
    repo = "acme/ord-newest"
    item = await add(client, repo, "the pr", ref_kind="pr", ref_value="41")
    await add(client, repo, "plain")
    old = await review(client, repo, 41, ci_status="FAIL")
    await age_run(old["id"], days=2)
    await review(client, repo, 41, ci_status="PASS", round=2)

    body = await order(client, repo)
    e = entry(body, item["item_id"])
    assert e["run"]["ci"] == "PASS"
    assert e["inputs"]["open_work"] is False
    assert body["suggested_order"] == [item["item_id"], body["suggested_order"][1]]
    assert e["basis"] == "ambiguous"


async def test_a_merged_pr_is_read_from_the_newest_run_even_when_an_older_says_open(client):
    """The exact shape of #101's round-2 defect: a PR panelled while OPEN, merged,
    and re-panelled after the merge came back OPEN from the stale run."""
    repo = "acme/ord-merged"
    merged = await add(client, repo, "landed", ref_kind="pr", ref_value="7")
    live = await add(client, repo, "still going")
    old = await review(client, repo, 7, pr_state="OPEN")
    await age_run(old["id"], days=1)
    await review(client, repo, 7, pr_state="MERGED", round=2)

    body = await order(client, repo)
    assert body["suggested_order"] == [live["item_id"], merged["item_id"]]
    e = entry(body, merged["item_id"])
    assert e["inputs"]["finished"] is True and e["inputs"]["bucket"] == "finished"
    # Labelled a preference, not a constraint: it is a snapshot, and snapshots go
    # stale. That distinction is the whole of #232's derived-versus-judged ask.
    assert e["basis"] == "preference"


async def test_a_finding_with_a_recorded_outcome_stops_holding_an_item_at_the_head(client):
    """All four outcomes count as answered, ``deferred`` included: it says the work
    moved to an issue, which is a decision. NO outcome row is what counts as open
    — nobody has said, which is neither fixed nor refuted."""
    repo = "acme/ord-outcome"
    item = await add(client, repo, "under review", ref_kind="pr", ref_value="12")
    other = await add(client, repo, "quiet")
    await review(client, repo, 12, to_fix=[finding("of1")])

    before = await order(client, repo)
    assert before["suggested_order"] == [item["item_id"], other["item_id"]]
    assert entry(before, item["item_id"])["inputs"]["open_work"] is True

    r = await client.post("/review/outcomes",
                          json={"repo": repo, "pr": 12,
                                "outcomes": [{"key": "of1", "outcome": "fixed"}]},
                          headers=AGENT)
    assert r.status_code in (200, 201), r.text

    after = await order(client, repo)
    e = entry(after, item["item_id"])
    assert e["run"]["confirmed"] == 1 and e["run"]["outstanding_findings"] == 0
    assert e["inputs"]["open_work"] is False
    assert e["basis"] == "ambiguous"


async def test_a_repo_spelt_with_capitals_does_not_hide_its_panel_run(client):
    """GitHub repos are case-insensitive and the plan lower-cases its copy for that
    reason, while ``review_runs.repo`` is stored as the panel sent it. Comparing
    them as text would leave the PR looking like one the board had never seen —
    #101's silent absence wearing a different hat."""
    repo = "acme/ord-case"
    item = await add(client, repo, "cased", ref_kind="pr", ref_value="9")
    await add(client, repo, "other")
    await review(client, "Acme/Ord-Case", 9, ci_status="FAIL")

    e = entry(await order(client, repo), item["item_id"])
    assert e["run"] is not None and e["run"]["ci"] == "FAIL"


async def test_an_item_with_no_pr_is_named_rather_than_assumed_healthy(client):
    """Most of a plan references issues. The response says which items the rules
    had no review state for, because an order computed from partial evidence and
    published as complete is #101's failure from the other end."""
    repo = "acme/ord-unknown"
    issue_item = await add(client, repo, "an issue", ref_kind="issue", ref_value="3")
    bare = await add(client, repo, "no ref at all")
    never = await add(client, repo, "unpanelled pr", ref_kind="pr", ref_value="99")

    body = await order(client, repo)
    reasons = {u["reason"]: u for u in body["unknown"]}
    overlap = next(u for u in body["unknown"] if u["input"] == "overlap")
    assert "#101" in overlap["reason"] and overlap["items"] is None

    no_ref = next(u for u in body["unknown"]
                  if u["input"] == "review_state" and "references an issue" in u["reason"])
    assert sorted(no_ref["items"]) == sorted([issue_item["item_id"], bare["item_id"]])

    unpanelled = next(u for u in body["unknown"]
                      if u["input"] == "review_state" and "never recorded" in u["reason"])
    assert unpanelled["items"] == [never["item_id"]]
    assert "not evidence the PR is fine" in unpanelled["reason"]
    assert reasons  # every entry carries a reason string


async def test_an_unresolvable_ref_is_reported_not_dropped(client):
    """A plan item may carry a PR ref with no repo (the fleet scope), and there is
    then no run to find. Reported, because an item silently placed on fewer inputs
    than its neighbours is the thing nobody can see."""
    item_id = (await add_fleet(client, "fleet pr", ref_kind="pr", ref_value="5"))["item_id"]
    body = await client.get("/plan/order", headers=AGENT)
    assert body.status_code == 200
    unresolved = next(u for u in body.json()["unknown"] if u["input"] == "ref")
    assert any(i["item_id"] == item_id for i in unresolved["items"])


async def test_a_week_old_panel_run_is_still_used_and_still_named(client):
    """It remains the best evidence there is; it is also a snapshot. Both halves
    are said, so a reader can see that part of the order rests on last week."""
    repo = "acme/ord-stale-ev"
    item = await add(client, repo, "old evidence", ref_kind="pr", ref_value="21")
    await add(client, repo, "fresh")
    run = await review(client, repo, 21, ci_status="FAIL")
    await age_run(run["id"], days=EVIDENCE_STALE_DAYS + 1)

    body = await order(client, repo)
    e = entry(body, item["item_id"])
    assert e["inputs"]["open_work"] is True          # used
    assert body["suggested_order"][0] == item["item_id"]
    named = next(u for u in body["unknown"] if "days old" in u["reason"])
    assert [i["item_id"] for i in named["items"]] == [item["item_id"]]
    assert named["items"][0]["age_days"] >= EVIDENCE_STALE_DAYS


async def test_a_long_untouched_item_rises_at_the_plans_own_threshold(client):
    """The endpoint passes the plan's own ``STALE_DAYS``, so a reader meets the
    word "stale" once rather than twice with two meanings."""
    repo = "acme/ord-stale"
    fresh = await add(client, repo, "fresh")
    old = await add(client, repo, "forgotten")
    await age_item(old["item_id"], days=STALE_DAYS + 5)

    body = await order(client, repo)
    assert body["suggested_order"] == [old["item_id"], fresh["item_id"]]
    assert body["stale_days"] == STALE_DAYS
    assert entry(body, old["item_id"])["inputs"]["stale"] is True


async def test_the_rules_and_the_plan_agree_on_the_word_stale(client):
    """Two answers to one question, disagreeing for an hour a fortnight. `/plan`
    renders `idle_days` ROUNDED — `round(idle, 1)` — while its own `stale` flag is
    computed from the unrounded age, so an item at 13.96 days displays as 14.0.
    Reading the rule off the display value ordered it as stale while the plan said
    it was not (Codex, review pass five).

    Pinned just inside the threshold, where the two definitions can differ and
    nowhere else."""
    repo = "acme/ord-round"
    edge = await add(client, repo, "just inside")
    await add(client, repo, "other")
    await age_item(edge["item_id"], days=STALE_DAYS - 0.04)

    live = await client.get("/plan", params={"repo": repo, "exact": True}, headers=AGENT)
    view = next(i for i in live.json()["items"] if i["item_id"] == edge["item_id"])
    assert view["idle_days"] == float(STALE_DAYS), "the fixture must round up to the threshold"
    assert view["stale"] is False, "and the plan itself must still say not stale"

    body = await order(client, repo)
    assert entry(body, edge["item_id"])["inputs"]["stale"] is False
    assert body["changed"] is False


async def test_a_claim_is_evidence_and_never_a_rule(client):
    """A claim expires passively, so ordering on it would make the sequence flap on
    a TTL — and ``next`` already skips a claimed item, which is the behaviour that
    question wants. So it rides the entry and moves nothing."""
    repo = "acme/ord-claim"
    held = await add(client, repo, "held")
    free = await add(client, repo, "free")
    r = await client.post("/plan/item/claim", json={"item_id": held["item_id"]}, headers=AGENT)
    assert r.status_code == 200, r.text

    body = await order(client, repo)
    assert body["suggested_order"] == [held["item_id"], free["item_id"]]
    assert entry(body, held["item_id"])["claim"] is not None
    assert body["changed"] is False


async def test_the_scope_is_exact_so_two_rank_sequences_are_never_interleaved(client):
    """A read of ``GET /plan`` widens to the fleet items because context helps. An
    order cannot: ranks are allocated per scope, so a widened read is two
    sequences the scope band interleaved — and not one sequence anybody could hand
    to ``/plan/reorder``."""
    repo = "acme/ord-scope"
    mine = await add(client, repo, "repo item")
    fleet_id = (await add_fleet(client, "fleet item"))["item_id"]

    body = await order(client, repo)
    assert body["scope"] == "exact"
    assert body["active_order"] == [mine["item_id"]]
    assert fleet_id not in body["suggested_order"]


async def test_a_proposal_records_its_evidence_and_says_what_it_could_not_decide(client):
    """The prediction side of #232's ledger. What is stored is the placements and
    their inputs — not the titles, which the plan row still holds and rule 1 says
    the plan never restates."""
    repo = "acme/ord-record"
    blocked_on = await add(client, repo, "first")
    blocked = await add(client, repo, "second", depends_on=[blocked_on["item_id"]])
    tie_a = await add(client, repo, "tie a")
    tie_b = await add(client, repo, "tie b")

    body = await propose(client, repo)
    assert body["recorded"] is True
    p = body["proposal"]
    assert p["source"] == "deterministic" and p["rules_version"] == RULES_VERSION
    assert p["overlap_known"] is False and len(p["inputs_digest"]) == 64
    assert p["active_order"] == [blocked_on["item_id"], blocked["item_id"],
                                 tie_a["item_id"], tie_b["item_id"]]
    by_key = {e["key"]: e for e in p["placements"]}
    assert by_key[blocked["item_id"]]["basis"] == "constraint"
    # The two fields answer different questions, and this row is why both exist:
    # the tied pair IS derived — a constraint puts both of them above the blocked
    # item — and their order relative to each other is still open. Reporting only
    # the basis counts would say nothing was left undecided when two positions
    # were; reporting only the groups would hide that a rule placed them at all.
    assert by_key[tie_a["item_id"]]["basis"] == "constraint"
    # Three of the four are interchangeable, and the one that is not is the one an
    # edge pins: `second` goes last however the other three are shuffled, so every
    # permutation of the group is a valid order and the group says which.
    assert set(by_key[tie_a["item_id"]]["ambiguous_with"]) == {
        blocked_on["item_id"], tie_b["item_id"]}
    assert sorted(p["ambiguous"][0]) == sorted(
        [blocked_on["item_id"], tie_a["item_id"], tie_b["item_id"]])
    assert p["counts"]["interchangeable"] == 3
    assert by_key[blocked["item_id"]]["ambiguous_with"] == []
    assert p["counts"]["derived"] + p["counts"]["ambiguous"] == p["counts"]["entries"]
    # Evidence, not restatement: no titles in the stored row, titles in the reply.
    assert "title" not in by_key[tie_a["item_id"]]
    assert entry(body, tie_a["item_id"])["title"] == "tie a"
    # The outcome half is absent rather than stubbed — #232's remaining work.
    assert "outcome" not in p


async def test_a_caller_cannot_assert_an_order_and_call_it_deterministic(client):
    """The board computes the order it stores. An agent's opinion belongs on the
    board addressed to whoever is deciding, which needs no endpoint — and a row
    here always says what the RULES produced."""
    repo = "acme/ord-assert"
    a = await add(client, repo, "a")
    b = await add(client, repo, "b", depends_on=[a["item_id"]])
    body = await propose(client, repo, order=[b["item_id"], a["item_id"]],
                         source="agent-consensus", suggested_order=[b["item_id"]])
    assert body["proposal"]["source"] == "deterministic"
    assert body["proposal"]["suggested_order"] == [a["item_id"], b["item_id"]]


async def test_an_identical_proposal_is_not_recorded_twice_unless_forced(client):
    """A cron floor runs dirty or not (#232), so an un-deduplicated ledger buries
    the moment the answer changed under a thousand copies of it."""
    repo = "acme/ord-dedupe"
    await add(client, repo, "a")
    await add(client, repo, "b")
    first = await propose(client, repo)
    again = await client.post("/plan/order-proposal", json={"repo": repo}, headers=AGENT)
    # 200, not 201: nothing was created. The same distinction `/review/outcomes`
    # already draws between a row it wrote and a row it found already there.
    assert again.status_code == 200, again.text
    assert again.json()["recorded"] is False
    assert again.json()["proposal"]["id"] == first["proposal"]["id"]
    assert "identical" in again.json()["reason"]

    forced = await propose(client, repo, force=True)
    assert forced["recorded"] is True and forced["proposal"]["id"] != first["proposal"]["id"]


async def test_a_changed_world_is_recorded_as_a_new_proposal(client):
    repo = "acme/ord-changed"
    a = await add(client, repo, "a")
    await add(client, repo, "b")
    first = await propose(client, repo)
    await age_item(a["item_id"], days=STALE_DAYS + 1)
    second = await propose(client, repo)
    assert second["recorded"] is True
    assert second["proposal"]["inputs_digest"] != first["proposal"]["inputs_digest"]
    assert second["proposal"]["suggested_order"] == first["proposal"]["suggested_order"]


async def test_a_placement_says_which_run_its_readings_came_from(client):
    """#227 asks a proposal to record the exact inputs used. A stored reading of
    "CI was green" that cannot be traced to the run that said so is a reading
    nobody can check afterwards, which is the whole point of keeping it."""
    repo = "acme/ord-prov"
    item = await add(client, repo, "traceable", ref_kind="pr", ref_value="33")
    await add(client, repo, "other")
    run = await review(client, repo, 33, ci_status="PASS", head_sha=HEAD_SHA)

    p = await propose(client, repo)
    placed = next(e for e in p["proposal"]["placements"] if e["key"] == item["item_id"])
    assert placed["evidence"]["run_id"] == run["id"]
    assert placed["evidence"]["head_sha"] == HEAD_SHA
    assert placed["evidence"]["round"] == 1
    # The readings themselves live once, under the inputs — not copied into the
    # provenance, where they would be free to disagree with themselves.
    assert placed["inputs"]["readings"]["ci"] == "PASS"
    assert "ci" not in placed["evidence"]
    # An item with nothing to read says so, rather than carrying an empty dict
    # that reads like a run with no fields.
    plain = next(e for e in p["proposal"]["placements"] if e["key"] != item["item_id"])
    assert plain["evidence"] is None


async def test_evidence_arriving_is_a_new_proposal_even_when_the_order_does_not_move(client):
    """The first draft digested only the QUANTISED rule inputs, so a PR panelled
    for the first time — or a confirmed finding somebody finally recorded an
    outcome for — left every flag untouched and read as "nothing has happened".
    The ledger exists to show when the answer moved and why; a row it skips is a
    change nobody can see afterwards. Codex found this on review."""
    repo = "acme/ord-evidence"
    item = await add(client, repo, "watched", ref_kind="pr", ref_value="77")
    await add(client, repo, "quiet")
    first = await propose(client, repo)

    # A run whose readings change no rule flag: CI unknown, no findings, PR open.
    await review(client, repo, 77, ci_status="unknown", pr_state="OPEN")
    second = await propose(client, repo)
    assert second["recorded"] is True
    assert second["proposal"]["suggested_order"] == first["proposal"]["suggested_order"]
    assert second["proposal"]["inputs_digest"] != first["proposal"]["inputs_digest"]

    # And an outcome recorded against a finding, which changes a COUNT the rules
    # read but not the boolean they read it through.
    await review(client, repo, 77, to_fix=[finding("e1"), finding("e2")], round=2)
    third = await propose(client, repo)
    r = await client.post("/review/outcomes",
                          json={"repo": repo, "pr": 77,
                                "outcomes": [{"key": "e1", "outcome": "fixed"}]},
                          headers=AGENT)
    assert r.status_code in (200, 201), r.text
    fourth = await propose(client, repo)
    assert fourth["recorded"] is True
    assert fourth["proposal"]["inputs_digest"] != third["proposal"]["inputs_digest"]
    # One finding still open, so the order is unchanged — which is exactly the case
    # a digest over the flags alone would have deduplicated away.
    assert fourth["proposal"]["suggested_order"] == third["proposal"]["suggested_order"]
    placed = next(e for e in fourth["proposal"]["placements"] if e["key"] == item["item_id"])
    assert placed["inputs"]["open_work"] is True


async def test_a_recorded_proposal_is_replayed_with_TODAYS_caveats(client, monkeypatch):
    """Evidence ages. A run at six days carries no staleness caveat and the same run
    at eight days does, with every fact the rules read unchanged — so the proposal
    is correctly deduplicated (a threshold a clock crossed is not a new proposal)
    and the WARNING has to be current anyway.

    The row keeps what was true when it was written; the reply's top level says what
    is true now. Answering a caller today with the caveats of the day the row was
    recorded is the one reading a staleness warning must never be given.
    """
    repo = "acme/ord-replay"
    item = await add(client, repo, "watched", ref_kind="pr", ref_value="55")
    await add(client, repo, "other")
    await review(client, repo, 55, ci_status="PASS")
    first = await propose(client, repo)
    assert not [u for u in first["unknown"] if "days old" in u["reason"]]

    # The CLOCK moves; the run does not. Ageing the run instead would be a change
    # of evidence — a different run id's worth of difference — and the digest is
    # right to notice that, which is exactly what this test must not do.
    later = datetime.now(UTC) + timedelta(days=EVIDENCE_STALE_DAYS + 1)
    monkeypatch.setattr(plan_api, "_utcnow", lambda: later)
    again = await client.post("/plan/order-proposal", json={"repo": repo}, headers=AGENT)
    monkeypatch.undo()
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["recorded"] is False and body["proposal"]["id"] == first["proposal"]["id"]
    # The row is unchanged and still honest about the day it was written...
    assert not [u for u in body["proposal"]["unknown"] if "days old" in u["reason"]]
    # ...and the answer a caller gets today says the evidence has gone stale.
    stale = next(u for u in body["unknown"] if "days old" in u["reason"])
    assert [i["item_id"] for i in stale["items"]] == [item["item_id"]]


async def test_a_clock_tick_alone_is_still_not_a_new_proposal(client):
    """The other half of the same rule: the digest covers every input EXCEPT the
    one that moves on its own. Without this the cron floor #232 describes — which
    runs dirty or not — writes a row a minute."""
    repo = "acme/ord-tick"
    a = await add(client, repo, "a")
    await add(client, repo, "b")
    first = await propose(client, repo)
    # Aged, but not across the threshold: the rules cannot see the difference, so
    # neither may the digest.
    await age_item(a["item_id"], days=STALE_DAYS - 2)
    again = await client.post("/plan/order-proposal", json={"repo": repo}, headers=AGENT)
    assert again.status_code == 200, again.text
    assert again.json()["recorded"] is False
    assert again.json()["proposal"]["id"] == first["proposal"]["id"]


async def test_the_writer_takes_the_scope_lock_and_the_reader_does_not(client, monkeypatch):
    """The dedupe reads the newest row, decides against it and inserts, with nothing
    between the two — so it is serialised on the same per-scope advisory lock
    `POST /plan/reorder` takes, and for the same reason.

    Asserted on the lock rather than by racing two requests, because racing them
    does not prove it: the window is a few milliseconds wide, `asyncio.gather` over
    one loop closes it more often than not, and a test that passes whether or not
    the lock is there is not evidence that it is. `test_two_writers_...` below is
    the smoke test; this is the assertion.

    The READ half matters as much: a caller asking what the rules imply must not
    block behind whoever is recording a proposal, and a GET that quietly takes a
    write lock is how a read path acquires a writer's contention.
    """
    repo = "acme/ord-lock"
    await add(client, repo, "a")
    taken: list[str | None] = []
    real = plan_api._lock_scope

    async def spy(session, scope):
        taken.append(scope)
        await real(session, scope)

    monkeypatch.setattr(plan_api, "_lock_scope", spy)
    await client.get("/plan/order", params={"repo": repo}, headers=AGENT)
    assert taken == [], "a read took the scope write lock"
    r = await client.post("/plan/order-proposal", json={"repo": repo}, headers=AGENT)
    assert r.status_code == 201, r.text
    assert taken == [repo]


async def test_two_writers_recording_at_once_leave_one_proposal(client):
    """The commonest second writer is not exotic: it is the same caller retrying
    after its client timed out on a request the board had already accepted.

    A smoke test, and honest about it — see the test above for why the lock itself
    is asserted separately.
    """
    repo = "acme/ord-race"
    await add(client, repo, "a")
    await add(client, repo, "b")
    both = await asyncio.gather(
        client.post("/plan/order-proposal", json={"repo": repo}, headers=AGENT),
        client.post("/plan/order-proposal", json={"repo": repo}, headers=AGENT),
    )
    # 201 for whichever inserted, 200 for whichever found the row already there —
    # in either order, since that is what "concurrent" means.
    assert sorted(r.status_code for r in both) == [200, 201], [r.text for r in both]
    listing = await client.get("/plan/order-proposals", params={"repo": repo}, headers=AGENT)
    assert listing.json()["count"] == 1


async def test_the_ledger_reads_back_newest_first_without_the_evidence_blob(client):
    """The list omits ``placements`` for the reason ``/reviews`` omits
    ``changed_files``: it is the unbounded half of the row. The counts derived from
    it still ride the list, because "how much of that was derived" is what a reader
    scanning the ledger is looking for."""
    repo = "acme/ord-ledger"
    await add(client, repo, "a")
    await add(client, repo, "b")
    one = await propose(client, repo)
    two = await propose(client, repo, force=True)

    r = await client.get("/plan/order-proposals", params={"repo": repo}, headers=AGENT)
    assert r.status_code == 200, r.text
    listing = r.json()
    assert [p["id"] for p in listing["proposals"]] == [two["proposal"]["id"],
                                                       one["proposal"]["id"]]
    assert all("placements" not in p for p in listing["proposals"])
    assert listing["proposals"][0]["counts"]["entries"] == 2
    assert "#232" in listing["outcomes"]

    full = await client.get(f"/plan/order-proposal/{one['proposal']['id']}", headers=AGENT)
    assert full.status_code == 200
    assert len(full.json()["placements"]) == 2
    assert full.json()["moves"] == []


async def test_an_unknown_proposal_is_a_404(client):
    r = await client.get("/plan/order-proposal/999999", headers=AGENT)
    assert r.status_code == 404


async def test_the_ledger_defaults_to_every_scope_and_narrows_on_request(client):
    """``repo=null`` is the fleet-wide scope when reordering, and the fleet-wide
    scope is a real place a proposal can be made about — so listing needs a way to
    say "that one only" as well as "all of them"."""
    repo = "acme/ord-scopes"
    await add(client, repo, "scoped")
    scoped = await propose(client, repo)
    await add_fleet(client, "fleet-only")
    fleet = await propose(client, None)

    everything = await client.get("/plan/order-proposals", params={"limit": 100},
                                  headers=AGENT)
    ids = [p["id"] for p in everything.json()["proposals"]]
    assert scoped["proposal"]["id"] in ids and fleet["proposal"]["id"] in ids
    assert everything.json()["scope"] == "all"

    only_fleet = await client.get("/plan/order-proposals",
                                 params={"exact": True, "limit": 100}, headers=AGENT)
    assert only_fleet.json()["scope"] == "exact"
    assert {p["repo"] for p in only_fleet.json()["proposals"]} == {None}


async def test_an_empty_scope_answers_rather_than_erroring(client):
    """A repo with nothing in the plan is the state every repo starts in, and the
    honest answer is an empty order — not a 404, which would read as "no such
    scope", and not a crash on a degenerate case nobody thought about."""
    body = await order(client, "acme/ord-empty")
    assert body["active_order"] == [] and body["suggested_order"] == []
    assert body["changed"] is False and body["entries"] == []
    assert body["counts"]["entries"] == 0 and body["counts"]["derived"] == 0
    assert body["ambiguous"] == [] and body["cycles"] == []
    # And it can be recorded: a scope that is empty today is a fact about today,
    # and the ledger is what a later reading is compared against.
    p = await propose(client, "acme/ord-empty")
    assert p["recorded"] is True and p["proposal"]["placements"] == []


async def test_reading_the_order_needs_a_token(client):
    r = await client.get("/plan/order")
    assert r.status_code == 401
    r = await client.post("/plan/order-proposal", json={})
    assert r.status_code == 401


async def test_the_cap_refuses_rather_than_truncating_and_does_so_on_both_reads(
        client, monkeypatch):
    """An order is not pageable: a partial one reads as an order about the whole
    scope. So the cap refuses — and it refuses on the READ as well as the write,
    because a cap that only stopped the writer would have left the served answer,
    the one anybody actually uses, unbounded.

    Driven by lowering the cap rather than by adding 501 plan items: the number is
    a policy and the behaviour at the line is what matters.
    """
    repo = "acme/ord-cap"
    await add(client, repo, "one")
    await add(client, repo, "two")
    monkeypatch.setattr(plan_api, "MAX_ORDER_ENTRIES", 1)
    read = await client.get("/plan/order", params={"repo": repo}, headers=AGENT)
    write = await client.post("/plan/order-proposal", json={"repo": repo}, headers=AGENT)
    monkeypatch.undo()
    assert read.status_code == 422, read.text
    assert write.status_code == 422, write.text
    assert read.json()["detail"]["open_items"] == 2
    assert "truncated" in read.json()["detail"]["hint"]
    # And it is the cap that refused, not the plan being broken: raise it back and
    # the same scope answers.
    assert (await order(client, repo))["counts"]["entries"] == 2
