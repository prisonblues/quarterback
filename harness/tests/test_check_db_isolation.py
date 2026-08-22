"""The isolation check `/fix-issue` runs on every route, and the brief that has to run it.

#340 has two causes and only one of them is a flag.

**The flag.** Step 2 asked the agent to classify its change — "read-only / no DB → shared
DB is fine and faster" — and step 7 then ran the full suite unconditionally. The suite's
teardown truncates, so a correct classification produced a worktree that `tests/dbtarget.py`
refused to run in. The property that decides is not "does my change touch the DB" but "will
anything I run truncate it", and step 7 answers that yes, every time. So `--shared-db` is
gone from the brief.

**The one that bites harder.** `feat/issue-85` reached the shared database with nobody
choosing `--shared-db` at all: `/fix-issue` **reused** an existing worktree, weeks old,
from before per-worktree databases existed. `create-worktree` refuses an existing directory,
so nothing provisioned anything and the agent inherited a pre-#30 `.env`. The brief's
isolation check could not catch it — it read `create-worktree`'s output for the residual-var
warning, so it only ran when `create-worktree` ran, which on that route it had not. A check
conditional on the safe path having been taken is not a check.

Six of 38 worktrees carrying a `.env` on one box named the shared database, five of them
besides the main checkout. Nothing counted either number until somebody looked.

So this suite is in two halves, which is deliberate and which is why it lives in
`worktree-tests` rather than with the prose suites: the mechanism (`check-db-isolation`,
driven against real git worktrees) and the shipped brief that calls it. Splitting them by
which tool each half needs would leave the coupling — a command nobody runs, or a brief
calling a command that does not exist — asserted in neither.

Run: pytest harness/tests
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys

from pathlib import Path

import pytest

import _flake_sandbox

HARNESS = Path(__file__).resolve().parents[1]
REPO_ROOT = HARNESS.parent
SCRIPT = HARNESS / "bin" / "check-db-isolation"
BRIEF = HARNESS / "commands" / "fix-issue.md"
FLAKE = REPO_ROOT / "flake.nix"

#: Paths outside `harness/bin` and `harness/tests` that this suite cannot run without.
#:
#: `harness/commands/fix-issue.md` is read here directly; `harness/templates` is read by the
#: script under test, which imports `dbtarget.py` out of it — a missing template is not a
#: failed assertion, it is `check-db-isolation` exiting before it checks anything and every
#: behavioural test below failing for a reason unrelated to the code under test. Neither was
#: in `worktree-tests`'s sandbox before this suite, which is #163's mechanism exactly: a read
#: nobody installed does not fail there, it ERRORS on a missing file in a build no workflow
#: runs.
READS = ("harness/commands/fix-issue.md", "harness/templates")

#: The check that runs this suite. Written out rather than discovered, for the reason
#: `test_claude_wiring.py` gives: a renamed check has to be an error here rather than an
#: empty comparison that reports everything as fine.
CHECK_NAME = "worktree-tests"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

MAIN_URL = "postgresql+asyncpg://app:pw@localhost:5435/myapp"
OWN_URL = "postgresql+asyncpg://app:pw@localhost:5435/myapp_fix_issue_340"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def main_checkout(tmp_path: Path) -> Path:
    """A repository with one commit and a `.env` naming the main database."""
    main = tmp_path / "myapp"
    main.mkdir()
    git(main, "init", "-q")
    git(main, "config", "user.email", "t@example.com")
    git(main, "config", "user.name", "t")
    (main / "README").write_text("x\n")
    git(main, "add", "README")
    git(main, "commit", "-qm", "first")
    (main / ".env").write_text(f"DATABASE_URL={MAIN_URL}\n")
    return main


def worktree(main: Path, name: str, env: str) -> Path:
    """A linked worktree of `main` carrying `env` as its `.env`.

    Made with real `git worktree add`, because what the script asks git is which
    checkouts exist and which one holds the shared `.git` — a fabricated directory
    with a hand-written pointer file would be testing the fallback path instead of
    the one every worktree on the box uses.
    """
    path = main.parent / f"myapp-{name}"
    git(main, "worktree", "add", "-q", "-b", name, str(path))
    (path / ".env").write_text(env)
    return path


def check(checkout: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the script as it ships — no `QB_DBTARGET`, so it resolves `dbtarget.py` itself.

    `sys.executable` rather than the shebang: in a nix sandbox there is no
    `/usr/bin/env` until `patchShebangs` has run, and a test that fails on exec
    says nothing about the code under test.
    """
    return subprocess.run([sys.executable, str(SCRIPT), *args, str(checkout)],
                          capture_output=True, text=True)


# ---- the mechanism ---------------------------------------------------------


def test_a_reused_worktree_naming_the_main_database_is_refused(main_checkout):
    """The reconstruction of #340's second cause.

    A worktree that already existed, whose `.env` still names the main database because
    nothing ever re-provisioned it. Every decision above this point can be correct and
    the worktree is still pointed at data that belongs to somebody else.
    """
    stale = worktree(main_checkout, "feat-issue-85", f"DATABASE_URL={MAIN_URL}\n")

    got = check(stale)

    assert got.returncode == 1, got.stdout + got.stderr
    assert "REFUSING" in got.stderr
    assert "myapp" in got.stderr


def test_a_worktree_with_its_own_database_is_allowed(main_checkout):
    """The other half, and the one that has to stay quiet: a check that refused the
    normal case would be switched off within a day."""
    fresh = worktree(main_checkout, "fix-issue-340", f"DATABASE_URL={OWN_URL}\n")

    got = check(fresh)

    assert got.returncode == 0, got.stdout + got.stderr
    assert "myapp_fix_issue_340" in got.stdout


def test_the_main_checkout_is_never_refused(main_checkout):
    """It owns that database. `tests/dbtarget.py` draws the same line and for the same
    reason: a main checkout rebuilding its own dev database is the documented behaviour."""
    got = check(main_checkout)

    assert got.returncode == 0, got.stdout + got.stderr
    assert "main checkout" in got.stdout


def test_a_residual_variable_is_caught_even_when_the_url_was_repointed(main_checkout):
    """`create-worktree`'s own safety net, moved somewhere it can run later.

    The provisioner rewrites every conventional DB-name variable it knows, but the bug
    class it warns about is the one it does NOT know — an app reading `PGDATABASE` while
    the URL was the only thing repointed. That worktree looks isolated in the line
    anybody reads and is not.
    """
    mixed = worktree(main_checkout, "residual",
                     f"DATABASE_URL={OWN_URL}\nPGDATABASE=myapp\n")

    got = check(mixed)

    assert got.returncode == 1, got.stdout + got.stderr
    assert "PGDATABASE" in got.stderr, "the refusal must name the variable that collides"


def test_a_siblings_database_collides_as_surely_as_the_mains(main_checkout):
    """A `.env` copied from one worktree to another, or a worktree rebuilt on a branch
    name that was used before. The sibling's data is exactly as unrecoverable, and the
    sibling is exactly as unlikely to be looking."""
    worktree(main_checkout, "first", f"DATABASE_URL={OWN_URL}\n")
    second = worktree(main_checkout, "second", f"DATABASE_URL={OWN_URL}\n")

    got = check(second)

    assert got.returncode == 1, got.stdout + got.stderr
    assert "sibling worktree" in got.stderr
    assert "myapp-first" in got.stderr, "the refusal must name which checkout owns it"


def test_a_host_alias_does_not_hide_a_collision(main_checkout):
    """`127.0.0.1` and `localhost` name one server. Comparing URLs as text is a hole,
    and this asserts the script gets its comparison from `dbtarget` rather than doing
    its own — the whole point of importing it."""
    aliased = worktree(main_checkout, "aliased",
                       "DATABASE_URL=postgresql://app:pw@127.0.0.1:5435/myapp\n")

    got = check(aliased)

    assert got.returncode == 1, got.stdout + got.stderr


def test_a_different_port_is_a_different_database(main_checkout):
    """The converse. A second Postgres on another port is a different server, and a
    guard that refused it would be refusing correct setups."""
    other = worktree(main_checkout, "other-server",
                     "DATABASE_URL=postgresql://app:pw@localhost:5999/myapp\n")

    got = check(other)

    assert got.returncode == 0, got.stdout + got.stderr


def test_an_empty_value_is_not_read_as_a_collision(main_checkout):
    """`same_database` fails closed — an unparseable target is assumed to collide — so an
    empty `PGDATABASE=` left in two `.env` files would refuse every worktree on the box.
    Empty values name no database and are dropped before the comparison."""
    empty = worktree(main_checkout, "empty-var", f"DATABASE_URL={OWN_URL}\nPGDATABASE=\n")
    (main_checkout / ".env").write_text(f"DATABASE_URL={MAIN_URL}\nPGDATABASE=\n")

    got = check(empty)

    assert got.returncode == 0, got.stdout + got.stderr


def test_a_worktree_with_no_env_is_refused_when_another_checkout_has_one(main_checkout):
    """"Names nothing" is not "targets nothing", which the first cut of this script got
    wrong and a second model reading the diff caught.

    An application whose `.env` is silent falls back to a value compiled into it, and that
    value is the dev database by construction — `dbtarget.DEV_FALLBACK_URL` is exactly that
    and its own comment says a worktree reaching it is the bug. Exiting 0 here would have
    been the check reporting "nothing to see" about a worktree pointed straight at the main
    database, which is the failure it exists to prevent wearing a different hat."""
    bare = worktree(main_checkout, "no-env", "")
    (bare / ".env").unlink()

    got = check(bare)

    assert got.returncode == 1, got.stdout + got.stderr
    assert "no database at all" in got.stderr
    assert "falls back" in got.stderr, "the refusal has to say why an absent .env is a target"


def test_a_repository_with_no_database_anywhere_stays_quiet(main_checkout):
    """The other half, and the reason the rule above is "while another checkout names one"
    rather than "always". A repo with no database has nothing to protect, and a check that
    refused every worktree of it would be noise with a stop attached."""
    (main_checkout / ".env").unlink()
    bare = worktree(main_checkout, "db-less", "")
    (bare / ".env").unlink()

    got = check(bare)

    assert got.returncode == 0, got.stdout + got.stderr
    assert "nothing to check" in got.stdout


def test_a_variable_declared_in_worktree_json_is_read(main_checkout):
    """`.worktree.json`'s `database.name_env` is what `create-worktree` rewrites for a repo
    whose database name lives somewhere non-conventional. A check that read only the
    conventional five would wave through precisely the repo that configured itself."""
    (main_checkout / ".worktree.json").write_text('{"database": {"name_env": "MYAPP_DB"}}\n')
    (main_checkout / ".env").write_text("MYAPP_DB=myapp\n")
    odd = worktree(main_checkout, "declared", "MYAPP_DB=myapp\n")

    got = check(odd)

    assert got.returncode == 1, got.stdout + got.stderr
    assert "MYAPP_DB" in got.stderr


def test_the_refusal_says_what_to_do_about_it(main_checkout):
    """A refusal that stops an agent without telling it the way out gets worked around,
    and the way out here is two commands nobody remembers under pressure."""
    stale = worktree(main_checkout, "stale", f"DATABASE_URL={MAIN_URL}\n")

    got = check(stale)

    assert "remove-worktree" in got.stderr and "create-worktree" in got.stderr


def test_the_way_out_names_the_branch_not_the_directory(main_checkout):
    """`remove-worktree` takes a BRANCH and derives the path itself, so a hint carrying the
    directory basename fails with a confusing "no such worktree" — which is what the first
    draft of this message printed. `create-worktree`'s half-built warning made the identical
    mistake and `test_create_worktree_db_name.py` pins it there; the same paste, the same
    trap, so the same assertion."""
    stale = worktree(main_checkout, "hinting", f"DATABASE_URL={MAIN_URL}\n")

    got = check(stale)

    assert "remove-worktree hinting" in got.stderr, got.stderr
    assert "remove-worktree myapp-hinting" not in got.stderr, (
        "the hint pastes a directory into a command that takes a branch")


def test_the_refusal_does_not_print_the_password(main_checkout):
    """It is read off a terminal and kept in CI logs. `dbtarget.redact` exists for this;
    the point of the assertion is that the script actually calls it."""
    stale = worktree(main_checkout, "secretive", f"DATABASE_URL={MAIN_URL}\n")

    got = check(stale)

    assert "pw@" not in got.stderr, got.stderr
    assert "***" in got.stderr


def test_a_second_argument_is_a_usage_error(main_checkout):
    """It takes one checkout. Silently ignoring the rest is how a flag somebody invented
    gets read as consent."""
    got = subprocess.run([sys.executable, str(SCRIPT), str(main_checkout), "--yes-really"],
                         capture_output=True, text=True)

    assert got.returncode == 2, got.stdout + got.stderr


def test_it_imports_the_guard_rather_than_reimplementing_it():
    """The coupling that makes the two answers the same answer.

    `tests/dbtarget.py` refuses at pytest start and this refuses before the work; if they
    disagree about what "the same database" means, the earlier one is worse than nothing —
    it is a green light. So the comparison has exactly one implementation, and a copy of
    `same_database` appearing here is the thing to fail on.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    assert "dbtarget" in source, "the script no longer imports the shared guard"
    for borrowed in ("def same_database", "def endpoint", "def database_name", "def redact"):
        assert borrowed not in source, (
            f"{borrowed} is re-implemented in check-db-isolation instead of imported from "
            "dbtarget.py — two implementations of this comparison is two answers")


def test_the_shipped_dbtarget_is_where_the_script_looks_for_it():
    """The source-tree half of `load_dbtarget`'s candidate list, asserted against the tree
    rather than trusted. The store-path half cannot be checked from here; this is the one
    that breaks if `templates/` moves."""
    assert (HARNESS / "templates" / "dbtarget.py").is_file(), (
        "check-db-isolation resolves dbtarget.py as ../templates/dbtarget.py relative to "
        "harness/bin — that file has moved and the script will exit before checking anything")


# ---- the brief that has to run it ------------------------------------------


@pytest.fixture(scope="module")
def brief() -> str:
    assert BRIEF.is_file(), (
        f"{BRIEF} has moved — this half of the suite is now green about nothing. If it "
        "moved on purpose, update BRIEF and flake.nix's worktree-tests install line.")
    return BRIEF.read_text(encoding="utf-8")


def test_the_brief_never_asks_create_worktree_for_a_shared_database(brief):
    """Cause 1. The flag is meaningful on `create-worktree` — a caller that genuinely runs
    no suite — so what is asserted is that THIS brief never passes it, not that the word
    is unmentionable. It is mentioned, in the paragraph explaining why it is not used."""
    invocations = re.findall(r"create-worktree[^\n`]*", brief)
    offenders = [line for line in invocations if "--shared-db" in line]
    assert not offenders, (
        f"fix-issue.md still passes --shared-db to create-worktree: {offenders}. Step 7 runs "
        "the full suite unconditionally and its teardown truncates, so a shared database is "
        "never safe here — #340")
    argline = next(line for line in brief.splitlines() if line.startswith("@arguments"))
    assert "--shared-db" not in argline and "--isolated-db" not in argline, (
        f"the brief still advertises a DB mode in its arguments: {argline}")


def test_the_brief_does_not_ask_the_question_that_does_not_decide(brief):
    """"Read-only / no DB → shared DB is fine" was the instruction, and it was followed
    correctly. Classifying the CHANGE cannot answer a question about what the RUN executes,
    so the classification is gone rather than tightened — a more conservative version of a
    question that does not decide the outcome is still a question that does not decide it."""
    assert "DB_MODE" not in brief, (
        "fix-issue.md still records a DB_MODE, so there is still a shared-database branch "
        "for step 7's mandatory suite run to fall into")


def test_the_brief_runs_the_check_on_the_resolved_worktree(brief):
    """Cause 2. Not "scan create-worktree's output", not "verify isolation" as prose: a
    command, on `$WT_DIR`, whose exit status the brief is told to obey."""
    assert re.search(r'check-db-isolation "\$WT_DIR"', brief), (
        "fix-issue.md does not run `check-db-isolation \"$WT_DIR\"`. The isolation check has "
        "to be on the resolved .env, whatever route produced the worktree — #340")
    assert "STOP" in brief.split("check-db-isolation", 1)[1][:800], (
        "the brief runs the check but does not say a non-zero exit stops the run, which is "
        "the only part of it that does anything")


def test_the_check_is_not_conditional_on_create_worktree_having_run(brief):
    """The precise shape of the bug. The old check began "After `create-worktree` runs, scan
    its output" — so the route where nothing provisioned a database, and the `.env` is
    therefore least trustworthy, was the one route that skipped it."""
    step3 = brief.split("## 3.", 1)[1].split("## 4.", 1)[0]
    assert "scan its output" not in step3, (
        "the isolation check still reads create-worktree's output, so a reused worktree "
        "skips it entirely")
    assert "skip to step 4" not in step3, (
        "the epic-driver route still jumps past the isolation check. Every route ends at the "
        "check — an inherited worktree's database was provisioned by somebody else, which is "
        "an assumption to test rather than a reason to skip")


@pytest.mark.parametrize("path", READS)
def test_the_flake_check_supplies_what_this_suite_needs(path: str):
    """The coupling guard for this file's own sandbox — `test_claude_wiring.py`'s, applied to
    a suite that adds two paths `worktree-tests` never held.

    Through `_flake_sandbox` rather than a regex of this file's own, which is what that module
    was factored out for: a copy line inside a comment, `${ ./x }` spacing, a destination that
    does not mirror its source — one parser to get those right, not four."""
    region = _flake_sandbox.check_region(FLAKE.read_text(encoding="utf-8"), CHECK_NAME)
    pairs = _flake_sandbox.copies(region)
    # prefix="": this check builds `harness/…` at the top level and `cd harness`, where the
    # prose and release-metadata sandboxes build a `repo/` tree.
    assert not _flake_sandbox.misdirected(pairs, prefix=""), \
        _flake_sandbox.misdirected(pairs, prefix="")
    assert _flake_sandbox.supplied_by(path, set(pairs)), (
        f"flake.nix's {CHECK_NAME} sandbox does not supply {path}, which this suite needs. "
        f"Add a `cp -r`/`install -D` line for it beside the others, or the assertions about "
        f"it error on a missing file instead of being evaluated (#163).")


def test_every_declared_read_exists():
    """A declaration pointing at a file nobody has — what catches a rename that updated the
    flake and the constants but not this list."""
    assert (REPO_ROOT / "harness" / "commands" / "fix-issue.md").exists()
    assert (REPO_ROOT / "harness" / "templates").is_dir()


def test_the_brief_says_reuse_must_re_verify(brief):
    """Salvaging an abandoned branch is a normal thing to want and was the right call for
    #85/#86. The rule is not "never reuse"; it is that reuse re-verifies. A brief that
    forbade it would be worked around, and the worked-around version has no check in it."""
    step3 = brief.split("## 3.", 1)[1].split("## 4.", 1)[0]
    assert re.search(r"[Rr]euse", step3), "step 3 no longer describes the reuse route at all"
    assert "already exists" in step3, (
        "step 3 does not tell the agent what create-worktree's refusal looks like, which is "
        "the moment the reuse decision actually gets made")
