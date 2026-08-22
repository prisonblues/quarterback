"""Alembic environment for quarterback.

Besides running the migrations, this file is where a **new** revision gets its id.
`process_revision_directives` below replaces the id alembic would have picked with an
opaque `m<8 hex>` one, so `alembic revision --autogenerate -m "..."` produces a
hash-named revision with nobody having to remember a flag. That is the whole of #341
on the authoring side: the ids `0001` … `0034` were minted by hand out of a sequence
two branches could both count through, and four of them did on one morning. Nothing
was renamed — a renumber rewrites `revision`, which is what `alembic_version` stores —
so the numbered chain stays exactly as it is and everything above it is opaque.

The hook fires wherever alembic runs this file, which is `revision --autogenerate` and
the programmatic equivalent. Two spellings reach past it, and both are left to ask for
an id rather than being papered over. A bare `alembic revision` (no `--autogenerate`)
skips env.py entirely; `revision_environment = true` would fix that, at the price of a
live database connection for a command that needs none. `alembic merge heads` runs env
under that setting but numbers its revision itself without consulting this hook, so it
would pay the price and still not be fixed — which is why the setting is not on. Both
take `--rev-id "$(scripts/migration_reconcile.py new-id)"`, which is exactly what the
reconciler prints when it recommends a merge, and `tests/test_migration_ids.py` refuses
an id that came from neither.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
from app.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

#: The one implementation of the id scheme, so this hook and the reconciler that reads
#: the resulting graph cannot drift apart. `scripts/` is a directory of standalone
#: tools rather than an importable package, and `prepend_sys_path` is resolved against
#: the working directory alembic was invoked from, so it is loaded by absolute path —
#: the same way `tests/test_migration_reconcile.py` loads it.
_RECONCILER = Path(__file__).resolve().parent.parent / "scripts" / "migration_reconcile.py"


def _adopt_revision_id(chosen: str | None, *, explicit: bool) -> str:
    """`migration_reconcile.adopt_revision_id`, loaded by path.

    The decision itself lives there, with the other revision-id shapes and with the
    reconciler that has to read the resulting graph, so the two cannot drift apart. It
    is loaded rather than imported because `scripts/` is a directory of standalone
    tools rather than a package, and `prepend_sys_path` resolves against whatever
    directory alembic was invoked from.
    """
    spec = importlib.util.spec_from_file_location("migration_reconcile", _RECONCILER)
    if spec is None or spec.loader is None:  # pragma: no cover - a missing file
        raise RuntimeError(f"cannot load the revision-id scheme from {_RECONCILER}")
    module = importlib.util.module_from_spec(spec)
    # Registered before it executes: @dataclass resolves annotations through
    # sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return str(module.adopt_revision_id(chosen, explicit=explicit))


def process_revision_directives(context_, revision, directives) -> None:
    """Give every generated revision an opaque id (#341).

    Called by alembic for `revision` / `--autogenerate`; `directives` is empty when
    autogenerate found no changes, and alembic then writes nothing.

    `cmd_opts.rev_id` is the parsed `--rev-id`, so the decision reads the flag itself
    rather than guessing from the id's shape — an explicit id that happens to look like
    one alembic generated is still the caller's. It is absent (`cmd_opts` is None) when
    alembic is driven programmatically rather than from its CLI.
    """
    explicit = bool(getattr(config.cmd_opts, "rev_id", None))
    for directive in directives:
        directive.rev_id = _adopt_revision_id(directive.rev_id, explicit=explicit)


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        process_revision_directives=process_revision_directives,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported for quarterback")
else:
    run_migrations_online()
