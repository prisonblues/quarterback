"""What migration 0025's backfill does with phases that were already over.

The suite's schema fixture runs 0025 in both directions on every db-backed run,
but only over an EMPTY ``plan_items`` — which is the one shape a data migration
cannot be wrong about. This module gives it rows.

The defect it pins: the first version of the backfill inserted every distinct
(repo, phase) as an ``open`` plan, including the phases whose every item was
already ``done``. ``ix_plans_open_label`` is UNIQUE per (scope, folded label)
while open, so after that migration ``POST /plan/submit {label: "stage 1"}``
answered 409 in any repo that had ever finished a phase called "stage 1" — and
the plan it pointed at had zero open items, so nothing would ever close it and
nothing would ever prompt anyone to. ``test_plans.py::
test_a_finished_label_is_free_for_the_next_plan`` asserts that reusing a label
after finishing is the intended workflow; an all-open backfill denied it for
every label the board had ever used.

**A throwaway database, created and dropped here.** The migration is the thing
under test, so it has to run for real, at 0024, over data — and the suite's own
database is at ``head`` with every other test's rows in it. Walking it back to
0024 mid-session would break them, and in a worktree it is shared with whatever
else is running. So this builds its own.

**One fixture, both directions, snapshots.** Running the chain is the expensive
part, so it runs once: seed at 0024, upgrade, snapshot, then downgrade and
snapshot again. Each test below names one fact about those snapshots, which is
what makes a failure say which half broke.
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

#: (repo, phase, rank, state, added_by, title, done_at offset in minutes).
#: Every shape the backfill has to tell apart, and the case variants are the
#: reason the grouping folds at all.
SEED = [
    # An open item in the group: the plan is still open.
    ("own/alpha", "stage 1", 1, "open", "zeus/one", "s1 open", None),
    ("own/alpha", "stage 1", 2, "done", "zeus/two", "s1 done", 10),
    # Nothing open, something done: finished, at the moment the last item was.
    ("own/alpha", "stage 2", 3, "done", "zeus/three", "s2 first done", 20),
    ("own/alpha", "stage 2", 4, "done", "zeus/four", "s2 last done", 99),
    ("own/alpha", "stage 2", 5, "dropped", "zeus/five", "s2 dropped", None),
    # Mixed, with the open item ranked BELOW a finished one — a `first row of
    # the group` reading of the state would call this one done.
    ("own/alpha", "stage 5", 6, "done", "zeus/six", "s5 done first", 30),
    ("own/alpha", "stage 5", 7, "open", "zeus/seven", "s5 open later", None),
    # One label in three spellings. One plan, labelled and authored by the
    # first-ranked item — not lower-cased, and not `min()` over the group.
    ("own/alpha", "Stage 3", 8, "done", "zeus/eight", "v1", 40),
    ("own/alpha", "stage 3", 9, "done", "zeus/nine", "v2", 50),
    ("own/alpha", "  STAGE 3  ", 10, "done", "zeus/ten", "v3", 60),
    # Every item dropped: abandoned is not finished, and carries no done_at.
    ("own/alpha", "stage 4", 11, "dropped", "zeus/x1", "d1", None),
    ("own/alpha", "stage 4", 12, "dropped", "zeus/x2", "d2", None),
    # The same label in another repo is another plan — the scope is half the key.
    ("own/beta", "stage 2", 13, "open", "laptop/a", "beta s2 open", None),
    # Fleet scope: repo IS NULL, which COALESCE(repo, '') has to group as itself.
    (None, "fleet phase", 14, "done", "laptop/b", "fleet done", 70),
    # No phase, and a whitespace-only one: neither is a plan.
    ("own/alpha", None, 15, "open", "zeus/none", "phaseless", None),
    ("own/alpha", "   ", 16, "open", "zeus/blank", "blank phase", None),
]

_INSERT_ITEM = """
    INSERT INTO plan_items (id, repo, title, phase, rank, state, added_by,
                            created_at, updated_at, done_at, done_by)
    VALUES ($1, $2, $3, $4, $5, $6, $7,
            now() - interval '1 day', now() - interval '1 day',
            CASE WHEN $8::int IS NULL THEN NULL
                 ELSE now() - interval '1 day' + ($8::int * interval '1 minute') END,
            CASE WHEN $8::int IS NULL THEN NULL ELSE $7 END)
"""


def _alembic(url: str, *args: str) -> None:
    """Run alembic against `url`, from the repo root so alembic.ini is found.

    An explicit DATABASE_URL wins over the checkout's .env in `app.config`, which
    is the same lever `tests/conftest.py` and CI both pull.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT, env={**os.environ, "DATABASE_URL": url},
        capture_output=True, text=True,
    )
    if proc.returncode != 0:  # pragma: no cover - only when a migration breaks
        raise AssertionError(
            f"alembic {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}")


@pytest.fixture(scope="module")
async def snapshots(_db_claim):
    """Seed at 0024, upgrade, downgrade, and hand back what each direction left.

    Both directions in one fixture because the chain is what costs: the tests
    read snapshots rather than a live database, so they cannot depend on each
    other's ordering either.
    """
    base = make_url(os.environ["DATABASE_URL"])
    scratch = f"{base.database}_m0025"
    sa_url = base.set(database=scratch).render_as_string(hide_password=False)
    dsn = base.set(drivername="postgresql", database=scratch).render_as_string(
        hide_password=False)
    # The maintenance database, not the bound one: this connection creates and
    # drops the scratch database below, and since #366 the bound database is
    # this run's own — which a run that collects only these modules never
    # builds, because nothing here asks for the schema fixture.
    admin_dsn = dbrun.admin_dsn(os.environ["DATABASE_URL"])

    admin = await asyncpg.connect(admin_dsn)
    try:
        # FORCE, because a half-finished earlier run may still hold a session on
        # it and a plain DROP would fail on that rather than on anything real.
        await admin.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{scratch}"')
    finally:
        await admin.close()

    try:
        _alembic(sa_url, "upgrade", "0024")
        conn = await asyncpg.connect(dsn)
        try:
            for repo, phase, rank, state, added_by, title, off in SEED:
                await conn.execute(_INSERT_ITEM, uuid.uuid4(), repo, title, phase,
                                   rank, state, added_by, off)
        finally:
            await conn.close()

        _alembic(sa_url, "upgrade", "head")
        conn = await asyncpg.connect(dsn)
        try:
            up = {
                "plans": [dict(r) for r in await conn.fetch("""
                    SELECT repo, label, state, added_by, done_at, done_by
                      FROM plans ORDER BY COALESCE(repo, ''), lower(label)""")],
                "items": [dict(r) for r in await conn.fetch("""
                    SELECT i.rank, i.title, i.state, p.label AS plan_label
                      FROM plan_items i LEFT JOIN plans p ON p.id = i.plan_id
                     ORDER BY i.rank""")],
                "duplicate_groups": await conn.fetch("""
                    SELECT COALESCE(repo, '') AS scope, lower(label) AS folded
                      FROM plans GROUP BY 1, 2 HAVING count(*) > 1"""),
                "last_s2_done": await conn.fetchval("""
                    SELECT max(done_at) FROM plan_items
                     WHERE title LIKE 's2 %' AND state = 'done'"""),
                "phase_column": await conn.fetchval("""
                    SELECT count(*) FROM information_schema.columns
                     WHERE table_name = 'plan_items' AND column_name = 'phase'"""),
            }
            # The consequence, asked of the index itself rather than of the
            # states: is the finished label free for the next plan, and is the
            # open one still taken? A guard that only checked the first would
            # pass just as well if the index had been dropped.
            up["reuse_of_a_finished_label"] = await _insert_open_plan(
                conn, "own/alpha", "STAGE 2")
            up["reuse_of_an_open_label"] = await _insert_open_plan(
                conn, "own/alpha", "Stage 1")
        finally:
            await conn.close()

        _alembic(sa_url, "downgrade", "0024")
        conn = await asyncpg.connect(dsn)
        try:
            down = {
                "items": [dict(r) for r in await conn.fetch(
                    "SELECT rank, title, phase FROM plan_items ORDER BY rank")],
                "plans_table": await conn.fetchval("""
                    SELECT count(*) FROM information_schema.tables
                     WHERE table_name = 'plans'"""),
                "plan_id_column": await conn.fetchval("""
                    SELECT count(*) FROM information_schema.columns
                     WHERE table_name = 'plan_items' AND column_name = 'plan_id'"""),
            }
        finally:
            await conn.close()

        yield {"up": up, "down": down}
    finally:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
        finally:
            await admin.close()


async def _insert_open_plan(conn: asyncpg.Connection, repo: str, label: str) -> str:
    """"admitted" or "refused" — whether the open-label index takes this plan."""
    try:
        await conn.execute(
            "INSERT INTO plans (repo, label, added_by) VALUES ($1, $2, 'zeus/next')",
            repo, label)
    except asyncpg.UniqueViolationError:
        return "refused"
    return "admitted"


def _plan(plans: list[dict], repo: str | None, folded: str) -> dict:
    match = [p for p in plans if p["repo"] == repo and p["label"].lower() == folded]
    assert len(match) == 1, f"expected one {folded!r} plan in {repo!r}, got {match}"
    return match[0]


async def test_a_phase_with_no_open_items_arrives_finished(snapshots):
    """The finding. A phase that was over before the migration ran must not
    occupy its label slot afterwards, and `state` is read off the items rather
    than assumed — the same arithmetic `POST /plan/done` applies."""
    plans = snapshots["up"]["plans"]

    assert _plan(plans, "own/alpha", "stage 2")["state"] == "done"
    assert _plan(plans, "own/alpha", "stage 3")["state"] == "done"
    assert _plan(plans, None, "fleet phase")["state"] == "done"
    # Abandoned is not finished: `app.models.plan.STATES` exists to keep those
    # two apart, and every item of this one was dropped by a person.
    assert _plan(plans, "own/alpha", "stage 4")["state"] == "dropped"
    # And an open item anywhere in the group keeps the plan open — including
    # when it is ranked below a finished one.
    assert _plan(plans, "own/alpha", "stage 1")["state"] == "open"
    assert _plan(plans, "own/alpha", "stage 5")["state"] == "open"
    assert _plan(plans, "own/beta", "stage 2")["state"] == "open"


async def test_the_label_of_a_finished_phase_is_free_for_the_next_plan(snapshots):
    """`test_a_finished_label_is_free_for_the_next_plan` asserts this workflow
    against the API; this asserts the migration does not take it away. Asked of
    `ix_plans_open_label` directly, and asked BOTH ways round so that an index
    that had stopped working could not pass the first half."""
    assert snapshots["up"]["reuse_of_a_finished_label"] == "admitted"
    assert snapshots["up"]["reuse_of_an_open_label"] == "refused"


async def test_a_finished_plan_ended_when_its_last_item_did(snapshots):
    """`done_at` is the last item's completion, not the migration's clock: the
    phase ended when the work did, and a plan stamped `now()` would read as
    finished today for every phase the board has ever closed.

    `done_by` stays NULL, deliberately. Nobody closed these plans — they did not
    exist to be closed — and naming whichever agent finished the last item would
    invent a decision that was never taken."""
    up = snapshots["up"]
    stage2 = _plan(up["plans"], "own/alpha", "stage 2")

    assert stage2["done_at"] == up["last_s2_done"]
    assert stage2["done_by"] is None
    # A dropped plan carries no completion time, exactly as dropping an item
    # clears its own.
    assert _plan(up["plans"], "own/alpha", "stage 4")["done_at"] is None
    # Nor does an open one.
    assert _plan(up["plans"], "own/alpha", "stage 1")["done_at"] is None


async def test_case_variants_of_one_label_become_one_plan(snapshots):
    """The whole point of folding when grouping. The label KEPT is the
    first-ranked item's own spelling, trimmed — a plan's label is read by
    people — and so is the author: the plan's author is whoever started the
    phase, which is the truest thing this schema can know about a string."""
    up = snapshots["up"]
    stage3 = _plan(up["plans"], "own/alpha", "stage 3")

    assert stage3["label"] == "Stage 3"
    assert stage3["added_by"] == "zeus/eight"
    assert [i["rank"] for i in up["items"] if i["plan_label"] == "Stage 3"] == [8, 9, 10]
    assert not up["duplicate_groups"], "one plan per (scope, folded label)"


async def test_every_item_that_named_a_phase_points_at_its_plan(snapshots):
    """A phase left behind is an item nobody can find through the plan it was
    written into. Whitespace-only and NULL phases are the two that correctly
    become nothing: `phase` is gone by the end of the upgrade, so an item is
    only reachable through `plan_id`."""
    items = {i["title"]: i["plan_label"] for i in snapshots["up"]["items"]}

    assert items["beta s2 open"] == "stage 2"
    assert items["fleet done"] == "fleet phase"
    assert items["s5 open later"] == "stage 5"
    assert items["phaseless"] is None
    assert items["blank phase"] is None
    assert sum(1 for v in items.values() if v is not None) == 14
    assert snapshots["up"]["phase_column"] == 0, "the upgrade drops `phase`"


async def test_the_downgrade_puts_the_phase_back_on_every_item(snapshots):
    """The rollback the module docstring promises. Every item that carried a
    phase carries one again — including the items of a plan the backfill closed,
    which a downgrade keyed on open plans alone would have stranded.

    What a rollback cannot carry is stated in 0025's own docstring: a plan's
    note, state and identity. Two more, visible here: the case variants come
    back as the spelling that was kept, and a whitespace-only phase comes back
    NULL. Both are the folding doing its job, one direction later."""
    down = snapshots["down"]
    phases = {i["title"]: i["phase"] for i in down["items"]}

    assert phases["s1 open"] == "stage 1"
    assert phases["s2 dropped"] == "stage 2", "a closed plan's items keep their phase"
    assert phases["d1"] == "stage 4"
    assert phases["fleet done"] == "fleet phase"
    assert phases["v1"] == phases["v2"] == phases["v3"] == "Stage 3"
    assert phases["phaseless"] is None
    assert phases["blank phase"] is None
    assert sum(1 for v in phases.values() if v is not None) == 14
    assert down["plans_table"] == 0 and down["plan_id_column"] == 0
