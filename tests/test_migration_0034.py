"""What migration 0034 does to a board holding one repository under two spellings.

The suite's schema fixture runs 0034 in both directions on every db-backed run,
but only over EMPTY tables — the one shape a data migration cannot be wrong about.
This module gives it the shape #350 is filed about, in both of its columns:

* **`dial_settings`** — a floor set under `PrisonBlues/Quarterback` beside one set
  under `prisonblues/quarterback`. `ix_dial_settings_live` is UNIQUE over
  `COALESCE(repo,'')` and `dial` where `cleared_at IS NULL`, so those are two live
  values for one setting and a resolution answers with whichever it matched.
* **`worktrees`** — the same repository registered by two devices whose remotes
  are spelled differently, which is what `GET /worktrees?repo=` compared with `==`.

Three halves, and they fail differently:

* **The fold** is the repair, and its acceptance is that afterwards there is one
  spelling for a reader to ask about.
* **The CHECK constraints** are what close the class rather than fixing two
  endpoints a third time (#67), and they are asserted by writing a second spelling
  *around* the API — a validator is only a promise about the callers you know
  about.
* **The refusal**, which no live board exercises: folding can put two live rows on
  one dial, and those are two values a person set, each with a reason and an
  author. :func:`~migrations.versions.plan_dial_folds` raises instead of picking,
  for the reason `0022`, `0031` and `0033` raise.

**A throwaway database, created and dropped here**, for ``test_migration_0033``'s
reason: the migration is the thing under test, so it has to run for real at 0033
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

FLOOR = "review_panel.fix_severity_floor"
ROUNDS = "review_panel.max_rounds"

#: `(repo, dial, value, cleared)`. One repository under the two spellings two
#: checkouts produce, a fleet dial that must come through untouched, and a
#: repository whose owner starts with the letter the CHECK's whitespace class must
#: not contain — `E'…\v'` has no meaning in Postgres, so the backslash is dropped
#: and `btrim` gains a literal `v`, folding this row to `ercel/next` and failing
#: the constraint the same migration adds. Seeded rather than asserted in the
#: abstract, because that is the form the failure actually takes.
DIALS = [
    ("PrisonBlues/Quarterback", FLOOR, '"P2"', False),
    ("prisonblues/quarterback", ROUNDS, "3", False),
    (None, "reviewers.pi.enabled", "false", False),
    ("Vercel/Next", FLOOR, '"P3"', False),
    # Cleared: outside `ix_dial_settings_live`, so nothing can collide with it —
    # and it still folds, because the constraint is on the column and a history
    # row spelled two ways is one a repo-scoped read of it would miss.
    ("Acme/Widget", FLOOR, '"P1"', True),
    # A spelling the OLD validator admitted and `canonical_repo` refuses. Cleared,
    # so it is history rather than a setting in force — it folds and stays, and the
    # migration does not stop for it. A LIVE one does; see the planner tests.
    ("A_B/C", ROUNDS, "5", True),
]

#: `(device, path, repo)`. Two devices, one repository, two remotes; a checkout
#: with no GitHub-style remote at all (NULL, which the column allows and `/sync`
#: reads as "has none"); and a legacy BARE name from before the MCP tools derived
#: the slug — it folds to lower case and stays, because the constraint asserts the
#: case and not the shape.
WORKTREES = [
    ("zeus", "/src/a", "PrisonBlues/Quarterback"),
    ("laptop", "/src/b", "prisonblues/quarterback"),
    ("zeus", "/src/c", "Vercel/Next"),
    ("zeus", "/src/d", None),
    ("zeus", "/src/e", "Quarterback"),
]

_INSERT_DIAL = """
    INSERT INTO dial_settings (repo, dial, value, reason, set_by, set_at, cleared_at)
    VALUES ($1, $2, $3::jsonb, 'seeded', 'human/rich', now(),
            CASE WHEN $4::boolean THEN now() ELSE NULL END)
"""
_INSERT_WORKTREE = """
    INSERT INTO worktrees (device, path, repo, branch) VALUES ($1, $2, $3, 'main')
"""


@pytest.fixture(scope="module")
def migration():
    """Revision 0034, imported by path — it is not on a package path."""
    path = REPO_ROOT / "migrations" / "versions" / "0034_canonical_dial_and_worktree_repo.py"
    spec = importlib.util.spec_from_file_location("m0034", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- the planner, without a database -----------------------------------------


def _row(rid, repo, dial=FLOOR, by="human/rich", at="2026-08-22"):
    return (rid, repo, dial, by, at)


def test_a_capitalised_row_is_folded(migration):
    assert migration.plan_dial_folds([_row(1, "Acme/Widget")]) == [(1, "acme/widget")]


def test_a_row_already_canonical_does_not_move(migration):
    """A no-op UPDATE on a settings table is a write nobody made, on rows whose
    whole purpose is to record who moved a floor and when."""
    assert migration.plan_dial_folds([_row(1, "acme/widget")]) == []


def test_surrounding_space_is_part_of_the_spelling_too(migration):
    """`canonical_repo` strips before it folds, so a value that reached the column
    with a trailing space is a third spelling of the same repo — and one more live
    row the unique index counted as a different scope."""
    assert migration.plan_dial_folds([_row(1, " Acme/Widget ")]) == [(1, "acme/widget")]


def test_the_fleet_scope_folds_to_itself(migration):
    """NULL is every repo, not a repo spelled badly. It is carried through the
    grouping rather than filtered out — the index keys it as `''` and a fleet dial
    can be doubled like any other — but it never moves."""
    assert migration.plan_dial_folds([_row(1, None)]) == []


def test_two_spellings_holding_one_dial_stops_the_migration(migration):
    """The refusal. These are two values in force for one setting, and picking the
    newer would move a policy floor on the strength of a timestamp."""
    with pytest.raises(RuntimeError) as e:
        migration.plan_dial_folds([
            _row(1, "Acme/Widget", by="human/rich", at="2026-08-01"),
            _row(2, "acme/widget", by="human/sam", at="2026-08-02"),
        ])
    assert "two live rows on one dial" in str(e.value)
    # It has to name the rows, or the human it stops cannot act on it.
    assert "acme/widget" in str(e.value)
    assert "'human/rich'" in str(e.value) and "'human/sam'" in str(e.value)
    # And the ids, because the SQL it hands over is keyed on one: a message that
    # names the rows in prose and then asks for `<the stale id>` has stopped one
    # step short of being actionable.
    assert "(id 1)" in str(e.value) and "(id 2)" in str(e.value)
    # And say how to settle it — cleared, not deleted: the history of a dial's
    # moves is what the column is for.
    assert "SET cleared_at" in str(e.value)


def test_two_fleet_rows_on_one_dial_are_a_clash_too(migration):
    """`COALESCE(repo,'')` is what the index keys on, so the fleet scope is as
    capable of holding two live rows for one dial as a repo scope is — #276's
    throttle writes there, and a planner blind to it would hand the constraint a
    violation to discover."""
    with pytest.raises(RuntimeError) as e:
        migration.plan_dial_folds([_row(1, None), _row(2, None)])
    assert "(fleet)" in str(e.value)


def test_a_live_dial_in_a_shape_the_endpoints_refuse_stops_the_migration(migration):
    """The old validator admitted `a_b/c` and `canonical_repo` does not, so after
    this revision a row scoped that way is a setting IN FORCE that no caller can
    name, list or turn off. Left standing it would be worse than the second
    spelling the revision is about."""
    with pytest.raises(RuntimeError) as e:
        migration.plan_dial_folds([_row(1, "a_b/c"), _row(2, "acme/widget", dial=ROUNDS)])
    assert "not `owner/name`" in str(e.value)
    assert "'a_b/c'" in str(e.value) and "(id 1)" in str(e.value)
    assert "acme/widget" not in str(e.value), "named a row that is perfectly fine"


def test_a_dot_git_scope_is_caught_by_the_same_check(migration):
    """`REPO_RE` refuses a trailing `.git` rather than stripping it — GitHub does
    not allow a repository name to end that way either, so the only thing that
    spelling can be is a clone URL's suffix."""
    with pytest.raises(RuntimeError) as e:
        migration.plan_dial_folds([_row(1, "acme/widget.git")])
    assert "not `owner/name`" in str(e.value)


def test_both_refusals_are_reported_together(migration):
    """Two problems fixed one deploy at a time is two failed deploys. The planner
    accumulates and raises once, so an operator sees the whole of what is in the
    way."""
    with pytest.raises(RuntimeError) as e:
        migration.plan_dial_folds([
            _row(1, "Acme/Widget"), _row(2, "acme/widget"), _row(3, "a_b/c"),
        ])
    assert "two live rows on one dial" in str(e.value)
    assert "not `owner/name`" in str(e.value)


def test_one_repo_holding_two_different_dials_is_not_a_clash(migration):
    """The key is `(repo, dial)`. A repo with a floor and a round cap is the
    ordinary case, and a planner grouping on the repo alone would refuse to run on
    any board that had one."""
    assert migration.plan_dial_folds([
        _row(1, "Acme/Widget", dial=FLOOR),
        _row(2, "Acme/Widget", dial=ROUNDS),
    ]) == [(1, "acme/widget"), (2, "acme/widget")]


def test_the_same_dial_in_two_repos_is_not_a_clash(migration):
    assert migration.plan_dial_folds([
        _row(1, "Acme/Widget"), _row(2, "acme/other"),
    ]) == [(1, "acme/widget")]


def test_a_clean_board_is_a_noop(migration):
    assert migration.plan_dial_folds([]) == []


def test_the_whitespace_class_does_not_eat_a_letter(migration):
    r"""Postgres's escape-string syntax has no `\v`: a backslash before it is
    dropped and the class silently gains the LETTER `v`, so
    `btrim('vercel/next', E' \t\n\r\f\v')` is `'ercel/next'` — and the CHECKs this
    migration adds would refuse a repository for being named after its owner.
    `\013` is the octal spelling, which the models and `0033` already use for the
    same reason. Asserted on the SQL text because the failure is invisible in every
    test whose repo happens not to start or end with a `v`."""
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
    """A fresh database at 0033, and the URLs for reaching it."""
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
    _must(sa_url, "upgrade", "0033")
    return sa_url, dsn, admin_dsn, db


async def _drop(admin_dsn: str, db: str) -> None:
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
    finally:
        await admin.close()


@pytest.fixture(scope="module")
async def migrated():
    """Seed two spellings at 0033, upgrade, then ask what the board holds."""
    sa_url, dsn, admin_dsn, db = await _scratch("m0034")
    try:
        conn = await asyncpg.connect(dsn)
        try:
            for row in DIALS:
                await conn.execute(_INSERT_DIAL, *row)
            for row in WORKTREES:
                await conn.execute(_INSERT_WORKTREE, *row)
        finally:
            await conn.close()

        _must(sa_url, "upgrade", "head")
        conn = await asyncpg.connect(dsn)
        try:
            state = {
                "dials": [dict(r) for r in await conn.fetch(
                    "SELECT repo, dial, cleared_at IS NULL AS live FROM dial_settings "
                    "ORDER BY dial, repo NULLS FIRST")],
                "worktrees": [dict(r) for r in await conn.fetch(
                    "SELECT device, path, repo FROM worktrees ORDER BY path")],
                "refused": {},
            }
            try:
                await conn.execute(_INSERT_DIAL, "Acme/Sneaky", FLOOR, "1", False)
            except asyncpg.exceptions.CheckViolationError as e:
                state["refused"]["dial_settings"] = e.constraint_name
            try:
                await conn.execute(_INSERT_WORKTREE, "zeus", "/src/z", "Acme/Sneaky")
            except asyncpg.exceptions.CheckViolationError as e:
                state["refused"]["worktrees"] = e.constraint_name
        finally:
            await conn.close()

        _must(sa_url, "downgrade", "0033")
        conn = await asyncpg.connect(dsn)
        try:
            state["after_downgrade"] = {
                "dials": await conn.fetchval("SELECT count(*) FROM dial_settings"),
                "worktrees": await conn.fetchval("SELECT count(*) FROM worktrees"),
                # Only this revision's two. `0033`'s are still in place at the
                # revision we downgraded TO, and a `LIKE '%repo_canonical%'`
                # would report them as this downgrade having failed.
                "checks": [r["conname"] for r in await conn.fetch(
                    "SELECT conname FROM pg_constraint WHERE conname IN "
                    "('ck_dial_settings_repo_canonical', 'ck_worktrees_repo_canonical')")],
            }
        finally:
            await conn.close()
        yield state
    finally:
        await _drop(admin_dsn, db)


def test_the_two_dial_spellings_become_one(migrated):
    """#350's acceptance at the storage layer for the settings table: the two
    scopes two checkouts could write become the one scope every read asks about."""
    assert {(d["repo"], d["dial"]) for d in migrated["dials"]} == {
        ("prisonblues/quarterback", FLOOR),
        ("prisonblues/quarterback", ROUNDS),
        (None, "reviewers.pi.enabled"),
        ("vercel/next", FLOOR),
        ("acme/widget", FLOOR),
        ("a_b/c", ROUNDS),
    }


def test_the_fleet_dial_is_left_alone(migrated):
    """NULL is every repo. A migration that coalesced it to `''` would have made
    the one scope #276's throttle writes to unreachable by every read, all of which
    ask for it with `repo IS NULL`."""
    fleet = [d for d in migrated["dials"] if d["repo"] is None]
    assert [(d["dial"], d["live"]) for d in fleet] == [("reviewers.pi.enabled", True)]


def test_a_cleared_row_folds_too(migrated):
    """It sits outside `ix_dial_settings_live` and so cannot clash — but the
    constraint is on the column, and the history of who moved a floor is only
    readable by repo if it is spelled the way the repo is."""
    cleared = [d for d in migrated["dials"] if not d["live"]]
    assert sorted((d["repo"], d["dial"]) for d in cleared) == [
        ("a_b/c", ROUNDS), ("acme/widget", FLOOR)]


def test_the_two_worktree_spellings_become_one(migrated):
    """The disagreement in the other column: `/worktrees?repo=` compared exactly
    while `/sync` folded by basename, so two devices reporting one repository were
    one repo to one endpoint and two to the other."""
    assert [(w["path"], w["repo"]) for w in migrated["worktrees"]] == [
        ("/src/a", "prisonblues/quarterback"),
        ("/src/b", "prisonblues/quarterback"),
        ("/src/c", "vercel/next"),
        ("/src/d", None),
        ("/src/e", "quarterback"),
    ]


def test_a_legacy_bare_name_folds_rather_than_aborting_the_migration(migrated):
    """The constraint asserts case and surrounding whitespace, NOT `owner/name`.
    The shape is refused at ingest where a caller can be told why; rows written
    before that check are legitimately here, and a constraint that rejected them
    would make this revision unrunnable rather than make it canonical. Folded, it
    is also exactly what `GET /worktrees?repo=quarterback` now matches."""
    assert ("/src/e", "quarterback") in [(w["path"], w["repo"]) for w in migrated["worktrees"]]


def test_a_repository_named_after_its_owner_survives(migrated):
    """`Vercel/Next` is the row that fails if the whitespace class is written
    `E' \\t\\n\\r\\f\\v'`: `btrim` would eat the leading letter and the constraint
    the same migration adds would reject a real repository."""
    assert "vercel/next" in {d["repo"] for d in migrated["dials"]}
    assert "vercel/next" in {w["repo"] for w in migrated["worktrees"]}


def test_the_database_itself_refuses_the_next_second_spelling(migrated):
    """What makes this the last time. An INSERT that never touches the API — an
    admin script, a backfill, the write path somebody adds next year — cannot
    reintroduce the spelling the endpoints stopped folding for."""
    assert migrated["refused"] == {
        "dial_settings": "ck_dial_settings_repo_canonical",
        "worktrees": "ck_worktrees_repo_canonical",
    }


def test_the_downgrade_lets_go_without_losing_rows(migrated):
    """Which capitals a row was written with is not recoverable and does not need
    to be: the downgrade drops the constraints and leaves the rows, because the
    only thing it changed was the case of a name GitHub itself folds."""
    assert migrated["after_downgrade"]["dials"] == len(DIALS)
    assert migrated["after_downgrade"]["worktrees"] == len(WORKTREES)
    assert migrated["after_downgrade"]["checks"] == []
