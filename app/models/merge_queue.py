from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
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

#: The states that admit a PR to the line. **Closed on purpose**: the whole value
#: of the queue is that a non-head is refused with a reason a machine can act on,
#: and a reason drawn from an open vocabulary is prose.
#:
#: The first two are `harness.loops.preland`'s own verdicts, lower-cased —
#: ``READY`` and ``RECONCILE`` — so the caller relays a verdict rather than
#: translating one, and a third preland verdict cannot be silently mapped onto an
#: existing meaning here. ``queued`` is the queue's own: nothing is wrong with
#: this PR except that it is not its turn, which is the state an entry lands in
#: after being refused by this very endpoint.
#:
#: All three are the issue's "plausibly landable": preland READY, or blocked only
#: by queue position or a stale base — both blocks that landing in turn dissolves.
#: preland ``HOLD`` is not, and :func:`app.api.merge_queue._admit` refuses it
#: rather than parking a PR in a line it cannot leave.
VERDICTS = ("ready", "reconcile", "queued")

#: Only ``ready`` lets the head actually merge. ``reconcile`` and ``queued`` say
#: "nothing is wrong with me except my turn and my base", which is exactly what
#: the head is about to fix by integrating — admissible, but not sufficient.
PROCEEDS = "ready"

_VERDICT_LIST = ", ".join(f"'{v}'" for v in VERDICTS)


class MergeQueueEntry(Base):
    """One PR's place in the line to land on ``repo``/``base`` — #227.

    ``kind='merge'`` in :class:`~app.models.resource_lease.ResourceLease` says
    *somebody is landing right now*. It cannot say who is next, who is second, or
    whether the agent about to spend twenty minutes of CI is anywhere near the
    front. So five ready PRs each rebased, pushed, waited for CI, re-ran preland,
    discovered somebody else had landed, and did it again — #80's quadratic
    integration cost, with every loser also invalidating the winners' green
    checks on the way past.

    This table is the ORDER, and nothing else. It is emphatically **not a second
    lock**: no path here takes, renews or releases a ``kind='merge'`` claim, and
    being at the head of the queue is not permission to merge — it is permission
    to *go and ask for the claim*. Two implementations of "who has this right
    now" is the outcome #99 was filed to avoid, and a queue that also held the
    resource would have been the second one.

    **Strict FIFO, by arrival, and only by arrival.** Every cleverer input the
    issue lists — file overlap, size, risk flags, plan dependencies — is
    deliberately absent, because #227's own argument is that *"agents may propose
    order; they must not silently rewrite the queue while also trying to land…
    otherwise the queue itself becomes another shared resource every agent
    thrashes."* A deterministic order cannot thrash. Ordering proposals are the
    second half of the issue and are not implemented here.

    **Position is fixed at arrival and re-enqueueing does not move you.**
    ``entered_at`` is written once and never bumped; ``updated_at``, ``head_sha``
    and the expiry all move on every idempotent re-enqueue. The alternative —
    treating each enqueue as a fresh arrival — would send an agent that politely
    reported its new head to the back of a line it was at the front of, which is
    the same "spend work to discover you are still waiting" this exists to stop.

    **Abandonment is passive**, borrowed wholesale from
    :class:`~app.models.resource_lease.ResourceLease` because it is the same
    problem: an entry is live while ``left_at IS NULL AND expires_at > now()``, a
    crashed agent stops renewing, and the queue advances with nobody
    intervening. No reaper — a wedged head would block everybody's landing, which
    is worse here than a wedged session lease is there.

    **A head change invalidates readiness, and does not cost the slot.**
    ``ready_sha`` is the commit at which ``ready`` was asserted; ``head_sha`` is
    where the PR is now. Reporting a new head clears the first unless preland is
    re-run and re-asserted at that head, and a reader that already knows the PR's
    real head (``GET /merge-queue?pr=&head=``) is told the entry is behind it
    without anything having to be written. What invalidation does *not* do is
    demote the entry: pushing is precisely the work a queue head's slot is for,
    and dropping it to second for doing that would hand the slot to a PR that then
    invalidates the first one's checks — the thrash this table exists to stop.
    #227's acceptance list also reads "no longer READY" as an advancement
    trigger; the passive expiry above is what covers the case it is really about,
    an entry that goes quiet and never comes back.
    """

    __tablename__ = "merge_queue_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: ``owner/name``, canonicalised through :func:`app.claimkey.canonical_repo` —
    #: the same folding the claim key gets, because a queue on ``Acme/Widget`` and
    #: a claim on ``acme/widget`` describing one repository is #148 reproduced in
    #: a new table.
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    #: The branch being landed ONTO. Case is not folded: ``main`` and ``Main`` are
    #: two refs, and :func:`app.claimkey._branch` says so for the merge key this
    #: queue sits beside.
    base: Mapped[str] = mapped_column(Text, nullable=False)
    pr: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Where the PR is now, as last reported by whoever enqueued it.
    head_sha: Mapped[str] = mapped_column(Text, nullable=False)
    #: The commit at which this PR was last asserted :data:`PROCEEDS`-ready, and
    #: NULL once a push has invalidated that. Kept as a separate column from
    #: :attr:`head_sha` rather than collapsed into a boolean because a boolean
    #: cannot be checked by a third party: a peer, or the entry's own agent after
    #: a restart, can compare this against the commit GitHub actually reports and
    #: see for itself whether the readiness on file is about the PR as it stands.
    ready_sha: Mapped[str | None] = mapped_column(Text)
    #: One of :data:`VERDICTS`, as asserted by the caller about :attr:`head_sha`.
    #: The board cannot run preland, read CI or ask GitHub whether a PR is a
    #: draft, so this is testimony rather than measurement — what the board adds
    #: is that the testimony names the commit it is about, so it can be shown to
    #: have expired instead of being remembered as true.
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    holder: Mapped[str] = mapped_column(Text, nullable=False)  # token name (see auth.identify)
    #: Which agent on the holder's machine is driving this PR — a box runs
    #: several, and "who do I talk to about position 1" needs the finer address.
    session: Mapped[str | None] = mapped_column(Text)
    #: One line on what this entry is for, shown to everyone queued behind it.
    note: Mapped[str | None] = mapped_column(Text)
    #: The FIFO key. Written once; see the class docstring.
    entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ttl_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    left_by: Mapped[str | None] = mapped_column(Text)
    #: Why it left, in the leaver's words: ``merged``, ``closed``, ``superseded``.
    #: Required by the endpoint, because "the head vanished" with no why is the
    #: obstruction the claim table's ``note`` already exists to prevent.
    left_reason: Mapped[str | None] = mapped_column(Text)
    #: TRUE when the TTL swept it rather than the holder standing down. "Landed"
    #: and "stopped answering" are different facts about a queue head, and a
    #: dashboard that showed them alike would report an abandoned land as a
    #: finished one — the same distinction
    #: :attr:`app.models.resource_lease.ResourceLease.lapsed` carries.
    lapsed: Mapped[bool] = mapped_column(server_default=text("false"), nullable=False)

    __table_args__ = (
        CheckConstraint("pr > 0", name="ck_merge_queue_pr"),
        CheckConstraint("length(btrim(head_sha)) > 0", name="ck_merge_queue_head_sha"),
        CheckConstraint(f"verdict IN ({_VERDICT_LIST})", name="ck_merge_queue_verdict"),
        # A row claiming to be ready must be ready AT THE COMMIT IT IS ON. This is
        # the one guarantee the table adds over an agent's own memory — an agent
        # remembers "preland said READY" and does not reliably notice that the
        # thing preland said it about was three pushes ago — so it is enforced by
        # the database rather than by every write path remembering to.
        CheckConstraint(f"verdict <> '{PROCEEDS}' OR ready_sha = head_sha",
                        name="ck_merge_queue_ready_at_head"),
        # Idempotency, as a database fact. A second enqueue for a PR already in
        # the line updates that row; it cannot create a second place in the queue
        # for one PR, however many agents or retries ask.
        Index("ix_merge_queue_open", "repo", "base", "pr", unique=True,
              postgresql_where=text("left_at IS NULL")),
        # The read: one queue, in order. `pr` breaks a tie because two entries can
        # share a timestamp and the order must not depend on which row the planner
        # happens to return first — a queue that reports two different heads on
        # two reads is worse than no queue.
        Index("ix_merge_queue_order", "repo", "base", "entered_at", "pr"),
    )
