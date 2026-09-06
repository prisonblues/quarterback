"""What happened to a finding BETWEEN rounds — the vocabulary, in one place (#772).

A finding exists inside a round and nowhere else. It is raised, judged, maybe
fixed, and what became of it survives as prose in a report plus, if somebody
remembers, one row in ``review_finding_outcomes``. Nothing carries a finding's
STATE from one round into the next, and the two costs of that are measured: a
deferred finding comes back next round looking like fresh damage, which inflates
``escalate_on.fix_injection`` and stops cycles that were converging; and "did
round 3's fix answer round 2's complaint" cannot be answered without a human
reading two reports. No cycle this board has ever recorded is marked converged —
0 of 27 — and this is one of the reasons.

This module holds the vocabulary and nothing else. It imports nothing from the
app, on ``app/needs_human.py``'s rule and for its reason: the producer, the API,
the database CHECK and the tests must read the same tuple rather than four
paraphrases of it, which is the drift this board has already paid for once over
``provenance``.

**Two axes, not one, and this is the argument for the second table.**
``ReviewFindingOutcome`` records what a PERSON (or the fixer acting for one)
concluded about a defect: one terminal row per ``(repo, pr, finding_key)``,
carrying the evidence for a refutation and feeding a published precision figure.
It answers *what was decided*. This vocabulary answers a different question —
*where the defect stood at the end of round N, and who put it there* — and it
answers it once per round, not once per defect.

The two must not be one table, on the outcome table's own argument turned round.
That table refuses to be per-round because a defect raised in rounds 2, 3 and 4
is one refutation and three observations, and a per-round outcome would multiply
one refutation by however many rounds the loop ran — largest exactly where the
measurement matters most. A ledger has the opposite requirement: it is worthless
unless every row is stamped with the round it happened in. Adding ``round`` to
the outcome table would break the unique constraint that makes "what happened to
this?" a question with one answer; adding ``state`` to it would silently
overwrite the decision a human recorded with whatever the last round's mechanical
disposal was. So: two tables, one per question, allowed to disagree — exactly the
way ``ReviewFinding.verdict`` and ``ReviewFindingOutcome.outcome`` are already
kept apart, and for the same reason.

**The vocabularies are nested, not parallel.** :data:`OUTCOME_STATES` IS
``app.api.reviews.OUTCOMES``, imported from here rather than restated there, so a
sixth outcome becomes a sixth lifecycle state on the commit that adds it and
cannot become a second vocabulary that means almost the same thing. What the
ledger adds is three states an outcome cannot express, because they are not
decisions: :data:`LIFECYCLE_ONLY_STATES`.

**Named skip reasons, not one bucket** — the point of the whole feature.
"Nobody looked at it" and "somebody looked and said no" are different facts and
must be stored as different facts. ``unpaid`` and ``refuted`` are those two
facts, and ``source`` says which writer produced each, so the pair
``(unpaid, budget)`` and the pair ``(refuted, judge)`` can never be read as one
population. mergeCraft (MIT, ``findings/ledger.py``) stamps five writers with
five ``source`` values for this reason and it is the part of that design most
worth taking: a single ``skipped`` bucket makes a budget that is too small look
identical to a reviewer being wrong.
"""

from __future__ import annotations

#: The five recorded OUTCOMES, and the definition of ``app.api.reviews.OUTCOMES``
#: rather than a copy of it. Order is load-bearing only in that it is the order
#: rejection messages render, and it is the order that endpoint has always used.
#:
#: Each is a DECISION somebody took about a defect, which is why each is also a
#: legal ledger state: recording "round 4 fixed it" in the ledger and "this defect
#: was fixed" on the outcome row are the same claim at two grains, and forcing the
#: ledger to spell it with a different word would make the two unjoinable for no
#: gain.
OUTCOME_STATES: tuple[str, ...] = ("fixed", "narrowed", "refuted", "deferred",
                                   "superseded")

#: The three states no outcome can express, because none of them is a decision.
#:
#: * ``raised`` — a round put this finding on the table and it is open work. The
#:   ordinary state, and the one a promotion returns a finding to.
#: * ``unpaid`` — a budget ran out before anybody looked. **Nobody judged this**,
#:   which is why it cannot be spelled ``deferred``: a deferral is somebody's
#:   decision to park a finding they have read, and an unpaid finding has been
#:   read by no one. Collapsing the two is the single failure this vocabulary
#:   exists to prevent — it makes a verification budget that is too small look
#:   like a reviewer exercising judgement, and it makes the fix (raise the budget)
#:   invisible.
#: * ``escalated`` — no fix round can settle it; a person owes an answer
#:   (:mod:`app.needs_human`). Distinct from ``deferred`` in the same direction:
#:   a deferred finding is coming back to a fix pass, an escalated one never is.
LIFECYCLE_ONLY_STATES: tuple[str, ...] = ("raised", "unpaid", "escalated")

#: The closed state vocabulary. Constrained in the database as well as at ingest,
#: on ``ck_review_finding_outcomes_vocabulary``'s reason: this table feeds a
#: convergence figure, and an unknown value would silently leave every numerator
#: while still counting as coverage.
LIFECYCLE_STATES: tuple[str, ...] = (*LIFECYCLE_ONLY_STATES, *OUTCOME_STATES)

#: States that still OWE work — the ledger's answer to "what is this PR still
#: carrying?". ``unpaid`` is here beside ``raised`` deliberately: a finding nobody
#: was paid to look at is outstanding in the strongest sense, and filing it with
#: the settled states is how an unread finding becomes an invisible one.
#: ``escalated`` is NOT here — it owes a person, not a round, and #279's whole
#: point is that a fix pass must never be briefed with one.
OPEN_STATES: frozenset[str] = frozenset({"raised", "unpaid"})

#: States a :data:`SOURCE_PROMOTION` entry may return to ``raised`` from. Both are
#: "this finding is coming back": ``deferred`` because somebody parked it and the
#: next round's diff has touched it again, ``unpaid`` because the budget that
#: skipped it may not skip it twice. A promotion from a settled state is refused —
#: a fixed or refuted defect that reappears is a new observation and the round
#: that raises it says so, rather than a ledger edit quietly reopening a decision
#: somebody made.
PROMOTABLE_STATES: frozenset[str] = frozenset({"deferred", "unpaid"})

#: States that cost a reason, refused without one at the API AND at the database.
#: Everything except ``raised`` and ``fixed``, and the cut is: those two are the
#: loop working, and every other state is a claim that this finding is NOT being
#: worked. A bare one is a confident assertion with nothing behind it — the exact
#: shape ``ck_review_finding_outcomes_refuted_note`` refuses one table over — and
#: here it lands in the number that decides whether a cycle converged.
REASON_REQUIRED_STATES: frozenset[str] = frozenset(
    set(LIFECYCLE_STATES) - {"raised", "fixed"})

#: The writers. Each stamps its own value, so two rows reaching the same state by
#: different routes stay two facts.
#:
#: * ``panel`` — a review round raised or re-raised the finding.
#: * ``judge`` — the round's adjudicator ruled on it. Somebody LOOKED.
#: * ``budget`` — a limit ran out. Nobody looked, and this is the half of the pair
#:   that the single-bucket design loses.
#: * ``fix-pass`` — a fix pass acted on it.
#: * ``promotion`` — the round-to-round move: a parked finding came back. Its own
#:   source, not folded into ``panel``, because "this round raised it" and "this
#:   round is the round it came back in" are the two sentences a reader of a chain
#:   needs to tell apart, and only one of them counts as new damage.
#: * ``human`` — a person at a terminal said so.
LIFECYCLE_SOURCES: tuple[str, ...] = ("panel", "judge", "budget", "fix-pass",
                                      "promotion", "human")

#: The source a promotion must carry, named once so the API rule and the tests
#: cannot drift from the vocabulary.
SOURCE_PROMOTION = "promotion"

#: The longest stored reason. ``MAX_NOTE_CHARS``' bound one table over, and for
#: its reason: long enough for the argument, bounded because an authenticated
#: sender is not a bounded one.
MAX_LIFECYCLE_REASON_CHARS = 4000

if not set(LIFECYCLE_STATES) >= OPEN_STATES:  # pragma: no cover - import guard
    raise RuntimeError("OPEN_STATES names a state outside LIFECYCLE_STATES")
if not set(LIFECYCLE_STATES) >= PROMOTABLE_STATES:  # pragma: no cover - import guard
    raise RuntimeError("PROMOTABLE_STATES names a state outside LIFECYCLE_STATES")
if SOURCE_PROMOTION not in LIFECYCLE_SOURCES:  # pragma: no cover - import guard
    raise RuntimeError("SOURCE_PROMOTION is not one of LIFECYCLE_SOURCES")


def sql_list(values: tuple[str, ...] | frozenset[str]) -> str:
    """A vocabulary rendered for a CHECK constraint.

    Composed from the tuple rather than spelled out in the constraint, which is
    the whole point of this module having no imports: #578 found two constraints
    still naming six ``needs_human`` classes after the vocabulary had grown to
    seven, because both had been written as literals. A state added here reaches
    the database CHECK through the next migration and nowhere does a second list
    have to be remembered.
    """
    return ", ".join(f"'{v}'" for v in sorted(values))
