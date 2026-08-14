"""The guard that decides which database the suite may destroy.

Pure-function tests: no database, no app. What they protect is the accident
where a worktree's isolated database is provisioned correctly and then ignored,
so the suite rebuilds the shared one instead.
"""

from __future__ import annotations

from pathlib import Path

from .dbtarget import (
    DEV_FALLBACK_URL,
    database_name,
    env_file_value,
    isolation_error,
    main_checkout,
    resolve_database_url,
)

MAIN_URL = "postgresql+asyncpg://quarterback:quarterback@localhost:5435/quarterback"
WT_URL = "postgresql+asyncpg://quarterback:quarterback@localhost:5435/quarterback_fix_x"


def _main_repo(tmp_path: Path, env: str | None = MAIN_URL) -> Path:
    repo = tmp_path / "quarterback"
    (repo / ".git").mkdir(parents=True)
    if env is not None:
        (repo / ".env").write_text(f"DATABASE_URL={env}\n")
    return repo


def _worktree(tmp_path: Path, main: Path, name: str = "wt", env: str | None = WT_URL) -> Path:
    wt = tmp_path / f"quarterback-{name}"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/{name}\n")
    if env is not None:
        (wt / ".env").write_text(f"DATABASE_URL={env}\n")
    return wt


def test_env_file_value_handles_quotes_comments_and_reassignment(tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        "# DATABASE_URL=commented-out\n"
        'OTHER = "x"\n'
        "DATABASE_URL='first'\n"
        "  DATABASE_URL =  second  \n"
    )
    assert env_file_value("DATABASE_URL", f) == "second"  # last assignment wins
    assert env_file_value("OTHER", f) == "x"
    assert env_file_value("ABSENT", f) is None
    assert env_file_value("DATABASE_URL", tmp_path / "nope") is None


def test_database_name_strips_host_and_query():
    assert database_name(MAIN_URL) == "quarterback"
    assert database_name("postgresql://u:p@h:5432/db?sslmode=require") == "db"


def test_main_checkout_identifies_a_worktree_and_its_origin(tmp_path):
    main = _main_repo(tmp_path)
    wt = _worktree(tmp_path, main)
    assert main_checkout(wt) == main
    assert main_checkout(main) is None  # a main checkout has no main checkout behind it


def test_explicit_env_var_wins_over_the_checkout_env_file(tmp_path):
    wt = _worktree(tmp_path, _main_repo(tmp_path))
    pinned = "postgresql+asyncpg://u:p@localhost:5435/pinned"
    assert resolve_database_url({"DATABASE_URL": pinned}, wt) == (pinned, "DATABASE_URL")


def test_checkout_env_file_is_used_when_the_environment_is_silent(tmp_path):
    # The whole point: create-worktree writes the isolated database into .env,
    # and an empty environment must not override it with the main database.
    wt = _worktree(tmp_path, _main_repo(tmp_path))
    assert resolve_database_url({}, wt) == (WT_URL, ".env")


def test_dev_fallback_only_when_nothing_is_configured(tmp_path):
    main = _main_repo(tmp_path, env=None)
    assert resolve_database_url({}, main) == (DEV_FALLBACK_URL, "fallback")


def test_worktree_pointing_at_the_main_database_is_refused(tmp_path):
    main = _main_repo(tmp_path)
    wt = _worktree(tmp_path, main, env=MAIN_URL)  # isolation silently didn't happen
    problem = isolation_error(MAIN_URL, wt)
    assert problem is not None
    assert "quarterback" in problem and str(wt) in problem


def test_worktree_falling_back_to_the_dev_default_is_refused(tmp_path):
    # No .env at all in the worktree: the fallback IS the main dev database.
    main = _main_repo(tmp_path)
    wt = _worktree(tmp_path, main, env=None)
    url, source = resolve_database_url({}, wt)
    assert source == "fallback"
    assert isolation_error(url, wt) is not None


def test_worktree_with_its_own_database_is_allowed(tmp_path):
    main = _main_repo(tmp_path)
    wt = _worktree(tmp_path, main)
    assert isolation_error(WT_URL, wt) is None


def test_main_checkout_may_rebuild_its_own_database(tmp_path):
    # Documented behaviour, not an accident: the dev database is the main
    # checkout's to rebuild, and refusing there would block the normal run.
    main = _main_repo(tmp_path)
    assert isolation_error(MAIN_URL, main) is None


def test_guard_uses_the_main_env_not_a_hardcoded_name(tmp_path):
    # A repo whose main .env names a non-default database is still protected.
    other = "postgresql+asyncpg://quarterback:quarterback@localhost:5435/board_dev"
    main = _main_repo(tmp_path, env=other)
    wt = _worktree(tmp_path, main, env=other)
    assert isolation_error(other, wt) is not None
    assert isolation_error(WT_URL, wt) is None
