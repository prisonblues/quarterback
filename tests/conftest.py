"""Shared fixtures. Requires the compose Postgres up: `docker compose up -d postgres`.

Point the app at that database and configure test tokens *before* importing app
modules (pytest imports conftest before collecting test modules).

The suite rebuilds the schema from scratch, so the target database loses every
row. `dbtarget` decides which database that may be — an explicit DATABASE_URL,
else this checkout's own .env (a worktree's names its isolated copy), else the
dev fallback — and refuses to run when a worktree would rebuild the main
checkout's database. Setting the resolved URL back into the environment keeps
the alembic subprocess below on the same database as the app, whatever the
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

from .dbtarget import database_name, isolation_error, resolve_database_url

REPO_ROOT = Path(__file__).resolve().parent.parent

_url, _source = resolve_database_url(dict(os.environ), REPO_ROOT)
_problem = isolation_error(_url, REPO_ROOT)
if _problem:
    raise RuntimeError(_problem)
os.environ["DATABASE_URL"] = _url

# Tokens are test fixtures, not deployment config: these names are what the
# assertions below expect, so they take precedence over any .env the checkout has.
os.environ["API_TOKENS"] = "laptop:tok-laptop,server:tok-server,desktop:tok-desktop"

from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402

LAPTOP = {"Authorization": "Bearer tok-laptop"}
SERVER = {"Authorization": "Bearer tok-server"}
DESKTOP = {"Authorization": "Bearer tok-desktop"}


def pytest_report_header() -> str:
    # In the run header, so the database about to be rebuilt is stated before
    # it is rebuilt rather than discovered afterwards.
    return f"database: {database_name(_url)} (from {_source})"


@pytest.fixture(scope="session", autouse=True)
async def _schema():
    # Rebuild the schema (+ trigger) via alembic so tests exercise the real migrations.
    alembic = [sys.executable, "-m", "alembic"]
    subprocess.run([*alembic, "downgrade", "base"], check=False)
    subprocess.run([*alembic, "upgrade", "head"], check=True)
    yield
    await engine.dispose()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
