"""One repository, one spelling — for the review tables this time (#326)

`review_runs.repo` and `review_finding_outcomes.repo` were stored exactly as the
panel sent them, and every query in `app/api/reviews.py` compared them with `==`.
GitHub folds owner and repository names while preserving what you typed, so
`PrisonBlues/Quarterback` and `prisonblues/quarterback` are one repository the
board held as two, and the consequences were both silent:

* `GET /review/collisions` 404ed — "no run of X#12 recorded a changed-file list",
  which reads as "this PR was never panelled";
* or it found the subject and matched no rivals, answering `counts.considered: 0`
  — an all-clear produced by nothing having matched, on the one endpoint written
  to make absence unrepresentable.

The second is the dangerous direction. A lander reads `considered: 0` as "landing
this disturbs nothing".

## Why the write, and not a fold at each read

#232 fixed the same defect at one read site — `_pr_evidence` folds both sides —
and the fix did not reach #101's own endpoint because the two branches were in
flight together. Per #67 the second instance closes the class rather than being
patched again, and the alternatives are not equal:

* **Folding at each read** is what already existed. It has to be remembered by
  every query written afterwards, which is precisely how this became the second
  instance; it costs `ix_review_runs_repo_pr` at every site that does it; and it
  cannot reach the read paths that never compare a repo at all — the
  `COUNT(DISTINCT repo)` in `/review/stats`, the `(repo, pr, finding_key)` tuples
  the needs-human chain view keys a Python dict by, or the
  `ReviewFindingOutcome.repo = ReviewRun.repo` join that decides whether a defect
  has been answered. Twelve sites, and the next one is somebody's next commit.
* **Folding on the write** is one validator per table, and migration `0022` has
  already settled the argument for release keys in words that need no amending:
  *resolving once, on write, makes the alias set empty by construction.*

So `app.api.reviews` routes both write paths through `app.claimkey.canonical_repo`
— the same function the claim keys, the merge queue and the plan already use —
this migration folds the rows written before that, and a CHECK constraint on each
column holds it there. The constraint is the part that closes the class: a write
path added later cannot quietly reintroduce a second spelling, and the read sites
that stopped folding (`app.api.plan._pr_evidence`, four in `app.api.review_queue`)
now depend on the column rather than on an endpoint's memory.

## What the constraint does NOT say

Case and surrounding whitespace only, not `owner/name` shape. The shape is
refused at ingest, where a caller can be told why; rows written before that check
existed are legitimately here — this board holds a run under `acme/v237-hash#1`
and one whose repo carries an ingest diagnostic and a newline — and a constraint
that rejected them would make this migration unrunnable rather than make them
canonical. Folding their case is still right and still cheap.

## Two bounds this deliberately has, both named because they are choices

**The constraint is looser than `canonical_repo`, and has to be.** It folds case
and the six ASCII whitespace characters `btrim` can be given; `str.strip()` takes
every Unicode space besides. Tightening it to "no whitespace anywhere" would be
the honest rule for what the API now writes and would also refuse the legacy rows
above, which would make this migration abort rather than run — so the constraint
is a backstop against the plausible mistake (a write path that forgets to fold)
rather than an encoding of the whole ingest rule. Nothing can produce the residual
shape: `REPO_RE` admits no whitespace at all, so a Unicode-padded value cannot
reach the column through any endpoint, and one inserted around them was equally
unreachable before this revision.

**Legacy rows in a shape the endpoints refuse become unreachable BY REPO.** The
`?repo=` parameters canonicalise now, so `?repo=acme/v237-hash#1` is a 422 where
it used to be a 200 over that row. That is the point rather than a side effect —
the alternative is `?repo=quarterback` answering `[]`, which is the false-clean
this issue is about — and the rows are not orphaned: `GET /reviews` with no repo
filter still lists them and `GET /review/{run_id}` still fetches one by id.

## Why the outcome clash is refused rather than resolved

`review_finding_outcomes` is UNIQUE on `(repo, pr, finding_key)`, so folding can
put two rows on one key. Those two rows are two terminal answers to "what happened
to this defect?" — one may say `fixed` and the other `refuted` — and picking the
newer is a guess about which one stands, made by a migration, about a table that
feeds a published precision figure. The whole reason `revisions` and
`prior_outcome` exist is that an outcome quietly changing is not acceptable. So
this stops and names the rows with the SQL to settle them, the way `0022` and
`0031` stop. On the board this was written against there are none: every row in
both tables is already lower case, and this migration is a no-op over the data and
a guarantee about the next write.

## What this revision has to run BEFORE

`app.api.plan._pr_evidence` and four queries in `app.api.review_queue` carried a
`func.lower()` over these columns and no longer do — they compare the column,
because the column is now guaranteed. So the new code is correct only against a
database at this revision, and the thing that makes that safe is the Dockerfile's
`alembic upgrade head && uvicorn`: the container migrates before it serves and
exits if the migration fails, so no request is ever answered by new code on an
old schema. **That deploy shape is a dependency of this revision, not a detail**
— the same comment that promises it also says a migration lock is needed before
scaling to multiple replicas, and this is one of the things that would need it.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-22

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The columns this folds, and the CHECK each one gains. Both hold a GitHub
#: `owner/name` with the same meaning, and a spelling folded in one table and not
#: the other is a run whose outcomes no longer join to it — `/review/stats` reads
#: `review_finding_outcomes.repo = review_runs.repo` as a column-to-column join.
_TABLES = (
    ("review_runs", "ck_review_runs_repo_canonical"),
    ("review_finding_outcomes", "ck_review_finding_outcomes_repo_canonical"),
)

#: The canonical form, as SQL. `app.claimkey.canonical_repo` strips and lower-cases;
#: this is that operation and its assertion, spelled out rather than imported for
#: the reason `0022` and `0031` give — a migration is a statement about the
#: database at one moment, and a rule it imports can move underneath it.
#:
#: **The character class is named, because one-argument `btrim` trims spaces and
#: nothing else.** Python's `str.strip()` takes every whitespace character, so
#: `btrim(repo)` alone would leave a tab- or newline-padded legacy row standing —
#: a value the constraint then declares canonical and `canonical_repo` does not,
#: which is two rules disagreeing about one column in the file written to stop
#: exactly that. This board holds one such row already: a repo carrying an ingest
#: diagnostic and a newline, from before the shape was checked.
#:
#: **Vertical tab is `\013`, never `\v`.** Postgres's escape-string syntax has no
#: `\v`, so a backslash before it is dropped and the class gains the *letter* `v`
#: — `btrim('vercel/next', E' \t\n\r\f\v')` is `'ercel/next'`, which would make
#: this migration reject a repository for being named after its owner. The models
#: already spell it `\013` for the same reason (`needs_human_reason`'s evidence
#: check); this is that fact, not a new one.
_WHITESPACE = r"E' \t\n\r\f\013'"
_CANON = f"lower(btrim(repo, {_WHITESPACE}))"


def plan_folds(
    rows: list[tuple[object, str, int, str, str]],
) -> list[tuple[object, str]]:
    """``[(id, folded_repo)]`` for the outcome rows that move, or raise naming the clash.

    Pure, and separated from :func:`upgrade` so it can be exercised against the row
    shapes that produce a conflict rather than only against the empty table a fresh
    database gives it — `0022`'s reason, and the branch that does nothing is the
    one a migration test otherwise covers twice.

    ``rows`` is ``(id, repo, pr, finding_key, outcome)``.
    """
    groups: dict[tuple[str, int, str], list[tuple[object, str, str]]] = {}
    for rid, repo, pr, key, outcome in rows:
        groups.setdefault((repo.strip().lower(), pr, key), []).append((rid, repo, outcome))

    clashes = sorted(
        f"  {canon}#{pr} {key!r} — "
        + ", ".join(f"{repo!r} says {outcome!r}" for _rid, repo, outcome in group)
        for (canon, pr, key), group in groups.items() if len(group) > 1
    )
    if clashes:
        raise RuntimeError(
            "folding the repo would put two outcomes on one defect:\n"
            + "\n".join(clashes)
            + "\n\nThese are two answers to a question that has one, and which of "
              "them stands is not a migration's to decide. Delete the row that is "
              "not the answer and re-run, e.g.\n"
              "  DELETE FROM review_finding_outcomes WHERE id = <the stale id>;\n"
              "Asked once: nothing folds a repo spelling at read time after this."
        )

    return [(rid, canon) for (canon, _pr, _key), group in groups.items()
            for rid, repo, _outcome in group if repo != canon]


def upgrade() -> None:
    bind = op.get_bind()

    # Read whole rather than filtered to the rows that move: a row already in the
    # canonical spelling is exactly what a folding row can collide WITH, so a
    # conflict check that could not see it would be the check not happening. The
    # table is one row per defect a person has recorded an answer about — 40 on the
    # board this was written against — and this runs once.
    rows = bind.execute(sa.text(
        "SELECT id, repo, pr, finding_key, outcome FROM review_finding_outcomes"
    )).fetchall()
    for rid, folded in plan_folds([(r.id, r.repo, r.pr, r.finding_key, r.outcome)
                                   for r in rows]):
        bind.execute(
            sa.text("UPDATE review_finding_outcomes SET repo = :r WHERE id = :i"),
            {"r": folded, "i": rid},
        )

    # No unique constraint touches `review_runs.repo`, so this needs no plan: the
    # rows fold in place and nothing can collide.
    bind.execute(sa.text(
        f"UPDATE review_runs SET repo = {_CANON} WHERE repo <> {_CANON}"))

    # After the folds above every row satisfies this, so the constraint validates
    # rather than aborting — and `lower(btrim(...))` is idempotent, so the fold
    # and its assertion cannot disagree about a row either now or later.
    for table, name in _TABLES:
        op.create_check_constraint(name, table, f"repo = {_CANON}")


def downgrade() -> None:
    """Drop the constraints. The fold itself does not come back, and cannot.

    Which capitals a row was written with is not information the board kept
    anywhere else, so there is nothing to restore it from — and restoring it would
    mean putting rows back under spellings the endpoints now refuse to write and
    the queries no longer look for. The rows are all still here; only the case of
    a name GitHub itself treats as case-insensitive changed.
    """
    for table, name in reversed(_TABLES):
        op.drop_constraint(name, table, type_="check")
