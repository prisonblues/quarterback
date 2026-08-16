"""Shared fixtures for the board-client suite.

The client under test is an HTTP client, so every test here drives it through a
fake rather than a live board: the interesting behaviour is what it does with a
payload, a drop, or a 401 — none of which needs a server to reproduce, and all of
which are awkward to reproduce with one.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeClient:
    """Enough of QuarterbackClient for the tail and the panes.

    ``stream`` yields from a scripted list of batches: each batch is either a
    list of posts (delivered, after which the connection "drops") or an exception
    to raise. Once the script runs out every reconnect yields nothing, which is
    what a quiet board looks like — so a test bounds the loop with
    ``max_reconnects`` rather than by starving the fake.
    """

    def __init__(self, board=None, batches=None, posts=None) -> None:
        self._board = board or []
        self._batches = list(batches or [])
        self._posts = posts or {}
        self.board_calls: list[dict] = []
        self.stream_calls: list[int] = []
        self.posted: list[dict] = []

    def board(self, params):
        self.board_calls.append(dict(params))
        return list(self._board)

    def stream(self, since=0, read_timeout=90.0):
        self.stream_calls.append(since)
        if not self._batches:
            return
        batch = self._batches.pop(0)
        if isinstance(batch, BaseException):
            raise batch
        yield from batch

    def get_post(self, post_id):
        return self._posts[post_id]

    def post(self, body):
        self.posted.append(dict(body))
        return {"id": 999}


@pytest.fixture
def git_repo(tmp_path):
    """A real git repo with a real upstream, so the refusals are exercised for real.

    Stubbing git would test the stub. The refusals in ``local.py`` are entirely
    about what git actually reports — a detached branch with no upstream, an
    unpushed commit, a dirty file — so the suite builds those states.
    """

    def run(cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
        )

    origin = tmp_path / "origin.git"
    run(tmp_path, "init", "--bare", "-b", "main", str(origin))

    work = tmp_path / "work"
    work.mkdir()
    run(work, "init", "-b", "main")
    run(work, "config", "user.email", "test@example.invalid")
    run(work, "config", "user.name", "Test")
    (work / "README").write_text("one\n")
    run(work, "add", "README")
    run(work, "commit", "-m", "first")
    run(work, "remote", "add", "origin", str(origin))
    run(work, "push", "-u", "origin", "main")
    return work


@pytest.fixture
def holder_stub(tmp_path, monkeypatch):
    """Put a scripted ``worktree-holder`` on PATH and let a test choose its verdict."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    script = bin_dir / "worktree-holder"

    # An absolute interpreter, NOT `#!/usr/bin/env bash`: there is no
    # /usr/bin/env inside a nix build sandbox, so an env shebang fails to exec at
    # all — and every refusal test then passes for the wrong reason, reporting
    # "could not run worktree-holder" instead of the verdict it was given.
    bash = shutil.which("bash") or "/bin/sh"

    #: What worktree-holder really prints when a worktree is held: a generic
    #: headline, and the holder on the lines AFTER it. Reproduced because a
    #: one-line stub let a bug through — the code kept only the first line, which
    #: is the line with nothing in it a person can act on.
    held_output = (
        "worktree-holder: /some/worktree is held by another live agent\n"
        '  zeus/other-agent · on feat/x · "Investigating" · held for 4m · session abcd1234\n'
    )

    def set_exit(code: int, message: str = held_output) -> None:
        script.write_text(
            f"#!{bash}\n"
            f'[ "$1" = "--quiet" ] || printf %s {shlex.quote(message)} >&2\n'
            f"exit {code}\n"
        )
        script.chmod(0o755)

    set_exit(0)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    return set_exit
