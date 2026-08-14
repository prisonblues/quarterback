"""TEMPLATE — make a pytest suite honour the worktree's database.

Drop this into your `tests/` (or paste it into `conftest.py`) when your suite
does anything destructive to its target database: creating and dropping the
schema, `alembic downgrade base`, `manage.py flush`, `create_all/drop_all`,
truncating between tests.

## The trap it exists for

`create-worktree` gives a worktree its own copy of the database and writes the
name into the worktree's `.env`. That does nothing for a test suite that decides
its own URL, and suites usually do — most commonly:

    os.environ.setdefault("DATABASE_URL", "postgresql://…/myapp")   # <-- the bug

Two ways that bites. Config libraries that read `.env` (pydantic-settings,
python-dotenv with `override=False`, Django's environ helpers) treat a real
environment variable as higher priority than the file — so the `setdefault`
above *wins over* the worktree's `.env` and the suite rebuilds the main
database. And with no `.env` support at all, the hardcoded default is the main
database by construction. Either way the isolated copy sits unused while the
shared one is destroyed, and nothing in the output says so.

Three rules fix it:

1. Resolve the URL in one place, with an explicit environment variable first
   (so CI and `DATABASE_URL=… pytest` still pin it), then the checkout's own
   `.env`, then a fallback — never the fallback first.
2. Assign the resolved value back into the environment, so subprocesses
   (alembic, manage.py) target the same database as the in-process app whatever
   directory pytest was run from.
3. Refuse to run when a *worktree* would rebuild the *main* checkout's database.
   That is never intentional, and it is the one mistake with unrecoverable
   consequences.

A fourth rule, learned the hard way while writing this: take *only* the database
target from the environment, and pin every other setting your app reads. Once
your suite honours `.env` it honours all of it, and a `.env` is developer
convenience — dev auth bypasses, debug flags, log paths. In quarterback's case
`.env.example` sets a browser dev-user that authenticates every request, which
turned the test asserting an endpoint returns 401 into one that opened a live
event stream and hung until killed. Pin them:

    os.environ["API_TOKENS"] = "…"      # what the assertions expect
    os.environ["BROWSER_DEV_USER"] = ""  # no auth bypass during tests

Adapt: change DEV_FALLBACK_URL and the variable name if yours isn't
DATABASE_URL. If your suite writes to more than one store, apply the same three
rules to each (a shared Redis or Elasticsearch index is the usual second one).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

# Last-resort target when nothing is configured. Correct only in the main
# checkout — a worktree that reaches this fallback is misconfigured, and the
# guard below reports it rather than letting it run.
DEV_FALLBACK_URL = "postgresql://myapp:myapp@localhost:5432/myapp"

ENV_VAR = "DATABASE_URL"

_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def env_file_value(var: str, env_file: Path) -> str | None:
    """Read `var` from a .env-style file, or None. Last assignment wins."""
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
    """The database a connection URL points at ('…:5432/foo?ssl=1' -> 'foo')."""
    return url.rsplit("/", 1)[-1].split("?", 1)[0]


def endpoint(url: str) -> tuple[str | None, int | None, str]:
    """(host, port, database) — what actually identifies one database.

    Credentials excluded on purpose: two URLs differing only in user or password
    address the same data, while comparing the name alone would refuse a
    genuinely separate database that happens to share a name on another host.
    """
    parts = urlsplit(url)
    return parts.hostname, parts.port, database_name(url)


def redact(url: str) -> str:
    """The URL with any password starred out, for printing."""
    return re.sub(r"(://[^:/@]+):[^@]*@", r"\1:***@", url)


def main_checkout(checkout: Path) -> Path | None:
    """The main repo behind a linked worktree, or None if this *is* the main repo.

    A worktree's `.git` is a file holding `gitdir: <main>/.git/worktrees/<name>`,
    where a main checkout's is a directory — that difference is the whole test.
    """
    dotgit = checkout / ".git"
    if not dotgit.is_file():
        return None
    pointer = dotgit.read_text().strip()
    if not pointer.startswith("gitdir:"):
        return None
    gitdir = Path(pointer.split(":", 1)[1].strip())
    for parent in gitdir.parents:
        if parent.name == ".git":
            return parent.parent
    return None


def resolve_database_url(environ: dict[str, str], checkout: Path) -> tuple[str, str]:
    """Return (url, where-it-came-from): environment, then .env, then fallback."""
    explicit = environ.get(ENV_VAR)
    if explicit:
        return explicit, ENV_VAR
    from_file = env_file_value(ENV_VAR, checkout / ".env")
    if from_file:
        return from_file, ".env"
    return DEV_FALLBACK_URL, "fallback"


def isolation_error(url: str, checkout: Path) -> str | None:
    """Why this worktree must not run against `url`, or None if it's fine."""
    main = main_checkout(checkout)
    if main is None:
        return None  # main checkout: its dev database is its own to rebuild
    main_url = env_file_value(ENV_VAR, main / ".env") or DEV_FALLBACK_URL
    if endpoint(url) != endpoint(main_url):
        return None
    main_db = database_name(main_url)
    # Derived from the main URL so the hint fits this server, and redacted
    # because refusal messages land in terminal scrollback and CI logs.
    scratch = f"{redact(main_url).rsplit('/', 1)[0]}/{main_db}_scratch"
    return (
        f"refusing to run: this worktree ({checkout}) would rebuild the main "
        f"checkout's database '{main_db}', destroying its data.\n"
        f"  target: {redact(url)}\n"
        f"  Expected the worktree's .env to name its own database. Check that the "
        f"repo has a .worktree.json with a `database` block and that the main "
        f"checkout has a .env, then re-provision — or pin a scratch database "
        f"(with the real password):\n"
        f"    {ENV_VAR}={scratch} pytest"
    )


# --- wire it up (this part goes in conftest.py, before importing app code) ---

REPO_ROOT = Path(__file__).resolve().parent.parent

_url, _source = resolve_database_url(dict(os.environ), REPO_ROOT)
_problem = isolation_error(_url, REPO_ROOT)
if _problem:
    raise RuntimeError(_problem)
os.environ[ENV_VAR] = _url


def pytest_report_header() -> str:
    # State the database about to be rebuilt before rebuilding it, so a wrong
    # target is something you read at the top of the run, not deduce afterwards.
    return f"database: {database_name(_url)} (from {_source})"
