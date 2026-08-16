"""Where the client remembers its place: the cursor file, and who may move it.

A tail and a full-screen client can be open on the same board on the same
machine, and both write this one file. The tests that matter are therefore the
ones about two writers, not the round trip.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp_server.board.state import cursor_path, read_cursor, state_dir, write_cursor

URL = "https://board.example"
OTHER = "https://board.work.example"


def env_for(home: Path) -> dict[str, str]:
    return {"HOME": str(home)}


# -- where the file lives ----------------------------------------------


def test_state_dir_follows_xdg_when_it_is_set(tmp_path):
    assert state_dir({"XDG_STATE_HOME": str(tmp_path), "HOME": "/home/nobody"}) == (
        tmp_path / "quarterback"
    )


def test_state_dir_without_a_home_is_still_an_absolute_path(tmp_path, monkeypatch):
    """`Path` does not expand a tilde, so a "~" fallback made a directory called `~`.

    In the working directory, wherever that happened to be — so the cursor was
    written somewhere different on every invocation and resume never worked.
    """
    monkeypatch.chdir(tmp_path)
    path = state_dir({})
    assert path.is_absolute()
    assert "~" not in path.parts
    assert not (tmp_path / "~").exists()


def test_two_boards_do_not_share_a_cursor(tmp_path):
    """The fleet keeps a deliberately disjoint board on the work host."""
    assert cursor_path(URL, env_for(tmp_path)) != cursor_path(OTHER, env_for(tmp_path))


def test_a_trailing_slash_is_the_same_board(tmp_path):
    assert cursor_path(URL, env_for(tmp_path)) == cursor_path(f"{URL}/", env_for(tmp_path))


# -- reading -----------------------------------------------------------


def test_a_board_never_read_here_starts_at_zero(tmp_path):
    assert read_cursor(URL, env_for(tmp_path)) == 0


def test_an_unreadable_cursor_starts_at_zero_rather_than_raising(tmp_path):
    path = cursor_path(URL, env_for(tmp_path))
    path.parent.mkdir(parents=True)
    path.write_text("not a number\n")
    assert read_cursor(URL, env_for(tmp_path)) == 0


def test_the_cursor_round_trips(tmp_path):
    write_cursor(URL, 42, env_for(tmp_path))
    assert read_cursor(URL, env_for(tmp_path)) == 42


# -- two writers -------------------------------------------------------


def test_the_recorded_cursor_never_moves_backwards(tmp_path):
    """The file means "seen up to here", which holds even when a cursor rewinds.

    A TUI backfilling older posts winds its in-memory cursor back on purpose; if
    that reached disk it would make the tail on the same board replay everything
    it had already printed.
    """
    write_cursor(URL, 90, env_for(tmp_path))
    write_cursor(URL, 12, env_for(tmp_path))
    assert read_cursor(URL, env_for(tmp_path)) == 90


def test_the_scratch_file_is_per_process(tmp_path, monkeypatch):
    """One fixed `.tmp` name let two writers interleave into a single file.

    Which then got renamed into place as the mixture of both.
    """
    scratch: list[str] = []
    real_replace = Path.replace

    def spy(self, target):
        scratch.append(self.name)
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy)
    monkeypatch.setattr(os, "getpid", lambda: 111)
    write_cursor(URL, 1, env_for(tmp_path))
    monkeypatch.setattr(os, "getpid", lambda: 222)
    write_cursor(URL, 2, env_for(tmp_path))

    assert len(set(scratch)) == 2
    assert read_cursor(URL, env_for(tmp_path)) == 2


def test_no_scratch_file_is_left_behind(tmp_path):
    write_cursor(URL, 5, env_for(tmp_path))
    assert list(state_dir(env_for(tmp_path)).glob("*.tmp")) == []


def test_an_unwritable_state_directory_costs_the_next_run_not_this_one(tmp_path):
    """The cursor is a convenience; failing to save it must not end a tail."""
    blocked = state_dir(env_for(tmp_path))
    blocked.parent.mkdir(parents=True)
    blocked.write_text("in the way\n")  # a file where the directory should be

    write_cursor(URL, 7, env_for(tmp_path))  # must not raise
    assert read_cursor(URL, env_for(tmp_path)) == 0
