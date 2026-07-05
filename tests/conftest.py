"""Shared fixtures. Requires the compose Postgres up: `docker compose up -d postgres`.

Point the app at that database and configure test tokens *before* importing app
modules (pytest imports conftest before collecting test modules).
"""

from __future__ import annotations

import os
import subprocess
import sys

import httpx
import pytest
from httpx import ASGITransport

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://quarterback:quarterback@localhost:5435/quarterback",
)
os.environ.setdefault("API_TOKENS", "laptop:tok-laptop,zeus:tok-zeus,desktop:tok-desktop")

from app.db import engine
from app.main import app

LAPTOP = {"Authorization": "Bearer tok-laptop"}
ZEUS = {"Authorization": "Bearer tok-zeus"}
DESKTOP = {"Authorization": "Bearer tok-desktop"}


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
