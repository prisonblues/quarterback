"""Reviewer-panel stats (v2.10).

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
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import identify, reader
from app.db import get_session
from app.identity import agent_row, compose, machine_of
from app.models.review import ReviewFinding, ReviewReviewer, ReviewRun

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


# ----------------------------------------------------------------- ingest models

class FindingIn(BaseModel):
    """One deduped finding, exactly as ``panel.py --json`` serialises it."""

    severity: str = "P3"
    file: str | None = None
    line: int | None = None
    title: str = ""
    detail: str = ""
    reviewers: list[str] = Field(default_factory=list)
    reason: str = ""


class ReviewerIn(BaseModel):
    """A panel member as configured for this run — its brain, not its findings."""

    model: str | None = None
    effort: str | None = None
    ran: bool = True
    skip: str | None = None
    max_diff_chars: int | None = None
    truncated: bool | None = None
    duration_ms: int | None = None


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
    sonar_gate: str | None = None
    ci_status: str | None = None

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


def _scorecards(
    findings: list[tuple[FindingIn, str]],
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
    credited = {r for f, _ in findings for r in f.reviewers}
    skips = {s.split(":", 1)[0].strip(): s for s in skipped if ":" in s}
    names = sorted(set(cfg) | set(selected) | credited)

    # Tallied as plain counters first: a column ``default=0`` is applied at
    # flush, so incrementing a freshly-constructed ORM object would start from
    # None.
    zero = ("raised", "confirmed", "dismissed", "unjudged", "solo",
            *(s.lower() for s in SEVERITIES))
    tally: dict[str, dict[str, int]] = {n: dict.fromkeys(zero, 0) for n in names}
    for f, verdict in findings:
        for name in f.reviewers:
            t = tally[name]
            t["raised"] += 1
            if verdict == "confirmed":
                t["confirmed"] += 1
                if len(f.reviewers) == 1:
                    t["solo"] += 1
                sev = (f.severity or "").upper()
                if sev in SEVERITIES:
                    t[sev.lower()] += 1
            elif verdict in ("dismissed", "unjudged"):
                t[verdict] += 1

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

    findings: list[tuple[FindingIn, str]] = (
        [(f, _verdict(f, body.judged)) for f in body.to_fix]
        + [(f, "dismissed") for f in body.dismissed]
        + [(f, "sonar") for f in body.sonar_findings]
    )
    counts = {v: sum(1 for _, x in findings if x == v) for v in
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
    scored = [(f, v) for f, v in findings if v != "sonar"]
    for card in _scorecards(scored, body.reviewers, body.reviewers_selected, body.skipped):
        card.run_id = run.id
        session.add(card)

    for f, verdict in findings:
        session.add(
            ReviewFinding(
                run_id=run.id,
                verdict=verdict,
                severity=(f.severity or "").upper() or None,
                file=f.file,
                line=f.line,
                title=f.title or "(untitled)",
                detail=f.detail or None,
                reason=f.reason or None,
                reviewers=f.reviewers or None,
                n_reviewers=len(f.reviewers),
            )
        )

    await session.commit()
    return {"id": run.id, "recorded": True, "findings": len(findings)}


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
        "raised": c.raised,
        "confirmed": c.confirmed,
        "dismissed": c.dismissed,
        "unjudged": c.unjudged,
        "solo": c.solo,
        "p1": c.p1, "p2": c.p2, "p3": c.p3, "p4": c.p4,
    }


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
            )
            .join(ReviewRun, ReviewRun.id == ReviewReviewer.run_id)
            .where(*filters)
            .group_by(ReviewReviewer.name, ReviewReviewer.model, ReviewReviewer.effort)
        )
    ).all()

    by_model = []
    for (name, model, effort, runs, skipped, raised, confirmed, dismissed,
         unjudged, solo, p1, p2, p3, p4, avg_ms) in model_rows:
        confirmed, dismissed = int(confirmed or 0), int(dismissed or 0)
        ruled = confirmed + dismissed
        ran = runs - skipped
        by_model.append({
            "reviewer": name,
            "model": model,
            "effort": effort,
            "runs": runs,
            "ran": ran,
            "skipped_runs": skipped,
            "raised": int(raised or 0),
            "confirmed": confirmed,
            "dismissed": dismissed,
            "unjudged": int(unjudged or 0),
            "solo": int(solo or 0),
            # None, not 0.0 — "the judge never ruled on anything it raised" is a
            # different statement from "everything it raised was wrong".
            "precision": round(confirmed / ruled, 3) if ruled else None,
            "confirmed_per_run": round(confirmed / ran, 2) if ran else None,
            "p1": int(p1 or 0), "p2": int(p2 or 0), "p3": int(p3 or 0), "p4": int(p4 or 0),
            "avg_duration_ms": round(float(avg_ms)) if avg_ms is not None else None,
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
    return {
        **_run_view(run),
        "reviewers": [_card_view(c) for c in cards],
        "findings": [
            {
                "verdict": f.verdict,
                "severity": f.severity,
                "file": f.file,
                "line": f.line,
                "title": f.title,
                "detail": f.detail,
                "reason": f.reason,
                "reviewers": f.reviewers or [],
            }
            for f in findings
        ],
    }
