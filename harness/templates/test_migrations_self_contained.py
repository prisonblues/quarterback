"""Guard: an Alembic migration must not import live application code.

Drop this file into your repo's ``tests/`` unchanged, then adjust the four
constants below it — ``VERSIONS_DIR`` if alembic keeps its revisions somewhere
other than ``migrations/versions``, ``MIN_EXPECTED_MIGRATIONS`` to a floor under
however many you have, ``POLICY_REFERENCE`` to wherever you write the rule down.
``EXEMPT_MODULES`` starts empty and should stay that way. Nothing else needs
editing: the scan needs no database, no app import and no fixtures, so it runs
in whatever your fast suite is.

**A migration is a frozen artefact; live app code is not.** A migration runs at
a fixed point in schema history, but anything imported from your application
package is whatever that package says *today*. The sharpest form is an ORM
model: an ORM SELECT or INSERT names *every* mapped column, so the day a later
migration adds a column, the older migration starts emitting SQL for a column
that does not exist yet at that point in the chain — ``UndefinedColumn``, and
the whole replay aborts.

And it hides. It is invisible on any database already past that revision,
because applied revisions never re-run. It detonates on a **fresh-database
replay**: a migration-drift test, a suite that rebuilds its schema with
``downgrade base && upgrade head``, provisioning a new worktree or environment,
a disaster-recovery rebuild, or any instance still behind that revision when the
new-column code deploys.

Adopt it early. The guard costs nothing on a repo whose migrations are already
self-contained, and every migration written after it lands is one that cannot
acquire the problem. Adopting it late means an audit first.

quarterback runs this same file as ``tests/test_migrations_self_contained.py``;
the two are kept byte-identical below their constants, so the copy you are given
is the copy that is tested.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Where alembic keeps the revisions — `<script_location>/versions` from your
#: `alembic.ini`. `alembic/versions` and `db/migrations/versions` are the other
#: two common spellings; this default assumes the file sits in `<repo>/tests/`.
VERSIONS_DIR = REPO_ROOT / "migrations" / "versions"

#: A floor, not a count: it exists so the glob cannot silently match nothing —
#: a renamed folder, a moved `script_location` — and turn the whole sweep into a
#: no-op that always passes. Set it under however many migrations you have and
#: then leave it alone; it does not need raising as the directory grows.
MIN_EXPECTED_MIGRATIONS = 1

#: Where the rule is written down in prose, quoted in the failure message. The
#: person who trips this guard needs somewhere to read *why*.
POLICY_REFERENCE = "your repo's migration policy"

#: Reviewed exemptions: ``stem -> (frozenset of exactly the import statements
#: this migration may make, written justification)``.
#:
#: **Empty, and worth keeping empty.**
#:
#: An entry is exact in both directions. An exempt migration that grows a *new*
#: import fails anyway — an exemption bounds one reviewed statement, it is not a
#: blanket pass for the file, or the exact hazard this guard exists to prevent
#: would walk straight in through an exempt file. And one that no longer makes
#: its exempted import fails too, so the entry gets deleted rather than rotting
#: into a permanent hole.
EXEMPT_MODULES: dict[str, tuple[frozenset[str], str]] = {}

# --- shared guard: kept byte-identical by quarterback's own parity test ------
#: Top-level modules a migration may import. The standard library is allowed
#: wholesale: it is versioned with the interpreter, knows nothing about this
#: schema, and cannot drift underneath a revision. ``alembic`` and ``sqlalchemy``
#: are the migration toolchain itself. Everything else — first-party packages
#: above all, but third-party libraries too — has to be argued for, which is the
#: entire point: adding a name here is a deliberate review decision.
#:
#: An ALLOWLIST rather than a denylist of the app package, because every
#: first-party package carries the identical hazard (``app``, ``tests``,
#: ``scripts``, a top-level ``config`` module …) and so does any third-party
#: library whose next release changes what a frozen migration does.
ALLOWED_IMPORT_ROOTS = frozenset({"alembic", "sqlalchemy"}) | sys.stdlib_module_names

#: Callables whose constant-string argument is an import target.
_DYNAMIC_IMPORT_CALLS = ("import_module", "__import__")


def _migration_files() -> list[Path]:
    return sorted(p for p in VERSIONS_DIR.glob("*.py") if p.name != "__init__.py")


def _import_root(dotted: str) -> str:
    return dotted.split(".")[0]


def _rendered(name: str, asname: str | None) -> str:
    """One name as the source spells it, `as` clause included.

    The alias is part of the statement, so an exemption pinned to
    `from x import y` cannot be satisfied by `from x import y as z` — an
    exemption that says "exact" has to mean the line, not a normalisation of it.
    """
    return f"{name} as {asname}" if asname else name


def _dynamic_import_aliases(tree: ast.Module) -> frozenset[str]:
    """Local names an import statement bound to a dynamic-import callable.

    `from importlib import import_module as load` renames the hazard, and
    `importlib` is stdlib so the import itself is allowed — without this, the
    following `load('app.models')` is a bare call to an unremarkable name and
    walks straight through the scan below.

    Known limit, and it is a floor rather than a hole to be plugged: a name
    rebound by *assignment* (`load = importlib.import_module`) is not tracked.
    Following that needs dataflow, and this guard is a tripwire against the
    accident — the migration that reaches for a model because it was the
    shortest way to write the backfill — not a sandbox against someone routing
    around it on purpose.
    """
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            aliases.update(
                alias.asname
                for alias in node.names
                if alias.name in _DYNAMIC_IMPORT_CALLS and alias.asname
            )
    return frozenset(aliases)


def _dynamic_import_target(node: ast.Call, aliases: frozenset[str]) -> str | None:
    """The callable name if `node` is a dynamic-import call, else None.

    Recognises `__import__(...)`, `import_module(...)`, the attribute spelling
    `importlib.import_module(...)`, and any local name `aliases` says was bound
    to one of those by an import statement.
    """
    func = node.func
    if isinstance(func, ast.Name) and (func.id in _DYNAMIC_IMPORT_CALLS or func.id in aliases):
        return func.id
    if isinstance(func, ast.Attribute) and func.attr in _DYNAMIC_IMPORT_CALLS:
        return func.attr
    return None


def disallowed_imports(path: Path) -> list[tuple[int, str]]:
    """`[(lineno, statement)]` for every import `path` may not make.

    Catches, at module level or inside a function:

    * `import x` / `import x.y` and `from x.y import z` whose top-level module
      is not in `ALLOWED_IMPORT_ROOTS`;
    * `importlib.import_module('x.y')` / `__import__('x.y')`, the same rule
      applied to the constant target — under any local name an import statement
      bound one of those to (`from importlib import import_module as load`);
    * a dynamic import whose target is *not* a constant, which cannot be
      checked statically at all and is therefore not permitted in a migration.

    An AST walk rather than a grep precisely so the function-local spellings are
    covered — a migration reaching for app code almost always does it inside
    `upgrade()`, where a line-oriented scan of the file's head sees nothing.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = _dynamic_import_aliases(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _import_root(alias.name) not in ALLOWED_IMPORT_ROOTS:
                    found.append((node.lineno, f"import {_rendered(alias.name, alias.asname)}"))
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import, and migrations/versions is not a
            # package, so those cannot resolve to anything at all — alembic
            # loads each revision as a standalone module by path.
            module = node.module or ""
            if node.level == 0 and _import_root(module) not in ALLOWED_IMPORT_ROOTS:
                names = ", ".join(_rendered(a.name, a.asname) for a in node.names)
                found.append((node.lineno, f"from {module} import {names}"))
        elif isinstance(node, ast.Call):
            callee = _dynamic_import_target(node, aliases)
            if callee is None:
                continue
            target = node.args[0] if node.args else None
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                if _import_root(target.value) not in ALLOWED_IMPORT_ROOTS:
                    found.append((node.lineno, f"{callee}({target.value!r})"))
            else:
                found.append((node.lineno, f"{callee}(<non-constant target>)"))
    return sorted(set(found))


def _explain(path: Path, offenders: list[tuple[int, str]]) -> str:
    return (
        f"{path.relative_to(REPO_ROOT)} imports code a migration may not depend on:\n"
        + "\n".join(f"  line {lineno}: {stmt}" for lineno, stmt in offenders)
        + "\n\nA migration runs at a fixed point in schema history — inline the SQL it "
        "needs (op.*, sa.table()/sa.column(), text()), naming only the columns that "
        "exist at that revision, and freeze that list in a comment. Allowed import "
        f"roots are the standard library plus alembic/sqlalchemy. See {POLICY_REFERENCE}."
    )


def test_the_migrations_directory_is_discoverable():
    """Sanity: the sweep below is worthless if it silently globs nothing."""
    assert VERSIONS_DIR.is_dir(), VERSIONS_DIR
    files = _migration_files()
    assert len(files) >= MIN_EXPECTED_MIGRATIONS, (
        f"only found {len(files)} migrations under {VERSIONS_DIR} — the sweep is "
        "globbing (almost) nothing, so every test below would pass vacuously"
    )


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.stem)
def test_a_migration_imports_no_live_application_code(path: Path):
    """No migration may import live code (see this module's docstring).

    Exempt migrations are **not** skipped: their imports are pinned to an exact
    frozen set, so an exemption bounds one reviewed statement instead of
    switching the guard off for a whole file.
    """
    allowed, _reason = EXEMPT_MODULES.get(path.stem, (frozenset(), ""))
    offenders = disallowed_imports(path)
    statements = {stmt for _lineno, stmt in offenders}

    unexpected = sorted((lineno, stmt) for lineno, stmt in offenders if stmt not in allowed)
    assert not unexpected, _explain(path, unexpected)

    stale = sorted(allowed - statements)
    assert not stale, (
        f"{path.relative_to(REPO_ROOT)} no longer makes the import(s) it is exempted "
        f"for: {stale}. Delete the stale entry from EXEMPT_MODULES — a stale exemption "
        "is a permanently open hole in the guard."
    )


def test_every_exemption_names_a_real_migration_and_says_why():
    """An exemption for a migration that no longer exists is dead policy.

    That an exemption is still *needed* is enforced by the sweep above, which
    fails when an exempted import is no longer made.
    """
    stems = {p.stem for p in _migration_files()}
    for stem, (allowed, reason) in EXEMPT_MODULES.items():
        assert stem in stems, f"EXEMPT_MODULES names a migration that does not exist: {stem}"
        assert reason.strip(), f"exemption {stem} has no written justification"
        assert allowed, (
            f"exemption {stem} allows no import statements — an empty allowlist exempts "
            "nothing, so the entry should be deleted"
        )


# --- the detector's own negative tests ---------------------------------------
#
# A guard nobody has watched fail is not a guard. Every spelling the docstring
# claims to cover is exercised here against a synthetic file, so the sweep above
# going green means the detector looked and found nothing rather than that it
# never looked.


def _probe(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "probe_migration.py"
    path.write_text(source, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "source, expected",
    [
        pytest.param(
            "import app.models\n",
            "import app.models",
            id="module-level-import",
        ),
        pytest.param(
            "from app.models import Post\n",
            "from app.models import Post",
            id="module-level-from-import",
        ),
        pytest.param(
            "def upgrade():\n    from app.db import engine\n",
            "from app.db import engine",
            id="function-local-from-import",
        ),
        pytest.param(
            "def upgrade():\n    import app.models\n",
            "import app.models",
            id="function-local-import",
        ),
        pytest.param(
            "import importlib\n\n\ndef upgrade():\n    importlib.import_module('app.models')\n",
            "import_module('app.models')",
            id="dynamic-importlib",
        ),
        pytest.param(
            "def upgrade():\n    __import__('app.db')\n",
            "__import__('app.db')",
            id="dynamic-dunder-import",
        ),
        pytest.param(
            "def upgrade():\n    name = 'app' + '.db'\n    __import__(name)\n",
            "__import__(<non-constant target>)",
            id="dynamic-non-constant",
        ),
        pytest.param(
            "from importlib import import_module as load\n\n\n"
            "def upgrade():\n    load('app.models')\n",
            "load('app.models')",
            id="dynamic-under-a-renamed-importlib",
        ),
        pytest.param(
            "import app.models as m\n",
            "import app.models as m",
            id="module-level-import-aliased",
        ),
        pytest.param(
            "from app.models import Post as P\n",
            "from app.models import Post as P",
            id="module-level-from-import-aliased",
        ),
        pytest.param(
            "import scripts.release\n",
            "import scripts.release",
            id="another-first-party-package",
        ),
        pytest.param(
            "import httpx\n",
            "import httpx",
            id="third-party-library",
        ),
    ],
)
def test_the_guard_flags_a_disallowed_import(tmp_path: Path, source: str, expected: str):
    offenders = disallowed_imports(_probe(tmp_path, source))
    assert [stmt for _lineno, stmt in offenders] == [expected]


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import os\nimport json\nimport re\n", id="stdlib"),
        pytest.param("from alembic import op\nimport sqlalchemy as sa\n", id="toolchain"),
        pytest.param("from sqlalchemy.sql import text\n", id="toolchain-submodule"),
        pytest.param("from __future__ import annotations\n", id="future"),
        pytest.param("from collections.abc import Sequence\n", id="stdlib-submodule"),
        pytest.param("def upgrade():\n    from . import helpers\n", id="relative-unresolvable"),
        pytest.param("import importlib\n\nimportlib.import_module('json')\n", id="dynamic-stdlib"),
        pytest.param(
            "from importlib import import_module as load\n\nload('json')\n",
            id="dynamic-stdlib-under-a-renamed-importlib",
        ),
        pytest.param("import sqlalchemy as sa\nfrom alembic import op as o\n", id="toolchain-aliased"),
    ],
)
def test_the_guard_allows_what_a_migration_legitimately_imports(tmp_path: Path, source: str):
    assert disallowed_imports(_probe(tmp_path, source)) == []

# --- end shared guard -------------------------------------------------------
