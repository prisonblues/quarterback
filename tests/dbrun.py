"""Every pytest run gets its own database, and claims it before it destroys it.

The suite's session fixture used to rebuild the schema in place, emptying every
table. Two runs pointed at one database therefore deleted each other's rows and
dropped each other's tables mid-test — and the victim did not error out with
anything naming the cause. It reported a scattered handful of impossible failures
instead (`relation "posts" does not exist`, `assert 0 == 1`) in whichever modules
happened to be executing, moving around between runs, so they read as flakiness
or as the branch's fault. Re-running alone made them disappear, which confirms
the wrong conclusion. That was issue #366: a landing agent reported 118
failures on a merged PR, re-ran, got a clean pass, and lost the time to it.

PR #30 gave every *worktree* its own database, which removed the cross-checkout
case. It did not remove two runs inside one worktree, which is what a landing
agent does by default when it runs a targeted suite while a full one is going.

**So the database name is per run, not per checkout.** `<base>_r<pid>`, created
empty at the start of the run and dropped at the end. Two runs never share one,
so neither has to wait for the other — measured on this box, a full suite and a
targeted suite now finish concurrently in the time the full suite takes alone.

It also fixes a second collision for free. `tests/test_migration_*.py` build
their own scratch databases by suffixing `os.environ["DATABASE_URL"]`'s name
(`_m0025`, `_drift`, …) and `DROP … WITH (FORCE)` them, which kills every
connection on them. Those names were per checkout too, so two runs raced there
as well; deriving them from a per-run base makes them per run without those
modules changing. `SCRATCH_HEADROOM` is the room they are left.

**The advisory claim is the backstop, not the mechanism.** Per-run names make
contention impossible through the ordinary path, so what is left is the case
where two runs compute the *same* name: a pid reused after a crash left its
database behind, or two machines against one Postgres server. Both end at a
`DROP DATABASE … WITH (FORCE)` on a database another run is using, which is the
original bug wearing a different hat. A run therefore takes a PostgreSQL
advisory lock on its database name before it touches it, and the reaper takes
one before it drops anything. A run that cannot take the lock refuses, naming
the process that holds it — which is the whole point of the issue: the symptom
had to stop pointing away from its cause.

Nothing here waits. lexray hit this bug first (its #1418) and answered it with a
machine-wide claim so that runs queued; that worked and then stopped their work,
because a claim is held for a whole session and the second run waits out the
first — a 2.6s test took 54.5s, which reads as a hang. They moved to per-run
databases (their `docs/tests.md`, "Every run gets its own database"). quarterback
already had per-worktree databases, so per-run was the smaller step from here,
and it is the one taken.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from pathlib import Path

import asyncpg
from sqlalchemy.engine import make_url

#: PostgreSQL truncates an identifier past this *silently*, so two run databases
#: whose names differ only past byte 63 would collapse into one — the exact
#: collision this module exists to prevent, arriving through the fix. Every name
#: composed here is checked against it rather than trusted to be short. BYTES,
#: not characters: `NAMEDATALEN` counts the encoded form, so a base name with a
#: non-ASCII character in it fits fewer of them than `len()` suggests.
MAX_IDENTIFIER = 63

#: Bytes left free at the end of a run database's name for the suffixes
#: `tests/test_migration_*.py` append to it. Their scratch databases are named
#: from ours, so our budget is not the whole identifier; `_m0031_fold` is the
#: longest of them today and `test_dbrun.py` fails if one outgrows this.
SCRATCH_HEADROOM = 12

#: How much of the base name survives. The remainder is `_r` plus the run id;
#: Linux's `PID_MAX_LIMIT` is 4194304, so seven digits is the most a pid can
#: need and ten is generous. `test_dbrun.py` pins the arithmetic.
BASE_BUDGET = MAX_IDENTIFIER - SCRATCH_HEADROOM - len("_r") - 10

#: Written into the database's comment when it is created, and required before
#: the reaper will drop anything. Name-shape alone is not enough: a worktree on a
#: branch called `r123` would be given the database `quarterback_r123` by
#: create-worktree, which reads as a run database of the main checkout and is
#: not one.
MARKER = "quarterback test run"

#: The maintenance database every administrative connection is made to. It has
#: to be a database other than the one being created or dropped.
MAINTENANCE_DB = "postgres"

#: What follows `<stem>_r` in a run database: the run id, and for the databases
#: the migration suites derive from it, their own suffix as well.
_RUN_TAIL = re.compile(r"(\d+)(?:_[A-Za-z0-9_]+)?")


class TestDatabaseBusyError(RuntimeError):
    """Another live pytest run holds the database this one was about to rebuild."""


def current_run_id(environ: Mapping[str, str] | None = None) -> str:
    """What distinguishes this run's database from a concurrent one's.

    The pid, because the reaper needs to be able to ask whether the run that
    left a database behind is still going, and a pid is the only handle that
    answers that without a registry to keep in step. Under `pytest -n` each
    xdist worker is its own process, so workers get their own databases too.

    `QB_TEST_RUN_ID` overrides it, for a run that wants a database it can name
    in advance. It must still be digits: `is_alive` reads it as a pid, and an id
    that is not one would leave a database nothing ever reaps.
    """
    environ = os.environ if environ is None else environ
    override = (environ.get("QB_TEST_RUN_ID") or "").strip()
    if not override:
        return str(os.getpid())
    if not override.isdigit():
        raise ValueError(f"QB_TEST_RUN_ID must be digits, got {override!r}")
    return override


def quoted(identifier: str) -> str:
    """A PostgreSQL identifier, ready to interpolate into DDL.

    `CREATE DATABASE` and friends take no bind parameters, so every name in this
    module reaches the server inside a string. The names come from a URL a
    checkout supplies and from `pg_database`, neither of which this module gets
    to vet, and a `"` in one would end the quoting and leave the rest of it as
    SQL.
    """
    return '"' + identifier.replace('"', '""') + '"'


def fit(name: str, budget: int) -> str:
    """`name` cut to `budget` bytes, never through the middle of a character."""
    return name.encode()[:budget].decode(errors="ignore")


def run_database_name(base_name: str, run_id: str) -> str:
    """`<base>_r<run id>`, trimmed to fit PostgreSQL's identifier limit.

    The base is what gets trimmed and the run id is kept whole: the run id is
    the part that makes the name unique, and a truncation that ate it would
    hand two concurrent runs one database while looking like it had not.
    """
    if not base_name:
        raise ValueError("cannot derive a run database from a URL that names none")
    if not run_id.isdigit():
        raise ValueError(f"run id must be digits, got {run_id!r}")
    name = f"{fit(base_name, BASE_BUDGET)}_r{run_id}"
    if len(name.encode()) > MAX_IDENTIFIER - SCRATCH_HEADROOM:
        raise ValueError(
            f"run database name {name!r} leaves no room for the migration suites' "
            f"scratch suffixes ({SCRATCH_HEADROOM} bytes of {MAX_IDENTIFIER})"
        )
    return name


def run_scoped_url(url: str, run_id: str) -> str:
    """`url` repointed at this run's own database."""
    base = make_url(url)
    name = run_database_name(base.database or "", run_id)
    return base.set(database=name).render_as_string(hide_password=False)


def admin_dsn(url: str) -> str:
    """`url` as a plain libpq DSN on the maintenance database.

    asyncpg is connected to directly rather than through SQLAlchemy because
    `CREATE DATABASE` and `DROP DATABASE` cannot run inside a transaction, and
    because this connection has to outlive the engine it is protecting.
    """
    return (
        make_url(url)
        .set(drivername="postgresql", database=MAINTENANCE_DB)
        .render_as_string(hide_password=False)
    )


def run_id_of(candidate: str, base_name: str) -> int | None:
    """The run id in `candidate` if it is a run database of `base_name`, else None.

    Anchored on the *exact* stem this checkout composes names from, never on a
    prefix of it: `quarterback` is a prefix of `quarterback_fix_issue_366`, so a
    prefix match would let the main checkout treat a worktree's databases as its
    own to reap. Anchored from the front, too — a search from the right reads
    `<base>_r1_r2` as belonging to `<base>`, when the run that built it is 2.
    """
    stem = f"{fit(base_name, BASE_BUDGET)}_r"
    if not candidate.startswith(stem):
        return None
    tail = _RUN_TAIL.fullmatch(candidate[len(stem) :])
    return int(tail.group(1)) if tail else None


def is_alive(pid: int) -> bool:
    """Whether a process with this pid exists. Fails closed.

    A `PermissionError` means the pid is real and owned by somebody else, which
    is alive. Anything else unexpected is answered "alive" as well, because the
    consequence of a wrong "dead" is dropping a database out from under a
    running suite and the consequence of a wrong "alive" is one database left
    behind until the next run.
    """
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def lock_keys(db_name: str) -> tuple[int, int]:
    """The two int4 keys naming this database's advisory lock.

    The two-argument form of `pg_try_advisory_lock`, because that is the one
    whose keys land in `pg_locks.classid`/`objid` unchanged — which is how the
    refusal below finds out *who* is holding it. Both halves are masked to 31
    bits so they are valid as a signed int4 argument and as the unsigned oid
    `pg_locks` stores them in.
    """
    digest = hashlib.blake2b(db_name.encode(), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return (value >> 32) & 0x7FFFFFFF, value & 0x7FFFFFFF


def claim_label(run_id: str, checkout: Path) -> str:
    """What a run calls itself in `pg_stat_activity`, so a collision can name it.

    Truncated to PostgreSQL's identifier limit, which is what `application_name`
    is stored in — a longer one is silently cut, and cutting it here means the
    part that survives is chosen rather than whatever happened to fit.
    """
    return f"pytest r{run_id} {checkout.name}"[:MAX_IDENTIFIER]


async def connect_admin(url: str, label: str) -> asyncpg.Connection:
    """An administrative connection to the maintenance database, labelled."""
    return await asyncpg.connect(
        admin_dsn(url), server_settings={"application_name": label}
    )


async def claim(conn: asyncpg.Connection, db_name: str) -> None:
    """Take the advisory lock on `db_name`, or raise naming who holds it.

    Held by the connection, so PostgreSQL releases it when the connection goes
    away — a killed or crashed run leaves nothing stale to recover from. It does
    not wait: with per-run names a collision means a pid was reused or two
    machines share this server, and neither resolves by waiting.
    """
    key1, key2 = lock_keys(db_name)
    if await conn.fetchval("SELECT pg_try_advisory_lock($1, $2)", key1, key2):
        return
    raise TestDatabaseBusyError(_busy_message(db_name, await _holder(conn, key1, key2)))


async def release(conn: asyncpg.Connection, db_name: str) -> None:
    key1, key2 = lock_keys(db_name)
    await conn.fetchval("SELECT pg_advisory_unlock($1, $2)", key1, key2)


async def _holder(conn: asyncpg.Connection, key1: int, key2: int) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT a.pid, a.application_name, a.client_addr, a.backend_start
          FROM pg_locks l
          JOIN pg_stat_activity a ON a.pid = l.pid
         WHERE l.locktype = 'advisory'
           AND l.classid = $1 AND l.objid = $2 AND l.objsubid = 2
           AND l.granted
         LIMIT 1
        """,
        key1,
        key2,
    )


def _busy_message(db_name: str, holder: asyncpg.Record | None) -> str:
    if holder is None:
        who = (
            "another connection holds it, and PostgreSQL would not say which — it "
            "may have just gone away"
        )
    else:
        where = f" from {holder['client_addr']}" if holder["client_addr"] else ""
        who = (
            f"held by backend pid {holder['pid']}{where}, "
            f"application_name {holder['application_name']!r}, "
            f"connected since {holder['backend_start']:%Y-%m-%d %H:%M:%S}"
        )
    return (
        f"refusing to run: another test run already owns the database "
        f"'{db_name}', and this run would drop and rebuild it — {who}.\n"
        f"  Every run normally gets a database of its own named after its pid, so "
        f"this means two runs computed one name: a pid reused after a crash left "
        f"its database behind, or two machines sharing this Postgres server.\n"
        f"  Wait for that run to finish, or start this one again — a new pid is a "
        f"new database."
    )


async def build(conn: asyncpg.Connection, db_name: str, note: str) -> None:
    """Create `db_name` empty, replacing any leftover of the same name.

    FORCE, because a crashed run of the same pid may still have a session on it
    and a plain DROP would fail on that rather than on anything real. Safe to
    force here and nowhere else: the caller holds this name's claim, so a *live*
    run cannot be the one being disconnected.

    The comment is what makes the database reapable. It is set after CREATE
    rather than before, so a database that exists without one is a half-built
    leftover and is replaced rather than adopted.
    """
    await conn.execute(f"DROP DATABASE IF EXISTS {quoted(db_name)} WITH (FORCE)")
    await conn.execute(f"CREATE DATABASE {quoted(db_name)}")
    # COMMENT takes no bind parameters either, so the text is dollar-quoted and
    # stripped of the one character that could close the quoting early. It is
    # composed here from a pid and a path, but a comment that ends the statement
    # it is in is not a thing to leave to the caller's discipline.
    await conn.execute(
        f"COMMENT ON DATABASE {quoted(db_name)} IS $comment${note.replace('$', '')}$comment$"
    )


async def drop(conn: asyncpg.Connection, db_name: str) -> None:
    await conn.execute(f"DROP DATABASE IF EXISTS {quoted(db_name)} WITH (FORCE)")


async def reap(conn: asyncpg.Connection, base_name: str, keep: str) -> list[str]:
    """Drop the run databases of this checkout whose run is gone. Returns their names.

    Scoped to this checkout's own stem on purpose — another checkout's leftovers
    are another agent's to reap — and it errs towards leaving a database alone,
    because dropping one out from under a live suite is far worse than leaving
    one behind. Three things have to agree before anything is dropped: the name
    is one this checkout composes, the comment says the suite created it, and
    the advisory claim is free.
    """
    rows = await conn.fetch(
        """
        SELECT d.datname, shobj_description(d.oid, 'pg_database') AS note
          FROM pg_database d
         WHERE NOT d.datistemplate
        """
    )
    dropped: list[str] = []
    for row in rows:
        name = row["datname"]
        if name == keep or not (row["note"] or "").startswith(MARKER):
            continue
        run_id = run_id_of(name, base_name)
        if run_id is None or is_alive(run_id):
            continue
        key1, key2 = lock_keys(name)
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1, $2)", key1, key2):
            continue  # a live run owns it after all; its pid means something else here
        try:
            await drop(conn, name)
            dropped.append(name)
        finally:
            await conn.fetchval("SELECT pg_advisory_unlock($1, $2)", key1, key2)
    return dropped
