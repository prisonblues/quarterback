"""Reviewer-panel stats (v2.10, per-reviewer accounts in v2.11).

``~/.claude/loops/panel.py`` reviews one PR diff with several vendor models at
once and has a master judge rule each deduped finding real or not. That is a
controlled comparison — same diff, same judge, different models — and it was
being discarded every run. ``POST /review`` records it; ``GET /review/stats``
aggregates it into the two answers worth having:

* **which reviewer finds the most real issues** — confirmed counts and, more
  usefully, *solo* counts: findings nobody else on the panel raised.
* **is the higher tier worth it** — precision (confirmed vs dismissed) per
  (reviewer, model, effort), so the same vendor at two tiers competes with itself.

Precision is only counted over **judged** runs. When the judge is skipped the
panel keeps every finding unadjudicated, and scoring those as correct would
flatter whichever reviewer was noisiest that day.

The ingest payload is ``panel.py --json`` as-is plus a small envelope, so the
panel needs no bespoke serialiser and the two can't drift apart.

**v2.11 — what each reviewer said, and which defect it was.** A finding used to
be one title, one detail and a list of reviewer *names*, because the panel
merged before the judge and kept one member's text; "codex and pi both reported
this" was recorded but not what either of them said. ``reported_by`` now carries
each reporter's verbatim account with its own severity and line
(``review_finding_reports``), so merging is additive and severity calibration
against the judge is answerable. Each finding also carries a ``key`` — the
identity of the *defect*, not of the observation — so the same bug seen in run 3
and again in run 7 stays two rows that can be joined (``GET /review/findings``),
which is what makes "was it actually fixed?" a query. The older payload shape
(``reviewers: ["codex", "pi"]``, no key) still records exactly as before.

**v2.14 — rounds, and what a run could not see.** Two runs of a PR were two
unrelated records: nothing said which was the re-review of the other's fix, what
this round found that the last had not, or what stopped the loop. And a run said
only what was *found* — a reviewer handed a prefix of the diff, one that never
ran, and one that had nothing to say all recorded the same zero.

A run now carries its ``round``, ``new_findings``, ``stop_reason`` and
``stop_confident``; a member carries ``could_not_assess`` (its own declaration)
alongside the panel-measured ``truncated``; a finding carries ``needs_rereview``,
per reporter. Together those make the review reviewable: whether a clean verdict
was *earned* is on the row rather than in a transcript, and a reviewer that says
"I could not assess X" and turns out to be right becomes distinguishable from one
that silently reported clean — see ``GET /review/findings``, which checks each
re-review flag against what the following round actually found. Payloads without
any of it record exactly as before, as round 1 with nothing declared.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import identify, reader
from app.db import get_session
from app.identity import agent_row, compose, machine_of
from app.models.review import ReviewFinding, ReviewFindingReport, ReviewReviewer, ReviewRun

router = APIRouter(tags=["review"])


async def _authored_as(session: AsyncSession, author: str) -> str:
    """The spelling a run was recorded under, for an ``author=`` filter.

    Runs store the agent's name, so a filter written from a `whoami` alias (the
    key form) has to be translated or it silently matches nothing. Unlike
    addressing, this resolves to the name even for a retired agent: the question
    is "what did it author under", and the answer does not change when it goes.

    Which means a name that has since been recycled attributes both holders'
    runs to one filter. That is a property of storing names in history, not of
    the translation — a filter written with the name directly does the same —
    and telling them apart would need a tenure log this doesn't keep.
    """
    row = await agent_row(session, author)
    return compose(machine_of(author), row.name) if row is not None else author


SEVERITIES = ("P1", "P2", "P3", "P4")

_INT32 = 2_147_483_647


def _line_or_none(v: int | None) -> int | None:
    """A line number the column cannot hold is no line number.

    Recording is best-effort for the panel — a review must never fail because
    the board choked — so a garbled line is dropped rather than costing the run
    its whole record, which is what both a 422 here and the driver's error on
    an out-of-range INTEGER would do.
    """
    return v if v is None or -_INT32 - 1 <= v <= _INT32 else None


# ----------------------------------------------------------------- ingest models

class ReportIn(BaseModel):
    """One reviewer's own account of a finding, before the judge merged it.

    ``severity``/``line`` are that reviewer's, not the judge's: the difference is
    the calibration signal, so they are stored rather than reconciled.
    """

    model_config = ConfigDict(populate_by_name=True)

    reviewer: str
    severity: str | None = None
    line: int | None = None
    #: Verbatim. ``detail`` is accepted as an alias because that is what the
    #: panel calls the same text on an unmerged finding.
    account: str = Field(default="", validation_alias=AliasChoices("account", "detail"))
    #: This reviewer declared the FIX for this finding needs re-reading.
    needs_rereview: bool = False

    @field_validator("reviewer")
    @classmethod
    def _trim(cls, v: str) -> str:
        return v.strip()

    @field_validator("line")
    @classmethod
    def _line(cls, v: int | None) -> int | None:
        return _line_or_none(v)


class FindingIn(BaseModel):
    """One merged finding, exactly as ``panel.py --json`` serialises it.

    The aliases follow the same rule as :class:`ReviewIn`'s — take the panel's
    words rather than making it translate. The judge's merged statement is
    ``synthesis`` in its canonical shape and ``title`` in the older one; both
    land here, because a renamed field would otherwise fail silently into a null
    column rather than erroring.
    """

    model_config = ConfigDict(populate_by_name=True)

    severity: str = "P3"
    file: str | None = None
    line: int | None = None
    title: str = Field(default="", validation_alias=AliasChoices("title", "synthesis"))
    detail: str = ""
    reviewers: list[str] = Field(default_factory=list)
    reason: str = Field(default="", validation_alias=AliasChoices("reason", "rationale"))

    #: Per-reviewer accounts. Supersedes ``reviewers`` (the names are implied by
    #: it) but does not replace it: a panel may list a member that contributed no
    #: text, and every older panel sends names only.
    reported_by: list[ReportIn] = Field(default_factory=list)

    #: The panel's id for this finding *within this run* (e.g. ``"1609-F03"``).
    #: Used only to resolve ``related`` into finding keys — never as the defect's
    #: identity, because the numbering restarts every run.
    id: str | None = None
    #: A stable identity for the defect, if the caller has one. Wins over the
    #: derived key, which is a best-effort fallback (see :func:`_derive_key`).
    key: str | None = None
    #: ``id``s of other findings in this payload that share a cause.
    related: list[str] = Field(default_factory=list)

    #: A reporter declared that fixing this takes a structural change whose result
    #: should be re-read. ``rereview_by`` names which members said so, for a panel
    #: that merges before it can send per-reporter accounts; where ``reported_by``
    #: carries its own flags those win, being the finer grain.
    needs_rereview: bool = False
    rereview_by: list[str] = Field(default_factory=list)
    #: No earlier round of this PR raised this. The panel computes it against the
    #: baseline it was given; None means it was not asked to.
    new_this_round: bool | None = None

    @field_validator("line")
    @classmethod
    def _line(cls, v: int | None) -> int | None:
        return _line_or_none(v)


class ReviewerIn(BaseModel):
    """A panel member as configured for this run — its brain, not its findings,
    plus what it declared about its own coverage."""

    model: str | None = None
    effort: str | None = None
    ran: bool = True
    skip: str | None = None
    max_diff_chars: int | None = None
    truncated: bool | None = None
    duration_ms: int | None = None
    #: Areas it could not judge. None = not asked (every panel before v2.14);
    #: [] = asked, and it had nothing to declare. The two must not collapse, or a
    #: reviewer that was never given the chance to say reads as one that had
    #: nothing to say.
    could_not_assess: list[str] | None = None


class StopIn(BaseModel):
    """The panel's mechanical verdict on whether the loop should go again."""

    stop: bool = True
    reason: str = ""
    #: Whether stopping was convergence. False when the round was capped, or a
    #: reviewer was truncated / absent / unparsed / declaring a gap — the cases
    #: where "no new findings" is a fact about the panel, not about the code.
    confident: bool = False
    veto: list[str] = Field(default_factory=list)


class ReviewIn(BaseModel):
    """The panel's ``--json`` payload, accepted verbatim.

    The aliases exist so the panel needs no bespoke serialiser for the board: it
    calls the repo's GitHub slug ``github`` (``repo`` there is the *local*
    checkout name) and the PR's subject ``title``. Taking its words rather than
    making it translate is what keeps the two from drifting — a renamed field
    would otherwise fail silently into a null column.
    """

    model_config = ConfigDict(populate_by_name=True)

    repo: str = Field(min_length=1, validation_alias=AliasChoices("github", "repo"),
                      description="github nameWithOwner")
    pr: int = Field(ge=1)
    pr_title: str | None = Field(default=None,
                                 validation_alias=AliasChoices("pr_title", "title"))
    base: str | None = None
    changed_lines: int | None = None
    diff_chars: int | None = None
    diff_truncated: bool | None = None

    judged: bool = False
    judge_model: str | None = None
    judge_skip: str | None = None
    coverage_note: str | None = None
    sonar_gate: str | None = None
    ci_status: str | None = None

    #: Where this run sat in the panel -> fix -> panel cycle. Absent = round 1,
    #: which is what every pre-v2.14 run was.
    round: int = Field(default=1, ge=1)
    new_findings: int | None = Field(default=None, ge=0)
    #: The stopping rule's own account of itself. ``stop_reason`` is accepted flat
    #: as well, because the panel prints it both ways and a caller reproducing the
    #: payload by hand should not have to nest one string.
    round_stop: StopIn | None = None
    stop_reason: str | None = None

    reviewers_selected: list[str] = Field(default_factory=list)
    reviewers_override: str | None = None
    skipped: list[str] = Field(default_factory=list)
    #: Per-member config keyed by vendor name. Optional: an older panel sends
    #: none and the members are inferred from finding attribution, with no model
    #: recorded — a run that can still be counted, just not tiered.
    reviewers: dict[str, ReviewerIn] = Field(default_factory=dict)

    to_fix: list[FindingIn] = Field(default_factory=list)
    dismissed: list[FindingIn] = Field(default_factory=list)
    sonar_findings: list[FindingIn] = Field(default_factory=list)

    session: str | None = None
    run_key: str | None = Field(
        default=None,
        description="idempotency key; re-POSTing the same key returns the first run",
    )


def _verdict(f: FindingIn, judged: bool) -> str:
    """Where a to_fix finding sits: adjudicated real, or merely never judged.

    The panel marks the latter with ``reason='unjudged'`` and keeps it (it never
    suppresses on a missing verdict), so the two arrive in the same list.
    """
    return "confirmed" if judged and f.reason != "unjudged" else "unjudged"


_NOT_WORD = re.compile(r"[^a-z0-9]+")


def _derive_key(file: str | None, title: str) -> str:
    """A defect identity for a caller that has none of its own.

    File plus a normalised title, and deliberately **not** the line: the line
    moves when the fix above it lands, and an identity that moves links nothing.
    Best-effort by nature — a judge that rewords its synthesis between runs
    breaks the chain, which is why an explicit ``key`` always wins.

    Duplicated as SQL in migration 0012 to backfill pre-v2.11 rows; the two must
    stay identical or old runs join no chain.
    """
    norm = _NOT_WORD.sub(" ", title.lower()).strip()
    return hashlib.md5(f"{file or ''}|{norm}".encode(), usedforsecurity=False).hexdigest()[:16]


@dataclass(slots=True)
class Prepared:
    """A finding with its ingest-time derivations settled once, up front."""

    f: FindingIn
    verdict: str
    #: What gets stored, which is also what the key is derived from — a title
    #: defaulted at storage time but keyed before it would put an untitled
    #: finding in a different chain from the backfilled ones.
    title: str
    #: Every member credited, in payload order: ``reviewers`` then any reporter
    #: only ``reported_by`` names.
    reviewers: list[str]
    reports: list[ReportIn]
    key: str
    related: list[str] = field(default_factory=list)
    #: Members that declared this finding's fix worth re-reading, finest grain
    #: first: each reporter's own flag, then the panel's ``rereview_by``, then —
    #: when the finding is flagged with no attribution at all — everyone credited
    #: on it, which over-credits but never silently drops the declaration.
    rereview_by: list[str] = field(default_factory=list)


def _prepare(findings: list[tuple[FindingIn, str]]) -> list[Prepared]:
    """Settle attribution, defect key and ``related`` links for one payload.

    Attribution is unioned rather than chosen: ``reported_by`` is authoritative
    about *what* was said, but a panel may still list a member alongside it that
    contributed no text, and dropping that member would silently un-credit it.
    """
    prepared: list[Prepared] = []
    for f, verdict in findings:
        reports: list[ReportIn] = []
        seen: set[str] = set()
        for r in f.reported_by:
            # Two accounts from one reviewer would violate the table's
            # (finding, reviewer) uniqueness; the first is kept rather than the
            # request being rejected, since ingest is best-effort for the panel.
            if r.reviewer and r.reviewer not in seen:
                seen.add(r.reviewer)
                reports.append(r)

        reviewers = [n for n in dict.fromkeys(x.strip() for x in f.reviewers) if n]
        reviewers += [r.reviewer for r in reports if r.reviewer not in reviewers]

        title = f.title or "(untitled)"
        # Who declared the fix worth re-reading, finest grain first. A reporter
        # that sent an account is authoritative about ITSELF — including its
        # silence, so `rereview_by` may only fill in for members that sent none.
        # Reading a member's own `false` as "no data" and then crediting it from
        # the coarser list would manufacture a declaration it did not make.
        named = {r.reviewer for r in reports}
        flagged = [r.reviewer for r in reports if r.needs_rereview]
        flagged += [n for n in f.rereview_by if n in reviewers and n not in named]
        # A finding flagged with no attribution at all: credit everyone credited
        # on it. Over-crediting is visible and correctable; dropping the
        # declaration is neither.
        if not flagged and f.needs_rereview and not named and not f.rereview_by:
            flagged = list(reviewers)
        prepared.append(Prepared(
            f=f,
            verdict=verdict,
            title=title,
            reviewers=reviewers,
            reports=reports,
            key=(f.key or "").strip() or _derive_key(f.file, title),
            rereview_by=flagged,
        ))

    # `related` arrives as the panel's run-local ids; stored as keys so the
    # links survive the run they were made in. A ref to something not in this
    # payload names nothing that can be linked, so it is dropped.
    by_id = {p.f.id: p.key for p in prepared if p.f.id}
    for p in prepared:
        p.related = sorted({by_id[r] for r in p.f.related if r in by_id} - {p.key})
    return prepared


def _calibration(own: str | None, judged: str | None) -> str | None:
    """Which way this reviewer's severity missed the judge's, if it can be told.

    ``P1 < P2`` lexically and P1 is the more severe, so a reviewer whose severity
    sorts *before* the judge's called it worse than it was.
    """
    a, b = (own or "").upper(), (judged or "").upper()
    if a not in SEVERITIES or b not in SEVERITIES:
        return None
    if a == b:
        return "sev_agree"
    return "sev_stricter" if a < b else "sev_looser"


def _scorecards(
    findings: list[Prepared],
    cfg: dict[str, ReviewerIn],
    selected: list[str],
    skipped: list[str],
) -> list[ReviewReviewer]:
    """Tally each panel member from the findings it is credited on.

    Derived here rather than sent, so a scorecard cannot contradict the findings
    it summarises. Members that ran but found nothing, and members that never
    ran at all, still get a row — a zero is data and a silent absence isn't.

    Without a ``reviewers`` block (an older panel) a member is assumed to have
    run unless it appears in ``skipped``, whose entries read ``"codex: CLI
    absent"``. Assuming the opposite would file every quiet reviewer as broken.
    """
    credited = {r for p in findings for r in (*p.reviewers, *p.rereview_by)}
    skips = {s.split(":", 1)[0].strip(): s for s in skipped if ":" in s}
    names = sorted(set(cfg) | set(selected) | credited)

    # Tallied as plain counters first: a column ``default=0`` is applied at
    # flush, so incrementing a freshly-constructed ORM object would start from
    # None.
    zero = ("raised", "confirmed", "dismissed", "unjudged", "solo", "shared",
            "sev_stricter", "sev_agree", "sev_looser", "rereview_flagged",
            *(s.lower() for s in SEVERITIES))
    tally: dict[str, dict[str, int]] = {n: dict.fromkeys(zero, 0) for n in names}
    for p in findings:
        own = {r.reviewer: r for r in p.reports}
        for name in p.rereview_by:
            tally[name]["rereview_flagged"] += 1
        for name in p.reviewers:
            t = tally[name]
            t["raised"] += 1
            if len(p.reviewers) > 1:
                t["shared"] += 1
            if p.verdict == "confirmed":
                t["confirmed"] += 1
                if len(p.reviewers) == 1:
                    t["solo"] += 1
                sev = (p.f.severity or "").upper()
                if sev in SEVERITIES:
                    t[sev.lower()] += 1
                # Calibration only over confirmed findings: on a dismissal the
                # recorded severity is the panel's own, so comparing a reviewer
                # against it would be comparing it to itself.
                bucket = _calibration(own[name].severity, sev) if name in own else None
                if bucket:
                    t[bucket] += 1
            elif p.verdict in ("dismissed", "unjudged"):
                t[p.verdict] += 1

    cards = []
    for name in names:
        c = cfg.get(name)
        skip = c.skip if c else skips.get(name)
        cards.append(ReviewReviewer(
            name=name,
            model=(c.model or None) if c else None,
            effort=(c.effort or None) if c else None,
            ran=c.ran if c else skip is None,
            skip_reason=skip,
            max_diff_chars=c.max_diff_chars if c else None,
            truncated=c.truncated if c else None,
            duration_ms=c.duration_ms if c else None,
            could_not_assess=c.could_not_assess if c else None,
            **tally[name],
        ))
    return cards


@router.post("/review", status_code=status.HTTP_201_CREATED)
async def record_review(
    body: ReviewIn,
    author: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record one panel run: the run, a scorecard per member, every finding.

    Best-effort from the caller's side — the panel must never fail a review
    because the board was down — so this stays cheap and idempotent on
    ``run_key``.
    """
    if body.run_key:
        existing = await session.scalar(
            select(ReviewRun).where(ReviewRun.run_key == body.run_key)
        )
        if existing is not None:
            return {"id": existing.id, "recorded": False, "reason": "duplicate run_key"}

    findings = _prepare(
        [(f, _verdict(f, body.judged)) for f in body.to_fix]
        + [(f, "dismissed") for f in body.dismissed]
        + [(f, "sonar") for f in body.sonar_findings]
    )
    counts = {v: sum(1 for p in findings if p.verdict == v) for v in
              ("confirmed", "dismissed", "unjudged", "sonar")}

    run = ReviewRun(
        author=author,
        session=body.session,
        repo=body.repo,
        pr=body.pr,
        pr_title=body.pr_title,
        base_branch=body.base,
        changed_lines=body.changed_lines,
        diff_chars=body.diff_chars,
        diff_truncated=body.diff_truncated,
        judged=body.judged,
        judge_model=body.judge_model or None,
        judge_skip=body.judge_skip,
        coverage_note=body.coverage_note or None,
        round=body.round,
        new_findings=body.new_findings,
        # The nested verdict wins over the flat string: it is the one that also
        # carries whether the stop was earned, and a payload sending both sends
        # the same reason twice.
        stop_reason=(body.round_stop.reason if body.round_stop else body.stop_reason) or None,
        stop_confident=body.round_stop.confident if body.round_stop else None,
        sonar_gate=body.sonar_gate,
        ci_status=body.ci_status,
        reviewers_selected=body.reviewers_selected or None,
        reviewers_override=body.reviewers_override,
        skipped=body.skipped or None,
        n_confirmed=counts["confirmed"],
        n_dismissed=counts["dismissed"],
        n_unjudged=counts["unjudged"],
        n_sonar=counts["sonar"],
        run_key=body.run_key,
    )
    session.add(run)
    await session.flush()  # need run.id for the children

    # Sonar's hard-gate issues are the gate's own output, not a panel member's
    # judged findings — excluded from the scorecards so they can't inflate a
    # precision the judge never ruled on.
    scored = [p for p in findings if p.verdict != "sonar"]
    for card in _scorecards(scored, body.reviewers, body.reviewers_selected, body.skipped):
        card.run_id = run.id
        session.add(card)

    rows = [
        (
            ReviewFinding(
                run_id=run.id,
                verdict=p.verdict,
                severity=(p.f.severity or "").upper() or None,
                file=p.f.file,
                line=p.f.line,
                title=p.title,
                detail=p.f.detail or None,
                reason=p.f.reason or None,
                finding_key=p.key,
                related=p.related or None,
                reviewers=p.reviewers or None,
                n_reviewers=len(p.reviewers),
                needs_rereview=bool(p.rereview_by) or p.f.needs_rereview,
                new_this_round=p.f.new_this_round,
            ),
            p.reports,
        )
        for p in findings
    ]
    for finding, _ in rows:
        session.add(finding)
    if rows:
        await session.flush()  # need finding.id for the accounts hanging off it

    accounts = 0
    # Zipped with the prepared findings rather than looked up by key: two findings
    # in one payload can share a defect key (the same title raised and dismissed),
    # and a lookup would then hand one finding's declarations to the other.
    for p, (finding, reports) in zip(findings, rows, strict=True):
        for r in reports:
            accounts += 1
            session.add(
                ReviewFindingReport(
                    finding_id=finding.id,
                    reviewer=r.reviewer,
                    severity=(r.severity or "").upper() or None,
                    line=r.line,
                    account=r.account or None,
                    # A flag the panel attributed via `rereview_by` belongs on the
                    # reporter's row too — same declaration, arriving by the only
                    # channel a panel that merges before the judge still has.
                    needs_rereview=r.needs_rereview or r.reviewer in p.rereview_by,
                )
            )

    await session.commit()
    return {"id": run.id, "recorded": True, "findings": len(findings), "accounts": accounts}


# ------------------------------------------------------------------ read paths

def _run_view(r: ReviewRun) -> dict:
    return {
        "id": r.id,
        "ts": r.ts.isoformat(),
        "author": r.author,
        "session": r.session,
        "repo": r.repo,
        "pr": r.pr,
        "pr_title": r.pr_title,
        "base": r.base_branch,
        "changed_lines": r.changed_lines,
        "diff_chars": r.diff_chars,
        "diff_truncated": r.diff_truncated,
        "judged": r.judged,
        "judge_model": r.judge_model,
        "judge_skip": r.judge_skip,
        "coverage_note": r.coverage_note,
        "round": r.round,
        "new_findings": r.new_findings,
        "stop_reason": r.stop_reason,
        "stop_confident": r.stop_confident,
        "sonar_gate": r.sonar_gate,
        "ci_status": r.ci_status,
        "reviewers_selected": r.reviewers_selected or [],
        "reviewers_override": r.reviewers_override,
        "skipped": r.skipped or [],
        "confirmed": r.n_confirmed,
        "dismissed": r.n_dismissed,
        "unjudged": r.n_unjudged,
        "sonar": r.n_sonar,
    }


def _card_view(c: ReviewReviewer) -> dict:
    return {
        "name": c.name,
        "model": c.model,
        "effort": c.effort,
        "ran": c.ran,
        "skip_reason": c.skip_reason,
        "max_diff_chars": c.max_diff_chars,
        "truncated": c.truncated,
        "duration_ms": c.duration_ms,
        "could_not_assess": c.could_not_assess,
        "rereview_flagged": c.rereview_flagged,
        "raised": c.raised,
        "confirmed": c.confirmed,
        "dismissed": c.dismissed,
        "unjudged": c.unjudged,
        "solo": c.solo,
        "shared": c.shared,
        "sev_stricter": c.sev_stricter,
        "sev_agree": c.sev_agree,
        "sev_looser": c.sev_looser,
        "p1": c.p1, "p2": c.p2, "p3": c.p3, "p4": c.p4,
    }


def _report_view(r: ReviewFindingReport) -> dict:
    return {
        "reviewer": r.reviewer,
        "severity": r.severity,
        "line": r.line,
        "account": r.account,
        "needs_rereview": r.needs_rereview,
    }


def _finding_view(f: ReviewFinding, reports: list[ReviewFindingReport]) -> dict:
    return {
        "key": f.finding_key,
        "verdict": f.verdict,
        "severity": f.severity,
        "file": f.file,
        "line": f.line,
        "title": f.title,
        "detail": f.detail,
        "reason": f.reason,
        "reviewers": f.reviewers or [],
        "related": f.related or [],
        "needs_rereview": f.needs_rereview,
        "new_this_round": f.new_this_round,
        "reported_by": [_report_view(r) for r in reports],
    }


async def _reports_by_finding(
    session: AsyncSession, finding_ids: list[int]
) -> dict[int, list[ReviewFindingReport]]:
    """Every account for these findings, in one query rather than N."""
    if not finding_ids:
        return {}
    rows = (await session.scalars(
        select(ReviewFindingReport)
        .where(ReviewFindingReport.finding_id.in_(finding_ids))
        .order_by(ReviewFindingReport.reviewer)
    )).all()
    out: dict[int, list[ReviewFindingReport]] = {}
    for r in rows:
        out.setdefault(r.finding_id, []).append(r)
    return out


def _since_clause(since: str | None, days: int | None):
    """``since`` as an ISO instant, or ``days`` as a lookback. Neither = all time."""
    if since:
        try:
            ts = datetime.fromisoformat(since)
        except ValueError as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"since={since!r} is not an ISO timestamp"
            ) from e
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts
    if days:
        return datetime.now(UTC) - timedelta(days=days)
    return None


@router.get("/reviews")
async def list_reviews(
    _reader: str = Depends(reader),
    repo: str | None = Query(None),
    pr: int | None = Query(None),
    author: str | None = Query(None),
    since: str | None = Query(None, description="ISO timestamp"),
    days: int | None = Query(None, ge=1, le=3650),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Recorded panel runs, newest first."""
    stmt = select(ReviewRun)
    if repo is not None:
        stmt = stmt.where(ReviewRun.repo == repo)
    if pr is not None:
        stmt = stmt.where(ReviewRun.pr == pr)
    if author is not None:
        author = await _authored_as(session, author)
        stmt = stmt.where(ReviewRun.author == author)
    cutoff = _since_clause(since, days)
    if cutoff is not None:
        stmt = stmt.where(ReviewRun.ts >= cutoff)
    stmt = stmt.order_by(ReviewRun.ts.desc(), ReviewRun.id.desc()).limit(limit)

    runs = list((await session.scalars(stmt)).all())
    if not runs:
        return []
    cards = list(
        (await session.scalars(
            select(ReviewReviewer).where(ReviewReviewer.run_id.in_([r.id for r in runs]))
        )).all()
    )
    by_run: dict[int, list[dict]] = {}
    for c in cards:
        by_run.setdefault(c.run_id, []).append(_card_view(c))
    return [{**_run_view(r), "reviewers": sorted(by_run.get(r.id, []), key=lambda c: c["name"])}
            for r in runs]


@router.get("/review/stats")
async def review_stats(
    _reader: str = Depends(reader),
    repo: str | None = Query(None),
    author: str | None = Query(None),
    since: str | None = Query(None, description="ISO timestamp"),
    days: int | None = Query(None, ge=1, le=3650),
    judged_only: bool = Query(
        True, description="count only judge-adjudicated runs (required for precision)"
    ),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Per-model and per-agent aggregates over the recorded runs.

    ``by_model`` is grouped by (reviewer, model, effort) — the same vendor at a
    different tier is a different competitor, which is the whole question.
    """
    filters = []
    if repo is not None:
        filters.append(ReviewRun.repo == repo)
    if author is not None:
        author = await _authored_as(session, author)
        filters.append(ReviewRun.author == author)
    cutoff = _since_clause(since, days)
    if cutoff is not None:
        filters.append(ReviewRun.ts >= cutoff)
    if judged_only:
        filters.append(ReviewRun.judged.is_(True))

    totals = (
        await session.execute(
            select(
                func.count(ReviewRun.id),
                func.count(func.distinct(func.concat(ReviewRun.repo, "#", ReviewRun.pr))),
                func.count(func.distinct(ReviewRun.repo)),
                func.min(ReviewRun.ts),
                func.max(ReviewRun.ts),
            ).where(*filters)
        )
    ).one()

    model_rows = (
        await session.execute(
            select(
                ReviewReviewer.name,
                ReviewReviewer.model,
                ReviewReviewer.effort,
                func.count(ReviewReviewer.id),
                func.count(ReviewReviewer.id).filter(ReviewReviewer.ran.is_(False)),
                func.sum(ReviewReviewer.raised),
                func.sum(ReviewReviewer.confirmed),
                func.sum(ReviewReviewer.dismissed),
                func.sum(ReviewReviewer.unjudged),
                func.sum(ReviewReviewer.solo),
                func.sum(ReviewReviewer.p1),
                func.sum(ReviewReviewer.p2),
                func.sum(ReviewReviewer.p3),
                func.sum(ReviewReviewer.p4),
                func.avg(ReviewReviewer.duration_ms).filter(
                    ReviewReviewer.duration_ms.isnot(None)
                ),
                func.sum(ReviewReviewer.shared),
                func.sum(ReviewReviewer.sev_stricter),
                func.sum(ReviewReviewer.sev_agree),
                func.sum(ReviewReviewer.sev_looser),
                func.sum(ReviewReviewer.duration_ms),
                # How often this member reviewed a PREFIX of the diff. A row that
                # says "12 confirmed" reads differently when half of those runs
                # only showed it half the change.
                func.count(ReviewReviewer.id).filter(ReviewReviewer.truncated.is_(True)),
                # Runs where it said what it could not judge. A member that was
                # never asked (pre-v2.14) must not read as one that declared
                # nothing. Deliberately NOT `jsonb_array_length(...) > 0`: this
                # column holds JSON `null` for "not asked" (SQLAlchemy's JSONB
                # rendering of a Python None), that function ERRORS on a scalar
                # rather than returning NULL, and SQL gives no evaluation-order
                # guarantee that a typeof guard beside it would run first. A
                # comparison against an empty array is total over every jsonb
                # value. It is built server-side rather than cast from "[]",
                # because a Python string bound to a JSONB parameter serialises
                # to the jsonb *string* `"[]"`, which no array ever equals — so
                # every row would count as a declared gap.
                func.count(ReviewReviewer.id).filter(
                    func.jsonb_typeof(ReviewReviewer.could_not_assess) == "array",
                    ReviewReviewer.could_not_assess != func.jsonb_build_array(),
                ),
                func.sum(ReviewReviewer.rereview_flagged),
            )
            .join(ReviewRun, ReviewRun.id == ReviewReviewer.run_id)
            .where(*filters)
            .group_by(ReviewReviewer.name, ReviewReviewer.model, ReviewReviewer.effort)
        )
    ).all()

    by_model = []
    for (name, model, effort, runs, skipped, raised, confirmed, dismissed,
         unjudged, solo, p1, p2, p3, p4, avg_ms,
         shared, stricter, agree, looser, total_ms,
         truncated_runs, declared_runs, rereview_flagged) in model_rows:
        confirmed, dismissed = int(confirmed or 0), int(dismissed or 0)
        raised = int(raised or 0)
        ruled = confirmed + dismissed
        ran = runs - skipped
        shared = int(shared or 0)
        stricter, agree, looser = int(stricter or 0), int(agree or 0), int(looser or 0)
        rated = stricter + agree + looser
        total_ms = int(total_ms) if total_ms is not None else None
        by_model.append({
            "reviewer": name,
            "model": model,
            "effort": effort,
            "runs": runs,
            "ran": ran,
            "skipped_runs": skipped,
            "raised": raised,
            "confirmed": confirmed,
            "dismissed": dismissed,
            "unjudged": int(unjudged or 0),
            "solo": int(solo or 0),
            # Findings someone else raised too. Its complement is a superset of
            # `solo` — a lone reporter is either the only one who saw it or the
            # only one who was wrong, and precision is what separates those.
            "shared": shared,
            "consensus_rate": round(shared / raised, 3) if raised else None,
            # None, not 0.0 — "the judge never ruled on anything it raised" is a
            # different statement from "everything it raised was wrong".
            "precision": round(confirmed / ruled, 3) if ruled else None,
            "confirmed_per_run": round(confirmed / ran, 2) if ran else None,
            "p1": int(p1 or 0), "p2": int(p2 or 0), "p3": int(p3 or 0), "p4": int(p4 or 0),
            "sev_stricter": stricter,
            "sev_agree": agree,
            "sev_looser": looser,
            # Needs `reported_by` severities to be non-null, so it stays None for
            # every pre-v2.11 run rather than reading as perfect disagreement.
            "severity_calibration": round(agree / rated, 3) if rated else None,
            # The coverage side of a scorecard: how often this member reviewed
            # only part of the diff, how often it said so about something else,
            # and how many fixes it asked to have re-read. A reviewer that
            # reliably declares what it could not see is worth more than one that
            # silently reports clean, and nothing else here tells them apart.
            "truncated_runs": truncated_runs,
            "declared_gaps_runs": declared_runs,
            "rereview_flagged": int(rereview_flagged or 0),
            "avg_duration_ms": round(float(avg_ms)) if avg_ms is not None else None,
            # The cost side of "is the expensive tier worth it": time spent per
            # finding that survived the judge, not per finding raised.
            "ms_per_confirmed": round(total_ms / confirmed) if total_ms and confirmed else None,
        })
    by_model.sort(key=lambda m: (-m["confirmed"], m["reviewer"]))

    agent_rows = (
        await session.execute(
            select(
                ReviewRun.author,
                func.count(ReviewRun.id),
                func.count(func.distinct(func.concat(ReviewRun.repo, "#", ReviewRun.pr))),
                func.sum(ReviewRun.n_confirmed),
                func.sum(ReviewRun.n_dismissed),
                func.max(ReviewRun.ts),
            )
            .where(*filters)
            .group_by(ReviewRun.author)
        )
    ).all()
    by_agent = [
        {
            "author": author_id,
            "runs": runs,
            "prs": prs,
            "confirmed": int(confirmed or 0),
            "dismissed": int(dismissed or 0),
            "last_run": last.isoformat(),
        }
        for author_id, runs, prs, confirmed, dismissed, last in agent_rows
    ]
    by_agent.sort(key=lambda a: -a["runs"])

    runs_total, prs_total, repos_total, first_ts, last_ts = totals
    return {
        "window": {
            "since": cutoff.isoformat() if cutoff else None,
            "judged_only": judged_only,
            "repo": repo,
            "author": author,
            "first_run": first_ts.isoformat() if first_ts else None,
            "last_run": last_ts.isoformat() if last_ts else None,
        },
        "runs": runs_total,
        "prs": prs_total,
        "repos": repos_total,
        "by_model": by_model,
        "by_agent": by_agent,
    }


@router.get("/review/findings")
async def pr_finding_history(
    _reader: str = Depends(reader),
    repo: str = Query(..., min_length=1, description="github nameWithOwner"),
    pr: int = Query(..., ge=1),
    limit: int = Query(50, ge=1, le=200, description="trace this many of the PR's runs"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One PR's findings as chains of observations — did the fix land?

    Observations are never collapsed: run 3 and run 7 seeing the same defect are
    two rows joined by ``key``, which is what makes "was this actually fixed?"
    and "how many rounds did this PR take?" answerable at all. Collapsing them
    into one current-state row would erase precisely that.

    ``status`` is what the record supports, not a claim about the code:

    * ``dismissed`` — the judge ruled against it every time it was raised.
    * ``gone`` — raised in an earlier run of this PR and not in the latest one.
      Usually the fix landed; it can also mean the reviewer that raised it did
      not run again, which the observation list shows.
    * ``open`` — still raised in the most recent run.

    Scoped to one PR because ``key`` identifies a defect within a PR: the same
    "unused import" in two repos is not one chain.
    """
    # One over the window, so "there is older history" is a fact rather than the
    # guess "we returned exactly as many as we asked for".
    fetched = list(
        (await session.scalars(
            select(ReviewRun)
            .where(ReviewRun.repo == repo, ReviewRun.pr == pr)
            .order_by(ReviewRun.ts.desc(), ReviewRun.id.desc())
            .limit(limit + 1)
        )).all()
    )
    if not fetched:
        return {"repo": repo, "pr": pr, "rounds": 0, "stopped": None,
                "stop_confident": None, "truncated": False,
                "runs": [], "findings": []}

    truncated = len(fetched) > limit
    runs = list(reversed(fetched[:limit]))  # chronological: a chain reads left to right
    order = {r.id: i for i, r in enumerate(runs)}
    ts_by_run = {r.id: r.ts for r in runs}
    latest_id = runs[-1].id

    findings = list(
        (await session.scalars(
            select(ReviewFinding)
            .where(ReviewFinding.run_id.in_(list(order)))
            .order_by(ReviewFinding.id)
        )).all()
    )
    reports = await _reports_by_finding(session, [f.id for f in findings])

    chains: dict[str, list[ReviewFinding]] = {}
    for f in sorted(findings, key=lambda f: order[f.run_id]):
        chains.setdefault(f.finding_key, []).append(f)

    # Was each round's re-review declaration any good? A reviewer that says "the
    # fix for this needs re-reading" is making a checkable claim, and this is the
    # check: the round that followed either did raise something new in that file
    # or it did not. Derived from the record rather than asked for — the declarer
    # cannot mark its own homework.
    #
    # "New" is computed within the traced window, so a finding first raised before
    # it counts as new here; the run's own `new_findings`, which the panel
    # computed against its real baseline, is reported alongside for that reason.
    first_seen = {key: order[obs[0].run_id] for key, obs in chains.items()}
    flagged_files: dict[int, set[str]] = {}
    fresh_files: dict[int, set[str]] = {}
    flagged_counts: dict[int, int] = {}
    for f in findings:
        i = order[f.run_id]
        if f.needs_rereview:
            flagged_counts[i] = flagged_counts.get(i, 0) + 1
            if f.file:
                flagged_files.setdefault(i, set()).add(f.file)
        if f.file and first_seen[f.finding_key] == i:
            fresh_files.setdefault(i, set()).add(f.file)

    out = []
    for key, obs in chains.items():
        last = obs[-1]
        reviewers: list[str] = []
        related: list[str] = []
        for f in obs:
            reviewers += [r for r in (f.reviewers or []) if r not in reviewers]
            related += [r for r in (f.related or []) if r not in related]
        verdicts = {f.verdict for f in obs}
        out.append({
            "key": key,
            # The latest observation's words: the newest statement of the defect
            # is the one worth showing, and its line has survived any fix above it.
            "file": last.file,
            "line": last.line,
            "title": last.title,
            "severity": last.severity,
            "status": ("dismissed" if verdicts == {"dismissed"}
                       else "open" if last.run_id == latest_id else "gone"),
            "runs_seen": len(obs),
            "first_run": obs[0].run_id,
            "last_run": last.run_id,
            "reviewers": reviewers,
            "related": related,
            "needs_rereview": any(f.needs_rereview for f in obs),
            "observations": [
                {
                    "run_id": f.run_id,
                    "ts": ts_by_run[f.run_id].isoformat(),
                    **_finding_view(f, reports.get(f.id, [])),
                }
                for f in obs
            ],
        })
    out.sort(key=lambda c: (c["severity"] or "P9", order[c["first_run"]], c["key"]))

    return {
        "repo": repo,
        "pr": pr,
        "rounds": len(runs),
        # What ended the cycle, from the last round that ran — and whether that
        # was convergence or merely a stop. A PR whose panel gave up at the round
        # cap, or stopped while a reviewer was reading half the diff, must not
        # read like one that was reviewed until there was nothing left.
        "stopped": runs[-1].stop_reason,
        "stop_confident": runs[-1].stop_confident,
        # More runs exist than the window traced, so `first_run` and a `gone`
        # status describe the window, not the PR's whole history.
        "truncated": truncated,
        "runs": [
            {"id": r.id, "ts": r.ts.isoformat(), "author": r.author, "judged": r.judged,
             "confirmed": r.n_confirmed, "dismissed": r.n_dismissed,
             "unjudged": r.n_unjudged, "sonar": r.n_sonar,
             "round": r.round, "new_findings": r.new_findings,
             "stop_reason": r.stop_reason, "stop_confident": r.stop_confident,
             # Findings this round declared worth re-reading, and whether the
             # round that followed found anything where it pointed. None = no
             # round followed, which is a different answer from "nothing there".
             "rereview_flagged": flagged_counts.get(i, 0),
             "rereview_hit": (
                 None if i + 1 >= len(runs) or not flagged_files.get(i)
                 else bool(flagged_files[i] & fresh_files.get(i + 1, set()))
             )}
            for i, r in enumerate(runs)
        ],
        "findings": out,
    }


@router.get("/review/{run_id}")
async def get_review(
    run_id: int,
    _reader: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One run in full — scorecards plus every finding and its verdict."""
    run = await session.get(ReviewRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no review run {run_id}")
    cards = list(
        (await session.scalars(
            select(ReviewReviewer)
            .where(ReviewReviewer.run_id == run_id)
            .order_by(ReviewReviewer.name)
        )).all()
    )
    findings = list(
        (await session.scalars(
            select(ReviewFinding)
            .where(ReviewFinding.run_id == run_id)
            .order_by(ReviewFinding.severity, ReviewFinding.id)
        )).all()
    )
    reports = await _reports_by_finding(session, [f.id for f in findings])
    return {
        **_run_view(run),
        "reviewers": [_card_view(c) for c in cards],
        "findings": [_finding_view(f, reports.get(f.id, [])) for f in findings],
    }
