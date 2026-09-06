"""The finding ledger — a round-stamped state per finding per PR (#772).

`POST /review/ledger` writes it and `GET /review/ledger` reads it back. Together
they are the seam #772 asks for: one call at the START of a round hydrates what
the last round left behind, one call at the END records what this round did with
it, and cross-round state stops living in prose.

**Why a separate module rather than another 600 lines of `app/api/reviews.py`.**
That file is 9,000 lines and every read of it is a scroll. This one is a table,
two endpoints and one rule set, and it depends on `reviews` in exactly one
direction — canonicalisation, echoing and the bounds are imported from there so
the two endpoints refuse the same repo spelling, quote unprintables the same way
and cap the same fields. Nothing in `reviews` imports this, so there is no cycle
and the router is included beside it in `app/main.py`.

**The shape is `POST /review/outcomes`', deliberately and line for line.** A
batch, per-ITEM rejection with a named reason, an idempotent repeat, a retry on
the unique constraint, and a status code that agrees with the body. Not because
symmetry is pretty: a fix loop recording twelve findings must not lose eleven of
them to one typo, and the harness wiring this is the harness that already wires
that endpoint. A second endpoint in this family with different manners is a
second thing to get wrong at 3am.

**What this module refuses to do.** It does not compute a finding's identity, it
does not decide which state a finding is in, and it does not derive a state from
anything the panel sent to another endpoint. `finding_key` comes from
`harness/loops/panel_locality.py`, which matches findings across rounds by
diff-hunk overlap; a board-side identity would be a second opinion about which
two findings are one, formed without the diff. And the states are the producer's
verdicts, on the argument `m6bc45ff1` makes at length for `converged`: a
board-side derivation would be a second reading of the policy that produced the
verdict stored beside it, free to disagree with it about the same round.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from math import isfinite
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.reviews import (
    _MAX_ITEM_ERRORS,
    _PG_UNIQUE_VIOLATION,
    MAX_OUTCOMES,
    MAX_OUTCOMES_ACCEPTED,
    MAX_REF_CHARS,
    MAX_REJECTIONS_LOGGED,
    _asked_repo,
    _echo,
    _stored_repo,
    _trimmed_or_none,
)
from app.auth import identify, reader
from app.db import get_session
from app.finding_lifecycle import (
    LIFECYCLE_SOURCES,
    LIFECYCLE_STATES,
    MAX_LIFECYCLE_REASON_CHARS,
    OPEN_STATES,
    PROMOTABLE_STATES,
    REASON_REQUIRED_STATES,
    SOURCE_PROMOTION,
)
from app.models.review import ReviewFinding, ReviewFindingLedger, ReviewRun

_log = logging.getLogger("app.review_ledger")

router = APIRouter(tags=["review"])

#: The single-line fields an entry may set, and the bound each takes. One dict
#: because three rules iterate the same set — bounds, "was it sent", fill-versus-
#: rewrite — and a field added to one list and not the others is the half-wiring
#: `OUTCOME_FIELDS` was written to stop.
ENTRY_FIELDS = {
    "reason": MAX_LIFECYCLE_REASON_CHARS,
    "superseded_by": MAX_REF_CHARS,
}

#: How many findings one ledger read returns. Higher than the outcome cap because
#: this is the HYDRATION read: a long-running pull request's ledger is the thing
#: the next round is briefed against, and a truncated ledger silently reports
#: promotable findings as absent — which reads as "nothing was deferred", the
#: flattering direction and the exact failure the table exists to end.
MAX_LEDGER_FINDINGS = 1000


class LedgerEntryIn(BaseModel):
    """One writer's word about one finding in one round.

    ``extra="forbid"``, like :class:`app.api.reviews.OutcomeIn` and unlike the
    ingest path: a misspelled ``supersededBy`` here silently drops a pointer the
    chain view follows, and this model can afford the strictness because a
    rejection costs its own row rather than the batch.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    #: The defect's identity, as the producer computed it. ``finding_key`` is
    #: accepted as an alias because that is what every read path calls it back —
    #: the same alias `OutcomeIn.key` takes, for the same reason.
    key: str = Field(min_length=1, max_length=MAX_REF_CHARS,
                     validation_alias=AliasChoices("key", "finding_key"))
    state: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    source: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    reason: str | None = None
    superseded_by: str | None = None

    @field_validator("key", "state", "source")
    @classmethod
    def _trim(cls, v: str) -> str:
        # `min_length=1` runs before this, so `"   "` passes it and trims to
        # empty — and an empty key was then rejected downstream with "no finding
        # with this key on this PR", which points the caller at its keys when the
        # fault was a blank one. `OutcomeIn._trim`'s lesson, verbatim.
        s = v.strip()
        if not s:
            raise ValueError("blank")
        return s

    @field_validator("state", "source")
    @classmethod
    def _fold(cls, v: str) -> str:
        return v.lower()

    @field_validator("reason", "superseded_by")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        # None survives as None and means CLEAR when the key was sent explicitly;
        # `model_fields_set` tells that from an absent key.
        return _trimmed_or_none(v)

    @model_validator(mode="after")
    def _bounds(self) -> LedgerEntryIn:
        long = [f for f, cap in ENTRY_FIELDS.items()
                if (getattr(self, f) or "") and len(getattr(self, f)) > cap]
        if long:
            caps = ", ".join(f"{f} over {ENTRY_FIELDS[f]} characters" for f in long)
            raise ValueError(f"too long: {caps}")
        return self

    @model_validator(mode="after")
    def _no_nul(self) -> LedgerEntryIn:
        """A NUL is refused here rather than marked (#646).

        Postgres holds no NUL in a ``text`` column, so an entry carrying one was a
        500 at INSERT that took every sibling with it. Refused rather than marked
        on `OutcomeIn._no_nul`'s rule: ingest must never fail a review because the
        board was fussy, but a writer recording a state can simply be told, and a
        rejection costs that entry alone. Marking would be worse than useless on
        ``key``, which is matched with ``==`` — a marked key names no finding.
        """
        bad = [f for f in ("key", "state", "source", *ENTRY_FIELDS)
               if "\x00" in (getattr(self, f) or "")]
        if bad:
            raise ValueError(f"NUL in {', '.join(bad)} — no text column holds one")
        return self


class LedgerIn(BaseModel):
    """A round's worth of ledger entries for one pull request.

    ``round`` and ``cycle`` ride on the REQUEST and not on each entry, and that is
    the invariant the rest of the endpoint is built on: one call is one writer
    inside one round. It makes the idempotency key legible, it makes
    ``origin_round`` derivable from the rows already stored, and it removes the
    payload that would otherwise be possible — a batch claiming two rounds at once,
    which no producer has and no reader could order.
    """

    model_config = ConfigDict(populate_by_name=True)

    repo: str = Field(min_length=1, max_length=MAX_REF_CHARS,
                      validation_alias=AliasChoices("github", "repo"),
                      description="github nameWithOwner")
    pr: int = Field(ge=1)
    #: The round every entry in this call belongs to. 1-based, matching
    #: ``ReviewRun.round`` and the panel's own numbering.
    round: int = Field(ge=1)
    #: The panel cycle, where the producer has one. Stored because a round number
    #: alone cannot separate two agents looping one pull request — the ambiguity
    #: `GET /review/findings`' ``followed_by`` refuses to guess through.
    cycle: str | None = None
    #: The run this round recorded, where there is one. Checked to belong to this
    #: (repo, pr) before it is stored, so a mistyped id cannot attach a ledger row
    #: to another pull request's round.
    run_id: int | None = Field(default=None, ge=1)
    #: The writer's session, beside its identity — the pairing every other review
    #: write keeps, and what lets a peer reach whoever recorded a state it
    #: disagrees with.
    session: str | None = None
    #: ``list[Any]`` for `OutcomesIn.outcomes`' reason and it is load-bearing:
    #: FastAPI validates a typed list BEFORE the handler runs, so one entry with a
    #: missing ``key`` would 422 the request and lose every valid sibling — the
    #: opposite of what this endpoint promises. The item schema is attached by
    #: hand because `list[Any]` erases it from the generated client.
    entries: list[Any] = Field(
        min_length=1, max_length=MAX_OUTCOMES_ACCEPTED,
        json_schema_extra=lambda f: f.update(items=LedgerEntryIn.model_json_schema()),
    )

    @model_validator(mode="before")
    @classmethod
    def _finite(cls, v: object) -> object:
        """Drop ``NaN``/``Infinity`` from this REQUEST's own fields (#646).

        Not about storage — no column here takes a float. It is about the refusal:
        ``pr`` and ``round`` are plain ``int``, so a non-finite one raises, and
        FastAPI renders that 422 by quoting the offending input back into JSON,
        which cannot represent it. The request then 500s having reached no
        database and the caller is told nothing about a batch that was refused
        before it began.

        The request's own keys only, never inside an entry: a ``reason`` is
        ``str | None`` where ``None`` is an explicit CLEAR, so nulling
        ``reason: NaN`` would turn a garbled value into a deliberate erasure.
        Left alone, the entry fails :class:`LedgerEntryIn` and becomes that
        entry's rejection, which is this endpoint's rule.
        """
        if not isinstance(v, Mapping):
            return v
        return {k: None if isinstance(x, float) and not isfinite(x) else x
                for k, x in v.items()}

    @field_validator("repo")
    @classmethod
    def _repo(cls, v: str) -> str:
        # Canonicalised and not merely trimmed, because it is matched with `==`
        # against what `POST /review` stored, and since #326 that is the folded
        # spelling. A trailing space made the known-key set empty and rejected
        # every entry with "no finding with this key on this PR" — which points
        # the caller at its keys, which were fine.
        return _stored_repo(v)

    @field_validator("cycle", "session")
    @classmethod
    def _line(cls, v: str | None) -> str | None:
        # Refused, not sliced, on `OutcomesIn._session`'s rule: a truncated
        # session is a contact address that resolves to nothing handed back as if
        # it were real, and a truncated cycle id silently splits one loop into two
        # in every join that groups on it.
        s = _trimmed_or_none(v)
        if s and len(s) > MAX_REF_CHARS:
            raise ValueError(f"over {MAX_REF_CHARS} characters")
        if s and "\x00" in s:
            raise ValueError("holds a NUL, which Postgres refuses in a text column")
        return s


def _entry_item(raw: object) -> tuple[LedgerEntryIn | None, str | None]:
    """One payload entry as a validated item, or the reason it is not one.

    Validated here rather than by FastAPI — the point of ``entries`` being a list
    of raw objects. One entry with a missing ``key`` would otherwise 422 the whole
    request and lose every valid sibling.
    """
    if not isinstance(raw, dict):
        return None, f"not an object: {_echo(type(raw).__name__)}"
    try:
        return LedgerEntryIn.model_validate(raw), None
    except ValidationError as e:
        parts = [f"{'.'.join(str(p) for p in err['loc']) or 'item'}: {err['msg']}"
                 for err in e.errors()]
        shown = "; ".join(_echo(p) for p in parts[:_MAX_ITEM_ERRORS])
        # ...and say how many were not shown. Dropping the rest in silence is the
        # "a caller told nothing assumes it was told everything" failure the whole
        # endpoint is organised against, one layer down.
        rest = len(parts) - _MAX_ITEM_ERRORS
        return None, f"{shown} (+{rest} more)" if rest > 0 else shown


def _entry_reason(item: LedgerEntryIn, known: set[str], current: ReviewFindingLedger | None,
                  seen: set[tuple[str, str]]) -> str | None:
    """Why this entry cannot be recorded, or None if it can.

    Itemised and named rather than refusing the request: a round reporting twelve
    findings must not lose the eleven good ones to one typo. Refused rather than
    coerced, on `_outcome_reason`'s split — the panel must never fail a REVIEW
    because the board was fussy, so that path takes what it can read; a writer
    recording a state has no such constraint and can be told.
    """
    if item.state not in LIFECYCLE_STATES:
        return (f"unknown state {_echo(item.state)!r}; "
                f"one of {'|'.join(LIFECYCLE_STATES)}")
    if item.source not in LIFECYCLE_SOURCES:
        return (f"unknown source {_echo(item.source)!r}; "
                f"one of {'|'.join(LIFECYCLE_SOURCES)}")
    if (item.key, item.source) in seen:
        # `seen` holds every (key, source) this payload has spoken about, accepted
        # or not — `_apply`'s note on the outcome endpoint's own version of this
        # bug: a pair whose FIRST entry was rejected had its duplicates told "the
        # first entry was kept" when nothing had been kept for it at all.
        return ("this key already appears earlier in this payload under the same "
                "source; one source may speak once per finding per request")
    if item.key not in known:
        # The rule that keeps the board out of the identity business while still
        # refusing a state with no evidence under it. The board does not mint keys
        # and does not check their SHAPE; it checks that some run of this pull
        # request recorded a finding under this one. A producer hitting this has
        # POSTed its ledger before its review, or is sending a key from another PR.
        return ("no finding with this key on this PR — POST /review for the round "
                "before its ledger, so every state has an observation under it")
    if item.state in REASON_REQUIRED_STATES and not item.reason:
        # The named skip reason itself. `raised` and `fixed` are the loop working;
        # every other state is a claim that this finding is NOT being worked, and a
        # bare one is a confident assertion with nothing behind it — landing, here,
        # in the number that decides whether a cycle converged.
        return (f"{item.state} needs a reason: it says this finding is not being "
                "worked, and a bare one is the assertion this ledger exists to "
                "make countable")
    if item.state == "superseded" and not item.superseded_by:
        return "superseded needs superseded_by: the key of the finding that replaced it"
    if item.superseded_by:
        if item.state != "superseded":
            return ("superseded_by is only meaningful on a superseded state, "
                    f"not {item.state}")
        if item.superseded_by == item.key:
            return "superseded_by names this finding itself"
        if item.superseded_by not in known:
            return "superseded_by names no finding on this PR"
    # ---- the two rules about coming BACK, which are the whole round-to-round move.
    was = current.state if current is not None else None
    if item.source == SOURCE_PROMOTION:
        if item.state != "raised":
            return (f"a {SOURCE_PROMOTION} entry returns a finding to 'raised', "
                    f"not to {item.state}")
        if was is None:
            return ("nothing to promote: this finding has no ledger state on this "
                    "PR yet, so the round that raises it should say so")
        if was not in PROMOTABLE_STATES:
            # A settled defect that reappears is a new OBSERVATION, and the round
            # raising it says so. Letting a promotion reopen it would let a ledger
            # edit quietly overturn a decision somebody recorded — with none of the
            # evidence the outcome table demands for the same move.
            return (f"cannot promote from {was}: only "
                    f"{'|'.join(sorted(PROMOTABLE_STATES))} come back")
    elif item.state == "raised" and was is not None and was != "raised":
        # THE FAILURE THIS FEATURE EXISTS TO END, refused at the door. A deferred
        # finding re-raised by the ordinary panel writer is indistinguishable from
        # fresh damage — which is what inflates `fix_injection` and stops cycles
        # that were converging. Coming back is allowed; coming back SILENTLY is
        # not, so the caller is sent to the door that costs it a reason.
        return (f"this finding is {was}; returning it to 'raised' is a promotion — "
                f"send source={SOURCE_PROMOTION!r} with a reason")
    return None


@router.post(
    "/review/ledger",
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "existing ledger entries updated, amended or unchanged"},
        201: {"description": "at least one state recorded for the first time"},
        409: {"description": "another writer recorded one of these while this was in flight"},
        422: {"description": "either the request shape was wrong (`detail`) or every entry "
                             "in a well-formed request was refused (`rejected`)"},
    },
)
async def record_ledger(
    body: LedgerIn,
    response: Response,
    author: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record where each of these findings stands at round ``round`` (#772).

    A finding used to exist inside a round and nowhere else. This is the row that
    carries it out: a state, in a closed vocabulary, stamped with the round it
    happened in and the writer that wrote it, with the reason it got there.

    **The states**, from :mod:`app.finding_lifecycle`: ``raised`` (open work),
    ``unpaid`` (a budget ran out before anybody looked), ``deferred`` (somebody
    looked and parked it), ``escalated`` (no fix round can settle it),
    ``fixed`` / ``narrowed`` / ``refuted`` / ``superseded`` (the recorded outcome
    vocabulary, unchanged and shared rather than paraphrased). ``unpaid`` beside
    ``deferred`` is the point: "nobody looked" and "somebody looked and said no"
    are different facts, only one of them argues for a bigger budget, and a design
    with one ``skipped`` bucket cannot tell them apart.

    **The round is part of the row, twice.** ``round`` is when this transition
    happened; ``origin_round`` is when the defect first entered the ledger and is
    carried forward unchanged by every later row — the board copies it from the
    earliest stored row, so a promoted finding cannot acquire a fresh round and
    read as new damage. It is not sent by the caller and is ignored if it is.

    **Promotion is the round-to-round move.** An entry with
    ``source: "promotion"``, ``state: "raised"`` and a reason returns a
    ``deferred`` or ``unpaid`` finding to open AS THE SAME FINDING. Any other
    route back to ``raised`` is refused and the caller is pointed at this one: a
    finding that comes back silently is exactly what this table was built to stop.

    **Idempotent per writer per round.** The unique key is
    ``(repo, pr, key, round, source)``, so a retry — the ordinary case, one agent
    whose request landed and whose client timed out — confirms rather than
    doubles. A writer that changes its own word inside one round rewrites its row,
    ``revisions`` counts it, ``prior_state`` keeps what it used to say, and the
    response names it under ``changed`` or ``amended``: a state that moves is
    legitimate, a state that moves silently is not.
    """
    # Retried once, and never in a loop, on `record_outcomes`' reasoning: the
    # commonest race for one (repo, pr, key, round, source) is one agent's own
    # timed-out retry, and both attempts re-read the stored rows so the second
    # takes the update path. A third failure is not contention and a request that
    # keeps retrying itself hides whatever it is.
    for attempt in (1, 2):
        try:
            result = await _apply_ledger(session, body, author)
            break
        except IntegrityError as e:
            await session.rollback()
            # ONLY the unique constraint is contention. A CHECK violation is
            # deterministic — retrying builds the identical invalid row, and
            # reporting it as "another writer got there first" sends the caller
            # round a loop over a bug in this service while hiding it from the
            # logs. `sqlstate` is asyncpg's spelling and `pgcode` psycopg's;
            # reading one fails closed in the wrong direction under the other.
            code = getattr(e.orig, "sqlstate", None) or getattr(e.orig, "pgcode", None)
            if code != _PG_UNIQUE_VIOLATION:
                raise
            if attempt == 2:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "another writer recorded a ledger entry for one of these findings "
                    "while this request was in flight; retry",
                ) from None

    if result["rejected"]:
        # Named back AND logged, the rule every write path here keeps: a caller
        # told nothing assumes everything landed, and these are the rows a
        # coverage marker will otherwise report as never recorded. The response
        # names every rejection; the log takes a bounded prefix and says how many
        # there were, because a shared log is not the place for a 5,000-item batch.
        listed = result["rejected"][:MAX_REJECTIONS_LOGGED]
        _log.warning("review ledger rejected: %s", json.dumps(
            {"repo": body.repo, "pr": body.pr, "round": body.round, "author": author,
             "rejected_total": len(result["rejected"]), "rejected": listed}, default=str))

    # The code has to agree with the body: a shell pipeline built around `qb` (a
    # curl wrapper) checks the code and nothing else, and "created" over a body of
    # twelve rejections is the response lying.
    if result["recorded"]:
        response.status_code = status.HTTP_201_CREATED
    elif result["changed"] or result["amended"] or result["unchanged"]:
        response.status_code = status.HTTP_200_OK
    else:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    return result


async def _apply_ledger(session: AsyncSession, body: LedgerIn, author: str) -> dict:
    """One attempt at recording a round's entries — see :func:`record_ledger`.

    Separate so the retry RE-READS. The stored rows decide insert from update, so
    replaying a failed attempt against the map fetched before it would take the
    same doomed path again.
    """
    known = set((await session.scalars(
        select(ReviewFinding.finding_key)
        .join(ReviewRun, ReviewRun.id == ReviewFinding.run_id)
        .where(ReviewRun.repo == body.repo, ReviewRun.pr == body.pr)
        .distinct()
    )).all())

    # A run id that belongs to another pull request is refused for the WHOLE
    # request rather than per entry: it is the request's field, not an entry's,
    # and a ledger row pointing at another PR's round is a join that silently
    # answers about the wrong loop. Refused rather than nulled, because a producer
    # that sent one meant it.
    if body.run_id is not None:
        owner = (await session.execute(
            select(ReviewRun.repo, ReviewRun.pr).where(ReviewRun.id == body.run_id)
        )).first()
        if owner is None or owner.repo != body.repo or owner.pr != body.pr:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"run_id {body.run_id} is not a run of {body.repo}#{body.pr}",
            )

    # Validate every entry BEFORE the row read, so the locked set names only keys
    # that could be written and one malformed entry costs its own row.
    items: list[tuple[int, LedgerEntryIn | None, str | None]] = []
    for i, raw in enumerate(body.entries):
        if i >= MAX_OUTCOMES:
            # Named rather than 422'd: a caller batching a long round would
            # otherwise lose all 501 rows when the first 500 were fine.
            items.append((i, None, f"over the {MAX_OUTCOMES}-entry cap for one request"))
            continue
        item, why = _entry_item(raw)
        items.append((i, item, why))
    sent = sorted({it.key for _, it, why in items if it is not None and why is None})

    # EVERY stored row for these keys, not only this round's, and locked.
    #
    # Every row, because two questions need the history: what this finding's
    # CURRENT state is (which decides whether a `raised` entry is a silent
    # re-open) and which round it FIRST entered the ledger in (`origin_round`,
    # which a promotion must not move).
    #
    # `with_for_update`, because two writers UPDATING one row raise nothing at
    # all: both read, both mutate in Python, and the second commit discards the
    # first's reason and revision count. Ordered under the lock because two
    # overlapping batches otherwise take their locks in whatever order the planner
    # returns them, and a plan change between them is enough to deadlock.
    rows = list((await session.scalars(
        select(ReviewFindingLedger).where(
            ReviewFindingLedger.repo == body.repo,
            ReviewFindingLedger.pr == body.pr,
            ReviewFindingLedger.finding_key.in_(sent),
        ).order_by(ReviewFindingLedger.finding_key, ReviewFindingLedger.round,
                   ReviewFindingLedger.id).with_for_update()
    )).all()) if sent else []

    history: dict[str, list[ReviewFindingLedger]] = {}
    for r in rows:
        history.setdefault(r.finding_key, []).append(r)
    # This round's own row per (key, source) — the one a repeat updates.
    mine = {(r.finding_key, r.source): r for r in rows if r.round == body.round}

    now = datetime.now(UTC)
    recorded: list[str] = []
    changed: list[dict] = []
    amended: list[dict] = []
    unchanged: list[str] = []
    rejected: list[dict] = []
    promoted: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for i, item, why in items:
        if item is None:
            # The (key, source) an unparseable entry MEANT, dug out of the raw
            # object under either spelling the model accepts. Not politeness: a
            # pair that reserves nothing lets a LATER well-formed entry for the
            # same pair slip past the once-per-request rule on the strength of an
            # earlier entry that failed to parse.
            raw = body.entries[i]
            key = src = None
            if isinstance(raw, dict):
                key = raw.get("key") if raw.get("key") is not None else raw.get("finding_key")
                src = raw.get("source")
            if isinstance(key, str) and key.strip():
                seen.add((key.strip(), str(src).strip().lower() if isinstance(src, str) else ""))
            rejected.append({"key": _echo(key) if key is not None else f"item {i}",
                             "reason": why})
            continue

        past = history.get(item.key, [])
        # The finding's state as it stands NOW: the newest row by (round, id),
        # which is the order the rows were written and therefore the order they
        # happened. Rows from this same round count — a promotion at the start of
        # a round is what a later entry in the same round is measured against.
        current = past[-1] if past else None
        reason = _entry_reason(item, known, current, seen)
        seen.add((item.key, item.source))
        if reason is not None:
            rejected.append({"key": _echo(item.key), "reason": reason})
            continue

        # Carried forward, never sent. The earliest stored round for this defect,
        # or this round when it has none — so a promotion keeps the ORIGINAL round
        # and a deferred finding returning in round 5 still reads as two rounds
        # old rather than as fresh damage.
        origin = past[0].origin_round if past else body.round

        row = mine.get((item.key, item.source))
        if row is None:
            session.add(ReviewFindingLedger(
                repo=body.repo, pr=body.pr, finding_key=item.key,
                state=item.state, source=item.source, reason=item.reason,
                superseded_by=item.superseded_by,
                round=body.round, origin_round=origin, cycle=body.cycle,
                run_id=body.run_id, set_by=author, session=body.session,
            ))
            recorded.append(item.key)
            if item.source == SOURCE_PROMOTION and current is not None:
                promoted.append({"key": item.key, "from": current.state,
                                 "origin_round": origin})
            continue

        if row.state != item.state:
            row.revisions += 1
            row.prior_state = row.state
            row.state = item.state
            # The old state's explanation does not survive the state. Anything the
            # caller did not resend is cleared, so a reason reading "past the
            # verification budget" cannot end up filed under `fixed`.
            for attr in ENTRY_FIELDS:
                setattr(row, attr, getattr(item, attr))
            changed.append({"key": item.key, "source": row.source,
                            "from": row.prior_state, "to": row.state})
            row.set_by, row.session = author, body.session
            row.updated_at = now
        else:
            # A repeat of the same state FILLS an empty field and never silently
            # rewrites a stored one. Rewriting is a real change — replacing the
            # reason that IS the evidence for a skip — so it counts as a revision
            # and is named back. Absent is "nothing to add"; an explicit null
            # CLEARS.
            rewrote, filled = [], []
            for attr in ENTRY_FIELDS:
                if attr not in item.model_fields_set:
                    continue
                value, was = getattr(item, attr), getattr(row, attr)
                if value == was:
                    continue
                (rewrote if was is not None else filled).append(attr)
                setattr(row, attr, value)
            if rewrote or filled:
                row.revisions += 1
                amended.append({"key": item.key, "source": row.source, "state": row.state,
                                "filled": sorted(filled), "rewrote": sorted(rewrote)})
                # `set_by` names whoever is responsible for the row's CURRENT
                # content, and a fill counts — the same rule the outcome endpoint
                # arrived at after a fill moved authorship invisibly. A genuine
                # no-op steals nothing, which is the case an idempotent retry hits
                # and the reason this is not "last toucher wins". `updated_at`
                # moves only with it, or "when was this last decided" answers
                # "just now" for a decision taken last week.
                row.set_by, row.session = author, body.session
                row.updated_at = now
            else:
                unchanged.append(item.key)

    await session.commit()

    return {
        "repo": body.repo,
        "pr": body.pr,
        "round": body.round,
        "recorded": recorded,
        "changed": changed,
        # A repeat that REWROTE a stored field under an unchanged state. Its own
        # bucket rather than folded into `changed` (which is about the state
        # moving) or `unchanged` (which it is not): the field that matters is
        # `reason`, and a rewritten reason is rewritten evidence.
        "amended": amended,
        "unchanged": unchanged,
        "rejected": rejected,
        # The round-to-round move, named back so a caller can see it happened and
        # what it came from. `origin_round` rides along because it is the fact the
        # caller cannot compute — it is the whole reason a promotion does not read
        # as new damage.
        "promoted": promoted,
    }


# ------------------------------------------------------------------ the read path

def _entry_view(r: ReviewFindingLedger) -> dict:
    return {
        "state": r.state,
        "source": r.source,
        "reason": r.reason,
        "superseded_by": r.superseded_by,
        "round": r.round,
        "origin_round": r.origin_round,
        "cycle": r.cycle,
        "run_id": r.run_id,
        "set_by": r.set_by,
        "session": r.session,
        "ts": r.ts.isoformat(),
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        # A state that moved, and what it moved from. Silence is a first answer;
        # a count is an answer that changed.
        "revisions": r.revisions,
        "prior_state": r.prior_state,
    }


@router.get("/review/ledger")
async def pr_ledger(
    _reader: str = Depends(reader),
    repo: str = Query(..., min_length=1, description="github nameWithOwner"),
    pr: int = Query(..., ge=1),
    state: str | None = Query(None, description="only findings currently in this state"),
    limit: int = Query(MAX_LEDGER_FINDINGS, ge=1, le=MAX_LEDGER_FINDINGS),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One pull request's finding ledger — what the last round left behind.

    The hydration read. Called once at the START of a round, it answers the three
    questions a round cannot otherwise ask: what is still open, what was parked
    and may be promoted, and what nobody was paid to look at.

    Each finding carries its CURRENT state — the newest entry by
    ``(round, id)``, which is the order the entries were written — plus its whole
    ``history``, oldest first. The history is returned rather than summarised
    because the summary is exactly what a round would have to reconstruct: "did
    round 3's fix answer round 2's complaint" is one walk down these rows, and a
    current-state-only read would put it back where it was, in two reports.

    ``origin_round`` is the round the defect first entered the ledger and never
    moves. Read it, not ``round``, when asking whether a finding is new: a
    promotion in round 5 of a finding deferred in round 2 has ``round: 5`` and
    ``origin_round: 2``, and counting it as new is the fix-injection inflation
    this whole feature exists to end.

    ``open_keys``, ``deferred_keys`` and ``unpaid_keys`` are the three lists a
    round actually consumes, published ready to be used rather than derived from
    ``findings[]`` by every caller — the shape ``needs_human_keys`` already takes
    on ``GET /review/findings``. ``deferred_keys`` is what ``promote`` may return;
    ``unpaid_keys`` is the population that says a budget is too small, and it is
    kept apart from ``deferred_keys`` because pooled they argue for nothing.

    ``truncated`` says the ledger held more findings than ``limit``. A truncated
    hydration reports promotable findings as absent, which reads as "nothing was
    deferred" — so it is stated rather than left to be inferred from a length.
    """
    repo = _asked_repo(repo)
    if state is not None:
        state = state.strip().lower()
        if state not in LIFECYCLE_STATES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"state={_echo(state)!r} is not one of {'|'.join(LIFECYCLE_STATES)}",
            )

    rows = list((await session.scalars(
        select(ReviewFindingLedger)
        .where(ReviewFindingLedger.repo == repo, ReviewFindingLedger.pr == pr)
        # (round, id) and never ts: `ts` is when the BOARD heard about a row, and
        # a retry or a board that was down and caught up gives a later round an
        # earlier timestamp. The panel numbers the rounds and `id` breaks ties
        # inside one — the same ordering rule `GET /review/convergence` states for
        # picking a cycle's terminal round.
        .order_by(ReviewFindingLedger.finding_key, ReviewFindingLedger.round,
                  ReviewFindingLedger.id)
    )).all())

    by_key: dict[str, list[ReviewFindingLedger]] = {}
    for r in rows:
        by_key.setdefault(r.finding_key, []).append(r)

    findings = []
    for key, entries in by_key.items():
        current = entries[-1]
        if state is not None and current.state != state:
            continue
        findings.append({
            "key": key,
            **_entry_view(current),
            # The defect's age in rounds, since the subtraction is only correct
            # against `origin_round` and a reader doing it against `round` would
            # get zero for every promoted finding.
            "rounds_open": current.round - current.origin_round,
            "open": current.state in OPEN_STATES,
            "promotable": current.state in PROMOTABLE_STATES,
            "history": [_entry_view(e) for e in entries],
        })

    # Sorted by how long the defect has been on the books, oldest first — the
    # order a round wants to be briefed in, and stable, so two identical reads
    # never differ.
    findings.sort(key=lambda f: (f["origin_round"], f["key"]))
    truncated = len(findings) > limit
    findings = findings[:limit]

    return {
        "repo": repo,
        "pr": pr,
        # The highest round the ledger holds. NOT a count of rounds: rounds can go
        # unrecorded, and the number a cap is set in is the number.
        "last_round": max((r.round for r in rows), default=0),
        "entries": len(rows),
        "truncated": truncated,
        "findings": findings,
        # The three lists a round consumes, over the RETURNED findings so they
        # cannot disagree with `findings[]` — a key list computed over a wider
        # population than the rows beside it is worse than no list at all.
        "open_keys": [f["key"] for f in findings if f["open"]],
        "deferred_keys": [f["key"] for f in findings if f["state"] == "deferred"],
        "unpaid_keys": [f["key"] for f in findings if f["state"] == "unpaid"],
    }


@router.get("/review/ledger/vocabulary")
async def ledger_vocabulary(_reader: str = Depends(reader)) -> dict:
    """The closed vocabularies, so a producer can discover them rather than keep a
    copy — ``GET /review/needs-human``'s ``classes`` block, one table over.

    A hardcoded second copy in the harness is the drift this board has already
    paid for over ``provenance``: a state added here and not there is silently
    rejected per entry, and a state spelled differently there is a whole round of
    ledger writes on the floor.
    """
    return {
        "states": list(LIFECYCLE_STATES),
        "sources": list(LIFECYCLE_SOURCES),
        "open_states": sorted(OPEN_STATES),
        "promotable_states": sorted(PROMOTABLE_STATES),
        "reason_required_states": sorted(REASON_REQUIRED_STATES),
        "promotion_source": SOURCE_PROMOTION,
        "max_reason_chars": MAX_LIFECYCLE_REASON_CHARS,
        "max_entries_per_request": MAX_OUTCOMES,
    }
