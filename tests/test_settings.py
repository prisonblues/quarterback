"""The suite's configuration is pinned, and the fallbacks all name one database.

Two invariants that used to be comments. Neither needs Postgres: they read
`Settings`' declared fields and the checked-in `.env.example`, nothing more.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import TypeAdapter

from app.config import Settings, settings

from . import dbtarget
from .conftest import PINNED_SETTINGS

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_every_setting_but_the_database_is_pinned_by_conftest():
    # conftest takes the database from the environment and pins the rest, so the
    # suite means the same thing in every checkout. That was four names in a
    # comment and nothing enforcing it: a fifth field added to app/config.py
    # would have quietly started reading a developer's .env — which is exactly
    # how BROWSER_DEV_USER once turned a 401 assertion into a hung SSE stream.
    # Adding a field here is deliberate work: pin it, or justify not pinning it.
    declared = {name.upper() for name in Settings.model_fields}
    assert declared - {"DATABASE_URL"} == set(PINNED_SETTINGS)


def test_the_pinned_values_are_what_the_app_actually_reads():
    # Compared through the field's own type rather than as strings: the
    # environment only carries text, and a pinned "false" that the app reads as
    # the boolean False is the pin working, not the pin failing.
    for name, value in PINNED_SETTINGS.items():
        field = Settings.model_fields[name.lower()]
        expected = TypeAdapter(field.annotation).validate_python(value)
        assert getattr(settings, name.lower()) == expected


def test_the_suites_fallback_the_apps_default_and_env_example_name_one_database():
    # Three places answer "which database is the dev database" and they must not
    # disagree: dbtarget's fallback decides what a worktree is refused for,
    # app/config.py's default is what a checkout with no .env connects to, and
    # .env.example is what `cp .env.example .env` installs. When config.py still
    # said 5432 while the other two said 5435, skipping the copy step connected
    # you to whatever unrelated Postgres owned the standard port.
    app_default = Settings.model_fields["database_url"].default
    example = re.search(r"^DATABASE_URL=(.+)$", (REPO_ROOT / ".env.example").read_text(), re.M)
    assert example is not None
    assert dbtarget.DEV_FALLBACK_URL == app_default == example.group(1).strip()
