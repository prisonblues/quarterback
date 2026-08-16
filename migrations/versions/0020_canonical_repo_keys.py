"""v2.38: one repo, one namespace — canonicalise the repo half of every claim key

`resource_leases.key` is built from a repo string the caller supplies as free
text, and the fleet supplies two of them for one repo. `qb-hook` derives repo
identity from the origin remote and takes the *basename* (`quarterback`); `gh`
and every review payload use GitHub's `nameWithOwner` (`prisonblues/quarterback`).
Both are locally correct, and the allocator's atomicity — a partial unique index
over `(kind, key)` — is only unique within a spelling. So the board kept two
independent release sequences over one repo and **handed 2.36 to two agents 28
minutes apart, `claimed: true` on both**. That is #148/#150.

The endpoints now normalise on the way in (see `app/repokey.py`). This migration
is the other half: without it the first post-fix allocation reads a floor that is
missing whatever is stranded under the other spelling, and re-issues it.

## What it does to the rows

Every key's repo head is rewritten to canonical `owner/name`, lowercased, when
this board can positively identify the head as a repo it has seen — drawn from
`review_runs.repo`, which is `nameWithOwner` by documented contract, and from
`kind='release'` claim keys, which only the allocator can write. Those two
sources are the whole expansion table on purpose: reading every claim key
regardless of kind would let anybody mint a repo identity by taking a generic
claim on `attacker/thing#1`.

**A head that cannot be identified is left exactly as it is.** Guessing at an
owner would coin the third namespace this whole change exists to prevent, and
those rows cannot grow: the endpoints refuse that spelling now, so nothing new
lands beside them. `_repo_prefix` keeps reading them so a stranded number still
raises the floor it belongs to.

## Two live rows can converge, and that is the bug's own output

`quarterback:2.36` and `prisonblues/quarterback:2.36` were both held on the day
this was written. The partial unique index will admit one. So the later-acquired
one is **released as part of the rewrite** — it keeps its canonical key, because
history has to record that the number was handed out twice or the floor forgets
it, and it stops being live. First-claim-wins, applied to a fact that was already
true and merely unrepresentable.

## Why this revision imports `app.repokey` instead of vendoring it

The usual rule is that a migration vendors its logic, so that re-running it years
later reproduces what it produced today. **That argument does not reach this
revision, and it is worth saying why rather than waving at it.** A migration on a
fresh database runs against an empty `resource_leases` and an empty `review_runs`
— every row it could rewrite is one this revision has already passed over — so
there is no "what it produced today" to reproduce. The only database where 0020
does any work at all is one that already carried pre-v2.38 rows when it ran, and
it runs there exactly once.

What can actually go wrong here is the opposite failure: a vendored copy drifting
from `app/repokey.py` and rewriting a key into a form the endpoints of the day
cannot read. The rows this touches are read back immediately by
`_highest_known`, `_repo_prefix` and `_canonical_key`; agreeing with those is the
correctness condition, not agreeing with 2026. Sharing the code makes that
agreement structural. `tests/test_v238.py` pins it from both ends — the pure
planner and this `upgrade()` against a real Postgres.

The revision number and the release number are unrelated counters: this is schema
revision **0020** and it ships in product version **v2.38**.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from app.repokey import (
    LeaseRow,
    canonical_key_of,
    canonical_repo,
    known_repos_from,
    plan_rewrites,
    split_repo_head,
)

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_log = logging.getLogger("alembic.runtime.migration")


def _known_repos(bind: sa.Connection) -> set[str]:
    """Every canonical `owner/name` this board has seen, from its two trusted sides.

    Same two sources as the runtime's `_repos_named`, and restricted for the same
    reason: `review_runs.repo` is written by the review pipeline and a
    `kind='release'` key only by the allocator, so neither can be minted by
    taking a claim. Reading every key regardless of kind meant one legal
    `kind='deploy', key='attacker/thing#1'` taught the migration that
    `attacker/thing` is a repo, and any historical row keyed on the bare basename
    `thing` was then rewritten into somebody else's namespace.
    """
    return known_repos_from(
        r for (r,) in bind.execute(sa.text(
            "SELECT repo FROM review_runs "
            "UNION SELECT key FROM resource_leases WHERE kind = 'release'")))


def _free_the_seats(bind: sa.Connection, raw: list, known: set[str],
                    now: datetime) -> set:
    """Sweep the expired-but-unswept rows sitting on a key a rewrite has to land on.

    The unique index is partial on `released_at IS NULL` and CANNOT test
    `expires_at` — a partial predicate has to be immutable — so an
    expired-but-unswept row still occupies its key, and rewriting another row
    onto that key aborts the whole migration. The rows this is likeliest to
    happen to are precisely the ones it exists for.

    **Scoped to contended seats, and the scoping is the point.** Sweeping every
    expired claim on the table would also stamp a fresh `released_at` and
    `lapsed = true` on `deploy` and `work` rows for repos this migration does not
    touch — rows whose real lapse time was days earlier. Every read path already
    treats them as gone, so nothing behaves differently, but the timestamp saying
    *when* a claim died is the one fact nobody can reconstruct afterwards, and a
    schema migration has no business overwriting it.

    A seat is contended when more than one unreleased row canonicalises onto it,
    and that is exactly the set where the index can bite: a rewrite lands on a
    seat, and anything else unreleased sitting there canonicalises onto the same
    seat by definition. Where the seat is contended and one of the occupants has
    expired, sweeping it is also the right answer on the merits — otherwise
    `plan_rewrites` awards the seat to whoever acquired first, and a dead claim
    would take a number off a live one purely because nothing had got round to
    sweeping it.

    Returns the ids it released, so the caller can score `held` without re-reading.
    """
    seats = {r.id: (r.kind, canonical_key_of(r.key, known)) for r in raw}
    contended = Counter(seats[r.id] for r in raw if r.released_at is None)
    swept = {r.id for r in raw
             if r.released_at is None and r.expires_at <= now
             and contended[seats[r.id]] > 1}
    if swept:
        bind.execute(
            sa.text("UPDATE resource_leases SET released_at = :now, lapsed = true "
                    "WHERE id IN :ids").bindparams(sa.bindparam("ids", expanding=True)),
            {"now": now, "ids": sorted(swept, key=str)})
    return swept


def _apply(bind: sa.Connection, plans: list, now: datetime) -> None:
    """Write the plan out, releases before renames.

    The order between the two groups is the contract `plan_rewrites` documents:
    apply a rewrite before the duplicate it lands on has let go and the partial
    unique index refuses it mid-migration. It is re-derived here by partitioning
    rather than trusted from the incoming order, so the one thing that aborts
    this migration is enforced where the statements are actually issued. The
    order *within* a group is not load-bearing, so each group is one statement
    with many parameter sets rather than a round trip per row — on a board with
    years of claims that is the difference between a lock held for a moment and
    one held for a while.
    """
    released = [p for p in plans if p.release]
    renamed = [p for p in plans if not p.release and p.new_key != p.old_key]
    if released:
        # Released, not lapsed: `lapsed` means the TTL swept it, and saying so
        # here would record that the holder vanished when in fact the board took
        # the claim off it. The note carries the truth, because a holder finding
        # its number gone deserves to read why rather than infer it.
        bind.execute(
            sa.text("UPDATE resource_leases "
                    "SET key = :key, released_at = :now, "
                    "    note = coalesce(note || ' — ', '') || :reason "
                    "WHERE id = :id"),
            [{"key": p.new_key, "now": now, "reason": p.reason, "id": p.id}
             for p in released])
    if renamed:
        bind.execute(
            sa.text("UPDATE resource_leases SET key = :key WHERE id = :id"),
            [{"key": p.new_key, "id": p.id} for p in renamed])

    for plan in renamed:
        _log.info("0020: %s -> %s (%s)", plan.old_key, plan.new_key, plan.reason)
    for plan in released:
        # WARNING, not info, and naming the holder: this is the one action here
        # that takes something away from a live agent. `GET /releases` reports a
        # loser identically to any other released row, so the operator reading
        # this log is the only party who can go and tell them.
        _log.warning("0020: released %s (now %s) held by %s session %s — %s",
                     plan.old_key, plan.new_key, plan.holder, plan.session, plan.reason)


def _warn_about_the_stranded(rows: list[LeaseRow], plans: list) -> None:
    """Say out loud which release keys this could not identify a repo for.

    A silently stranded row is a number the allocator can no longer see under the
    spelling anybody asks with — and "nothing to report" is exactly what this
    migration looks like when it has left work behind.
    """
    rewritten = {p.id for p in plans if p.new_key != p.old_key}
    stranded = sorted({
        # Release keys only. A generic key may legitimately name no repo at all
        # (`kind='deploy', key='portainer-stack-189'`), so warning about those
        # would be noise — and noise is how a real warning gets skimmed past.
        r.key for r in rows
        if r.kind == "release" and r.id not in rewritten
        and canonical_repo(split_repo_head(r.key)[0]) is None
    })
    if stranded:
        _log.warning("0020: %d key(s) name a repo this board cannot identify and were "
                     "left as they are: %s", len(stranded), ", ".join(stranded))


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC)
    known = _known_repos(bind)

    raw = list(bind.execute(sa.text(
        "SELECT id, kind, key, holder, session, acquired_at, released_at, expires_at "
        "FROM resource_leases")))
    swept = _free_the_seats(bind, raw, known, now)
    rows = [
        # `held`, not "live": the index's own predicate. See LeaseRow.
        LeaseRow(id=r.id, kind=r.kind, key=r.key, acquired_at=r.acquired_at,
                 held=r.released_at is None and r.id not in swept,
                 holder=r.holder, session=r.session)
        for r in raw
    ]
    plans = plan_rewrites(rows, known)
    _apply(bind, plans, now)
    _warn_about_the_stranded(rows, plans)


def downgrade() -> None:
    """Deliberately a no-op — and this is the honest answer, not a shortcut.

    The spelling a row was written with is not recorded anywhere else, so there
    is nothing to restore it from; and a claim released because it collided with
    an earlier one cannot be un-released without handing one number back to two
    holders, which is the failure being fixed. Downgrading the schema past 0020
    leaves the keys canonical, which every pre-0020 code path reads correctly —
    it simply keeps the two spellings apart again for anything written after.
    """
