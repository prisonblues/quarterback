"""Which database the suite is allowed to point its destructive rebuild at.

The session fixture rebuilds the schema with ``alembic downgrade base && upgrade
head``. That destroys every row in the target database, so *which* database the
target is has to be decided deliberately rather than inherited by accident.

The accident this exists to prevent: ``create-worktree`` gives a worktree its own
database and writes the name into the worktree's ``.env`` — but pydantic-settings
reads real environment variables in preference to ``.env``, so a conftest that
did ``os.environ.setdefault("DATABASE_URL", <main dev DB>)`` silently overrode
the isolated database and wiped the shared one instead. Isolation that the test
suite ignores is not isolation.

So: resolve the URL here, from an explicit environment variable first and the
checkout's own ``.env`` second, and refuse to run at all when a worktree is about
to rebuild the main checkout's database.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Last-resort target when nothing is configured: the compose Postgres from
#: README's Development section. Only correct in the main checkout — a worktree
#: reaching this fallback is the bug this module reports.
DEV_FALLBACK_URL = "postgresql+asyncpg://quarterback:quarterback@localhost:5435/quarterback"

_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def env_file_value(var: str, env_file: Path) -> str | None:
    """Read ``var`` from a ``.env``-style file, or None. Last assignment wins."""
    try:
        text = env_file.read_text()
    except OSError:
        return None
    found: str | None = None
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if m and m.group(1) == var:
            found = m.group(2).strip("\"'")
    return found or None


def database_name(url: str) -> str:
    """The database a SQLAlchemy/libpq URL points at ('…:5435/foo?x=1' -> 'foo')."""
    return url.rsplit("/", 1)[-1].split("?", 1)[0]


def main_checkout(checkout: Path) -> Path | None:
    """The main repo behind a linked worktree, or None if this *is* the main repo.

    A worktree's ``.git`` is a file holding ``gitdir: <main>/.git/worktrees/<name>``,
    where a main checkout's is a directory — that difference is the whole test.
    """
    dotgit = checkout / ".git"
    if not dotgit.is_file():
        return None
    pointer = dotgit.read_text().strip()
    if not pointer.startswith("gitdir:"):
        return None
    gitdir = Path(pointer.split(":", 1)[1].strip())
    # …/<main>/.git/worktrees/<name> -> <main>
    for parent in gitdir.parents:
        if parent.name == ".git":
            return parent.parent
    return None


def resolve_database_url(environ: dict[str, str], checkout: Path) -> tuple[str, str]:
    """Return (url, where-it-came-from) for the suite's target database.

    Precedence: an explicit ``DATABASE_URL`` in the environment (how CI and a
    one-off ``DATABASE_URL=… pytest`` pin it), then the checkout's ``.env``
    (which is what create-worktree repoints at the worktree's own database),
    then the dev fallback.
    """
    explicit = environ.get("DATABASE_URL")
    if explicit:
        return explicit, "DATABASE_URL"
    from_env_file = env_file_value("DATABASE_URL", checkout / ".env")
    if from_env_file:
        return from_env_file, ".env"
    return DEV_FALLBACK_URL, "fallback"


def isolation_error(url: str, checkout: Path) -> str | None:
    """Why this worktree must not run the suite against ``url``, or None if fine.

    Only linked worktrees are checked: the main checkout rebuilding its own dev
    database is the documented behaviour, while a worktree doing it destroys
    data that belongs to somebody else's checkout.
    """
    main = main_checkout(checkout)
    if main is None:
        return None  # main checkout: its dev database is its own to rebuild
    main_url = env_file_value("DATABASE_URL", main / ".env")
    main_db = database_name(main_url) if main_url else database_name(DEV_FALLBACK_URL)
    if database_name(url) != main_db:
        return None
    return (
        f"refusing to run: this worktree ({checkout}) would rebuild the main "
        f"checkout's database '{main_db}', destroying its data.\n"
        f"  target: {url}\n"
        f"  The worktree's .env should name its own database — create-worktree "
        f"writes one when the repo has a .worktree.json with a `database` block "
        f"and the main checkout has a .env.\n"
        f"  Fix that .env, or pin a scratch database for this run:\n"
        f"    DATABASE_URL=postgresql+asyncpg://quarterback:quarterback@localhost:5435/"
        f"{main_db}_scratch pytest"
    )
