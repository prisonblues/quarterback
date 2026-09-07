"""The refutation set — what this pull request has already DISPROVED (#773).

``GET /review/refutations`` is the read half. There is no write half, and that is
the design rather than an omission: the two writers already exist and already
refuse a bare verdict.

* ``POST /review/outcomes`` with ``outcome: "refuted"``, which will not record
  without a ``note`` ("refuted needs a note: the refutation is the evidence for
  it").
* ``POST /review/ledger`` with ``state: "refuted"``, which will not record without
  a ``reason`` (:data:`app.finding_lifecycle.REASON_REQUIRED_STATES`), and whose
  ``source`` says who said so — the ``(refuted, judge)`` pair
  :mod:`app.finding_lifecycle` names, as against ``(unpaid, budget)``.

Both were already stored and neither was ever read back. So a seat that raised a
false positive raised it again next round, the judge ruled on it again, and the
fix pass either re-refuted it — which #616 measured as costing MORE than
complying — or complied with something that was wrong. The loop's cheapest path
was to comply with a finding nobody believed.

**What is published is the REASON, not the verdict.** mergeCraft's fix-side brief
is the rule, and it is the reason this endpoint exists rather than a boolean on
``GET /review/findings``: *"Record the reason, not the verdict:* `docstring schema
is not enforced under tests/** — make lint only checks src/ and scripts/` *is
reusable,* `docstring finding was wrong` *is not."* A reusable reason is the only
thing a later reviewer can check against the code in front of it; a verdict is an
appeal to authority, and a reviewer that cannot check it either obeys it blindly
or ignores it. An entry with no reason is therefore not published at all — see
:func:`_reason_of`.

**Per pull request, and there is no parameter that widens it.** Their own brief
names the cost: *"a wrong entry here silently suppresses a real finding later."*
Both ``repo`` and ``pr`` are required and both are matched with ``==``, so a
refutation recorded on #1611 cannot reach #1780 even if the two share a file. That
is the safety valve, and it is spelled as an absent capability rather than as a
default somebody can flip: promoting a refutation to repo-wide is a deliberate act
and #773 does not build it (what it would take is written up in the issue).

**Its own module, on `review_ledger`'s argument.** It reads three tables across
two modules' territory — the outcome table and the findings table from
``reviews``, the ledger from ``review_ledger`` — and belongs wholly to neither. It
imports ``reviews`` in one direction only, exactly as ``review_ledger`` does, so
there is no cycle, and its router is registered beside that one and BEFORE
``reviews``' catch-all ``GET /review/{run_id}``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.reviews import _asked_repo
from app.auth import reader
from app.db import get_session
from app.models.review import (
    ReviewFinding,
    ReviewFindingLedger,
    ReviewFindingOutcome,
    ReviewRun,
)

router = APIRouter(tags=["review"])

#: The state both writers spell, in both vocabularies. One constant because three
#: things test it — the outcome filter, the ledger filter and the source label —
#: and a second spelling of it is a filter that silently matches nothing.
REFUTED = "refuted"

#: How many refutations one read returns. Deliberately far above what any prompt
#: would show: the harness caps the PROMPT separately, and the two caps are
#: different promises. A refutation that falls off THIS end can no longer suppress
#: a re-raise, which is a behaviour change; one that falls off the prompt's end is
#: merely a line a reviewer was not shown. Cutting the wrong one at 8 would make
#: the suppression set depend on how much a prompt could afford.
MAX_REFUTATIONS = 100

#: What ``source`` says for a refutation that came from the outcome table. Not one
#: of :data:`app.finding_lifecycle.LIFECYCLE_SOURCES`, and prefixed so it can never
#: be read as one: the ledger's sources name a WRITER inside a round, and this
#: names a different table with a different unique constraint (one terminal row per
#: defect, no round). A consumer that wants to tell "the fixer proved it wrong" from
#: "the judge dropped it" reads this field, so the two vocabularies must not merge.
SOURCE_OUTCOME = "outcome"
#: The ledger's sources, rendered so they cannot be mistaken for the above.
SOURCE_LEDGER = "ledger:{source}"


def _reason_of(value: object) -> str:
    """One writer's reason, trimmed — or ``""``, which means DO NOT PUBLISH THIS.

    The API refuses a bare ``refuted`` at both doors, so an empty reason here is
    not the ordinary case; it is a row that predates the rule, a row written
    straight to the database, or a reason that was whitespace. Whichever it is,
    the row cannot be published: a refutation with no reason is exactly the
    "docstring finding was wrong" form the whole feature is organised against, and
    handed to a reviewer as binding it suppresses a finding on nobody's argument.

    Dropped rather than published with a placeholder. A placeholder would still
    bind — the harness suppresses on the row's presence, not on its prose — so
    "(no reason recorded)" would be a silent suppression wearing an apology.
    """
    return " ".join(str(value or "").split())


async def _from_outcomes(session: AsyncSession, repo: str, pr: int) -> dict[str, dict]:
    """Every ``refuted`` outcome row on this PR, by finding key.

    One row per defect by the table's own unique constraint, so there is no
    newest-wins pick to make here — ``POST /review/outcomes`` amends in place and
    records the count in ``revisions``. A defect whose outcome was later CHANGED
    away from ``refuted`` is simply not selected, which is the retraction path and
    needs no separate machinery.
    """
    rows = (await session.scalars(
        select(ReviewFindingOutcome).where(
            ReviewFindingOutcome.repo == repo,
            ReviewFindingOutcome.pr == pr,
            ReviewFindingOutcome.outcome == REFUTED,
        )
    )).all()
    out: dict[str, dict] = {}
    for o in rows:
        reason = _reason_of(o.note)
        if not reason:
            continue
        out[o.finding_key] = {
            "reason": reason,
            "source": SOURCE_OUTCOME,
            "round": None,
            "set_by": o.set_by,
            # A CLAIM and never proof — the board cannot authenticate a human, and
            # this field is free text from the same request that carried the
            # refutation. Published for the reason `_outcome_view` publishes it:
            # #77's rule is that an agent must not grade its own findings
            # unattended, and a suppression that rests on an unattested refutation
            # is exactly that rule's subject.
            "attested_by": o.attested_by,
            "ts": (o.updated_at or o.ts).isoformat(),
        }
    return out


async def _from_ledger(session: AsyncSession, repo: str, pr: int) -> dict[str, dict]:
    """Every finding whose CURRENT ledger state is ``refuted``, by key.

    Newest entry per key first, then the state test — the ordering
    ``GET /review/next-door`` argues for at length, and it matters here for the
    same reason in the other direction. Filtered the obvious way (``WHERE state =
    'refuted'`` before the pick) a finding refuted in round 2 and PROMOTED back to
    ``raised`` in round 5 would still be published as refuted, and the promotion —
    the ledger's own mechanism for "this is coming back" — would be invisible to
    the one consumer that acts on it.

    ``(round, id)`` and never ``ts``, on ``GET /review/ledger``'s rule: ``ts`` is
    when the board HEARD about a row, so a retry or a board that was down and
    caught up gives a later round an earlier timestamp.
    """
    rows = (await session.scalars(
        select(ReviewFindingLedger)
        .where(ReviewFindingLedger.repo == repo, ReviewFindingLedger.pr == pr)
        .order_by(ReviewFindingLedger.finding_key, ReviewFindingLedger.round,
                  ReviewFindingLedger.id)
    )).all()
    current: dict[str, ReviewFindingLedger] = {}
    for r in rows:
        current[r.finding_key] = r
    out: dict[str, dict] = {}
    for key, entry in current.items():
        if entry.state != REFUTED:
            continue
        reason = _reason_of(entry.reason)
        if not reason:
            continue
        out[key] = {
            "reason": reason,
            "source": SOURCE_LEDGER.format(source=entry.source),
            "round": entry.round,
            "set_by": None,
            "attested_by": None,
            "ts": entry.ts.isoformat(),
        }
    return out


async def _localities(session: AsyncSession, repo: str, pr: int,
                      keys: set[str]) -> dict[str, ReviewFinding]:
    """The NEWEST observation of each of these defects on this PR — the place the
    refutation is about.

    A refutation is stored against a ``finding_key``, and ``finding_key`` is
    derived deliberately WITHOUT the line (see :class:`ReviewFinding`), so the key
    alone says nothing about where the defect is. The harness matches by locality
    rather than by wording (#771), so it needs the file and the line, and the
    newest observation is the one whose line survived the most fix passes.

    A key with no observation at all is possible — an outcome or a ledger row can
    name a key this board never saw a finding under — and it is left out of this
    map rather than defaulted. The caller publishes such a row with a null file, so
    a reader can tell "nobody recorded where this was" from "line 1".
    """
    if not keys:
        return {}
    rows = (await session.execute(
        select(ReviewFinding, ReviewRun.ts)
        .join(ReviewRun, ReviewFinding.run_id == ReviewRun.id)
        .where(ReviewRun.repo == repo, ReviewRun.pr == pr,
               ReviewFinding.finding_key.in_(keys))
        .order_by(ReviewRun.ts, ReviewFinding.id)
    )).all()
    # Ascending, last wins: the same "newest observation" pick `_from_ledger` makes,
    # written as a dict overwrite rather than a `DISTINCT ON` because the population
    # is bounded by `keys` and is small, and because a `DISTINCT ON` here would have
    # to repeat its ordering in the `ORDER BY` to satisfy Postgres — two places to
    # get one rule right.
    return {f.finding_key: f for f, _ts in rows}


def _view(key: str, entry: dict, seen: ReviewFinding | None) -> dict[str, Any]:
    """One refutation on the wire.

    ``file``/``line`` come from the observation and everything else from the
    writer, because they answer different questions: where the defect WAS, and why
    it is not one. ``locatable`` is the conjunction the harness actually acts on,
    published rather than left to be re-derived — a consumer that computed it from
    a null check would disagree with this one the first time a line arrived as 0.
    """
    line = seen.line if seen is not None else None
    return {
        "key": key,
        # First, because it is the field this endpoint exists to carry.
        "reason": entry["reason"],
        "source": entry["source"],
        "round": entry["round"],
        "set_by": entry["set_by"],
        "attested_by": entry["attested_by"],
        "ts": entry["ts"],
        "file": seen.file if seen is not None else None,
        "line": line,
        "severity": seen.severity if seen is not None else None,
        "title": seen.title if seen is not None else None,
        # Can this bind a MATCHER, as against a reader? A refutation with no file
        # or no line can be shown to a reviewer — the reason is still reusable
        # prose — and it can never be matched by locality, because a finding that
        # names no line matches nothing by `panel_locality.same_finding`'s own
        # rule, including another finding that names no line. Saying so on the row
        # is what lets the consumer report "this one could only be read, not
        # enforced" instead of quietly enforcing nothing.
        "locatable": bool(seen is not None and seen.file
                          and isinstance(line, int) and line >= 1),
    }


@router.get("/review/refutations")
async def pr_refutations(
    _reader: str = Depends(reader),
    repo: str = Query(..., min_length=1, description="github nameWithOwner"),
    pr: int = Query(..., ge=1),
    limit: int = Query(MAX_REFUTATIONS, ge=1, le=MAX_REFUTATIONS),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """What this pull request has already disproved, and WHY (#773).

    Read twice by a round: once before the seats run, so a refuted finding is
    never raised, and again before the judge rules, so a ruling is never spent on
    one that was. A finding never raised costs nothing to judge, nothing to fix,
    and cannot write the next round's findings.

    **The reason is the payload.** Every row carries the sentence its writer
    recorded — ``docstring schema is not enforced under tests/**`` — because that
    is the only form a later reviewer can check against the code in front of it. A
    row whose reason is empty is not returned at all: both write doors refuse a
    bare refutation, and one that got in behind them would suppress a finding on
    nobody's argument.

    **Two writers, kept apart on the wire.** ``source: "outcome"`` is a terminal
    decision somebody recorded about the defect (``POST /review/outcomes``);
    ``source: "ledger:<writer>"`` is a round-stamped one (``POST /review/ledger``),
    and the writer is named because ``ledger:judge`` — somebody adjudicated and
    said no — and ``ledger:human`` are different facts. Where both speak about one
    key the outcome wins, because it is the terminal record and the ledger is a
    per-round observation; the ledger's ``round`` rides along either way when it
    has one.

    **A judge's ``dismissed`` verdict is NOT a refutation and is not selected
    here.** The temptation is real — a dismissal is the judge saying no — and it is
    declined for two reasons that both cut the same way. A verdict is a ruling made
    inside one round, on that round's material, by a party that may not have been
    able to read the code (`GET /review/next-door` documents the case where exactly
    that happened); and nothing ever required it to state a REUSABLE reason, so
    binding it would carry a judge's context failure forward for the life of the
    pull request with no sentence anybody could check it against. A judge that
    means to bind writes a ledger row and states why.

    **Scoped to this pull request, with no way to widen it.** A wrong entry
    silently suppresses a real finding later, so the blast radius is one PR by
    construction rather than by default.

    ``considered`` is the pre-cap count and ``dropped`` the difference, on
    ``GET /review/next-door``'s rule that a cap which trims an answer announces
    itself. Ordering is locatable-first and then newest: a truncated read must lose
    the rows that could only ever have been read before it loses the ones that also
    bind a matcher.
    """
    repo = _asked_repo(repo)

    # The outcome table second in the merge, so it WINS: it is the terminal record
    # of what somebody found out by acting on the defect, where a ledger row is one
    # round's observation. `dict |` is the whole of that rule, stated once.
    merged = (await _from_ledger(session, repo, pr)) | (await _from_outcomes(session, repo, pr))
    seen = await _localities(session, repo, pr, set(merged))

    rows = [_view(key, entry, seen.get(key)) for key, entry in merged.items()]
    # Three stable passes, weakest key first, because Python's sort is stable and
    # the two directions cannot be spelled in one key function — `ts` descends and
    # the others ascend, and negating a string is not a thing. `ts` compares as text
    # and that is the same order as chronologically: every writer above renders
    # `isoformat()` off a timezone-aware UTC column, so the strings are fixed-shape.
    # The key is the last tie-break so two identical reads never differ, which is
    # what lets an operator diff two rounds' prompts and see only the findings move.
    rows.sort(key=lambda r: r["key"])
    rows.sort(key=lambda r: r["ts"], reverse=True)
    rows.sort(key=lambda r: not r["locatable"])
    considered = len(rows)
    rows = rows[:limit]

    return {
        "repo": repo,
        "pr": pr,
        "generated_at": datetime.now(UTC).isoformat(),
        "considered": considered,
        "refutations": rows,
        "dropped": max(0, considered - len(rows)),
        # Said on the wire and not only in the docstring, because both are
        # properties a consumer must implement and neither can be inferred from the
        # rows. The first is the safety valve; the second is what stops a consumer
        # matching on the seat's phrasing, which is the failure #771 fixed.
        "scope": "this pull request only — a refutation recorded here binds no "
                 "other PR, and nothing widens it",
        "contract": "match a refutation to a finding by LOCALITY (file plus line "
                    "span), never by wording; a row with locatable=false can be "
                    "read but cannot be matched",
    }
