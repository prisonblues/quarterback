"""Harness dials, set on the board and answered in one call — #305.

``review_panel.fix_severity_floor`` decides which findings a fix pass may touch.
``round_trigger_floor`` decides which ones buy another round. Between them they
decide what a review costs and what it is worth — and until this endpoint existed,
**changing either was a commit on a pull request**, reviewed by the panel those
very dials configure.

That is the wrong shape for a policy knob, and it is how ``.harness-rules.sample``
came to state both floors at P2 while every round of the five run on #299 put P4
findings in ``to_fix`` with ``below_fix_floor`` empty — which cannot happen under a
P2 fix floor. The file that stated the policy and the rounds that applied it
disagreed, for five rounds, four agents and a landed release, because there was no
way to *ask* what the floor was. You could only read a file and hope it was the one
that ran.

**The principle: a dial is a setting.** The repo supplies a DEFAULT, the board
states the value IN FORCE, and the reported answer names which layer produced it.
``harness_rules.py`` documents two layers and both are right for what they are —
the tracked sample is policy on a protected branch, so a poisoned PR cannot rewrite
the rules governing its own review; the per-box overlay is capability, what this
machine can actually run. Neither is a settings channel: policy-as-a-commit cannot
be changed for one run, cannot expire, and cannot be changed at all by anyone not
landing a PR.

## The board does not know what a dial IS

``dial`` is opaque text and ``value`` is opaque JSON. This module does not know
that ``review_panel.max_rounds`` is an integer, that ``fix_severity_floor`` is a
severity band, or that ``reviewers.pi.enabled`` is the one dial an unreviewed
channel may only narrow. It cannot, and it must not want to: the harness ships its
dial table in ``harness/loops/harness_rules.py``, the server image carries no
``harness/`` directory at all (see the Dockerfile's COPY list), and a copy here
would be a **second place a dial is written down** — the exact confusion #56's
rule and #305 exist to end.

So the client owns the vocabulary. It validates every value it reads against the
shape of the built-in default, refuses what it cannot apply, and reports the
refusal by name at every resolution. A dial nobody's harness recognises is stored,
returned and ignored, loudly. ``merge_queue_entries`` made the same choice in the
same words: the board takes testimony, not measurements.

What IS enforced here is only what the board can see for itself — a dial name that
is not blank and not absurdly long, a reason that exists, an expiry in the future,
and a value small enough to store.

## Writes are human-only, and that is the security argument

``harness_rules``' two-ref rule exists so that a poisoned pull request cannot
rewrite the rules governing its own review. A board layer read on the unattended
path is a new door into exactly that, and the honest version of it is named rather
than argued away: **anything running while a branch under review is checked out**
— a test suite, a build step, a git hook — runs as a user whose machine token this
board accepts. If a machine token could set a dial, that code could turn the
``claude`` seat off, or raise the fix floor to P1, on the review of its own change.

So :func:`set_dial` and :func:`clear_dial` take :func:`app.auth.human`, the same
gate the plan's order takes, for a related reason: every agent on a box holds the
same machine token, so nothing inside a request distinguishes one from a person.
A dial is a judgement about what a review is worth, which is a decision; reads are
:func:`app.auth.reader`, because every enrolled agent must be able to resolve.

#276's budget throttle is the constrained case of this layer, not a second one. It
wants the opposite write gate — an automatic governor, holding a machine token,
setting a throttle when the shared five-hour window runs low — and it earns that by
being **narrow-only**: it may move a dial in the cheaper direction and never the
other way. That rule cannot govern a floor (raising ``fix_severity_floor`` from P3
to P2 makes rounds cheaper and coverage thinner; lowering it does the reverse, so
neither direction is the safe one), which is why the general layer is human-gated
and the throttle will bring its own gate, its own direction rule and its own
provider→seat mapping to the same table and the same resolver.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.claims import is_unique_violation
from app.auth import human, reader
from app.db import get_session
from app.models.dial import MAX_DIAL, MAX_REASON, DialSetting

router = APIRouter(tags=["dials"])

#: A dotted path into the harness's rules tree. Deliberately a SHAPE check and
#: not a vocabulary: segments of word characters and hyphens joined by dots, so
#: ``review_panel.escalate_on.premise_repeated`` and ``reviewers.antigravity.enabled``
#: both pass and ``"; drop table"`` does not. What the segments MEAN is the
#: client's business — see the module docstring.
_DIAL_RE = re.compile(r"^[A-Za-z_][\w-]*(\.[A-Za-z_][\w-]*)*$")

#: ``owner/name``, GitHub's own shape. Fleet scope is the empty/absent repo.
_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

#: The largest a stored value may serialise to. A dial is a knob, not a document:
#: 8 KiB is room for a list of title patterns and nowhere near room for somebody
#: using the board as a blob store by accident.
MAX_VALUE_CHARS = 8192


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(ts: datetime | None) -> datetime | None:
    """Postgres hands back aware datetimes; SQLite and hand-built rows may not."""
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _norm_repo(repo: str | None) -> str | None:
    """``None`` for the fleet scope, a validated ``owner/name`` otherwise.

    Blank and absent are the SAME scope on purpose. A query string cannot express
    "absent" reliably — ``?repo=`` arrives as the empty string — and a fleet dial
    that could be written under two different keys is a fleet dial that can be set
    twice and resolved once.
    """
    r = (repo or "").strip()
    if not r:
        return None
    if not _REPO_RE.match(r):
        raise HTTPException(422, detail={
            "error": "repo must be owner/name", "repo": r,
            "hint": "omit it entirely for a dial that applies to every repo"})
    return r


def _live(rows: list[DialSetting], now: datetime) -> list[DialSetting]:
    """Rows that have not been cleared and have not expired.

    An expired dial is ABSENT, not reported-as-expired. #276's requirement is that
    "a resolution with no throttle layer is indistinguishable from one that never
    had it", and the way to mean that is to not return the row — a client that had
    to filter for itself would be a client that could forget to.
    """
    return [r for r in rows
            if r.cleared_at is None
            and (r.expires_at is None or _aware(r.expires_at) > now)]


def _view(row: DialSetting, now: datetime) -> dict:
    expires = _aware(row.expires_at)
    return {
        "dial": row.dial,
        # Unwrapped for the reader. The column stores `{"value": …}` so that the
        # JSON value `null` — the documented off switch for `max_fix_growth`,
        # `distant_merge_lines` and `escalate_on.premise_repeated` — survives a
        # round trip that would otherwise collapse it into SQL NULL.
        "value": row.value.get("value") if isinstance(row.value, dict) else None,
        "scope": "fleet" if row.repo is None else "repo",
        "repo": row.repo,
        "reason": row.reason,
        "set_by": row.set_by,
        "set_at": _aware(row.set_at).isoformat() if row.set_at else None,
        "expires_at": expires.isoformat() if expires else None,
        "expires_in": int((expires - now).total_seconds()) if expires else None,
    }


class DialIn(BaseModel):
    dial: str = Field(min_length=1, max_length=MAX_DIAL)
    #: Any JSON. Absent is NOT the same as ``null``: ``null`` is a value several
    #: dials document as their off switch, so the field is required and the
    #: wrapper in the column keeps the two apart all the way down.
    value: object
    reason: str = Field(min_length=1, max_length=MAX_REASON)
    repo: str | None = None
    #: ISO-8601. Omit for a dial that stays until somebody clears it — which is
    #: what moving a floor for good wants. A temporary one names its own end, so
    #: that a setting cannot outlive its reason with nothing saying it is in force.
    expires_at: datetime | None = None


class ClearIn(BaseModel):
    dial: str = Field(min_length=1, max_length=MAX_DIAL)
    repo: str | None = None


def _check_dial(name: str) -> str:
    d = name.strip()
    if not _DIAL_RE.match(d):
        raise HTTPException(422, detail={
            "error": "a dial is a dotted path into the harness rules",
            "dial": name,
            "hint": "e.g. review_panel.fix_severity_floor, reviewers.pi.enabled. "
                    "The board does not check the NAME against a vocabulary — the "
                    "harness owns that list and reports a name it does not know."})
    return d


@router.get("/dials")
async def list_dials(
    repo: str = Query(default=""),
    _who: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Every dial in force, for one repo or for the fleet.

    With ``repo`` given, returns the repo's own dials AND the fleet-wide ones, each
    labelled with its scope, so one call answers "what is in force here" — the
    acceptance criterion this endpoint exists for. Precedence is the client's to
    apply and it is stated in one line: **a repo dial beats a fleet dial of the
    same name**, and the resolver says which answered.

    Cleared and expired rows are not returned. Nothing here reports history; the
    rows are kept so that "who moved the floor and what did they say" survives, and
    a reader of the live set must not have to step over the dead.
    """
    now = _utcnow()
    scope = _norm_repo(repo)
    q = select(DialSetting).where(DialSetting.cleared_at.is_(None))
    q = q.where(DialSetting.repo.is_(None) if scope is None
                else or_(DialSetting.repo.is_(None), DialSetting.repo == scope))
    rows = _live(list((await session.execute(q)).scalars()), now)
    # Repo before fleet, then by name: the order a reader would write the table in,
    # and the order the client's own precedence walks.
    rows.sort(key=lambda r: (r.repo is None, r.dial))
    return {
        "repo": scope,
        "now": now.isoformat(),
        "dials": [_view(r, now) for r in rows],
    }


@router.post("/dials")
async def set_dial(
    body: DialIn,
    editor: str = Depends(human),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set a dial. **Human-only** — see the module docstring for why.

    Idempotent per (repo, dial): whatever occupies the slot is cleared and the new
    row inserted beside it, so the history of a floor's moves survives and
    ``ix_dial_settings_live`` still guarantees at most one live row per scope.

    The old value comes back in ``replaced``. Moving a floor without being told what
    it was is how a dial gets nudged twice by two people who each believed they were
    setting it from the default.
    """
    now = _utcnow()
    dial = _check_dial(body.dial)
    scope = _norm_repo(body.repo)
    expires = _aware(body.expires_at)
    if expires is not None and expires <= now:
        raise HTTPException(422, detail={
            "error": "expires_at is in the past", "expires_at": expires.isoformat(),
            "hint": "omit it for a dial with no end date; a past one would store a "
                    "setting that is absent the moment it is written"})
    try:
        blob = json.dumps(body.value)
    except (TypeError, ValueError) as e:
        raise HTTPException(422, detail={
            "error": f"value is not JSON-serialisable: {e}", "dial": dial}) from e
    if len(blob) > MAX_VALUE_CHARS:
        raise HTTPException(422, detail={
            "error": "value is too large", "dial": dial, "chars": len(blob),
            "limit": MAX_VALUE_CHARS,
            "hint": "a dial is a knob, not a document"})

    prior = (await session.execute(
        select(DialSetting)
        .where(DialSetting.dial == dial, DialSetting.cleared_at.is_(None))
        .where(DialSetting.repo.is_(None) if scope is None else DialSetting.repo == scope)
    )).scalars().all()
    replaced = [_view(p, now) for p in prior]
    if prior:
        await session.execute(
            update(DialSetting)
            .where(DialSetting.id.in_([p.id for p in prior]))
            .values(cleared_at=now, cleared_by=editor))
    row = DialSetting(repo=scope, dial=dial, value={"value": body.value},
                      reason=body.reason.strip(), set_by=editor, set_at=now,
                      expires_at=expires)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        if is_unique_violation(e):
            # Two humans setting one dial in the same instant. The loser is told to
            # look, not silently overwritten: the point of `replaced` is that
            # nobody moves a floor believing it was at its default.
            raise HTTPException(409, detail={
                "error": "another write took this dial in the same moment",
                "dial": dial, "repo": scope,
                "hint": "GET /dials to see what is in force, then set it again"}) from e
        raise
    return {"dial": _view(row, now), "replaced": replaced, "by": editor}


@router.post("/dials/clear")
async def clear_dial(
    body: ClearIn,
    editor: str = Depends(human),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Take a dial off the board. **Human-only**, like setting one.

    The repo's own default takes over on the next resolution, which is the state a
    repo with no board dial has always been in. Clearing something that is not
    there is not an error — it is the state the caller asked for.
    """
    now = _utcnow()
    dial = _check_dial(body.dial)
    scope = _norm_repo(body.repo)
    rows = (await session.execute(
        select(DialSetting)
        .where(DialSetting.dial == dial, DialSetting.cleared_at.is_(None))
        .where(DialSetting.repo.is_(None) if scope is None else DialSetting.repo == scope)
    )).scalars().all()
    cleared = [_view(r, now) for r in rows]
    if rows:
        await session.execute(
            update(DialSetting)
            .where(DialSetting.id.in_([r.id for r in rows]))
            .values(cleared_at=now, cleared_by=editor))
        await session.commit()
    return {"dial": dial, "repo": scope, "cleared": cleared, "by": editor}
