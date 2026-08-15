"""Reading a headless agent's run: what it said, and whether it ran at all.

The loops turn `claude -p` loose from a systemd timer. Nothing captured is
anything read, and the failure that matters exits 0 — a tool auto-denied because
headless mode cannot prompt leaves an agent that finishes tidily having changed
nothing. So these pin the two halves the loops depend on: run_agent keeps both
streams (while still passing them through, or an unattended run has no live log
at all), and agent_failure/agent_gist turn the result into the one sentence a
"nothing happened" line has room for.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness_rules  # noqa: E402


def done(rc=0, out="", err=""):
    return subprocess.CompletedProcess(["claude", "-p", "…"], rc, out, err)


# ------------------------------------------------------------- the gists

def test_tail_gist_keeps_the_end_not_the_beginning():
    """An agent streams its working and finishes with the conclusion, so the
    interesting end of stdout is the last one."""
    text = "thinking\n" * 200 + "I could not edit: permission denied for Write"
    gist = harness_rules.tail_gist(text)
    assert gist.endswith("permission denied for Write")
    assert len(gist) <= 201  # the ellipsis marks that it was cut


def test_tail_gist_collapses_to_one_line():
    assert harness_rules.tail_gist("two\n  lines\n") == "two lines"


def test_tail_gist_of_nothing_is_empty():
    assert harness_rules.tail_gist("") == ""
    assert harness_rules.tail_gist("   \n\n ") == ""


def test_agent_gist_prefers_stderr_the_harness_complaint():
    proc = done(out="I have finished.", err="Error: unknown flag --nope")
    assert harness_rules.agent_gist(proc) == "Error: unknown flag --nope"


def test_agent_gist_falls_back_to_what_the_agent_itself_said():
    """The denial in #31 is described on stdout and nowhere else — a stderr-only
    reading would report the interesting case as silence."""
    proc = done(out="I was not permitted to run that tool, so I made no changes.")
    assert harness_rules.agent_gist(proc) == (
        "I was not permitted to run that tool, so I made no changes.")


# ---------------------------------------------------------- did it run?

def test_a_clean_run_is_not_a_failure():
    assert harness_rules.agent_failure(done(out="Fixed the import.")) == ""


def test_nonzero_exit_is_reported_with_the_reason():
    failure = harness_rules.agent_failure(done(rc=2, err="Error: credit balance too low"))
    assert failure == "exited 2 (Error: credit balance too low)"


def test_nonzero_exit_with_nothing_on_stderr_still_names_the_code():
    assert harness_rules.agent_failure(done(rc=1)) == "exited 1"


def test_exit_zero_with_an_empty_reply_is_a_failure_not_an_empty_answer():
    """#19's signature, at the call sites that never captured it. A real run
    always says something; silence is a failed invocation wearing exit 0."""
    failure = harness_rules.agent_failure(done(out="   \n", err="API error: overloaded"))
    assert failure == "exited 0 having printed nothing (API error: overloaded)"


# ------------------------------------------------------------- run_agent

def _py(code):
    return [sys.executable, "-c", code]


def test_run_agent_captures_both_streams(capsys):
    proc = harness_rules.run_agent(
        _py("import sys; print('to stdout'); print('to stderr', file=sys.stderr)"))
    assert proc.returncode == 0
    assert "to stdout" in proc.stdout
    assert "to stderr" in proc.stderr


def test_run_agent_still_passes_the_output_through(capsys):
    """Capturing must not cost the live log: these runs last tens of minutes, and
    a journal that shows nothing until the process exits cannot tell a working
    agent from a wedged one."""
    harness_rules.run_agent(
        _py("import sys; print('progress'); print('trouble', file=sys.stderr)"))
    captured = capsys.readouterr()
    assert "progress" in captured.out
    assert "trouble" in captured.err


def test_run_agent_does_not_deadlock_on_a_chatty_agent():
    """The whole reason both streams are pumped by threads: a child that fills
    one pipe's buffer while we wait on the other never exits."""
    proc = harness_rules.run_agent(_py(
        "import sys; sys.stdout.write('o' * 300000); sys.stderr.write('e' * 300000)"))
    assert len(proc.stdout) == 300000
    assert len(proc.stderr) == 300000


def test_run_agent_gives_the_agent_no_stdin():
    """An unattended agent that decides to ask a question must read EOF, not
    inherit a terminal and hang the loop."""
    proc = harness_rules.run_agent(_py("import sys; print(repr(sys.stdin.read()))"))
    assert proc.stdout.strip() == "''"


def test_run_agent_runs_where_it_was_told_to():
    proc = harness_rules.run_agent(_py("import os; print(os.getcwd())"), cwd="/")
    assert Path(proc.stdout.strip()) == Path("/")


def test_a_cli_that_is_not_installed_is_reported_not_raised():
    """The one route that never reaches a child process still has to arrive as a
    result: a raise here would abandon the rest of a sweep, which is precisely the
    contract this function exists to keep."""
    proc = harness_rules.run_agent(["definitely-not-a-real-cli-31"])

    assert proc.returncode == 127
    failure = harness_rules.agent_failure(proc)
    assert failure.startswith("exited 127")
    assert "No such file" in failure


def test_a_missing_working_directory_is_reported_the_same_way(tmp_path):
    proc = harness_rules.run_agent(_py("print('hi')"), cwd=tmp_path / "gone")

    assert proc.returncode == 127
    assert harness_rules.agent_failure(proc).startswith("exited 127")


def test_run_agent_reports_a_failed_exit_rather_than_raising():
    """No `check`: raising throws away the very output the caller has to report."""
    proc = harness_rules.run_agent(_py("import sys; sys.exit(3)"))
    assert proc.returncode == 3
    assert harness_rules.agent_failure(proc).startswith("exited 3")
