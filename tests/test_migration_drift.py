"""Does the whole migration chain build the schema the models declare? (#344)

Everything else in this suite checks a *step*. ``test_migration_reconcile.py``
checks the graph is a graph; ``test_migration_0025.py`` and
``test_migration_0031.py`` each check one migration's own before and after. This
module is the only one that asks about the **end state**: build a database from
nothing by replaying every revision in order, then diff the result against
``app.models``. Anything the two disagree about is drift, and drift is the class
of defect that surfaces on a machine nobody was looking at, weeks later, as
``column does not exist``.

Three things it catches that nothing else here does:

* a model changed with no migration written for it;
* a migration that builds something other than what the models declare;
* **the version table lying about the real schema.** ``alembic stamp`` records a
  revision without executing its SQL, so a database stamped past a failure
  asserts a schema that was never built. Hence the rule: **never stamp, always
  upgrade.** A dev or test database here is a disposable artefact, rebuilt by
  running the migrations forward. If one fails, fix the migration.

And one this repo has a particular need for: **the chain runs under two naming
schemes and the replay has to walk both** (#341). ``0001`` … ``0034`` are
hand-numbered; everything above them carries an opaque ``m<8 hex>`` id. That split
exists because renumbering rewrites revision *identity* — the id is what
``alembic_version`` stores, so renaming one makes every database holding it name a
revision the repository no longer has. Three worktree databases were dropped and
rebuilt for exactly that on 2026-08-22, and the symptom was a wall of errors from
an unrelated migration's downgrade. The numbered chain was therefore left alone
rather than converted, and this replay is what keeps the resulting mixed graph
honest: it is checked once, in CI, rather than once per developer.

**Its other half is ``test_migrations_self_contained.py``.** A migration that
imports live application code emits SQL for whatever columns the models have
*today*, which is fine on any database already past that revision and fatal on a
fresh replay — so this test is where that particular mistake detonates, and that
one is what stops it being made. Detect and prevent.

**A throwaway database, created and dropped here**, for the reason
``test_migration_0025.py`` gives: the suite's own database is at ``head`` with
every other test's rows in it, and a replay from empty needs an empty database.

The other rule the guards exist to state: **more than one row in
``alembic_version`` is a multi-head state**, and its fix is a merge migration,
never a stamp. ``alembic upgrade head`` refuses on an ambiguous head before this
module can see it, so the single-row assertion below is a cheap, permanent
restatement rather than the enforcement.

Run just this module (it needs the compose Postgres up)::

    .venv/bin/pytest -q tests/test_migration_drift.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base

from . import dbrun

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Suffixed onto the suite's own database name, so the throwaway is unmistakably
#: this module's and lands on the same server the run was already pointed at.
SCRATCH_SUFFIX = "_drift"

#: A floor, not a count. It exists so `test_the_comparison_is_not_vacuous` cannot
#: itself pass vacuously — there are 20 mapped tables today and this never needs
#: raising as that grows.
MIN_EXPECTED_TABLES = 10


def _alembic(url: str, *args: str) -> None:
    """Run alembic against `url`, from the repo root so alembic.ini is found.

    An explicit DATABASE_URL wins over the checkout's .env in `app.config`,
    which is the same lever `tests/conftest.py` and CI both pull.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT, env={**os.environ, "DATABASE_URL": url},
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"alembic {' '.join(args)} failed against a FRESH database — the chain "
            f"does not replay from empty, whatever it does on a database that is "
            f"already past the failing revision:\n{proc.stdout}\n{proc.stderr}")


@pytest.fixture(scope="module")
async def replayed(_db_claim):
    """A database built from nothing by every migration in order.

    Module-scoped: the replay is the expensive part and the tests below only
    read what it left, so they can neither pay for it twice nor depend on each
    other's ordering.
    """
    base = make_url(os.environ["DATABASE_URL"])
    db = f"{base.database}{SCRATCH_SUFFIX}"
    sa_url = base.set(database=db).render_as_string(hide_password=False)
    dsn = base.set(drivername="postgresql", database=db).render_as_string(
        hide_password=False)
    # The maintenance database, not the bound one: this connection creates and
    # drops the scratch database below, and since #366 the bound database is
    # this run's own — which a run that collects only these modules never
    # builds, because nothing here asks for the schema fixture.
    admin_dsn = dbrun.admin_dsn(os.environ["DATABASE_URL"])

    await _recreate(admin_dsn, db)
    try:
        _alembic(sa_url, "upgrade", "head")
        yield sa_url, dsn
    finally:
        await _drop(admin_dsn, db)


async def _recreate(admin_dsn: str, db: str) -> None:
    admin = await asyncpg.connect(admin_dsn)
    try:
        # FORCE, because a half-finished earlier run may still hold a session on
        # it and a plain DROP would fail on that rather than on anything real.
        await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{db}"')
    finally:
        await admin.close()


async def _drop(admin_dsn: str, db: str) -> None:
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
    finally:
        await admin.close()


async def _diffs(sa_url: str) -> list:
    """What alembic's autogenerate would still have to do to reach the models.

    `compare_metadata` wants a synchronous connection and this application has
    only an async engine, so it runs through `run_sync` — the same bridge
    `migrations/env.py` uses to run the migrations themselves.
    """
    engine = create_async_engine(sa_url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(
                lambda sync_conn: compare_metadata(
                    MigrationContext.configure(sync_conn), Base.metadata))
    finally:
        await engine.dispose()


def _render(diffs: list) -> str:
    lines = "\n".join(f"  {diff}" for diff in diffs)
    return (
        f"the migration chain and app/models disagree about {len(diffs)} thing(s):\n"
        f"{lines}\n\n"
        "Each line is an operation alembic would still have to perform on a "
        "fully migrated database to reach the models, so it reads as "
        "(what-to-do, what-it-found, what-the-models-want). Either a model "
        "changed without a migration, or a migration builds something other "
        "than what it should. Write the migration — do NOT stamp past this."
    )


async def test_a_fresh_replay_of_every_migration_matches_the_models(replayed):
    sa_url, _dsn = replayed
    diffs = await _diffs(sa_url)
    assert diffs == [], _render(diffs)


async def test_the_comparison_is_not_vacuous(replayed):
    """The test above passing has to mean something was compared.

    Two ways it could report no drift while checking nothing: model metadata
    that is empty (an import that stopped pulling the model modules in — the
    package's `__init__` is what registers them on `Base`), or a replay that
    created no tables. Either would be caught above as well, since an empty side
    produces a diff per table in the other — this asks directly, so a failure
    says which.
    """
    _sa_url, dsn = replayed
    declared = set(Base.metadata.tables)
    assert len(declared) >= MIN_EXPECTED_TABLES, (
        f"app.models declares almost nothing: {sorted(declared)}")

    conn = await asyncpg.connect(dsn)
    try:
        built = {
            row["tablename"]
            for row in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
    finally:
        await conn.close()
    assert declared <= built, f"the replay built no table for: {sorted(declared - built)}"


async def test_a_fresh_replay_leaves_exactly_one_head(replayed):
    """One row in `alembic_version`, and it is the chain's own head.

    More than one row is a multi-head state, whose fix is a merge migration and
    never a stamp. `upgrade head` refuses outright on an ambiguous head, so the
    count is belt to that braces — but the second assertion is not: it is the
    one that says the version table records what actually ran, which is the half
    a stamp breaks.

    The expected head comes from alembic's own script directory rather than from
    sorting filenames. The legacy chain puts the revision id in the name, which
    makes "highest file wins" tempting and wrong — it would not notice a revision
    on disk but unreachable from the chain, and it went red the moment a migration
    was named anything else, which since #341 every new one is.
    """
    _sa_url, dsn = replayed
    conn = await asyncpg.connect(dsn)
    try:
        stamped = [row["version_num"] for row in await conn.fetch(
            "SELECT version_num FROM alembic_version")]
    finally:
        await conn.close()
    assert len(stamped) == 1, (
        f"alembic_version holds {len(stamped)} rows ({stamped}) after a fresh "
        "replay — that is a multi-head state. Merge the heads with a new "
        "migration; do not stamp.")

    expected = ScriptDirectory(str(REPO_ROOT / "migrations")).get_current_head()
    assert stamped[0] == expected, (
        f"the replay stopped at {stamped[0]}, but the chain's head is {expected}.")
