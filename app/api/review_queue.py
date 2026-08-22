"""``POST /review-queue`` — what review is waiting on, and how long it has waited (#273).

The queue in front of #227's landing queue: the PRs that are **not** ready and are
not being made ready. :mod:`app.review_queue` holds the derivation and the
reasoning behind each state; this module is the join that feeds it and the view
that comes back.

## Why this is a POST and not a `GET ?repo=`

The queue is *"every open PR not in a terminal state"*, and **the board cannot
enumerate open PRs.** It holds no GitHub credential, the server image carries no
``gh``, and ``httpx`` is not a runtime dependency — all three on purpose. The set
of open PRs therefore arrives from the caller, exactly as
:class:`~app.models.merge_queue.MergeQueueEntry` takes a head oid from
``gh pr view``: the board takes testimony and joins it to what it knows.

The tempting alternative is a ``GET`` over the PRs the board has already heard
about, and it is worse than having no reader at all. ``ReviewRun.pr_state`` is
recorded *as of the last panel* — its own docstring says so — so on this repo it
still reports ``OPEN`` for #182, #161, #158 and #154, all merged days ago. A
``GET`` would answer with a queue mostly composed of PRs that no longer exist,
which is how an advisory endpoint stops being read. A snapshot the caller took a
second ago cannot be stale in that way.

This is still *derived from state and not accumulated from events* — the point
#273 makes against #54. The snapshot **is** state: no arrival is observed,
nothing is enqueued, and a board that has been down for a week returns the same
answer the moment it comes back. A backlog opened before this endpoint existed
comes back in full on the first call.

## It writes nothing

No table is touched, no lapsed row is swept, nothing is claimed. That is
deliberate beyond tidiness: this reader is the thing a drainer would consult, and
a reader that could exempt, reorder or claim its own awkward entries has no
queue depth worth reading. The exemption authority lives where the drainer cannot
reach it — an open :class:`~app.models.plan_item.PlanItem` — and this endpoint
only reports what it finds there.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.merge_queue import is_ready
from app.auth import reader
from app.claimkey import WORK, BadRef, canonical_repo, derive
from app.db import get_session
from app.models.merge_queue import MergeQueueEntry
from app.models.plan_item import PlanItem
from app.models.resource_lease import ResourceLease
from app.models.review import ReviewFinding, ReviewFindingOutcome, ReviewRun
from app.review_queue import (
    ACTIONS,
    AGE_BASES,
    CLEARING_OUTCOMES,
    HOLDS,
    STATES,
    Exemption,
    Held,
    Landing,
    LastRun,
    NeedsHuman,
    PullRequest,
    Verdict,
    age_seconds,
    classify,
    drainable,
    exempting,
    idle_reason,
    same_commit,
)

router = APIRouter(tags=["review-queue"])

#: The most PRs one call may describe. GitHub itself will not hand a sane repo
#: more open PRs than this, and an unbounded list is an unbounded join.
MAX_PRS = 500


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(ts: datetime | None) -> datetime | None:
    """Postgres hands back aware datetimes; SQLite and hand-built rows may not."""
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


class PullRequestIn(BaseModel):
    """One open PR, in the words ``gh pr list --json`` already uses.

    The aliases are the point: the caller pipes GitHub's own field names through
    unchanged, so there is no bespoke serialiser between ``gh`` and the board to
    drift. ``ReviewIn`` takes the panel's words for the same reason.
    """

    model_config = ConfigDict(populate_by_name=True)

    number: int = Field(ge=1, validation_alias=AliasChoices("number", "pr"))
    head: str | None = Field(
        default=None,
        validation_alias=AliasChoices("head", "headRefOid", "head_sha"),
        description="the PR's current head oid",
    )
    mergeable: str | None = Field(
        default=None, description="GitHub's own word: MERGEABLE | CONFLICTING | UNKNOWN")
    opened: datetime | None = Field(
        default=None, validation_alias=AliasChoices("opened", "createdAt", "created_at"))
    title: str | None = Field(default=None, validation_alias=AliasChoices("title", "pr_title"))
    draft: bool = Field(default=False,
                        validation_alias=AliasChoices("draft", "isDraft", "is_draft"))
    #: An escalation the caller knows about and the board does not. **Additive**:
    #: since #279 the board records ``needs_human`` per defect, so it settles the
    #: question for anything a panel raised and this is only for a judgement
    #: formed somewhere that has not reached it — an ``epic.py`` triage, a premise
    #: put to the seats. It can add an escalation and can never remove one.
    escalated: bool = False


class ReviewQueueIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    repo: str = Field(min_length=1, validation_alias=AliasChoices("repo", "github"),
                      description="github nameWithOwner")
    prs: list[PullRequestIn] = Field(
        default_factory=list, max_length=MAX_PRS,
        description="every OPEN pull request, from `gh pr list --state open --json "
                    "number,title,headRefOid,mergeable,createdAt,isDraft`")
    #: The caller's round cap, if it has one. Deliberately not read off
    #: ``/dials``: the board stores dials as opaque JSON and does not know that
    #: ``review_panel.max_rounds`` is a number — see :mod:`app.api.dials` on why a
    #: second place that knew would be the drift #305 exists to end.
    max_rounds: int | None = Field(default=None, ge=1, le=100)


async def _last_runs(session: AsyncSession, repo: str,
                     numbers: list[int]) -> tuple[dict[int, ReviewRun], dict[int, int]]:
    """The newest run per PR, and how many rounds the PR has had in total.

    Newest by ``(ts, id)`` rather than by ``round``: two agents looping one PR
    interleave, so the highest round number is not reliably the latest thing that
    happened, and ``ReviewRun.cycle`` exists because of it.

    The total is NOT what the round cap is counted against — see
    :func:`_cycle_rounds`. It is published beside it because "this PR has had
    eleven rounds across four cycles" is a fact about the PR that no per-cycle
    number carries.
    """
    if not numbers:
        return {}, {}
    rows = list((await session.scalars(
        select(ReviewRun)
        .where(ReviewRun.pr.in_(numbers), ReviewRun.repo == repo)
        .order_by(ReviewRun.pr, ReviewRun.ts.desc(), ReviewRun.id.desc())
    )).all())
    newest: dict[int, ReviewRun] = {}
    counts: dict[int, int] = {}
    for run in rows:
        newest.setdefault(run.pr, run)
        counts[run.pr] = counts.get(run.pr, 0) + 1
    return newest, counts


async def _cleared(session: AsyncSession, repo: str,
                   run_ids: dict[int, int]) -> dict[int, int]:
    """Per PR, how many of its newest run's confirmed findings have an outcome.

    Joined on ``finding_key`` and scoped by ``(repo, pr)``, which is how
    :class:`~app.models.review.ReviewFindingOutcome` is keyed — the outcome
    outlives the round that first raised the defect, so it deliberately has no
    foreign key to a run.
    """
    if not run_ids:
        return {}
    stmt = (
        select(ReviewRun.pr, func.count(func.distinct(ReviewFinding.finding_key)))
        .select_from(ReviewFinding)
        .join(ReviewRun, ReviewRun.id == ReviewFinding.run_id)
        .join(
            ReviewFindingOutcome,
            (ReviewFindingOutcome.repo == repo)
            & (ReviewFindingOutcome.pr == ReviewRun.pr)
            & (ReviewFindingOutcome.finding_key == ReviewFinding.finding_key)
            & ReviewFindingOutcome.outcome.in_(sorted(CLEARING_OUTCOMES)),
        )
        .where(ReviewFinding.run_id.in_(list(run_ids.values())),
               ReviewFinding.verdict == "confirmed")
        .group_by(ReviewRun.pr)
    )
    return {pr: n for pr, n in (await session.execute(stmt)).all()}


async def _needs_human(session: AsyncSession, repo: str,
                       numbers: list[int]) -> dict[int, NeedsHuman]:
    """Per PR, the defects a person is owed an answer about, and since when (#279).

    "Waiting" is #279's own rule and nothing subtler: a flagged defect with no
    outcome recorded against it, any value — ``deferred`` retires it too, because
    that is somebody having ACTED and where the deferral went is on the outcome's
    own ``deferred_to``. Dismissed and ``sonar`` findings are excluded and
    ``unjudged`` ones are kept, for the reasons
    :func:`app.api.reviews.needs_human_open` gives.

    Only the COUNT and the age are derived here. Which judgement each defect wants
    lives at ``GET /review/needs-human``, which is the authority for the class
    vocabulary and for the "newest flagged observation decides" rule — a second
    implementation of that rule is exactly what #65's class of drift looks like,
    and one number needs none of it.

    The repo is compared exactly, as it is everywhere else on this path, and that
    is safe **since #326 folded the write**: ``POST /review`` stores the
    canonical spelling, this endpoint canonicalises ``body.repo`` once at the top,
    and migration ``0033``'s CHECK constraint is what stops the two drifting
    apart. The ``func.lower()`` these queries used to carry was the read-side half
    of the same idea, and it cost the ``ix_review_runs_repo_pr`` index to say what
    the column now guarantees. A differently-spelt repo reporting zero is the
    failure mode a queue can least afford, since zero here reads as "nobody is
    owed an answer".
    """
    if not numbers:
        return {}
    rows = (await session.execute(
        select(ReviewRun.pr, ReviewFinding.finding_key, func.min(ReviewRun.ts))
        .select_from(ReviewFinding)
        .join(ReviewRun, ReviewRun.id == ReviewFinding.run_id)
        .where(ReviewFinding.needs_human.is_(True),
               ReviewFinding.verdict.in_(("confirmed", "unjudged")),
               ReviewRun.pr.in_(numbers),
               ReviewRun.repo == repo)
        .group_by(ReviewRun.pr, ReviewFinding.finding_key)
    )).all()
    if not rows:
        return {}
    settled = {
        (pr, key) for pr, key in (await session.execute(
            select(ReviewFindingOutcome.pr, ReviewFindingOutcome.finding_key)
            .where(ReviewFindingOutcome.repo == repo,
                   ReviewFindingOutcome.pr.in_(numbers))
        )).all()
    }
    tally: dict[int, tuple[int, datetime]] = {}
    for pr, key, first in rows:
        if (pr, key) in settled:
            continue
        flagged = _aware(first) or _utcnow()
        seen, oldest = tally.get(pr, (0, flagged))
        tally[pr] = (seen + 1, min(oldest, flagged))
    return {pr: NeedsHuman(waiting=n, since=since) for pr, (n, since) in tally.items()}


def _exemptions(items: dict[int, PlanItem], now: datetime) -> dict[int, Exemption]:
    """The subset of those items that actually exempts, keyed by PR number.

    ``ix_plan_items_open_ref`` already makes "one open item per PR" a database
    fact, so there is never a choice to make between two items for one PR.
    """
    return {
        number: Exemption(
            item_id=str(item.id), title=item.title, note=item.note,
            added_by=item.added_by, updated=_aware(item.updated_at) or now,
            rank=item.rank,
        )
        for number, item in items.items() if exempting(item.note)
    }


async def _plan_items(session: AsyncSession, repo: str,
                      numbers: list[int]) -> dict[int, PlanItem]:
    """Every open plan item naming one of these PRs, exempting or not.

    Reported even when it does not exempt, because *"a PR with no plan item is in
    the queue"* is one of the two consequences #273 spells out, and a reader
    cannot check that claim against a response that only shows the exemptions.
    """
    if not numbers:
        return {}
    items = list((await session.scalars(
        select(PlanItem).where(
            PlanItem.repo == repo,
            PlanItem.state == "open",
            PlanItem.ref_kind == "pr",
            PlanItem.ref_value.in_(sorted({str(n) for n in numbers})),
        )
    )).all())
    out: dict[int, PlanItem] = {}
    for item in items:
        try:
            out[int(str(item.ref_value).lstrip("#"))] = item
        except (TypeError, ValueError):  # pragma: no cover - filtered by the IN above
            continue
    return out


async def _claims(session: AsyncSession, repo: str, numbers: list[int],
                  now: datetime) -> dict[int, Held]:
    """Live ``work`` claims on these PRs, by number.

    Keyed through :func:`app.claimkey.derive` rather than composed here, so a PR
    claimed by hand is a claim this queue can see. Two spellings of one resource
    is what made ``claims()`` useless for four months (#172).
    """
    if not numbers:
        return {}
    keys: dict[str, int] = {}
    for n in numbers:
        try:
            _, key = derive("pr", repo=repo, value=n)
        except BadRef:  # pragma: no cover - repo already canonicalised
            continue
        keys[key] = n
    if not keys:
        return {}
    rows = list((await session.scalars(
        select(ResourceLease).where(
            ResourceLease.kind == WORK,
            ResourceLease.key.in_(sorted(keys)),
            ResourceLease.released_at.is_(None),
            ResourceLease.expires_at > now,
        )
    )).all())
    return {
        keys[r.key]: Held(holder=r.holder, session=r.session, note=r.note,
                          expires=_aware(r.expires_at) or now)
        for r in rows if r.key in keys
    }


async def _landings(session: AsyncSession, repo: str, heads: dict[int, str | None],
                    now: datetime) -> dict[int, Landing]:
    """Live landing-queue entries for these PRs, with their FIFO position.

    Lapsed-but-unswept entries are filtered on the way past rather than swept,
    because a read must not mutate — the rule ``GET /merge-queue`` and
    ``GET /claims`` both keep. Every base is looked at: which branch a PR is
    landing onto is not this queue's business, only that #227 has it.

    ``heads`` is what makes the entry's testimony expire. An entry is a claim
    about a commit, and a PR that has been pushed since is not the PR that
    entry describes — ``GET /merge-queue?pr=&head=`` exists so a caller can say
    so without a write, and this is the same comparison made on the caller's
    behalf.
    """
    if not heads:
        return {}
    rows = list((await session.scalars(
        select(MergeQueueEntry)
        .where(MergeQueueEntry.repo == repo,
               MergeQueueEntry.left_at.is_(None),
               MergeQueueEntry.expires_at > now)
        .order_by(MergeQueueEntry.base, MergeQueueEntry.entered_at, MergeQueueEntry.id)
    )).all())
    out: dict[int, Landing] = {}
    position: dict[str, int] = {}
    for e in rows:
        position[e.base] = position.get(e.base, 0) + 1
        if e.pr in heads and e.pr not in out:
            at_head = same_commit(e.head_sha, heads[e.pr])
            out[e.pr] = Landing(
                verdict=e.verdict,
                # An entry ready at a commit the PR has left is not ready now.
                ready=is_ready(e) and at_head is not False,
                position=position[e.base], entered=_aware(e.entered_at) or now,
                head_sha=e.head_sha, at_head=at_head)
    return out


def _cycle_rounds(run: ReviewRun, total: int) -> int:
    """Rounds in the newest run's own cycle — what a round cap counts.

    ``round`` is 1-based within a cycle and the panel writes it, so the newest
    run's own number IS the count for that cycle. Falls back to the PR's total
    only for pre-v2.15 runs, where the column did not exist: over-counting an
    unlabelled history is the conservative direction, since it can only refuse a
    round, never buy one.
    """
    return run.round if run.round else total


def _last_run_view(run: ReviewRun, cleared: int) -> LastRun:
    confirmed = run.n_confirmed or 0
    return LastRun(
        run_id=run.id,
        ts=_aware(run.ts) or _utcnow(),
        round=run.round,
        head_sha=run.head_sha,
        stopped=run.stopped,
        stop_reason=run.stop_reason,
        stop_confident=run.stop_confident,
        stop_veto=run.stop_veto,
        pr_state=run.pr_state,
        ci_status=run.ci_status,
        confirmed=confirmed,
        outstanding=max(0, confirmed - cleared),
        cleared=min(cleared, confirmed),
    )


def _entry(repo: str, pr: PullRequestIn, verdict: Verdict, run: LastRun | None,
           rounds: int, rounds_total: int, exemption: Exemption | None,
           human: NeedsHuman | None, item: PlanItem | None,
           held: Held | None, landing: Landing | None, now: datetime) -> dict:
    return {
        "pr": pr.number,
        "title": pr.title,
        "state": verdict.state,
        "next_action": verdict.action,
        "reason": verdict.reason,
        "drainable": drainable(verdict),
        "holds": verdict.holds,
        "review_state": verdict.review_state,
        "review_action": verdict.review_action,
        "since": verdict.since.isoformat(),
        "since_basis": verdict.since_basis,
        "age_seconds": age_seconds(verdict, now),
        "age_is_upper_bound": verdict.age_is_upper_bound,
        "head": pr.head,
        "mergeable": (pr.mergeable or "UNKNOWN").upper(),
        "draft": pr.draft,
        "opened": pr.opened.isoformat() if pr.opened else None,
        # Rounds in the newest run's cycle, and across the PR's whole history.
        # The cap is counted against the first; the second is what says a PR has
        # been round the loop four times under four different cycles.
        "rounds": rounds,
        "rounds_total": rounds_total,
        "last_run": None if run is None else {
            "run_id": run.run_id,
            "ts": run.ts.isoformat(),
            "round": run.round,
            "head_sha": run.head_sha,
            "stopped": run.stopped,
            "stop_reason": run.stop_reason,
            "stop_confident": run.stop_confident,
            # As of that run, never live, and no state above is derived from
            # either — see LastRun on why, and on #324 in particular.
            "pr_state": run.pr_state,
            "ci_status": run.ci_status,
            "confirmed": run.confirmed,
            "cleared": run.cleared,
            "outstanding": run.outstanding,
        },
        # The count and the age only. `GET /review/needs-human?repo=&pr=` is the
        # authority for WHICH judgement each defect wants (#279).
        "needs_human": None if human is None else {
            "waiting": human.waiting,
            "first_flagged": human.since.isoformat(),
            "detail": f"GET /review/needs-human?repo={repo}&pr={pr.number}",
        },
        "exemption": None if exemption is None else {
            "item_id": exemption.item_id,
            "title": exemption.title,
            "note": exemption.note,
            "added_by": exemption.added_by,
            "updated": exemption.updated.isoformat(),
            "stale_seconds": max(0, int((now - exemption.updated).total_seconds())),
        },
        "plan_item": None if item is None else {
            "item_id": str(item.id),
            "title": item.title,
            "rank": item.rank,
            "note": item.note,
            "exempts": exemption is not None,
        },
        "claim": None if held is None else {
            "holder": held.holder,
            "session": held.session,
            "note": held.note,
            "expires": held.expires.isoformat(),
        },
        "landing": None if landing is None else {
            "verdict": landing.verdict,
            "ready": landing.ready,
            "position": landing.position,
            "entered": landing.entered.isoformat(),
            "head_sha": landing.head_sha,
            # False = the entry is about a commit this PR has left, so its
            # verdict has expired and #227 has not been handed the PR as it is.
            "at_head": landing.at_head,
        },
    }


def _oldest(entries: list[dict], want_drainable: bool) -> dict | None:
    pool = [e for e in entries if e["drainable"] is want_drainable]
    if not pool:
        return None
    e = max(pool, key=lambda x: x["age_seconds"])
    return {"pr": e["pr"], "state": e["state"], "next_action": e["next_action"],
            "since": e["since"], "since_basis": e["since_basis"],
            "age_seconds": e["age_seconds"],
            "age_is_upper_bound": e["age_is_upper_bound"]}


@router.post("/review-queue")
async def review_queue(
    body: ReviewQueueIn,
    _reader: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Every open PR review is not finished with: what it waits on, and since when.

    Send the repo's open PRs as ``gh pr list --json`` gives them; get back one
    entry each, carrying a ``state``, the ``next_action`` it implies, an ``age``
    with the basis it was measured from, and every ``hold`` standing between it
    and that action. ``counts.drainable`` is the queue's depth and ``oldest`` is
    the age this issue exists to make visible — until now it had to be
    reconstructed by hand from timestamps.

    Nothing is written and no order is decided: entries come back in PR order,
    which is a stable spelling and not a work order. The plan owns the order
    (#232) and the landing queue owns the landing order (#227).
    """
    try:
        repo = canonical_repo(body.repo)
    except BadRef as e:
        raise HTTPException(422, str(e)) from None

    now = _utcnow()
    numbers = [p.number for p in body.prs]
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicates:
        raise HTTPException(422, detail={
            "error": f"pull request(s) {duplicates} appear more than once",
            "hint": "the queue is one entry per open PR; send each number once",
        })

    newest, rounds = await _last_runs(session, repo, numbers)
    cleared = await _cleared(session, repo, {pr: r.id for pr, r in newest.items()})
    items = await _plan_items(session, repo, numbers)
    exempt = _exemptions(items, now)
    human = await _needs_human(session, repo, numbers)
    held = await _claims(session, repo, numbers, now)
    landing = await _landings(session, repo, {p.number: p.head for p in body.prs}, now)

    entries: list[dict] = []
    for p in sorted(body.prs, key=lambda x: x.number):
        run = newest.get(p.number)
        last = _last_run_view(run, cleared.get(p.number, 0)) if run is not None else None
        total = rounds.get(p.number, 0)
        cycle = _cycle_rounds(run, total) if run is not None else 0
        verdict = classify(
            PullRequest(number=p.number, head=p.head, mergeable=p.mergeable,
                        opened=_aware(p.opened), title=p.title, draft=p.draft,
                        escalated=p.escalated),
            run=last,
            rounds=cycle,
            exemption=exempt.get(p.number),
            needs_human=human.get(p.number),
            held=held.get(p.number),
            landing=landing.get(p.number),
            max_rounds=body.max_rounds,
            now=now,
        )
        entries.append(_entry(repo, p, verdict, last, cycle, total,
                              exempt.get(p.number), human.get(p.number),
                              items.get(p.number), held.get(p.number),
                              landing.get(p.number), now))

    by_state = {s: sum(1 for e in entries if e["state"] == s) for s in STATES}
    by_action = {a: sum(1 for e in entries if e["next_action"] == a) for a in ACTIONS}
    by_hold: dict[str, int] = {h: 0 for h in HOLDS}
    for e in entries:
        for h in e["holds"]:
            by_hold[h["code"]] = by_hold.get(h["code"], 0) + 1

    return {
        "repo": repo,
        "generated": now.isoformat(),
        "derivation": "state, not events: every open PR you sent, joined to what the "
                      "board already knows. Nothing is enqueued and nothing is stored, "
                      "so a backlog opened before this endpoint existed comes back in "
                      "full (#273 vs #54)",
        "ordering": "pr number ascending — a stable spelling, NOT a work order. The "
                    "plan owns the order (#232) and the landing queue owns the landing "
                    "order (#227)",
        "counts": {
            "open": len(entries),
            "drainable": sum(1 for e in entries if e["drainable"]),
            "held": sum(1 for e in entries if not e["drainable"]),
            "by_state": by_state,
            "by_next_action": by_action,
            "by_hold": by_hold,
            "waiting_on_a_human": sum(
                (e["needs_human"] or {}).get("waiting", 0) for e in entries),
        },
        "oldest": _oldest(entries, want_drainable=True),
        "oldest_held": _oldest(entries, want_drainable=False),
        "idle_reason": idle_reason(entries),
        "max_rounds": body.max_rounds,
        "vocabulary": {"states": list(STATES), "next_actions": list(ACTIONS),
                       "holds": list(HOLDS), "age_bases": list(AGE_BASES)},
        "entries": entries,
    }
