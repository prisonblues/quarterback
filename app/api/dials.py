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

## Where a person actually sees one (#477)

For its first releases this endpoint reached **no screen**. A dial was set with
curl and read back by one function in ``harness/loops/panel_seats.py``, so the
value governing every round on the fleet was invisible on ``qb-dash``,
``qb-dash-tui``, ``qb-board`` and the web board alike — tolerable while a dial
configured a review, and not once ``tempo`` (#474) is the answer to "is this fleet
working right now". Three surfaces read it now: both dashboards draw a DIALS panel
off ``GET /dials`` (``harness/bin/qbdata.py``, ``fetch_dials``), and
:func:`app.api.board_view.dials_view` serves the page at ``/dials/view`` that the
dashboards print the URL of — because they cannot write here and say so instead.

## Writes take a person, or an agent a person has delegated to (#591)

**A machine token still cannot set a dial, and that part of the argument is
unchanged.** ``harness_rules``' two-ref rule exists so that a poisoned pull
request cannot rewrite the rules governing its own review, and the threat it
names is real: **anything running while a branch under review is checked out** —
a test suite, a build step, a git hook — runs as a user whose machine token this
board accepts. If a *machine token* could set a dial, that code could turn the
``claude`` seat off, or raise the fix floor to P1, on the review of its own
change. It cannot. :func:`app.auth.delegated` refuses a bare bearer.

What changed in #591 is that a bearer **plus that machine's own
``X-Agent-Elevated`` secret** now passes, because Rich asked for an agent he has
told to turn a dial to be able to turn it. Reads stay :func:`app.auth.reader`,
because every enrolled agent must be able to resolve.

**The residual, stated rather than argued away.** #479 says it plainly for the
credential as a whole — *"any process running as the user can read the secret"* —
and hydrating it to ``/run/op-secrets/quarterback-elevated`` (nix-fleet#50) does
not change that: the file is the user's to read. So the poisoned-PR path above is
not closed, only **lengthened**: that code must now find and read a second
credential rather than reuse the bearer it already has. That is a real reduction
and it is not a proof, and anybody weighing a further tightening should start
from this paragraph rather than from the sentence that used to be here.

**What makes it acceptable is that a dial is reversible and a plan order is
not.** #479's own list of tightenings puts "undo" first for ``/plan/reorder``
precisely because nothing stores the previous order — *"a snapshot before each
reorder turns 'an agent clobbered my order' from a loss into an annoyance"*. A
dial already has that: the row is **cleared, not deleted**, ``expires_at`` bounds
it in time, and ``set_via`` records which door it came through, so a dial an
agent turned is legible as such on ``GET /dials`` and on the page. The exclusion
was argued on blast radius, and the blast radius here is one named key whose last
value survives its own replacement.

**Provenance is the thing that must not be lost**, and it is the reason this
takes :func:`app.auth.delegated` rather than lending an agent a person's key.
A delegated caller keeps its own identity: ``set_by`` records
``hermes/mist-harbour``, never ``human/rich``, and ``set_via`` records
``agent``. That is ``rank_source: "derived"`` applied to the same problem — the
lesson of #183 and of the design #479 records as rejected, where an agent that
borrowed a person's cookie became indistinguishable from them in the history.

**Two of the three exclusions stand.** ``POST /plan/scope`` and ``exempt``'s
``grant: true`` remain :func:`app.auth.human`-only; only the dial one was
reversed, and each still has its test in ``tests/test_delegated_writes.py``.

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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.claims import is_unique_violation
from app.auth import AUTH_AGENT, delegated, human_method, reader
from app.claimkey import BadRef, canonical_repo
from app.db import get_session
from app.models.dial import MAX_DIAL, MAX_REASON, DialSetting

router = APIRouter(tags=["dials"])

#: A dotted path into the harness's rules tree. Deliberately a SHAPE check and
#: not a vocabulary: segments of word characters and hyphens joined by dots, so
#: ``review_panel.escalate_on.premise_repeated`` and ``reviewers.antigravity.enabled``
#: both pass and ``"; drop table"`` does not. What the segments MEAN is the
#: client's business — see the module docstring.
_DIAL_RE = re.compile(r"^[A-Za-z_][\w-]*(\.[A-Za-z_][\w-]*)*$")

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
    """``None`` for the fleet scope, a canonical ``owner/name`` otherwise.

    Blank and absent are the SAME scope on purpose. A query string cannot express
    "absent" reliably — ``?repo=`` arrives as the empty string — and a fleet dial
    that could be written under two different keys is a fleet dial that can be set
    twice and resolved once.

    **The case is folded, and that argument is the same one one line down** (#350).
    This used to check the shape with a regex of its own and stop there — the only
    validator on the board that checked a repo's shape without folding its case —
    while ``merge_queue`` cites the hazard for its own column three files away.
    GitHub treats owner and repository names case-insensitively and preserves what
    you typed, so ``Acme/X`` and ``acme/x`` are one repository, and
    ``ix_dial_settings_live`` is UNIQUE over ``COALESCE(repo,'')`` and ``dial``:
    two spellings could each hold a **live row for the same dial**, which is two
    answers to a settings question that has one. ``harness_rules.detect_github``
    reads the repo off the origin remote and preserves its capitals, so which of
    the two a resolution saw depended on how the remote was spelled.

    :func:`app.claimkey.canonical_repo` is the fold, and it brings the board's one
    shape rule with it: the local regex admitted ``a_b/c.git`` and a bare
    ``quarterback`` was already refused, so what changes for a caller is that the
    refusal is now the board's own :data:`app.claimkey.REPO_SHAPE` message, and
    dial scopes are spelled the way claim keys, plan scopes and review rows are.
    """
    r = (repo or "").strip()
    if not r:
        return None
    try:
        return canonical_repo(r)
    except BadRef as e:
        raise HTTPException(422, detail={
            "error": str(e), "repo": r,
            "hint": "omit it entirely for a dial that applies to every repo"}) from e


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


def _refuse_displacing_a_person(request: Request, rows: list[DialSetting]) -> None:
    """An agent may not clear or overwrite a dial a PERSON set. #591's second pass.

    The first pass guarded the wrong half. `qb-start` refuses a `spawn.max_sessions`
    row whose `set_via` is `agent`, which stops an agent writing itself a bigger
    number — and does nothing about an agent DELETING the smaller one somebody
    else wrote. A person sets the ceiling to 2, the policy file says 8, an agent
    clears the dial, and `ceilings_from_board` reports no board ceiling, so the
    file's 8 applies. The agent has raised its own ceiling to 8 without ever
    getting an agent-authored value accepted, and `spawn.max_sessions_fleet` is
    worse because it has no file fallback at all: clearing it removes the fleet
    runaway guard outright. Found by an adversarial review, not by these tests,
    which only ever built an isolated agent row.

    Closed HERE rather than in `qb-start`, because absence is exactly what the
    reader cannot interpret: "nobody has set one" and "an agent removed the one
    somebody set" are the same empty answer by the time it gets there.

    **Stated without a vocabulary**, which is why it is this rule and not a list of
    protected dial names. This module does not know what a dial IS — `dial` is
    opaque text and the harness owns the meaning (see the module docstring), and a
    table of special names here would be the second place a dial is written down
    that #56 and #305 exist to prevent. "An agent may not overwrite a person's
    decision" needs no such table and covers `reviewers.*.enabled` and the fix
    floors on the same terms as the ceilings.

    A person is anything that is not `agent`, NULL included: a row older than the
    column was written when only `human()` could write one.
    """
    if human_method(request) != AUTH_AGENT:
        return
    theirs = [r for r in rows if r.set_via != AUTH_AGENT]
    if not theirs:
        return
    raise HTTPException(403, detail={
        "error": "this dial was set by a person, and a delegated agent may not "
                 "replace or clear one",
        "dial": theirs[0].dial,
        "repo": theirs[0].repo,
        "set_by": theirs[0].set_by,
        "set_via": theirs[0].set_via,
        "hint": "an agent may set a dial nobody has set, and may replace its own. "
                "Overwriting a person's is a decision, not an application of one — "
                "ask them to change it, or to clear it first. Clearing is refused "
                "for the same reason it is allowed otherwise: removing a value is "
                "how a ceiling gets raised without writing a bigger number."})


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
        # HOW they proved it, beside who they were. `null` is "not recorded" — a
        # row older than the column — and never "some other method": see the
        # model. A reader deciding how much weight to put on a dial's provenance
        # needs the two kept apart, which is the whole reason it is stored.
        "set_via": row.set_via,
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
    request: Request,
    body: DialIn,
    editor: str = Depends(delegated),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set a dial. **A person, or an agent one has delegated to** (#591) — see the
    module docstring for what that does and does not open.

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
    reason = body.reason.strip()
    if not reason:
        # Pydantic's `min_length` counts characters, and `"   "` has three. The
        # database refuses a blank reason (`ck_dial_settings_reason`), so without
        # this the natural mistake comes back as a 500 from the commit instead of a
        # 422 naming the field — and a dial whose argument was never written down is
        # one nobody can later decide to remove.
        raise HTTPException(422, detail={
            "error": "a dial needs a reason", "dial": dial,
            "hint": "why is this value in force? A dial nobody can read an argument "
                    "for is a dial nobody can decide to remove"})
    try:
        # `allow_nan=False`: json.dumps emits the JavaScript literals `NaN` and
        # `Infinity` by default and Postgres JSONB refuses both, so a float dial set
        # to one would pass every check here and fail at the commit as a 500.
        blob = json.dumps(body.value, allow_nan=False)
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
    _refuse_displacing_a_person(request, list(prior))
    replaced = [_view(p, now) for p in prior]
    if prior:
        await session.execute(
            update(DialSetting)
            .where(DialSetting.id.in_([p.id for p in prior]))
            .values(cleared_at=now, cleared_by=editor,
                    cleared_via=human_method(request)))
    row = DialSetting(repo=scope, dial=dial, value={"value": body.value},
                      reason=reason, set_by=editor, set_at=now, expires_at=expires,
                      set_via=human_method(request))
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
    request: Request,
    body: ClearIn,
    editor: str = Depends(delegated),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Take a dial off the board. **Same gate as setting one**, deliberately.

    A dial an agent can set but not clear is a trap: the reversal is the half that
    makes the write safe to have granted, and #479's standard for this credential
    is reversibility rather than prevention. Splitting the two gates would leave an
    agent able to make a change only a person could undo.

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
    _refuse_displacing_a_person(request, list(rows))
    cleared = [_view(r, now) for r in rows]
    if rows:
        await session.execute(
            update(DialSetting)
            .where(DialSetting.id.in_([r.id for r in rows]))
            .values(cleared_at=now, cleared_by=editor,
                    cleared_via=human_method(request)))
        await session.commit()
    return {"dial": dial, "repo": scope, "cleared": cleared, "by": editor}
