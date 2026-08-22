"""What migration 0033 does to a board holding one repository under two spellings.

The suite's schema fixture runs 0033 in both directions on every db-backed run,
but only over EMPTY review tables — the one shape a data migration cannot be
wrong about. This module gives it the shape #326 is filed about: a run recorded
as ``PrisonBlues/Quarterback`` beside one recorded as ``prisonblues/quarterback``,
which is what two checkouts with two remotes produce.

Two halves, and they fail differently:

* **The fold** is the repair, and its acceptance is that after it there is one
  spelling, so ``review_runs.repo = review_finding_outcomes.repo`` — a plain
  column-to-column join that ``/review/stats`` makes — matches again.
* **The CHECK constraint** is what closes the class rather than fixing the
  endpoint a third time (#67). Folding the rows repairs the board as it stands;
  the constraint is what stops the next write path putting a second spelling back,
  and it is asserted by writing one *around* the API, because a validator is only
  a promise about the callers you know about.

And the refusal, which is the part no live board exercises: folding can put two
outcome rows on one ``(repo, pr, finding_key)``, and those are two answers to a
question that has one. :func:`~migrations.versions.plan_folds` raises instead of
picking, for the reason ``0022`` and ``0031`` raise.

**A throwaway database, created and dropped here**, for ``test_migration_0031``'s
reason: the migration is the thing under test, so it has to run for real at 0032
over data, and the suite's own database is at ``head`` with every other test's
rows in it.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parent.parent

#: One repository, spelt the two ways two checkouts spell it, plus one whose case
#: is already canonical and must come through untouched.
RUNS = [
    ("zeus/one", "PrisonBlues/Quarterback", 326),
    ("zeus/two", "prisonblues/quarterback", 327),
    ("zeus/three", "Acme/Widget", 12),
    ("zeus/four", "acme/other", 1),
    # A repository whose owner starts with the letter the CHECK's whitespace class
    # must not contain. `E'…\\v'` has no meaning in Postgres, so the backslash is
    # dropped and `btrim` gains a literal `v`: this row folds to `ercel/next` and
    # fails the constraint the same migration adds. Seeded rather than asserted in
    # the abstract, because that is the form the failure actually takes.
    ("zeus/five", "Vercel/Next", 5),
]

#: An outcome under the capitalised spelling. Its run is under the folded one, so
#: before the migration the join that decides "has this defect been answered?"
#: misses — and after it, it does not.
OUTCOMES = [
    ("PrisonBlues/Quarterback", 327, "F-1", "fixed", "zeus/two"),
    ("acme/other", 1, "F-2", "fixed", "zeus/four"),
]

_INSERT_RUN = """
    INSERT INTO review_runs (author, repo, pr, ts) VALUES ($1, $2, $3, now())
"""
_INSERT_OUTCOME = """
    INSERT INTO review_finding_outcomes (repo, pr, finding_key, outcome, set_by, ts)
    VALUES ($1, $2, $3, $4, $5, now())
"""


@pytest.fixture(scope="module")
def migration():
    """Revision 0033, imported by path — it is not on a package path."""
    path = REPO_ROOT / "migrations" / "versions" / "0033_canonical_review_repo.py"
    spec = importlib.util.spec_from_file_location("m0033", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- the planner, without a database -----------------------------------------


def test_a_capitalised_row_is_folded(migration):
    assert migration.plan_folds([(1, "Acme/Widget", 12, "F-1", "fixed")]) == \
        [(1, "acme/widget")]


def test_a_row_already_canonical_does_not_move(migration):
    """A no-op UPDATE is not harmless on a table with an `updated_at`: it is a
    revision nobody made, on a record whose whole point is that changes are
    counted."""
    assert migration.plan_folds([(1, "acme/widget", 12, "F-1", "fixed")]) == []


def test_surrounding_space_is_part_of_the_spelling_too(migration):
    """`canonical_repo` strips before it folds, so a value that reached the column
    with a trailing space is a third spelling of the same repo — and the one that
    produced "no finding with this key on this PR" for a whole batch."""
    assert migration.plan_folds([(1, " Acme/Widget ", 12, "F-1", "fixed")]) == \
        [(1, "acme/widget")]


def test_two_spellings_holding_two_outcomes_stops_the_migration(migration):
    """The refusal. These are two terminal answers to "what happened to this
    defect?", one of them says `fixed` and the other `refuted`, and which stands
    is not a migration's to decide — the table feeds a published precision
    figure and `revisions`/`prior_outcome` exist because a quietly changed
    outcome is not acceptable."""
    with pytest.raises(RuntimeError) as e:
        migration.plan_folds([
            (1, "Acme/Widget", 12, "F-1", "fixed"),
            (2, "acme/widget", 12, "F-1", "refuted"),
        ])
    assert "two outcomes on one defect" in str(e.value)
    # It has to name the rows, or the human it stops cannot act on it.
    assert "acme/widget#12" in str(e.value)
    assert "'fixed'" in str(e.value) and "'refuted'" in str(e.value)


def test_the_same_defect_in_two_repos_is_not_a_clash(migration):
    """The key is `(repo, pr, finding_key)`. Two repositories sharing a finding
    key is the ordinary case, and a planner that grouped on the key alone would
    refuse to run on any board that had one."""
    assert migration.plan_folds([
        (1, "Acme/Widget", 12, "F-1", "fixed"),
        (2, "acme/other", 12, "F-1", "refuted"),
    ]) == [(1, "acme/widget")]


def test_a_clean_board_is_a_noop(migration):
    assert migration.plan_folds([]) == []


def test_the_whitespace_class_does_not_eat_a_letter(migration):
    r"""Postgres's escape-string syntax has no `\v`: a backslash before it is
    dropped and the class silently gains the LETTER `v`, so
    `btrim('vercel/next', E' \t\n\r\f\v')` is `'ercel/next'` — and the CHECK
    this migration adds would refuse a repository for being named after its
    owner. `\013` is the octal spelling, which the models already use for the
    same reason. Asserted on the SQL text because the failure is invisible in
    every test whose repo happens not to start or end with a `v`."""
    assert "\\v" not in migration._CANON, (
        "`E'…\\v'` is the letter v to Postgres — use `\\013`")
    assert "\\013" in migration._CANON


# ---- the migration, over data ------------------------------------------------


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT, env={**os.environ, "DATABASE_URL": url},
        capture_output=True, text=True,
    )


def _must(url: str, *args: str) -> None:
    proc = _alembic(url, *args)
    if proc.returncode != 0:  # pragma: no cover - only when a migration breaks
        raise AssertionError(
            f"alembic {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}")


async def _scratch(name: str):
    """A fresh database at 0032, and the two URLs for reaching it."""
    base = make_url(os.environ["DATABASE_URL"])
    db = f"{base.database}_{name}"
    sa_url = base.set(database=db).render_as_string(hide_password=False)
    dsn = base.set(drivername="postgresql", database=db).render_as_string(
        hide_password=False)
    admin_dsn = base.set(drivername="postgresql").render_as_string(hide_password=False)
    admin = await asyncpg.connect(admin_dsn)
    try:
        # FORCE, because a half-finished earlier run may still hold a session on it
        # and a plain DROP would fail on that rather than on anything real.
        await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{db}"')
    finally:
        await admin.close()
    _must(sa_url, "upgrade", "0032")
    return sa_url, dsn, admin_dsn, db


async def _drop(admin_dsn: str, db: str) -> None:
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
    finally:
        await admin.close()


@pytest.fixture(scope="module")
async def migrated():
    """Seed two spellings at 0032, upgrade, then ask what the board holds."""
    sa_url, dsn, admin_dsn, db = await _scratch("m0033")
    try:
        conn = await asyncpg.connect(dsn)
        try:
            for author, repo, pr in RUNS:
                await conn.execute(_INSERT_RUN, author, repo, pr)
            for row in OUTCOMES:
                await conn.execute(_INSERT_OUTCOME, *row)
        finally:
            await conn.close()

        _must(sa_url, "upgrade", "head")
        conn = await asyncpg.connect(dsn)
        try:
            state = {
                "runs": [dict(r) for r in await conn.fetch(
                    "SELECT author, repo, pr FROM review_runs ORDER BY author")],
                "outcomes": [dict(r) for r in await conn.fetch(
                    "SELECT repo, pr, finding_key, outcome FROM "
                    "review_finding_outcomes ORDER BY finding_key")],
                # The join `/review/stats` makes, column to column, with no fold
                # anywhere in it. Before the migration this answered 0.
                "joined": await conn.fetchval(
                    "SELECT count(*) FROM review_finding_outcomes o "
                    "JOIN review_runs r ON o.repo = r.repo AND o.pr = r.pr"),
                "refused": None,
            }
            try:
                await conn.execute(_INSERT_RUN, "zeus/five", "Acme/Sneaky", 2)
            except asyncpg.exceptions.CheckViolationError as e:
                state["refused"] = e.constraint_name
        finally:
            await conn.close()

        _must(sa_url, "downgrade", "0032")
        conn = await asyncpg.connect(dsn)
        try:
            state["after_downgrade"] = {
                "rows": await conn.fetchval("SELECT count(*) FROM review_runs"),
                "checks": [r["conname"] for r in await conn.fetch(
                    "SELECT conname FROM pg_constraint WHERE conname LIKE "
                    "'%repo_canonical%'")],
            }
        finally:
            await conn.close()
        yield state
    finally:
        await _drop(admin_dsn, db)


def test_the_two_spellings_become_one(migrated):
    """#326's acceptance at the storage layer: after this there is one repository
    on the board, not two, and every read that compares the column with `==` —
    which is all of them — sees the whole of it."""
    assert {r["repo"] for r in migrated["runs"]} == {
        "prisonblues/quarterback", "acme/widget", "acme/other", "vercel/next"}


def test_nothing_but_the_case_of_the_name_changed(migrated):
    """A fold is not an edit. Same rows, same authors, same PR numbers — a
    migration that also renumbered or dropped one would have destroyed the record
    it was asked to repair."""
    assert [(r["author"], r["pr"]) for r in migrated["runs"]] == \
        [("zeus/five", 5), ("zeus/four", 1), ("zeus/one", 326),
         ("zeus/three", 12), ("zeus/two", 327)]


def test_an_outcome_recorded_under_the_other_spelling_rejoins_its_run(migrated):
    """The consequence, not just the column. `/review/stats` joins
    `review_finding_outcomes.repo = review_runs.repo` with no fold on either side,
    so a capitalised outcome silently left-joined to NULL and its defect counted
    as unanswered."""
    assert [(o["repo"], o["pr"]) for o in migrated["outcomes"]] == \
        [("prisonblues/quarterback", 327), ("acme/other", 1)]
    assert migrated["joined"] == 2


def test_the_database_itself_refuses_the_next_second_spelling(migrated):
    """What makes this the last time. An INSERT that never touches the API — an
    admin script, a backfill, the write path somebody adds next year — cannot
    reintroduce the spelling the endpoints stopped folding for."""
    assert migrated["refused"] == "ck_review_runs_repo_canonical"


def test_the_downgrade_lets_go_without_losing_rows(migrated):
    """Which capitals a row was written with is not recoverable and does not need
    to be: the downgrade drops the constraints and leaves the rows, because the
    only thing it changed was the case of a name GitHub itself folds."""
    assert migrated["after_downgrade"]["rows"] == len(RUNS)
    assert migrated["after_downgrade"]["checks"] == []
