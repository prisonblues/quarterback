"""The first opaque revision id — the seam between two naming schemes (#341)

This migration changes no schema. It exists to be the first revision quarterback
did not number, and it is deliberately kept rather than folded into whatever
lands next.

## What it marks

Below it, `0001` … `0034`: a hand-numbered chain where the id *is* the chain
position. Above it, ids like this one: `m` and eight hex digits, minted at random
by `scripts/migration_reconcile.py new-id` and put on every generated revision by
`migrations/env.py`, so `alembic revision --autogenerate -m "..."` produces one
without anybody remembering a flag.

The reason for the change is that the next number is a value two branches can
both work out. On 2026-08-22 four of them worked out `0029`, all four preflight
runs truthfully said GO — each branch was single-headed against `main` on its
own — and the duplicate existed only in the union of branches none of which had
landed. There is no ref to read that catches that. An id nobody chooses from a
shared sequence cannot be minted twice, so two concurrent migrations stop being a
naming problem and become an ordinary two-head graph: detectable by the guards
that already exist, and resolvable by relinking one onto the other.

## Why nothing below it was renamed

A renumber rewrites `revision`, and `revision` is what `alembic_version` stores.
Every database holding a renamed id would then mean something other than what it
says — three worktree databases were dropped and rebuilt on 2026-08-22 for
exactly that. So the legacy chain keeps its numbers permanently, this repo runs a
mixed graph on purpose, and `tests/test_migration_ids.py` pins every existing id
against the rename that would break a running system.

## Why a no-op revision rather than nothing at all

The mixed chain has to exist somewhere a machine looks at it, not only in prose.
With this revision in place `tests/test_migration_drift.py` replays legacy numbers
and an opaque id in one pass on a fresh database, on every CI run, forever — which
is the only way the claim that the two coexist stays true.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "mdee05a89"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No schema change: this revision's whole content is its id."""


def downgrade() -> None:
    """Nothing to undo."""
