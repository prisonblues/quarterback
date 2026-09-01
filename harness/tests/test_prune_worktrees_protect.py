"""`.database.protect` matches GLOB patterns, so a generated family can be named (#662).

`prune-worktrees` calls a database orphaned when no live worktree maps to it, and that
mapping is per-worktree. A database a project mints on some other axis matches nothing and
is offered for deletion on every run — the worked case being a test suite's
per-migration-head templates, `<project>_test_tmpl_<head>`.

The list used to be exact-match (`PROTECT["$db"]`), which cannot name that family: the live
heads sit on branches no single checkout can see, so a hand-pinned list is unverifiable from
anywhere and silently protects dead templates the day a migration lands. Patterns remove the
staleness; what they introduce is the opposite risk, and it is the one this suite is mostly
about — **a pattern can suppress a genuine orphan**, which is a quieter failure than the one
it replaces. Hence `test_a_pattern_that_matches_is_reported_with_the_pattern`.

Two ways the fix could look like it worked while doing nothing, both pinned here:

* `test_a_wildcard_protects_the_whole_family` — quoting `$g` on the right of `[[ == ]]`
  degrades it to a literal comparison. Every *literal* entry keeps passing, so a suite
  without a wildcard case would stay green over the exact bug being fixed.
* `test_a_configured_name_outside_the_project_prefix_is_still_not_an_orphan` — the scan only
  ever considers `<project>_*`, so an entry outside it is inert. Inert is correct (nothing
  out there was going to be dropped) and worth pinning, because "inert" and "broken" look
  identical from a passing test that never tries it.

The blocks are extracted from the real script rather than copied, so a refactor that moves
or renames them fails here instead of leaving this suite green about code nobody runs.

Run: pytest harness/tests
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# A sibling module, imported by bare name — see `_path_sandbox`'s own docstring for why a
# suite driving a stanza cannot build its PATH from the host's.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_sandbox  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "prune-worktrees"

BASH = shutil.which("bash")
#: `cfg_array` pipes through `jq`; the container resolver this stanza also covers shells out
#: to `grep`. Put back one BINARY at a time — `_path_sandbox` explains why a directory is
#: the wrong unit.
JQ = shutil.which("jq")
GREP = shutil.which("grep")

pytestmark = pytest.mark.skipif(
    BASH is None or JQ is None or GREP is None,
    reason="bash, jq and grep must all be on PATH")


def _block(start: str, end: str) -> str:
    """The lines the two markers bracket, out of the live script."""
    src = SCRIPT.read_text()
    assert start in src and end in src, (
        f"the {start} / {end} markers are gone from prune-worktrees, so this suite is "
        "asserting nothing — fix the markers rather than deleting the test")
    return src.split(start, 1)[1].split("\n", 1)[1].split(end, 1)[0]


def cfg_array_block() -> str:
    block = _block("# >>> cfg-array", "# <<< cfg-array")
    assert "cfg_array()" in block, "the markers no longer bracket cfg_array"
    return block


def db_scan_block() -> str:
    block = _block("# >>> db-scan", "# <<< db-scan")
    assert "ORPHAN_DBS+=" in block, "the markers no longer bracket the database scan"
    assert "PROTECT_GLOBS" in block, "the markers no longer bracket the protect list"
    return block


def pg_container_block() -> str:
    """The container resolver, extracted rather than reproduced.

    Reproducing it was the first attempt and it was wrong in a way worth recording: the
    real `postgres_container_candidates` is two greps, stable human-named containers before
    hex-prefixed ephemerals, and a one-grep copy answers a different question about ordering
    than the script does. A stub of the *answer* is fine here (see `PRELUDE`); a
    hand-simplified copy of the logic is not.
    """
    block = _block("# >>> pg-container", "# <<< pg-container")
    assert "postgres_container_answers()" in block, (
        "the markers no longer bracket the container resolver")
    return block


#: What the scan is run AGAINST rather than what is under test: the project name, the
#: live-worktree answer, and the credential the resolver would have worked out. Reproduced
#: for the reason `test_prune_worktrees_claims.py` gives — a stub of the answer, not of the
#: logic.
PRELUDE = """
set -uo pipefail
GREEN='' YELLOW='' CYAN='' NC=''
PROJECT='acme'
PG_USER='acme'
DB_CONTAINER='%(container)s'
CONFIG_JSON=%(config)s
declare -A LIVE_DB=()
%(live)s
ORPHAN_DBS=()
PROTECTED_DBS=()
"""


def run_scan(databases, *, protect=None, live=(), container="acme-postgres-1", tmp_path):
    """Run the real scan over `databases`, with a stub `docker` answering for psql."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    # Two questions, one stub: `ps --format` names the candidate containers, and
    # `exec <c> psql ... -tAc <sql>` answers either the `SELECT 1` liveness probe or the
    # database listing. Discriminating on the SQL keeps those apart the way real psql does.
    # The container name has to satisfy the real resolver's `postgres|pgdb|_db` filter, or
    # `container: auto` finds no candidate and the scan is skipped — which reads as "no
    # orphans" rather than as "not scanned". That is the failure mode the script itself
    # calls out in its yellow "NOT SCANNED, not clean" note.
    listing = " ".join(f"'{d}'" for d in databases) or "''"
    docker.write_text(
        f"#!{BASH}\n"
        'if [[ "${1:-}" == "ps" ]]; then printf "%s\\n" "acme-postgres-1" "app-1"; '
        'exit 0; fi\n'
        'if [[ "${1:-}" == "exec" ]]; then\n'
        '    for a in "$@"; do\n'
        '        [[ "$a" == "SELECT 1" ]] && exit 0\n'
        '        if [[ "$a" == *pg_database* ]]; then\n'
        f'            for d in {listing}; do [[ -n "$d" ]] && printf "%s\\n" "$d"; done\n'
        '            exit 0\n'
        '        fi\n'
        '    done\n'
        '    exit 0\n'
        'fi\n'
        'exit 0\n')
    docker.chmod(0o755)

    config = {"project": "acme", "database": {"engine": "postgresql"}}
    if protect is not None:
        config["database"]["protect"] = protect

    script = _path_sandbox.sibling_dir(tmp_path) / "scan.sh"
    script.write_text(
        PRELUDE % {
            "config": json.dumps(json.dumps(config)),
            "container": container,
            "live": "\n".join(f'LIVE_DB["{d}"]=1' for d in live),
        }
        + pg_container_block()
        + cfg_array_block()
        + db_scan_block()
        + '\nprintf "ORPHAN|%s\\n" "${ORPHAN_DBS[@]:-}"\n'
        + 'printf "KEPT|%s\\n" "${PROTECTED_DBS[@]:-}"\n')

    got = subprocess.run(
        [BASH, str(script)], capture_output=True, text=True,
        env={"PATH": _path_sandbox.sandbox_path(tmp_path, bindir, tools=("jq", "grep")),
             "HOME": str(tmp_path)})
    assert got.returncode == 0, got.stderr
    got.orphans = [ln.split("|", 1)[1] for ln in got.stdout.splitlines()
                   if ln.startswith("ORPHAN|") and ln != "ORPHAN|"]
    got.kept = [ln.split("|", 1)[1] for ln in got.stdout.splitlines()
                if ln.startswith("KEPT|") and ln != "KEPT|"]
    return got


# --------------------------------------------------------------- the family case

def test_a_wildcard_protects_the_whole_family(tmp_path):
    """The defect this exists for. A pattern quoted on the right of `[[ == ]]` compares as
    a literal, which every literal-entry test below would still pass."""
    got = run_scan(["acme_test_tmpl_m1564a", "acme_test_tmpl_m1671a", "acme_gone"],
                   protect=["acme_test_tmpl_*"], tmp_path=tmp_path)
    assert got.orphans == ["acme_gone"]


def test_the_family_is_protected_however_the_head_moves(tmp_path):
    """The property a hand-pinned list cannot have: nobody edits anything when the
    migration head changes, so there is nothing to go stale."""
    got = run_scan(["acme_test_tmpl_whatever_lands_next"],
                   protect=["acme_test_tmpl_*"], tmp_path=tmp_path)
    assert got.orphans == []


# ------------------------------------------------- what patterns must not quietly do

def test_a_pattern_that_matches_is_reported_with_the_pattern(tmp_path):
    """A too-broad pattern is the one new way this can fail, and it fails by *hiding*
    orphans. The report names the pattern so a dry run shows which entry did it."""
    got = run_scan(["acme_test_tmpl_m1564a"], protect=["acme_*"], tmp_path=tmp_path)
    assert got.orphans == []
    assert len(got.kept) == 1
    assert "acme_test_tmpl_m1564a" in got.kept[0]
    assert "acme_*" in got.kept[0], (
        "the pattern that protected it has to be in the line, or a sweep that suppresses "
        "a real orphan looks identical to one that found nothing")


def test_nothing_is_reported_as_protected_when_nothing_configured_matched(tmp_path):
    """The built-in protections stay silent: `acme` and `acme_test` are names these scripts
    mint themselves, so there is nothing for a reader to be surprised by."""
    got = run_scan(["acme", "acme_test", "acme_gone"], protect=None, tmp_path=tmp_path)
    assert got.orphans == ["acme_gone"]
    assert got.kept == []


# ---------------------------------------------------------- backward compatibility

def test_a_literal_entry_still_means_exactly_itself(tmp_path):
    """Every list written before this change is metacharacter-free, so it has to keep
    meaning what it meant."""
    got = run_scan(["acme_keepme", "acme_keepme_2"], protect=["acme_keepme"],
                   tmp_path=tmp_path)
    assert got.orphans == ["acme_keepme_2"]


def test_the_built_in_protections_survive_an_empty_config(tmp_path):
    got = run_scan(["acme", "acme_test", "acme_debris"], protect=[], tmp_path=tmp_path)
    assert got.orphans == ["acme_debris"]


def test_no_protect_key_at_all_is_not_an_error(tmp_path):
    """`.database.protect` is optional, and a project that never wrote one must sweep
    exactly as it always did."""
    got = run_scan(["acme_debris"], protect=None, tmp_path=tmp_path)
    assert got.orphans == ["acme_debris"]


# ------------------------------------------------------------- the surrounding rules

def test_a_live_worktrees_database_is_kept_without_any_pattern(tmp_path):
    """Liveness is decided before protection and is not weakened by it."""
    got = run_scan(["acme_feat_x", "acme_dead"], live=["acme_feat_x"],
                   protect=["acme_test_tmpl_*"], tmp_path=tmp_path)
    assert got.orphans == ["acme_dead"]
    assert got.kept == [], "a live database is not a *protected* one — it never got that far"


def test_a_configured_name_outside_the_project_prefix_is_still_not_an_orphan(tmp_path):
    """The scan only considers `<project>_*`, so such an entry is inert rather than wrong —
    nothing outside the prefix was ever going to be dropped."""
    got = run_scan(["postgres", "template1", "acme_debris"],
                   protect=["postgres"], tmp_path=tmp_path)
    assert got.orphans == ["acme_debris"]


def test_a_pattern_cannot_reach_outside_the_project_prefix(tmp_path):
    """Even a pattern broad enough to match them: `<project>_` is checked first."""
    got = run_scan(["postgres", "acme_debris"], protect=["*"], tmp_path=tmp_path)
    assert got.orphans == []
    assert got.kept == ["acme_debris  (.database.protect: *)"], (
        "`postgres` must not appear — it was never a candidate, so it cannot be reported "
        "as something a pattern saved")


def test_the_container_resolver_still_runs_when_the_container_is_auto(tmp_path):
    """`container: auto` is the common configuration, and the protect list is built before
    the resolver — an ordering worth pinning, since a failure in one used to discard both."""
    got = run_scan(["acme_test_tmpl_m1564a", "acme_debris"], protect=["acme_test_tmpl_*"],
                   container="auto", tmp_path=tmp_path)
    assert got.orphans == ["acme_debris"]
