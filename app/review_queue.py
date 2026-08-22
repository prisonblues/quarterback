"""The review queue — every open PR that review is not finished with — #273.

#52 named the problem in one sentence: *review coverage is currently a function
of human attention*. #54 proposed watching for ``opened`` and ``synchronize`` and
enqueuing a panel, which is **arrival**, and arrival is not drain. On 2026-08-20
six of eight open PRs had never been panelled at all, and every one of them was
opened before any watcher existed. A watcher that starts tomorrow starts empty
and those six stay invisible for ever.

So the queue here is **derived from state and never accumulated from events**.
There is no queue table, nothing enqueues, and a board that has been down for a
week returns the same answer when it comes back. The queue is a join, computed on
demand, over facts that already exist:

* the newest :class:`~app.models.review.ReviewRun` for the PR — its ``head_sha``,
  ``stopped``, ``stop_reason`` and confirmed count;
* what has since been recorded about those findings
  (:class:`~app.models.review.ReviewFindingOutcome`);
* whether a live :class:`~app.models.merge_queue.MergeQueueEntry` says preland
  has already passed it to #227's landing queue;
* whether anybody holds the PR's ``work`` claim;
* whether an open :class:`~app.models.plan_item.PlanItem` exempts it;
* and the PR's own head, mergeability and opening time, which the caller supplies
  because the board holds no GitHub credential.

## Three distinctions this keeps

**"Panelled once" is not a terminal state.** A PR leaves the queue when it is
merged, closed, or exempted by the plan — not when a round has run against it.
PR #188 carried 37 judge-confirmed findings for two and a half days with a round
recorded at its current head, and was no closer to landing than a PR nobody had
looked at.

**The next action differs per PR.** One queue, four verbs: a CONFLICTING branch
needs integrating before a round is worth paying for (#271); a PR whose head has
moved past its newest round needs re-reviewing (#278); a PR with outstanding
confirmed findings needs a fix pass; a PR with no round needs a first one.

**Being idle and being broken must not look alike** (#244). Every entry carries
its ``holds`` — every reason it cannot be acted on right now, not just the first
— and the queue as a whole carries an ``idle_reason`` whenever nothing is
drainable. A reader can always tell "everything is blocked" from "nothing was
computed".

## What is deliberately absent

**Order.** Entries come back in PR order, which is a stable spelling and not a
work order; ``ordering`` says so in the response. The plan owns the order (#232)
and the landing queue owns the landing order (#227). A drainer that also ordered
would be the hub-and-spoke shape ``qb-seats`` was written to refuse.

When something *does* want this queue ranked, the ranking already exists and must
not be written again here: :func:`app.ordering.order` is a pure five-rule
function over :class:`app.ordering.Candidate` values, and it took candidates
rather than plan rows precisely so a queue could reuse it with its own table.
Every field a ``Candidate`` reads is on an entry already — ``blocked``, the
newest run's ``pr_state``, ``draft``, ``ci_status``, its outstanding findings, and
the plan item's rank — so the mapping is a projection and not a second
derivation. What is missing is the *table*: #232's rule 3 rises a red PR in a
plan and sinks it in a landing queue, and which way it goes here is a decision
this issue does not make.

**A round cap.** ``max_rounds`` is a caller-supplied number and is not read off
the board's dials, for the reason :mod:`app.api.dials` gives at length: the board
stores dials as opaque JSON and does not know what any of them mean. A second
place that knew ``review_panel.max_rounds`` was an integer is the drift #305
exists to end.

**An actor.** Nothing here runs a panel, and nothing here writes. Sequencing step
1 of the issue is the derived read on its own, because it is already the answer to
"what is the state of play"; the drainer is step 3, and it is a separate change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

#: Queue states, in the order they are TESTED. Precedence is the whole of the
#: correctness here: #190 was both CONFLICTING and reviewed at a head it had moved
#: past, and it must come back ``blocked`` rather than ``stale``, because a round
#: spent on a branch that will not merge is a round bought twice (#271).
#:
#: ``exempt`` outranks everything because the plan is the only exemption
#: authority, and an exemption a state test could shadow is not an exemption.
#: ``escalated`` comes next: a human is owed an answer, and no automatic step may
#: be taken past one.
STATES: tuple[str, ...] = (
    "exempt",
    "escalated",
    "blocked",
    "stale",
    "unresolved",
    "unconverged",
    "ready",
    "unreviewed",
)

#: The verb each state implies — the thing this issue says nothing computes today.
#: Read it as *what would happen next if this entry were drainable*, never as
#: permission: ``drainable`` is the permission, and it is a separate field.
ACTIONS: tuple[str, ...] = (
    "integrate",   # rebase / merge the base in; do not spend a round yet (#271)
    "review",      # a first round, or another one
    "re-review",   # the head moved past the newest round (#278)
    "fix",         # confirmed findings are outstanding at the current head
    "land",        # nothing left for review; #227's landing queue owns it now
    "answer",      # a human is owed a reply to an escalation
    "none",        # exempt: leave it alone, and say which plan item said so
)

#: Every reason an entry may not be acted on. A LIST per entry, not a first
#: match — "it is a draft" and "somebody holds the claim" are two facts, and a
#: reader that saw only the first would act the moment the draft flag cleared.
HOLDS: tuple[str, ...] = (
    "exempt",
    "escalated",
    "conflicting",
    "mergeable-unknown",
    "draft",
    "claimed",
    "round-cap",
    "no-head",
)

#: What ``since`` is measured from. ``age`` is the number this issue exists to
#: make visible, and until now it had to be reconstructed by hand from
#: timestamps — so the basis is published beside it rather than assumed.
AGE_BASES: tuple[str, ...] = (
    "pr_opened",
    "last_run",
    "queue_entered",
    "plan_item_updated",
    "needs_human_first_flagged",
)

#: An outcome recorded against a finding clears it from THIS PR's ledger. All
#: four count: ``fixed`` and ``refuted`` are self-evident, ``superseded`` names
#: the finding that replaced it, and ``deferred`` names the issue it moved to —
#: which is precisely a decision that it does not block this PR.
CLEARING_OUTCOMES: frozenset[str] = frozenset(
    {"fixed", "refuted", "deferred", "superseded"}
)

#: The token that turns a plan item's note into an EXEMPTION rather than a
#: remark about the PR.
#:
#: **Silence is not exemption** — that is the rule the issue states, and the
#: converse has to be just as sharp or the rule inverts by accident: an item
#: whose note happens to mention review would exempt its PR, and the queue's
#: depth would quietly depend on prose. So the marker is a token somebody had to
#: type on purpose, and the whole note is echoed back beside it so a reader
#: judges the reason rather than trusting the match.
EXEMPT_MARKER = re.compile(r"(?<![\w-])review\s*:\s*exempt(?![\w-])", re.IGNORECASE)

#: GitHub's ``mergeable`` vocabulary, as ``gh pr view --json mergeable`` spells it.
MERGEABLE = "MERGEABLE"
CONFLICTING = "CONFLICTING"
UNKNOWN = "UNKNOWN"

#: The shortest prefix two commit ids may be compared on. Below this a match is
#: not evidence of anything — and a *wrong* answer here re-panels a PR that did
#: not move, or skips one that did.
MIN_SHA_PREFIX = 7


def exempting(note: str | None) -> bool:
    """Does this plan-item note exempt its PR from review?"""
    return bool(note and EXEMPT_MARKER.search(note))


def same_commit(a: str | None, b: str | None) -> bool | None:
    """``True``/``False`` if the two commit ids can be compared, else ``None``.

    ``None`` is the answer that matters and the reason this is not a ``==``. A
    run recorded before v2.26 has no ``head_sha`` at all, and a caller may send an
    abbreviated oid; treating either as "different" re-panels a PR that never
    moved, and treating either as "same" leaves a moved head reviewed at a commit
    nobody has looked at. Neither is a guess this function is entitled to make.
    """
    if not a or not b:
        return None
    x, y = a.strip().lower(), b.strip().lower()
    n = min(len(x), len(y))
    if n < MIN_SHA_PREFIX:
        return None
    return x[:n] == y[:n]


@dataclass(frozen=True, slots=True)
class PullRequest:
    """One open PR, as the caller sees it on GitHub.

    Testimony, not measurement — the same bargain
    :class:`~app.models.merge_queue.MergeQueueEntry` strikes, and for the same
    reason: the board has no GitHub credential and the server image carries no
    ``gh``. What the board adds is that the testimony is joined to everything the
    board *does* know, in one place, so no caller has to re-derive the join.
    """

    number: int
    head: str | None = None
    mergeable: str | None = None
    opened: datetime | None = None
    title: str | None = None
    draft: bool = False
    #: An escalation the caller knows about that the board does not — a premise
    #: put to the seats, an ``epic.py`` triage. **Additive only**: the board's own
    #: :class:`NeedsHuman` record settles the question for anything recorded
    #: through a panel (#279), and this exists so a judgement formed somewhere
    #: that has not reached the board yet is not silently drainable.
    escalated: bool = False


@dataclass(frozen=True, slots=True)
class LastRun:
    """The newest panel round recorded for a PR, and what it left behind."""

    run_id: int
    ts: datetime
    round: int
    head_sha: str | None
    stopped: bool | None
    stop_reason: str | None
    #: Whether that stop was EARNED. ``False`` when a reviewer was truncated,
    #: absent, unparsed or declared a gap — the cases where a counter reading
    #: zero says nothing about the code. A clean round that admits it is not
    #: evidence must not be handed to the landing queue as a converged one.
    stop_confident: bool | None
    stop_veto: list | None
    #: The PR state and CI verdict **as of that run**, carried through untouched
    #: and deliberately NOT inputs to any state below. They are here because
    #: :class:`app.ordering.Candidate` reads both and an orderer should not have
    #: to make a second query for them.
    #:
    #: Neither is live, and ``ci_status`` is worse than merely old: #324 records
    #: that a red run followed by an approval-gated one reports as no checks at
    #: all, and no checks reads as no news. Nothing in this module decides a verb
    #: from it, so that defect is inherited by anything that starts to — say so
    #: before you do.
    pr_state: str | None
    ci_status: str | None
    confirmed: int
    #: ``confirmed`` less every finding with a recorded outcome. The two are both
    #: published: the issue's own acceptance criterion is about ``confirmed``, and
    #: ``outstanding`` is what decides whether a fix pass is owed.
    outstanding: int
    cleared: int


@dataclass(frozen=True, slots=True)
class NeedsHuman:
    """Defects on this PR that a person is owed an answer about — #279.

    Before that issue landed, "a human has to look at this" was a judgement formed
    in four places and recorded in none, so a queue could only ask the caller and
    believe the answer. It is board state now, which is why this queue derives its
    ``escalated`` state instead of taking testimony for it: an agent whose own
    round raised the flag cannot then decide the flag is not there.

    ``waiting`` counts DEFECTS, keyed as ``finding_key`` is keyed, not
    observations — a defect flagged in rounds 2, 3 and 4 is one thing a person
    owes an answer about. The classes and the reasons are deliberately not
    duplicated here: ``GET /review/needs-human?repo=&pr=`` is their one authority
    and the entry points at it rather than paraphrasing it.
    """

    waiting: int
    #: The oldest flagged observation among them — the age of the QUESTION, not of
    #: the latest round to restate it, which is the distinction #279 draws.
    since: datetime


@dataclass(frozen=True, slots=True)
class Exemption:
    """The open plan item that takes a PR out of the line, and its reasoning."""

    item_id: str
    title: str
    note: str | None
    added_by: str
    updated: datetime
    rank: int


@dataclass(frozen=True, slots=True)
class Held:
    """A live ``work`` claim on the PR — somebody is already on it."""

    holder: str
    session: str | None
    note: str | None
    expires: datetime


@dataclass(frozen=True, slots=True)
class Landing:
    """A live entry in #227's landing queue for this PR."""

    verdict: str
    ready: bool
    position: int
    entered: datetime
    #: The commit the entry says the PR is on. An entry is written once and
    #: updated on re-enqueue, so it can be behind what GitHub reports.
    head_sha: str | None = None
    #: Whether that commit is the PR's current head. ``None`` when it cannot be
    #: compared. An entry behind the head describes a commit the PR has left,
    #: and reporting it as "already in the landing queue" is the readiness that
    #: never expires which ``ready_sha`` exists to prevent.
    at_head: bool | None = None


@dataclass(slots=True)
class Verdict:
    """One queue entry's derived answer."""

    state: str
    action: str
    reason: str
    since: datetime
    since_basis: str
    #: What the review record alone says, before mergeability, escalation and the
    #: plan get a vote. Published beside ``state`` rather than discarded by it,
    #: because the two answer different questions and the issue asks both: #190
    #: must come back ``blocked`` so no round is spent on it, and the six PRs with
    #: no run must still be legible as never panelled. A ``blocked`` entry whose
    #: ``review_state`` is ``unreviewed`` says exactly that, in one row.
    review_state: str = "unreviewed"
    review_action: str = "review"
    #: True when ``since`` is the earliest moment the state COULD have begun
    #: rather than the moment it did. A PR does not become CONFLICTING at a time
    #: anybody records, so its block is measured from the PR's opening and the age
    #: that comes back is an upper bound. Saying which is which is the difference
    #: between a number and a number somebody can rely on.
    age_is_upper_bound: bool = False
    holds: list[dict] = field(default_factory=list)


def _hold(code: str, detail: str) -> dict:
    return {"code": code, "detail": detail}


def _norm_mergeable(value: str | None) -> str:
    return (value or "").strip().upper() or UNKNOWN


def _review_state(pr: PullRequest, run: LastRun | None) -> tuple[str, str, str]:
    """``(state, action, reason)`` from the review record alone.

    Split out from :func:`classify` because it is the part with no GitHub in it,
    and because the precedence inside it is subtle enough to want reading on its
    own:

    * outstanding findings mean a fix pass, whether or not the cycle stopped. A
      round that hit the cap with 43 findings outstanding is not converged, and
      the panel's own ``stop_reason`` says as much in words the board does not
      have to parse.
    * ``stopped is None`` is "the panel did not say", which is neither ``True``
      nor ``False``. A clean round that never declared convergence buys another
      round rather than a landing.
    """
    if run is None:
        return ("unreviewed", "review", "no panel round has ever run against this PR")

    moved = same_commit(run.head_sha, pr.head)
    if moved is False:
        return (
            "stale",
            "re-review",
            f"round {run.round} reviewed {(run.head_sha or '')[:12]}; the PR is now on "
            f"{(pr.head or '')[:12]}",
        )
    if moved is None:
        return (
            "stale",
            "re-review",
            "the newest round did not record which commit it reviewed, so it cannot "
            "be shown to be current",
        )

    if run.outstanding > 0:
        return (
            "unresolved",
            "fix",
            f"{run.outstanding} confirmed finding(s) outstanding at the current head"
            + (f" ({run.confirmed} confirmed, {run.cleared} cleared)"
               if run.cleared else ""),
        )
    if run.stopped is True and run.stop_confident is not True:
        # `stop_confident` is the column that exists so a clean verdict can be
        # told from an EARNED one — a reviewer truncated out of half the diff
        # raises nothing and a counter reading zero says nothing about the code.
        # Reading it as convergence hands the landing queue a round that already
        # said it was not evidence.
        #
        # `is not True` rather than `is False`, for the reason `stopped is None`
        # gets its own branch below: NULL is "nobody said", which is not a claim
        # of confidence. ``StopIn.confident`` defaults to False precisely so a
        # payload that never said cannot buy a landing, and a row written around
        # that API must not be read more generously than one written through it.
        why = ("; ".join(str(v) for v in (run.stop_veto or []))
               or ("the round did not record whether the stop was earned"
                   if run.stop_confident is None else "the panel did not say why"))
        return (
            "unconverged",
            "review",
            f"round {run.round} stopped clean but not confidently: {why}",
        )
    if run.stopped is True:
        return (
            "ready",
            "land",
            f"round {run.round} converged at this head with nothing outstanding",
        )
    if run.stopped is False:
        return (
            "unconverged",
            "review",
            f"round {run.round} did not converge: {run.stop_reason or 'no reason given'}",
        )
    return (
        "unconverged",
        "review",
        f"round {run.round} left nothing outstanding but never said whether it converged",
    )


def classify(
    pr: PullRequest,
    *,
    run: LastRun | None = None,
    #: Rounds in the newest run's CYCLE, not in the PR's whole history.
    rounds: int = 0,
    exemption: Exemption | None = None,
    needs_human: NeedsHuman | None = None,
    held: Held | None = None,
    landing: Landing | None = None,
    max_rounds: int | None = None,
    now: datetime | None = None,
) -> Verdict:
    """What this PR is waiting for, what it needs next, and since when.

    Pure: every input is a value, nothing is fetched, and the same arguments give
    the same answer on a board that has been unreachable all week. That is what
    "derived from state" means in practice, and it is why this function has a
    unit test suite that never touches a database.
    """
    now = now or datetime.now(UTC)
    opened = pr.opened or now
    holds: list[dict] = []
    rs, ra, reason = _review_state(pr, run)
    base = {"review_state": rs, "review_action": ra}

    if exemption is not None:
        return Verdict(
            state="exempt",
            action="none",
            reason=(f"plan item {exemption.item_id} exempts this PR: "
                    f"{exemption.note or exemption.title}"),
            since=exemption.updated,
            since_basis="plan_item_updated",
            holds=[_hold("exempt", f"open plan item {exemption.item_id} "
                                   f"({exemption.title}) — the plan is the only "
                                   f"exemption authority (#232)")],
            **base,
        )

    if needs_human is not None:
        return Verdict(
            state="escalated",
            action="answer",
            reason=(f"{needs_human.waiting} defect(s) waiting on a human — see "
                    f"`GET /review/needs-human?repo=&pr={pr.number}` for which "
                    f"judgement each one wants (#279)"),
            since=needs_human.since,
            since_basis="needs_human_first_flagged",
            holds=[_hold("escalated", f"{needs_human.waiting} defect(s) no fix round "
                                      f"can settle: not drainable until a person "
                                      f"answers or an outcome is recorded")],
            **base,
        )
    if pr.escalated:
        return Verdict(
            state="escalated",
            action="answer",
            reason="the caller reports an escalation the board has no record of",
            since=run.ts if run else opened,
            since_basis="last_run" if run else "pr_opened",
            holds=[_hold("escalated", "not drainable: no automatic step may be taken "
                                      "past an escalation")],
            **base,
        )

    mergeable = _norm_mergeable(pr.mergeable)
    state, action = rs, ra

    if mergeable == CONFLICTING:
        state, action = "blocked", "integrate"
        reason = (f"mergeable=CONFLICTING — integrate before spending a round (#271); "
                  f"once it merges clean it is {rs}")
        holds.append(_hold("conflicting", "a round on a branch that will not merge is "
                                          "a round bought twice (#271)"))
    elif mergeable != MERGEABLE:
        holds.append(_hold("mergeable-unknown", "GitHub has not computed mergeability "
                                                "for this PR yet; ask again before "
                                                "spending a round"))

    if pr.head is None:
        holds.append(_hold("no-head", "the caller sent no head oid, so a round could "
                                      "not be recorded against a commit"))
    if pr.draft:
        holds.append(_hold("draft", "a draft PR is open and not asking for review yet"))
    if held is not None:
        holds.append(_hold("claimed", f"{held.holder} holds the work claim"
                                      + (f": {held.note}" if held.note else "")))
    # Only against `review`, which is another round of the SAME cycle. A
    # re-review is a new cycle at round 1 — `ReviewRun.cycle` exists because two
    # agents looping one PR interleave — so a cap counted across cycles would
    # refuse the first round after every head change.
    if max_rounds is not None and action == "review" and rounds >= max_rounds:
        holds.append(_hold("round-cap", f"{rounds} round(s) in this cycle and the "
                                        f"caller's cap is {max_rounds}"))

    if landing is not None and landing.at_head is not False and state == "ready":
        # An entry that is not `ready` is still #227's — it is in the line, and
        # what it waits on (its turn, its base) is that queue's business and not
        # this one's. The reason says which, because "handed over" and "waiting
        # its turn" are not the same sentence to read at a glance.
        place = ("at the head of the landing queue" if landing.ready
                 else f"waiting its turn in the landing queue (verdict {landing.verdict})")
        return Verdict(
            state="ready",
            action="land",
            reason=(f"{place}, position {landing.position} — review is finished "
                    f"with it and #227 owns it now"),
            since=landing.entered,
            since_basis="queue_entered",
            holds=holds,
            **base,
        )

    if state in ("blocked", "unreviewed"):
        return Verdict(state, action, reason, since=opened, since_basis="pr_opened",
                       age_is_upper_bound=(state == "blocked"), holds=holds, **base)
    if run is None:  # unreachable: every remaining state is derived from a round
        return Verdict(state, action, reason, since=opened, since_basis="pr_opened",
                       age_is_upper_bound=True, holds=holds, **base)
    return Verdict(state, action, reason, since=run.ts, since_basis="last_run",
                   age_is_upper_bound=(state == "stale"), holds=holds, **base)


#: States nothing may act on, whatever else is true of the entry.
UNDRAINABLE: frozenset[str] = frozenset({"exempt", "escalated", "ready"})


def drainable(verdict: Verdict) -> bool:
    """Is there a review step to take on this entry right now?

    ``ready`` is not drainable *by this queue* and that is not a nuance: the PR
    has left it. Landing is preland's verdict plus the ``kind=merge`` claim (#258,
    #99), and this queue does not merge.
    """
    return not verdict.holds and verdict.state not in UNDRAINABLE


def age_seconds(verdict: Verdict, now: datetime) -> int:
    since = verdict.since if verdict.since.tzinfo else verdict.since.replace(tzinfo=UTC)
    return max(0, int((now - since).total_seconds()))


def idle_reason(entries: list[dict]) -> str | None:
    """Why nothing is drainable, or ``None`` when something is.

    #244's shape, at the queue level: an idle tick because everything is blocked
    and an idle tick because the reader fell over must not read alike. This
    sentence is what a drainer reports instead of silence.
    """
    if not entries:
        return "no open pull requests were supplied for this repo"
    if any(e["drainable"] for e in entries):
        return None
    counts: dict[str, int] = {}
    for e in entries:
        for h in e["holds"]:
            counts[h["code"]] = counts.get(h["code"], 0) + 1
        if not e["holds"]:
            counts[e["state"]] = counts.get(e["state"], 0) + 1
    parts = ", ".join(f"{n} {code}" for code, n in sorted(counts.items()))
    return f"all {len(entries)} open PR(s) are held: {parts}"
