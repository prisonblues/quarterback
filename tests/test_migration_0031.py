"""What migration 0031 does with the two rows #323 was filed about.

The suite's schema fixture runs 0031 in both directions on every db-backed run,
but only over an EMPTY ``plan_items`` — the one shape a data migration cannot be
wrong about. This module gives it the live board's rows, item ids and all.

**These are the acceptance test.** #323 says the two ``65lowther`` rows are to be
migrated onto whatever gets built, "not deleted", and not hand-edited into place
either. So they are seeded exactly as they sit on the live plan — same ids, same
titles, same ``ref: None``, same author, same scope — the migration runs, and the
questions asked afterwards are the ones the issue asks: can the board address them
by their own scope, and are they still the rows they were.

The other half is the refusal. Every legacy scope that is not a repo is either a
name somebody meant or something nobody can now interpret, and there is no rule
that separates the two — inventing one is the parser PR #152 was closed for. So
the migration handles the shape the new namespace holds and raises on anything
else, and both of those are pinned below.

**A throwaway database, created and dropped here**, for ``test_migration_0025``'s
reason: the migration is the thing under test, so it has to run for real at 0030
over data, and the suite's own database is at ``head`` with every other test's
rows in it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from . import dbrun

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The two rows, copied from the live plan as #323 quotes them. The ids are the
#: issue's own, so a failure here names the rows the issue names.
LIVE = [
    ("d3b9660a-f17f-4cd6-9887-3a9644c63134", "65lowther", 12, "zeus/crimson-umber",
     "D-007 has measured answers now: no hinged wardrobe fits, and the bathroom "
     "takes no shower tray"),
    ("2648089e-f869-4178-824e-d4b0518fd771", "65lowther", 14, "zeus/crimson-umber",
     "Move the ASHP off the living room window — Rich's D-008 call"),
]

#: Beside them: a repo scope, which must come through untouched, and a fleet row,
#: which has named no repo since the column existed and is not a legacy anything.
UNTOUCHED = [
    (str(uuid.uuid4()), "prisonblues/quarterback", 1, "zeus/one", "a repo item"),
    (str(uuid.uuid4()), None, 1, "zeus/two", "a fleet item"),
]

_INSERT = """
    INSERT INTO plan_items (id, repo, title, rank, state, added_by,
                            created_at, updated_at)
    VALUES ($1, $2, $3, $4, 'open', $5,
            now() - interval '5 days', now() - interval '5 days')
"""

_INSERT_PLAN = """
    INSERT INTO plans (id, repo, label, added_by, created_at, updated_at)
    VALUES (gen_random_uuid(), $1, $2, $3,
            now() - interval '5 days', now() - interval '5 days')
"""


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
    """A fresh database at 0030, and the two URLs for reaching it."""
    base = make_url(os.environ["DATABASE_URL"])
    db = f"{base.database}_{name}"
    sa_url = base.set(database=db).render_as_string(hide_password=False)
    dsn = base.set(drivername="postgresql", database=db).render_as_string(
        hide_password=False)
    # The maintenance database, not the bound one: this connection creates and
    # drops the scratch database below, and since #366 the bound database is
    # this run's own — which a run that collects only these modules never
    # builds, because nothing here asks for the schema fixture.
    admin_dsn = dbrun.admin_dsn(os.environ["DATABASE_URL"])
    admin = await asyncpg.connect(admin_dsn)
    try:
        # FORCE, because a half-finished earlier run may still hold a session on it
        # and a plain DROP would fail on that rather than on anything real.
        await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{db}"')
    finally:
        await admin.close()
    _must(sa_url, "upgrade", "0030")
    return sa_url, dsn, admin_dsn, db


async def _drop(admin_dsn: str, db: str) -> None:
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
    finally:
        await admin.close()


@pytest.fixture(scope="module")
async def snapshots(_db_claim):
    """Seed the live rows at 0030, upgrade, snapshot, downgrade, snapshot again."""
    sa_url, dsn, admin_dsn, db = await _scratch("m0031")
    try:
        conn = await asyncpg.connect(dsn)
        try:
            for item_id, repo, rank, added_by, title in [*LIVE, *UNTOUCHED]:
                await conn.execute(_INSERT, uuid.UUID(item_id), repo, title, rank,
                                   added_by)
            # A plan row in the same legacy scope: `plans.repo` carries the same
            # column with the same meaning, and a scope renamed in one table and
            # not the other is a plan whose items are in a list it is not in.
            await conn.execute(_INSERT_PLAN, "65lowther", "the works",
                               "zeus/crimson-umber")
        finally:
            await conn.close()

        _must(sa_url, "upgrade", "head")
        conn = await asyncpg.connect(dsn)
        try:
            up = {
                "items": [dict(r) for r in await conn.fetch(
                    "SELECT id, repo, rank, state, title, added_by FROM plan_items "
                    "ORDER BY COALESCE(repo, ''), rank")],
                "scopes": [dict(r) for r in await conn.fetch(
                    "SELECT name, note, added_by FROM plan_scopes ORDER BY name")],
                "plans": [dict(r) for r in await conn.fetch(
                    "SELECT repo, label FROM plans ORDER BY COALESCE(repo, '')")],
            }
        finally:
            await conn.close()

        _must(sa_url, "downgrade", "0030")
        conn = await asyncpg.connect(dsn)
        try:
            down = {
                "items": [dict(r) for r in await conn.fetch(
                    "SELECT id, repo, rank, title FROM plan_items "
                    "ORDER BY COALESCE(repo, ''), rank")],
                "scopes_table": await conn.fetchval(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name = 'plan_scopes'"),
            }
        finally:
            await conn.close()
        yield {"up": up, "down": down}
    finally:
        await _drop(admin_dsn, db)


# ---- the acceptance test -----------------------------------------------------


def test_both_live_rows_land_in_the_project_scope(snapshots):
    """#323's acceptance, by item id: the two rows are in a scope, not deleted."""
    by_id = {str(r["id"]): r for r in snapshots["up"]["items"]}
    for item_id, _, _, _, _ in LIVE:
        assert item_id in by_id, "a migrated row is not a deleted one"
        assert by_id[item_id]["repo"] == "project:65lowther"


def test_the_rows_are_otherwise_exactly_the_rows_they_were(snapshots):
    """Only the scope moves. A migration that also renumbered, retitled or
    reauthored them would have destroyed the record it was asked to preserve."""
    by_id = {str(r["id"]): r for r in snapshots["up"]["items"]}
    for item_id, _, rank, added_by, title in LIVE:
        row = by_id[item_id]
        assert (row["rank"], row["added_by"], row["title"], row["state"]) == \
            (rank, added_by, title, "open")


def test_the_scope_is_declared_so_the_rows_are_addressable(snapshots):
    """Moving the rows is half of it. Without the registry row the new scope is
    undeclared, and `_norm_scope` refuses an undeclared one — which would leave
    them stranded exactly as before, under a longer name."""
    assert [s["name"] for s in snapshots["up"]["scopes"]] == ["project:65lowther"]


def test_the_scope_records_who_first_put_work_in_it(snapshots):
    """`added_by` means "somebody decided this". The truest available answer is the
    identity that created the earliest row in the scope — better than a synthetic
    "migration" in a column about a decision."""
    assert snapshots["up"]["scopes"][0]["added_by"] == "zeus/crimson-umber"
    assert "0031" in snapshots["up"]["scopes"][0]["note"]


def test_the_plan_row_in_that_scope_moves_with_its_items(snapshots):
    """`plans.repo` is the same column with the same meaning. Renaming one table
    and not the other would put a plan's items in a list the plan is not in."""
    assert ("project:65lowther", "the works") in \
        {(p["repo"], p["label"]) for p in snapshots["up"]["plans"]}


def test_a_repo_scope_and_the_fleet_are_left_alone(snapshots):
    """The rewrite is for scopes that fail `REPO_RE`. A valid repo is not one, and
    NULL is the fleet — which has named no repository since the column existed and
    is not a legacy anything."""
    scopes = {r["repo"] for r in snapshots["up"]["items"]}
    assert "prisonblues/quarterback" in scopes and None in scopes


# ---- downgrade ---------------------------------------------------------------


def test_the_downgrade_leaves_the_database_as_it_found_it(snapshots):
    """Not merely "reversible": leaving `project:65lowther` behind would be WORSE
    than the state being reverted to, because the pre-0031 code refuses a colon —
    so those rows would be unreadable by every scope including their own."""
    by_id = {str(r["id"]): r for r in snapshots["down"]["items"]}
    for item_id, repo, _, _, _ in LIVE:
        assert by_id[item_id]["repo"] == repo
    assert snapshots["down"]["scopes_table"] == 0


# ---- what it refuses ---------------------------------------------------------


@pytest.mark.parametrize("scope,because", [
    ("https://github.com/acme/widget", "a half-typed URL is not a name anybody meant"),
    ("acme/widget.git", "a clone suffix is the parser #152 deleted, one step in"),
    ("../etc/passwd", "a path is not a scope"),
    ("a" * 80, "past what a scope name may carry"),
])
async def test_a_legacy_scope_it_cannot_resolve_stops_the_migration(scope, because, _db_claim):
    """It raises rather than minting `project:<whatever that was>`.

    There is no rule separating "a name somebody meant" from "something that went
    wrong", and three review rounds on a parser that tried produced three more
    holes (#152). A migration that fails loudly is the cheap kind; the alternative
    puts an uninterpretable string in the namespace forever, under a prefix that
    asserts a person chose it."""
    sa_url, dsn, admin_dsn, db = await _scratch("m0031_bad")
    try:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(_INSERT, uuid.uuid4(), scope, "a row", 1, "zeus/x")
        finally:
            await conn.close()
        proc = _alembic(sa_url, "upgrade", "head")
        assert proc.returncode != 0, f"{because}: {proc.stdout}"
        assert "cannot resolve" in proc.stderr + proc.stdout
        assert scope[:40] in proc.stderr + proc.stdout, \
            "the refusal has to name the row, or nobody can go and fix it"
    finally:
        await _drop(admin_dsn, db)


async def test_two_scopes_that_would_fold_into_one_stop_the_migration(_db_claim):
    """`65lowther` and `65Lowther` are one scope in the new namespace and were two
    lists in the old one, each with its own 1..n ranks. Merging them would
    interleave two orders nobody has ever compared — the failure `_scope_items`
    names — so it is refused with both spellings on the message instead."""
    sa_url, dsn, admin_dsn, db = await _scratch("m0031_fold")
    try:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(_INSERT, uuid.uuid4(), "65lowther", "lower", 1, "z/a")
            await conn.execute(_INSERT, uuid.uuid4(), "65Lowther", "upper", 1, "z/b")
        finally:
            await conn.close()
        proc = _alembic(sa_url, "upgrade", "head")
        assert proc.returncode != 0, proc.stdout
        out = proc.stderr + proc.stdout
        assert "would fold" in out and "65Lowther" in out and "65lowther" in out
    finally:
        await _drop(admin_dsn, db)
