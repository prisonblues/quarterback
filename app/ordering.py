"""What order the plan would be in if only the rules decided (#232, the deterministic slice).

#232 wants one agent that owns the order and is told what its last orders cost.
The half of it that needs no agent at all is this: **most ordering inputs are
mechanical** — dependency edges, review state, CI, changed-file overlap, age —
and "applying a model to a case that had a right answer is an expensive random
number generator". So the rules run here, first, in a pure function, and what
they *cannot* separate is reported as exactly that rather than guessed at.

That report is the point of this module, not a by-product of it. An order whose
derived and judged parts are indistinguishable gets trusted uniformly — usually
too much — so every placement carries the rules that produced it and, where no
rule applied, says so out loud. The set of placements nothing separated is the
remainder a model would be asked about, and it is a field in the output rather
than something a reader has to work out.

**Pure, and deliberately so.** No session, no clock, no claim, no I/O: the caller
reads the plan and the panel runs, hands over :class:`Candidate` values, and gets
an :class:`Ordering` back. That makes every rule testable against a literal in a
test file — and it is what lets the same function order a landing queue (#227)
later, since it never learns what a candidate *is*.

## The rules, in the order they apply

1. **Dependency** (*constraint*). An item follows the items it depends on. This
   asserts nothing — per #183 it removes a contradiction — and it is the one rule
   enforced structurally rather than by a sort key: the walk below cannot emit a
   node before the nodes it waits on, so no preference can outrank it.
2. **Bucket** — ``workable`` before ``waiting`` before ``finished``.
   *waiting* is an item with an open blocker (*constraint*: the plan's own
   ``next`` already skips it, so sinking it asserts nothing new). *finished* is
   an item whose PR was merged or closed **as of its last panel run** — a
   *preference*, because that is a snapshot and snapshots go stale. Waiting sits
   above finished on purpose: blocked work becomes workable, finished work never
   does.
3. **Open work** (*preference*). Red CI, or confirmed findings nobody has
   recorded an outcome for, rises. In a *plan* — as opposed to a landing queue,
   where the same fact sinks a PR — a known red thing is work that exists, is
   already identified, and is holding something up.
4. **Staleness** (*preference*). An open item nobody has touched in
   ``stale_days`` rises, below (3). The threshold is the caller's, and it is
   meant to be the same number the plan already renders ``stale`` at: a reader
   should meet the word once, not twice with two meanings.
5. **Overlap** (*preference, and only on the remainder*). Where the rules above
   left two candidates interchangeable *and* they touch the same files, the one
   closer to landing goes first. This is why changed-file overlap is a
   refinement and not a prerequisite: with it absent (the collision query over
   ``review_run_files`` is #101, still open) nothing above changes and the
   ambiguous set is merely larger.
6. **Nothing else.** Ties fall back to the order that is already in force, which
   is what makes the guarantee below true.

## The guarantee, stated exactly

**No placement is chosen by a coin.** Every item is placed by a rule, or by the
order already in force. It follows that if no rule fires anywhere in a scope, the
suggested order *is* the active order — the common case, and the one worth being
sure of.

It does **not** follow that only rule-bearing items move, and the earlier draft of
this paragraph claimed it did. Inverting a pair the rules do separate drags
whatever sits between them: with an active order of ``slow, bystander, fast`` and
a rule that puts ``fast`` before ``slow``, *something* has to cross a pair no rule
compares — there is no sequence that changes the one relation and no other. Both
minimal repairs disturb exactly one such pair, so the walk takes one and
**labels it**: a ``displaced`` reason names every item that crossed this one
without a rule ordering the two, and both ends of such an inversion carry it. The
test is per PAIR and not per item — an item's own separating rule explains its own
placement, which is a different claim from explaining an inversion — and it leaves
``basis`` alone, because a displaced item's position still was not derived.

So ``moves`` reads as "a rule asked for this, or a rule elsewhere pushed it", and
the reasons say which — never "the coin came down here".

## What it is not

Not a writer. It returns a proposal; ``suggested_order`` is a list shaped exactly
like ``POST /plan/reorder``'s ``order`` so that a *human* can apply it in one
call, which is the whole of #232's non-privileged-writer rule. And not a store:
``inputs_digest`` covers every input except the clock, and each candidate carries
``evidence`` saying where its readings came from — so a recorded proposal can be
compared with a later one, and traced back to the runs it read, without
re-deriving anything or persisting anything from its own prior output.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

#: Bumped when a rule changes, and stored beside every recorded proposal. Two
#: proposals made by different rules are not comparable, and a ledger that cannot
#: say which rules made a row cannot tell a rule change from a world change —
#: which is the only thing the ledger is for.
RULES_VERSION = 1

#: PR states, as of a panel run, that mean the item has no work left in it. Upper
#: case because that is what the GitHub API and ``review_runs.pr_state`` carry;
#: the readers below fold case anyway, since the column is free text.
FINISHED_PR_STATES = frozenset({"MERGED", "CLOSED"})

#: ``review_ci`` in ``harness/loops/panel_scope.py`` reports exactly
#: ``PASS | FAIL | PENDING | none | unknown``, and ``review_runs.ci_status`` is a
#: free-text column an authenticated sender fills — so these two are compared for
#: equality and everything else is treated as "not known to be green" and "not
#: known to be red" respectively. An unrecognised status must never read as
#: either: a typo that ordered the plan would be a rule firing on noise.
CI_PASS = "PASS"
CI_FAIL = "FAIL"

#: The buckets of rule 2, in the order they sort.
WORKABLE, WAITING, FINISHED = 0, 1, 2
BUCKET_NAMES = {WORKABLE: "workable", WAITING: "waiting", FINISHED: "finished"}

#: How a placement came about — the field #232's acceptance criterion is about.
#:
#: * ``constraint`` — a dependency edge or an open blocker put it here. Board-owned
#:   facts that cannot be stale, and removals of a contradiction rather than
#:   assertions.
#: * ``preference`` — a graded rule put it here: finished/open-work/stale/overlap.
#:   Deterministic, but a policy, and (bar none of them) read off a snapshot.
#: * ``ambiguous`` — no rule separated it from a peer that was equally placeable.
#:   It kept the position it already had. **This is the remainder**: the only part
#:   of the order a model would have anything to add to.
#: * ``unopposed`` — nothing to compare it against at the point it was placed.
#:   Not the same as ambiguous, and folding the two would overstate the remainder.
#: * ``unresolved`` — it is in a dependency cycle, so the rules contradict each
#:   other and no placement is derivable. Reported, never repaired.
BASES = ("constraint", "preference", "ambiguous", "unopposed", "unresolved")

#: Which basis each separating rule confers. ``dependency`` and ``blocked`` are
#: the two facts the board itself owns; the rest are read off a panel run's
#: snapshot or are a policy about age, and are labelled accordingly.
_RULE_BASIS = {
    "dependency": "constraint",
    "blocked": "constraint",
    "finished": "preference",
    "open_work": "preference",
    "stale": "preference",
    "overlap": "preference",
}


@dataclass(frozen=True)
class Candidate:
    """One orderable thing, with the facts the rules read and nothing else.

    The caller decides what a candidate *is* — a plan item today, a queued PR
    when #227 exists — and resolves every field before calling. Tri-states are
    kept tri-state: ``None`` means nobody said, and it is never folded into
    ``False``, because "the panel reported CI green" and "no panel has run" are
    the two readings this module must not confuse.
    """

    #: Stable identity, and what ``suggested_order`` is a list of. For a plan item
    #: this is its uuid, so the output can be fed straight to ``/plan/reorder``.
    key: str
    #: Keys this one must follow. Entries naming something outside the candidate
    #: set are ignored *here* — an out-of-scope blocker reaches the rules through
    #: ``blocked``, which is the only thing that can be said about an edge whose
    #: other end is not being ordered.
    depends_on: tuple[str, ...] = ()
    #: It waits on something open — in this set or outside it. The plan's own
    #: ``next`` skips such an item, so this is a constraint rather than a policy.
    blocked: bool = False
    #: The rest are as of the newest panel run for the referenced PR, and NULL
    #: when there is no such run (which is most of a plan).
    pr_state: str | None = None
    draft: bool | None = None
    ci: str | None = None
    #: Confirmed findings on that run for which nobody has recorded an outcome.
    #: ``0`` means the run had confirmed findings and they are all answered, or it
    #: had none; ``None`` means no run to count over.
    outstanding_findings: int | None = None
    #: Days since the item was last touched. Read only through the ``stale_days``
    #: threshold — see :func:`rule_inputs` on why the digest cannot carry the float.
    idle_days: float | None = None
    #: Keys it shares changed files with. Meaningful only when the caller passes
    #: ``overlap_known=True``; an empty set otherwise means "not asked", never
    #: "nothing shared".
    collides_with: tuple[str, ...] = ()
    #: WHERE the fields above were read — a run id, when it ran, what commit it
    #: saw. Opaque here: no rule looks at it, and it must contain nothing that
    #: moves on its own. It exists because a stored reading that cannot be traced
    #: to its source cannot be checked afterwards (#227 asks a proposal to record
    #: "exact inputs used"), and because "the evidence was refreshed" has to be a
    #: different proposal from "nothing happened" even when the order is identical.
    #: Any JSON-serialisable mapping; ``None`` when there was nothing to read.
    evidence: dict | None = None


@dataclass(frozen=True)
class Reason:
    """One rule fact about a candidate, and whether it moved anything.

    Both halves are needed. A listing of only the *separating* rules cannot say
    why an item that is blocked, sunk and then unopposed ended up where it did;
    a listing of every true fact cannot say which of them a reader should hold
    the order against. ``separating`` is the difference between the two.
    """

    rule: str
    detail: str
    separating: bool = False

    def as_dict(self) -> dict:
        return {"rule": self.rule, "detail": self.detail, "separating": self.separating}


@dataclass(frozen=True)
class Placement:
    """Where one candidate landed, and everything behind it."""

    key: str
    index: int
    basis: str
    reasons: tuple[Reason, ...]
    #: Peers it was interchangeable with as far as the rules could tell. Non-empty
    #: is compatible with a ``constraint`` basis: a dependency can fix that an item
    #: comes after another and still leave it swappable with a third.
    ambiguous_with: tuple[str, ...] = ()
    #: Exactly what the rules read about it — see :func:`rule_inputs`.
    inputs: dict = field(default_factory=dict)
    #: The candidate's ``evidence``: where those readings came from. ``None`` when
    #: there was nothing to read, which is most of a plan.
    evidence: dict | None = None

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "index": self.index,
            "basis": self.basis,
            "reasons": [r.as_dict() for r in self.reasons],
            "ambiguous_with": list(self.ambiguous_with),
            "inputs": self.inputs,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class Ordering:
    """A proposal: the order the rules produce, and how much of it they produced."""

    active_order: tuple[str, ...]
    suggested_order: tuple[str, ...]
    placements: tuple[Placement, ...]
    #: Groups of keys nothing separated, as the transitive closure of "these two
    #: were placeable at the same moment and no rule preferred either". Transitive
    #: because that is what a reader wants — the set of positions still open — and
    #: it is stated rather than implied: A may be tied with B and B with C without
    #: A and C ever having been compared.
    ambiguous: tuple[tuple[str, ...], ...]
    #: Keys the walk could not place because they depend on each other. Left in
    #: their active order at the end, basis ``unresolved``.
    cycles: tuple[str, ...]
    rules_version: int
    #: sha256 over every input except the one that moves on its own — see
    #: :func:`_digest`. Two proposals with the same digest were made from the same
    #: world by the same rules, which is what lets a caller record a proposal when
    #: something actually changed rather than once per cron tick.
    inputs_digest: str
    overlap_known: bool
    stale_days: float

    @property
    def changed(self) -> bool:
        return self.active_order != self.suggested_order

    def moves(self) -> tuple[dict, ...]:
        """Where the suggestion differs from what is in force, and by how much."""
        return moves_between(self.active_order, self.suggested_order)

    def as_dict(self) -> dict:
        return {
            "active_order": list(self.active_order),
            "suggested_order": list(self.suggested_order),
            "changed": self.changed,
            "moves": list(self.moves()),
            "placements": [p.as_dict() for p in self.placements],
            "ambiguous": [list(g) for g in self.ambiguous],
            "cycles": list(self.cycles),
            "rules_version": self.rules_version,
            "inputs_digest": self.inputs_digest,
            "overlap_known": self.overlap_known,
            "stale_days": self.stale_days,
            "counts": self.counts(),
        }

    def counts(self) -> dict:
        """How much of this order each basis accounts for.

        The headline number of the whole exercise: ``derived`` against
        ``ambiguous`` is the answer to "how much of this did the rules actually
        decide", and it is the one figure a reader needs before deciding how much
        of the order to believe.
        """
        by = dict.fromkeys(BASES, 0)
        for p in self.placements:
            by[p.basis] += 1
        return {
            **by,
            "entries": len(self.placements),
            "derived": by["constraint"] + by["preference"],
            "moved": len(self.moves()),
            # Not the same number as ``ambiguous``, and both are needed. A
            # placement can be pinned below a blocked item (so: derived) and
            # still be swappable with its neighbour — reporting only the basis
            # counts would say nothing was left open when two positions were.
            "interchangeable": sum(1 for p in self.placements if p.ambiguous_with),
            "ambiguous_groups": len(self.ambiguous),
        }


def moves_between(active: tuple[str, ...] | list[str],
                  suggested: tuple[str, ...] | list[str]) -> tuple[dict, ...]:
    """Where two orders differ, and by how much.

    Derived on read and never stored — including for a proposal read back out of
    the database months later, which is why this is a module function rather than
    a method on the fresh result. A stored ``moves`` would be free to disagree
    with the two orders it came from, and #232's whole argument about a planner
    regenerating from its own prior output applies to its ledger as much as to
    its reasoning.

    A key present in one order and not the other is reported from the order that
    has it; ``from``/``to`` is null on the side that does not. That cannot happen
    for an ordering this module produced — but a stored row is data, and a reader
    of it should get "these two lists disagree about which items exist" rather
    than a KeyError.
    """
    was = {k: i for i, k in enumerate(active)}
    now = {k: i for i, k in enumerate(suggested)}
    out = [
        {"key": k, "from": was.get(k), "to": i,
         "delta": i - was[k] if k in was else None}
        for i, k in enumerate(suggested)
        if was.get(k) != i
    ]
    out.extend({"key": k, "from": i, "to": None, "delta": None}
               for i, k in enumerate(active) if k not in now)
    return tuple(out)


def _upper(v: str | None) -> str | None:
    return v.upper() if v else None


def _finished(c: Candidate) -> bool:
    return _upper(c.pr_state) in FINISHED_PR_STATES


def _open_work(c: Candidate) -> bool:
    """Something red and already identified is attached to this item."""
    return _upper(c.ci) == CI_FAIL or bool(c.outstanding_findings)


def _stale(c: Candidate, stale_days: float) -> bool:
    return c.idle_days is not None and c.idle_days >= stale_days


def _ready(c: Candidate) -> bool:
    """Positively known to be landable — every clause, no inference from absence.

    Used only by the overlap refinement, and deliberately unforgiving: an unknown
    CI status or an unrecorded draft flag makes a candidate *not* ready rather
    than assumed ready, because the refinement's whole claim is "this one is
    closer to landing" and a missing fact is not evidence for it.
    """
    return (
        _upper(c.ci) == CI_PASS
        and c.outstanding_findings == 0
        and c.draft is False
        and _upper(c.pr_state) == "OPEN"
    )


def _bucket(c: Candidate) -> int:
    if _finished(c):
        return FINISHED
    if c.blocked:
        return WAITING
    return WORKABLE


def rule_inputs(c: Candidate, stale_days: float, overlap_known: bool) -> dict:
    """Exactly what the rules read about one candidate.

    Published on every placement and hashed into ``inputs_digest``, and those two
    uses are why it is the *quantised* form rather than the raw fields. ``stale``
    is a boolean because the rules cannot see anything finer than the threshold —
    and a digest built over ``idle_days`` would change every second, which would
    make it useless for the one thing a digest is for here (telling a world that
    moved from a clock that ticked). The raw readings still ride along on the
    placement's ``inputs`` for a human, under names that say they are readings.
    """
    return {
        "bucket": BUCKET_NAMES[_bucket(c)],
        "blocked": c.blocked,
        "finished": _finished(c),
        "open_work": _open_work(c),
        "stale": _stale(c, stale_days),
        "ready": _ready(c),
        "depends_on": sorted(c.depends_on),
        # NULL when nobody asked, ``[]`` when nobody shares a path. The same
        # distinction ``unread_files`` and ``stop_veto`` keep in ``review_runs``,
        # and here it is the difference between "overlap says these two are
        # unrelated" and "#101 is still open so overlap said nothing".
        "collides_with": sorted(c.collides_with) if overlap_known else None,
    }


def _readings(c: Candidate) -> dict:
    """The raw fields behind :func:`rule_inputs`, for a reader rather than a rule."""
    return {
        "pr_state": c.pr_state,
        "draft": c.draft,
        "ci": c.ci,
        "outstanding_findings": c.outstanding_findings,
        "idle_days": c.idle_days,
    }


def _sort_key(c: Candidate, stale_days: float) -> tuple[int, int, int]:
    """Rules 2-4 as one ascending key. Rule 1 is the walk; rule 5 breaks ties."""
    return (
        _bucket(c),
        0 if _open_work(c) else 1,
        0 if _stale(c, stale_days) else 1,
    )


def _separating_rule(best: Candidate, rival: Candidate, stale_days: float) -> str | None:
    """The first rule that put ``best`` ahead of ``rival``, or None if none did.

    The bucket component is two facts, not one, so it is decomposed rather than
    reported as "bucket": a workable item ahead of a waiting one was separated by
    a constraint, and anything ahead of a finished one by a preference read off a
    snapshot. Reporting both as the same rule would put the two labels this whole
    module exists to keep apart back into one field.
    """
    kb, kr = _sort_key(best, stale_days), _sort_key(rival, stale_days)
    if kb == kr:
        return None
    if kb[0] != kr[0]:
        return "finished" if _bucket(rival) == FINISHED else "blocked"
    if kb[1] != kr[1]:
        return "open_work"
    return "stale"


#: How a separating rule is worded on a placement. One sentence per rule, so a
#: reader meets the same words for the same rule everywhere and can hold the
#: order against them.
_RULE_DETAIL = {
    "blocked": "workable work before work waiting on an open blocker",
    "finished": "unfinished work before an item whose PR was merged or closed "
                "as of its last panel run",
    "open_work": "work carrying red CI or unanswered confirmed findings first",
    "stale": "work untouched past the staleness threshold first",
    "overlap": "of two items touching the same files, the one closer to landing first",
}

#: Why an item moved past something no rule of its compares it with. Not a rule and
#: not a separation: it is the cost of applying a rule to a pair that had something
#: between it, and it is said out loud because an unexplained move in a proposal
#: whose entire selling point is stated reasons is the one entry a reader cannot
#: check.
_DISPLACED = ("moved {delta:+d} place(s): {crossed} crossed it on a rule that does not "
              "compare them, and no sequence inverts one pair without disturbing what sat "
              "between")

#: How the same rule is worded when it is merely TRUE of a candidate and did not
#: move it. Separate strings on purpose: "this item is blocked" and "this item is
#: ahead of a blocked one" are the two halves a single wording would blur.
_FACT_DETAIL = {
    "blocked": "waiting on an open blocker",
    "finished": "its PR was merged or closed as of its last panel run",
    "open_work": "carries red CI or confirmed findings nobody has answered",
    "stale": "untouched past the staleness threshold",
    "ready": "green CI, no unanswered findings, open and not a draft",
}

#: At most this many keys are named in a ``dependency`` detail before it counts
#: the rest. ``MAX_DEPS`` in the plan API is 32, and a reason line naming all of
#: them is a reason line nobody reads.
_NAMED_KEYS = 3


def _names(keys: tuple[str, ...] | list[str]) -> str:
    head = list(keys)[:_NAMED_KEYS]
    rest = len(keys) - len(head)
    return ", ".join(head) + (f" (+{rest} more)" if rest else "")


def _collide(a: str, b: str, by_key: dict[str, Candidate]) -> bool:
    """Do these two share a changed file?

    Symmetric by construction rather than by assumption. ``collides_with`` is
    supplied by the caller, and a collision query that reported one direction only
    would otherwise make the rule below depend on which of the pair happened to be
    asked about — an ordering that changed with the spelling of its input.
    """
    return b in set(by_key[a].collides_with) or a in set(by_key[b].collides_with)


def _peers(key: str, group: list[str], by_key: dict[str, Candidate]) -> list[str]:
    """The members of ``group`` that ``key`` itself shares a file with.

    **Pairwise, and that is the whole rule.** This has now been wrong twice in the
    same way, which is what makes it worth stating rather than patching a third
    time (#67). The first version asked only *whether* the tied group contained a
    colliding pair and then readiness-sorted the whole group, so an item sharing no
    file with anybody could be moved and come back labelled ``overlap`` — a reason
    that was not true of it. The second narrowed that to the group's colliding
    members and was wrong for the same reason one level in: with two DISCONNECTED
    pairs in one group, the readiest member of pair B could be promoted past the
    head of pair A, and those two share nothing either.

    Both instances came of asking a question about a SET when the fact available is
    about a PAIR. So the refinement only ever compares one item with the items it
    itself collides with, and any promotion it makes rests on a collision between
    exactly those two.
    """
    return [k for k in group if k != key and _collide(key, k, by_key)]


def _all_separations(key: str, facts: dict, deps: dict[str, tuple[str, ...]],
                     dependents: dict[str, list[str]]) -> list[str]:
    """Every rule that placed this item: the graded ones, overlap, and dependency.

    ``dependency`` is decided here rather than stored in the walk, because it is a
    property of the edges and not of the step: an item with an in-set dependency is
    necessarily after it, and one with a dependent necessarily before it, whichever
    order the walk happened to reach them in.
    """
    out = list(facts.get(key, {}).get("separating", []))
    if deps.get(key) or dependents.get(key):
        out.append("dependency")
    return out


def _basis(separating: list[str], tied: bool) -> str:
    """The strongest thing that determined a placement.

    ``tied`` is asked of the finished tie GROUPS rather than of the step that
    placed the candidate, and the difference is the last item in a group: nothing
    was left to compare it against by the time it was placed, so a step-local
    answer called it ``unopposed`` while the two items above it were correctly
    reported as interchangeable with it.
    """
    kinds = {_RULE_BASIS[r] for r in separating}
    if "constraint" in kinds:
        return "constraint"
    if "preference" in kinds:
        return "preference"
    return "ambiguous" if tied else "unopposed"


def suggest_order(
    candidates: list[Candidate] | tuple[Candidate, ...],
    *,
    stale_days: float = 14.0,
    overlap_known: bool = False,
) -> Ordering:
    """Order the candidates by the rules, and say what the rules did not decide.

    The input order **is** the active order: the sequence currently in force, and
    the tiebreak of last resort. Pass the plan in rank order and a suggestion that
    moves nothing comes back identical, which is the common case and should be.

    ``overlap_known`` says whether ``collides_with`` was populated at all. It is a
    separate flag rather than an inference from empty sets because "these two
    share no path" and "nobody ran the collision query" are different facts with
    different consequences, and only one of them is a reason to order anything.

    Raises ``ValueError`` on a duplicate key: two candidates with one identity
    would silently drop one from the order, and a proposal that loses an item is
    worse than no proposal.
    """
    cands = list(candidates)
    by_key: dict[str, Candidate] = {}
    for c in cands:
        if c.key in by_key:
            raise ValueError(f"duplicate candidate key {c.key!r}")
        by_key[c.key] = c
    active = tuple(c.key for c in cands)
    at = {k: i for i, k in enumerate(active)}

    # Edges are restricted to the candidate set, and a self-edge is dropped. The
    # plan API refuses both a cycle and a self-dependency at write time, so
    # neither should arrive — but this function is also the one #227 would call
    # with edges it derives itself, and a dependency on something not being
    # ordered is not an edge this walk can honour. It reaches the rules as
    # ``blocked`` instead, which is the whole of what can be said about it.
    deps = {
        c.key: tuple(dict.fromkeys(d for d in c.depends_on if d in by_key and d != c.key))
        for c in cands
    }
    dependents: dict[str, list[str]] = {k: [] for k in active}
    for key, ds in deps.items():
        for d in ds:
            dependents[d].append(key)

    keyof = {c.key: _sort_key(c, stale_days) for c in cands}
    indeg = {k: len(deps[k]) for k in active}
    available = {k for k in active if not indeg[k]}

    order: list[str] = []
    facts: dict[str, dict] = {}
    ties: list[tuple[str, str]] = []
    #: Pairs the overlap refinement decided. Not an edge in the walk — the rule is a
    #: preference, not a constraint — but it IS a pair a rule ordered, which is what
    #: the displacement note has to know.
    overlap_edges: list[tuple[str, str]] = []

    while available:
        ranked = sorted(available, key=lambda k: (keyof[k], at[k]))
        chosen = ranked[0]
        tied = [k for k in ranked if keyof[k] == keyof[chosen]]
        separating: list[str] = []

        # Rule 5, and it fires ONLY here — on a tie, among the members of the tied
        # group that actually touch each other's files, and only when readiness
        # tells those members apart. That is what makes overlap a refinement:
        # absent, this branch never runs and the group stays ambiguous; present, it
        # converts ambiguity into a decision and nothing else about the order
        # changes.
        #
        # It can only take the head slot from an item it itself collides with — see
        # :func:`_peers`. An item at the head that shares no file with the
        # contender is not something an overlap fact can say anything about, and
        # the contender is promoted at whichever later step it reaches a head it
        # does collide with.
        contenders = [chosen, *_peers(chosen, tied, by_key)] if overlap_known else []
        if len(contenders) > 1:
            readiest = min(contenders, key=lambda k: (0 if _ready(by_key[k]) else 1, at[k]))
            # Only the items the winner collides with, and only those readiness
            # tells apart from it: those two facts together ARE the decision, and
            # anything else in the group was never compared with it.
            demoted = [k for k in _peers(readiest, tied, by_key)
                       if _ready(by_key[k]) != _ready(by_key[readiest])]
            if demoted:
                # Recorded whether or not the winner had to move, because "a rule
                # decided this head" and "no rule had anything to say" are
                # different answers and the incumbent order is frequently right.
                # Reporting a confirmed placement as ambiguous would put it in the
                # remainder a model is asked about, which is where it does not
                # belong: the rules have already answered it.
                chosen = readiest
                separating.append("overlap")
                # On the demoted ones too, not only the winner. A refinement that
                # explains the winner and leaves the loser reading "nothing
                # separated me" is half a record, and the loser is the placement
                # somebody will want to argue with.
                for m in demoted:
                    facts.setdefault(m, {})["overlap_after"] = chosen
                    overlap_edges.append((chosen, m))
                # Everything else stays interchangeable with the winner: no rule
                # compared them, which is exactly what the tie group means.
                tied = [k for k in tied if k not in demoted]

        if "overlap_after" in facts.get(chosen, {}):
            separating.append("overlap")

        partners = [k for k in tied if k != chosen]
        ties.extend((chosen, k) for k in partners)
        facts.setdefault(chosen, {})["overlap_separating"] = separating
        order.append(chosen)

        available.discard(chosen)
        for d in dependents[chosen]:
            indeg[d] -= 1
            if not indeg[d]:
                available.add(d)

    # Anything left is in a dependency cycle: the rules contradict each other, so
    # no placement is derivable. Reported and appended in the order already in
    # force — never "repaired", because every repair here is a guess about which
    # edge was the wrong one, and the plan API refuses cycles at write time
    # precisely so that a human decides that.
    cycles = tuple(k for k in active if k not in set(order))
    order.extend(cycles)

    # Which pairs a rule ordered, for the displacement test below. Dependency
    # REACHABILITY rather than the direct edges: an item is ordered against its
    # grandparent too, and calling that crossing unexplained would be a note about
    # a pair the walk did decide.
    related = _reachable(deps, dependents)
    for a, b in overlap_edges:
        related[a].add(b)
        related[b].add(a)

    # Which graded rule put each item where, read off the FINISHED order rather
    # than off the step that emitted it. The step-local version compared each item
    # with its predecessor and with the best rival still available, and that misses
    # a whole tail: with `[blocked1, blocked2, free]` the second blocked item has
    # no un-emitted rival left and ties with the first, so it came back
    # `ambiguous` with no reason at all — while the bucket rule had demonstrably
    # put it below `free`. Reported as underived work in `counts`, and as an
    # unexplained move to anybody reading the entry (Codex, review pass five).
    in_a_cycle = set(cycles)
    for i, key in enumerate(order):
        # Cycle members are appended to the order without ever having been walked,
        # so they have no facts and no derivable placement — `unresolved` is their
        # whole answer and asking a graded rule about them would invent one.
        if key in in_a_cycle:
            continue
        entry = facts.setdefault(key, {})
        entry["separating"] = _graded_separations(
            key, i, order, keyof, by_key, stale_days, entry)

    groups = _tie_groups(ties)
    group_of = {k: g for g in groups for k in g}
    placements = tuple(
        Placement(
            key=k,
            index=i,
            basis="unresolved" if k in set(cycles) else _basis(
                _all_separations(k, facts, deps, dependents), k in group_of),
            reasons=_reasons_with_displacement(
                by_key[k], facts.get(k, {}), deps[k], tuple(dependents[k]),
                stale_days, in_cycle=k in set(cycles), key=k, was=at[k], now=i,
                order=order, at=at, keyof=keyof, related=related),
            ambiguous_with=tuple(x for x in group_of.get(k, ()) if x != k),
            inputs={
                **rule_inputs(by_key[k], stale_days, overlap_known),
                "readings": _readings(by_key[k]),
            },
            evidence=by_key[k].evidence,
        )
        for i, k in enumerate(order)
    )
    return Ordering(
        active_order=active,
        suggested_order=tuple(order),
        placements=placements,
        ambiguous=groups,
        cycles=cycles,
        rules_version=RULES_VERSION,
        inputs_digest=_digest(cands, active, stale_days, overlap_known),
        overlap_known=overlap_known,
        stale_days=stale_days,
    )


def _displacement(key: str, was: int, now: int, order: list[str], at: dict[str, int],
                  keyof: dict[str, tuple], related: dict[str, set[str]]) -> Reason | None:
    """Which items crossed this one that no rule of its own accounts for.

    Returns None when nothing did — the ordinary case, including every move a rule
    asked for. The test is per PAIR, not per item, and that is the correction: a
    first version suppressed this note whenever the item had *any* separating
    reason of its own, so an item pinned by a dependency edge and then crossed by
    an unrelated overlap promotion reported the dependency and said nothing about
    the crossing. Its own rule explained its own placement and not that inversion,
    which is a different claim (Codex, review pass four).

    A crossing is accounted for when the two are ordered by something:

    * their sort keys differ — the graded rules ordered that pair by construction;
    * one reaches the other through dependency edges — the walk did;
    * the overlap refinement fired between exactly those two.

    Anything else inverted with nothing said about it, which is what this reports.
    """
    index = {k: i for i, k in enumerate(order)}
    crossed = [
        k for k in order
        if k != key
        and (at[k] < was) != (index[k] < now)
        and keyof[k] == keyof[key]
        and k not in related[key]
    ]
    if not crossed:
        return None
    return Reason("displaced", _DISPLACED.format(delta=now - was, crossed=_names(crossed)),
                  separating=False)


def _reasons(c: Candidate, seen: dict, deps: tuple[str, ...], dependents: tuple[str, ...],
             stale_days: float, in_cycle: bool) -> tuple[Reason, ...]:
    """Every rule fact about one candidate, with the separating ones marked."""
    if in_cycle:
        return (Reason("dependency_cycle",
                       "in a dependency cycle: no order satisfies these edges, so this "
                       "position is the one already in force", separating=False),)
    out: list[Reason] = []
    separating = set(seen.get("separating", []))
    if deps or dependents:
        separating.add("dependency")
    if "dependency" in separating:
        parts = []
        if deps:
            parts.append(f"follows {_names(deps)}")
        if dependents:
            parts.append(f"precedes {_names(dependents)}")
        out.append(Reason("dependency", "; ".join(parts), separating=True))
    for side, word in (("below", "after"), ("above", "before")):
        pair = seen.get(side)
        if pair:
            other, rule = pair
            out.append(Reason(rule, f"placed {word} {other} — {_RULE_DETAIL[rule]}",
                              separating=True))
    if "overlap" in separating:
        after = seen.get("overlap_after")
        where = (f"placed after {after}" if after
                 else "placed ahead of the peers it shares files with")
        out.append(Reason("overlap", f"{where} — {_RULE_DETAIL['overlap']}", separating=True))
    named = {r.rule for r in out}
    for rule, true_of in (("blocked", c.blocked), ("finished", _finished(c)),
                          ("open_work", _open_work(c)), ("stale", _stale(c, stale_days)),
                          ("ready", _ready(c))):
        if true_of and rule not in named:
            out.append(Reason(rule, _FACT_DETAIL[rule], separating=False))
    return tuple(out)


def _reasons_with_displacement(c: Candidate, seen: dict, deps: tuple[str, ...],
                               dependents: tuple[str, ...], stale_days: float, in_cycle: bool,
                               *, key: str, was: int, now: int, order: list[str],
                               at: dict[str, int], keyof: dict[str, tuple],
                               related: dict[str, set[str]]) -> tuple[Reason, ...]:
    """:func:`_reasons`, plus the note for any crossing its own rules do not explain.

    Not gated on the item having no separating reason: those two questions are
    different. "Why is this item here" and "why did it cross that one" can have
    different answers, and only the first is what a separating reason is about.
    """
    out = _reasons(c, seen, deps, dependents, stale_days, in_cycle)
    if in_cycle:
        return out
    note = _displacement(key, was, now, order, at, keyof, related)
    return (*out, note) if note else out


def _graded_separations(key: str, index: int, order: list[str], keyof: dict[str, tuple],
                        by_key: dict[str, Candidate], stale_days: float,
                        seen: dict) -> list[str]:
    """The rules that placed one item, with a witness for each direction.

    Over every other item rather than a neighbour, and only for pairs the final
    order AGREES with: a dependency edge is allowed to override a graded rule (a
    finished item that something still waits on stays above it), and reporting
    "placed before X by the finished rule" about a pair that came out the other way
    round would be a reason that is not true of the order it describes.

    The witness is the CLOSEST such item in the final order — the one a reader
    checking the entry against the list will actually look at.
    """
    out: list[str] = []
    above = [k for i, k in enumerate(order)
             if i < index and keyof[k] < keyof[key]]
    below = [k for i, k in enumerate(order)
             if i > index and keyof[k] > keyof[key]]
    if above:
        witness = above[-1]
        rule = _separating_rule(by_key[witness], by_key[key], stale_days)
        if rule:
            out.append(rule)
            seen.setdefault("below", (witness, rule))
    if below:
        witness = below[0]
        rule = _separating_rule(by_key[key], by_key[witness], stale_days)
        if rule:
            out.append(rule)
            seen.setdefault("above", (witness, rule))
    return out + seen.get("overlap_separating", [])


def _reachable(deps: dict[str, tuple[str, ...]],
               dependents: dict[str, list[str]]) -> dict[str, set[str]]:
    """For each key, everything it is ordered against by dependency edges.

    Ancestors and descendants, transitively. A plan is tens of rows and the cap on
    an order is five hundred, so the straightforward walk per node is the right
    amount of machinery here.
    """
    out: dict[str, set[str]] = {}
    for start in deps:
        seen: set[str] = set()
        for step in (deps, dependents):
            frontier = [start]
            while frontier:
                node = frontier.pop()
                for nxt in step.get(node, ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        frontier.append(nxt)
        out[start] = seen - {start}
    return out


def _tie_groups(pairs: list[tuple[str, str]]) -> tuple[tuple[str, ...], ...]:
    """Transitive closure of "no rule preferred either of these two".

    Union-find rather than the pairs themselves, because the question a reader has
    is "which positions are still open", and that is a set. Stated as a closure in
    :class:`Ordering` because it is one: A tied with B and B with C are reported as
    one group of three even though A and C may never have been compared.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    seen: list[str] = []
    for a, b in pairs:
        for k in (a, b):
            if k not in parent:
                seen.append(k)
                parent[k] = k
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups: dict[str, list[str]] = {}
    for k in seen:
        groups.setdefault(find(k), []).append(k)
    return tuple(tuple(g) for g in groups.values() if len(g) > 1)


#: The one input deliberately left out of :func:`_digest`, and the only one that
#: moves with nothing happening. Everything else in a candidate is a fact about
#: the world that changed because somebody changed it.
_TICKS = ("idle_days",)


def _digest(cands: list[Candidate], active: tuple[str, ...], stale_days: float,
            overlap_known: bool) -> str:
    """A hash over every input except the one that changes on its own.

    The rule, stated once so it can be checked: **the digest covers each fact the
    rules read and where it came from, and excludes only the clock.** Two
    proposals with one digest were made from the same world by the same rules, and
    that is what lets a caller record a proposal when something actually changed
    rather than once per cron tick.

    The exclusion is `idle_days` alone, because the rules cannot see anything
    finer than the staleness threshold and the quantised flag is already in
    ``rule_inputs`` — a digest over the float would change every second and
    deduplicate nothing.

    Everything else is in, and the first draft was wrong to hold only the
    QUANTISED inputs (Codex found it on review). Under that version a rival PR
    panelled for the first time, or a confirmed finding somebody finally recorded
    an outcome for, left every rule flag untouched and so read as "nothing has
    happened" — and the ledger, whose entire purpose is to show when the answer
    moved and why, would have silently skipped the row that said so.

    Includes the active order too, because the same facts against a different
    incumbent sequence are a different proposal, and the rules version, because
    the same facts under different rules are a different answer.
    """
    payload = {
        "rules_version": RULES_VERSION,
        "stale_days": stale_days,
        "overlap_known": overlap_known,
        "active_order": list(active),
        "inputs": {
            c.key: {
                **rule_inputs(c, stale_days, overlap_known),
                **{k: v for k, v in _readings(c).items() if k not in _TICKS},
                "evidence": c.evidence,
            }
            for c in cands
        },
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()
