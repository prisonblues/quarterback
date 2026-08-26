"""A blocker is a row: what is waiting, on whom, for what answer — #328.

The fleet could already say *"item A waits on item B"* and could not say *"this
waits on Rich to answer a question"*. The gap was measured rather than assumed:
``counts.blocked`` read **0 across 20 open items** on a plan where three of them
carried a blocker written as English in ``note`` — *"RANK IS WRONG AND A HUMAN
MUST FIX IT"* among them. Countable by nobody, and rendered as ordinary open work.

**Why a row and not a post or a label.** A post says a thing happened; a blocker
is a thing that is *still true*, and the three questions worth asking about one
are all state questions — how many are open, how old is the oldest, and which are
mine. None is answerable over an append-only stream. ``ReviewFindingOutcome``
already makes this argument one level down: *"the refutation is already being
written, in the PR comment and the fix commit's message, in prose where nothing
can count it."*

**It does not replace the announcement.** #274's ``stuck`` post is the doorbell
and this is the queue behind it; a producer does both. That was the one design
question #328 left open and it answered it itself.

**The class list is imported, never re-spelled.** :mod:`app.needs_human` owns the
vocabulary (#279) and ``tests/test_post_type_drift.py`` exists because a second
copy of a closed list is how the two stop agreeing.

**Six classes, not seven.** #328 proposed adding ``authorisation`` — *may I do
this* — as distinct from #279's ``auth`` (*does the credential path work*). It is
not here, on two grounds. :mod:`app.needs_human` states its own rule for growing
the vocabulary: ``other`` *"is how the vocabulary GROWS — a class that keeps
turning up under `other` with the same reason is the evidence for adding a
word"*, and nothing has ever been filed under ``other`` because until this table
nothing was filed at all. And Rich, 2026-08-26, on whether the evidence is likely
to arrive: *"in general the agents are highly trusted, and have full and wide
autonomy to do gh actions. I don't think auth based limits are likely to be
common."* So the word would be speculative twice over. Widening the CHECK later
is a fifteen-line migration; narrowing one is not.
"""

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
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.needs_human import NEEDS_HUMAN_CLASSES

#: The subject kinds a blocker can name. The same ``(kind, value)`` shape `refs`
#: already uses, so a blocker on a PR and a board post about that PR agree on how
#: to spell it — and so :func:`app.claimkey.derive` can key them the same way.
SUBJECT_KINDS = ("item", "issue", "pr", "repo")

#: The question must fit on a line, because a blocker that cannot state its
#: question in one is not yet a blocker — it is a feeling about some work.
MAX_QUESTION = 500

#: The long half: options, what each costs, what the agent would do absent an
#: answer. Bounded like ``DialSetting.reason`` and for the same reason.
MAX_DETAIL = 8000

_CLASS_LIST = ", ".join(f"'{c}'" for c in NEEDS_HUMAN_CLASSES)
_KIND_LIST = ", ".join(f"'{k}'" for k in SUBJECT_KINDS)


class Blocker(Base):
    """One question a human owes an answer to, with a resolution when they give it.

    **Open blockers are unique on (subject, class).** A loop that re-raises the
    same question every run would otherwise fill the table, and the second row
    would say nothing the first did not. Re-raising is therefore a no-op that
    returns the existing row rather than an error: the caller's intent — *this is
    still blocked* — is satisfied either way, and refusing would make a producer
    choose between crashing and checking first.

    **``resolution`` is required to close one**, and that is the payload rather
    than bookkeeping: the next agent reads the human's own words, which is the
    whole reason this beats a label somebody ticked. An unblock with no
    resolution is how "waiting on a human" turns quietly back into a guess.
    """

    __tablename__ = "blockers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))

    #: NULL is fleet scope, the same convention :class:`~app.models.plan.Plan`
    #: and :class:`~app.models.dial.DialSetting` already use.
    repo: Mapped[str | None] = mapped_column(Text, index=True)

    #: What is blocked. Not a foreign key: the whole complaint in #328 is that
    #: ``depends_on`` takes plan-item uuids, so *a blocker with no item to point
    #: at is inexpressible*. An issue nobody has planned yet can be blocked.
    subject_kind: Mapped[str] = mapped_column(Text, nullable=False)
    subject_value: Mapped[str] = mapped_column(Text, nullable=False)

    #: #279's vocabulary, imported.
    kind: Mapped[str] = mapped_column(Text, nullable=False)

    question: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)

    #: Who is being asked — a board identity, or NULL for "any human". NULL is
    #: not "nobody": it is the queue everyone can see, and the difference matters
    #: to the ``⛔ N waiting on you`` chip, which must not claim work is yours.
    #: No plain index: ``ix_blockers_open_owner`` below is partial on
    #: ``resolved_at IS NULL`` and serves the only query that is hot — *what is
    #: waiting on me* — while a second, total index would be maintained on every
    #: answered row for the sake of a history read nobody makes in a loop.
    owner: Mapped[str | None] = mapped_column(Text)

    raised_by: Mapped[str] = mapped_column(Text, nullable=False)
    raised_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(f"kind IN ({_CLASS_LIST})", name="ck_blockers_kind"),
        CheckConstraint(f"subject_kind IN ({_KIND_LIST})",
                        name="ck_blockers_subject_kind"),
        CheckConstraint("length(question) > 0", name="ck_blockers_question_present"),
        # A resolved blocker carries all three or none of them. The biconditional
        # is #279's rule for `needs_human` evidence applied here: a flag with
        # nothing behind it is the confident assertion the vocabulary exists to
        # prevent, and "resolved, by nobody, saying nothing" is exactly that.
        CheckConstraint(
            "(resolved_at IS NULL AND resolved_by IS NULL AND resolution IS NULL)"
            " OR (resolved_at IS NOT NULL AND resolved_by IS NOT NULL"
            "     AND resolution IS NOT NULL AND length(resolution) > 0)",
            name="ck_blockers_resolution_complete"),
        # One OPEN blocker per (subject, class). Partial, so the history of
        # answered questions on one subject is kept in full — the resolutions are
        # the record worth having.
        Index("ix_blockers_open_subject", "repo", "subject_kind", "subject_value",
              "kind", unique=True, postgresql_where=text("resolved_at IS NULL")),
        Index("ix_blockers_open_owner", "owner",
              postgresql_where=text("resolved_at IS NULL")),
    )
