from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ReviewRun(Base):
    """One reviewer-panel run over one PR.

    The panel (``~/.claude/loops/panel.py``) fans a PR diff out to several
    vendor CLIs, dedups what they report, and has a master judge rule each
    finding real or not. That produces exactly the comparison nobody was
    keeping: the same diff, reviewed by several models, with an adjudicated
    answer for who was right. Recording it turns "which model should review?"
    and "is the expensive tier worth it?" into queries instead of opinions.

    ``author`` is the board identity that ran the panel (``machine/instance``),
    so a run is attributable to the agent that ordered it, not just the machine.

    Not a board post: posts are an append-only wire read newest-last by agents,
    and aggregating months of them by model would mean unpicking JSON in the
    read path. This is the durable, queryable half; the board still gets a
    one-line note so peers see the review happened.
    """

    __tablename__ = "review_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    author: Mapped[str] = mapped_column(Text, nullable=False)
    session: Mapped[str | None] = mapped_column(Text)

    # What was reviewed.
    repo: Mapped[str] = mapped_column(Text, nullable=False)  # github "owner/name"
    pr: Mapped[int] = mapped_column(Integer, nullable=False)
    pr_title: Mapped[str | None] = mapped_column(Text)
    base_branch: Mapped[str | None] = mapped_column(Text)
    changed_lines: Mapped[int | None] = mapped_column(Integer)
    diff_chars: Mapped[int | None] = mapped_column(Integer)
    diff_truncated: Mapped[bool | None] = mapped_column(Boolean)

    # How it was adjudicated. An unjudged run keeps every finding (the panel
    # never suppresses), so its findings must NOT count towards precision —
    # hence the flag rather than inferring from the verdicts.
    judged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    judge_model: Mapped[str | None] = mapped_column(Text)
    judge_skip: Mapped[str | None] = mapped_column(Text)

    # Hard gates that sit alongside the LLM panel.
    sonar_gate: Mapped[str | None] = mapped_column(Text)
    ci_status: Mapped[str | None] = mapped_column(Text)

    reviewers_selected: Mapped[list[str] | None] = mapped_column(JSONB)
    reviewers_override: Mapped[str | None] = mapped_column(Text)
    skipped: Mapped[list[str] | None] = mapped_column(JSONB)

    # Denormalised run totals, so the run list renders without touching findings.
    n_confirmed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    n_dismissed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    n_unjudged: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    n_sonar: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: Client-chosen idempotency key. A panel run that records, times out on the
    #: response and retries would otherwise double-count itself into the stats.
    run_key: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (
        Index("ix_review_runs_repo_pr", "repo", "pr"),
        Index("ix_review_runs_ts", "ts"),
        Index("ix_review_runs_author", "author"),
    )


class ReviewReviewer(Base):
    """One panel member's scorecard for one run — the row the stats aggregate.

    Counts are computed server-side from the findings rather than sent, so the
    scorecard cannot disagree with the findings it summarises.

    ``model``/``effort`` are recorded per run because they drift: a repo's
    ``.harness-rules`` gets repinned, a slug is retired, a run is hand-picked
    with ``--reviewers``. Grouping stats by (name, model, effort) is what makes
    "is the higher tier worth it" answerable at all — the same vendor at two
    tiers is two competitors, not one.
    """

    __tablename__ = "review_reviewers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)  # claude|codex|gemini|pi|sonarqube
    model: Mapped[str | None] = mapped_column(Text)
    effort: Mapped[str | None] = mapped_column(Text)

    #: False when the vendor was selected but never reviewed (CLI absent, model
    #: refused, auth expired). Kept as a row rather than dropped: a reviewer that
    #: keeps failing to run is a finding about the panel, invisible if unrecorded.
    ran: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    skip_reason: Mapped[str | None] = mapped_column(Text)

    max_diff_chars: Mapped[int | None] = mapped_column(Integer)
    truncated: Mapped[bool | None] = mapped_column(Boolean)
    #: Wall-clock for this reviewer's CLI call. Nullable and unset for now — the
    #: panel doesn't time its members yet; the column is here so it can start
    #: without a migration, since duration is the cost proxy that turns
    #: "finds more" into "worth it".
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    raised: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    confirmed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    dismissed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    unjudged: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Confirmed findings no other panel member raised — the marginal value of
    #: keeping this reviewer on the panel, which a raw count can't show.
    solo: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Findings (any verdict) at least one other member also reported. With
    #: ``raised`` this is the consensus rate: a member that agrees with everyone
    #: and a member that only ever reports alone are different propositions even
    #: at the same precision.
    shared: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: Severity calibration against the judge, over confirmed findings where the
    #: member sent its own severity (``reported_by[].severity``). "Stricter" =
    #: the member rated it more severe than the judge settled on. A reviewer that
    #: is right but always cries P1 costs triage time, which precision can't say.
    sev_stricter: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sev_agree: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sev_looser: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    p1: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    p2: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    p3: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    p4: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        UniqueConstraint("run_id", "name", name="uq_review_reviewer_run_name"),
        Index("ix_review_reviewers_name_model", "name", "model"),
    )


class ReviewFinding(Base):
    """One merged finding from a run, with the judge's verdict on it.

    Stored per run (not per PR): the same defect found again after a fix loop is
    a new observation, and collapsing the two would erase the fix. ``key`` is
    what links those observations without collapsing them — see below.

    ``title``/``detail`` are the judge's synthesis of the group. What each
    reviewer actually said lives in :class:`ReviewFindingReport`, one row per
    reporter, so a merge is additive rather than a survivor of a coin toss.
    """

    __tablename__ = "review_findings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False
    )
    #: confirmed | dismissed | unjudged | sonar (the hard gate's own issues)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str | None] = mapped_column(Text)  # P1..P4, post-judge
    file: Mapped[str | None] = mapped_column(Text)
    line: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)  # the judge's rationale

    #: Identity of the *defect*, so two observations of it can be joined without
    #: being merged: "was this actually fixed?" and "how many rounds did this PR
    #: take?" are then queries. Scoped by the run's (repo, pr) at read time, so
    #: the same key in another repo is a different chain. Client-supplied when
    #: the panel has a stable id of its own; otherwise derived from file + a
    #: normalised title, deliberately *without* the line — a line number moves
    #: when the fix above it lands, and an identity that moves links nothing.
    finding_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: Other findings in the same run that share a cause and should be fixed
    #: together, as their ``finding_key``s. A decision spread over four files is
    #: not one finding, but it is one fix — no positional rule can say that.
    related: Mapped[list[Any] | None] = mapped_column(JSONB)

    reviewers: Mapped[list[Any] | None] = mapped_column(JSONB)  # ["codex", "gemini"]
    #: Denormalised len(reviewers) so consensus/solo queries don't unnest JSONB.
    n_reviewers: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        Index("ix_review_findings_run", "run_id"),
        Index("ix_review_findings_verdict", "verdict"),
        Index("ix_review_findings_key", "finding_key"),
    )


class ReviewFindingReport(Base):
    """What one reviewer actually said about one finding — verbatim.

    The panel used to merge before the judge and keep a single representative's
    text, so two models describing the same bug from different angles left one
    description on the floor. This is where the other accounts live: merging
    becomes additive, the synthesis is new and the originals ride along, and a
    human can audit whether the merge was right.

    It also turns attribution into a stored fact rather than an inference.
    ``severity``/``line`` are *this reviewer's own*, which differ from the
    judge's and are the raw material for calibration stats; "confirmed findings
    where pi was the sole reporter" becomes a join rather than a JSONB unnest.
    """

    __tablename__ = "review_finding_reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    finding_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_findings.id", ondelete="CASCADE"), nullable=False
    )
    reviewer: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str | None] = mapped_column(Text)  # what this reviewer called it
    line: Mapped[int | None] = mapped_column(Integer)  # where this reviewer put it
    account: Mapped[str | None] = mapped_column(Text)  # verbatim, never rewritten

    __table_args__ = (
        UniqueConstraint("finding_id", "reviewer", name="uq_review_report_finding_reviewer"),
        Index("ix_review_finding_reports_finding", "finding_id"),
        Index("ix_review_finding_reports_reviewer", "reviewer"),
    )
