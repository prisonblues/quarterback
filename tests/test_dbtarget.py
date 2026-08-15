"""The guard that decides which database the suite may destroy.

Pure functions and temporary directories: no Postgres, no HTTP client, no app
state. What they protect is the accident where a worktree's isolated database is
provisioned correctly and then ignored, so the suite rebuilds a shared one.

Every scenario runs twice: once against `tests/dbtarget.py` and once against
`harness/templates/dbtarget.py`, the copy `harness/package.nix` installs for
other repos. That copy makes decisions about other people's databases, so it is
tested rather than trusted, and a parity test at the bottom pins the two files
byte-for-byte so they cannot drift into disagreeing.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from urllib.parse import urlsplit, urlunsplit

import pytest

from . import dbtarget

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "harness" / "templates" / "dbtarget.py"

SHARED_START = "# --- shared guard"
SHARED_END = "# --- end shared guard"

#: Git run with the ambient configuration switched off, so a global `gpgsign`,
#: hooksPath or template dir cannot make these fixtures fail or hang.
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _load_template() -> ModuleType:
    spec = importlib.util.spec_from_file_location("harness_templates_dbtarget", TEMPLATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass resolves its string annotations through
    # sys.modules, and a module absent from it raises on the class definition.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TEMPLATE = _load_template()


@pytest.fixture(params=[dbtarget, TEMPLATE], ids=["tests/dbtarget", "harness/templates/dbtarget"])
def guard(request) -> ModuleType:
    """Each test, run against both copies of the guard."""
    return request.param


# --- fixtures: URLs and fake checkouts ---------------------------------------


def _on_same_server(url: str, database: str) -> str:
    """`url` with a different database name — a separate database, same server."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", "", ""))


@pytest.fixture
def main_url(guard) -> str:
    """The database the main checkout uses, in this guard's own dialect."""
    return guard.DEV_FALLBACK_URL


@pytest.fixture
def wt_url(guard) -> str:
    return _on_same_server(guard.DEV_FALLBACK_URL, "app_fix_x")


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    )
    return done.stdout


def _repo(tmp_path: Path, name: str = "app") -> Path:
    """A real git repository with one commit, so `git worktree add` works."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("x\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "init")
    return repo


def _worktree_of(main: Path, name: str, url: str | None) -> Path:
    wt = main.parent / f"{main.name}-{name}"
    _git(main, "worktree", "add", "-q", "-b", name, str(wt))
    _write_env(wt, url)
    return wt


def _write_env(checkout: Path, url: str | None, var: str = "DATABASE_URL") -> None:
    if url is not None:
        (checkout / ".env").write_text(f"{var}={url}\n")


def _fake_main(tmp_path: Path, url: str | None = None) -> Path:
    """A main checkout as it looks on disk, without git being able to confirm it."""
    fake = tmp_path / "fake-main"
    (fake / ".git").mkdir(parents=True)
    _write_env(fake, url)
    return fake


def _fake_worktree(tmp_path: Path, pointer: str, url: str | None = None) -> Path:
    """A worktree whose `.git` pointer says exactly `pointer`."""
    wt = tmp_path / "fake-wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {pointer}\n")
    _write_env(wt, url)
    return wt


@pytest.fixture(autouse=True)
def _tmp_path_is_outside_any_repo(tmp_path):
    # The hand-written `.git` pointers below only exercise the fallback path if
    # git genuinely cannot answer for tmp_path. If pytest's temp root ever moved
    # inside a checkout, those tests would quietly stop testing what they say.
    done = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "--git-dir"], capture_output=True
    )
    assert done.returncode != 0, "tmp_path is inside a git repository"


# --- reading .env ------------------------------------------------------------


def test_env_file_value_handles_quotes_comments_and_reassignment(guard, tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        "# DATABASE_URL=commented-out\n"
        'OTHER = "x"\n'
        "DATABASE_URL='first'\n"
        "  DATABASE_URL =  second  \n"
    )
    assert guard.env_file_value("DATABASE_URL", f) == "second"  # last assignment wins
    assert guard.env_file_value("OTHER", f) == "x"
    assert guard.env_file_value("ABSENT", f) is None
    assert guard.env_file_value("DATABASE_URL", tmp_path / "nope") is None


def test_env_file_value_matches_dotenv_on_export_and_inline_comments(guard, tmp_path):
    # Both spellings are ordinary in a hand-edited .env, and both used to resolve
    # to something the application's own loader would not agree with: the export
    # line fell through to the fallback (the main database), and the comment was
    # kept as part of the URL.
    f = tmp_path / ".env"
    f.write_text("export DATABASE_URL=postgresql://h/db   # the worktree's own\n")
    assert guard.env_file_value("DATABASE_URL", f) == "postgresql://h/db"


def test_env_file_value_never_invents_a_value_by_stripping_odd_quotes(guard, tmp_path):
    # `strip("\"'")` takes any mix of both characters off each end, so it reads
    # `"mismatched'` as `mismatched` and truncates a value that legitimately
    # ends in a quote. Either answer is a URL the application would not agree
    # with, which is the one thing this module must never produce: the full
    # literal or nothing, never a quietly shortened version.
    mismatched = tmp_path / "mismatched.env"
    mismatched.write_text("A=\"mismatched'\n")
    assert guard.env_file_value("A", mismatched) in (None, "\"mismatched'")

    quoted = tmp_path / "quoted.env"
    quoted.write_text("B=\"ends-in-a-quote'\"\n")
    assert guard.env_file_value("B", quoted) == "ends-in-a-quote'"


def test_env_file_value_ignores_a_directory_in_place_of_the_file(guard, tmp_path):
    (tmp_path / ".env").mkdir()
    assert guard.env_file_value("DATABASE_URL", tmp_path / ".env") is None


# --- identifying a database --------------------------------------------------


def test_database_name_strips_host_and_query(guard):
    assert guard.database_name("postgresql://u:p@h:5432/db?sslmode=require") == "db"
    assert guard.database_name("postgresql://u:p@h:5432/my%20db") == "my db"


def test_a_url_with_no_database_path_names_no_database(guard):
    # Splitting on the last "/" returns the *host* here, which then compares
    # equal to any database that happens to share the host's name.
    assert guard.database_name("postgresql://localhost") == ""
    assert guard.database_name("postgresql://localhost:5435/") == ""


def test_endpoint_identifies_a_database_without_its_credentials(guard, main_url):
    parts = urlsplit(main_url)
    server = f"{parts.hostname}:{parts.port}"
    anonymous = urlunsplit((parts.scheme, server, parts.path, "", ""))
    other_user = urlunsplit((parts.scheme, f"admin:hunter2@{server}", parts.path, "", ""))
    assert guard.endpoint(main_url) == guard.endpoint(anonymous) == guard.endpoint(other_user)
    assert guard.endpoint(main_url) == ("localhost", parts.port, parts.path.lstrip("/"))


def test_equivalent_spellings_of_one_endpoint_are_one_database(guard):
    same = [
        "postgresql://u:p@localhost:5432/app",
        "postgresql://u:p@127.0.0.1:5432/app",
        "postgresql://u:p@LOCALHOST.:5432/app",
        "postgresql+asyncpg://u:p@localhost/app",  # port omitted: libpq's 5432
        "postgresql://u:p@[::1]:5432/app",
        "postgresql://u:p@localhost:5432/%61pp",  # percent-encoded name
    ]
    for url in same:
        assert guard.same_database(url, same[0]), url


def test_same_name_on_another_server_is_a_different_database(guard):
    # Refusing these would block a legitimate run: same name, different host or
    # port means different data.
    base = "postgresql://u:p@localhost:5435/app"
    assert not guard.same_database(base, "postgresql://u:p@db.internal:5435/app")
    assert not guard.same_database(base, "postgresql://u:p@localhost:5999/app")
    assert not guard.same_database(base, "postgresql://u:p@localhost:5435/other")
    # …but different credentials on the same server are the same database.
    assert guard.same_database(base, "postgresql://admin:hunter2@localhost:5435/app")


def test_a_url_it_cannot_read_is_assumed_to_collide(guard):
    # Fail closed: this answer gates a refusal that prevents unrecoverable loss,
    # so "can't tell" has to mean "assume they are the same database".
    assert guard.same_database("postgresql://localhost", "postgresql://localhost:5435/app")
    assert guard.same_database("postgresql://localhost:not-a-port/app", "postgresql://h:5435/app")


# --- redaction ---------------------------------------------------------------


def test_redact_stars_out_the_password_in_either_spelling(guard):
    userinfo = guard.redact("postgresql://u:hunter2@h:5432/db")
    assert "hunter2" not in userinfo and ":***@" in userinfo
    in_query = guard.redact("postgresql://h:5432/db?password=hunter2&sslmode=require")
    assert "hunter2" not in in_query and "password=***" in in_query
    assert "sslmode=require" in in_query


def test_redact_leaves_a_credential_free_url_alone(guard):
    plain = "postgresql://localhost:5435/db"
    user_only = "postgresql://user@localhost:5435/db"
    assert guard.redact(plain) == plain
    assert guard.redact(user_only) == user_only


# --- where a checkout sits in its repository ---------------------------------


def test_a_worktree_knows_its_main_checkout_and_its_siblings(guard, tmp_path, main_url, wt_url):
    main = _repo(tmp_path)
    _write_env(main, main_url)
    a = _worktree_of(main, "a", wt_url)
    _worktree_of(main, "b", None)

    layout = guard.repo_layout(a)
    assert layout.linked is True
    assert layout.main == main.resolve()
    assert set(layout.others) == {main.resolve(), (main.parent / f"{main.name}-b").resolve()}
    assert guard.repo_layout(main).linked is False


def test_a_worktree_of_a_bare_clone_is_understood_rather_than_guessed(guard, tmp_path, wt_url):
    # The pointer scan looked for a `.git` path component and found none here,
    # then returned "not a worktree" — which let the run through.
    origin = _repo(tmp_path)
    bare = tmp_path / "app.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(origin), str(bare)],
        check=True,
        capture_output=True,
        env=GIT_ENV,
    )
    wt = tmp_path / "bare-wt"
    _git(bare, "worktree", "add", "-q", "-b", "wt", str(wt))
    _write_env(wt, wt_url)

    layout = guard.repo_layout(wt)
    assert layout.linked is True
    assert layout.main is None  # a bare clone has no working tree of its own
    assert layout.others == ()
    # Nothing to collide with, so a run here is allowed rather than refused.
    assert guard.isolation_error(wt_url, wt) is None


def test_a_relative_gitdir_pointer_resolves_against_the_checkout(guard, tmp_path, monkeypatch):
    # Git writes a relative pointer in some setups, and it is relative to the
    # directory holding the .git file — not to wherever pytest was invoked from.
    main = _fake_main(tmp_path)
    wt = _fake_worktree(tmp_path, "../fake-main/.git/worktrees/wt")
    monkeypatch.chdir(tmp_path)  # where the same relative path means something else
    assert guard.repo_layout(wt).main == main.resolve()


def test_an_unreadable_pointer_refuses_rather_than_shrugs(guard, tmp_path, main_url):
    # Fail closed: a .git file means this IS a worktree, so "whose database is
    # that?" going unanswered must not resolve to "nobody's".
    wt = _fake_worktree(tmp_path, "", url=main_url)
    (wt / ".git").write_text("this is not a gitdir pointer\n")
    problem = guard.isolation_error(main_url, wt)
    assert problem is not None
    assert "cannot tell which checkout owns" in problem


def test_a_directory_that_is_not_a_checkout_is_not_a_worktree(guard, tmp_path):
    plain = tmp_path / "tarball"
    plain.mkdir()
    assert guard.repo_layout(plain).linked is False


# --- resolving the target ----------------------------------------------------


def test_explicit_env_var_wins_over_the_checkout_env_file(guard, tmp_path, wt_url):
    wt = _fake_worktree(tmp_path, str(tmp_path / "fake-main/.git/worktrees/wt"), url=wt_url)
    pinned = _on_same_server(wt_url, "pinned")
    assert guard.resolve_database_url({"DATABASE_URL": pinned}, wt) == (pinned, "DATABASE_URL")


def test_checkout_env_file_is_used_when_the_environment_is_silent(guard, tmp_path, wt_url):
    # The whole point: create-worktree writes the isolated database into .env,
    # and an empty environment must not override it with the main database.
    wt = _fake_worktree(tmp_path, str(tmp_path / "fake-main/.git/worktrees/wt"), url=wt_url)
    assert guard.resolve_database_url({}, wt) == (wt_url, ".env")


def test_dev_fallback_only_when_nothing_is_configured(guard, tmp_path):
    main = _fake_main(tmp_path)
    assert guard.resolve_database_url({}, main) == (guard.DEV_FALLBACK_URL, "fallback")


# --- the refusal itself ------------------------------------------------------


def test_worktree_pointing_at_the_main_database_is_refused(guard, tmp_path, main_url):
    main = _repo(tmp_path)
    _write_env(main, main_url)
    wt = _worktree_of(main, "wt", main_url)  # isolation silently didn't happen

    problem = guard.isolation_error(main_url, wt)
    assert problem is not None
    assert f"database '{guard.database_name(main_url)}'" in problem
    assert str(wt) in problem and str(main.resolve()) in problem
    assert "the main checkout" in problem


def test_worktree_falling_back_to_the_dev_default_is_refused(guard, tmp_path):
    # No .env anywhere: the fallback IS the main checkout's database, in both.
    main = _repo(tmp_path)
    wt = _worktree_of(main, "wt", None)
    url, source = guard.resolve_database_url({}, wt)
    assert source == "fallback"
    assert guard.isolation_error(url, wt) is not None


def test_worktree_pointing_at_a_sibling_worktrees_database_is_refused(guard, tmp_path, wt_url):
    # Trivially produced by copying a .env between worktrees, or by rebuilding a
    # worktree on a branch name that was used before. The sibling's data is as
    # unrecoverable as the main checkout's.
    main = _repo(tmp_path)
    _write_env(main, guard.DEV_FALLBACK_URL)
    _worktree_of(main, "a", wt_url)
    b = _worktree_of(main, "b", wt_url)

    problem = guard.isolation_error(wt_url, b)
    assert problem is not None
    assert "a sibling worktree" in problem
    assert str((main.parent / "app-a").resolve()) in problem


def test_a_sibling_without_an_env_file_claims_no_database(guard, tmp_path, wt_url):
    # It has not been provisioned, so it owns nothing — and it would refuse to
    # run itself. Treating its silence as a claim would block every worktree.
    main = _repo(tmp_path)
    _write_env(main, guard.DEV_FALLBACK_URL)
    _worktree_of(main, "a", None)
    b = _worktree_of(main, "b", wt_url)
    assert guard.isolation_error(wt_url, b) is None


def test_worktree_with_its_own_database_is_allowed(guard, tmp_path, wt_url):
    main = _repo(tmp_path)
    _write_env(main, guard.DEV_FALLBACK_URL)
    wt = _worktree_of(main, "wt", wt_url)
    assert guard.isolation_error(wt_url, wt) is None


def test_main_checkout_may_rebuild_its_own_database(guard, tmp_path, main_url):
    # Documented behaviour, not an accident: the dev database is the main
    # checkout's to rebuild, and refusing there would block the normal run.
    main = _repo(tmp_path)
    _write_env(main, main_url)
    assert guard.isolation_error(main_url, main) is None


def test_an_equivalent_spelling_does_not_get_past_the_refusal(guard, tmp_path):
    # The bypass this guard is most likely to meet: the same database written
    # with the other loopback name and the port left implicit.
    main = _repo(tmp_path)
    _write_env(main, "postgresql://u:p@localhost:5432/app")
    wt = _worktree_of(main, "wt", "postgresql+asyncpg://u:p@127.0.0.1/app")
    assert guard.isolation_error("postgresql+asyncpg://u:p@127.0.0.1/app", wt) is not None


def test_guard_uses_the_main_env_not_a_hardcoded_name(guard, tmp_path, wt_url):
    # A repo whose main .env names a non-default database is still protected.
    other = _on_same_server(guard.DEV_FALLBACK_URL, "board_dev")
    main = _repo(tmp_path)
    _write_env(main, other)
    wt = _worktree_of(main, "wt", other)
    assert guard.isolation_error(other, wt) is not None
    assert guard.isolation_error(wt_url, wt) is None


def test_refusal_does_not_print_the_password(guard, tmp_path):
    secret = "postgresql+asyncpg://app:hunter2@localhost:5435/app?password=hunter2"
    main = _repo(tmp_path)
    _write_env(main, secret)
    wt = _worktree_of(main, "wt", secret)
    problem = guard.isolation_error(secret, wt)
    assert problem is not None
    assert "hunter2" not in problem
    assert ":***@" in problem and "password=***" in problem


# --- the two copies stay one copy --------------------------------------------


def _shared_region(path: Path) -> str:
    text = path.read_text()
    start = text.index(SHARED_START)
    end = text.index(SHARED_END)
    return text[text.index("\n", start) + 1 : end]


def _import_lines(path: Path) -> list[str]:
    return [
        line
        for line in path.read_text().splitlines()
        if line.startswith(("import ", "from ", "try:", "except ImportError"))
        and not line.startswith("from __future__")
    ]


def test_the_shipped_template_is_this_guard_byte_for_byte():
    # package.nix installs the template, so its parsing runs against databases
    # this repo will never see. Keeping it a copy rather than a reimplementation
    # is what lets the tests above cover it; this is the assertion that keeps it
    # one. Both files are generated from the same body — if this fails, the two
    # were edited separately and the fix is to apply the change to both.
    assert _shared_region(TEMPLATE_PATH) == _shared_region(REPO_ROOT / "tests" / "dbtarget.py")
    assert _import_lines(TEMPLATE_PATH) == _import_lines(REPO_ROOT / "tests" / "dbtarget.py")


def test_the_template_differs_only_in_the_constants_it_tells_you_to_change():
    # The flip side: the template must NOT ship quarterback's own database name,
    # or a repo that adopts it protects the wrong thing.
    assert TEMPLATE.DEV_FALLBACK_URL != dbtarget.DEV_FALLBACK_URL
    assert "quarterback" not in TEMPLATE.DEV_FALLBACK_URL
    assert "ADAPT ME" in TEMPLATE_PATH.read_text()


def test_the_target_database_is_announced_even_under_q():
    # Run for real rather than asserted about, because the two ways of getting
    # this wrong are both invisible from inside the process: `-q` suppresses
    # pytest_report_header entirely, and a conftest's own pytest_configure runs
    # before the terminal reporter has registered itself. --collect-only, so
    # nothing is rebuilt; DATABASE_URL is inherited, so the child targets the
    # same database this session already resolved.
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", "tests/test_dbtarget.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=os.environ,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "database: " in done.stdout


def test_the_template_is_importable_as_shipped():
    # It used to be called conftest-db-isolation.py: a name Python cannot import
    # and pytest does not auto-load, so "drop this into your tests/" left it
    # inert and only the copy-paste path ever worked.
    assert TEMPLATE_PATH.name == "dbtarget.py"
    assert TEMPLATE.__name__ == "harness_templates_dbtarget"
