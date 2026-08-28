"""`derived` joins the rank-source vocabulary, and the way back out is lossy (#478).

The suite builds every run's schema with `alembic upgrade head`, so the UPGRADE
half of any migration is exercised constantly and the DOWNGRADE half is exercised
never — and here the downgrade is the half that makes a decision. It narrows the
CHECK constraint, so rows written while the newer code was deployed would violate
it and take the whole rollback with them unless something maps them first.

What that mapping chooses is the thing worth pinning. `ordered` is the tempting
target and the wrong one: it would silently promote an agent-applied sequence to a
human decision — the exact claim #478 exists to stop the board making — at the one
moment nothing afterwards records that it happened. `appended` understates instead,
and understating confidence in an order is the correct direction to be wrong in.

Run: uv run pytest tests/test_migration_derived.py
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

#: The revision under test, and the one below it.
THIS, PREVIOUS = "m3a9c41e7", "mfe8671ba"

_INSERT = """
INSERT INTO plan_items (id, repo, title, rank, rank_source, state, added_by)
VALUES ($1, $2, $3, $4, $5, 'open', 'laptop')
"""


def _alembic(url: str, *args: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT, env={**os.environ, "DATABASE_URL": url},
        capture_output=True, text=True)
    if proc.returncode != 0:  # pragma: no cover - only when a migration breaks
        raise AssertionError(
            f"alembic {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}")


async def _check(conn) -> str:
    return await conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_plan_items_rank_source'")


@pytest.fixture(scope="module")
async def roundtrip(_db_claim):
    """Upgrade, write a `derived` row, downgrade, upgrade again — snapshotted.

    One fixture for the whole chain because the chain is what costs, and because
    tests reading snapshots cannot depend on each other's ordering.
    """
    base = make_url(os.environ["DATABASE_URL"])
    scratch = f"{base.database}_mderived"
    sa_url = base.set(database=scratch).render_as_string(hide_password=False)
    dsn = base.set(drivername="postgresql", database=scratch).render_as_string(
        hide_password=False)

    admin = await asyncpg.connect(dbrun.admin_dsn(os.environ["DATABASE_URL"]))
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{scratch}"')
    finally:
        await admin.close()

    try:
        _alembic(sa_url, "upgrade", THIS)
        conn = await asyncpg.connect(dsn)
        try:
            up_check = await _check(conn)
            derived_id = uuid.uuid4()
            await conn.execute(_INSERT, derived_id, "acme/mig", "an agent applied this",
                               1, "derived")
            await conn.execute(_INSERT, uuid.uuid4(), "acme/mig", "a person typed this",
                               2, "ordered")
            accepted = True
        finally:
            await conn.close()

        _alembic(sa_url, "downgrade", PREVIOUS)
        conn = await asyncpg.connect(dsn)
        try:
            down_check = await _check(conn)
            after_down = {r["title"]: r["rank_source"] for r in await conn.fetch(
                "SELECT title, rank_source FROM plan_items ORDER BY rank")}
            ranks_down = {r["title"]: r["rank"] for r in await conn.fetch(
                "SELECT title, rank FROM plan_items")}
        finally:
            await conn.close()

        _alembic(sa_url, "upgrade", THIS)
        yield {"up_check": up_check, "accepted": accepted, "down_check": down_check,
               "after_down": after_down, "ranks_down": ranks_down}
    finally:
        admin = await asyncpg.connect(dbrun.admin_dsn(os.environ["DATABASE_URL"]))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
        finally:
            await admin.close()


def test_the_upgrade_admits_derived(roundtrip):
    assert "'derived'" in roundtrip["up_check"]
    assert roundtrip["accepted"], "a derived row was refused by the widened CHECK"


def test_the_downgrade_narrows_the_constraint_again(roundtrip):
    assert "'derived'" not in roundtrip["down_check"]
    # Everything the old vocabulary had is still there — narrowing must not
    # quietly drop a value the previous revision allowed.
    for kept in ("appended", "submitted", "placed", "ordered", "picked-up"):
        assert f"'{kept}'" in roundtrip["down_check"], kept


def test_a_derived_row_rolls_back_to_appended_and_not_to_ordered(roundtrip):
    """The decision this migration makes. `ordered` would promote an agent-applied
    sequence to a human's at the one moment nothing records that it happened."""
    assert roundtrip["after_down"]["an agent applied this"] == "appended"


def test_a_human_ordered_row_is_untouched_by_the_rollback(roundtrip):
    """Only rows the old vocabulary cannot hold are rewritten."""
    assert roundtrip["after_down"]["a person typed this"] == "ordered"


def test_the_rollback_changes_no_rank(roundtrip):
    """The list still reads in the same sequence — only the claim about who chose
    it weakens. A downgrade that reordered the plan would be a much larger event
    than the one being described."""
    assert roundtrip["ranks_down"] == {"an agent applied this": 1,
                                       "a person typed this": 2}
