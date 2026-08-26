"""Shared fixtures. The database-backed ones need the compose Postgres up:
`docker compose up -d postgres`.

Point the app at that database and configure test tokens *before* importing app
modules (pytest imports conftest before collecting test modules).

The suite builds its schema from scratch, so its target database holds nothing
but what the run puts there. `dbtarget` decides which database a checkout may
build on — an explicit DATABASE_URL, else this checkout's own .env (a worktree's
names its isolated copy), else the dev fallback — and refuses to run when a
worktree would rebuild a database another checkout is using.

`dbrun` then gives *this run* its own database under that name, `<base>_r<pid>`,
created empty and dropped at the end, so two concurrent runs in one checkout
cannot corrupt each other either (#366). Setting the resolved URL back into the
environment keeps the alembic subprocess below — and the scratch databases the
migration suites derive from it — on the same database as the app, whatever the
working directory pytest was invoked from.

Set `QB_TEST_DB_KEEP=1` to leave the run's database behind for inspection. The
next run reaps it once this one's pid is gone.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from . import dbrun
from .dbtarget import ENV_VAR, database_name, endpoint, isolation_error, resolve_database_url

REPO_ROOT = Path(__file__).resolve().parent.parent

_base_url, _source = resolve_database_url(dict(os.environ), REPO_ROOT)
# Asked of the base, not of this run's derived name: the run database is this
# run's alone whatever the base is, so the derived name would never collide and
# the guard would pass vacuously. The question `dbtarget` answers is whether the
# .env is right, and a worktree pointed at the main checkout's database has a
# wrong .env whether or not the suite would now go on to destroy anything.
_problem = isolation_error(_base_url, REPO_ROOT)
_run_id = dbrun.current_run_id()
_url = dbrun.run_scoped_url(_base_url, _run_id)
_db_name = database_name(_url)
#: Leave this run's database behind instead of dropping it, for when the point of
#: the run was to look at what it left. It is still per-run, so nothing reuses
#: it; the next run's reaper collects it once this pid is gone.
_KEEP = os.environ.get("QB_TEST_DB_KEEP", "").strip().lower() not in ("", "0", "false", "no")
os.environ[ENV_VAR] = _url

#: The database is the ONLY setting the suite takes from its surroundings.
#: Everything else the app reads is pinned here, because a checkout's .env is
#: developer convenience and the tests are assertions about behaviour: letting
#: .env through would make the suite mean different things in different
#: checkouts. BROWSER_DEV_USER is the concrete one — .env.example sets it, and
#: with it set `reader` authenticates everybody, so the test asserting that
#: /stream 401s without auth instead receives a live SSE stream and hangs
#: forever on a transport that buffers whole responses.
#:
#: tests/test_settings.py asserts this covers every Settings field except the
#: database, so a field added to app/config.py cannot quietly reopen the leak.
#: HUMAN_EDGE_SECRET is pinned to a known value rather than left empty for the
#: same class of reason: `human()` fails closed without it, so an empty one would
#: make every human-only test assert "403 because nothing is configured" instead
#: of the thing it means to assert. The tests that DO assert the boundary itself
#: send the wrong secret, or none. BROWSER_DEV_HUMAN stays off, because it is the
#: bypass, and a suite that runs with the bypass on tests nothing.
PINNED_SETTINGS = {
    "API_TOKENS": "laptop:tok-laptop,server:tok-server,desktop:tok-desktop",
    "API_TOKENS_FILE": "",
    "BROWSER_DEV_USER": "",
    "BROWSER_DEV_HUMAN": "false",
    "HUMAN_EDGE_SECRET": "tok-edge",
    # EMPTY, and deliberately: the second way to be a person here is a key from
    # `HUMAN_TOKENS`, and a suite that ran with one configured would be a suite in
    # which "unconfigured refuses everything" is arranged rather than free. The
    # tests that need the door open set it for themselves — `test_human_key.py`'s
    # `human_key` fixture — which is the same reason BROWSER_DEV_HUMAN stays off.
    "HUMAN_TOKENS": "",
    "HUMAN_TOKENS_FILE": "",
    "LOG_FILE": "",
}
os.environ.update(PINNED_SETTINGS)

from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402

LAPTOP = {"Authorization": "Bearer tok-laptop"}
SERVER = {"Authorization": "Bearer tok-server"}
DESKTOP = {"Authorization": "Bearer tok-desktop"}


def pytest_configure(config: pytest.Config) -> None:
    # Refusing here rather than at import: a UsageError raised from a hook is
    # printed as the message and nothing else, where the same message raised
    # while conftest is being imported comes out as a traceback with the advice
    # buried among the frames. This still runs before collection, so nothing has
    # touched the database yet.
    if _problem:
        raise pytest.UsageError(_problem)


def pytest_sessionstart(session: pytest.Session) -> None:
    # Deliberately not `pytest_report_header`: that block is suppressed at -q
    # (verbosity < 0), which is how this suite is normally run and how both
    # READMEs document running it — so the one line naming the database about to
    # be destroyed would never be seen by the people it is for. The terminal
    # reporter writes to the real stdout, so this survives capturing at any
    # verbosity. And in sessionstart rather than configure, because the reporter
    # registers itself during configure and is not there yet when a conftest's
    # own configure hook runs.
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:  # pragma: no cover - only with the terminal plugin off
        return
    host, port, _ = endpoint(_url)
    kept = " and KEPT (QB_TEST_DB_KEEP)" if _KEEP else ", dropped when it ends"
    reporter.write_line(
        f"database: {_db_name} on {host}:{port} — built for this run{kept} "
        f"(base {database_name(_base_url)}, from {_source})",
        bold=True,
    )


@pytest.fixture(scope="session")
async def _db_claim():
    """This run's claim on its own database name, held for the whole session.

    **Every fixture that creates or drops a database named after this run depends
    on it** — the schema below, and the scratch databases `tests/test_migration_*`
    build by suffixing this run's name. That breadth is the point: a run that
    cannot take the claim has to be refused before anything is destroyed, not
    only when it happens to want the schema. A run collecting nothing but the
    migration modules never reaches the schema fixture and would otherwise
    `DROP … WITH (FORCE)` its way through a namesake's databases unchallenged.

    The claim lives on this connection, so PostgreSQL releases it when the
    connection goes — a killed run leaves nothing stale to recover from.
    """
    conn = await dbrun.connect_admin(_url, dbrun.claim_label(_run_id, REPO_ROOT))
    # Taken OUTSIDE any block that tears a database down, and that placement is
    # the whole of it: a refusal means the database belongs to somebody else, and
    # a teardown reached on the way out would drop the very database the refusal
    # exists to protect.
    try:
        await dbrun.claim(conn, _db_name)
    except dbrun.TestDatabaseBusyError as exc:
        await conn.close()
        # `pytest.exit` rather than a raise: an exception here fails the session
        # fixture, which reports the same message once per test that wanted a
        # database — a thousand identical errors, which is the scattered-failure
        # shape this whole change exists to replace. Exit prints it once, alone,
        # and stops. 4 is pytest's own USAGE_ERROR.
        pytest.exit(str(exc), returncode=4)
    except BaseException:
        await conn.close()
        raise
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(scope="session")
async def _schema(_db_claim):
    # Build this run's database and its schema (+ trigger) via alembic, so tests
    # exercise the real migrations. Requested by `client` rather than autouse:
    # it needs a running Postgres, and the pure unit tests (tests/test_dbtarget.py
    # and friends) have no business needing one to assert on a regex.
    #
    # No `downgrade base` first any more. That existed to empty a database the
    # last run had left full; this one is created empty a line below, so the
    # downgrade would have nothing to do and the run pays a second migration
    # chain for it. Nothing was asserting on it either — it ran with check=False.
    try:
        await dbrun.reap(_db_claim, database_name(_base_url), keep=_db_name)
        await dbrun.build(_db_claim, _db_name, f"{dbrun.MARKER} r{_run_id} in {REPO_ROOT}")
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
        yield
    finally:
        # The engine before the database: dropping one with live sessions on it
        # needs FORCE, and forcing here would disconnect whatever else on this
        # box happens to be looking at it.
        await engine.dispose()
        if not _KEEP:
            await dbrun.drop(_db_claim, _db_name)


@pytest.fixture
async def client(_schema):
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
