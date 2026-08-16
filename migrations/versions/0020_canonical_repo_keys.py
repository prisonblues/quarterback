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

Every key's repo head is rewritten to canonical `owner/name`, lowercased. A bare
basename is expanded when exactly one repo the board has seen answers to that
name — drawn from `review_runs.repo`, which is `nameWithOwner` by documented
contract, and from claim keys already written in full.

**A basename that cannot be expanded is left exactly as it is.** Guessing at an
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

The revision number and the release number are unrelated counters: this is schema
revision **0020** and it ships in product version **v2.38**.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from app.repokey import LeaseRow, canonical_repo, plan_rewrites, split_repo_head

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_log = logging.getLogger("alembic.runtime.migration")


def _known_repos(bind: sa.Connection) -> set[str]:
    """Every canonical `owner/name` this board has seen, from both sides of it."""
    known = {
        canonical_repo(r)
        for (r,) in bind.execute(sa.text(
            "SELECT DISTINCT repo FROM review_runs WHERE repo IS NOT NULL"))
    }
    known |= {
        canonical_repo(split_repo_head(k)[0])
        for (k,) in bind.execute(sa.text("SELECT DISTINCT key FROM resource_leases"))
    }
    return {r for r in known if r is not None}


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC)

    # Sweep first, exactly as `_sweep_lapsed` does for one key. The unique index
    # is partial on `released_at IS NULL` and CANNOT test `expires_at` — a
    # partial predicate has to be immutable — so an expired-but-unswept row still
    # occupies its key. Rewriting another row onto that key aborts the whole
    # migration, and the rows this is likeliest to happen to are the ones it
    # exists for. Sweeping is not a side effect: every read path already reports
    # these as gone, and the next claim on that key would sweep them anyway.
    bind.execute(
        sa.text("UPDATE resource_leases SET released_at = :now, lapsed = true "
                "WHERE released_at IS NULL AND expires_at <= :now"),
        {"now": now})

    rows = [
        # `held`, not "live": the index's own predicate. See LeaseRow.
        LeaseRow(id=r.id, kind=r.kind, key=r.key, acquired_at=r.acquired_at,
                 held=r.released_at is None)
        for r in bind.execute(sa.text(
            "SELECT id, kind, key, acquired_at, released_at FROM resource_leases"))
    ]
    # Releases first — `plan_rewrites` guarantees that order, and applying a
    # rewrite before the duplicate it lands on has let go would hit the very
    # index this is all about.
    plans = plan_rewrites(rows, _known_repos(bind))

    for plan in plans:
        if plan.release:
            # Released, not lapsed: `lapsed` means the TTL swept it, and saying so
            # here would record that the holder vanished when in fact the board
            # took the claim off it. The note carries the truth, because a holder
            # finding its number gone deserves to read why rather than infer it.
            bind.execute(
                sa.text("UPDATE resource_leases "
                        "SET key = :key, released_at = :now, "
                        "    note = coalesce(note || ' — ', '') || :reason "
                        "WHERE id = :id"),
                {"key": plan.new_key, "now": now, "reason": plan.reason, "id": plan.id})
        elif plan.new_key != plan.old_key:
            bind.execute(sa.text("UPDATE resource_leases SET key = :key WHERE id = :id"),
                         {"key": plan.new_key, "id": plan.id})
        _log.info("0020: %s -> %s (%s)", plan.old_key, plan.new_key, plan.reason)

    # Loud about what it could NOT do, because a silently stranded row is a number
    # the allocator can no longer see under the spelling anybody asks with — and
    # "nothing to report" is exactly what this migration looks like when it has
    # left work behind.
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


def downgrade() -> None:
    """Deliberately a no-op — and this is the honest answer, not a shortcut.

    The spelling a row was written with is not recorded anywhere else, so there
    is nothing to restore it from; and a claim released because it collided with
    an earlier one cannot be un-released without handing one number back to two
    holders, which is the failure being fixed. Downgrading the schema past 0020
    leaves the keys canonical, which every pre-0020 code path reads correctly —
    it simply keeps the two spellings apart again for anything written after.
    """
