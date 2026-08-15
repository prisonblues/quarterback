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
    #: The judge's ruling on the coverage the reviewers declared — the split
    #: between "clean" and "could not tell", adjudicated rather than averaged.
    coverage_note: Mapped[str | None] = mapped_column(Text)

    # Where this run sat in the panel -> fix -> panel cycle (v2.14). A PR's round
    # COUNT is derivable by counting its runs; what is not derivable is what each
    # round found that the one before it had not, and what stopped the loop.
    #: 1 for a first review, 2+ for a re-review of the fix commit.
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    #: Which panel -> fix -> panel CYCLE this round belongs to. Every round of one
    #: cycle carries the same opaque id (the panel inherits it from its earliest
    #: baseline), so "the re-review of THIS round's declaration" is a join rather
    #: than the guess "whatever ran next on this PR" — two agents looping the same
    #: PR interleave, and a positional rule credits one cycle's round 2 to the
    #: other's round 1. NULL for every run recorded before the panel sent it.
    cycle: Mapped[str | None] = mapped_column(Text)
    #: Findings this round that no earlier round raised. NULL where the panel
    #: never said — "not reported" and "nothing new" are different facts, and
    #: storing the second for the first is how a pre-v2.14 run reads as converged.
    new_findings: Mapped[int | None] = mapped_column(Integer)
    #: Whether the cycle actually STOPPED here. A round that ends with findings
    #: outstanding sends ``stop: false`` and carries a reason for going again, so
    #: reading the reason as "this is where it stopped" labels a cycle that must
    #: continue as finished. NULL where the panel didn't say.
    stopped: Mapped[bool | None] = mapped_column(Boolean)
    #: What ended the loop, in the panel's words: dry / a P1-P2 still confirmed /
    #: the round cap. A cap reached with work outstanding is not convergence.
    #: Also carries the reason to go AGAIN when ``stopped`` is false — the two are
    #: told apart by that column, never by the prose.
    stop_reason: Mapped[str | None] = mapped_column(Text)
    #: Whether that stop was EARNED. False when a reviewer was truncated, absent,
    #: unparsed, or declared a gap — the cases where a counter reading zero says
    #: nothing about the code. This is the column that lets a human review the
    #: review without re-reading the transcript.
    stop_confident: Mapped[bool | None] = mapped_column(Boolean)
    #: WHY it was unearned, verbatim from the panel ("codex saw 60,000 of 118,402
    #: diff chars", "claude could not assess: the migration"). ``stop_confident``
    #: says a clean verdict was not evidence; without the reasons the reader has
    #: no way to judge how badly — which is the question this release exists to
    #: answer, and the one the operator is told to relay.
    stop_veto: Mapped[list[Any] | None] = mapped_column(JSONB)

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
    #: The reviewer read a PREFIX of the diff, at ``max_diff_chars``. Measured by
    #: the panel, never asked for: the one thing a truncated reviewer cannot
    #: notice is its own truncation, and a member that saw half the diff must be
    #: distinguishable from one that saw all of it on every row it contributed to.
    truncated: Mapped[bool | None] = mapped_column(Boolean)
    #: What this member said it could NOT judge — a file the diff omits, a runtime
    #: behaviour, a schema it cannot see. An observation, not a forecast, and the
    #: only thing that separates "clean" from "I could not tell"; a finding count
    #: reports both as zero. NULL = no structured declaration was obtained — the
    #: member was never asked (every pre-v2.14 panel), its CLI answered in the old
    #: bare-array shape, or its reply did not parse at all (see ``unstructured``);
    #: [] = asked, and it had nothing to declare. The two states must not collapse
    #: — that is the whole point of the column — so this says it the same way
    #: ``app.api.reviews.ReviewerIn.could_not_assess`` does.
    could_not_assess: Mapped[list[Any] | None] = mapped_column(JSONB)
    #: This member's reply carried no JSON and was kept as one raw finding. Its
    #: findings are real work, but nothing it might have declared survived the
    #: parse — so it lands on NULL ``could_not_assess`` for a reason that has
    #: nothing to do with never being asked, and only this column tells the two
    #: apart. Without it an unparsed reviewer is invisible to the honesty stats,
    #: which is the same NULL/[] collapse one level up. NULL = the panel didn't say.
    unstructured: Mapped[bool | None] = mapped_column(Boolean)
    #: Findings this member flagged as needing the FIX re-read. With the next
    #: round's new findings this is the accuracy check on the declaration itself —
    #: the raw material for honesty per reviewer, which precision cannot show.
    rereview_flagged: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
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

    #: A reporter declared that fixing this takes a structural change whose RESULT
    #: should be re-read. Stored on the finding as well as per reporter
    #: (:class:`ReviewFindingReport`) because that is the grain the next round is
    #: checked against: did the round that followed find something here?
    needs_rereview: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: This observation was not raised by any earlier round of the same PR — the
    #: dry-round counter, per finding. NULL where the panel didn't say.
    new_this_round: Mapped[bool | None] = mapped_column(Boolean)

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

    Fed by callers that send ``reported_by``. ``panel.py`` does not yet — it
    merges a group down to one representative before it serialises — so for panel
    runs this table stays empty, the finding's own ``rereview_by`` attribution
    carries the declaration instead, and the calibration counters stay at zero.
    Issue #26 is where the panel starts sending the accounts.
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
    #: THIS reviewer said the fix for this finding needs re-reading. Per reporter,
    #: not per finding, because the declaration's accuracy is per reviewer: a
    #: group flag credited to everyone who happened to raise the finding makes the
    #: member that called it and the member that didn't indistinguishable.
    needs_rereview: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    __table_args__ = (
        UniqueConstraint("finding_id", "reviewer", name="uq_review_report_finding_reviewer"),
        Index("ix_review_finding_reports_finding", "finding_id"),
        Index("ix_review_finding_reports_reviewer", "reviewer"),
    )
