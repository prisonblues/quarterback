"""Shared fixtures. The database-backed ones need the compose Postgres up:
`docker compose up -d postgres`.

Point the app at that database and configure test tokens *before* importing app
modules (pytest imports conftest before collecting test modules).

The suite rebuilds the schema from scratch, so the target database loses every
row. `dbtarget` decides which database that may be — an explicit DATABASE_URL,
else this checkout's own .env (a worktree's names its isolated copy), else the
dev fallback — and refuses to run when a worktree would rebuild a database
another checkout is using. Setting the resolved URL back into the environment
keeps the alembic subprocess below on the same database as the app, whatever the
working directory pytest was invoked from.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from .dbtarget import ENV_VAR, endpoint, isolation_error, resolve_database_url

REPO_ROOT = Path(__file__).resolve().parent.parent

_url, _source = resolve_database_url(dict(os.environ), REPO_ROOT)
_problem = isolation_error(_url, REPO_ROOT)
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
PINNED_SETTINGS = {
    "API_TOKENS": "laptop:tok-laptop,server:tok-server,desktop:tok-desktop",
    "API_TOKENS_FILE": "",
    "BROWSER_DEV_USER": "",
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
    host, port, name = endpoint(_url)
    reporter.write_line(
        f"database: {name} on {host}:{port} (from {_source}) — the db-backed tests rebuild it",
        bold=True,
    )


@pytest.fixture(scope="session")
async def _schema():
    # Rebuild the schema (+ trigger) via alembic so tests exercise the real migrations.
    # Requested by `client` rather than autouse: it drops and recreates every
    # table, and the pure unit tests (tests/test_dbtarget.py and friends) have no
    # business needing a running Postgres to assert on a regex.
    alembic = [sys.executable, "-m", "alembic"]
    subprocess.run([*alembic, "downgrade", "base"], check=False)
    subprocess.run([*alembic, "upgrade", "head"], check=True)
    yield
    await engine.dispose()


@pytest.fixture
async def client(_schema):
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
