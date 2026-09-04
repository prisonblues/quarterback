from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal, get_args

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.claims import release_session_claims
from app.api.subagents import active_subagents_by_session
from app.auth import author, identify, reader
from app.db import get_session
from app.identity import is_human, retire, same_machine
from app.models.blob import Blob
from app.models.lease import Lease
from app.models.post import Post
from app.models.session import SessionRecord
from app.repomatch import fold_repo
from app.schemas import CWD_MAX

router = APIRouter(tags=["lease"])


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _lease_view(lease: Lease) -> dict:
    return {
        "lease_id": str(lease.id),
        "session": lease.session,
        "device": lease.device,
        "holder": lease.holder,
        "expires": lease.expires_at.isoformat(),
        # The two fields that separate "this session ended" from "this lease ran
        # out". Always present, so a reader never has to infer the difference
        # from which keys happen to be there: `released` with no `end_reason` is
        # a handoff, both set is a reported ending, and neither set on a lease
        # whose `expires` has passed is the one case where the board genuinely
        # does not know what happened.
        "released": lease.released_at.isoformat() if lease.released_at else None,
        "end_reason": lease.end_reason,
    }


#: Why a session stopped — the whole vocabulary, and it is closed on purpose.
#:
#: Free text here would be the same mistake as a free-text ``state``: this is
#: read as a word in a fleet view and branched on by a dashboard, so a sixth
#: spelling of "finished" reaches a human as an unknown and reaches a reader as a
#: case it has no branch for. Five values, each naming a DIFFERENT observer:
#:
#: * ``finished`` — the session itself said so (its SessionEnd hook fired).
#: * ``killed`` — something closed it from outside: a ✕ on the seat bar, a human.
#: * ``timed_out`` — a bounded run hit its ceiling (``run_agent``'s timeout).
#: * ``context_reset`` — ``/clear`` or ``/new``: the pane lives on, this
#:   conversation does not. #263 is what happens when that is not said out loud.
#: * ``superseded`` — another session took the work over.
#:
#: Deliberately NOT here: ``crashed`` and ``stalled``. Both are conclusions a
#: reader draws from silence, never reports somebody makes about themselves —
#: the same rule ``LeaseIn.state`` states for ``stalled``. A crashed session
#: reports nothing at all, and a lease with no reason on it IS that report.
EndReason = Literal["finished", "killed", "timed_out", "context_reset", "superseded"]

#: The same five as a tuple, DERIVED from the type rather than written twice — a
#: second listing is a second answer to "which reasons exist", and it disagrees
#: with the first the day somebody adds a sixth to one of them.
END_REASONS: tuple[str, ...] = get_args(EndReason)

#: The SHAPE of a workflow stage, and deliberately nothing about its vocabulary.
#:
#: ``F0``, ``R1``, ``R1F``, ``R2`` … is a convention between the skills and the
#: reader, not a closed set like :data:`END_REASONS`, and the difference is which
#: way the two failure modes cost. A closed set has to be edited by anyone adding
#: a stage — a skill inventing ``R4F`` would need a server release — while an
#: unknown-but-well-formed token renders as six harmless characters in a column
#: and a rejected one stops a workflow to argue about a cosmetic field. So this
#: defends the pixels and nothing else: something that fits a narrow column and
#: cannot smuggle markup, control characters or a paragraph into a fleet view.
#:
#: **Byte-identical to the check ``harness/bin/qb-stage`` makes** before it
#: writes its marker, and ``tests/test_lease_stage.py`` holds the two together.
#: A board that accepted what the producer refuses (or the reverse) would put the
#: local status bar and the fleet view into disagreement about the same session,
#: which is the gap #262 exists to close.
STAGE_RE = r"^[A-Za-z0-9]{1,6}$"


async def _active_lease(session: AsyncSession, sess_key: str, now: datetime) -> Lease | None:
    """The single active lease on a session, or None. Active = unreleased and unexpired."""
    stmt = (
        select(Lease)
        .where(
            Lease.session == sess_key,
            Lease.released_at.is_(None),
            Lease.expires_at > now,
        )
        .order_by(Lease.expires_at.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def _newest_lease(session: AsyncSession, sess_key: str) -> Lease | None:
    """The most recently acquired lease on this key, whatever became of it.

    Newest by ``acquired_at`` rather than by expiry, because a key can be leased
    again: an ending belongs to a lease, and the one that matters is the last one
    taken. Anything older has been superseded and is history.
    """
    return await session.scalar(
        select(Lease).where(Lease.session == sess_key)
        .order_by(Lease.acquired_at.desc()).limit(1)
    )


async def _retire_if_idle(session: AsyncSession, holder: str, now: datetime) -> None:
    """Free ``holder``'s shortname once its last live lease is gone.

    An agent can hold several sessions at once, and ending one is not the end of
    it. Retiring on the first release would hand its name away mid-life — the
    rename this whole design exists to avoid — and split the rest of its work
    across two identities.
    """
    still_working = await session.scalar(
        select(Lease.id)
        .where(Lease.holder == holder, Lease.released_at.is_(None), Lease.expires_at > now)
        .limit(1)
    )
    if still_working is None:
        await retire(session, holder)


async def _record_blob(
    session: AsyncSession, sess_key: str, blob_sha: str,
    holder: str, fields: dict, now: datetime,
) -> None:
    """Upsert the durable sessions pointer (shared by /handoff and /snapshot).

    ``fields`` carries optional metadata (device/cwd/title/recap); a None value is
    inserted but never overwrites an existing value on conflict.
    """
    base = {"latest_blob": blob_sha, "holder": holder, "updated_at": now}
    set_ = {**base, **{k: v for k, v in fields.items() if v is not None}}
    await session.execute(
        pg_insert(SessionRecord)
        .values(session=sess_key, **base, **fields)
        .on_conflict_do_update(index_elements=[SessionRecord.session], set_=set_)
    )


class LeaseIn(BaseModel):
    session: str = Field(min_length=1)
    device: str = Field(min_length=1)
    ttl: int = Field(default=300, ge=1, le=86400)
    cwd: str | None = Field(default=None, max_length=CWD_MAX)  # project dir (peer `--resume`)
    #: The repository this session is standing in — ``owner/name`` off the origin
    #: remote, or the bare repository name from a checkout that has no GitHub one
    #: (and from every lifecycle hook older than #714). Both shapes are matched by
    #: repository name at the reads; see :mod:`app.repomatch` for why the column is
    #: the one repo field on this board that is not a key.
    repo: str | None = None
    branch: str | None = None   # git branch (finer overlap signal)
    title: str | None = None    # CC ai-title
    recap: str | None = None    # compact-summary head / last prompt
    model: str | None = None    # model id from last assistant msg
    #: working | waiting | input — what the holder is doing, for a human reading
    #: a wall of panes. Constrained rather than free text because it is rendered
    #: as a word in a footer and a colour in a dashboard: an unknown value would
    #: reach a human as a blank or a crash, and there is no reader that can do
    #: anything useful with a fourth spelling.
    #:
    #: `stalled` is not accepted. Nobody reports being stalled — that is the
    #: reader's conclusion from `state_at` — and accepting it would let a holder
    #: assert a state it cannot know it is in.
    state: Literal["working", "waiting", "input"] | None = None

    @field_validator("repo")
    @classmethod
    def _fold_repo(cls, value: str | None) -> str | None:
        """One repository, one stored spelling — #326's rule, on the reporting column.

        A qualified ``owner/name`` is folded; a bare name (or a blank, which becomes
        NULL) passes through. :func:`app.repomatch.fold_repo` carries the argument
        for why this is a fold and not a refusal: the alternative to accepting the
        un-qualified half is taking a heartbeat's whole lease away over a field that
        is optional in the first place.
        """
        return fold_repo(value)


class StageIn(BaseModel):
    """``qb-stage``'s report: this session has got to here.

    Keyed by ``session`` and not by ``lease_id`` because the caller is a one-line
    shell script that knows ``$CLAUDE_CODE_SESSION_ID`` and nothing else. Making
    it look a lease id up first would be a second round trip on a fail-open path,
    for an identifier it would then have to keep in step with a lease it can be
    re-granted at any time.

    ``stage`` absent or null is *clear it* — the shape ``qb-stage --clear`` and
    ``/drop-worktree`` need, because a lease still advertising ``R2`` after the
    work landed is worse than one that says nothing.
    """

    session: str = Field(min_length=1)
    stage: str | None = Field(
        default=None,
        pattern=STAGE_RE,
        description="1-6 alphanumerics (F0, R1, R1F, R2 …); null clears it",
    )


class RenewIn(BaseModel):
    lease_id: uuid.UUID


class ReleaseIn(BaseModel):
    lease_id: uuid.UUID


class HandoffIn(BaseModel):
    session: str = Field(min_length=1)
    blob: str = Field(min_length=1, description="sha of the JSONL blob already PUT to /blob")
    cwd: str | None = Field(default=None, max_length=CWD_MAX)
    title: str | None = None
    recap: str | None = None
    model: str | None = None


class SnapshotIn(BaseModel):
    """Update a live session's latest blob WITHOUT releasing the lease — the
    mid-session freshness path (Stop hook), so a peer can pull a current
    transcript. Contrast /handoff, which also releases."""
    session: str = Field(min_length=1)
    blob: str = Field(min_length=1, description="sha of the JSONL blob already PUT to /blob")
    cwd: str | None = Field(default=None, max_length=CWD_MAX)
    title: str | None = None
    recap: str | None = None
    model: str | None = None


class EndIn(BaseModel):
    """``end(session, reason)`` — the issue's signature, and no third argument.

    There is no ``keep_lease`` knob and no way to end a session without saying
    why. A caller that could end a session and leave its lease standing would be
    able to tell the board two contradictory things in one call, and a default
    reason would let the commonest ending arrive unlabelled — which is today's
    behaviour, and the thing being fixed.
    """

    session: str = Field(min_length=1)
    reason: EndReason


@router.post("/lease")
async def acquire_lease(
    body: LeaseIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Claim a session, or renew if you already hold it.

    409 if a *different* device holds an active lease — that device must crash
    (lease lapses) or hand off before this one can take over. "Different" is
    judged at machine granularity: a lease belongs to the box, so a session
    reclaimed by another agent on the same machine is a renew, not a conflict.
    """
    now = _utcnow()
    active = await _active_lease(session, body.session, now)
    if active is not None and (
        not same_machine(active.holder, holder) or active.device != body.device
    ):
        raise HTTPException(
            409,
            detail={
                "error": "session is leased by another device",
                "held_by": active.holder,
                "device": active.device,
                "expires": active.expires_at.isoformat(),
            },
        )

    if active is not None:
        # Same device re-claiming — treat as a renew. Take the caller's identity:
        # a lease claimed before the holder had an instance (or by the machine
        # itself) upgrades to the live agent's address on the next heartbeat.
        active.holder = holder
        active.ttl_seconds = body.ttl
        active.expires_at = now + timedelta(seconds=body.ttl)
        # Every reported field below is STICKY: a renewal that omits one, or sends
        # it blank, leaves the stored value alone. Blank-to-NULL is only exercised
        # on creation, so a session that loses or changes its origin remote keeps
        # the old repository for the life of its lease (#721) — and the hook omits
        # the field when it derives nothing, so there is currently no spelling of
        # the request that clears it. It self-heals within the TTL and the same is
        # true of `cwd`, `branch`, `title`, `recap` and `model`, which is why this
        # is a note and not a patch: "clear on blank" is one rule for six columns
        # and one wire contract, and changing it under a repo fix would be changing
        # what an omitted field means for every caller on the fleet.
        if body.cwd:
            active.cwd = body.cwd
        if body.repo:
            active.repo = body.repo
        if body.branch:
            active.branch = body.branch
        if body.title:
            active.title = body.title
        if body.recap:
            active.recap = body.recap
        if body.model:
            active.model = body.model
        if body.state:
            active.state = body.state
            active.state_at = now
        await session.commit()
        return {**_lease_view(active), "renewed": True}

    lease = Lease(
        session=body.session,
        device=body.device,
        holder=holder,
        ttl_seconds=body.ttl,
        expires_at=now + timedelta(seconds=body.ttl),
        cwd=body.cwd,
        repo=body.repo,
        branch=body.branch,
        title=body.title,
        recap=body.recap,
        model=body.model,
        state=body.state,
        state_at=now if body.state else None,
    )
    session.add(lease)
    await session.commit()
    await session.refresh(lease)
    return {**_lease_view(lease), "renewed": False}


@router.post("/lease/renew")
async def renew_lease(
    body: RenewIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    lease = await session.get(Lease, body.lease_id)
    if lease is None:
        raise HTTPException(404, "lease not found")
    if not same_machine(lease.holder, holder):
        raise HTTPException(403, "not your lease")
    if lease.released_at is not None:
        raise HTTPException(409, "lease already released; re-acquire via POST /lease")
    now = _utcnow()
    if lease.expires_at <= now:
        raise HTTPException(409, "lease expired; re-acquire via POST /lease")
    lease.expires_at = now + timedelta(seconds=lease.ttl_seconds)
    await session.commit()
    return _lease_view(lease)


@router.post("/lease/stage")
async def report_stage(
    body: StageIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Where this session has got to — ``F0``, ``R1``, ``R1F``, ``R2`` … (#262).

    Its own endpoint rather than a field on ``POST /lease``, because the two are
    reported by different things at different times. The lifecycle hook heartbeats
    the lease and has never been told a stage; ``qb-stage`` is told one and is not
    a heartbeat. A stage riding the heartbeat would have to be *re-sent* on every
    beat by a caller that does not know it, and the first beat that forgot would
    look exactly like a clear.

    **The post is written here, not by the caller.** ``qb-stage`` fails open and
    must stay one non-blocking call, so a second request to ``POST /post`` is a
    second thing to get wrong on a path that is allowed to be dropped entirely.
    And "on change" is a comparison only the board can make: the caller's marker
    is its own machine's, and a session re-leased on another box has no marker at
    all. So this compares against the stored value and emits exactly when it
    moves — which is what keeps a low-volume, high-signal event low-volume when a
    skill re-asserts the same stage twice.

    404 when nothing holds this session. That is a real answer and not a shrug:
    a stage belongs to a live lease, and there is nowhere to put one otherwise.
    The caller treats it, and every other failure, as nothing — see ``qb-stage``.
    """
    now = _utcnow()
    lease = await _active_lease(session, body.session, now)
    if lease is None:
        raise HTTPException(404, "no active lease on this session")
    if not same_machine(lease.holder, holder):
        raise HTTPException(403, "not your lease")
    if lease.stage == body.stage:
        return {"session": body.session, "stage": lease.stage, "changed": False}

    lease.stage = body.stage
    # Where, as well as what. A stage on its own is six characters with no subject
    # — `R2` says nothing to a follower who cannot see which of eight sessions
    # moved — and repo/branch are what the rest of the board threads on.
    where = " · ".join(filter(None, (lease.repo, lease.branch)))
    summary = f"stage {body.stage}" if body.stage else "stage cleared"
    # The same two facts as structured context, so the board can group a stage
    # post with the rest of that branch's traffic rather than only print it. A
    # `repo` ref names the repo in `value`, so it carries no `repo` key of its
    # own; a `branch` ref does, because a branch name means nothing without one.
    refs: list[dict[str, str]] = []
    if lease.repo:
        refs.append({"kind": "repo", "value": lease.repo})
    if lease.branch:
        refs.append({"kind": "branch", "value": lease.branch,
                     **({"repo": lease.repo} if lease.repo else {})})
    session.add(
        Post(
            author=holder,
            session=body.session,
            # `status`, so it rides the default board read. `presence` is muted as
            # volume and this is the opposite of volume: a handful per session per
            # day, against the heartbeats that stream already carries, and the one
            # event that tells a peer whether the round it was about to start is
            # already running.
            type="status",
            summary=f"{summary} · {where}" if where else summary,
            refs=refs or None,
        )
    )
    await session.commit()
    return {"session": body.session, "stage": lease.stage, "changed": True}


@router.post("/lease/release")
async def release_lease(
    body: ReleaseIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    lease = await session.get(Lease, body.lease_id)
    if lease is None:
        raise HTTPException(404, "lease not found")
    if not same_machine(lease.holder, holder):
        raise HTTPException(403, "not your lease")
    if lease.released_at is None:
        now = _utcnow()
        lease.released_at = now
        # Releasing is SessionEnd: that agent is going. Free *its* shortname —
        # the holder's, not the caller's, since a co-tenant may release on its
        # behalf — keeping the name on everything it authored (identity.retire),
        # so the live space recycles without rewriting the past.
        #
        # Only while the lease is still live. `holder` is a name, and names
        # recycle, so a belated release of a lease that lapsed weeks ago would
        # otherwise unname whichever agent inherited it since. A lapsed lease
        # already gave up its claim; there is nothing left here to retire.
        if lease.expires_at > now:
            await _retire_if_idle(session, lease.holder, now)
        await session.commit()
    return {"lease_id": str(lease.id), "released": True}


@router.post("/session/end")
async def end_session(
    body: EndIn,
    ender: str = Depends(author),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """End a session: release its lease and its claims, and record WHY (#277).

    The stop half of a session's lifecycle, and until now the fleet had no verb
    for it at all — three ways to start a session and not one to end one. What
    stood in for it was expiry, and expiry is a floor rather than a report: an
    expired lease says *nobody renewed*, which is the same row whether the work
    finished, the pane was closed, or the agent is thinking hard (#252).

    Three things happen here and they belong in one call, because a caller that
    had to make three could make two:

    * every live claim the session took is released, at the moment it ends,
      rather than left to lapse an hour later (#263);
    * the lease is released with ``end_reason`` on it, so an ended session and a
      lapsed one stop looking alike;
    * the agent's shortname is retired if this was its last live session, the
      same rule ``/lease/release`` and ``/handoff`` already apply.

    **It does not operate anything.** Nothing is signalled, no process is
    touched, no pane is closed. This records that a session has stopped; whatever
    stopped it is what calls this, and on a machine that is ``qb-hook``'s
    SessionEnd or the seat bar's ✕ — the board still only coordinates.

    **Idempotent, and honest about what it found.** Ending a session that already
    ended is a fine answer, not a 409: the two callers most likely to race here
    are a hook and a human clicking ✕, and the second must not fail because the
    first won. ``ended`` says whether THIS call was the one that released a live
    lease; ``lease_was`` says what it found.

    **Authorised by machine for an agent**, as every other path on this table is
    (:func:`app.identity.same_machine`) — a session's lease belongs to the box,
    so a co-tenant may end it on its behalf, which is exactly what the seat bar
    does when it closes somebody else's pane. The CLAIMS are narrower: they go
    through the claims table's own ownership rule, so ending one agent's session
    can never release a co-tenant's work.

    **And by the edge for a person** (#378). This is the one verb the fleet page
    carries, because it is the one somebody actually needs from a phone when an
    agent has gone wrong, and until now no browser could reach it: the dependency
    was :func:`app.auth.identify`, which wants a bearer token no browser holds.
    :func:`app.auth.author` is the door both callers already come through
    elsewhere — an agent by token, a person by an edge-proved ``Remote-User``
    with the secret only the proxy knows — and it authors them into namespaces
    that cannot be confused, so nothing here has to trust a header.

    The machine check is skipped for a person and only for a person, because the
    question it asks has no answer for one. It is not a widening of who may end
    what: an agent's reach is unchanged, and a person's is the whole fleet by
    construction, being the person whose fleet it is.
    """
    now = _utcnow()
    active = await _active_lease(session, body.session, now)
    # A PERSON is not on the fleet's machine table, and the check that asks which
    # box they are would refuse every session on it (#378). The machine rule is
    # about machines: it stops zeus ending a session running on the laptop,
    # because only the box a session runs on can see what closing it does. A
    # person proved at the edge is not another box — they are whoever owns all of
    # them, holding the credential that already reorders the plan, and a phone is
    # the one place from which a stuck agent gets noticed at all.
    by_person = is_human(ender)
    if active is not None and not by_person and not same_machine(active.holder, ender):
        raise HTTPException(403, detail={
            "error": "that session is leased by another machine",
            "held_by": active.holder,
            "hint": "a session is ended by the box it runs on, or by whatever is "
                    "closing it there — not from across the fleet",
        })

    released, refused = await release_session_claims(
        session, sess_key=body.session, holder=ender, now=now)

    won = False
    stamped = active          # the lease this call reports on, live or lapsed
    was = None                # what it was already doing, decided before we touch it
    if active is None:
        # **A lease that merely LAPSED can still be told what happened to it**
        # (#378), and until now it could not. `/session/end` stamped the reason
        # onto an *active* lease and did nothing at all otherwise — so the one
        # case a person opens the fleet page for, an agent that went quiet
        # twenty minutes ago and never came back, was exactly the case the verb
        # could not record. The row stayed "nobody ever said", permanently,
        # because the only window in which anything could be said had closed.
        #
        # That is not a widening of what an ending means. #277's rule is that an
        # ending is a REPORT by an observer, and a person looking at a dead pane
        # is an observer; what expiry gives you is the absence of one. A lease
        # already released is left alone, because a handoff is not an ending and
        # an ending already recorded belongs to whoever saw it first.
        lapsed = await _newest_lease(session, body.session)
        # `expires_at <= now` as well as unreleased, because the read above and
        # this one are two statements and a session can be RESUMED between them:
        # `_active_lease` finds nothing, somebody POSTs /lease, and the newest
        # lease is now a live one. Without this it would be stamped ended — a
        # working session killed, with a `released_at` in the future — which is
        # the one outcome this whole path exists to avoid causing.
        if (lapsed is not None and lapsed.released_at is None
                and lapsed.expires_at <= now):
            if not by_person and not same_machine(lapsed.holder, ender):
                # The machine rule reaches here too, now that reaching here
                # WRITES something. Before this it was a no-op and needed no
                # authority; a record of who stopped what does.
                raise HTTPException(403, detail={
                    "error": "that session was leased by another machine",
                    "held_by": lapsed.holder,
                    "hint": "its lease has lapsed, but saying what happened to it "
                            "is still the box's call, or a person's",
                })
            done = await session.execute(
                update(Lease)
                # Both halves again, evaluated by the DATABASE at write time —
                # the Python check above is one statement earlier and the resume
                # can land in between it and this.
                .where(Lease.id == lapsed.id, Lease.released_at.is_(None),
                       Lease.expires_at <= now)
                # `expires_at`, not `now`: this is when the lease stopped being
                # valid, which is the closest the board can get to when the
                # session stopped. Stamping `now` on a lease that lapsed on
                # Tuesday would have `GET /sessions` report it as ending the
                # moment somebody got round to saying so.
                .values(released_at=lapsed.expires_at, end_reason=body.reason)
                .returning(Lease.id)
            )
            won = done.scalar_one_or_none() is not None
            # Deliberately NO `_retire_if_idle` here, and `/lease/release` states
            # the reason for the same case: `holder` is a name, names recycle,
            # and retiring against a lease that lapsed weeks ago would unname
            # whichever agent has inherited it since.
            await session.refresh(lapsed)
            if won:
                # `lapsed`, not "released": `lease_was` says what this call FOUND,
                # and what it found was a lease nobody renewed. Reporting
                # "released" would describe the write rather than the state, and
                # a caller reading it would think a live session had been let go.
                stamped, was = lapsed, "lapsed"
    else:
        # Conditional UPDATE, not read-then-write — the rule `renew_claim` states
        # for the same reason. The two callers most likely to be here at once are
        # a hook and a human on the ✕, and read-then-write would let both set
        # `end_reason`, both report `ended: true`, and the recorded reason be
        # whichever committed last. The observer that actually saw the ending
        # should be the one on the record, so the predicate is evaluated by the
        # database at write time and the loser is told it lost.
        done = await session.execute(
            update(Lease)
            .where(Lease.id == active.id,
                   Lease.released_at.is_(None),
                   Lease.expires_at > now)
            .values(released_at=now, end_reason=body.reason)
            .returning(Lease.id)
        )
        won = done.scalar_one_or_none() is not None
        if won:
            # Ending is what release and handoff already treat as "that agent is
            # going": free its shortname, but only if this was its last live
            # session. `_retire_if_idle` reads the leases table, and the UPDATE
            # above has already landed in this transaction, so this lease is
            # excluded from "still working".
            await _retire_if_idle(session, active.holder, now)
        # Whatever happened, the in-memory row is now behind the database.
        await session.refresh(active)

    lease_was = was or ("released" if won else await _lease_was(session, body.session))
    await session.commit()
    out = {
        "session": body.session,
        "reason": body.reason,
        # True only when this call is the one that ended a live session. A second
        # ender gets `false` and the same released claims, which is the truthful
        # answer to "did I do it" rather than an error that says nothing.
        "ended": won,
        "lease": _lease_view(stamped) if stamped is not None else None,
        "lease_was": lease_was,
        "released_claims": released,
        "ended_at": now.isoformat(),
    }
    if refused:
        # Reported, never swallowed. A claim stamped with this session but held
        # by another machine is a state nothing here can produce, so if one turns
        # up the caller needs to see it rather than read `released_claims` as
        # "everything is let go".
        out["refused_claims"] = refused
    return out


async def _lease_was(session: AsyncSession, sess_key: str) -> str:
    """What the session's lease was already doing when nothing live was found.

    Three different histories reach this line and a caller deciding whether to
    worry needs to tell them apart: an ending that already happened, a lease that
    ran out with nobody reporting anything (#252's shape), and a session that
    never held one at all.

    No ``now``, unlike every other helper here: :func:`_active_lease` has already
    established there is no unreleased, unexpired lease on this key, so the newest
    row either was released or it lapsed, and there is no third case left to
    date-compare for.
    """
    last = await _newest_lease(session, sess_key)
    if last is None:
        return "never leased"
    if last.released_at is None:
        return "lapsed"
    return "already ended" if last.end_reason else "already released"


@router.post("/handoff")
async def handoff(
    body: HandoffIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record the session's latest JSONL blob and release your lease.

    Requires that you hold the active lease and that the blob has already been
    PUT — the sessions row is the durable pointer a peer pulls after claiming.
    """
    now = _utcnow()
    active = await _active_lease(session, body.session, now)
    if active is None or not same_machine(active.holder, holder):
        raise HTTPException(409, "you do not hold an active lease on this session")
    if await session.get(Blob, body.blob.lower()) is None:
        raise HTTPException(400, "unknown blob; PUT it to /blob/<sha> first")

    await _record_blob(session, body.session, body.blob.lower(), holder, {
        "device": active.device,
        "cwd": body.cwd or active.cwd,
        "title": body.title or active.title,
        "recap": body.recap or active.recap,
        "model": body.model or active.model,
    }, now)
    active.released_at = now
    await _retire_if_idle(session, active.holder, now)  # handoff releases — that agent is done
    await session.commit()
    return {
        "session": body.session,
        "latest_blob": body.blob.lower(),
        "released_lease": str(active.id),
    }


@router.post("/snapshot")
async def snapshot(
    body: SnapshotIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Update a live session's latest blob without releasing the lease.

    The mid-session freshness path (Stop hook): a peer can pull a current
    transcript. Requires you hold the active lease and the blob is already PUT.
    """
    now = _utcnow()
    active = await _active_lease(session, body.session, now)
    if active is None or not same_machine(active.holder, holder):
        raise HTTPException(409, "you do not hold an active lease on this session")
    if await session.get(Blob, body.blob.lower()) is None:
        raise HTTPException(400, "unknown blob; PUT it to /blob/<sha> first")
    await _record_blob(session, body.session, body.blob.lower(), holder, {
        "device": active.device,
        "cwd": body.cwd or active.cwd,
        "title": body.title or active.title,
        "recap": body.recap or active.recap,
        "model": body.model or active.model,
    }, now)
    await session.commit()
    return {"session": body.session, "latest_blob": body.blob.lower()}


async def _last_leases(session: AsyncSession, keys: list[str]) -> dict[str, Lease]:
    """The newest lease per key, or nothing for a key that never held one.

    Newest by ``acquired_at``, the rule :func:`_newest_lease` states for one key:
    a key can be leased again, and anything older has been superseded.

    ``DISTINCT ON`` so the row count is one per key rather than one per lease in
    that key's history, and one statement whatever the page size.
    """
    if not keys:
        return {}
    rows = await session.scalars(
        select(Lease)
        .where(Lease.session.in_(set(keys)))
        .distinct(Lease.session)
        .order_by(Lease.session, Lease.acquired_at.desc())
    )
    return {ln.session: ln for ln in rows}


def _ending(lease: Lease | None) -> dict | None:
    """How this lease ended, or nothing because nobody said it did.

    Only a lease carrying an ``end_reason``: a released lease with none is a
    handoff, and a lapsed one is nobody saying anything at all. Both are honestly
    reported as "no ending", which is the distinction #277 exists to draw.
    """
    if lease is None or not lease.end_reason:
        return None
    return {"reason": lease.end_reason,
            "at": lease.released_at.isoformat() if lease.released_at else None,
            "holder": lease.holder}


def _lease_clock(lease: Lease | None) -> dict | None:
    """When this key's newest lease stopped being valid, and who held it (#378).

    **The clock a reader measures SILENCE with, and the one ``updated_at`` is
    not.** ``updated_at`` belongs to the transcript and moves on ``/snapshot``;
    the lease moves on every prompt. They usually track each other, and where
    they do not the gap runs the wrong way: a session that pushed at ten, kept
    renewing until noon and then died is two minutes quiet at 12:02 and two
    HOURS quiet by the transcript. A fleet view judging liveness off the wrong
    one reads a working agent as long gone — which is the misreading the fleet
    page exists to prevent, arriving through the field it trusted.
    """
    if lease is None:
        return None
    return {"holder": lease.holder,
            "since": lease.acquired_at.isoformat(),
            "expires": lease.expires_at.isoformat(),
            "released": lease.released_at.isoformat() if lease.released_at else None,
            "end_reason": lease.end_reason}


async def _last_endings(session: AsyncSession, keys: list[str]) -> dict[str, dict]:
    """How each session in view ended, or nothing because it has not.

    **The ending belongs to a LEASE, not to a session key**, and that is the
    subtlety worth stating: a key can be leased again. A session that ended at
    noon and was resumed at one is live, and reporting noon's ending beside
    ``live: true`` would be the board asserting two contradictory things about
    one row. So this reads the NEWEST lease per key and speaks only if that one
    carries a reason — a key whose latest lease is held, or lapsed, or handed
    off has no ending to report however many it had before.

    Only leases carrying an ``end_reason``: a released lease with none is a
    handoff, and a lapsed one is nobody saying anything at all. Both are honestly
    reported as "no ending", which is the distinction #277 exists to draw —
    before it, a finished session, a handed-off one and a crashed one were the
    same absence here.

    ``DISTINCT ON`` so the row count is one per key rather than one per lease in
    that key's history, and one statement whatever the page size.
    """
    leases = await _last_leases(session, keys)
    return {key: end for key, ln in leases.items() if (end := _ending(ln))}


@router.get("/sessions")
async def list_sessions(
    _reader: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(50, ge=1, le=500),
    include_ended: bool = Query(
        False,
        description="also list sessions whose last lease was ENDED but which never "
                    "pushed a transcript — an ending with no `sessions` row behind it",
    ),
) -> list[dict]:
    """All known sessions — live (held now) and resumable (handed off) — with
    freshness and transcript size, for the board + `qb sessions`/`qb resume`.

    ``include_ended`` widens it to endings with nothing behind them (#378). It is
    a flag rather than the default, and the reason is the ``limit``: this list is
    built from transcript records and live leases, one page of them, and folding
    an unbounded second population in would spend a caller's page on rows it did
    not ask for. Off, every existing reader sees exactly what it saw before; on,
    a fleet view gets the row a session leaves when it ends without ever having
    pushed anything — which is the one transition such a view most needs, and the
    same hole ``GET /session/{key}`` had until #277.
    """
    now = _utcnow()
    records = (
        await session.scalars(
            select(SessionRecord).order_by(SessionRecord.updated_at.desc()).limit(limit)
        )
    ).all()
    active = (
        await session.scalars(
            select(Lease).where(Lease.released_at.is_(None), Lease.expires_at > now)
        )
    ).all()

    last = await _last_leases(session, [r.session for r in records])
    ends = {key: end for key, ln in last.items() if (end := _ending(ln))}
    shas = {r.latest_blob for r in records if r.latest_blob}
    sizes: dict[str, int] = {}
    if shas:
        rows = await session.execute(select(Blob.sha, Blob.size).where(Blob.sha.in_(shas)))
        sizes = dict(rows.all())

    live = {lease.session: lease for lease in active}
    out: dict[str, dict] = {}
    for r in records:
        lv = live.get(r.session)
        out[r.session] = {
            "session": r.session,
            "cwd": r.cwd,
            "title": (lv.title if lv else None) or r.title,
            "recap": (lv.recap if lv else None) or r.recap,
            "model": (lv.model if lv else None) or r.model,
            "device": (lv.device if lv else r.device),
            "holder": r.holder,
            "updated_at": r.updated_at.isoformat(),
            "blob": r.latest_blob,
            "size": sizes.get(r.latest_blob) if r.latest_blob else None,
            "live": lv is not None,
            "resumable": r.latest_blob is not None,
            # What ended this session last time, or None because nothing ever
            # reported an ending. Not the same as `live: false` — that covers a
            # lease that lapsed with nobody saying anything, which is the reading
            # a fleet view most needs to stop guessing at (#252, #277).
            "ended": ends.get(r.session),
            # The newest lease this key ever held, live or not. It is what a
            # reader has to measure a SILENCE against: `updated_at` above is the
            # transcript's clock, and a lease outlives its last push.
            "last_lease": _lease_clock(lv or last.get(r.session)),
        }
    for lease in active:  # live sessions not yet handed off (no record)
        out.setdefault(lease.session, {
            "session": lease.session,
            "cwd": lease.cwd,
            "title": lease.title,
            "recap": lease.recap,
            "model": lease.model,
            "device": lease.device,
            "holder": lease.holder,
            "updated_at": lease.acquired_at.isoformat(),
            "blob": None,
            "size": None,
            "live": True,
            "resumable": False,
            # Live by definition on this branch — a lease that is held now cannot
            # also have been ended — but present, because a caller must not have
            # to know which branch built its row to know which keys exist.
            "ended": None,
            "last_lease": _lease_clock(lease),
        })

    # **An ending with no `sessions` row behind it is still an ending** (#378).
    # The two loops above are built from transcript records and from live leases,
    # so a session that never pushed a transcript is visible exactly while it
    # holds a lease and vanishes from this list the moment it ends — the one
    # transition a fleet view most needs to show. #277 fixed the same hole in
    # `GET /session/{key}` ("it ended, at this time, for this reason" is not
    # nothing to say) and left the list, which is what a page renders.
    #
    # Newest lease per key first, then the ones carrying a reason, so this reads
    # the same rule `_last_endings` does: a key whose latest lease is held, or
    # lapsed, or handed off has no ending to report however many it had before.
    if include_ended:
        newest = (
            select(Lease)
            # "no `sessions` row behind it" is the actual condition, so it is
            # the actual filter. Excluding the keys already in `out` looked
            # equivalent and is not: `out` is one PAGE of records, so a session
            # whose record sits past the limit would be picked up here and
            # rendered as transcript-less — `blob: null`, `resumable: false` —
            # about a session that is perfectly resumable.
            .where(Lease.session.not_in(select(SessionRecord.session)))
            .distinct(Lease.session)
            .order_by(Lease.session, Lease.acquired_at.desc())
            .subquery()
        )
        orphaned = await session.execute(
            select(newest)
            .where(newest.c.end_reason.is_not(None))
            .order_by(newest.c.released_at.desc())
            .limit(limit)
        )
        for ln in orphaned.mappings():
            at = ln["released_at"]
            out[ln["session"]] = {
                "session": ln["session"],
                "cwd": ln["cwd"],
                "title": ln["title"],
                "recap": ln["recap"],
                "model": ln["model"],
                "device": ln["device"],
                "holder": ln["holder"],
                # There is no transcript to have been updated, so the lease's own
                # ending is the freshest thing known about this key.
                "updated_at": (at or ln["expires_at"]).isoformat(),
                "blob": None,
                "size": None,
                "live": False,
                "resumable": False,
                "ended": {"reason": ln["end_reason"],
                          "at": at.isoformat() if at else None,
                          "holder": ln["holder"]},
                "last_lease": {"holder": ln["holder"],
                               "since": ln["acquired_at"].isoformat(),
                               "expires": ln["expires_at"].isoformat(),
                               "released": at.isoformat() if at else None,
                               "end_reason": ln["end_reason"]},
            }

    subs_by_session = await active_subagents_by_session(session, now)
    for s in out.values():
        s["subagents"] = subs_by_session.get(s["session"], [])

    return sorted(out.values(), key=lambda s: (s["live"], s["updated_at"]), reverse=True)[:limit]


@router.get("/session/{session_key}")
async def get_session_state(
    session_key: str,
    _reader: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Peer discovery: the latest handed-off blob plus any active lease.

    A peer claims (POST /lease) then GETs ``latest_blob`` from /blob to resume.
    """
    now = _utcnow()
    record = await session.get(SessionRecord, session_key)
    active = await _active_lease(session, session_key, now)
    ended = (await _last_endings(session, [session_key])).get(session_key)
    # A reported ending is enough to know this session. It used to 404 — a session
    # that held a lease, took claims and was ended without ever pushing a
    # transcript has no `sessions` row, so the one endpoint that could say how it
    # finished answered "unknown session" about it. "It ended, at this time, for
    # this reason" is not nothing to say.
    if record is None and active is None and ended is None:
        raise HTTPException(404, "unknown session")
    return {
        "session": session_key,
        "latest_blob": record.latest_blob if record else None,
        "cwd": (record.cwd if record else None) or (active.cwd if active else None),
        "title": (record.title if record else None) or (active.title if active else None),
        "recap": (record.recap if record else None) or (active.recap if active else None),
        "model": (record.model if record else None) or (active.model if active else None),
        "device": record.device if record else None,
        "holder": record.holder if record else None,
        "updated_at": record.updated_at.isoformat() if record else None,
        "active_lease": _lease_view(active) if active else None,
        "ended": ended,
    }
