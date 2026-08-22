"""A run's database is its own, and a run that would take another's is refused.

Two pytest runs in one checkout used to share one database and rebuild it under
each other, which reported as impossible failures in whichever modules happened
to be executing rather than as anything naming the cause (#366). These are the
guards on the answer: names that cannot collide, a claim that refuses when they
somehow do, and a reaper conservative enough to be allowed to drop databases.

The naming and matching tests need no Postgres. The rest do, and say so by
taking an administrative connection to the same server the suite is bound to.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from . import dbrun
from .dbtarget import database_name

REPO_ROOT = Path(__file__).resolve().parent.parent


def bound_database() -> str:
    """The database this run is pointed at.

    Read out of the environment rather than imported from `conftest`, so these
    tests can be run against a `conftest` that has not been fixed and fail on
    their assertions rather than on an import — which is what makes them a
    regression test for #366 and not a description of the code beneath them.
    """
    return database_name(os.environ["DATABASE_URL"])

#: A base name nothing on the box uses, so the reaper tests can create and drop
#: real databases without a name any checkout could also have composed.
SYNTHETIC_BASE = f"qbdbrun{os.getpid()}"


# --- names ------------------------------------------------------------------


def test_each_run_gets_its_own_database_name():
    one = dbrun.run_database_name("quarterback", "111")
    other = dbrun.run_database_name("quarterback", "222")
    assert one == "quarterback_r111"
    assert one != other


def test_run_scoped_url_changes_the_database_and_nothing_else():
    url = "postgresql+asyncpg://user:pw@db.example:5433/quarterback?ssl=require"
    scoped = make_url(dbrun.run_scoped_url(url, "77"))
    assert scoped.database == "quarterback_r77"
    original = make_url(url)
    assert (scoped.drivername, scoped.username, scoped.host, scoped.port, scoped.query) == (
        original.drivername,
        original.username,
        original.host,
        original.port,
        original.query,
    )
    assert scoped.password == original.password


def test_a_url_that_names_no_database_cannot_be_run_scoped():
    with pytest.raises(ValueError, match="names none"):
        dbrun.run_scoped_url("postgresql+asyncpg://user:pw@db.example:5433/", "77")


def test_the_run_id_survives_a_base_too_long_for_the_identifier_limit():
    # PostgreSQL truncates past 63 bytes silently, so a name composed base-first
    # would hand two concurrent runs one database while looking like it had not.
    # The base is what gives way.
    name = dbrun.run_database_name("q" * 200, "31415")
    assert name.endswith("_r31415")
    assert len(name) <= dbrun.MAX_IDENTIFIER
    assert name != dbrun.run_database_name("q" * 200, "31416")


def test_a_run_database_leaves_room_for_the_migration_suites_scratch_names():
    # tests/test_migration_*.py append their own suffixes to whatever database
    # the suite is bound to and DROP … WITH (FORCE) the result. Read off those
    # modules rather than assumed, and checked against a base already at its own
    # limit, so a longer one added later fails here instead of silently colliding
    # with a sibling run past byte 63.
    longest_scratch = _longest_scratch_suffix()
    name = dbrun.run_database_name("q" * 200, "4194304")  # Linux's PID_MAX_LIMIT
    assert len(name + longest_scratch) <= dbrun.MAX_IDENTIFIER
    assert len(longest_scratch) <= dbrun.SCRATCH_HEADROOM


def test_a_run_id_too_long_to_fit_is_refused_rather_than_truncated():
    with pytest.raises(ValueError, match="scratch suffixes"):
        dbrun.run_database_name("quarterback", "1" * 40)


def test_a_run_id_that_is_not_a_pid_is_refused():
    with pytest.raises(ValueError, match="must be digits"):
        dbrun.run_database_name("quarterback", "3; DROP DATABASE quarterback")


def test_this_runs_id_is_this_process():
    assert dbrun.current_run_id({}) == str(os.getpid())


def test_a_run_id_can_be_named_in_advance():
    assert dbrun.current_run_id({"QB_TEST_RUN_ID": " 4242 "}) == "4242"


def test_a_named_run_id_that_no_reaper_could_read_is_refused():
    with pytest.raises(ValueError, match="QB_TEST_RUN_ID"):
        dbrun.current_run_id({"QB_TEST_RUN_ID": "landing-agent"})


# --- which databases are a checkout's to reap -------------------------------


def test_a_run_database_is_recognised_by_the_checkout_that_composed_it():
    assert dbrun.run_id_of("quarterback_r4242", "quarterback") == 4242


def test_a_scratch_database_belongs_to_the_run_that_derived_it():
    assert dbrun.run_id_of("quarterback_r4242_m0025", "quarterback") == 4242
    assert dbrun.run_id_of("quarterback_r4242_drift", "quarterback") == 4242


def test_the_main_checkout_does_not_read_a_worktrees_database_as_its_own():
    # `quarterback` is a prefix of `quarterback_fix_issue_366`, so a prefix match
    # would let the main checkout reap every worktree's run databases. The stem
    # has to match exactly.
    assert dbrun.run_id_of("quarterback_fix_issue_366_r99", "quarterback") is None
    assert dbrun.run_id_of("quarterback_fix_issue_366_r99", "quarterback_fix_issue_366") == 99


def test_a_nested_run_belongs_to_the_run_it_was_derived_from():
    # A run inside a run — which is what the end-to-end tests below produce.
    # Read from its own base it is run 2, and the tail has to be matched from the
    # front to say so. Read from the outer base it is one of run 1's derived
    # databases, which is also true; and if run 1 ends while run 2 has not, run
    # 2's claim is what stops the outer checkout reaping it.
    assert dbrun.run_id_of("quarterback_r1_r2", "quarterback_r1") == 2
    assert dbrun.run_id_of("quarterback_r1_r2", "quarterback") == 1


def test_a_database_with_no_run_suffix_belongs_to_no_run():
    for name in ("quarterback", "quarterback_fix_issue_366", "quarterback_review_2"):
        assert dbrun.run_id_of(name, "quarterback") is None


def test_liveness_fails_closed():
    assert dbrun.is_alive(os.getpid())
    assert dbrun.is_alive(0)  # not a pid a run database can carry; not a licence to drop
    finished = subprocess.Popen([sys.executable, "-c", ""])
    finished.wait()
    assert not dbrun.is_alive(finished.pid)


def test_an_identifier_with_a_quote_in_it_cannot_end_the_statement_it_is_in():
    # CREATE/DROP DATABASE take no bind parameters, so every name reaches the
    # server inside a string — one from a checkout's URL, one from pg_database.
    assert dbrun.quoted('q"; DROP DATABASE quarterback; --') == (
        '"q""; DROP DATABASE quarterback; --"'
    )


def test_the_identifier_limit_is_counted_in_bytes():
    # NAMEDATALEN counts the encoded form. Measured in characters, a base name
    # with a non-ASCII character in it fits, PostgreSQL truncates it anyway, and
    # the run id that made the name unique is what falls off the end.
    name = dbrun.run_database_name("é" * 200, "12345")
    assert len(name.encode()) <= dbrun.MAX_IDENTIFIER - dbrun.SCRATCH_HEADROOM
    assert name.endswith("_r12345")
    assert "\ufffd" not in name  # cut between characters, never through one


def test_the_advisory_keys_fit_the_columns_postgres_stores_them_in():
    # Both halves are passed as int4 and read back out of pg_locks' oid columns,
    # so a negative one would be rejected going in or unfindable coming out.
    for name in ("quarterback", "quarterback_r1", "q" * 63):
        for key in dbrun.lock_keys(name):
            assert 0 <= key < 2**31
    assert dbrun.lock_keys("quarterback_r1") != dbrun.lock_keys("quarterback_r2")


def test_the_administrative_connection_is_not_made_to_the_database_being_dropped():
    dsn = make_url(dbrun.admin_dsn("postgresql+asyncpg://u:p@h:5433/quarterback_r1"))
    assert dsn.database == dbrun.MAINTENANCE_DB
    assert dsn.drivername == "postgresql"  # asyncpg is connected to directly
    assert (dsn.host, dsn.port, dsn.username, dsn.password) == ("h", 5433, "u", "p")


# --- what the suite is actually bound to ------------------------------------


def test_the_suite_binds_a_database_of_its_own_not_the_one_it_was_pointed_at():
    # The bug, stated as a property: a database whose name does not carry the
    # run is a database the next run in this checkout binds as well.
    bound = bound_database()
    assert bound.endswith(f"_r{dbrun.current_run_id()}"), (
        f"the suite bound {bound!r}, which a second run in this checkout would "
        f"bind too — and rebuild under it (#366)"
    )


async def test_the_run_database_exists_and_says_which_run_built_it(_schema):
    # The comment is not decoration: the reaper refuses to drop a database that
    # does not carry it, which is what keeps it away from the per-worktree
    # databases create-worktree makes.
    conn = await dbrun.connect_admin(os.environ["DATABASE_URL"], "pytest dbrun check")
    try:
        note = await conn.fetchval(
            "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = $1",
            bound_database(),
        )
    finally:
        await conn.close()
    assert note is not None, (
        f"{bound_database()} carries nothing recording that this run built it, so a "
        f"run that found it left behind could not tell it apart from a checkout's own"
    )
    assert note.startswith(dbrun.MARKER)
    assert f"r{dbrun.current_run_id()}" in note
    assert str(REPO_ROOT) in note


# --- the claim --------------------------------------------------------------


@pytest.fixture
async def admin():
    """An administrative connection to the server the suite is bound to."""
    conn = await dbrun.connect_admin(os.environ["DATABASE_URL"], "pytest dbrun tests")
    try:
        yield conn
    finally:
        await conn.close()


async def test_a_second_run_on_one_database_is_refused_and_the_refusal_names_the_holder(admin):
    # The whole point of the issue: the symptom has to stop pointing away from
    # its cause. A collision must not be a scattered set of impossible failures.
    name = f"{SYNTHETIC_BASE}_r1"
    label = "pytest r1 some-other-checkout"
    holder = await dbrun.connect_admin(os.environ["DATABASE_URL"], label)
    try:
        await dbrun.claim(holder, name)
        holder_pid = await holder.fetchval("SELECT pg_backend_pid()")
        with pytest.raises(dbrun.TestDatabaseBusyError) as refusal:
            await dbrun.claim(admin, name)
    finally:
        await holder.close()
    message = str(refusal.value)
    assert name in message
    assert str(holder_pid) in message
    assert label in message
    assert "refusing to run" in message


async def test_the_claim_goes_away_with_the_run_that_held_it(admin):
    # Held by the connection, so a crashed or killed run leaves nothing stale.
    name = f"{SYNTHETIC_BASE}_r2"
    holder = await dbrun.connect_admin(os.environ["DATABASE_URL"], "pytest r2 crashed")
    await dbrun.claim(holder, name)
    await holder.close()
    await dbrun.claim(admin, name)  # would raise if the lock outlived the connection
    await dbrun.release(admin, name)


async def test_a_released_claim_can_be_taken_again(admin):
    name = f"{SYNTHETIC_BASE}_r3"
    await dbrun.claim(admin, name)
    await dbrun.release(admin, name)
    other = await dbrun.connect_admin(os.environ["DATABASE_URL"], "pytest r4 next")
    try:
        await dbrun.claim(other, name)
    finally:
        await other.close()


# --- the reaper -------------------------------------------------------------


async def _make(conn: asyncpg.Connection, name: str, note: str | None) -> None:
    await conn.execute(f"DROP DATABASE IF EXISTS {dbrun.quoted(name)} WITH (FORCE)")
    await conn.execute(f"CREATE DATABASE {dbrun.quoted(name)}")
    if note is not None:
        await conn.execute(f"COMMENT ON DATABASE {dbrun.quoted(name)} IS $c${note}$c$")


async def _exists(conn: asyncpg.Connection, name: str) -> bool:
    return bool(await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name))


@pytest.fixture
async def dead_pid():
    """A pid no process holds, for a run database whose run is over."""
    finished = subprocess.Popen([sys.executable, "-c", ""])
    finished.wait()
    return finished.pid


async def test_the_reaper_drops_the_database_of_a_run_that_is_over(admin, dead_pid):
    name = f"{SYNTHETIC_BASE}_r{dead_pid}"
    await _make(admin, name, f"{dbrun.MARKER} r{dead_pid} in /somewhere")
    try:
        assert name in await dbrun.reap(admin, SYNTHETIC_BASE, keep="")
        assert not await _exists(admin, name)
    finally:
        await dbrun.drop(admin, name)


async def test_the_reaper_leaves_a_live_runs_database_alone(admin):
    # Dropping one out from under a running suite is far worse than leaving one
    # behind, which is the whole shape of this module's caution.
    name = f"{SYNTHETIC_BASE}_r{os.getpid()}"
    await _make(admin, name, f"{dbrun.MARKER} r{os.getpid()} in {REPO_ROOT}")
    try:
        assert await dbrun.reap(admin, SYNTHETIC_BASE, keep="") == []
        assert await _exists(admin, name)
    finally:
        await dbrun.drop(admin, name)


async def test_the_reaper_leaves_the_database_of_the_run_calling_it(admin):
    name = f"{SYNTHETIC_BASE}_r1"
    await _make(admin, name, f"{dbrun.MARKER} r1 in {REPO_ROOT}")
    try:
        assert await dbrun.reap(admin, SYNTHETIC_BASE, keep=name) == []
        assert await _exists(admin, name)
    finally:
        await dbrun.drop(admin, name)


async def test_the_reaper_will_not_drop_a_database_the_suite_did_not_create(admin, dead_pid):
    # create-worktree names a worktree's database after its branch, so a branch
    # called `r123` produces a name shaped exactly like a run database. The
    # comment is what tells them apart, and it decides.
    name = f"{SYNTHETIC_BASE}_r{dead_pid}"
    await _make(admin, name, None)
    try:
        assert await dbrun.reap(admin, SYNTHETIC_BASE, keep="") == []
        assert await _exists(admin, name)
    finally:
        await dbrun.drop(admin, name)


async def test_the_reaper_leaves_another_checkouts_run_databases_alone(admin, dead_pid):
    name = f"{SYNTHETIC_BASE}_other_r{dead_pid}"
    await _make(admin, name, f"{dbrun.MARKER} r{dead_pid} in /somewhere")
    try:
        assert await dbrun.reap(admin, SYNTHETIC_BASE, keep="") == []
        assert await _exists(admin, name)
    finally:
        await dbrun.drop(admin, name)


async def test_the_reaper_will_not_drop_a_database_a_live_run_still_claims(admin, dead_pid):
    # The pid says the run is over, and on this machine it is — but two machines
    # against one Postgres server have separate pid spaces, so the claim is
    # asked as well and it is the one that decides.
    name = f"{SYNTHETIC_BASE}_r{dead_pid}"
    await _make(admin, name, f"{dbrun.MARKER} r{dead_pid} in /elsewhere")
    holder = await dbrun.connect_admin(os.environ["DATABASE_URL"], "pytest on another box")
    try:
        await dbrun.claim(holder, name)
        assert await dbrun.reap(admin, SYNTHETIC_BASE, keep="") == []
        assert await _exists(admin, name)
    finally:
        await holder.close()
        await dbrun.drop(admin, name)


# --- end to end -------------------------------------------------------------


async def _refused(holder_label: str, run_id: str, name: str, *selection: str):
    """Stage a namesake run holding `name`, and return what a fresh run reports."""
    holder = await dbrun.connect_admin(os.environ["DATABASE_URL"], holder_label)
    try:
        await dbrun.claim(holder, name)
        pid = await holder.fetchval("SELECT pg_backend_pid()")
        done = _pytest(
            {"DATABASE_URL": os.environ["DATABASE_URL"], "QB_TEST_RUN_ID": run_id}, *selection
        )
    finally:
        await holder.close()
    return done, pid


PYTEST_USAGE_ERROR = 4
ONE_DB_TEST = "tests/test_board.py::test_health_needs_no_auth"


def _pytest(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=REPO_ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=300,
    )


async def test_a_run_that_cannot_claim_its_database_refuses_and_says_whose_it_is(dead_pid):
    """The acceptance criterion, end to end: a refusal, not impossible failures.

    A run id is pinned so the collision can be staged deterministically. What is
    being checked is the shape of the report — one message, naming the database
    and the run holding it, and no test results at all — because the original
    symptom's whole problem was that it pointed away from its cause.
    """
    run_id = str(dead_pid)
    name = dbrun.run_database_name(bound_database(), run_id)
    label = "pytest r0 the-other-run"
    holder = await dbrun.connect_admin(os.environ["DATABASE_URL"], label)
    try:
        await dbrun.claim(holder, name)
        holder_pid = await holder.fetchval("SELECT pg_backend_pid()")
        # The database exists and holds a row, as the other run's would.
        await _make(holder, name, f"{dbrun.MARKER} r{run_id} in /the/other/checkout")
        witness = await asyncpg.connect(
            make_url(dbrun.admin_dsn(os.environ["DATABASE_URL"]))
            .set(database=name)
            .render_as_string(hide_password=False)
        )
        try:
            await witness.execute("CREATE TABLE kept (id int)")
        finally:
            await witness.close()

        done = _pytest(
            {"DATABASE_URL": os.environ["DATABASE_URL"], "QB_TEST_RUN_ID": run_id}, ONE_DB_TEST
        )
        # The refusal must not take the database down on its way out. A teardown
        # reached past a failed claim would drop exactly what the claim protects.
        survived = await _exists(holder, name)
    finally:
        await dbrun.drop(holder, name)
        await holder.close()

    report = f"{done.stdout}\n{done.stderr}"
    assert done.returncode == PYTEST_USAGE_ERROR, report
    assert "refusing to run" in report
    assert name in report
    assert str(holder_pid) in report
    assert label in report
    assert " passed" not in report and " failed" not in report, report
    assert survived, f"the refused run dropped {name}, which belonged to the run it refused"


async def test_two_concurrent_runs_in_one_checkout_do_not_share_a_database():
    """The incident, in miniature: a targeted suite while another run is going.

    Both are pointed at the same base database — which is exactly what a second
    terminal in one worktree does — and each has to end up somewhere else. Before
    #366 both bound the base itself, and whichever reached the rebuild second
    emptied the other's tables mid-test.
    """
    def run() -> subprocess.CompletedProcess:
        return _pytest({"DATABASE_URL": os.environ["DATABASE_URL"]}, ONE_DB_TEST)

    first, second = await asyncio.gather(asyncio.to_thread(run), asyncio.to_thread(run))
    for done in (first, second):
        assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    databases = {_reported_database(done.stdout) for done in (first, second)}
    assert len(databases) == 2, f"both runs bound {databases} — the second rebuilt the first"
    for name in databases:
        assert dbrun.run_id_of(name, bound_database()) is not None


def _longest_scratch_suffix() -> str:
    """The longest name `tests/test_migration_*.py` derives from the bound database.

    Read off those modules rather than restated here, so a longer suffix added
    later is caught by the budget test above instead of by a run whose database
    silently shared a sibling's past byte 63.
    """
    patterns = (
        r"""_scratch\(["'](\w+)["']\)""",          # _scratch("m0031_fold")
        r"""SCRATCH_SUFFIX = ["']_(\w+)["']""",     # SCRATCH_SUFFIX = "_drift"
        r"""base\.database\}_(\w+)""",             # f"{base.database}_m0025"
    )
    suffixes: set[str] = set()
    for module in sorted(REPO_ROOT.glob("tests/test_migration_*.py")):
        text = module.read_text()
        for pattern in patterns:
            suffixes |= set(re.findall(pattern, text))
    assert suffixes, "no scratch database names found — has the derivation moved?"
    return "_" + max(suffixes, key=len)


async def test_a_run_that_only_builds_scratch_databases_is_refused_as_well(dead_pid):
    """The claim belongs to the run, not to the schema fixture.

    tests/test_migration_* never ask for the schema — they build their own
    databases by suffixing this run's name and `DROP … WITH (FORCE)` them. A run
    collecting only those would otherwise walk through a namesake's databases
    without ever meeting the claim.
    """
    run_id = str(dead_pid)
    name = dbrun.run_database_name(bound_database(), run_id)
    done, pid = await _refused(
        "pytest r0 migration-only", run_id, name,
        "tests/test_migration_drift.py::test_a_fresh_replay_leaves_exactly_one_head",
    )
    report = f"{done.stdout}\n{done.stderr}"
    assert done.returncode == PYTEST_USAGE_ERROR, report
    assert "refusing to run" in report and name in report and str(pid) in report


def _reported_database(output: str) -> str:
    """The database named on the first line of a run, which every run prints."""
    for line in output.splitlines():
        if line.startswith("database: "):
            return line.removeprefix("database: ").split(" ", 1)[0]
    raise AssertionError(f"no run named its database:\n{output}")
