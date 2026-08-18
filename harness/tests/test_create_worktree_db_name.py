"""`create-worktree` resolving the main database name — the guard that could not fire.

Isolated mode has to know which database to copy. The resolution reads two
configurable variables (`database.url_env`, `database.name_env`) out of the
worktree's `.env`, and there is a `die` under it whose entire job is to explain the
case where neither is set.

**That `die` was unreachable by the one input it existed for.** The script runs
under `set -euo pipefail`, and `MAIN_DB_NAME` was only ever assigned inside the
branches above it — so when `url_env` was declared but absent from `.env`, the
first reader of the variable was the `[[ -z "$MAIN_DB_NAME" ]]` test itself, which
dereferenced an unset name and died with `MAIN_DB_NAME: unbound variable` at the
exact instruction written to say something useful. Measured twice on quarterback,
whose `.env` carries only `POSTGRES_PASSWORD`.

The second defect was in the same lines: `url_env` and `name_env` were an
`if/else`, so declaring the first *disabled* the second. A repo that assembles its
URL at runtime, or keeps the database name in docker-compose and only the password
in `.env`, could therefore never use an isolated database — and got an
unbound-variable crash instead of a reason.

The block is extracted from the real script rather than copied here, so a refactor
that moves or renames it fails in this suite instead of leaving it green about code
nobody runs any more — the same rule `test_create_worktree_rerere.py` follows.

Run: pytest harness/tests
"""

import shlex
import shutil
import subprocess

from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "create-worktree"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")

_START = "# >>> dbname"
_END = "# <<< dbname"


def dbname_block() -> str:
    """The database-name resolution, lifted out of create-worktree as it ships."""
    src = SCRIPT.read_text()
    assert _START in src and _END in src, (
        f"the {_START} / {_END} markers are gone from create-worktree, so this suite "
        "is asserting nothing — fix the markers rather than deleting the test")
    block = src.split(_START, 1)[1].split("\n", 1)[1].split(_END, 1)[0]
    assert "MAIN_DB_NAME" in block, "the markers no longer bracket the resolution"
    return block


def run_block(env_text: str, *, url_env="DATABASE_URL", name_env="POSTGRES_DB",
              user_env="POSTGRES_USER",
              branch="feat/probe") -> subprocess.CompletedProcess:
    """Run the stanza against a `.env` of the caller's choosing.

    `set -euo pipefail` is on, exactly as in the script: the whole point of the bug
    is that `set -u` turned an unwritten guard into a crash, so a test that relaxed
    it would pass against the broken version too.

    `env_val` is stubbed to the real one's *contract*, which matters more than it
    looks: it must yield empty and succeed when the variable is absent. The first
    version of this stub used `grep | head`, which exits 1 through `pipefail` and
    lets `set -e` kill the script with no output whatsoever — `grep_ok`'s own
    documented hazard in this file, reproduced inside its test. `sed -n …p` prints
    nothing and exits 0, so it cannot do that.

    `die_half_built` records its message and exits, so a test can assert on what
    the operator is actually told rather than only on the exit status.
    """
    envf = _tmp / ".env"
    envf.write_text(env_text)
    preamble = "\n".join([
        "set -euo pipefail",
        "RED=''; YELLOW=''; NC=''",
        f"WORKTREE_DIR={_tmp}",
        "BRANCH_NAME=" + shlex.quote(branch),
        # The real helper, not a stub: what it does to a hostile branch name is
        # precisely what one of these tests is about.
        "sh_quote() { printf '%q' \"$1\"; }",
        f"DB_URL_ENV='{url_env}'",
        f"DB_NAME_ENV='{name_env}'",
        f"DB_USER_ENV='{user_env}'",
        'env_val() { [[ -f "$2" ]] || return 0; '
        "sed -n \"s/^[[:space:]]*$1[[:space:]]*=//p\" \"$2\" | head -1 | tr -d '\"'; }",
        # Bash parameter expansion rather than sed: these stubs travel through a
        # Python string into `bash -c`, and the escaping needed to keep a sed regex
        # intact across both layers is its own source of bugs — it produced an
        # "unterminated `s' command" on the first attempt. Expansion needs no
        # escaping and states the intent more plainly anyway.
        'db_url_dbname() { local t="${1##*/}"; echo "${t%%\\?*}"; }',
        'db_url_user() { local t="${1#*://}"; t="${t%%@*}"; echo "${t%%:*}"; }',
        'die() { echo "DIE: $1" >&2; exit 1; }',
        'die_half_built() { echo "HALFBUILT: $1" >&2; exit 1; }',
        'PG_USER=$(env_val "$DB_USER_ENV" "$WORKTREE_DIR/.env")',
        "",
    ])
    script = preamble + dbname_block() + (
        '\necho "RESOLVED=$MAIN_DB_NAME"\necho "USER=$PG_USER"\n')
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def run_half_built(branch: str, workdir: Path) -> subprocess.CompletedProcess:
    """Run the REAL `die_half_built` and `sh_quote`, lifted out of the script.

    `run_block` above stubs `die_half_built` on purpose — it is testing the
    resolution, and wants the message without the worktree prose. These two
    functions are what the hint's safety actually lives in, so they are extracted
    and run rather than stubbed, or the test would be asserting about a copy.
    """
    src = SCRIPT.read_text()
    fns = []
    for name in ("sh_quote", "die_half_built"):
        marker = f"{name}() {{"
        assert marker in src, f"{name} is gone from create-worktree"
        fns.append(marker + src.split(marker, 1)[1].split("\n}", 1)[0] + "\n}")
    script = "\n".join([
        "set -uo pipefail",          # not -e: die_half_built exits 1 by design
        "RED=''; YELLOW=''; NC=''",
        f"WORKTREE_DIR={shlex.quote(str(workdir))}",
        "BRANCH_NAME=" + shlex.quote(branch),
        *fns,
        'die_half_built "the probe message"',
    ])
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _scratch(tmp_path):
    global _tmp
    _tmp = tmp_path
    yield


def test_a_missing_name_says_which_variables_it_tried(tmp_path):
    """The regression. Before the fix this exited on `MAIN_DB_NAME: unbound
    variable` — the guard's own dereference — and said nothing about databases at
    all, so the reader had no way to know which file or which variable was wrong.

    Asserted on the absence of the crash AND the presence of both variable names:
    a message that says only "from .env" sent someone reading the wrong two files,
    because the variables are configurable and the message did not name them."""
    r = run_block("POSTGRES_PASSWORD=secret\n")

    assert "unbound variable" not in r.stderr, (
        "set -u killed the script before its own guard could explain the failure")
    assert "HALFBUILT:" in r.stderr
    assert "DATABASE_URL" in r.stderr and "POSTGRES_DB" in r.stderr, \
        "the message must name the variables it actually tried"
    assert "--shared-db" in r.stderr, "the way out is not offered"


def test_a_url_that_is_declared_but_absent_falls_through_to_the_name(tmp_path):
    """The cascade, and the case the old `if/else` made impossible.

    Declaring `database.url_env` used to *disable* the name lookup, so a repo whose
    URL is assembled at runtime — or that keeps the name in docker-compose and only
    the password in `.env` — could never use an isolated database. Nothing about
    that repo is misconfigured; the two keys name two places one fact can live."""
    r = run_block("POSTGRES_PASSWORD=secret\nPOSTGRES_DB=mydb\nPOSTGRES_USER=u\n")

    assert r.returncode == 0, r.stderr
    assert "RESOLVED=mydb" in r.stdout


def test_a_url_wins_when_both_are_present(tmp_path):
    """Order matters and is not arbitrary: a URL is what the application actually
    connects with, so where the two disagree it is the one telling the truth."""
    r = run_block("DATABASE_URL=postgresql://u:p@h:5432/urldb\nPOSTGRES_DB=mydb\n")

    assert r.returncode == 0, r.stderr
    assert "RESOLVED=urldb" in r.stdout


def test_the_user_is_taken_from_the_url_when_the_variable_is_unset(tmp_path):
    """Unchanged behaviour, pinned because the cascade edit sits on top of it — the
    URL carries a user as well as a name, and only the name's lookup moved."""
    r = run_block("DATABASE_URL=postgresql://alice:p@h:5432/urldb\n")

    assert r.returncode == 0, r.stderr
    assert "USER=alice" in r.stdout


def test_no_url_env_configured_still_reads_the_name(tmp_path):
    """A repo that never declares `database.url_env` at all. This worked before and
    has to keep working: the cascade must not have made the name lookup conditional
    on a URL variable existing."""
    r = run_block("POSTGRES_DB=plain\nPOSTGRES_USER=u\n", url_env="")

    assert r.returncode == 0, r.stderr
    assert "RESOLVED=plain" in r.stdout


def test_a_missing_user_is_also_reported_as_half_built(tmp_path):
    """The sibling guard. It has the same problem in the same place — it fires after
    the worktree exists — so it gets the same treatment, and a name that resolves
    must not mask a user that does not."""
    r = run_block("POSTGRES_DB=mydb\n")

    assert r.returncode != 0
    assert "HALFBUILT:" in r.stderr
    assert "POSTGRES_USER" in r.stderr


def test_a_hostile_branch_name_cannot_produce_a_dangerous_paste(tmp_path):
    """Every hint this script prints is a command line meant to be COPIED, and git
    permits characters a shell parses: `$`, backtick, `;`, `>`, `&`, `|`, `(`, `'`
    are all legal in a refname and nothing in the script validates them.

    Nothing is executed by the script itself — a variable's value is never re-parsed
    inside double quotes — so this is not an injection in `create-worktree`. It is a
    hint that misbehaves in the reader's shell: `remove-worktree feat$(id)` runs
    `id` on their machine, `feat>out` truncates a file. `printf %q` is the fix, and
    it leaves an ordinary name alone so the common case stays readable.

    Found by a second model reviewing the diff, and the same shape was already at
    three older sites — including the one written into CLAUDE.local.md, which
    outlives the run that printed it. All four now go through `sh_quote`."""
    payload = "feat/x$(touch " + str(tmp_path / "PWNED") + ")"
    r = run_half_built(payload, tmp_path)

    assert "INCOMPLETE" in r.stderr, r.stderr
    assert not (tmp_path / "PWNED").exists(), \
        "the script itself executed the branch name"
    # The hint must not contain a live `$(`: that is what a paste would run.
    hint = r.stderr.split("Finish it:", 1)[1]
    assert "$(touch" not in hint, f"an unescaped substitution reached the hint: {hint!r}"
    assert "\\$\\(touch" in hint or "\\$(touch" in hint, \
        f"the substitution was neither escaped nor removed: {hint!r}"


def test_an_ordinary_branch_name_is_printed_unquoted(tmp_path):
    """The other half: `printf %q` must not make the normal case ugly. A hint nobody
    can read is a hint nobody pastes, and `remove-worktree feat/probe` is the whole
    point of printing it."""
    r = run_half_built("feat/probe", tmp_path)

    assert "remove-worktree feat/probe" in r.stderr, r.stderr


def test_the_half_built_warning_names_the_branch_not_the_directory():
    """`remove-worktree` takes a BRANCH and derives the path itself, so pasting the
    directory basename — which is what the warning's own prose names, and the first
    thing anyone reaches for — fails with a confusing "no such worktree". Asserted
    against the shipping text because this is a copy-paste path, and the first
    version of the warning got it wrong."""
    src = SCRIPT.read_text()
    warning = src.split("die_half_built() {", 1)[1].split("\n}", 1)[0]
    assert "BRANCH_NAME" in warning, "the hint does not use the branch name"
    assert 'basename "$WORKTREE_DIR"' not in warning, (
        "the hint pastes a directory into a command that takes a branch")
