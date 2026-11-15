from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: The longest a stage name may be. Opaque text — lexray's are ``fast``, ``db``,
#: ``sim run 1`` — so this bounds a label, not a vocabulary.
MAX_STAGE = 200

#: The longest the free-text fields (where the outputs live, what resets it) may
#: be. A pointer and a sentence, not a log.
MAX_NOTE = 2000

#: Consecutive clean runs that pass a gate whose owner named no bar: lexray's.
DEFAULT_EXIT_BAR = 2


class Gate(Base):
    """One promotion gate's state, per repository and branch — #852.

    lexray's v22 gate kept this in comments on a GitHub issue (lexray#4488): the
    head under test, the stage it had reached, how many clean heavy runs it had
    banked against the exit bar of two, and where the outputs were. The owner, a
    peer and a successor session on another box each had to rebuild that from
    prose, and two of the day's runs were superseded by merges to the gated branch
    that only the coordinator knew about.

    **A row of its own, not a plan item with fields.** A plan item is a unit of
    work in an order; a gate is a running count attached to a *branch*, and it is
    reset by the branch moving rather than by anybody deciding anything. Putting
    it on a plan item would give every item columns only gates use and would tie
    a gate's lifetime to an item's.

    **The exit bar lives on the record.** The board takes testimony (see
    :class:`app.models.dial.DialSetting`): the gate's owner states the bar when
    they set the gate, and every reader sees the number the count is measured
    against beside the count itself. A bar in a repo rules file would be a second
    place to look, and one the server cannot read.

    **The head is the whole mechanism.** A run counts only against the head it
    ran on (:func:`app.api.gates.advance_gate` refuses one that names another),
    and a ``published`` post putting a commit on this branch for the first time
    zeroes the count and moves the head (:func:`app.api.gates.reset_on_publish`),
    so a clean run on a superseded head can never be counted toward the current
    one. ``landed`` does not move it: that type says a commit exists somewhere,
    not that the branch's runners can fetch it.
    """

    __tablename__ = "gates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))

    #: ``owner/name``, folded through :func:`app.claimkey.canonical_repo`.
    repo: Mapped[str] = mapped_column(Text(), nullable=False)

    #: As git spells it; case is NOT folded (see :func:`app.claimkey.canonical_branch`).
    branch: Mapped[str] = mapped_column(Text(), nullable=False)

    #: The full commit id under test, lower-cased.
    head_sha: Mapped[str] = mapped_column(Text(), nullable=False)

    #: Where the gate has got to on this head. NULL after a reset: nothing has run
    #: on the new head yet.
    stage: Mapped[str | None] = mapped_column(Text(), nullable=True)

    clean_runs: Mapped[int] = mapped_column(Integer(), nullable=False, server_default="0")
    exit_bar: Mapped[int] = mapped_column(Integer(), nullable=False,
                                          server_default=str(DEFAULT_EXIT_BAR))

    #: ``clean`` or ``failed``, and when — the last run recorded on this head.
    last_result: Mapped[str | None] = mapped_column(Text(), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    #: Where the outputs live (``zeus:~/sim-runs/v22``) and what resets the gate
    #: beyond a new head. Opaque prose for the next reader.
    outputs: Mapped[str | None] = mapped_column(Text(), nullable=True)
    reset_rule: Mapped[str | None] = mapped_column(Text(), nullable=True)

    #: The head the gate was last moved off, and when: by a publish, or by a
    #: ``POST /gates`` naming a new head. ``updated_by`` says which. NULL until the
    #: first move.
    restarted_from: Mapped[str | None] = mapped_column(Text(), nullable=True)
    restarted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    updated_by: Mapped[str] = mapped_column(Text(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("repo = lower(btrim(repo)) AND length(repo) > 0",
                        name="ck_gates_repo_canonical"),
        CheckConstraint("length(btrim(branch)) > 0", name="ck_gates_branch"),
        CheckConstraint("head_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'",
                        name="ck_gates_head_sha"),
        CheckConstraint("clean_runs >= 0", name="ck_gates_clean_runs"),
        CheckConstraint("exit_bar >= 1", name="ck_gates_exit_bar"),
        CheckConstraint("last_result IS NULL OR last_result IN ('clean', 'failed')",
                        name="ck_gates_last_result"),
        CheckConstraint(f"stage IS NULL OR length(stage) <= {MAX_STAGE}",
                        name="ck_gates_stage_len"),
        Index("ix_gates_repo_branch", "repo", "branch", unique=True),
    )
