"""Blockers: raise a question a human owes an answer to, and record the answer — #328.

Three verbs over :class:`~app.models.blocker.Blocker`. The row's own docstring
argues for the table; this module owns who may write what, and there is exactly
one interesting rule in it.

## An agent asks; a person answers — and it is one endpoint either way

``POST /blockers/resolve`` takes :func:`app.auth.author`, so both callers reach
it, and the caller's credential decides which act happened. That is
:func:`app.api.plan.exempt_item`'s shape, deliberately, and its argument
transfers unchanged:

    the same call an agent makes to ask is the call a person makes to answer, and
    the only thing the caller's credential decides is which of the two it was

* **A person** ANSWERS. ``resolved_by`` is their ``human/<user>`` identity and the
  resolution is the payload the next agent reads.
* **An agent** may only WITHDRAW, and only a blocker it raised itself. Recorded
  the same way, with ``resolved_by`` naming the agent — so a reader can always
  tell an answer from a retraction, which is the whole reason the column holds an
  identity rather than a boolean.

Why an agent may withdraw at all: a loop that finds the answer in the docs two
minutes after asking should take its question out of a person's queue, and the
alternative is a queue that fills with resolved-by-circumstance rows nobody dares
close. Why only its own: withdrawing somebody else's question is answering it.

**What this deliberately does not do is let an agent withdraw its way past a
decision.** A withdrawal says *the question no longer needs answering*, and the
record says who decided that. If the work then proceeds on a guess, the guess is
attributable — which is more than the prose in a ``note`` field ever managed
(#328's measurement: three blockers written as English, countable by nobody).

## Raising is unrestricted, on purpose

Any enrolled agent may raise one, with no gate. A blocker costs a person a glance
and nothing else, and the failure this table exists to fix is judgements that were
never written down — so the friction belongs on *answering*, never on *asking*.
Re-raising an identical open question is a no-op returning the existing row, which
is what the partial unique index is for: a loop that asks every run must not fill
the table, and must not have to check first either.

## `condition` is what makes "identical" mean the question and not the class

#576: without it the key was (subject, class), so `qb-doctor` asking three
different things about one repo under `environment` got one row and two answers
saying an open blocker already covered it. `condition` names WHICH standing
question. It is normalised here — trimmed and lowercased — and otherwise passed
through untouched, which is `app.claimkey`'s rule for an open namespace: police
the spelling a consumer keys on, and do not police a vocabulary nobody here owns.
Refusing an unfamiliar condition would refuse an escalation, and that is the one
outcome this whole path exists to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.claims import is_unique_violation
from app.auth import author, reader
from app.db import get_session
from app.identity import is_human
from app.models.blocker import (
    MAX_CONDITION,
    MAX_DETAIL,
    MAX_QUESTION,
    SUBJECT_KINDS,
    Blocker,
)
from app.needs_human import NEEDS_HUMAN_CLASSES

router = APIRouter(tags=["blockers"])


class BlockIn(BaseModel):
    subject_kind: str = Field(description="item | issue | pr | repo")
    subject_value: str = Field(min_length=1, max_length=200)
    kind: str = Field(description="one of app.needs_human.NEEDS_HUMAN_CLASSES")
    #: WHICH standing question, when a subject carries more than one of a class.
    #: Omitted means the subject and the class are the whole question — right for
    #: a producer that already keys on a real PR or issue. See
    #: :class:`~app.models.blocker.Blocker` for the fault-not-reading boundary.
    condition: str = Field(default="", max_length=MAX_CONDITION)
    question: str = Field(min_length=1, max_length=MAX_QUESTION)
    detail: str | None = Field(default=None, max_length=MAX_DETAIL)
    #: Who is being asked. Omitted means "any human" — which is a real answer and
    #: not a missing one: it is the queue everyone can see, and the `⛔ N waiting
    #: on you` chip must not claim unowned work is yours.
    owner: str | None = None
    repo: str | None = None


class ResolveIn(BaseModel):
    blocker_id: str
    #: Required, and it is the payload rather than bookkeeping. An unblock with no
    #: resolution is how "waiting on a human" turns quietly back into a guess.
    resolution: str = Field(min_length=1, max_length=MAX_DETAIL)


def _view(b: Blocker) -> dict:
    return {
        "id": str(b.id),
        "repo": b.repo,
        "subject": {"kind": b.subject_kind, "value": b.subject_value},
        "kind": b.kind,
        # Returned rather than write-only, and that is the compatibility story:
        # a board predating #576 ignores an unknown field (`BlockIn` does not
        # forbid extras, deliberately — see the module docstring), so a producer
        # that sent a condition and gets none back has been told, in the answer,
        # that its rows will collapse. Silent degradation with a way to see it.
        "condition": b.condition,
        "question": b.question,
        "detail": b.detail,
        "owner": b.owner,
        "raised_by": b.raised_by,
        "raised_at": b.raised_at.isoformat() if b.raised_at else None,
        "resolved_at": b.resolved_at.isoformat() if b.resolved_at else None,
        "resolved_by": b.resolved_by,
        "resolution": b.resolution,
        # Said rather than left to be inferred from `resolved_by`'s namespace: a
        # client that wants "did a person answer this, or did the asker take it
        # back" should not have to know how identities are spelled.
        "answered_by_a_person": bool(b.resolved_by) and is_human(b.resolved_by or ""),
    }


@router.post("/blockers")
async def raise_blocker(body: BlockIn, who: str = Depends(author),
                        session: AsyncSession = Depends(get_session)) -> dict:
    """Raise one. Re-raising an identical open question returns the existing row.

    Idempotent by the partial unique index rather than by a read-then-write: two
    loops asking the same question in the same second would both pass a check and
    both insert, and the second row would say nothing the first did not.
    """
    condition = body.condition.strip().lower()
    if body.kind not in NEEDS_HUMAN_CLASSES:
        raise HTTPException(422, detail={
            "error": f"{body.kind!r} is not a blocker class",
            "classes": list(NEEDS_HUMAN_CLASSES),
            "hint": "`other` is the escape hatch, and a class that keeps turning "
                    "up under it with the same reason is the evidence for adding "
                    "a word — see app/needs_human.py"})
    if body.subject_kind not in SUBJECT_KINDS:
        raise HTTPException(422, detail={
            "error": f"{body.subject_kind!r} is not a subject kind",
            "kinds": list(SUBJECT_KINDS)})

    row = Blocker(repo=body.repo, subject_kind=body.subject_kind,
                  subject_value=body.subject_value, kind=body.kind,
                  condition=condition,
                  question=body.question.strip(), detail=body.detail,
                  owner=body.owner, raised_by=who)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        if not is_unique_violation(e):
            raise
        existing = (await session.execute(
            select(Blocker).where(
                Blocker.repo.is_(body.repo) if body.repo is None
                else Blocker.repo == body.repo,
                Blocker.subject_kind == body.subject_kind,
                Blocker.subject_value == body.subject_value,
                Blocker.kind == body.kind,
                # Every column of the index, or this recovery is not a recovery.
                # It fetches "the row the collision names", and with several
                # conditions open on one subject a four-part filter matches all
                # of them — so `scalar_one_or_none` raises `MultipleResultsFound`
                # and the documented no-op becomes a 500, in exactly the case
                # #576 added the column for. The lookup has to key on what the
                # index keys on.
                Blocker.condition == condition,
                Blocker.resolved_at.is_(None)))).scalar_one_or_none()
        if existing is None:  # pragma: no cover - lost a race with a resolve
            raise
        return {"blocker": _view(existing), "raised": False,
                "note": "an open blocker already asks this of this subject"
                        + (f" under {condition!r}" if condition else "")}
    await session.refresh(row)
    return {"blocker": _view(row), "raised": True}


@router.post("/blockers/resolve")
async def resolve_blocker(body: ResolveIn, who: str = Depends(author),
                          session: AsyncSession = Depends(get_session)) -> dict:
    """Answer one — or, if you raised it and are not a person, withdraw it."""
    row = await session.get(Blocker, body.blocker_id)
    if row is None:
        raise HTTPException(404, detail={"error": "no such blocker",
                                         "blocker_id": body.blocker_id})
    if row.resolved_at is not None:
        raise HTTPException(409, detail={
            "error": "that blocker is already resolved",
            "blocker": _view(row),
            "hint": "raise a new one rather than overwriting the answer — the "
                    "resolution is the record the next agent reads"})

    person = is_human(who)
    if not person and row.raised_by != who:
        raise HTTPException(403, detail={
            "error": "answering a blocker is a person's act",
            "raised_by": row.raised_by,
            "hint": "an agent may withdraw a question it raised itself; answering "
                    "somebody else's is answering it. A person is proved at the "
                    "edge — see app.auth.author.",
            "blocker_id": str(row.id)})

    row.resolved_at = datetime.now(UTC)
    row.resolved_by = who
    row.resolution = body.resolution.strip()
    await session.commit()
    await session.refresh(row)
    return {"blocker": _view(row), "answered": person, "withdrawn": not person}


@router.get("/blockers")
async def list_blockers(repo: str | None = None, owner: str | None = None,
                        kind: str | None = None, open_only: bool = Query(True, alias="open"),
                        limit: int = Query(200, ge=1, le=1000),
                        _who: str = Depends(reader),
                        session: AsyncSession = Depends(get_session)) -> dict:
    """The queue, oldest first — because the oldest unanswered question is the one
    most likely to have been forgotten, and age is the only signal here nobody has
    to maintain."""
    q = select(Blocker)
    if repo is not None:
        q = q.where(Blocker.repo == repo)
    if owner is not None:
        q = q.where(Blocker.owner == owner)
    if kind is not None:
        q = q.where(Blocker.kind == kind)
    if open_only:
        q = q.where(Blocker.resolved_at.is_(None))
    rows = (await session.execute(q.order_by(Blocker.raised_at).limit(limit))).scalars().all()
    by_class: dict[str, int] = {}
    for r in rows:
        if r.resolved_at is None:
            by_class[r.kind] = by_class.get(r.kind, 0) + 1
    return {
        "blockers": [_view(r) for r in rows],
        "open": sum(1 for r in rows if r.resolved_at is None),
        # Grouped, because #279's line is that five `ui` checks and one `decision`
        # is a different afternoon from six decisions — and that is only true if
        # the split is visible.
        "by_class": by_class,
        "ordering": "oldest first",
        "classes": list(NEEDS_HUMAN_CLASSES),
    }
