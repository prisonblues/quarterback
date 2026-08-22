"""One repository, one spelling — the dial and worktree columns (#350)

#326 closed this class for the review tables and its audit named the columns it
had not reached. Two of the three were real, and they are these; the third
(`leases.repo`) is a bare label the lifecycle hook writes, not a repository, and
is deliberately left alone — `canonical_repo` would refuse every value in it.

**`dial_settings.repo` is the sharp one.** `app.api.dials._norm_repo` checked the
shape with a regex of its own and never lower-cased — the one repo validator on
this board that did one without the other, while `merge_queue` cites the hazard
for its own column three files away. `ix_dial_settings_live` is UNIQUE over
`COALESCE(repo, '')` and `dial` where `cleared_at IS NULL`, so `Acme/X` and
`acme/x` could each hold a **live row for the same dial**: two answers to a
settings question that has one, with `GET /dials?repo=acme/x` seeing whichever
it matched. `harness_rules.detect_github` reads the repo off `remote.origin.url`
and preserves its capitals, so which answer a review ran under depended on how
the remote happened to be spelled.

**`worktrees.repo` is the same defect plus a disagreement.** It was stored
verbatim and `GET /worktrees?repo=` compared it with `==`, while `/sync` folded
the same column through `app.sync.repo_key` (basename, lower-cased). One column,
two readers, two different ideas of what "the same repo" is.

## Fold on the write, not at each read

#349 settled this argument for the review tables and the deciding evidence was
that four of its twelve read paths — a `COUNT(DISTINCT repo)`, a distinct-defect
tuple, a column-to-column join and a Python dict key — cannot be reached by a
`func.lower()` in a WHERE clause at all. Neither of this revision's columns has a
read that exotic, but both have something a read-side fold cannot fix either:
`dial_settings` has a **unique index**, where two spellings are two rows and no
query can undo that, and `worktrees.repo` is compared by a second endpoint that
does not know about the first. A guarantee about the column is the only thing
both of them can be written against.

So both write paths fold through `app.claimkey.canonical_repo`, this revision
folds the rows written before it, and a CHECK constraint on each column holds it
there — which is what makes the class closed rather than the endpoints patched: a
write path added later fails loudly instead of inventing a second spelling.

## What the constraints do NOT say

Case and surrounding whitespace only, not `owner/name` shape. The shape is
refused at ingest, where a caller can be told why; a row written before that check
existed is legitimately here — `worktrees` on the live board holds bare names from
before the MCP tools derived the slug — and a constraint that rejected them would
make this revision unrunnable rather than make them canonical. Folding their case
is still right and still cheap, and a bare name folded to lower case is exactly
what `GET /worktrees?repo=<bare name>` now matches on.

`btrim` is given its character class because the one-argument form trims ordinary
spaces and nothing else, while `canonical_repo`'s `str.strip()` takes every
whitespace character — a tab-padded row would otherwise be one the constraint
called canonical and the validator did not, which is two rules disagreeing about
one column in the file written to stop that. **Vertical tab is `\\013`, never
`\\v`**: Postgres's escape-string syntax has no `\\v`, so the backslash is dropped
and the class gains the *letter* `v` — `btrim('vercel/next', E' \\t\\n\\r\\f\\v')` is
`'ercel/next'`, and the constraint would refuse a repository for being named after
its owner. `0033` and the models spell it `\\013` for exactly this reason.

## The bound this deliberately has, named because it is a choice

**The constraints are looser than `canonical_repo`, and have to be.** They fold
case and the six ASCII whitespace characters `btrim` can be given; `str.strip()`
takes every Unicode space besides, so a row padded with a no-break space would
satisfy the CHECK and not the validator. Tightening to "no whitespace anywhere"
would be the honest rule for what the API writes and would also refuse the legacy
rows above, which would make this revision abort rather than run — so the
constraint is a backstop against the plausible mistake (a write path that forgets
to fold) rather than an encoding of the whole ingest rule. Nothing can produce the
residual shape either: `REPO_RE` admits no whitespace at all, so a Unicode-padded
value cannot reach the column through any endpoint, and one inserted around them
was equally unreachable before this revision. `0033` states the same bound for the
same reason. The live dial rows are the one place this is tighter — they fold
through Python's `str.strip()` in :func:`plan_dial_folds`, which does take the lot.

## Why a dial clash is refused rather than resolved

Folding can put two live rows on one `(COALESCE(repo,''), dial)` — that is the
defect, seen from the migration's side. Which of them is in force is not a
migration's to decide: they are two values a person set, each with a reason and
an author, and picking the newer would move a policy floor silently on the
strength of a timestamp. So this stops and names the rows with the SQL to settle
them, the way `0022`, `0031` and `0033` stop. Clearing rather than deleting,
because history surviving the next person moving a dial is what the `cleared_at`
column is for.

`worktrees` needs no such plan: its primary key is `(device, path)` and nothing
unique touches `repo`, so its rows fold in place and cannot collide.

## And why a live dial in the OLD shape is refused too

The validator this replaces checked the shape with a regex of its own
(`^[\\w.-]+/[\\w.-]+$`) and admitted spellings `canonical_repo` refuses — `a_b/c`,
`a/b.git`, a name of any length. After this revision every dial surface refuses
them, read and set and clear alike, so such a row would be a setting **in force
that no caller can name, list or turn off**: worse than the second spelling this
revision exists to remove. So a LIVE one stops the migration and is named with the
SQL to clear it. Cleared rows are not checked — they are history, reachable by
nothing that matters, and rewriting one would edit the record of what somebody
actually set. `worktrees` is not checked for this at all: a legacy bare name there
is still reachable, because the read takes one, and the next `report_git` replaces
the row outright.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-22

"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The columns this folds and the CHECK each one gains.
_TABLES = (
    ("dial_settings", "ck_dial_settings_repo_canonical"),
    ("worktrees", "ck_worktrees_repo_canonical"),
)

#: The canonical form, as SQL — `app.claimkey.canonical_repo`'s strip-and-lower
#: and its assertion, spelled out rather than imported for the reason `0022`,
#: `0031` and `0033` give: a migration is a statement about the database at one
#: moment, and a rule it imports can move underneath it. See the docstring on the
#: character class and on `\013`.
_WHITESPACE = r"E' \t\n\r\f\013'"
_CANON = f"lower(btrim(repo, {_WHITESPACE}))"

#: Both columns are nullable — a fleet-scoped dial and a checkout with no GitHub
#: remote — and `repo <> lower(...)` is NULL, not false, for a NULL repo. Stated
#: rather than relied on: a fold that quietly skipped every NULL row because of
#: three-valued logic would look identical to one that considered them.
_HAS_REPO = "repo IS NOT NULL"

#: `app.claimkey.REPO_RE` **as of this revision**, pinned rather than imported —
#: `0031` gives the reason and it is this file's own: a migration is a statement
#: about the database at one moment, and a rule it imports can move underneath it.
#: Used only to spot a LIVE dial the endpoints will no longer be able to name; see
#: :func:`plan_dial_folds`.
_REPO_RE = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z(?<!\.git)"
)


def plan_dial_folds(
    rows: list[tuple[object, str | None, str, str, object]],
) -> list[tuple[object, str]]:
    """``[(id, folded_repo)]`` for the live dial rows that move, or raise naming the clash.

    Pure, and separated from :func:`upgrade` so it can be exercised against the row
    shapes that produce a conflict rather than only against the clean table a fresh
    database gives it — `0022`'s reason, and the branch that does nothing is the one
    a migration test otherwise covers twice.

    ``rows`` is ``(id, repo, dial, set_by, set_at)`` for rows with
    ``cleared_at IS NULL`` — the exact population ``ix_dial_settings_live`` indexes,
    because a cleared row is outside the index and cannot collide with anything.

    A NULL repo is the fleet scope and folds to itself; it is carried through the
    grouping rather than filtered out, because the index keys it as ``''`` and a
    fleet dial is as capable of being doubled as a repo one is — it just cannot be
    doubled by a *spelling*, which is what makes it a useful thing to assert.
    """
    groups: dict[tuple[str, str], list[tuple[object, str | None, str, object]]] = {}
    for rid, repo, dial, set_by, set_at in rows:
        canon = repo.strip().lower() if repo is not None else None
        groups.setdefault((canon or "", dial), []).append((rid, repo, set_by, set_at))

    problems: list[str] = []

    clashes = sorted(
        f"  {scope or '(fleet)'} {dial!r} — "
        + ", ".join(f"{repo!r} set by {set_by!r} at {set_at} (id {rid})"
                    for rid, repo, set_by, set_at in sorted(group, key=lambda g: str(g[3])))
        for (scope, dial), group in groups.items() if len(group) > 1
    )
    if clashes:
        problems.append(
            "folding the repo would put two live rows on one dial:\n"
            + "\n".join(clashes)
            + "\n\nThese are two values in force for one setting, and which of them "
              "stands is not a migration's to decide — picking the newer would move "
              "a policy floor on the strength of a timestamp. Clear the one that is "
              "not in force and re-run, e.g.\n"
              "  UPDATE dial_settings SET cleared_at = now(), cleared_by = 'migration "
              "0034' WHERE id = '<the stale id above>';\n"
              "Cleared rather than deleted: the history of a dial's moves is what "
              "the column is for."
        )

    stranded = sorted(
        f"  {repo!r} {dial!r} set by {set_by!r} (id {rid})"
        for (scope, dial), group in groups.items() if scope and not _REPO_RE.match(scope)
        for rid, repo, set_by, _at in group
    )
    if stranded:
        problems.append(
            "these live dials are scoped to something that is not `owner/name`:\n"
            + "\n".join(stranded)
            + "\n\nThe old validator checked the shape with a regex of its own and "
              "admitted spellings `canonical_repo` refuses, and after this revision "
              "every dial surface — read, set and clear — refuses them too. A row "
              "left here would be a setting IN FORCE that no caller can name, list "
              "or turn off, which is worse than the second spelling this revision "
              "is about. Clear it and set it again under the repo's real spelling:\n"
              "  UPDATE dial_settings SET cleared_at = now(), cleared_by = 'migration "
              "0034' WHERE id = '<the id above>';\n"
              "Only LIVE rows are checked. A cleared one is history, it is reachable "
              "by nothing that matters, and rewriting it would be an edit to the "
              "record of what somebody actually set. `worktrees` is not checked at "
              "all for this: a legacy bare name there is still reachable — the read "
              "takes one — and the next `report_git` replaces the row outright."
        )

    if problems:
        raise RuntimeError(
            "\n\n".join(problems)
            + "\n\nAsked once: nothing folds a dial's repo at read time after this."
        )

    return [(rid, scope) for (scope, _dial), group in groups.items()
            for rid, repo, _by, _at in group
            if repo is not None and repo != scope]


def upgrade() -> None:
    bind = op.get_bind()

    # Read the whole live population rather than only the rows that move: a row
    # already in the canonical spelling is exactly what a folding row can collide
    # WITH, so a conflict check that could not see it would be the check not
    # happening.
    live = bind.execute(sa.text(
        "SELECT id, repo, dial, set_by, set_at FROM dial_settings WHERE cleared_at IS NULL"
    )).fetchall()
    for rid, folded in plan_dial_folds(
        [(r.id, r.repo, r.dial, r.set_by, r.set_at) for r in live]
    ):
        bind.execute(
            sa.text("UPDATE dial_settings SET repo = :r WHERE id = :i"),
            {"r": folded, "i": rid},
        )

    # Cleared rows sit outside `ix_dial_settings_live`, so they fold in bulk and
    # nothing can collide. They still fold: the constraint below is on the column,
    # not on the live subset, and a history row spelled two ways is a history row
    # a repo-scoped read of it would miss.
    bind.execute(sa.text(
        f"UPDATE dial_settings SET repo = {_CANON} "
        f"WHERE cleared_at IS NOT NULL AND {_HAS_REPO} AND repo <> {_CANON}"))

    # `worktrees` is keyed on (device, path) and nothing unique touches `repo`.
    bind.execute(sa.text(
        f"UPDATE worktrees SET repo = {_CANON} WHERE {_HAS_REPO} AND repo <> {_CANON}"))

    # After the folds above every row satisfies these, so they validate rather than
    # abort — and `lower(btrim(...))` is idempotent, so the fold and its assertion
    # cannot disagree about a row either now or later.
    for table, name in _TABLES:
        op.create_check_constraint(name, table, f"repo IS NULL OR repo = {_CANON}")


def downgrade() -> None:
    """Drop the constraints. The fold itself does not come back, and cannot.

    Which capitals a row was written with is not information the board kept
    anywhere else, so there is nothing to restore it from — and restoring it would
    mean putting rows back under spellings the endpoints now refuse to write and
    the queries no longer look for. Every row is still here; only the case of a
    name GitHub itself treats as case-insensitive changed.
    """
    for table, name in reversed(_TABLES):
        op.drop_constraint(name, table, type_="check")
