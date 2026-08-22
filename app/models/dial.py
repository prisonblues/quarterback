from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: The longest a dial name may be. A dotted path into the harness's own rules
#: tree (``review_panel.fix_severity_floor``), so this is generous rather than
#: tight — the board does not know the vocabulary and must not appear to.
MAX_DIAL = 200

#: The longest a reason may be. A dial that outlives its argument is the failure
#: ``expires_at`` exists for; a reason nobody can read is the same failure
#: arriving through prose, so it is stored in full up to a sane bound.
MAX_REASON = 2000


class DialSetting(Base):
    """One harness dial, set on the board, in force until it expires — #305.

    ``review_panel.fix_severity_floor`` decides which findings a fix pass may
    touch and ``round_trigger_floor`` decides which ones buy another round.
    Between them they decide what a review costs and what it is worth, and until
    this table existed **changing either was a commit on a pull request**,
    reviewed by the panel those very dials configure. That is the wrong shape for
    a policy knob, and it is how ``.harness-rules.sample`` came to claim both
    floors sat at P2 while every round on #299 put P4 findings in ``to_fix``: the
    file that states the policy and the rounds that applied it disagreed, and
    nothing could be *asked* which was right.

    **The board stores testimony, not vocabulary.** ``dial`` is opaque text and
    ``value`` is opaque JSON. This table does not know that
    ``review_panel.max_rounds`` is an integer, that ``fix_severity_floor`` is a
    severity band, or that ``reviewers.pi.enabled`` is the one dial an unreviewed
    channel may only narrow. It cannot: the harness ships its dial table in
    ``harness/loops/harness_rules.py`` and the server image carries no ``harness/``
    directory at all, so a copy here would be a **second place a dial is written
    down** — which is exactly the confusion #56's rule and this issue exist to
    end. The client owns the vocabulary, validates on read, and reports by name
    anything it refused. ``merge_queue_entries`` made the same choice in the same
    words: the board takes testimony, not measurements.

    **A repo dial beats a fleet dial.** ``repo`` NULL means every repo — which is
    the scope a budget throttle needs (#276: the five-hour window is one number
    shared by every project and machine on the subscription) — and a row naming
    ``prisonblues/quarterback`` overrides it for that repo alone. Two scopes, one
    table, and the resolver reports which one answered.

    **It expires by itself.** ``expires_at`` NULL is indefinite, which is what a
    floor somebody means to keep wants; a timestamp is what a fortnight's
    experiment wants, and an expired row is simply absent from every read. Not a
    flag somebody clears: the failure this closes is a temporary setting that
    outlives its reason and quietly becomes the permanent one, with nothing
    saying it is still in force.

    **History is kept, so the slot is cleared rather than deleted.**
    ``ix_dial_settings_live`` is UNIQUE over ``cleared_at IS NULL``, so at most
    one row per (repo, dial) is outstanding and "who moved the floor, when, and
    what did they say about it" survives the next person moving it. The predicate
    cannot also exclude expired rows — ``now()`` is not immutable and Postgres
    will not index on it — so a write clears whatever occupies the slot, expired
    or not, and inserts beside it.
    """

    __tablename__ = "dial_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))

    #: ``owner/name``, or NULL for every repo on the fleet.
    repo: Mapped[str | None] = mapped_column(Text(), nullable=True)

    #: The dotted path into the harness rules tree. Opaque here — see the class
    #: docstring for why the board must not learn the vocabulary.
    dial: Mapped[str] = mapped_column(Text(), nullable=False)

    #: ``{"value": <any JSON>}``, and the wrapper is load-bearing. A bare JSONB
    #: column cannot tell the JSON value ``null`` — which is the documented off
    #: switch for ``max_fix_growth``, ``distant_merge_lines`` and
    #: ``escalate_on.premise_repeated`` — from SQL NULL, because SQLAlchemy
    #: serialises Python ``None`` to the latter. Wrapping makes "set it to null"
    #: and "there is no row" two different facts, which is what they are.
    value: Mapped[dict] = mapped_column(JSONB(), nullable=False)

    #: Why. Required, and there is no empty-string escape: a dial whose argument
    #: was never written down is one nobody can decide to remove.
    reason: Mapped[str] = mapped_column(Text(), nullable=False)

    #: The human who set it. Writes are human-authenticated (see
    #: :func:`app.api.dials.set_dial`), so this is a person's name and not a
    #: machine token's.
    set_by: Mapped[str] = mapped_column(Text(), nullable=False)
    set_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    #: NULL is indefinite. A past timestamp is absent, not expired-and-reported:
    #: "a resolution with no dial layer is indistinguishable from one that never
    #: had it" is #276's requirement and it is met by not returning the row.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    cleared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    cleared_by: Mapped[str | None] = mapped_column(Text(), nullable=True)

    __table_args__ = (
        CheckConstraint("length(btrim(dial)) > 0", name="ck_dial_settings_dial"),
        CheckConstraint(f"length(dial) <= {MAX_DIAL}", name="ck_dial_settings_dial_len"),
        CheckConstraint("length(btrim(reason)) > 0", name="ck_dial_settings_reason"),
        CheckConstraint("repo IS NULL OR length(btrim(repo)) > 0",
                        name="ck_dial_settings_repo"),
        # COALESCE, not the bare column: a UNIQUE index treats two NULLs as
        # distinct, so the fleet scope — the one #276's throttle writes to —
        # would have been the one scope that could hold two contradictory live
        # rows for the same dial, with the resolver picking whichever the planner
        # returned first.
        Index("ix_dial_settings_live", text("COALESCE(repo, '')"), "dial",
              unique=True, postgresql_where=text("cleared_at IS NULL")),
        Index("ix_dial_settings_scope", "repo", "dial"),
    )
