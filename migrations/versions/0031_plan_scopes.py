"""vNEXT: a plan scope is not always a GitHub repo (#323)

Two rows sat on the live plan under scope `65lowther` — house renovation work,
deliberately planned, with no GitHub anything — and the board could not read them
by their own scope. `plan_read(repo='65lowther')` answered 422 with `REPO_SHAPE`;
the write that created them had been accepted before `app/api/plan.py` began
validating the scope, so they were addressable by nothing but an unfiltered read
of every scope at once. `qb-reconcile`, newly on a fifteen-minute timer, reported
them as a check it could not make, forever.

`app/scope.py` gives the plan a second, disjoint scope namespace behind an
explicit `project:` sigil, and this migration does the two things a schema has to
do for it:

1. **`plan_scopes`** — the registry that makes a project scope *exist*. A repo
   scope needs no row: `owner/name` is checkable against a rule that a typo fails.
   `project:65lowthr` passes every rule there is, so only a person saying so can
   distinguish it from `project:65lowther`, and the endpoint that writes this
   table is behind `app.auth.human` for that reason.

2. **The stranded rows**, moved onto it. `65lowther` becomes
   `project:65lowther` in `plan_items` and `plans`, and gains its registry row.

## Why it refuses rather than guesses

Every legacy scope that fails `REPO_RE` is either a name a person meant (fold it
into the new namespace) or something nobody can now interpret — a half-typed URL,
a path, a stray `.git`. There is no rule that separates the second from the first,
and inventing one is the parser PR #152 was closed for. So this migration handles
exactly the shape the new namespace can hold and **raises** on anything else,
naming the rows. A migration that fails loudly is the cheap kind; one that
silently mints `project:https://github.com/x/y` is not.

The same refusal covers the one collision this can create: two legacy scopes that
differ only in case fold to one project scope, and merging two scopes' rank
sequences would silently interleave two orders no human ever compared — the defect
`_scope_items` calls out by name. Refused, with both spellings named.

## Why the rewrite is safe to do in place

Nothing can already hold a `project:` scope. Before this revision every scope
reaching `plan_items.repo` or `plans.repo` went through `canonical_repo`, which
refuses a colon outright — so the target namespace is provably empty and the
rename cannot collide with an existing scope's ranks, labels or refs. The two
unique indexes involved (`ix_plan_items_open_ref`, `ix_plans_open_label`) both key
on `COALESCE(repo, '')`, and renaming a whole scope at once moves every member of
each key group together, so neither can be violated by this.

Revision ID: 0031
Revises: 0030
"""

import re

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels = None
depends_on = None

#: The scope tables this touches. Both carry the same nullable `repo` column with
#: the same meaning, and a scope renamed in one and not the other is a plan whose
#: items are in a list the plan itself is not in.
_SCOPED = ("plan_items", "plans")

#: `app.claimkey.REPO_RE` **as of this revision**, pinned rather than imported —
#: the same discipline `0022_canonical_release_repo.py` follows and for the same
#: reason: a migration describes the database at one moment in history, and one
#: that imports a live constant silently re-runs against a rule that has since
#: moved.
_REPO_RE = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z(?<!\.git)"
)

#: `app.scope.PROJECT_SIGIL` and `PROJECT_NAME_RE`, pinned for the same reason.
_SIGIL = "project:"
_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")


def _legacy_scopes(conn) -> list[str]:
    """Every distinct non-NULL scope in the plan that is not a valid repo name."""
    seen: set[str] = set()
    for table in _SCOPED:
        for (repo,) in conn.execute(
            sa.text(f"SELECT DISTINCT repo FROM {table} WHERE repo IS NOT NULL")
        ):
            if not _REPO_RE.match(repo):
                seen.add(repo)
    return sorted(seen)


def _resolve(legacy: list[str]) -> dict[str, str]:
    """Old scope -> new scope, or raise naming what could not be resolved."""
    plan: dict[str, str] = {}
    unresolvable = [s for s in legacy if not _NAME_RE.match(s.strip().lower())]
    if unresolvable:
        raise RuntimeError(
            "0031 cannot resolve these plan scopes onto the project namespace: "
            + ", ".join(repr(s) for s in unresolvable)
            + ". They are neither a valid `owner/name` repo nor a name a project "
              "scope can carry (letters, digits, `.`, `-`, `_`, up to 64). Fix or "
              "remove those rows and run this again — guessing at what they meant "
              "is the parser PR #152 was closed for."
        )
    for scope in legacy:
        target = _SIGIL + scope.strip().lower()
        clash = next((s for s, t in plan.items() if t == target), None)
        if clash is not None:
            raise RuntimeError(
                f"0031 would fold plan scopes {clash!r} and {scope!r} into one "
                f"{target!r}. Each carries its own 1..n rank sequence, and merging "
                "them would interleave two orders nobody has ever compared. Rename "
                "one of them and run this again."
            )
        plan[scope] = target
    return plan


def upgrade() -> None:
    op.create_table(
        "plan_scopes",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("added_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_plan_scopes_name"),
        sa.CheckConstraint(f"name LIKE '{_SIGIL}%'", name="ck_plan_scopes_sigil"),
        sa.CheckConstraint("name = lower(name)", name="ck_plan_scopes_lower"),
        sa.CheckConstraint(f"length(name) > {len(_SIGIL)}", name="ck_plan_scopes_name"),
    )

    conn = op.get_bind()
    moves = _resolve(_legacy_scopes(conn))
    for old, new in moves.items():
        # WHO declared it: the person or agent who first put work in that scope,
        # which is the truest answer available and better than a synthetic
        # "migration" identity in a column that means "somebody decided this".
        # Ordered by the earliest row across both tables.
        author = conn.execute(sa.text("""
            SELECT added_by FROM (
                SELECT added_by, created_at FROM plan_items WHERE repo = :old
                UNION ALL
                SELECT added_by, created_at FROM plans WHERE repo = :old
            ) AS rows ORDER BY created_at, added_by LIMIT 1
        """), {"old": old}).scalar()
        conn.execute(sa.text("""
            INSERT INTO plan_scopes (id, name, note, added_by)
            VALUES (gen_random_uuid(), :new, :note, :by)
            ON CONFLICT (name) DO NOTHING
        """), {"new": new, "by": author or "migration 0031",
               "note": f"carried over from the plan scope {old!r} by migration 0031 "
                       "(#323): work with no repo behind it"})
        for table in _SCOPED:
            conn.execute(
                sa.text(f"UPDATE {table} SET repo = :new WHERE repo = :old"),
                {"new": new, "old": old})


def downgrade() -> None:
    # Strip the sigil back off, so a downgraded database reads exactly as it did:
    # a scope that was `65lowther` before is `65lowther` again, stranded in the
    # same way and for the same reason. Leaving `project:65lowther` behind would
    # be worse than the state being reverted to — the old code refuses a colon,
    # so those rows would be unreadable by every scope including their own.
    conn = op.get_bind()
    # The offset is `len(_SIGIL) + 1` written out, not bound: it is a constant of
    # this revision rather than input, and a bound integer here is one asyncpg
    # cannot type against `substring(text from int)` — which fails the downgrade
    # the schema fixture runs on every db-backed test.
    cut = len(_SIGIL) + 1
    for table in _SCOPED:
        conn.execute(
            sa.text(f"UPDATE {table} SET repo = substring(repo from {cut}) "
                    "WHERE repo LIKE :like"),
            {"like": _SIGIL + "%"})
    op.drop_table("plan_scopes")
