"""Proposing an order for the PRs in one merge queue — #80's half of #227.

Pure and I/O-free, like :mod:`app.collisions`, :mod:`app.overlap` and
:mod:`app.sync`. :mod:`app.collisions` decides what one rival *is*; this decides
what a queue of them should *do*, and the split is deliberate — every question
about whether the evidence supports a verdict is already answered there, and
re-answering it here is how two modules end up disagreeing about one PR.

**The order is a proposal and nothing here can act on it.** Nothing in this
module or its caller mutates ``active_order``, and a rank is not permission to
merge. #227's strongest sentence is the reason: *"agents may propose order; they
must not silently rewrite the queue while also trying to land… otherwise the
queue itself becomes another shared resource every agent thrashes."*

What a collision costs
======================

#80 models a 28-PR backlog at ~378 integration merges and asks for an order that
reduces it. The first thing to say is that **reordering cannot reduce the
count.** Every pair of colliding PRs pays exactly one re-integration whichever of
the two lands first, so the number of integrations a queue costs is a property of
its collision *graph* and is invariant under permutation. Anyone who claims a
ranking made the quadratic smaller has measured something else.

What an order changes is **which end of each pair pays**, and the two ends are
not alike. When PR *i* lands, the work falls on *j*: merge the moved base into
*j*, re-run *j*'s CI, re-run *j*'s panel round against *j*'s diff. So the cost of
one collision, charged once, is a function of the PR that is *late*:

    cost(order) = Σ over pairs (i before j) that collide of  shared(i, j) · w(j)

with ``w`` the price of putting a branch through one more integration. Swapping
an adjacent colliding pair changes that sum by ``shared·(w(i) - w(j))`` — the
shared count cancels — so the sum is minimised, for every pair at once, by
sorting **heaviest first**. The expensive branch lands while it is still clean
and the cheap ones rebase onto it, instead of the expensive one being re-merged
against a base that moved under it.

That is the opposite of the intuitive "small PRs first", and it is the one #80's
own casualty list argues for. Both silent breakages it records were big
structural branches meeting a moved main: a function that a branch had *moved*
meeting a main that already had it, and ``panel.py`` coming back stale from a
split and costing 44 test failures. Neither was a small diff being re-merged.

``w`` is the changed-file count, and it is a proxy — stated here because the
payload says it too. The board holds no measure of how hard a particular branch
is to re-merge; file count is the only size it has, and it is at least monotone
in the two things that matter (how much rebasing there is to do, and how much
surface a wrong merge can hide in).

What a collision is measured in
===============================

**Paths, and paths over-report.** Two PRs editing different functions of one file
usually merge without a conflict, and this module will still call them
contended. :class:`~app.models.review.ReviewRunFile` stores paths and not hunk
ranges — *"paths answer 'will these two collide', ranges answer 'and where', and
nothing asks the second yet"* — so function-level overlap is not available to be
weighed, and pretending otherwise would be inventing precision. The error is in
the safe direction: an over-reported collision costs a PR a position, an
under-reported one costs a silent bad merge.

**And paths under-report, in one shape worth naming.** Two PRs that each add a
*different* file under ``migrations/`` share no path and collide absolutely: this
repo keeps a single alembic head, and its own pre-push hook refuses a protected
branch that has become multi-headed. So a small, evidenced set of shared
resources (:data:`SHARED_RESOURCES`) makes both PRs contended on the strength of
the directory rather than the filename. It is deliberately one entry long. A
prefix earns a place here only when landing two members at once is known to
break, not when they merely sound related.

Why not :mod:`app.ordering`
===========================

That module orders a *plan* and says it *"lets the same function order a landing
queue (#227) later, since it never learns what a candidate is"*. It does not, and
its own rule 3 is where the two part company: red CI **rises** in a plan, because
a known red thing is identified work holding something up, and **sinks** in a
landing queue. The rest follows the same way. A plan's rules are a priority ladder
over what to pick up; this is a cost model over what to land, with a direction
(heaviest first) a priority ladder has no analogue for, and with overlap as the
primary axis rather than a tiebreak applied to the remainder. Bending one into the
other would have produced a ladder whose rungs meant different things depending on
which caller ran it, which is worse than two small pure modules.

What is left out, and why
=========================

* **The landing graph — #294.** Which PRs gate which, fanning out and in, across
  repos, with hard temporal edges. It is the axis file overlap structurally
  cannot see and it is being built next door; this module does not guess at it.
  :class:`Row`'s rank is produced from a sort key precisely so a precedence edge
  can be layered above the cost tiers later without touching the cost model.
* **CI status.** The board takes testimony, not measurements — it cannot read a
  check run — and an order weighted by a fact nobody can verify is an order
  nobody can check.
* **Preland readiness**, which the queue *does* hold first-hand, pinned to a
  commit. Measured, reported per row, and deliberately **not** ranked on. A
  verdict is invalidated by every push, so tiering on it would reshuffle the
  proposal each time the head does the one thing its slot is for, and would
  demote the agent that pushed. The queue's own model refuses that trade for
  ``active_order`` — *"a head change invalidates readiness, and does not cost the
  slot"* — and a suggestion that made the opposite trade would be advising
  against the queue it advises on.

What the order is worth
=======================

Four tiers, and they are :data:`app.collisions.CLASSES` rather than a second
vocabulary, because a PR that the collision endpoint calls ``partial`` and a
ranking that calls it "probably fine" is the same fact told two ways.

1. :data:`~app.collisions.DISJOINT` — a complete, attested file list that shares
   nothing with any other queued PR. **First**, and it is free to put them there:
   a PR that collides with nobody contributes zero to the sum above from any
   position, so its placement is unconstrained by cost — and every land it waits
   through is exposure to the base moving under it for reasons unrelated to it.
2. :data:`~app.collisions.COLLIDES` — heaviest first, per the exchange argument.
3. :data:`~app.collisions.PARTIAL` — measured, sharing nothing *found*, and its
   list is a prefix, so it cannot be called disjoint. Sorted like a collider,
   ranked below the proven ones, and its row says it may belong a tier up.
4. :data:`~app.collisions.UNANSWERABLE` — no usable list at all. **Not ranked at
   any position**, because every position would be invented.

And the gates on the confident answer, which are the point of the exercise:

* A queue containing an unanswerable PR gets **no** ``suggested_order``. Not a
  best guess with a footnote — null, with the PRs it could not see named. This is
  the #94 hole and it is not a small one: the panel's title-skip path records no
  files, so merges, promotes and format-the-world commits are invisible here —
  which under the cost model above are exactly the PRs that should land *first*.
  A ranking that silently drops them to the bottom would make its largest
  possible error on its most important rows, quietly.
* A queue whose lists are all present but some are prefixes gets an order with
  :attr:`Ranking.trusted` false and a caveat naming them. The direction of that
  error is known — more collisions than were seen, never fewer — so the order is
  still the best evidence available, and it says so out loud rather than in a
  docstring.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from app.collisions import COLLIDES, DISJOINT, PARTIAL, UNANSWERABLE, files_complete

#: Directories where two PRs collide by touching the same *directory*, whatever
#: they named the files in it — the case path intersection is blind to.
#:
#: One entry, and the bar for a second is evidence rather than resemblance:
#: landing two members at once must be known to break something. ``migrations/``
#: clears it because this repo keeps a single alembic head, so two branches each
#: adding a revision produce a multi-headed base — and the repo's own pre-push
#: hook refuses that branch, which is the breakage already written down.
#:
#: ``changelog.d/`` deliberately does **not** qualify: one file per issue, so two
#: PRs there are disjoint by construction, and CHANGELOG.md itself is a file the
#: pre-push hook already stops a branch editing.
SHARED_RESOURCES: Mapping[str, str] = {
    "migrations/": "both add an alembic revision, and this repo keeps a single "
                   "head — they collide at merge without sharing a path, and the "
                   "pre-push hook refuses the multi-headed base that results",
}

#: How many shared paths one pair carries as evidence. The *count* is never
#: trimmed and is what the sort weighs by; this bounds only the sample a person
#: reads. Smaller than :data:`app.api.reviews.SHARED_FILES_CAP` because that cap
#: is per row and this one is per **pair**, and a queue of n PRs has n(n-1)/2 of
#: them.
SHARED_SAMPLE_CAP = 20

#: The tiers, in the order they are proposed. Named from
#: :data:`app.collisions.CLASSES` rather than reinvented — see the module
#: docstring. :data:`~app.collisions.EXCLUDED` has no meaning here: a merged or
#: closed PR is not in a merge queue.
TIERS = (DISJOINT, COLLIDES, PARTIAL, UNANSWERABLE)


def shared_resource_keys(paths: Iterable[str]) -> frozenset[str]:
    """Which :data:`SHARED_RESOURCES` these paths put a PR into.

    Exported so the caller's query and this module's reasoning cannot drift: the
    caller fetches the paths under these prefixes and hands them back through
    here, rather than deciding for itself what a ``migrations/`` path looks like.
    """
    return frozenset(key for key in SHARED_RESOURCES
                     for path in paths if path.startswith(key))


@dataclass(frozen=True)
class Candidate:
    """One queued PR with everything the ranking weighs, already gathered.

    Deliberately not a queue entry: the entry is a row in a table this module
    must not import, and a dataclass boundary is what lets the whole cost model
    be tested against hand-built queues with no database at all.

    ``changed_files_total`` and ``files_recorded`` are separate for the reason
    :class:`app.collisions.Rival` keeps them separate — GitHub caps a PR's file
    list at 3,000, so the two disagreeing is the only evidence the stored list is
    a prefix.
    """

    #: The PR number.
    pr: int
    #: Its place in ``active_order``, 1-based. The tiebreak everywhere, and the
    #: answer this module falls back to whenever it has nothing better: FIFO by
    #: arrival is the only order that cannot thrash.
    position: int
    #: Does the board hold a ``ready`` verdict about the commit this entry is on?
    #: Reported, never ranked on — see the module docstring.
    ready: bool
    #: GitHub's own count of the PR's changed files, or ``None`` when nobody
    #: counted. Not zero.
    changed_files_total: int | None
    #: How many paths the board actually stored for the run answering for this PR.
    files_recorded: int
    #: The run that answered, and when — so a reader can see how old the evidence
    #: is. ``None`` when no run of this PR recorded anything.
    run_id: int | None = None
    run_ts: str | None = None
    #: Which :data:`SHARED_RESOURCES` this PR touches, via
    #: :func:`shared_resource_keys`.
    resources: frozenset[str] = frozenset()

    @property
    def measured(self) -> bool:
        """Did anything answer for this PR at all?

        The same test as :data:`app.collisions.UNANSWERABLE`, spelt once: a count
        with no rows still means somebody counted, and a genuine zero-file PR is
        knowledge rather than silence.
        """
        return not (self.changed_files_total is None and self.files_recorded == 0)

    @property
    def complete(self) -> bool:
        return files_complete(self.changed_files_total, self.files_recorded)

    @property
    def weight(self) -> int:
        """``w`` — the price of putting this branch through one more integration.

        GitHub's count where there is one; otherwise the number of paths the
        board holds, which is a **floor** and is reported as such. Zero for an
        unmeasured PR, which never reaches a comparison anyway.
        """
        if self.changed_files_total is not None:
            return self.changed_files_total
        return self.files_recorded


@dataclass(frozen=True)
class Overlap:
    """What two queued PRs share.

    The path intersection is computed in the database and handed here as a count
    plus a sample, the same trade ``GET /review/collisions`` makes: the number is
    what the sort weighs by and is never trimmed, the sample is for the person
    reading the answer. Passing whole path sets into this module instead would
    mean shipping up to 3,000 strings per PR out of Postgres to compute something
    an index answers.
    """

    a: int
    b: int
    #: Shared paths, untrimmed. May be zero when the pair collides only on a
    #: shared resource.
    shared: int = 0
    #: A sample of them, capped at :data:`SHARED_SAMPLE_CAP`.
    sample: tuple[str, ...] = ()
    #: :data:`SHARED_RESOURCES` keys both PRs touch.
    resources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.a == self.b:
            raise ValueError(f"#{self.a} cannot collide with itself")
        # A sample longer than the total it samples means the caller filled one
        # field and not the other, and the failure it produces — a colliding pair
        # weighed as sharing less than it does — is silent. Refuse it here, the
        # same way `app.collisions.Rival` refuses its version of this.
        if self.shared < len(self.sample):
            raise ValueError(
                f"shared={self.shared} is below the {len(self.sample)} sampled "
                "paths it is meant to count")

    @property
    def key(self) -> tuple[int, int]:
        return (self.a, self.b) if self.a < self.b else (self.b, self.a)

    def other(self, pr: int) -> int:
        return self.b if pr == self.a else self.a


@dataclass(frozen=True)
class Row:
    """One PR's placement and the whole of why."""

    pr: int
    #: 1-based place in the proposal, or ``None`` for an unranked PR.
    rank: int | None
    #: One of :data:`TIERS`.
    tier: str
    #: How far it moved from its FIFO position. Negative is earlier. Reported so
    #: a reader can see at a glance whether the proposal is doing anything.
    moved: int | None
    weight: int
    #: Where ``weight`` came from, because a floor and a count must not read alike.
    weight_basis: str
    #: Total shared paths with all other queued PRs — the entanglement, not the
    #: cost. Cost is the ordered sum in the module docstring.
    shared_total: int
    ready: bool
    files_complete: bool
    run_id: int | None
    run_ts: str | None
    #: One line a reader or a board post can quote.
    reason: str
    collides_with: tuple[dict, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Ranking:
    """The proposal, with the exact terms on which it may be believed."""

    #: The proposed order over the PRs that could be ranked. **Not necessarily
    #: every queued PR** — see :attr:`covers_all`.
    order: tuple[int, ...]
    #: Queued PRs given no position, in FIFO order.
    unranked: tuple[int, ...]
    rows: tuple[Row, ...]
    #: True when every queued PR has a complete, attested file list. False makes
    #: the order advisory-with-a-caveat rather than worthless: the direction of
    #: the error is known.
    trusted: bool
    #: True when every queued PR could be ranked at all, so :attr:`order` is a
    #: permutation of the queue and may be published as ``suggested_order``.
    covers_all: bool
    #: True when the proposal is the arrival order — worth saying, so a reader
    #: does not diff two lists to find out.
    differs: bool
    counts: Mapping[str, int]


def _sort_key(c: Candidate, tier: str, shared_total: int) -> tuple:
    """Lexicographic, and every component is a measurement or the FIFO fallback.

    A tuple rather than a score, so that #294's precedence edges can be prepended
    as a component later without any of the cost reasoning below being restated
    or re-weighted. A single blended number could not take a hard constraint.
    """
    return (
        TIERS.index(tier),
        # Heaviest first inside a contended tier — the exchange argument. Zero
        # for a disjoint PR, where every position costs the same and this
        # collapses to the FIFO tiebreak below.
        -c.weight if tier in (COLLIDES, PARTIAL) else 0,
        # Most entangled first among equals: it is the one whose late land
        # re-integrates against the most moved surface.
        -shared_total,
        # Arrival. The only component that cannot thrash, and the whole answer
        # whenever the ones above tie.
        c.position,
    )


def _reason(c: Candidate, tier: str, shared_total: int, partners: Sequence[int],
            answerable: int, blind: int) -> str:
    """One line a reader or a board post can quote, and every claim in it bounded.

    ``answerable`` and ``blind`` are the size of the population the verdict was
    reached over and the size of the population it was not. A ``disjoint`` row
    especially must carry them: "shares nothing with any other queued PR" is a
    safety claim, "shares nothing with the four the board can answer for, and
    there is a fifth it cannot" is a description — and the first sentence in a
    queue holding an unanswerable PR is the exact confident-on-partial-data
    failure this module exists to refuse.
    """
    named = ", ".join(f"#{p}" for p in partners[:4])
    if len(partners) > 4:
        named += f" and {len(partners) - 4} more"
    # Two different sentences, because the doubt cuts two different ways. On a
    # `disjoint` row the hidden population makes a safety claim into a
    # description; on a `collides` row it makes an already-true collision count a
    # floor, which changes nothing about the verdict and only about its size.
    unseen = (f", and {blind} more the board cannot answer for at all — so this "
              f"describes what was seen and is not a safety claim" if blind else "")
    floor = (f". {blind} further queued PR(s) recorded no list, so this count is a "
             f"floor on what #{c.pr} actually collides with" if blind else "")
    if tier == UNANSWERABLE:
        return (f"no run of #{c.pr} recorded a changed-file list, so it is not "
                f"ranked at all: every position would be invented. A panel round "
                f"on it fills this in; the panel's skip path never will until #94")
    if tier == DISJOINT:
        return (f"a list of {c.weight} file(s), attested complete, sharing no path "
                f"with any of the {answerable} other queued PR(s) the board can "
                f"answer for{unseen} — landing it disturbs nothing it can see "
                f"here, so it costs nobody anything from any position and is "
                f"placed where it waits least")
    if tier == COLLIDES:
        return (f"collides with {named}: {shared_total} shared-path collision(s) "
                f"across {len(partners)} PR(s), a path counted once per pair. At "
                f"{c.weight} changed files it is the more expensive end of those "
                f"pairs to re-integrate later, so it is ranked ahead of the "
                f"lighter ones it collides with{floor}")
    return (f"{c.files_recorded} paths stored against "
            f"{'no count' if c.changed_files_total is None else c.changed_files_total} "
            f"— the list is a prefix, so no shared path being found is not "
            f"evidence of none. Ranked as a collider of weight {c.weight}; it may "
            f"belong a tier up{floor}")


def rank(candidates: Sequence[Candidate],
         overlaps: Sequence[Overlap] = ()) -> Ranking:
    """Propose an order for one merge queue. Deterministic, and never a mutation.

    ``candidates`` is the live queue in ``active_order``; ``overlaps`` is every
    pair that shares something, in either direction — the caller may send each
    pair once and this normalises it.

    Total: every candidate appears in exactly one of :attr:`Ranking.order` and
    :attr:`Ranking.unranked`, and has exactly one :class:`Row`. A PR that fell
    between the two would be a PR the queue holds and the proposal never mentions,
    which is the shape of bug :mod:`app.collisions` was rewritten twice to make
    unreachable.
    """
    by_pr = {c.pr: c for c in candidates}
    if len(by_pr) != len(candidates):
        raise ValueError("a PR appears twice in the queue")

    # Normalised once, so a caller that sent (a, b) and a caller that sent (b, a)
    # cannot produce two edges for one pair and double that pair's weight.
    edges: dict[tuple[int, int], Overlap] = {}
    for o in overlaps:
        if o.a not in by_pr or o.b not in by_pr:
            continue
        edges[o.key] = o

    # A shared resource is a collision the path intersection cannot see, so it is
    # derived here rather than expected from the caller — one rule, one place.
    for i, a in enumerate(candidates):
        for b in candidates[i + 1:]:
            common = sorted(a.resources & b.resources)
            if not common:
                continue
            key = (a.pr, b.pr) if a.pr < b.pr else (b.pr, a.pr)
            was = edges.get(key)
            edges[key] = Overlap(
                a=key[0], b=key[1],
                shared=was.shared if was else 0,
                sample=was.sample if was else (),
                resources=tuple(common),
            )

    partners: dict[int, list[Overlap]] = {c.pr: [] for c in candidates}
    for o in edges.values():
        # An unmeasured PR contributes no evidence and must not receive any
        # either: an edge to it would make its partner `collides` on the strength
        # of a list nobody has. It is unanswerable in both directions.
        if not (by_pr[o.a].measured and by_pr[o.b].measured):
            continue
        partners[o.a].append(o)
        partners[o.b].append(o)

    # The population every verdict below is reached OVER, and the population it
    # is not. A `disjoint` claim is only as wide as the first of these, and a row
    # that did not say so would be the confident-on-partial-data failure in one
    # sentence.
    blind = sum(1 for c in candidates if not c.measured)
    rows: list[tuple[tuple, Row]] = []
    unranked: list[Row] = []
    for c in candidates:
        mine = sorted(partners[c.pr], key=lambda o: (-o.shared, o.other(c.pr)))
        shared_total = sum(o.shared for o in mine)
        if not c.measured:
            tier = UNANSWERABLE
        elif mine:
            tier = COLLIDES
        elif not c.complete:
            tier = PARTIAL
        else:
            tier = DISJOINT
        row = Row(
            pr=c.pr, rank=None, tier=tier, moved=None, weight=c.weight,
            weight_basis=("github's changed-file count"
                          if c.changed_files_total is not None
                          else f"{c.files_recorded} stored paths — nobody counted, "
                               f"so this is a floor"),
            shared_total=shared_total, ready=c.ready, files_complete=c.complete,
            run_id=c.run_id, run_ts=c.run_ts,
            reason=_reason(c, tier, shared_total, [o.other(c.pr) for o in mine],
                           answerable=len(candidates) - blind - 1, blind=blind),
            collides_with=tuple(
                {"pr": o.other(c.pr), "shared": o.shared, "files": list(o.sample),
                 "files_dropped": max(0, o.shared - len(o.sample)),
                 "shared_resources": list(o.resources)}
                for o in mine),
        )
        if tier == UNANSWERABLE:
            unranked.append(row)
        else:
            rows.append((_sort_key(c, tier, shared_total), row))

    rows.sort(key=lambda pair: pair[0])
    # `moved` is measured against arrival order OVER THE RANKED POPULATION, not
    # over the whole queue. With an unrankable PR in the line the two differ, and
    # the queue-wide reading would report every row behind it as having moved by
    # the number of PRs the ranking could not see — a displacement the proposal
    # did not make, attributed to it.
    arrival = {row.pr: i for i, row in enumerate(
        sorted((row for _key, row in rows), key=lambda r: by_pr[r.pr].position),
        start=1)}
    ordered: list[Row] = []
    for i, (_key, row) in enumerate(rows, start=1):
        ordered.append(replace(row, rank=i, moved=i - arrival[row.pr]))

    order = tuple(r.pr for r in ordered)
    counts = {tier: 0 for tier in TIERS}
    for row in (*ordered, *unranked):
        counts[row.tier] += 1
    # FIFO over the SAME population the proposal covers. Comparing a partial
    # proposal against the whole queue would report `differs: true` for every
    # queue holding an unrankable PR, whatever the proposal actually did.
    ranked = set(order)
    fifo = tuple(c.pr for c in sorted(candidates, key=lambda c: c.position)
                 if c.pr in ranked)
    return Ranking(
        order=order,
        unranked=tuple(r.pr for r in unranked),
        rows=(*ordered, *unranked),
        # A prefix list means the overlap found is a floor, so `disjoint` is not
        # provable for anybody: one incomplete list is enough to make the whole
        # proposal advisory rather than attested.
        trusted=not unranked and all(c.complete for c in candidates),
        covers_all=not unranked,
        differs=bool(order) and order != fifo,
        counts=counts,
    )
