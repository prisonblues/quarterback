"""#775: round N asks the seats round N-1 did not, and a seat it does not ask is
never counted as coverage.

Every round dispatched the same panel. A seat that read the diff in round 1 read the
fix in round 2 with its own round-1 findings in front of it, and the cheapest thing it
could produce was more of the same shape — so nothing biased a later round toward the
questions nobody had asked yet. `route_seats` is the set-difference rule that does:
round N's seats are round N-1's complement, spent within #776's per-round budget,
falling back to a repeat only when the complement runs out.

**The half these tests are really for is the other one.** #790 declined the seat cut
on an argument that survives the routing untouched: a seat that is installed, selected
and then not dispatched produces no row, and its silence is indistinguishable from a
seat that ran and found nothing. The next round then banks *"four seats looked at this
and three had nothing to say"* when three were never asked. Two readers had to learn a
fifth reason a seat has no row — beside not installed, ran and failed, ran and was
silent, and cut by a ceiling — and each of them fails in a different way and on a
different day:

* :func:`panel_rounds.coverage_veto` reads THIS round's per-seat rows. Without the
  fifth reason, a held seat has no row at all and its quiet reads as a clean seat, so
  a round that asked half the panel can stop `confident`.
* :func:`panel_rounds.load_baseline` reads the PREVIOUS round's payload, and there the
  failure is worse than a silence: a partially-dispatched round satisfies every term
  of `reread` — its seats ran, read their whole target and read a diff — so it becomes
  the round that ERASES every earlier round's recorded coverage gap, on the strength
  of a panel that never looked at most of them.

Both are pinned here with the defect in front of the fix: each test states what would
be true if the branch it exercises were deleted.

**The three-state rule is the third subject.** "The prior round dispatched nobody" and
"no prior round said what it dispatched" are different facts, and the second is the
common one — round 1, a standalone `/panel`, every payload written before
`seat_routing` existed. Unknown must read as *ask everybody*, never as *everybody is a
complement seat*, because the second hands a tight budget the whole panel to choose
from by name order and that is the alphabet cut #790 refused.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_rounds  # noqa: E402
import panel_seats  # noqa: E402
from conftest import gh_stub  # noqa: E402

#: Four seats, so a budget of 2 has something to choose and something to leave. Named
#: in `LLM_REVIEWERS` order rather than alphabetically, because that order is the
#: router's tie-break of last resort and a fixture written in the other one would make
#: every ordering assertion below read as if it were asserting the alphabet.
FOUR = ["claude", "codex", "antigravity", "pi"]

#: Seats with no code-reading floor among them, for the tests about the complement
#: rule itself. `SEAT_READS_CODE` is `{"claude"}`, and a fixture including claude
#: measures the floor and the complement at once — which is the right thing to test
#: and the wrong thing to test FIRST, because a floor that swallowed the whole budget
#: would make a broken complement rule look correct.
BLIND_FOUR = ["codex", "antigravity", "pi", "grok"]

CFG = {"github": "acme/board", "path": "/tmp/acme-board", "name": "board",
       "_rules_baseline": ".harness-rules.sample",
       "review_panel": {}}


# ------------------------------------------------------------------ the router

def test_seats_that_did_not_run_last_round_are_preferred():
    """#775's rule, in its plainest form and with no floor in the way.

    Round 1 asked codex and antigravity; round 2 may ask two; so round 2 asks pi and
    grok. This is mergeCraft's `test_lenses_that_did_not_run_last_round_are_preferred`
    with seats in place of lenses, and it is the assertion the whole feature exists to
    satisfy — everything else here is about the cost of satisfying it.
    """
    r = panel_seats.route_seats(BLIND_FOUR, BLIND_FOUR, prior_round=1,
                                prior_dispatched=["codex", "antigravity"], budget=2)
    assert r.dispatched == {"pi", "grok"}
    assert r.held == {"codex", "antigravity"}


def test_complement_routing_does_not_rerun_every_seat_every_round():
    """The observation the issue opens with, pinned as a property of two consecutive
    rounds rather than of one call: the panel a round dispatches must not be the panel
    the round before it dispatched.

    Asserted as a set inequality and not as a specific pair, because WHICH two seats
    round 3 picks is the tie-break's business and the tie-break is arbitrary on
    purpose. What must never be arbitrary is that round 3 differs from round 2.
    """
    two = panel_seats.route_seats(BLIND_FOUR, BLIND_FOUR, prior_round=1,
                                  prior_dispatched=["codex", "antigravity"], budget=2)
    three = panel_seats.route_seats(BLIND_FOUR, BLIND_FOUR, prior_round=2,
                                    prior_dispatched=two.dispatched, budget=2)
    assert three.dispatched != two.dispatched
    assert three.dispatched == {"codex", "antigravity"}


def test_the_complement_falls_back_to_a_repeat_rather_than_an_empty_panel():
    """mergeCraft's own fallback: repeat a prior seat only when the complement is
    empty. A round whose predecessor asked everybody has no complement at all, and the
    budget still has to be spent on somebody — a round that dispatched nobody would be
    a review of nothing, recorded as a round."""
    r = panel_seats.route_seats(BLIND_FOUR, BLIND_FOUR, prior_round=1,
                                prior_dispatched=BLIND_FOUR, budget=2)
    assert len(r.dispatched) == 2
    assert all(r.why[n] == "repeat" for n in r.dispatched)


def test_the_budget_can_never_empty_the_panel():
    """A budget of 0 is an exhaustion, not a ceiling, and naming one belongs to a
    caller that can also end the cycle `confident: false`. `harness_rules.tapered`
    floors at 1 already; this pins the router against a caller that computed its own
    and got a zero, because the failure mode is a round that reviewed nothing and
    recorded itself as a round."""
    r = panel_seats.route_seats(BLIND_FOUR, BLIND_FOUR, prior_round=1,
                                prior_dispatched=BLIND_FOUR, budget=0)
    assert len(r.dispatched) == 1


def test_the_code_reading_seat_survives_a_budget_that_would_have_dropped_it():
    """The floor, and it is a floor rather than a preference: it is taken out of the
    budget BEFORE the complement gets any of it.

    claude ran last round, so the complement rule alone would drop it — and it is the
    only seat on this panel that can open a file (`SEAT_READS_CODE`). A round that
    routed it away would be a round where "I could not read the caller" stopped being
    a finding and became the panel's shape, on every remaining seat at once. That is
    the dimension this file can actually name, so it is the one the floor protects.
    """
    r = panel_seats.route_seats(FOUR, FOUR, prior_round=1,
                                prior_dispatched=["claude", "codex"], budget=2)
    assert "claude" in r.dispatched
    assert r.why["claude"] == "floor-code"
    # And the floor spends a slot rather than being granted one on top: the budget is
    # a ceiling, and a floor that could exceed it would be a second dial nobody wrote.
    assert len(r.dispatched) == 2
    assert r.dispatched == {"claude", "antigravity"}


def test_a_floor_seat_that_is_ALSO_a_complement_seat_records_the_complement():
    """The word is decided after the picking, not during it.

    claude did not run last round, so it would have been kept by the complement rule
    with no floor in sight. Recording `floor-code` there would inflate every count of
    how often the floor actually overrode the complement — a number somebody tuning
    the curve reads, and the whole reason the reason is recorded per seat."""
    r = panel_seats.route_seats(FOUR, FOUR, prior_round=1,
                                prior_dispatched=["codex", "pi"], budget=2)
    assert r.why["claude"] == "complement"


# ------------------------------------------------- the three states of "prior round"

def test_no_recorded_prior_round_asks_everybody_however_tight_the_budget():
    """THE three-state rule, and the direction it fails in.

    `None` is "no round said what it dispatched" — round 1, a standalone `/panel`, a
    cycle whose baselines all predate the field. There is no complement to compute, so
    a budget spent here would be spent by name order, which is #790's alphabet cut
    wearing this feature's clothes. Every seat is asked instead, which is exactly what
    happened before the routing landed and is the only direction that can never cost a
    round coverage.
    """
    r = panel_seats.route_seats(FOUR, FOUR, prior_dispatched=None, budget=1)
    assert r.dispatched == set(FOUR)
    assert r.held == set()
    assert all(r.why[n] == "unknown-prior" for n in FOUR)


def test_a_prior_round_that_dispatched_NOBODY_is_not_the_same_as_no_record():
    """The other side of the same coin, and the reason the field cannot be a bare set.

    An empty prior set makes EVERY seat a complement seat, so a tight budget picks by
    the tie-break — which is fine, because a round that really dispatched nobody read
    nothing and there is no repeat to prefer. Conflating it with `None` would be
    harmless in this direction and catastrophic in the other, which is why they are
    two values rather than one falsy one.
    """
    r = panel_seats.route_seats(BLIND_FOUR, BLIND_FOUR, prior_round=1,
                                prior_dispatched=[], budget=2)
    assert len(r.dispatched) == 2
    assert r.held
    assert all(r.why[n] == "complement" for n in r.dispatched)


def test_a_flat_curve_holds_nobody_and_says_so():
    """The shipped fleet. `round_budgets.multipliers` defaults to `[1.0]`, so
    `panel.py` passes no budget at all and every selected seat is asked — with the
    reason recorded rather than left as an absent field, so a reader can tell "routing
    ran and decided nothing" from "this payload predates routing"."""
    r = panel_seats.route_seats(FOUR, FOUR, prior_round=1,
                                prior_dispatched=["claude"], budget=None)
    assert r.dispatched == set(FOUR)
    assert r.held == set()
    assert all(r.why[n] == "all" for n in FOUR)
    assert r.budget is None


def test_a_budget_that_did_not_bind_is_told_apart_from_no_budget_at_all():
    """Both hold nobody and both record `all`; only one is a number somebody could
    tighten. `SeatRouting.budget` is what carries the difference into the payload."""
    loose = panel_seats.route_seats(FOUR, FOUR, prior_round=1,
                                    prior_dispatched=["claude"], budget=9)
    assert loose.held == set() and loose.budget == 9
    assert panel_seats.route_seats(FOUR, FOUR, prior_round=1,
                                   prior_dispatched=["claude"],
                                   budget=None).budget is None


# --------------------------------------------------------------- absence and record

def test_an_absent_seat_is_asked_and_does_not_spend_a_budget_slot():
    """A seat whose CLI is not on this box costs nothing to dispatch, and `run_seat`
    is the single authority on absence (#222) — so it is asked, and the budget is
    spent on seats that can actually read.

    The `absent: true` row it then writes is load-bearing in both downstream readers:
    `coverage_veto` exempts it and `load_baseline` refuses to bank its truncation.
    Spending a budget slot on it would buy the round nothing and cost it a seat.
    """
    r = panel_seats.route_seats(FOUR, ["codex", "antigravity", "pi"], prior_round=1,
                                prior_dispatched=["codex"], budget=1)
    assert "claude" in r.dispatched and r.why["claude"] == "absent"
    # One slot, spent on a seat that is here and did not run last round.
    assert r.dispatched == {"claude", "antigravity"}
    assert r.held == {"codex", "pi"}


def test_every_selected_seat_carries_a_reason_whether_it_was_asked_or_not():
    """#775 quotes its prior art on this: a skipped lens's reason is "as informative
    as selected ones". A record kept only for the seats that ran is a record of the
    decision's outcome and not of the decision, and the held seats are the ones a
    reader cannot reconstruct from anything else — a seat missing from `reviewers`
    looks exactly like a seat nobody ever configured."""
    r = panel_seats.route_seats(FOUR, FOUR, prior_round=1,
                                prior_dispatched=["claude", "codex"], budget=2)
    assert set(r.why) == set(FOUR)
    assert set(r.dispatched) | set(r.held) == set(FOUR)


@pytest.mark.parametrize("prior,budget,installed", [
    (None, None, FOUR), (None, 1, FOUR), ([], 2, FOUR),
    (["claude"], 1, FOUR), (["claude", "codex"], 2, FOUR),
    (FOUR, 3, FOUR), (["pi"], 2, ["codex", "pi"]), ([], 0, []),
])
def test_the_router_only_ever_emits_words_the_vocabulary_declares(prior, budget,
                                                                  installed):
    """The mutation pin on the closed vocabulary, and the reason it is closed.

    `SEAT_ROUTING_REASONS` maps every legal word to whether the seat was DISPATCHED
    under it, and this asserts three things at once across every shape the router
    takes: the word is in the table, the table agrees with the split, and no seat is
    in both halves. Add a word to the router without adding it to the table and this
    goes red; add it to the table with the wrong side and the first routing that emits
    it goes red.

    A free-text reason would have failed silently instead — it would fall through
    every branch in `coverage_veto`, and for a seat with no row falling through means
    its silence banks as coverage. That is the exact defect this feature had to avoid,
    arriving through the field added to prevent it.
    """
    r = panel_seats.route_seats(FOUR, installed, prior_dispatched=prior, budget=budget)
    assert set(r.why.values()) <= set(panel_seats.SEAT_ROUTING_REASONS)
    for seat, word in r.why.items():
        assert panel_seats.SEAT_ROUTING_REASONS[word] is (seat in r.dispatched)
    assert not (r.dispatched & r.held)


def test_the_serialised_block_keeps_an_unknown_prior_as_null():
    """`prior_dispatched: []` would claim the previous round dispatched nobody. That
    is a round that reviewed nothing, and it is a very different assertion from "no
    round said" — which is the one thing the next round's reader must be able to
    tell."""
    said = panel_seats.route_seats(FOUR, FOUR, prior_dispatched=None,
                                   budget=2).as_dict()
    assert said["prior_dispatched"] is None
    assert said["held"] == []
    empty = panel_seats.route_seats(FOUR, FOUR, prior_round=1, prior_dispatched=[],
                                    budget=2).as_dict()
    assert empty["prior_dispatched"] == []


# ------------------------------------------------------- coverage_veto's fifth reason

def _veto(reviewers, **kw):
    """`coverage_veto` with everything else about the round satisfied, so the only
    lines it can produce are the ones under test. `ci_status` is keyword-only with no
    default by design — a caller that forgets it must raise rather than quietly buy a
    confident stop — so it is stated here."""
    return panel_rounds.coverage_veto(reviewers, None, 0, 1000, ci_status="PASS", **kw)


def test_a_seat_the_round_did_not_ask_vetoes_a_confident_stop():
    """#790's defect, and the test that would have caught it.

    Delete the `SEAT_HELD` branch from `coverage_veto` and codex's row falls through
    to the ordinary "did not run" line — which still vetoes, so the defect is NOT that
    the veto disappears. Delete the ROW instead, as the obvious cheaper implementation
    of complement routing would have, and there is no line at all: `round_stop`
    computes `confident` as `not veto`, so a round that asked two of four seats stops
    confidently and the next round inherits four seats' worth of quiet off two.
    """
    veto = _veto({"claude": {"ran": True},
                  "codex": {"ran": False, "absent": False,
                            "routing": panel_seats.SEAT_HELD,
                            "skip": "codex: not dispatched this round"}})
    assert any("codex" in v and "not asked" in v for v in veto), veto


def test_the_line_says_a_question_was_not_put_and_not_that_a_seat_failed():
    """The wording is the requirement, not decoration. #790 states it in one clause: a
    held seat's silence "must never be read as coverage while also never being read as
    a fault in the seat". Nothing failed, nothing timed out, nothing was missing from
    the box — the round spent its seat budget somewhere else on purpose, and a reader
    triaging "codex did not run (…)" goes looking for a broken CLI."""
    line = [v for v in _veto({"claude": {"ran": True},
                              "codex": {"ran": False, "absent": False,
                                        "routing": panel_seats.SEAT_HELD,
                                        "skip": "codex: not dispatched"}})
            if v.startswith("codex")]
    assert len(line) == 1
    assert "did not run" not in line[0]
    assert "not asked" in line[0]


def test_a_held_seat_is_not_swallowed_by_the_absent_exemption():
    """The branch order, pinned. `absent` is the one way of not running that does NOT
    veto — it is a fact about the host, true every round, and vetoing on it makes a
    confident stop permanently unreachable on the unattended boxes. A row that carried
    both words would take the held seat's silence out of the list with it, which is
    the fail-open direction, so the routing word is checked FIRST.

    The pairing cannot arise from `panel.py` — a held seat is installed by
    construction — which is exactly why it is pinned here rather than left to the
    writer: a hand-edited baseline, or a future writer, is where it would come from.
    """
    veto = _veto({"claude": {"ran": True},
                  "codex": {"ran": False, "absent": True,
                            "routing": panel_seats.SEAT_HELD, "skip": "x"}})
    assert any("codex" in v for v in veto), veto


@pytest.mark.parametrize("word,dispatched", panel_seats.SEAT_ROUTING_REASONS.items())
def test_only_the_word_the_vocabulary_calls_undispatched_earns_the_new_line(word,
                                                                            dispatched):
    """The second mutation pin, and it points at the vocabulary rather than at a
    string literal in this file.

    Every DISPATCHED word describes a seat that was asked, so a row carrying one and
    `ran: False` is an ordinary failure and must keep the ordinary line — otherwise a
    crashed seat starts reading as a deliberate saving. Only the word the table marks
    undispatched earns the new sentence. Change which word that is in
    `SEAT_ROUTING_REASONS` and this test follows; change the literal in
    `coverage_veto` alone and it goes red.
    """
    veto = _veto({"claude": {"ran": True},
                  "codex": {"ran": False, "absent": False, "routing": word,
                            "skip": "codex: something happened"}})
    line = [v for v in veto if v.startswith("codex")]
    assert len(line) == 1
    assert ("not asked" in line[0]) is (not dispatched)


def test_a_payload_that_predates_the_field_reads_exactly_as_it_did_before():
    """Three states, at the veto. A row with no `routing` key at all was written by a
    panel that had no routing, so it cannot have held a seat back — it falls through
    to the line it has always produced. Reading an absent field as the held word would
    retro-veto every cycle already on disk; reading it as "asked" is what it means."""
    veto = _veto({"claude": {"ran": True},
                  "codex": {"ran": False, "absent": False,
                            "skip": "codex: timed out after 900s"}})
    assert any("codex did not run" in v for v in veto), veto


# ------------------------------------------------------- load_baseline's fifth reason

def _payload(tmp_path, name, *, round_no=1, reviewers=None, **kw):
    body = {"repo": "board", "github": "acme/board", "pr": 34, "round": round_no,
            "cycle": "cyc", "head_sha": "a" * 40, "reviewers_ran": ["claude"],
            "scope": "pr", "to_fix": [], "dismissed": [], "sonar_findings": [],
            "reviewers": reviewers if reviewers is not None
            else {"claude": {"ran": True, "truncated": False}},
            **kw}
    p = tmp_path / name
    p.write_text(json.dumps(body))
    return str(p)


def _baseline(paths, round_no=2):
    return panel_rounds.load_baseline(list(paths), {"repo": "board",
                                                    "github": "acme/board",
                                                    "pr": 34, "round": round_no})


def test_the_dispatch_set_is_read_back_so_the_next_round_can_complement_it(tmp_path):
    """The prerequisite #775 names first: which seats a round DISPATCHED, persisted
    and read back. Without this the complement is undefined on every round and the
    whole feature is inert."""
    p = _payload(tmp_path, "r1.json",
                 seat_routing={"dispatched": ["claude", "codex"], "held": [],
                               "why": {}, "budget": None, "prior_round": None,
                               "prior_dispatched": None})
    b = _baseline([p])
    assert b.dispatched == {1: {"claude", "codex"}}
    assert b.last_dispatch() == ({"claude", "codex"}, 1)


def test_a_payload_with_no_seat_routing_reads_as_UNKNOWN_and_not_as_nobody(tmp_path):
    """Every payload written before this feature is silent, and `--baseline` is fed
    them by design. Read as "dispatched nobody", every seat becomes a complement seat
    and a tight budget picks the panel by name order — #790's alphabet cut, arriving
    through the reader rather than the writer. `last_dispatch` answers None, and
    `route_seats` turns that into "ask everybody"."""
    b = _baseline([_payload(tmp_path, "r1.json")])
    assert b.dispatched == {}
    assert b.last_dispatch() == (None, None)


def test_an_unreadable_dispatch_set_is_reported_and_costs_the_round_nothing(tmp_path):
    """This function's standing rule — a bad payload costs a `problems` entry, never a
    review every reviewer CLI has already been paid for. The safe fallback here is the
    loud one: with no dispatch set the next round asks everybody, so the cost is a
    round that reads more than the curve asked and the caller is still told why."""
    p = _payload(tmp_path, "r1.json", seat_routing={"dispatched": "claude"})
    b = _baseline([p])
    assert b.dispatched == {}
    assert any("seat_routing.dispatched" in q for q in b.problems), b.problems


def test_a_partially_dispatched_round_does_not_erase_earlier_coverage_gaps(tmp_path):
    """THE second defect, and the one with teeth — `reread` is the most destructive
    thing in `load_baseline`.

    Round 1 was cut by a budget, so it banks a truncation the cycle inherits. Round 2
    holds a seat back and its remaining seat reads the whole PR untruncated — which
    satisfies every OTHER term of `reread`: a per-seat record exists, a seat ran, none
    was truncated, it was not a manifest, and the scope is `pr`. Without the
    `not held_back` term round 2 becomes the round that closes round 1's gap, on the
    strength of a panel that asked one seat. Drop that term and this test goes red
    with `truncated_rounds == set()`.
    """
    r1 = _payload(tmp_path, "r1.json", round_no=1,
                  reviewers={"claude": {"ran": True, "truncated": True,
                                        "max_diff_chars": 200}})
    r2 = _payload(tmp_path, "r2.json", round_no=2,
                  reviewers={"claude": {"ran": True, "truncated": False},
                             "codex": {"ran": False, "absent": False,
                                       "routing": panel_seats.SEAT_HELD}},
                  seat_routing={"dispatched": ["claude"], "held": ["codex"],
                                "why": {}, "budget": 1, "prior_round": 1,
                                "prior_dispatched": ["claude", "codex"]})
    b = _baseline([r1, r2], round_no=3)
    assert b.partial_rounds == {2}
    assert b.truncated_rounds == {1}


def test_a_round_that_asked_everybody_still_closes_them(tmp_path):
    """The signal, unharmed. The new term must not become "no round ever closes a
    gap": a full panel that read the whole PR is exactly the round `reread` exists
    for, and a guard that fired on every round would keep every inherited veto
    standing for the rest of the cycle — the permanent HOLD this module's neighbours
    spend most of their comments removing."""
    r1 = _payload(tmp_path, "r1.json", round_no=1,
                  reviewers={"claude": {"ran": True, "truncated": True,
                                        "max_diff_chars": 200}})
    r2 = _payload(tmp_path, "r2.json", round_no=2,
                  reviewers={"claude": {"ran": True, "truncated": False},
                             "codex": {"ran": True, "truncated": False}},
                  seat_routing={"dispatched": ["claude", "codex"], "held": [],
                                "why": {}, "budget": 4, "prior_round": 1,
                                "prior_dispatched": ["claude", "codex"]})
    b = _baseline([r1, r2], round_no=3)
    assert b.partial_rounds == set()
    assert b.truncated_rounds == set()


def test_an_unreadable_held_list_is_treated_as_a_round_that_may_have_held_one(tmp_path):
    """Every unknown in `load_baseline` fails toward keeping a gap open. The cost of
    this direction is an inherited veto that stands one round longer than it had to;
    the cost of the other is a coverage gap deleted by a round that never looked."""
    r1 = _payload(tmp_path, "r1.json", round_no=1,
                  reviewers={"claude": {"ran": True, "truncated": True,
                                        "max_diff_chars": 200}})
    r2 = _payload(tmp_path, "r2.json", round_no=2,
                  reviewers={"claude": {"ran": True, "truncated": False}},
                  seat_routing={"dispatched": ["claude"], "held": "codex"})
    b = _baseline([r1, r2], round_no=3)
    assert b.truncated_rounds == {1}
    assert any("seat_routing.held" in q for q in b.problems), b.problems


def test_the_complement_is_taken_against_the_LATEST_round_that_SAID(tmp_path):
    """`head_sha`'s rule, applied to this register: a newer payload written by an
    older panel names no dispatch set, and taking the last payload alone would clear
    an answer an earlier one gave. The cost of the fallback is a complement taken
    against a round further back, which reads MORE seats than a strict N-1 complement
    would — never fewer."""
    r1 = _payload(tmp_path, "r1.json", round_no=1,
                  seat_routing={"dispatched": ["claude"], "held": ["codex"]})
    r2 = _payload(tmp_path, "r2.json", round_no=2)          # an older panel wrote it
    assert _baseline([r1, r2], round_no=3).last_dispatch() == ({"claude"}, 1)


# ------------------------------------------------------------------- the whole round

def _round(monkeypatch, tmp_path, *, seats, curve=None, baselines=(), round_no=1):
    """One `run()` with `seats` configured and every one of them present on this box.

    The pre-flight verdict is switched off (`refuse_over_cap_multiple: 0`,
    `manifest_moves: false`) for `test_panel_absent_seat`'s reason: either verdict
    substitutes the round being measured, and what is measured here is which seats
    were asked.
    """
    cfg = {**CFG,
           "review_panel": {"refuse_over_cap_multiple": 0, "manifest_moves": False,
                            **({"round_budgets": {"multipliers": curve}}
                               if curve else {})},
           "reviewers": {n: {"enabled": True, "model": "sonnet"} for n in seats}}
    asked = []

    def fake_review(name, model, prompt, effort="", **kw):
        asked.append(name)
        return panel.ReviewerRun([], None, 10, None)

    monkeypatch.setattr(panel, "load_repo_cfg", lambda n: cfg)
    monkeypatch.setattr(panel_core, "sh", gh_stub())
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate",
                        lambda *a, **k: ([], None, panel.CoverageRuling()))
    out = tmp_path / "r.json"
    assert panel.run("e2e", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baselines), max_rounds=4) == 0
    return json.loads(out.read_text()), asked, tmp_path


def test_the_payload_records_DISPATCHED_apart_from_CONFIGURED(monkeypatch, tmp_path,
                                                              capsys):
    """#775's other named prerequisite, "pinned by its own test": *dispatched* is
    recorded separately from *recommended*.

    On the shipped flat curve the two sets are equal, which is precisely why the
    record has to be explicit — a consumer deriving one from the other would be right
    on every round until the day a curve was written, and wrong on exactly the rounds
    the distinction exists for.
    """
    payload, asked, _ = _round(monkeypatch, tmp_path, seats=FOUR)
    capsys.readouterr()
    assert payload["reviewers_selected"] == sorted(FOUR)
    assert payload["seat_routing"]["dispatched"] == sorted(FOUR)
    assert payload["seat_routing"]["held"] == []
    assert payload["seat_routing"]["budget"] is None
    assert sorted(asked) == sorted(FOUR)


def test_every_seat_with_a_row_carries_a_routing_word(monkeypatch, tmp_path, capsys):
    """Three loops write `reviewer_meta` — the LLM seats, sonarqube and slop — and a
    seat that has a row and no routing word is a seat whose silence has no name. The
    word is stamped in one place after the fact rather than threaded through all
    three, so a future seat cannot forget it."""
    payload, _, _ = _round(monkeypatch, tmp_path, seats=[*FOUR, "slop"])
    capsys.readouterr()
    rows = payload["reviewers"]
    assert rows, "no seat recorded a row at all"
    for name, row in rows.items():
        assert row.get("routing") in panel_seats.SEAT_ROUTING_REASONS, name


def test_a_tapered_round_asks_the_seats_the_last_round_did_not(monkeypatch, tmp_path,
                                                               capsys):
    """The feature, end to end, on a repo that has opted into a curve.

    Round 1 asked claude and codex. `[1.0, 0.5]` puts round 2 at x0.5, so it may ask
    two of four. claude is kept by the code-access floor; the remaining slot goes to
    the first complement seat. codex — which ran last round and is not the floor — is
    the one held, and pi loses the tie-break to antigravity.
    """
    prior = _payload(tmp_path, "r1.json", round_no=1,
                     seat_routing={"dispatched": ["claude", "codex"], "held": [],
                                   "why": {}, "budget": None, "prior_round": None,
                                   "prior_dispatched": None})
    payload, asked, _ = _round(monkeypatch, tmp_path, seats=FOUR, curve=[1.0, 0.5],
                               baselines=[prior], round_no=2)
    report = capsys.readouterr().out
    assert payload["seat_routing"]["dispatched"] == ["antigravity", "claude"]
    assert payload["seat_routing"]["held"] == ["codex", "pi"]
    assert payload["seat_routing"]["budget"] == 2
    assert payload["seat_routing"]["prior_round"] == 1
    assert sorted(asked) == ["antigravity", "claude"]
    # The held seats are RECORDED, not merely absent — this is the whole safety
    # argument, and "no row" is what the cheaper implementation would have written.
    assert payload["reviewers"]["codex"]["ran"] is False
    assert payload["reviewers"]["codex"]["routing"] == panel_seats.SEAT_HELD
    assert payload["reviewers"]["codex"]["absent"] is False
    # ...and the round cannot claim to be clean. Asserted on the LINE and not on
    # `confident`, which is `not veto` and would be False here for half a dozen
    # unrelated reasons — a test reading the boolean would pass without the branch
    # under test existing at all.
    assert any("codex" in v and "not asked" in v
               for v in payload["round_stop"]["veto"]), payload["round_stop"]["veto"]
    assert payload["round_stop"]["confident"] is False
    # The reader of the report is told too, and told which round the complement was
    # taken against — "the seats that did not run last round" is a rule, and a rule is
    # only checkable against a round number.
    assert "were not asked" in report and "codex" in report
    # NOT the degraded-panel banner: this round is narrower on purpose, and a warning
    # that fires on every round of every tapered cycle is one a reader learns to skip.
    assert "panel degraded" not in report


def test_the_shipped_flat_curve_changes_nothing_about_who_is_asked(monkeypatch,
                                                                    tmp_path, capsys):
    """`[1.0]` is what every repo on the fleet runs, and this feature has to cost them
    exactly nothing. Two independent mechanisms guarantee it — no budget is computed
    at 1.0, and a round with no recorded prior dispatch set cuts nothing whatever the
    budget says — and this pins the first with a baseline in hand so the second cannot
    be what makes it pass."""
    prior = _payload(tmp_path, "r1.json", round_no=1,
                     seat_routing={"dispatched": ["claude", "codex"], "held": [],
                                   "why": {}, "budget": None, "prior_round": None,
                                   "prior_dispatched": None})
    payload, asked, _ = _round(monkeypatch, tmp_path, seats=FOUR, curve=[1.0],
                               baselines=[prior], round_no=2)
    capsys.readouterr()
    assert sorted(asked) == sorted(FOUR)
    assert payload["seat_routing"]["held"] == []
    assert set(payload["seat_routing"]["why"].values()) == {"all"}
    # And no routing line in the veto. `confident` itself is not asserted: this
    # fixture's fix range is unreadable, so the round already vetoes for a reason
    # that has nothing to do with seats — and a test that read `confident` alone
    # would go green on a routing veto simply because something else was red too.
    assert not [v for v in payload["round_stop"]["veto"] if "not asked" in v]


def test_a_curve_on_round_one_cuts_nothing_because_there_is_no_complement(monkeypatch,
                                                                          tmp_path,
                                                                          capsys):
    """The three-state rule reaching production. `[0.25]` would cut a four-seat panel
    to one on every round — including the first, where no prior round exists to take a
    complement against. A budget with no order to spend it in is the alphabet cut, so
    round 1 asks everybody and records why."""
    payload, asked, _ = _round(monkeypatch, tmp_path, seats=FOUR, curve=[0.25])
    capsys.readouterr()
    assert sorted(asked) == sorted(FOUR)
    assert payload["seat_routing"]["held"] == []
    assert payload["seat_routing"]["budget"] == 1
    assert set(payload["seat_routing"]["why"].values()) == {"unknown-prior"}


def test_a_held_seat_is_reported_as_well_as_recorded(monkeypatch, tmp_path, capsys):
    """Two consumers, two keys, and a seat in one but not the other is how a round
    comes to look fuller than it was: `reviewers` is the structured record and
    `skipped` is parsed board-side as "<reviewer>: <reason>". `slop`'s precedent — a
    skip reported and deliberately NOT recorded — does not apply here, because slop's
    absences are constants and a held seat is a fact about this round."""
    prior = _payload(tmp_path, "r1.json", round_no=1,
                     seat_routing={"dispatched": ["claude", "codex"], "held": []})
    payload, _, _ = _round(monkeypatch, tmp_path, seats=FOUR, curve=[1.0, 0.5],
                           baselines=[prior], round_no=2)
    capsys.readouterr()
    assert any(s.startswith("codex: not dispatched") for s in payload["skipped"]), \
        payload["skipped"]


def test_the_round_after_a_tapered_one_complements_IT(monkeypatch, tmp_path, capsys):
    """The loop closing: round 2's payload is round 3's prior set, so the seats round
    2 held are the seats round 3 prefers. This is the round trip the whole feature is
    — a writer and a reader that agree — and it is the assertion neither half can pass
    alone."""
    r1 = _payload(tmp_path, "r1.json", round_no=1,
                  seat_routing={"dispatched": ["claude", "codex"], "held": []})
    p2, _, _ = _round(monkeypatch, tmp_path, seats=FOUR, curve=[1.0, 0.5],
                      baselines=[r1], round_no=2)
    capsys.readouterr()
    r2 = tmp_path / "r2.json"
    r2.write_text(json.dumps({**p2, "round": 2}))
    p3, asked3, _ = _round(monkeypatch, tmp_path, seats=FOUR, curve=[1.0, 0.5, 0.5],
                           baselines=[r1, str(r2)], round_no=3)
    capsys.readouterr()
    # Round 2 asked claude and antigravity, so round 3 prefers codex and pi — with
    # claude held in by the floor, which costs pi its slot.
    assert p3["seat_routing"]["prior_round"] == 2
    assert sorted(asked3) == ["claude", "codex"]
