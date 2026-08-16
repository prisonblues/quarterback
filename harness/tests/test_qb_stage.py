"""Tests for qb-stage, the workflow-stage marker the statusline reads.

The field is cosmetic, which is exactly why the failure modes matter more than
the happy path: a status marker that can fail a review, refuse a legal token, or
write outside its directory has cost more than it was ever worth. So the happy
path gets one test and the ways it must NOT misbehave get the rest.

Run: pytest harness/tests
"""

import os
import subprocess
from pathlib import Path

import pytest

QB_STAGE = Path(__file__).resolve().parent.parent / "bin" / "qb-stage"


@pytest.fixture
def run(tmp_path):
    """Invoke qb-stage with an isolated marker dir and a fixed session id."""

    def _run(*args, session_id="11111111-2222-3333-4444-555555555555", **kw):
        env = {**os.environ, "QB_SESSION_STAGE_DIR": str(tmp_path / "stage")}
        if session_id is None:
            env.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            env["CLAUDE_CODE_SESSION_ID"] = session_id
        return subprocess.run(
            [str(QB_STAGE), *args], env=env, capture_output=True, text=True, **kw
        )

    _run.dir = tmp_path / "stage"
    return _run


def test_a_stage_is_recorded_under_the_session_id(run):
    assert run("R1F").returncode == 0
    marker = run.dir / "11111111-2222-3333-4444-555555555555"
    assert marker.read_text() == "R1F"


def test_the_marker_carries_no_trailing_newline(run):
    """The statusline renders the contents verbatim into a one-line bar."""
    run("F0")
    assert (run.dir / "11111111-2222-3333-4444-555555555555").read_text() == "F0"


def test_a_later_stage_replaces_the_earlier_one(run):
    """A session moves R1 -> R1F -> R2; the bar shows where it is, not a history."""
    for stage in ("R1", "R1F", "R2"):
        run(stage)
    assert run("--show").stdout == "R2"


def test_clear_removes_the_marker(run):
    run("R2F")
    assert run("--clear").returncode == 0
    assert run("--show").stdout == ""


def test_clearing_a_stage_that_was_never_set_is_not_an_error(run):
    """/drop-worktree clears unconditionally; it must not care whether one existed."""
    assert run("--clear").returncode == 0


@pytest.mark.parametrize("stage", ["F0", "R1", "R1F", "R12F", "A", "ABCDEF"])
def test_well_formed_stages_are_accepted(run, stage):
    """The shape is checked, not the vocabulary — a new stage needs no edit here."""
    assert run(stage).returncode == 0, run(stage).stderr


@pytest.mark.parametrize(
    "stage",
    [
        "ABCDEFG",       # seven characters: past what the bar has room for
        "R1 F",          # a space would split the field
        "R1F;whoami",    # punctuation, in case a caller interpolates
        "R1/../../x",    # separators, which a filename must never carry
        "",              # an empty argument is a caller bug, not a clear
    ],
)
def test_malformed_stages_are_refused_loudly(run, stage):
    """Exit 2, because a typo here is a caller bug and silence would hide it."""
    result = run(stage)
    assert result.returncode == 2
    assert not (run.dir / "11111111-2222-3333-4444-555555555555").exists()


def test_no_session_id_is_silent_success(run):
    """A loop under systemd has no session and nobody watching a bar. Telemetry
    that can fail the thing it reports on is worse than no telemetry."""
    result = run("R1", session_id=None)
    assert result.returncode == 0
    assert result.stderr == ""
    assert not run.dir.exists()


@pytest.mark.parametrize("session_id", ["../../evil", "a/b", "..", "with space"])
def test_a_session_id_that_could_escape_the_directory_is_refused(run, session_id):
    """The id becomes a filename. Refused rather than sanitised: a mangled id
    would write a marker that no reader ever looks for."""
    result = run("R1", session_id=session_id)
    assert result.returncode == 2
    assert "session id" in result.stderr


def test_show_on_a_fresh_session_prints_nothing(run):
    result = run("--show")
    assert result.returncode == 0
    assert result.stdout == ""


def test_help_works_without_a_session(run):
    """Documentation must not depend on being inside a Claude Code session."""
    result = run("--help", session_id=None)
    assert result.returncode == 0
    assert "qb-stage <stage>" in result.stdout


def test_the_marker_dir_is_created_on_demand(run):
    """First use of a fresh machine must not need a mkdir from the caller."""
    assert not run.dir.exists()
    run("F0")
    assert run.dir.is_dir()
