"""The line to land on a branch: who is next, who is waiting, and why — #227.

``kind='merge'`` (#99) is a one-slot advisory claim meaning *somebody is landing
on this branch right now*. It answers exactly one question and #227 is about the
four it cannot: which PR is next, who is second, which ready PRs should wait, and
whether the agent about to spend twenty minutes of CI is anywhere near the front.

Without those answers every review-clean PR behaves as though it were next. It
merges the base, pushes, waits for CI, re-runs preland, discovers somebody else
landed, and does it again — #80's quadratic integration cost, plus a failure mode
of its own: each loser's integration push invalidates the winners' green checks on
the way past. #278 stopped a *distant* integration throwing away a review;
nothing stopped five agents each racing to be the one who integrates.

**This is ordering and visibility around the claim, not a second lock.** No path
in this module takes, renews or releases a ``kind='merge'`` claim, and no path
refuses one. Being at the head of the queue is not permission to merge — it is
permission to go and ask for the claim, which may still be held by somebody who
never enqueued at all, and :func:`_claim_view` reports that holder rather than
pretending the queue outranks them. Two implementations of "who has this right
now" is the outcome #99 was filed to avoid, and a queue that also held the
resource would have been the second one.

**Strict FIFO, and only FIFO — for ``active_order``.** The live queue is arrival
order and nothing in this module reorders it. What #80 added is a second,
strictly advisory field beside it: ``suggested_order``, computed in
:mod:`app.ranking` from the file overlap between the queued PRs (#82's changed
lists, #101's classification) weighted by how expensive each PR is to
re-integrate late. It is an opinion, published where an opinion is visibly not
the queue, and #227's own argument is why the separation is absolute rather than
tidy: *"agents may propose order; they must not silently rewrite the queue while
also trying to land… otherwise the queue itself becomes another shared resource
every agent thrashes."* So a rank is not a place in the line, being ranked first
is not being at the head, and nothing merges on a suggestion's say-so. Mutation
still needs a human or an accepted proposal, and the ``order-proposal`` /
``reorder`` endpoints that would carry an accepted one are still not here. #227
stays open for that half.

**And the suggestion refuses to be confident on partial data.** It is null unless
every queued PR has a changed-file list the board can read, because a PR nobody
can measure has no honest position — and the largest such hole (#94: the panel's
title-skip path records no files, so merges, promotes and format-the-world
commits are invisible) is exactly the set that the cost model says should land
*first*. The evidence still comes back either way, with an ``order_trust`` block
in the ``plan_read`` shape saying what the order is worth.

**The board takes testimony, not measurements.** It cannot run preland, read CI or
ask GitHub whether a PR is a draft. What ``verdict`` and ``head`` do is pin the
caller's claim about its own PR to a specific commit — so the claim expires by
itself when the branch moves, which is the one thing an agent's own memory of
"I was ready" structurally cannot do.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Text, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY, aggregate_order_by
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.claims import clean_session, is_unique_violation, live_claim
from app.auth import identify, optional_identity, reader
from app.claimkey import BadRef, derive
from app.collisions import UNANSWERABLE
from app.db import get_session
from app.models.merge_queue import PROCEEDS, VERDICTS, MergeQueueEntry
from app.models.resource_lease import ResourceLease
from app.models.review import ReviewRun, ReviewRunFile
from app.ranking import (
    SHARED_RESOURCES,
    SHARED_SAMPLE_CAP,
    Candidate,
    Overlap,
    Ranking,
    rank,
    shared_resource_keys,
)

router = APIRouter(tags=["merge-queue"])

#: How long an entry survives without being renewed. Much shorter than a claim's
#: hour, and on purpose: a lapsed claim frees a resource nobody is using, while a
#: lapsed queue entry lets everybody behind it move. The cost of getting this
#: wrong is asymmetric — an agent that is still working renews on its next poll
#: and keeps its place (``entered_at`` is never bumped), whereas a head that died
#: holds the whole line for however long this is.
#:
#: The number is not what #405 was about, and raising it was the wrong fix twice.
#: What renews an entry is: see :func:`_renew_on_read`. Until that landed, every
#: act that renewed one — enqueue at a new head, in practice a push — was an act
#: this endpoint's own ``reason`` tells a waiter not to take, so the agents most
#: likely to lose their place were the ones following instructions. PR #398's
#: landing was measured at 5m37s against this 1800s window; the 30 minutes that
#: expired its entry were 27 minutes of waiting politely.
DEFAULT_TTL = 1800
MAX_TTL = 86_400

#: The longest session identifier that means anything — the claim table's bound,
#: for the same reason it has one.
MAX_SESSION = 200

#: A git object name, full length. The rule is
#: :data:`app.api.reviews._SHA_RE`'s and the *trade* is the opposite one: reviews
#: drop a garbled head rather than lose a run's findings, because recording is
#: best-effort there. Here the head is the entire mechanism — an entry is ready
#: exactly while ``ready_sha == head_sha`` — so a value that is not a commit id is
#: refused with a 422. Dropping it would leave an entry whose readiness could
#: never expire, which is a permanent green light rather than a missing field.
_SHA_RE = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

#: What preland says when a PR is genuinely not landable, and the one verdict this
#: endpoint refuses to enqueue. Named so the refusal can say it back.
_HOLD = "hold"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _norm_sha(value: str) -> str:
    """A commit id, lower-cased, or a 422. Never a repair."""
    sha = value.strip().lower()
    if not _SHA_RE.match(sha):
        raise HTTPException(422, detail={
            "error": f"{value!r} is not a commit id",
            "hint": "send the PR's full head oid (`gh pr view --json headRefOid`). "
                    "The queue invalidates your readiness when this changes, so a "
                    "value that cannot be compared is a readiness that never expires",
        })
    return sha


def merge_key(repo: str, base: str) -> tuple[str, str]:
    """The ``kind='merge'`` claim this queue is the line for — ``(kind, key)``.

    Derived through :mod:`app.claimkey` rather than composed here, so the queue
    and the claim cannot end up naming the same land two ways. That is #172's
    whole finding, and a new table that spelled the key itself would be the next
    place to reproduce it.

    It also validates: the repo must be ``owner/name`` and the base must be a
    branch name ``git check-ref-format`` would accept, so a queue cannot be opened
    on a ref that cannot exist.

    The base is the branch a *lander* claims, which #318 settled after this landed
    keying on it: ``preland.check_merge_claim`` read the HEAD branch until then, so
    the queue reported a claim at one key while the gate read another and the two
    named one land two ways — the very thing the paragraph above says this function
    exists to prevent. Both read ``<repo>:<base>`` now.
    """
    return derive("branch", repo=repo, value=base)


def _scope(repo: str, base: str) -> tuple[str, str, str]:
    """``(repo, base, key)`` for a queue, validated. Raises 422 like the rest."""
    try:
        _, key = merge_key(repo, base)
    except BadRef as e:
        raise HTTPException(422, str(e)) from None
    # `derive` canonicalises the repo (lower-cased) and leaves the branch alone;
    # read both back off the key rather than re-deriving, so the row stored and
    # the claim looked up cannot disagree by one normalisation.
    canon_repo, _, canon_base = key.partition(":")
    return canon_repo, canon_base, key


async def _sweep_lapsed(session: AsyncSession, repo: str, base: str,
                        now: datetime) -> None:
    """Retire entries whose TTL ran out, so the queue advances past a dead head.

    Passive, exactly as ``app.api.claims._sweep_lapsed`` is: it runs only when
    somebody asks about this queue, so a quiet branch costs nothing and there is
    no reaper to wedge. ``lapsed`` is set as well as ``left_at`` because "landed
    and stood down" and "stopped answering" are different facts about a queue
    head, and a board that showed them alike would report an abandoned land as a
    finished one.

    Called only from the write paths, and still is. ``GET`` filters expired rows
    on the way past rather than sweeping them, so a read never retires anybody
    else's entry — the rule ``GET /claims`` keeps, and the half of it that matters:
    one caller's read must not change what another caller is told. The one write a
    read now makes is :func:`_renew_on_read`, which touches a single row, only the
    caller's own, and only to push its expiry out.
    """
    await session.execute(
        update(MergeQueueEntry)
        .where(MergeQueueEntry.repo == repo, MergeQueueEntry.base == base,
               MergeQueueEntry.left_at.is_(None), MergeQueueEntry.expires_at <= now)
        .values(left_at=now, lapsed=True,
                left_reason="entry lapsed: its holder stopped renewing")
    )


async def _live_entries(session: AsyncSession, repo: str, base: str,
                        now: datetime) -> list[MergeQueueEntry]:
    """The queue, in order: still in, not expired, oldest arrival first.

    ``pr`` breaks a tie on ``entered_at``. Two entries can share a timestamp, and
    an order that then depended on which row the planner returned first would
    report two different heads on two consecutive reads — worse than no queue,
    because both agents would believe they were next.
    """
    return list((await session.scalars(
        select(MergeQueueEntry)
        .where(MergeQueueEntry.repo == repo, MergeQueueEntry.base == base,
               MergeQueueEntry.left_at.is_(None), MergeQueueEntry.expires_at > now)
        .order_by(MergeQueueEntry.entered_at, MergeQueueEntry.pr)
    )).all())


#: A renewal is written as ``now + ttl_seconds``, and ``ttl_seconds`` is a column,
#: so the arithmetic happens in the database rather than in a read-then-write that
#: two overlapping polls could interleave.
_ONE_SECOND = text("interval '1 second'")


async def _renew_on_read(session: AsyncSession, repo: str, base: str, pr: int,
                         caller: str | None, now: datetime) -> datetime | None:
    """Push this PR's expiry out because its own holder just asked where it is.

    Returns the new expiry, or None when nothing was renewed.

    **A waiter asking its position IS the liveness the TTL was approximating.**
    Before this, the only acts that renewed an entry were enqueues — in practice a
    push, an integration, a re-run of CI — and :func:`decide` tells a non-head not
    to do any of them, because each costs a real CI run to learn what the line
    already says and invalidates the head's checks on the way past. So obeying the
    queue was what made an agent lapse from it, and the entries that expired were
    the well-behaved ones. #405 measured that twice on one night: PR #398's whole
    landing took 5m37s against a 1800-second window, and it lost its place anyway,
    27 minutes of which was spent waiting quietly. Renewing on the read removes the
    proxy and measures the thing itself.

    It still fails safe, which is the only property the TTL was ever for: an agent
    that has genuinely died stops reading, and its entry lapses exactly as before.
    An agent that polls forever holds its place forever — deliberately, because
    "still asking" and "still working" are the same fact from the board's side, and
    a head that holds position without landing is a thing to *observe* (qb-doctor's
    `queue` row) rather than a thing for this endpoint to evict. #405 and #227 both
    settled that the queue stays advisory.

    **Only the entry's own holder renews it, and that is the whole authorisation.**
    The holder recorded here is what :func:`app.auth.identify` returned to the
    enqueue — machine proved by the token, agent name allocated by the board — and
    the caller is resolved the same way, so this is not a field anybody can assert
    about themselves. Everything else that reads this endpoint renews nothing:

    * a **browser** on the human board authenticates at the edge and resolves to
      ``human/<user>``, which no entry is ever held by;
    * a **peer** checking on the head it waits behind (the whole reason ``pr`` may
      name somebody else's PR) is not evidence that the head's agent is alive —
      renewing there would let the queue's most attentive waiter hold a dead head's
      place indefinitely, which is precisely the case the timer exists for;
    * a **monitor** — `qb-doctor`, `qb-dash`, `qb-reconcile` — reads the queue
      whole, without a ``pr``, and renews nothing at all. That is why a bare read
      does not renew everything the caller holds: a poller on the same box would
      otherwise keep a dead agent's entry alive, and nothing on the board could
      tell the two apart.

    A **lapsed** entry is not renewed back to life: ``expires_at > now`` is in the
    predicate, so this cannot resurrect what a sweep has already retired, and a
    holder whose entry has gone re-enqueues (at the back, which is honest) rather
    than discovering it had silently been restored.

    ``updated_at`` is deliberately NOT moved. That column orders *content* writes —
    :func:`_join` refuses an enqueue stamped older than the row it would overwrite,
    which is what stops a slow poll putting a stale ``ready`` verdict back onto a
    commit the PR has moved off. A read that bumped it would make an in-flight
    enqueue lose that comparison and report the PR's old head back to the agent
    that had just told it the new one.

    Two things a second reviewer (codex) asked about, recorded here because the
    answers are the design rather than an oversight:

    * **Within one machine token, this grants nothing new.** The agent key that
      distinguishes ``zeus/one`` from ``zeus/two`` is client-supplied and unproved
      — :mod:`app.identity` says why — so anything holding that machine's bearer
      token can present itself as either. It could also simply
      ``POST /merge-queue/enqueue`` for that PR, which rewrites ``holder``,
      ``verdict``, ``head_sha`` and the expiry together. Renewal is strictly less
      than that, and the boundary it does enforce is the one a token actually
      proves: another *machine*, and a person at the human board, renew nothing.
    * **Expiry is judged against this request's own ``now``**, the single stamp
      :func:`_sweep_lapsed` and :func:`_live_entries` also use, so the three cannot
      disagree inside one answer. The cost is a window between stamping ``now`` and
      executing this statement — the first thing the handler does — in which an
      entry could cross its expiry and be renewed a moment after lapsing. That is
      sub-millisecond and self-correcting; a second clock source, judging one row by
      wall time while the response's own listing judged it by the request stamp,
      would buy it back by letting one answer contradict itself.
    """
    if caller is None:
        return None
    got = await session.execute(
        update(MergeQueueEntry)
        .where(MergeQueueEntry.repo == repo, MergeQueueEntry.base == base,
               MergeQueueEntry.pr == pr, MergeQueueEntry.left_at.is_(None),
               MergeQueueEntry.expires_at > now,
               MergeQueueEntry.holder == caller)
        # GREATEST, so a renewal can only ever push the expiry out. Two of one
        # agent's polls can overlap — it is a poll loop, that is what they do — and
        # a plain assignment would let the older one land second and pull the entry
        # in by the gap between them. Renewal is a floor under the expiry, never a
        # restatement of it. (codex)
        .values(expires_at=func.greatest(
            MergeQueueEntry.expires_at,
            literal(now) + MergeQueueEntry.ttl_seconds * _ONE_SECOND))
        .returning(MergeQueueEntry.expires_at)
    )
    fresh = got.scalar_one_or_none()
    await session.commit()
    return fresh


def _machine(agent: str) -> str:
    """The half of a board identity a token proves — ``zeus`` of ``zeus/jasper-moss``.

    Used only to phrase a refusal, never to grant one: a machine runs several
    agents at once and they all authenticate as it, so "same box" is not "same
    session" and cannot be what authorises a renewal.
    """
    return agent.partition("/")[0]


def _renewal_view(entries: list[MergeQueueEntry], pr: int, caller: str | None,
                  renewed: datetime | None) -> dict:
    """Did this read keep the PR's place, and if not, why not — said out loud.

    Reported rather than done silently, because a renewal an agent cannot observe
    is a renewal it cannot rely on: the failure #405 records is precisely a queue
    that was faithfully reporting its state to anyone who asked while nobody could
    see the mechanism that was retiring them. An agent that is polling to hold its
    place gets to read back that the polling worked, and a peer that expected to
    be renewing somebody else learns in one line that it never was.
    """
    if renewed is not None:
        return {"renewed": True, "expires": renewed.isoformat(),
                "why": f"this read renewed #{pr}'s entry: you hold it, and asking "
                       f"where you are in the line is what keeps your place"}
    entry = next((e for e in entries if e.pr == pr), None)
    if caller is None:
        why = ("nothing was renewed: this read carried no agent identity. Only the "
               "agent an entry names can renew it, and a read authorised at the "
               "edge is a person looking, not an agent working")
    elif entry is None:
        why = (f"nothing was renewed: #{pr} has no live entry on this base, so "
               f"there is no place to keep")
    elif entry.holder != caller:
        why = (f"nothing was renewed: #{pr}'s entry is held by {entry.holder}, and "
               f"only its holder renews it — your reading about it is not evidence "
               f"that they are still there")
        if _machine(entry.holder) == _machine(caller):
            # The near miss, and the one worth spelling out: a box runs several
            # agents, and a caller that authenticated as the bare machine (or as a
            # different agent on it) is not the session that enqueued. Said with
            # the way out, because there is one and it costs nothing — enqueueing
            # again rewrites `holder` and never touches `entered_at`.
            why += (f". That is this machine under another name, so if the land has "
                    f"been handed over, enqueue #{pr} again — it rewrites the holder "
                    f"and keeps the place")
    else:
        why = (f"nothing was renewed: #{pr}'s entry was retired between this read "
               f"and the renewal, so it is no longer in the line")
    return {"renewed": False, "expires": None, "why": why}


def is_ready(entry: MergeQueueEntry) -> bool:
    """Does the board hold a ``ready`` verdict about the commit this entry is on?

    Both halves matter and the check constraint keeps them together: an entry may
    only carry the proceeding verdict while ``ready_sha`` equals ``head_sha``. An
    agent remembers "preland said READY" and does not reliably notice that the
    thing preland said it about was three pushes ago; a row cannot forget which
    commit it was talking about.
    """
    return entry.verdict == PROCEEDS and entry.ready_sha == entry.head_sha


def _position(entries: list[MergeQueueEntry], pr: int) -> int | None:
    """Where this PR is in the line, or None if it is not in it.

    Total on purpose. The obvious spelling — ``[e.pr for e in entries].index(pr)``
    — raises when the entry is not there, and "not there" is reachable: a
    concurrent ``leave`` can retire an entry between the write and the read that
    renders it, and a 500 out of a successful enqueue is the worst of both
    answers.
    """
    for i, e in enumerate(entries, start=1):
        if e.pr == pr:
            return i
    return None


def entry_view(e: MergeQueueEntry, position: int | None) -> dict:
    return {
        "entry_id": str(e.id),
        "pr": e.pr,
        "position": position,
        "head": e.head_sha,
        "ready_sha": e.ready_sha,
        "verdict": e.verdict,
        "ready": is_ready(e),
        "holder": e.holder,
        "session": e.session,
        "note": e.note,
        "entered": e.entered_at.isoformat(),
        "updated": e.updated_at.isoformat(),
        "expires": e.expires_at.isoformat(),
    }


def _claim_view(claim: ResourceLease | None, key: str) -> dict:
    """Who holds the land right now, read-only.

    The queue reports this and never acts on it. An agent at the head of the line
    that finds the claim held has learned something useful (go and talk to that
    holder) and gained no authority whatsoever — a human merging in the UI, or an
    agent that never enqueued, lands regardless, which is the same advisory
    boundary ``app.api.claims`` insists on and must not be softened by a table
    that looks more official.
    """
    if claim is None:
        return {"key": key, "held": False, "holder": None, "session": None, "note": None}
    return {
        "key": key,
        "held": True,
        "claim_id": str(claim.id),
        "holder": claim.holder,
        "session": claim.session,
        "note": claim.note,
        "expires": claim.expires_at.isoformat(),
    }


#: Below this many live entries there is nothing to order, so none of the work
#: below is done at all. A queue of one has exactly one arrangement, and this
#: endpoint is polled in a loop by an automated lander whose queue is usually
#: that long — so the cost of the ranking is paid only where an order could
#: differ from the arrival order.
MIN_RANKABLE = 2


async def _evidence(session: AsyncSession, repo: str,
                    entries: list[MergeQueueEntry]) -> tuple[list[Candidate], list[Overlap]]:
    """Everything :func:`app.ranking.rank` weighs, read once, for one queue.

    **Each PR's newest run answers for it, and there is no fallback to an older
    one.** ``GET /review/collisions`` lets its *subject* fall back to the newest
    file-bearing run, on the grounds that a caller naming a PR wants that PR
    answered for; every entry here is one somebody named, so the same argument
    would apply to all of them — and it is refused anyway. A stale list produces
    a confident wrong answer (a PR ranked disjoint on the files it touched three
    rounds ago); no list produces a loud unanswerable one that suppresses
    ``suggested_order`` entirely. Loud beats silent, so the newest run answers or
    nothing does.

    No time window either. ``days`` on the collisions endpoint bounds which
    *rivals* are current enough to be worth considering; here the population is
    already bounded — it is the live queue — and a PR queued to land today is in
    play whenever its last panel ran. The run's ``ts`` rides along on every row so
    a reader can see the age of the evidence rather than have it silently
    filtered.
    """
    prs = [e.pr for e in entries]
    # One unconditional DISTINCT ON per queued PR: this repo, these PRs, and
    # nothing else. The selection rule is `app.collisions`' — any predicate in
    # front of it can resurrect an older run and hand back its answer in a
    # confident voice.
    newest = (
        select(ReviewRun.id.label("run_id"), ReviewRun.pr.label("pr"),
               ReviewRun.ts.label("ts"),
               ReviewRun.changed_files_total.label("total"))
        .where(ReviewRun.repo == repo, ReviewRun.pr.in_(prs))
        .distinct(ReviewRun.pr)
        .order_by(ReviewRun.pr, ReviewRun.ts.desc(), ReviewRun.id.desc())
        .subquery()
    )
    # A correlated count, not a join: a run that recorded no paths must come back
    # as 0 and stay in the population, because it is precisely the row whose
    # absence would read as "answered, and disjoint".
    recorded = (
        select(func.count()).select_from(ReviewRunFile)
        .where(ReviewRunFile.run_id == newest.c.run_id).scalar_subquery()
    )
    answered = {
        pr: (run_id, ts, total, count)
        for pr, run_id, ts, total, count in (await session.execute(
            select(newest.c.pr, newest.c.run_id, newest.c.ts, newest.c.total, recorded)
        )).all()
    }
    run_ids = [row[0] for row in answered.values()]

    # Which of the queue's runs touch a shared resource. Fetched as paths and put
    # through `shared_resource_keys` rather than classified in SQL, so the query
    # and the ranking cannot drift about what a `migrations/` path is.
    resources: dict[int, list[str]] = {}
    overlaps: list[Overlap] = []
    if run_ids:
        for run_id, path in (await session.execute(
            select(ReviewRunFile.run_id, ReviewRunFile.path)
            .where(ReviewRunFile.run_id.in_(run_ids),
                   or_(*[ReviewRunFile.path.like(f"{key}%") for key in SHARED_RESOURCES]))
        )).all():
            resources.setdefault(run_id, []).append(path)

        # The overlap, as a self-join on path, aggregated per PAIR. Whole path
        # sets are never read out of Postgres: a PR may hold 3,000 of them and a
        # queue holds several PRs, so what crosses the wire is one row per
        # colliding pair. `ix_review_run_files_path` is (path, run_id) exactly so
        # this join is answered from the index.
        #
        # `a.run_id < b.run_id` gives each pair once and drops the self-pairs;
        # the count is untrimmed and the sample is sliced in the database, the
        # same split `GET /review/collisions` makes between the number a ranking
        # weighs by and the paths a person reads.
        by_run = {run_id: pr for pr, (run_id, *_rest) in answered.items()}
        mine = aliased(ReviewRunFile)
        theirs = aliased(ReviewRunFile)
        sample = func.array_agg(
            aggregate_order_by(mine.path, mine.path.asc()), type_=ARRAY(Text),
        )[1:SHARED_SAMPLE_CAP]
        for a_run, b_run, shared, paths in (await session.execute(
            select(mine.run_id, theirs.run_id, func.count(), sample)
            .join(theirs, theirs.path == mine.path)
            .where(mine.run_id.in_(run_ids), theirs.run_id.in_(run_ids),
                   mine.run_id < theirs.run_id)
            .group_by(mine.run_id, theirs.run_id)
        )).all():
            overlaps.append(Overlap(a=by_run[a_run], b=by_run[b_run],
                                    shared=shared, sample=tuple(paths or ())))

    candidates = []
    for position, e in enumerate(entries, start=1):
        run_id, ts, total, count = answered.get(e.pr, (None, None, None, 0))
        candidates.append(Candidate(
            pr=e.pr, position=position, ready=is_ready(e),
            changed_files_total=total, files_recorded=count,
            run_id=run_id, run_ts=ts.isoformat() if ts is not None else None,
            resources=shared_resource_keys(resources.get(run_id, ())),
        ))
    return candidates, overlaps


#: The axes #227 lists that this ranking does not weigh, each with the reason and
#: the issue that would close it. In the payload rather than only in
#: :mod:`app.ranking`'s docstring, because a consumer deciding how much of its
#: landing decision to hand over needs to see the shape of what was left out —
#: and "file overlap said they are disjoint" is a very different claim from "file
#: overlap said they are disjoint and nothing modelled whether one gates the
#: other".
UNWEIGHED_AXES = (
    {"axis": "plan dependencies / the landing graph", "issue": 294,
     "why": "which PRs gate which — fanning out and in, ACROSS repos, with hard "
            "temporal edges — has no representation anywhere yet. It is the axis "
            "file overlap structurally cannot see: two PRs can be perfectly "
            "disjoint in files and still have a strict landing order"},
    {"axis": "hunk-level overlap", "issue": None,
     "why": "review_run_files stores paths, not ranges. Two PRs editing different "
            "functions of one file are counted as colliding here and usually merge "
            "cleanly, so a collision is an upper bound on the conflict"},
    {"axis": "CI status", "issue": None,
     "why": "the board takes testimony, not measurements — it cannot read a check "
            "run, and an order weighted by a fact nobody can verify is an order "
            "nobody can check"},
    {"axis": "preland readiness", "issue": None,
     "why": "MEASURED and deliberately not ranked on: it is reported per row and "
            "excluded from the sort. A verdict is invalidated by every push, so "
            "tiering on it would reshuffle the proposal each time the head does "
            "the one thing its slot is for — the trade active_order already "
            "refuses, in `a head change invalidates readiness, and does not cost "
            "the slot`"},
    {"axis": "release-number contention", "issue": 168,
     "why": "every branch takes its number from main at land time, so two PRs "
            "carrying an unstamped vNEXT collide on a resource no file list names. "
            "The pre-push hook refuses a branch that edits CHANGELOG.md, so the "
            "collision is not visible in review_run_files at all"},
)


def _caveat(ranking: Ranking, rows: list[dict]) -> str | None:
    """What the order must say about itself when the data behind it is thin.

    ``plan_read``'s ``next.caveat`` set the precedent and the argument is the
    same one: the answer is still the best available and an agent that reads
    nothing else should get it — what it must not get is unqualified confidence.
    Returned in the payload, never left in a docstring, because a consumer cannot
    read a docstring.
    """
    if ranking.unranked:
        blind = ", ".join(f"#{pr}" for pr in ranking.unranked)
        return (
            f"there is NO suggested_order: {len(ranking.unranked)} of "
            f"{len(rows)} queued PRs ({blind}) have no changed-file list on the "
            f"board, so any position given to them would be invented and every "
            f"position around them would be derived from a partial measurement. "
            f"The ranking below covers the other {len(ranking.order)} and is "
            f"published as `partial_order` precisely so it cannot be mistaken for "
            f"the whole queue. A PR lands here either because no panel ever ran on "
            f"it, or because the panel SKIPPED it — and the skip path catches "
            f"merges, promotes and format-the-world commits (#94), which under "
            f"this cost model are the PRs that should land FIRST. Run a panel "
            f"round on them, or land #94"
        )
    if not ranking.trusted:
        prefix = ", ".join(f"#{r['pr']}" for r in rows if not r["files_complete"])
        return (
            f"advisory, and not attested: the stored file list for {prefix} is a "
            f"prefix of what that PR touches, so a shared-path count is a FLOOR "
            f"and no PR in this queue can be proven disjoint. The error runs one "
            f"way — there may be more collisions than were found, never fewer — so "
            f"the order is the best evidence available and a `disjoint` row is a "
            f"description of what was seen rather than a safety claim"
        )
    return None


def _suggestion(ranking: Ranking) -> dict:
    """The proposal and its provenance, in one block a consumer can act on.

    Split from ``suggested_order`` at the top level on purpose. That field is the
    confident artefact and #227's acceptance criterion, so it is null whenever the
    order would not be a permutation of the queue; this block always carries the
    reasoning, so a queue the board cannot fully answer for still yields its
    per-PR evidence to a human or to a later consensus step instead of yielding
    nothing.
    """
    rows = [
        {
            "pr": r.pr, "rank": r.rank, "tier": r.tier, "moved": r.moved,
            "weight": r.weight, "weight_basis": r.weight_basis,
            "shared_total": r.shared_total,
            # First-hand, pinned to a commit, and NOT an input to the sort. Here
            # so a reader can apply it themselves.
            "ready": r.ready,
            "files_complete": r.files_complete,
            "run_id": r.run_id, "run_ts": r.run_ts,
            "reason": r.reason,
            "collides_with": list(r.collides_with),
        }
        for r in ranking.rows
    ]
    return {
        # The ranked subset, always — a permutation of the queue only when
        # `covers_all`, which is exactly when `suggested_order` above is non-null.
        "partial_order": list(ranking.order),
        "unranked": list(ranking.unranked),
        "covers_all": ranking.covers_all,
        "differs_from_active": ranking.differs,
        "counts": dict(ranking.counts),
        "cost_model": (
            "reordering CANNOT reduce #80's integration count — every colliding "
            "pair pays one re-integration whichever end lands first, so the total "
            "is a property of the collision graph and is invariant under "
            "permutation. What an order changes is which end pays. The work falls "
            "on the LATER PR (merge the moved base into it, re-run its CI, re-run "
            "its panel round), so cost(order) = sum over colliding pairs i-before-j "
            "of shared(i,j) * w(j), and that is minimised for every pair at once by "
            "landing the heaviest first: the big branch lands clean and the small "
            "ones rebase onto it, instead of the big one being re-merged against a "
            "base that moved under it. w is the changed-file count, which is a "
            "proxy for how expensive a re-integration is and not a measurement of "
            "it. Disjoint PRs cost nothing from any position, so they are placed "
            "where they wait least"),
        "tiers": (
            "disjoint (complete list, shares nothing with any other queued PR) "
            "first, then collides (heaviest first), then partial (list is a "
            "prefix, so `no shared path found` is not evidence of none), then "
            "unanswerable — which is not ranked at any position"),
        "order_trust": {
            # `trusted` is about the EVIDENCE, not about whether the sort ran.
            "trusted": ranking.trusted,
            "measured": len(ranking.order),
            "unmeasured": len(ranking.unranked),
            # Measured rows only. A PR with no list at all is already counted as
            # `unmeasured`, and letting it into this number too would report one
            # blind PR as two different shortfalls — a caller adding them up to
            # check the population against `counts` would find they do not.
            "incomplete_lists": sum(1 for r in rows
                                    if r["tier"] != UNANSWERABLE and not r["files_complete"]),
            "blind_spots": [
                {"pr": r["pr"], "class": UNANSWERABLE,
                 "why": "no run of this PR recorded a changed-file list",
                 "issue": 94}
                for r in rows if r["tier"] == UNANSWERABLE],
            "caveat": _caveat(ranking, rows),
        },
        "axes_not_weighed": [dict(axis) for axis in UNWEIGHED_AXES],
        "advisory": (
            "a proposal, and nothing acts on it. It does not mutate active_order, "
            "it is not permission to merge, and the queue stays FIFO by arrival "
            "unless a human reorders it. Being ranked first is not being at the "
            "head"),
        "prs": rows,
    }


def decide(entries: list[MergeQueueEntry], pr: int,
           at_head: str | None = None) -> dict:
    """What may this PR do right now, and why — the whole point of the queue.

    Returns ``may_integrate`` and ``may_merge`` rather than one verdict, because
    they are different permissions and collapsing them is exactly what the
    behaviour this replaces gets wrong. A non-head may do neither: it must not
    rebase, push or restart CI, because all three cost a real CI run to discover
    something the board already knew, and the push also invalidates the head's
    green checks. The one thing it *may* do is ask again — and since #405 that is
    also what holds its place, so the advice no longer costs an agent the entry it
    is being advised about. The head may integrate — that is what its slot is for — but may
    only merge while the board holds a ``ready`` verdict about the commit the PR
    is actually on.

    ``at_head`` is the caller's own reading of the PR's current head, and it is
    how a head change invalidates readiness without anything being written: a
    caller that has just asked GitHub, or a peer checking on somebody else's
    entry, passes it and is told the entry is behind the branch. Omitting it
    means "judge the entry as it stands", which is the honest answer for a caller
    that does not know.

    ``reason`` is populated on the yes as well as the no. An agent that is allowed
    to proceed still has to be able to say why on the board, and a caller that
    only ever logs refusals learns nothing about the grants.
    """
    order = [e.pr for e in entries]
    if pr not in order:
        return {
            "queued": False, "position": None, "is_head": False,
            "may_integrate": False, "may_merge": False,
            "reason": (f"#{pr} is not in the queue for this base — enqueue it "
                       f"before landing, so everyone else can see the line"),
            "waiting_on": None,
        }
    position = order.index(pr) + 1
    entry = entries[position - 1]
    # Position is checked FIRST, before anything about readiness. A non-head that
    # was told "your head moved" would go and do the one thing this queue exists
    # to stop it doing: push, and burn a CI run, while not being next.
    if position > 1:
        ahead = entries[0]
        return {
            "queued": True, "position": position, "is_head": False,
            "may_integrate": False, "may_merge": False,
            "reason": (f"queued behind #{ahead.pr}, position {position} of "
                       f"{len(order)} — do not rebase, push or restart CI: you "
                       f"would spend a run to learn what this line already says, "
                       f"and invalidate #{ahead.pr}'s checks doing it. Poll this "
                       f"endpoint instead: asking is what keeps your place"),
            "waiting_on": {"pr": ahead.pr, "holder": ahead.holder,
                           "session": ahead.session, "note": ahead.note},
        }
    if at_head is not None and at_head != entry.head_sha:
        return {
            "queued": True, "position": 1, "is_head": True,
            # Integrating stays allowed: it is how a head gets back to ready, and
            # the head has already pushed anyway — this is reporting the state, not
            # granting a new permission.
            "may_integrate": True, "may_merge": False,
            "reason": (f"#{pr} is the head, but it has moved to {at_head[:12]} "
                       f"since it enqueued at {entry.head_sha[:12]}: re-run "
                       f"preland against this head and re-enqueue before merging"),
            "waiting_on": None,
        }
    if not is_ready(entry):
        return {
            "queued": True, "position": 1, "is_head": True,
            "may_integrate": True, "may_merge": False,
            "reason": (f"#{pr} is the head at {entry.head_sha[:12]} with verdict "
                       f"{entry.verdict!r}: integrate with the base if you need to, "
                       f"then re-run preland and re-enqueue as {PROCEEDS!r} — the "
                       f"board holds no ready verdict for this commit"),
            "waiting_on": None,
        }
    return {
        "queued": True, "position": 1, "is_head": True,
        "may_integrate": True, "may_merge": True,
        "reason": (f"#{pr} is the head of the queue and ready at "
                   f"{entry.head_sha[:12]}. Being head is not the claim: take "
                   f"`kind=merge` on this base before you merge"),
        "waiting_on": None,
    }


class EnqueueIn(BaseModel):
    """Join the line, or renew and update the place you already have."""

    repo: str = Field(min_length=1, max_length=256, description="`owner/name`")
    base: str = Field(min_length=1, max_length=256,
                      description="the branch being landed ONTO")
    pr: int = Field(ge=1, description="the pull request number")
    #: The PR's head oid, full length. Required, and the reason it cannot be
    #: optional is :func:`is_fresh`: an entry with no head has a readiness that
    #: never expires.
    head: str = Field(min_length=7, max_length=64)
    #: What preland said about ``head``. Default ``queued`` is the honest one for
    #: an agent that has been refused by this endpoint and is re-registering: it
    #: is admissible, and it does not let anything merge.
    verdict: str = Field(default="queued",
                         description=f"one of: {', '.join(VERDICTS)}")
    ttl: int = Field(default=DEFAULT_TTL, ge=1, le=MAX_TTL)
    session: str | None = Field(default=None, max_length=MAX_SESSION)
    note: str | None = Field(default=None, max_length=500,
                             description="what you are landing — read by everyone behind you")


class LeaveIn(BaseModel):
    repo: str = Field(min_length=1, max_length=256)
    base: str = Field(min_length=1, max_length=256)
    pr: int = Field(ge=1)
    #: WHICH place in the line, when the caller knows — every enqueue response
    #: carries it. A PR number names a pull request, not one of its stays in the
    #: queue, so a leave that arrives after the PR left and re-joined would
    #: otherwise retire the new entry while meaning the old one. The timestamp
    #: guard below catches that only when the two overlap at the server; a leave
    #: delayed in transit gets a fresh timestamp on arrival and is
    #: indistinguishable from a prompt one. This is the exact identification, and
    #: it is optional rather than required so a caller that never held the id
    #: (retiring a peer's abandoned entry) can still stand it down.
    entry_id: uuid.UUID | None = None
    #: Required. The queue advancing is the moment everybody behind starts
    #: spending CI, and "the entry vanished" with no why makes that unauditable —
    #: the same argument the claim table's ``note`` already carries, one step
    #: further because leaving affects other agents rather than just the leaver.
    reason: str = Field(min_length=1, max_length=500,
                        description="merged / closed / superseded / abandoned")


def _admit(verdict: str) -> str:
    """The verdict, or a 422 refusing entry — the gate on the front of the line.

    A PR that is genuinely blocked does not belong in a queue: it would sit at the
    head holding everybody up until its TTL ran out, having never been able to
    land. So preland ``HOLD`` is named and refused rather than folded into
    ``queued``, and an unknown verdict is refused rather than guessed at, for the
    reason :data:`app.models.merge_queue.VERDICTS` gives.
    """
    v = (verdict or "").strip().lower()
    if v in VERDICTS:
        return v
    detail = {
        "error": f"{verdict!r} is not a state that admits a PR to the queue",
        "verdicts": list(VERDICTS),
    }
    if v == _HOLD:
        detail["hint"] = (
            "preland HOLD means something is wrong with the PR itself, not with "
            "its turn. An entry at the head that can never land holds the line "
            "until its TTL expires — fix the objection, then enqueue")
    else:
        detail["hint"] = (
            "`ready` is preland READY, `reconcile` is preland RECONCILE (a stale "
            "base, which landing in turn dissolves), and `queued` means the only "
            "objection is your position in this line")
    raise HTTPException(422, detail=detail)


@router.get("/merge-queue")
async def read_queue(
    repo: str = Query(..., min_length=1, description="`owner/name`"),
    base: str = Query(..., min_length=1, description="the branch being landed onto"),
    pr: int | None = Query(default=None, ge=1,
                           description="also answer `you`: what may THIS pr do right "
                                       "now — and renew that entry, if you hold it"),
    head: str | None = Query(default=None, min_length=7, max_length=64,
                             description="`pr`'s head oid as YOU see it — how a head "
                                         "change invalidates readiness without a write"),
    _: str = Depends(reader),
    caller: str | None = Depends(optional_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The line for one base: who is next, who is waiting, and what each waits on.

    **``active_order`` is the queue. ``suggested_order`` is an opinion about it**
    (#80), and the two never touch: nothing here reorders, and being ranked first
    is not being at the head. The queue is FIFO by arrival and stays that way
    unless a human acts, which is #227's own condition for an ordering proposal
    existing at all — *"agents may propose order; they must not silently rewrite
    the queue while also trying to land."*

    ``suggested_order`` is **null unless the proposal is a permutation of the whole
    queue**. A PR the board holds no changed-file list for cannot be given a
    position without inventing one, so one such PR suppresses the confident field
    entirely rather than being quietly dropped to the bottom — which under
    :mod:`app.ranking`'s cost model is the worst place it could go, since the
    biggest hole (#94's skipped merges and format-the-world commits) is exactly
    the set that should land first. The reasoning still comes back: ``suggestion``
    carries the per-PR evidence, the pairwise collisions, a ``partial_order`` over
    the PRs that could be ranked, and an ``order_trust`` block that says what the
    order is worth — the ``plan_read``/``order_trust`` precedent, and for the same
    reason. An order whose chosen and unchosen parts are indistinguishable gets
    trusted uniformly, and usually too much.

    Both are computed only from :data:`MIN_RANKABLE` live entries up: a queue of
    one has one arrangement, and this endpoint is polled in a loop.

    Expired-but-unswept entries are filtered out on the way past rather than swept,
    so this view and the unique index can briefly disagree about one lapsed row, and
    the reader that matters (an agent deciding whether it may move) gets the truthful
    answer.

    **Asking where you are in the line keeps your place.** Sending ``pr`` renews
    that entry when — and only when — the entry names you as its holder, and the
    answer says so in ``renewal``. This is the one write on a read path in this
    module and :func:`_renew_on_read` argues it: every *other* act that renewed an
    entry is an act the ``reason`` below tells a waiter not to take, so a queue
    whose TTL only noticed writes was retiring the agents that obeyed it (#405).
    Nothing else here mutates: a peer's read, a monitor's poll and a browser's page
    load all renew nothing, and an entry that has already lapsed is not revived.
    """
    canon_repo, canon_base, key = _scope(repo, base)
    now = _utcnow()
    # Before the entries are read, so the view reports the expiry this very call
    # just wrote rather than the one it replaced.
    renewed = (await _renew_on_read(session, canon_repo, canon_base, pr, caller, now)
               if pr is not None else None)
    entries = await _live_entries(session, canon_repo, canon_base, now)
    claim = await live_claim(session, "merge", key, now)
    ranking = None
    if len(entries) >= MIN_RANKABLE:
        candidates, overlaps = await _evidence(session, canon_repo, entries)
        ranking = rank(candidates, overlaps)
    out = {
        "repo": canon_repo,
        "base": canon_base,
        "generated": now.isoformat(),
        # What the LIVE queue is ordered by, and it is not affected by anything
        # below. A caller reading one field to learn what happens next reads this
        # one.
        "ordering": "fifo",
        "active_order": [e.pr for e in entries],
        # A proposal, and only when it covers every queued PR — see the
        # docstring. Null is a real answer here and `suggestion.order_trust`
        # says which of the two reasons produced it.
        "suggested_order": (list(ranking.order)
                            if ranking is not None and ranking.covers_all else None),
        "suggestion": _suggestion(ranking) if ranking is not None else None,
        "note_on_ordering": (
            "active_order is strict FIFO by arrival and nothing here mutates it. "
            "suggested_order is advisory (#80): file overlap between the queued "
            "PRs, weighted by how expensive each is to re-integrate late, with "
            "the axes it does not weigh named in `suggestion.axes_not_weighed`. "
            "It is null while the board cannot answer for every queued PR, "
            "because an order derived from a partial measurement must not be "
            "presented as a confident one"),
        "head": entry_view(entries[0], 1) if entries else None,
        "entries": [entry_view(e, i) for i, e in enumerate(entries, start=1)],
        "claim": _claim_view(claim, key),
        "note_on_claim": (
            "the queue is ordering around the `kind=merge` claim, not a second "
            "lock: being at the head does not hold this claim, and this claim "
            "being held by someone who never enqueued is normal and advisory"),
        "counts": {
            "queued": len(entries),
            "ready": sum(1 for e in entries if is_ready(e)),
            "not_ready": sum(1 for e in entries if not is_ready(e)),
        },
    }
    if pr is not None:
        out["you"] = decide(entries, pr, at_head=_norm_sha(head) if head else None)
        out["renewal"] = _renewal_view(entries, pr, caller, renewed)
    elif head is not None:
        # A head with no PR names nothing. Refused rather than ignored: a caller
        # that believed it was asking "is my entry stale" and silently got the
        # unconditioned answer is the failure this parameter exists to prevent.
        raise HTTPException(422, "`head` says which commit `pr` is on; send `pr` too")
    return out


@router.post("/merge-queue/enqueue")
async def enqueue(
    body: EnqueueIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Take a place in the line, or update the one you have. Idempotent.

    Idempotent in the strong sense: a second call for a PR already queued updates
    that row — head, verdict, note, expiry — and **leaves ``entered_at`` alone**,
    so re-registering never costs a place. An agent that has just been refused
    ("queued behind #123") is meant to call this again on its next poll, and a
    queue that sent it to the back for doing so would be a queue nobody could
    safely poll.

    The board is taking your word for it. It cannot run preland, read CI or ask
    GitHub whether this PR is a draft, so ``verdict`` is testimony — what it adds
    is that the testimony is pinned to ``head`` and stops counting the moment the
    branch moves.
    """
    canon_repo, canon_base, key = _scope(body.repo, body.base)
    head = _norm_sha(body.head)
    verdict = _admit(body.verdict)
    now = _utcnow()
    sess = clean_session(body.session)

    await _sweep_lapsed(session, canon_repo, canon_base, now)
    await session.commit()

    entry = await _join(session, canon_repo, canon_base, body.pr,
                        _row(head=head, verdict=verdict, holder=holder, sess=sess,
                             note=body.note, ttl=body.ttl, now=now), now)

    entries = await _live_entries(session, canon_repo, canon_base, now)
    claim = await live_claim(session, "merge", key, now)
    return {
        "repo": canon_repo,
        "base": canon_base,
        "entry": entry_view(entry, _position(entries, entry.pr)),
        "active_order": [e.pr for e in entries],
        "you": decide(entries, entry.pr),
        "claim": _claim_view(claim, key),
    }


def _row(*, head: str, verdict: str, holder: str, sess: str | None,
         note: str | None, ttl: int, now: datetime) -> dict:
    """The columns an enqueue writes, whether it is inserting or renewing.

    One dict for both paths, so the insert and the update cannot come to mean
    different things — a re-enqueue that set a column the first enqueue did not
    is how "calling this twice is safe" quietly stops being true.

    ``entered_at`` is conspicuously absent: it is the FIFO key, written once by
    the insert and never again. Everything else moves, including ``holder`` — a
    PR handed to another agent mid-land keeps its place, because the place
    belongs to the pull request and not to whoever is driving it this hour.

    ``ready_sha`` follows ``head`` **only** when the call asserts the proceeding
    verdict at that head; every other verdict clears it. So reporting a new head
    without re-running preland invalidates the readiness rather than carrying it
    forward onto a commit nobody checked, which is #227's "when a queued PR's head
    changes, its readiness is invalidated until preland is rerun against that
    head" — and it holds whether the head moved by one commit or by fifty. The
    check constraint says the same thing from the other side, so the two cannot
    drift.

    ``session`` and ``note`` are written only when sent, because a poll that
    omits them means "unchanged", not "cleared" — and clearing the note would
    blank the one line everyone queued behind this entry is reading.
    """
    values: dict = {
        "head_sha": head,
        "verdict": verdict,
        "ready_sha": head if verdict == PROCEEDS else None,
        "holder": holder,
        "ttl_seconds": ttl,
        "expires_at": now + timedelta(seconds=ttl),
        "updated_at": now,
    }
    if sess:
        values["session"] = sess
    if note is not None:
        values["note"] = note
    return values


async def _join(session: AsyncSession, repo: str, base: str, pr: int,
                values: dict, now: datetime) -> MergeQueueEntry:
    """Take or renew this PR's place. Decided by the database, never by looking first.

    Two attempts, and the second is not a retry-and-hope: each attempt can lose in
    exactly one way, and the loss says which branch to take next.

    * The INSERT loses to ``ix_merge_queue_open`` when somebody enqueued this PR
      in the gap after the SELECT. Their row is the real one, so the next pass
      renews it — this endpoint's whole contract is that calling it twice is safe.
    * The UPDATE is conditional and can lose two ways. ``left_at IS NULL`` loses
      when a concurrent ``leave`` or sweep retired the row in that same gap —
      read-then-write would have stamped a fresh expiry onto a departed entry,
      which is worse than failing: it resurrects nothing while reporting success,
      and the response then cannot find its own entry in the live queue.
      ``updated_at <= now`` loses to a *newer* enqueue that overtook this one,
      and there the newer row is simply the answer: returning it is truthful,
      where overwriting it would put a stale ``ready`` verdict back onto a commit
      the PR has moved off.

    So each loss names the next step, and two passes is exactly enough. A third
    would mean two writers are trading the row back and forth, which they should
    not be: one PR is driven by one agent, and the only contention expected here
    is that agent's own overlapping polls.
    """
    for _ in range(2):
        existing = await session.scalar(
            select(MergeQueueEntry).where(
                MergeQueueEntry.repo == repo, MergeQueueEntry.base == base,
                MergeQueueEntry.pr == pr, MergeQueueEntry.left_at.is_(None))
        )
        if existing is None:
            # Did this PR leave AFTER this request started? Then this request is
            # an obsolete poll — in flight when somebody stood the entry down —
            # and inserting would resurrect a PR that has merged, at the back of
            # a line it has no business being in. It would expire on its own, but
            # a stale record of a claim nobody is making is worse than none: it
            # is a second answer to a question that already has one.
            #
            # A leave that happened BEFORE this request is the ordinary re-join,
            # and goes to the back exactly as it should. The comparison is what
            # separates the two, and both timestamps come from one server clock.
            departed = await session.scalar(
                select(MergeQueueEntry)
                .where(MergeQueueEntry.repo == repo, MergeQueueEntry.base == base,
                       MergeQueueEntry.pr == pr, MergeQueueEntry.left_at > now)
                .order_by(MergeQueueEntry.left_at.desc()).limit(1)
            )
            if departed is not None:
                raise HTTPException(409, detail={
                    "error": f"#{pr} left the queue after this request started",
                    "repo": repo, "base": base, "pr": pr,
                    "left_at": departed.left_at.isoformat(),
                    "left_by": departed.left_by,
                    "left_reason": departed.left_reason,
                    "hint": "your request was in flight when the entry was stood "
                            "down. If this PR really is still landing, enqueue "
                            "again — it will join at the back, which is where a "
                            "PR that left the line belongs",
                })
            entry = MergeQueueEntry(repo=repo, base=base, pr=pr,
                                    entered_at=now, **values)
            session.add(entry)
            try:
                await session.commit()
            except IntegrityError as e:
                await session.rollback()
                # Only the idempotency index. Any other integrity failure — a
                # check constraint, say — is a real fault, and reporting it as
                # "you were already queued" would send the caller looking at a row
                # that does not exist.
                if not is_unique_violation(e):
                    raise
                continue
            await session.refresh(entry)
            return entry
        existing_id = existing.id
        done = await session.execute(
            update(MergeQueueEntry)
            .where(MergeQueueEntry.id == existing_id,
                   MergeQueueEntry.left_at.is_(None),
                   # Monotonic. Two enqueues for one PR can be in flight at once
                   # — an agent that pushed and re-registered while its previous
                   # poll was still on the wire — and last-writer-wins would let
                   # the older one land second and put a `ready` verdict back on
                   # a commit the PR has moved off. That is precisely the stale
                   # green light this whole feature exists to remove, so the
                   # older request loses at the database rather than by arriving
                   # first. All requests are stamped by one server clock, so
                   # there is no skew to compare across.
                   MergeQueueEntry.updated_at <= now)
            .values(**values)
            .returning(MergeQueueEntry.id)
        )
        if done.scalar_one_or_none() is None:
            await session.rollback()
            # Which guard refused it? The answers are different, so the test is
            # the guard's own predicate rather than "is the row still there" —
            # otherwise a row that merely lost a lap to contention would be
            # reported as somebody else's newer state.
            current = await session.get(MergeQueueEntry, existing_id)
            if (current is not None and current.left_at is None
                    and current.updated_at > now):
                # A NEWER enqueue won. Its answer is the true one: return it
                # rather than clobbering it, and rather than refusing a caller
                # whose PR is correctly registered — by somebody holding fresher
                # information about it, which is usually itself.
                return current
            # Retired in the gap, or simply contended. Either way the next pass
            # re-reads and decides again: if it left, coming back is a new
            # arrival, which is honest.
            continue
        await session.commit()
        fresh = await session.get(MergeQueueEntry, existing_id)
        if fresh is not None:
            return fresh
    raise HTTPException(409, detail={
        "error": "queue entry contended; try again",
        "repo": repo, "base": base, "pr": pr,
        "hint": "this PR was entering and leaving the queue at the same moment. "
                "One PR is driven by one agent, so if this repeats, two of your "
                "sessions are landing the same branch",
    })


@router.post("/merge-queue/leave")
async def leave_queue(
    body: LeaveIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Stand down, and let everybody behind you move. Idempotent.

    **Any authenticated agent may retire any entry, and that is deliberate.** The
    case this endpoint exists for is a head that merged, closed or was abandoned,
    and in every one of those the agent best placed to notice is somebody else —
    the one sitting at position 2 watching a PR that is already merged hold the
    line. Restricting it to the owner would leave the TTL as the only way out of
    exactly the situation the TTL is the crude fallback for.

    What guards it is the record rather than a refusal: ``left_by`` and
    ``left_reason`` are both stored, ``reason`` is required, and the row is never
    deleted — so an entry retired out from under its owner is visible as such
    afterwards. That is the trade the claim table makes with ``note``, taken one
    step further because leaving affects other agents and not only the leaver.

    Send ``entry_id`` when you have one — the enqueue that put you in the line
    returned it. A PR number names a pull request rather than one of its stays in
    the queue, and the difference matters exactly once: when the PR left and
    re-joined between your decision to stand down and this call arriving.

    It does **not** touch the ``kind=merge`` claim. An agent that held one releases
    it through ``POST /claim/release``; a queue that released claims on its own
    would be the second implementation of the claim this module exists not to be.
    """
    canon_repo, canon_base, key = _scope(body.repo, body.base)
    now = _utcnow()
    await _sweep_lapsed(session, canon_repo, canon_base, now)
    await session.commit()

    # Conditional UPDATE, not read-then-write: a concurrent sweep can retire this
    # row between a read and a write, and re-stamping it would overwrite `lapsed`
    # — turning "its holder stopped answering" into "it stood down cleanly",
    # which is the one distinction the column exists to keep.
    # The entry this leave is ABOUT, not merely one carrying the same PR number.
    # A PR that left, was reworked and re-enqueued must not be dropped back out
    # of the line by the tidy-up for its predecessor — the one failure a queue
    # cannot have. `entry_id` is the exact answer when the caller has one, and
    # `entered_at <= now` is the fallback: it separates the two incarnations
    # whenever they overlap at the server, which is the case a caller that never
    # held an id can actually be in.
    scoped = ((MergeQueueEntry.id == body.entry_id,) if body.entry_id is not None
              else (MergeQueueEntry.entered_at <= now,))
    left = await session.execute(
        update(MergeQueueEntry)
        .where(MergeQueueEntry.repo == canon_repo, MergeQueueEntry.base == canon_base,
               MergeQueueEntry.pr == body.pr, MergeQueueEntry.left_at.is_(None),
               *scoped)
        .values(left_at=now, left_by=holder, left_reason=body.reason.strip(),
                updated_at=now)
        .returning(MergeQueueEntry.id)
    )
    entry_id: uuid.UUID | None = left.scalar_one_or_none()
    await session.commit()

    entries = await _live_entries(session, canon_repo, canon_base, now)
    claim = await live_claim(session, "merge", key, now)
    return {
        "repo": canon_repo,
        "base": canon_base,
        "pr": body.pr,
        # False when the entry was already gone — swept, stood down by a peer,
        # or replaced by a later arrival this leave is not about. Not a 404: "the
        # entry you meant is not in the queue" is the state the caller wanted,
        # and an agent tidying up after a merge should not have to care whether
        # the TTL beat it to it.
        "left": entry_id is not None,
        "entry_id": str(entry_id) if entry_id is not None else None,
        "reason": body.reason.strip(),
        "active_order": [e.pr for e in entries],
        "head": entry_view(entries[0], 1) if entries else None,
        "claim": _claim_view(claim, key),
    }
