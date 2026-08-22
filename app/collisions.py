"""Classifying one rival PR against a subject's file list (#101).

Pure and I/O-free, like :mod:`app.overlap` and :mod:`app.sync` — and here that is
not tidiness, it is the fix. This endpoint was written twice and a four-seat
panel found the *same* defect both times, the second instance introduced by the
first's fix:

===== ===================================================== ==========================
round  the bug                                               the shape
===== ===================================================== ==========================
r1     a rival was answered for by its newest **file-        filter on *has files*,
       bearing** run                                         **then** pick newest
r2     a rival was answered for by its newest **OPEN-        filter on *state*,
       state** run                                           **then** pick newest
===== ===================================================== ==========================

The premise behind both: *that the rival population can be narrowed by filters
composed at query level, with the newest-run selection as just another filter in
that composition.* It cannot. Any predicate placed before the selection
resurrects a stale run — the PR was panelled again since, the newer round did not
match the predicate, and the older round's answer is handed back in a confident
voice with nothing marking it stale.

So the query does one thing and this module does the other. **Select first,
classify second.** The caller hands over a rival that is already, unconditionally,
that PR's newest run; every question about whether it counts is asked here, in
one ordered ladder, over data that is already selected. A predicate written into
this module cannot change *which* run answers for a PR, because by the time it
runs the run is chosen. That is what makes this class of bug impossible rather
than fixed twice.

The second half is exhaustiveness. :func:`classify` returns exactly one of
:data:`CLASSES` for every rival it is given, so a rival cannot fall between the
buckets and vanish. It did: r1's "has a file list" test was
``changed_files_total IS NOT NULL``, so a rival claiming 2,500 files with none
stored passed it, contributed no join rows, and was silently absent from *both*
the collision list and the unanswered list — read by a caller as "answered, and
disjoint" (``88-F07``). Here it is :data:`PARTIAL` by construction, because
"answerable" and "actually contributed paths" are not tested separately.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Shares at least one path with the subject. A *floor* on the overlap where the
#: rival's own list is a prefix — never a ceiling, which is why the row carries
#: its ``files_complete`` beside the shared paths.
COLLIDES = "collides"
#: Answerable, no shared path found, and **not** shown to be complete — so the
#: paths it never recorded may be the subject's. Neither colliding nor disjoint.
PARTIAL = "partial"
#: Its newest run recorded no file list at all. Every pre-v2.23 run lands here.
#: Not disjoint — unanswered.
UNANSWERABLE = "unanswerable"
#: Its newest run saw the PR merged or closed (or drafted, when the caller asked
#: for drafts to be set aside). Not something landing the subject can disturb.
EXCLUDED = "excluded"
#: Answerable, complete, and shares nothing. The only bucket that is a safety
#: claim, and the only one with a completeness requirement in front of it.
DISJOINT = "disjoint"

#: Every class, in the order :func:`classify` tries them. Exported so a caller can
#: assert its own coverage — the response's per-class counts sum to the population
#: exactly because this tuple is the whole of it.
CLASSES = (EXCLUDED, UNANSWERABLE, COLLIDES, PARTIAL, DISJOINT)

#: PR states that mean the PR is no longer landing. Compared against
#: :attr:`app.models.review.ReviewRun.pr_state`, which ingest upper-cases.
#:
#: ``None`` is deliberately not in here and must never be: every pre-v2.23 run
#: recorded no state at all, so treating "nobody said" as closed would silently
#: narrow the population to PRs panelled since v2.23 — a filter quietly becoming
#: a cutoff, on the side that loses rivals rather than gaining them.
CLOSED_STATES = frozenset({"MERGED", "CLOSED"})


@dataclass(frozen=True)
class Rival:
    """One rival PR's newest run, **already selected**.

    Selection is not this module's business and that is the whole design: the
    caller has already run the one unconditional ``DISTINCT ON (pr)`` that makes
    this the newest run outright, so nothing here can promote an older one.

    ``files_recorded`` is the count of :class:`~app.models.review.ReviewRunFile`
    rows for this run, and ``changed_files_total`` is GitHub's own count as the
    panel read it. They are separate for the reason v2.23 made them separate:
    GitHub caps a PR's file list at 3,000, so the two disagreeing is the only
    evidence the stored list is a prefix.
    """

    #: The rival PR number.
    pr: int
    #: The run answering for it — its newest, whatever that run recorded.
    run_id: int
    #: ``OPEN`` / ``MERGED`` / ``CLOSED`` as of that run, or ``None`` for a run
    #: recorded before the board stored it.
    pr_state: str | None
    #: Draft as of that run. ``None`` = not recorded; a draft's ``pr_state`` is
    #: ``OPEN``, so this cannot be inferred from the state.
    is_draft: bool | None
    #: GitHub's count of the PR's changed files. ``None`` = nobody counted, which
    #: is not zero.
    changed_files_total: int | None
    #: How many paths the board actually stored for this run.
    files_recorded: int
    #: The subject's paths this run also touched, sorted — and a **sample**: the
    #: caller may trim it, because a rival can share thousands of paths and this
    #: endpoint is polled over every PR the board has panelled. Never read its
    #: length as the overlap; :attr:`shared_total` is the overlap.
    shared: tuple[str, ...] = ()
    #: How many paths this run shares with the subject, untrimmed. The number the
    #: ladder reasons about and the number a ranking function weighs by, kept
    #: apart from the sample above so that a cap on the sample can never quietly
    #: become a smaller answer.
    shared_total: int = 0

    def __post_init__(self) -> None:
        # A sample longer than the total it is a sample of means the caller filled
        # one field and not the other — and the failure that would produce is a
        # colliding rival reported as sharing nothing, which is the exact shape of
        # wrong answer this module exists to make unreachable. Refuse it loudly
        # here rather than let it be classified.
        if self.shared_total < len(self.shared):
            raise ValueError(
                f"shared_total={self.shared_total} is below the {len(self.shared)} "
                "shared paths it is meant to count"
            )


@dataclass(frozen=True)
class Verdict:
    """One rival's bucket, plus why it was set aside if it was."""

    #: One of :data:`CLASSES`.
    cls: str
    #: For :data:`EXCLUDED` only: ``MERGED`` / ``CLOSED`` / ``DRAFT``. ``None``
    #: everywhere else. Stated rather than left to be re-derived from
    #: ``pr_state``, because "excluded because drafted" and "excluded because
    #: merged" are different facts and a draft's state reads ``OPEN``.
    because: str | None = None


def files_complete(changed_files_total: int | None, files_recorded: int) -> bool:
    """Is this run's stored path list **attested complete**?

    Not "does it have files" and not "is it non-empty": complete means somebody
    counted and the board holds that many. ``changed_files_total = 0`` with no
    rows is complete — a PR that genuinely changed zero files is *knowledge*, and
    it is disjoint from everything.

    A list stored with no count at all is **not** complete, and that is a
    deliberate reading of a genuinely ambiguous case. Nothing says it is a
    prefix; nothing says it is not, either, and :data:`DISJOINT` is the one
    verdict this endpoint gives that a caller may act on as a safety claim.
    Granting it from a list nobody ever attested to is precisely the failure this
    whole issue is about — an answer reading safer than the evidence supports. So
    the uncounted list lands in :data:`PARTIAL`, with ``changed_files_total:
    null`` beside ``files_recorded`` in the response saying exactly why.

    ``>=`` rather than ``==`` so that a run holding more rows than GitHub's count
    — which should not happen, and is a sender bug rather than a prefix if it
    does — is not reported as truncated.
    """
    return changed_files_total is not None and files_recorded >= changed_files_total


def classify(
    rival: Rival, *, include_closed: bool = False, exclude_drafts: bool = False
) -> Verdict:
    """Which single bucket this rival belongs in.

    The ladder, in order, and the order is load-bearing at exactly one rung:

    1. :data:`EXCLUDED` — the caller said this PR is not in play. Asked first
       because it is a question about the *caller's* interest and not about the
       evidence; a merged rival whose run recorded nothing is excluded, not
       unanswerable, because nobody wants it answered for.
    2. :data:`UNANSWERABLE` — the run recorded no list at all. It cannot share a
       path and it cannot be shown not to; both of the remaining verdicts would
       be inventions.
    3. :data:`COLLIDES` — a shared path was found. **This outranks**
       :data:`PARTIAL` **on purpose.** A rival whose list is a prefix *and* which
       shares a known path is a definite collision; filing it under "might share
       something" would hide a fact behind a doubt, and a caller reading only
       ``collides`` — which is what a ranking function does — would miss it. The
       row still carries ``files_complete: false``, so the shared list reads as a
       floor rather than the whole overlap.
    4. :data:`PARTIAL` — answerable, nothing shared, completeness not
       established. It may overlap on files it never reported, so it can never be
       called disjoint.
    5. :data:`DISJOINT` — everything left: counted, complete, sharing nothing.

    ``include_closed`` reclassifies rather than un-filters: with it on, a merged
    rival is put through the same rungs as any other and can come back
    :data:`COLLIDES`. It never changes *which run* answers for the PR — that
    happened before this function was called, which is the point of the split.
    """
    state = (rival.pr_state or "").upper()
    if not include_closed and state in CLOSED_STATES:
        return Verdict(EXCLUDED, because=state)
    if exclude_drafts and rival.is_draft:
        return Verdict(EXCLUDED, because="DRAFT")
    if rival.changed_files_total is None and rival.files_recorded == 0:
        return Verdict(UNANSWERABLE)
    if rival.shared_total:
        # The COUNT, never the sample: the sample is trimmed for the wire and a
        # rival sharing 3,000 paths must not depend on the trim for its verdict.
        return Verdict(COLLIDES)
    if not files_complete(rival.changed_files_total, rival.files_recorded):
        return Verdict(PARTIAL)
    return Verdict(DISJOINT)
