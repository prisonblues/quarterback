"""TEMPLATE — make a pytest suite honour the worktree's database.

Copy this file into your ``tests/`` as ``dbtarget.py``, change the two constants
below, and wire it into ``conftest.py`` with the three lines at the end of this
docstring. Do that whenever your suite does anything destructive to its target
database: creating and dropping the schema, ``alembic downgrade base``,
``manage.py flush``, ``create_all``/``drop_all``, truncating between tests.

## The trap it exists for

``create-worktree`` gives a worktree its own copy of the database and writes the
name into the worktree's ``.env``. That does nothing for a test suite that
decides its own URL, and suites usually do — most commonly::

    os.environ.setdefault("DATABASE_URL", "postgresql://…/myapp")   # <-- the bug

Two ways that bites. Config libraries that read ``.env`` (pydantic-settings,
python-dotenv with ``override=False``, Django's environ helpers) treat a real
environment variable as higher priority than the file — so the ``setdefault``
above *wins over* the worktree's ``.env`` and the suite rebuilds the main
database. And with no ``.env`` support at all, the hardcoded default is the main
database by construction. Either way the isolated copy sits unused while the
shared one is destroyed, and nothing in the output says so.

Three rules fix it:

1. Resolve the URL in one place, with an explicit environment variable first
   (so CI and ``DATABASE_URL=… pytest`` still pin it), then the checkout's own
   ``.env``, then a fallback — never the fallback first.
2. Assign the resolved value back into the environment, so subprocesses
   (alembic, manage.py) target the same database as the in-process app whatever
   directory pytest was run from.
3. Refuse to run when a *worktree* would rebuild a database another checkout is
   using. That is never intentional, and it is the one mistake with
   unrecoverable consequences.

A fourth rule, learned the hard way while writing this: take *only* the database
target from the environment, and pin every other setting your app reads. Once
your suite honours ``.env`` it honours all of it, and a ``.env`` is developer
convenience — dev auth bypasses, debug flags, log paths. In quarterback's case
``.env.example`` sets a browser dev-user that authenticates every request, which
turned the test asserting an endpoint returns 401 into one that opened a live
event stream and hung until killed.

## Wiring it up

In ``tests/conftest.py``, above every import of your application::

    import os
    from pathlib import Path
    import pytest
    from .dbtarget import ENV_VAR, database_name, isolation_error, resolve_database_url

    _url, _source = resolve_database_url(dict(os.environ), Path(__file__).resolve().parent.parent)
    if problem := isolation_error(_url, Path(__file__).resolve().parent.parent):
        pytest.exit(problem, returncode=pytest.ExitCode.USAGE_ERROR)
    os.environ[ENV_VAR] = _url
    os.environ["BROWSER_DEV_USER"] = ""   # …and every other setting your app reads

    def pytest_configure(config):
        # Not pytest_report_header: that block is suppressed at -q, and the
        # database about to be destroyed should not hide behind a verbosity flag.
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"database: {database_name(_url)} (from {_source})", bold=True)

Make the destructive fixture depend on the client/session fixture rather than
autouse, so the suite's own pure unit tests still run without a database.

Adapt: ``DEV_FALLBACK_URL`` and ``ENV_VAR`` below. If your suite writes to more
than one store, apply the same three rules to each (a shared Redis or
Elasticsearch index is the usual second one).
"""

from __future__ import annotations

import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

try:  # ships with pydantic-settings; the hand parser below covers its absence
    from dotenv import dotenv_values
except ImportError:  # pragma: no cover - only when python-dotenv is absent
    dotenv_values = None

#: ADAPT ME — the last-resort target when nothing is configured. Correct only in
#: the main checkout: a worktree that reaches this fallback is misconfigured, and
#: the guard below reports that rather than letting it run.
DEV_FALLBACK_URL = "postgresql://myapp:myapp@localhost:5432/myapp"

#: ADAPT ME — the variable that names the database, in the environment and in
#: `.env`. DATABASE_URL for most stacks; DJANGO_DATABASE_URL, MYAPP_DSN, … else.
ENV_VAR = "DATABASE_URL"

# --- shared guard: kept byte-identical by tests/test_dbtarget.py ------------
#: Every spelling of "this machine" collapses to one token. 127.0.0.1, ::1 and
#: the box's own hostname all name the server that `localhost` names, so
#: comparing hostnames as text is a hole: a worktree pointed at the main
#: checkout's database through a different alias would be waved through.
LOCAL_HOSTS = frozenset(
    {"", "localhost", "localhost.localdomain", "127.0.0.1", "::1", "0.0.0.0"}
    | {socket.gethostname().lower(), socket.gethostname().lower().split(".", 1)[0]}
)

#: libpq's default, applied when a URL omits the port so that
#: `postgresql://host/db` and `postgresql://host:5432/db` compare as one.
DEFAULT_PORT = 5432

#: Query parameters whose value is a secret, starred out by `redact`.
SECRET_QUERY_KEYS = ("password", "passwd", "pwd", "secret", "token", "key")

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_INLINE_COMMENT = re.compile(r"\s+#")


def env_file_value(var: str, env_file: Path) -> str | None:
    """Read `var` from a `.env` file the way the application's own loader will.

    python-dotenv when it is importable, because that is what pydantic-settings
    and django-environ parse `.env` with — asking the same library is the only
    way this module and the app cannot disagree about which database is
    configured, and a disagreement here is the accident the guard exists to
    stop. The hand parser is the standalone fallback: `export` prefixes, matched
    quotes, unquoted trailing comments, last assignment wins.
    """
    if not env_file.is_file():
        return None
    if dotenv_values is not None:
        try:
            return dotenv_values(env_file).get(var) or None
        except OSError:
            return None
    try:
        text = env_file.read_text()
    except OSError:
        return None
    found: str | None = None
    for line in text.splitlines():
        match = _ENV_LINE.match(line)
        if match and match.group(1) == var:
            found = _env_value(match.group(2))
    return found or None


def _env_value(raw: str) -> str:
    """One `.env` right-hand side: quotes stripped in pairs, comments dropped.

    Only a *matching* pair of quotes is removed. Stripping quote characters off
    each end independently accepts `"value'` and silently truncates a value that
    legitimately ends in one.
    """
    for mark in ('"', "'"):
        if len(raw) >= 2 and raw.startswith(mark) and raw.endswith(mark):
            return raw[1:-1]
    return _INLINE_COMMENT.split(raw, maxsplit=1)[0].strip()


def database_name(url: str) -> str:
    """The database a connection URL names, or "" when it names none.

    Parsed as a URL path rather than split on the last `/`: that shortcut
    returns the *host* for `postgresql://localhost`, which then compares equal
    to any other URL whose database happens to share the host's name. Percent
    escapes are decoded, so `/my%20db` and `/my db` are one database.
    """
    return unquote(urlsplit(url).path.lstrip("/"))


def endpoint(url: str) -> tuple[str, int | None, str]:
    """(host, port, database) — what identifies one database, normalised.

    Credentials are excluded deliberately: two URLs differing only in user or
    password address the same rows. Host aliases are collapsed and an omitted
    port is filled in, because equality here decides whether a destructive run
    is allowed. A port that will not parse comes back as None, which
    `same_database` reads as "unknown" rather than as a difference.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        port = parts.port
    except ValueError:
        port = None
    else:
        port = DEFAULT_PORT if port is None else port
    return ("localhost" if host in LOCAL_HOSTS else host), port, database_name(url)


def same_database(one: str, other: str) -> bool:
    """Whether two URLs may name the same database. Fails closed.

    This answer gates a refusal that prevents unrecoverable data loss, so "can't
    tell" has to mean "yes, assume they collide": a URL with no database name, a
    port that will not parse. Only a difference the comparison is *certain*
    about — a different database name, host or port — returns False and lets the
    run proceed.

    Known limit: two different hostnames that resolve to one server (a compose
    service name and the host's published port, say) compare as different. No
    name resolution happens here — a guard that runs at pytest start must not
    depend on DNS being up or fast — and both sides of the comparison come from
    checkouts on the same machine, so they are written the same way in practice.
    """
    host_a, port_a, db_a = endpoint(one)
    host_b, port_b, db_b = endpoint(other)
    if not db_a or not db_b:
        return True
    if db_a != db_b:
        return False
    if port_a is None or port_b is None:
        return True
    if port_a != port_b:
        return False
    return host_a == host_b


def redact(url: str) -> str:
    """The URL with its secrets starred out, for printing.

    Both spellings: the password in the userinfo (`user:pw@host`) and one passed
    as a query parameter (`?password=…`). A refusal message is read off a
    terminal and kept in CI logs, so anything this cannot parse is reduced to a
    placeholder rather than printed on the assumption that it is clean.
    """
    try:
        parts = urlsplit(url)
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, _, host = netloc.rpartition("@")
            user = userinfo.split(":", 1)[0]
            netloc = f"{user}:***@{host}" if ":" in userinfo else f"{user}@{host}"
        pairs = [
            (key, "***" if _is_secret(key) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        # quote_via/safe so the stars stay stars: the default quote_plus would
        # print `%2A%2A%2A`, and this string is meant to be read and re-pasted.
        query = urlencode(pairs, quote_via=quote, safe="*")
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    except ValueError:
        return "<unprintable URL>"


def _is_secret(query_key: str) -> bool:
    lowered = query_key.lower()
    return any(secret in lowered for secret in SECRET_QUERY_KEYS)


@dataclass(frozen=True)
class RepoLayout:
    """Where a checkout sits in its repository.

    `linked` is None when that could not be established, which is a refusal and
    not a shrug — see `isolation_error`.
    """

    linked: bool | None
    main: Path | None = None
    others: tuple[Path, ...] = ()


def repo_layout(checkout: Path) -> RepoLayout:
    """Ask git whether `checkout` is a linked worktree, and what its siblings are.

    Git is asked rather than the `.git` pointer file parsed, because that file
    is not a stable thing to read: its path is relative in some setups, and a
    bare main clone puts the administrative directory under `repo.git/worktrees/`
    with no `.git` component anywhere for a scan to find. Parsing it is the
    fallback for when git cannot answer at all, and there an unreadable pointer
    resolves to "a worktree whose owner is unknown" — the fail-closed answer.
    """
    out = _git(checkout, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    lines = (out or "").splitlines()
    if len(lines) != 2:
        return _layout_from_pointer(checkout)
    own, common = Path(lines[0].strip()), Path(lines[1].strip())
    if own == common:
        return RepoLayout(linked=False)
    others = _other_checkouts(checkout)
    if others is None:
        # Git named the shared directory but would not list the working trees.
        # Reading that as "no siblings to protect" is the fail-open answer, so
        # fall back to the pointer file, which at worst refuses.
        return _layout_from_pointer(checkout)
    # The main checkout is whatever directory holds the shared `.git`. A bare
    # main clone holds it in `repo.git` and has no working tree of its own, so
    # there is no main checkout to protect — only the sibling worktrees.
    main = _resolved(common.parent) if common.name == ".git" else None
    return RepoLayout(linked=True, main=main, others=others)


def _git(checkout: Path, *args: str) -> str | None:
    """Run git in `checkout` and return its stdout, or None if it cannot answer."""
    try:
        done = subprocess.run(
            ["git", "-C", str(checkout), *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _other_checkouts(checkout: Path) -> tuple[Path, ...] | None:
    """Every working tree of this repository except `checkout`; None if git won't say.

    Siblings count, not just the main checkout: a `.env` copied from one
    worktree to another — or a rebuilt worktree reusing a branch name — points
    the suite at a sibling's database, whose data is exactly as unrecoverable.
    An empty tuple is a real answer (a bare clone with one worktree has no
    siblings); None means the question went unanswered.
    """
    out = _git(checkout, "worktree", "list", "--porcelain")
    if out is None:
        return None
    here = _resolved(checkout)
    trees: list[Path] = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            trees.append(_resolved(Path(line.removeprefix("worktree "))))
        elif line.strip() == "bare" and trees:
            trees.pop()  # a bare repository has no working tree and no .env
    return tuple(tree for tree in trees if tree != here)


def _layout_from_pointer(checkout: Path) -> RepoLayout:
    """The git-less reading of `checkout/.git`, for when git could not answer."""
    dotgit = checkout / ".git"
    if dotgit.is_dir():
        return RepoLayout(linked=False)  # a main checkout keeps a real directory
    if not dotgit.exists():
        return RepoLayout(linked=False)  # not a checkout at all: nothing to isolate from
    try:
        pointer = dotgit.read_text().strip()
    except OSError:
        return RepoLayout(linked=None)
    if not pointer.startswith("gitdir:"):
        return RepoLayout(linked=None)
    raw = Path(pointer.split(":", 1)[1].strip())
    # A relative pointer resolves against the directory holding the .git file,
    # never against whatever directory pytest happened to be invoked from.
    gitdir = _resolved(raw if raw.is_absolute() else checkout / raw)
    for parent in gitdir.parents:
        if parent.name == ".git":
            main = parent.parent
            return RepoLayout(linked=True, main=main, others=(main,))
    return RepoLayout(linked=None)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - a broken symlink in the path
        return path.absolute()


def claimed_url(checkout: Path, is_main: bool) -> str | None:
    """The database another checkout is using, as far as this one can tell.

    Its `.env` if it has one. Failing that only the main checkout claims the
    fallback, because that is where an unconfigured run lands. A sibling
    worktree without a `.env` claims nothing — it would refuse to run itself.
    """
    from_file = env_file_value(ENV_VAR, checkout / ".env")
    if from_file:
        return from_file
    return DEV_FALLBACK_URL if is_main else None


def resolve_database_url(environ: dict[str, str], checkout: Path) -> tuple[str, str]:
    """Return (url, where-it-came-from) for the suite's target database.

    Precedence: an explicit variable in the environment (how CI and a one-off
    `DATABASE_URL=… pytest` pin it), then the checkout's own `.env` (which is
    what create-worktree repoints at the worktree's database), then the
    fallback. Never the fallback first — that is the bug.
    """
    explicit = environ.get(ENV_VAR)
    if explicit:
        return explicit, ENV_VAR
    from_env_file = env_file_value(ENV_VAR, checkout / ".env")
    if from_env_file:
        return from_env_file, ".env"
    return DEV_FALLBACK_URL, "fallback"


def isolation_error(url: str, checkout: Path) -> str | None:
    """Why this checkout must not rebuild `url`, or None if it may.

    Only linked worktrees are checked: a main checkout rebuilding its own dev
    database is the documented behaviour, while a worktree doing it destroys
    data belonging to a checkout that is not looking.
    """
    layout = repo_layout(checkout)
    if layout.linked is False:
        return None
    if layout.linked is None:
        return _unknown_owner_refusal(url, checkout)
    for other in layout.others:
        other_url = claimed_url(other, is_main=other == layout.main)
        if other_url and same_database(url, other_url):
            return _collision_refusal(url, checkout, other, other_url, other == layout.main)
    return None


def _collision_refusal(url: str, checkout: Path, owner: Path, owner_url: str, is_main: bool) -> str:
    whose = "the main checkout" if is_main else "a sibling worktree"
    return (
        f"refusing to run: this worktree ({checkout}) would rebuild the "
        f"database '{database_name(owner_url)}', which belongs to {whose} "
        f"({owner}) — destroying its data.\n"
        f"  target: {redact(url)}\n"
        f"  This worktree's .env should name its own database. create-worktree "
        f"writes one when the repo has a .worktree.json with a `database` block "
        f"and the main checkout has a .env.\n"
        f"  Fix that .env, or pin a scratch database for this run (with the real "
        f"password put back):\n"
        f"    {ENV_VAR}={_scratch_hint(owner_url)} pytest"
    )


def _unknown_owner_refusal(url: str, checkout: Path) -> str:
    return (
        f"refusing to run: cannot tell which checkout owns the target database. "
        f"This checkout ({checkout}) has a .git pointer file, so it is a linked "
        f"worktree, but neither git nor the pointer says which repository it "
        f"belongs to — so whether this run would destroy another checkout's data "
        f"is unknown, and unknown is not a yes.\n"
        f"  target: {redact(url)}\n"
        f"  Check `git -C {checkout} rev-parse --git-common-dir`, or pin a "
        f"scratch database for this run (with the real password put back):\n"
        f"    {ENV_VAR}={_scratch_hint(url)} pytest"
    )


def _scratch_hint(url: str) -> str:
    """A pin-this-instead URL on the same server as `url`, redacted."""
    parts = urlsplit(redact(url))
    name = f"{database_name(url) or 'db'}_scratch"
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))
# --- end shared guard -------------------------------------------------------
