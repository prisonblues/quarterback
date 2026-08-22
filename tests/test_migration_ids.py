"""Where a revision id comes from, and which ones may never change (#341).

quarterback's migration directory holds two naming schemes on purpose, and this
module is the only place that says which id belongs to which and refuses the
edits that would break either.

**The legacy chain, ``0001`` … ``0034``, is frozen.** Those ids were minted by
hand and they are staying exactly as they are. Not out of sentiment: a renumber
rewrites ``revision``, and ``revision`` is what ``alembic_version`` stores, so
renaming one makes every database that has applied it assert a revision that no
longer exists. Three worktree databases were dropped and rebuilt on 2026-08-22
for exactly that. ``LEGACY_IDS`` below is the pin, filename included, and it is
deliberately a literal table rather than a count — a count is satisfied by a
rename, which is the one thing it exists to catch.

**Everything above the seam is opaque.** ``m`` and eight hex digits, minted at
random by ``scripts/migration_reconcile.py``'s ``new_revision_id`` and put on
every generated revision by ``migrations/env.py``. The reason is the failure that
opened #341: the *next number* is a value two branches can both work out, four of
them worked out ``0029`` on one morning, and every preflight truthfully said GO
because each branch really was single-headed against ``main``. The duplicate
existed only in the union of branches none of which had landed, and no check that
reads one ref against a base can see that. An id nobody picks out of a shared
sequence cannot be picked twice, so the same situation degrades to an ordinary
two-head graph — which ``migration-heads``, ``pre-push`` and the reconciler all
already handle. ``tests/test_migration_reconcile.py`` reconstructs that morning
under both schemes and shows the difference.

So the rule this file enforces is: **a new revision id must not be a chain
number.** A hand-written ``0035`` is refused here rather than quietly renamed by
the generator, because a person who typed a number is better told than corrected.

Its neighbours: ``test_migration_drift.py`` replays the mixed chain on a fresh
database (which is what proves legacy numbers and opaque ids interoperate at all),
``test_migrations_self_contained.py`` scans what a migration may import, and
``test_migration_reconcile.py`` covers the graph tool. See README.md § Database
migrations.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_DIR = REPO_ROOT / "migrations" / "versions"
ENV_PY = REPO_ROOT / "migrations" / "env.py"

# `scripts/` is a directory of standalone tools, not an importable package, so the
# module is loaded by path. It must be registered in sys.modules before it executes:
# @dataclass resolves annotations through sys.modules[cls.__module__].
_SPEC = importlib.util.spec_from_file_location(
    "migration_reconcile", REPO_ROOT / "scripts" / "migration_reconcile.py"
)
mr = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = mr
_SPEC.loader.exec_module(mr)


#: Every hand-numbered revision, with the file that declares it. Frozen: an entry
#: that changes, disappears or gains a sibling is a rename, and a rename is what
#: makes a deployed `alembic_version` row lie. New migrations are never added here
#: — they carry an opaque id and are covered by the rules below instead.
LEGACY_IDS: dict[str, str] = {
    "0001": "0001_initial.py",
    "0002": "0002_leases_blobs_sessions.py",
    "0003": "0003_dev_context.py",
    "0004": "0004_session_cwd.py",
    "0005": "0005_session_title_recap.py",
    "0006": "0006_session_model.py",
    "0007": "0007_post_session.py",
    "0008": "0008_subagents.py",
    "0009": "0009_lease_repo.py",
    "0010": "0010_worktree_sync.py",
    "0011": "0011_review_stats.py",
    "0012": "0012_review_finding_reports.py",
    "0013": "0013_agent_names.py",
    "0014": "0014_review_rounds.py",
    "0015": "0015_reviewer_tokens.py",
    "0016": "0016_review_run_files.py",
    "0017": "0017_review_provenance.py",
    "0018": "0018_review_base.py",
    "0019": "0019_resource_leases.py",
    "0020": "0020_finding_outcomes.py",
    "0021": "0021_plan_items.py",
    "0022": "0022_canonical_release_repo.py",
    "0023": "0023_lease_state.py",
    "0024": "0024_reviewer_code_access.py",
    "0025": "0025_plans.py",
    "0026": "0026_merge_queue.py",
    "0027": "0027_dial_settings.py",
    "0028": "0028_order_proposals.py",
    "0029": "0029_needs_human.py",
    "0030": "0030_plan_item_placement.py",
    "0031": "0031_plan_scopes.py",
    "0032": "0032_lease_end_reason.py",
    "0033": "0033_canonical_review_repo.py",
    "0034": "0034_canonical_dial_and_worktree_repo.py",
}


def _migrations() -> list[tuple[Path, object]]:
    """Every migration file with its parsed revision, by the reconciler's own parser.

    The same reader the graph tool, the CI job and the pre-push hook use, so a file
    this module blesses cannot be one they refuse.
    """
    out = []
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        raw = path.read_bytes()
        try:
            rev = mr.parse_migration(raw.decode("utf-8"), path=path.name, raw=raw)
        except ValueError:  # a helper module, not a migration
            continue
        out.append((path, rev))
    return out


MIGRATIONS = _migrations()


def test_the_sweep_found_the_migrations_at_all() -> None:
    """A moved directory must fail loudly rather than make every test below vacuous."""
    assert len(MIGRATIONS) >= len(LEGACY_IDS) + 1, (
        f"{VERSIONS_DIR} yielded {len(MIGRATIONS)} migrations, fewer than the "
        f"{len(LEGACY_IDS)} legacy ones plus the seam — the glob has stopped matching"
    )


def test_no_existing_revision_id_changed() -> None:
    """The hand-numbered chain is exactly as it was, id and filename.

    This is the assertion #341 turns on. The issue's own proposal offered two
    routes to opaque ids — rewrite the existing chain once, or leave it alone and
    hash-name only what comes next — and called the second "less satisfying and
    much safer". It is safer because `revision` is what a live `alembic_version`
    stores: rewrite one and every database holding it now names a revision the
    repository no longer has.
    """
    found = {rev.id: path.name for path, rev in MIGRATIONS if rev.id in LEGACY_IDS}
    assert found == LEGACY_IDS

    missing = sorted(set(LEGACY_IDS) - {rev.id for _p, rev in MIGRATIONS})
    assert not missing, (
        f"revision(s) {missing} are gone from {VERSIONS_DIR.name}/. Every database in "
        "the fleet stores one of these ids; deleting or renaming one strands it."
    )


def test_a_new_revision_id_is_never_a_chain_number() -> None:
    """The rule, stated once: no migration written from now on carries a number.

    A number is a value the next branch can also work out, which is the whole
    mechanism of the collision — so this is the property that retires it, rather
    than one more place it gets detected.
    """
    numbered = sorted(
        rev.id for _p, rev in MIGRATIONS if rev.id not in LEGACY_IDS and rev.number is not None
    )
    assert not numbered, (
        f"revision id(s) {numbered} are chain numbers, and only the frozen legacy "
        "chain may be. Mint one instead: scripts/migration_reconcile.py new-id — or "
        "just drop --rev-id and let migrations/env.py do it."
    )


def test_every_new_revision_id_is_one_the_generator_would_mint() -> None:
    """House shape, not merely "not a number": `m` and eight hex digits."""
    wrong = sorted(
        rev.id
        for _p, rev in MIGRATIONS
        if rev.id not in LEGACY_IDS and not mr._HASH_ID_RE.match(rev.id)
    )
    assert not wrong, (
        f"revision id(s) {wrong} are neither a frozen legacy number nor the "
        f"{mr._HASH_ID_RE.pattern} shape scripts/migration_reconcile.py mints.\n"
        "The generator lives in migrations/env.py, which alembic only runs for "
        "`revision --autogenerate`. A bare `alembic revision` or `alembic merge heads` "
        "does not run it and keeps alembic's own 12-hex id — which is opaque and "
        "harmless, just not this repo's shape. Rename the file and its `revision` "
        "before it lands: scripts/migration_reconcile.py new-id"
    )


def test_a_filename_states_its_own_revision_id() -> None:
    """`<id>_<slug>.py`, both eras. Alembic reads the id out of the file, so a
    filename disagreeing with it is not wrong to Alembic and is wrong to every
    person who greps for a revision."""
    mismatched = [
        (path.name, rev.id) for path, rev in MIGRATIONS if not path.name.startswith(f"{rev.id}_")
    ]
    assert not mismatched, f"filename does not lead with its revision id: {mismatched}"


def test_revision_ids_are_unique() -> None:
    """The condition Alembic cannot load a graph without, asserted on the tree
    itself — `revs_at_ref` asserts it at a git ref, and a file added but not yet
    committed is only visible here."""
    assert not mr.duplicate_ids([rev for _p, rev in MIGRATIONS])


def test_the_chain_really_is_mixed() -> None:
    """Both schemes are present, so the drift test's fresh-database replay is
    actually walking legacy numbers *and* an opaque id rather than proving the
    mixed graph works by never containing one."""
    ids = {rev.id for _p, rev in MIGRATIONS}
    assert ids & set(LEGACY_IDS), "no legacy revision left"
    assert {i for i in ids if mr._HASH_ID_RE.match(i)}, (
        "no opaque revision id in the chain — nothing replays the mixed graph"
    )


# ---------------------------------------------------------------------------
# the generator
# ---------------------------------------------------------------------------


def test_a_minted_id_is_opaque_and_not_a_chain_number() -> None:
    ids = [mr.new_revision_id() for _ in range(500)]
    assert all(mr._HASH_ID_RE.match(i) for i in ids)
    assert all(mr.Rev(i).number is None for i in ids), (
        "a minted id read as a chain position would fall into the legacy renumber path"
    )


def test_minted_ids_do_not_repeat() -> None:
    """The one property the scheme exists for. 500 draws from 4.3e9 collide with
    probability about 3e-5, so a repeat here is a broken generator rather than
    bad luck."""
    ids = [mr.new_revision_id() for _ in range(500)]
    assert len(set(ids)) == len(ids)


def test_alembics_own_id_is_replaced() -> None:
    """`uuid4().hex[-12:]` is what alembic picks when nobody passed `--rev-id`."""
    adopted = mr.adopt_revision_id("a1b2c3d4e5f6")
    assert adopted != "a1b2c3d4e5f6"
    assert mr._HASH_ID_RE.match(adopted)
    assert mr._HASH_ID_RE.match(mr.adopt_revision_id(None))


def test_an_explicitly_chosen_id_is_kept() -> None:
    """`--rev-id` is the only way to write a merge or fixup revision under a
    known id, so the hook must not override it. A number chosen this way is
    refused by `test_a_new_revision_id_is_never_a_chain_number`, which tells the
    author, rather than being silently renamed here."""
    assert mr.adopt_revision_id("merge_0034_and_m7f2a") == "merge_0034_and_m7f2a"
    assert mr.adopt_revision_id("0035") == "0035"
    assert mr.adopt_revision_id("m7f2a91c4") == "m7f2a91c4"


def test_an_explicit_id_shaped_like_alembics_own_is_still_kept() -> None:
    """The shape test alone cannot tell `--rev-id deadbeef1234` from an id alembic
    invented, and would replace the caller's. `migrations/env.py` reads the parsed
    flag and says so, which is why the decision takes `explicit` rather than
    inferring it."""
    assert mr.adopt_revision_id("deadbeef1234", explicit=True) == "deadbeef1234"
    assert mr.adopt_revision_id("deadbeef1234") != "deadbeef1234"


def test_an_empty_explicit_id_is_not_a_choice() -> None:
    """`explicit` describes a flag the caller set; a caller that sets it and passes
    nothing has named no id, and an empty `revision` is not a migration."""
    assert mr._HASH_ID_RE.match(mr.adopt_revision_id(None, explicit=True))
    assert mr._HASH_ID_RE.match(mr.adopt_revision_id("", explicit=True))


# ---------------------------------------------------------------------------
# the wiring
# ---------------------------------------------------------------------------


def _env_configure_call() -> ast.Call:
    """The `context.configure(...)` call in `migrations/env.py`.

    Read out of the source rather than by importing: env.py runs the migrations at
    import time and needs a live Alembic context, so there is nothing to import.
    """
    tree = ast.parse(ENV_PY.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "configure"
    ]
    assert len(calls) == 1, f"expected one context.configure() in env.py, found {len(calls)}"
    return calls[0]


def test_env_hands_the_generator_to_alembic() -> None:
    """Without this keyword the hook below is dead code and every new revision
    silently goes back to alembic's own id — which is opaque, so nothing would
    look wrong until the house shape check above failed on somebody's branch."""
    keywords = {kw.arg for kw in _env_configure_call().keywords}
    assert "process_revision_directives" in keywords


def test_the_env_hook_delegates_to_the_shared_scheme() -> None:
    """env.py must not grow its own copy of the id rule. It has two jobs — report
    whether `--rev-id` was given, and call `adopt_revision_id` for each directive —
    and a second implementation of the shape is how the reconciler and the
    generator drift apart."""
    source = ENV_PY.read_text(encoding="utf-8")
    assert "adopt_revision_id" in source
    assert "cmd_opts" in source, (
        "the hook must read the parsed --rev-id rather than infer it from the id's "
        "shape, or an explicit 12-hex id is mistaken for one alembic generated"
    )
    tree = ast.parse(source)
    hook = next(
        (
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "process_revision_directives"
        ),
        None,
    )
    assert hook is not None, "migrations/env.py defines no process_revision_directives"


@pytest.mark.parametrize("bad", ["0035", "00351", "9999"])
def test_the_number_detector_the_rules_lean_on_still_matches_numbers(bad: str) -> None:
    """A guard whose detector has quietly stopped matching passes on everything.
    `Rev.number` is what `test_a_new_revision_id_is_never_a_chain_number` reads."""
    assert mr.Rev(bad).number is not None
