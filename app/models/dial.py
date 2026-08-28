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

    #: ``owner/name`` lower-cased, or NULL for every repo on the fleet. Folded on
    #: the write through :func:`app.claimkey.canonical_repo` and held there by
    #: ``ck_dial_settings_repo_canonical`` — see that constraint for why the
    #: unique index below needs it (#350).
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

    #: Who set it — a person (``human/rich``) or, since #591, the agent a person
    #: delegated to (``hermes/mist-harbour``). Never a bare machine token: writes
    #: take :func:`app.auth.delegated` (see :func:`app.api.dials.set_dial`), which
    #: refuses a bearer presented on its own.
    #:
    #: An agent keeps its OWN name here rather than borrowing the name of whoever
    #: asked it. That is the whole reason the capability arrived as a second
    #: credential instead of a lent-out first one: the design #479 records as
    #: rejected would have written ``human/rich`` for a dial an agent turned, and
    #: no later reader could have told the two apart. Read this column with
    #: ``set_via`` below, which says which of the two it was.
    set_by: Mapped[str] = mapped_column(Text(), nullable=False)
    set_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    #: HOW it was proved — `edge`, `key`, `dev` or `agent` (:mod:`app.auth`).
    #:
    #: The first three are all a PERSON, and the identity above is the same by any
    #: of those doors, deliberately: a person is one author however they arrived.
    #: But "a browser Authelia vouched for" and "a key sitting on a workstation"
    #: are not the same event, and the second carries a residual this repo writes
    #: down rather than argues away — anything running as that user can read the
    #: key and author as them (#479). A row that recorded only `set_by` could not
    #: tell the two apart afterwards, which is exactly the moment somebody asks.
    #:
    #: `agent` is the fourth and it is a different KIND of answer: not a door a
    #: person came through, but a statement that no person was in the request at
    #: all — an agent presented its machine's delegated secret and `set_by` is
    #: that agent (#591). It is the dial equivalent of `rank_source: "derived"` on
    #: a plan order, and it exists so that "Rich turned this floor down" and "an
    #: agent turned this floor down because Rich told it to" are two facts and not
    #: one. They are not equally strong evidence about intent, and a reader
    #: deciding whether to trust a dial needs to be able to see which it is
    #: holding.
    #:
    #: NULLABLE, and null is "not recorded" rather than "some other method": every
    #: row written after this column existed has one, and the rows written before
    #: it honestly have no answer. Back-filling a guess would put the one value a
    #: reader must be able to distrust into the column they consult to decide
    #: whether to trust it.
    set_via: Mapped[str | None] = mapped_column(Text(), nullable=True)

    #: NULL is indefinite. A past timestamp is absent, not expired-and-reported:
    #: "a resolution with no dial layer is indistinguishable from one that never
    #: had it" is #276's requirement and it is met by not returning the row.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    cleared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    cleared_by: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: And how THEY proved it. `cleared_by` exists so "who moved the floor, when,
    #: and what did they say about it" survives the next person moving it; the
    #: argument for recording the method on a write is the same argument on the
    #: write that ends it, so recording only half would be an odd place to stop.
    cleared_via: Mapped[str | None] = mapped_column(Text(), nullable=True)

    __table_args__ = (
        CheckConstraint("length(btrim(dial)) > 0", name="ck_dial_settings_dial"),
        CheckConstraint(f"length(dial) <= {MAX_DIAL}", name="ck_dial_settings_dial_len"),
        CheckConstraint("length(btrim(reason)) > 0", name="ck_dial_settings_reason"),
        CheckConstraint("repo IS NULL OR length(btrim(repo)) > 0",
                        name="ck_dial_settings_repo"),
        # One repository, one stored spelling (#350, migration 0034). `POST
        # /dials` folds through `canonical_repo` now, and this is what makes the
        # unique index above mean what it says: without it `Acme/X` and `acme/x`
        # are two rows in the index and two live values for one dial, and a
        # resolution answers with whichever spelling the reader's origin remote
        # happened to carry.
        #
        # Case and surrounding whitespace only, NOT `owner/name` shape — the
        # shape is refused at ingest where a caller can be told why, and a
        # constraint that also asserted it would make 0034 abort on any legacy
        # row instead of making it canonical. `btrim` is given its character
        # class because the one-argument form trims ordinary spaces and nothing
        # else, while `canonical_repo`'s `str.strip()` takes the lot; vertical
        # tab is `\013` and never `\v`, because Postgres has no `\v` escape and
        # the class would gain the LETTER `v` — `btrim('vercel/next', …)` is
        # `'ercel/next'`, and this constraint would refuse a repository for being
        # named after its owner.
        CheckConstraint(r"repo IS NULL OR repo = lower(btrim(repo, E' \t\n\r\f\013'))",
                        name="ck_dial_settings_repo_canonical"),
        # COALESCE, not the bare column: a UNIQUE index treats two NULLs as
        # distinct, so the fleet scope — the one #276's throttle writes to —
        # would have been the one scope that could hold two contradictory live
        # rows for the same dial, with the resolver picking whichever the planner
        # returned first.
        Index("ix_dial_settings_live", text("COALESCE(repo, '')"), "dial",
              unique=True, postgresql_where=text("cleared_at IS NULL")),
        Index("ix_dial_settings_scope", "repo", "dial"),
    )
