from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PlanReconcilePass(Base):
    """That a reconcile pass RAN over one scope, as opposed to what it found (#695).

    **A clean pass erases its own evidence, and this is the row that survives it.**
    :class:`PlanReconcile` is the findings, and a scope with nothing wrong has
    none — `report_reconcile` deletes the rows the pass did not re-report, which
    for a healthy scope is all of them. So an empty `plan_reconcile` is ambiguous
    between "nobody has run a pass since the board came up" and "somebody ran one
    four minutes ago and the plan is fine", and those are the two states a monitor
    most needs to tell apart. The findings table cannot be taught to say it: the
    absence of a finding is exactly what it is for.

    **Why the posts table cannot answer it either.** `qb-reconcile --post` is
    gated on `has_content`, so a clean pass makes no board post at all, and
    `REPOST_AFTER` only bounds how often a pass with something to say repeats
    itself. Reading "no `finding` post in four hours" as "nothing is reconciling"
    is wrong in precisely the healthy case, which is the absent-result-as-benign
    reading `_edge_spoof_refused` is annotated against on the other side of this
    same question.

    **One row per scope, not per host, and not per pass.** Per pass would be a
    log — ~96 rows a day per host, to answer a question that only ever concerns
    the newest. Per host would invite "zeus has not reconciled in a day" as a
    finding, and that is not a fault: the pass is a fleet singleton over a shared
    plan, two hosts hold the timer precisely so either can carry it, and a host
    that stops is only a problem when no host is left. `reported_by` says which
    one wrote the row that is here — the same claim, and the same wording, as
    :class:`PlanReconcile` makes about a finding.

    **It records the pass, never a verdict about it.** Whether four hours without
    a pass is stale is a monitor's judgement and belongs where the thresholds
    live; the board's job is to hold the fact that somebody ran one, and when.
    """

    __tablename__ = "plan_reconcile_pass"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The scope the pass covered — `owner/name`, matching `plan_items.repo` and
    #: `plan_reconcile.repo` so all three key by the same spelling.
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    #: When the pass that wrote this ran. Server-side `now()` on both insert and
    #: update: the client's clock is not the one the ages are measured against,
    #: and a host with a skewed clock reporting a pass from the future would read
    #: as fresh for as long as the skew lasts.
    last_pass_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: The machine whose pass wrote this. Nullable for the same reason
    #: `plan_reconcile.reported_by` is: the row is worth having from a caller the
    #: board could not name, and a monitor that says "reconciled 3m ago" without a
    #: host is still answering the question that was asked.
    reported_by: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # One row per scope: every pass updates it, so the table stays the size of
        # the plan rather than growing with time, and two hosts landing together
        # leave one row saying whichever of them committed last. That is the same
        # "harmless" `report_reconcile` already relies on for the findings.
        UniqueConstraint("repo", name="uq_plan_reconcile_pass_repo"),
    )
