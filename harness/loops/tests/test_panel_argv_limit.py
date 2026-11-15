"""A diff big enough to be worth a panel is big enough to break execve.

Linux caps ONE argv string at MAX_ARG_STRLEN = 131,072 bytes, so a prompt passed
as `["claude", "-p", prompt]` dies before the CLI starts once the diff crosses
it — and the panel used to report that as "LLM reviewers ran: none", which reads
like a clean PR. These tests pin the fix: the prompt travels on stdin (agy's as one
stream-json message — see test_panel_agy_tools.py).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402  — `sh` is the gh double's seam
import panel_seats  # noqa: E402  — run_cli lives here since #129
from conftest import gh_stub  # noqa: E402

# ------------------------------------------------------- the limit is real

def test_the_kernel_limit_this_is_all_about():
    """Not a mock: if this ever stops raising, the rest of this file is theatre."""
    with pytest.raises(OSError) as e:
        subprocess.run(["true", "x" * 200_000], capture_output=True)
    assert e.value.errno == 7                      # E2BIG
    subprocess.run(["cat"], input="x" * 200_000, capture_output=True, text=True)


# ------------------------------------------------------- prompts go on stdin

@pytest.mark.parametrize("name,model", [("claude", "sonnet"), ("codex", ""), ("pi", "")])
def test_a_reviewer_prompt_is_fed_on_stdin_not_in_argv(name, model, monkeypatch):
    """The prompt must not appear in argv at all — a 200 KB one there is not a
    slow reviewer, it is a dead one."""
    seen = {}

    def fake(args, label, timeout=panel.CLI_TIMEOUT, attempts=3, stdin_text=None,
             on_output=None, replied=None, cwd=None):
        # The session-pinned seats pass a thunk, because each attempt needs its
        # own id; run_cli calls it per attempt, so this does the same.
        seen["args"], seen["stdin"] = (args() if callable(args) else args), stdin_text
        return "[]", None

    monkeypatch.setattr(panel_seats, "run_cli", fake)
    monkeypatch.setattr(panel.shutil, "which", lambda _c: "/usr/bin/" + _c)
    monkeypatch.setattr(panel_seats, "claude_usage", lambda _sids: None)
    prompt = "REVIEW THIS DIFF " * 100
    panel.review_llm(name, model, prompt)
    assert seen["stdin"] == prompt
    assert not any(prompt in a for a in seen["args"])


def test_the_judge_prompt_is_fed_on_stdin_too(monkeypatch):
    """The judge is the prompt with an UNBUDGETED component — the findings
    listing grows with the panel — and a judge that dies takes every finding
    through unadjudicated, which reads like triage rather than like failure."""
    seen = {}

    def fake(args, label, timeout=panel.CLI_TIMEOUT, attempts=3, stdin_text=None, cwd=None):
        seen["args"], seen["stdin"] = args, stdin_text
        return "[]", None

    monkeypatch.setattr(panel_seats, "run_cli", fake)
    monkeypatch.setattr(panel.shutil, "which", lambda _c: "/usr/bin/claude")
    f = panel.Finding("claude", "P1", "a.py", 1, "title", "detail")
    panel.adjudicate([[f]], "DIFFDIFFDIFF", "sonnet", 1)
    assert "DIFFDIFFDIFF" in seen["stdin"]
    assert not any("DIFFDIFFDIFF" in a for a in seen["args"])


def test_stdin_stays_closed_when_there_is_no_prompt_to_feed(monkeypatch):
    """The DEVNULL guard exists so a CLI that decides to prompt cannot hang the
    panel on an inherited terminal. Feeding a prompt must not weaken it: the
    write is followed by a close, so the CLI reads EOF either way."""
    seen = {}
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: seen.update(k) or
                        subprocess.CompletedProcess(a[0], 0, "out", ""))
    panel.run_cli(["x"], "l")
    assert seen["stdin"] is subprocess.DEVNULL and "input" not in seen
    seen.clear()
    panel.run_cli(["x"], "l", stdin_text="hello")
    assert seen["input"] == "hello" and "stdin" not in seen


# ------------------------------------------------------- the error says what happened

def test_an_os_error_reports_errno_and_message(monkeypatch):
    """`OSError` alone sent people looking for a crash that was "Argument list
    too long", three times. Everything needed to name it was on the exception."""
    def boom(*_a, **_k):
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(subprocess, "run", boom)
    out, err = panel.run_cli(["x"], "codex (gpt-5.6-luna)", attempts=1)
    assert out is None
    assert "7" in err and "Argument list too long" in err
    assert err.startswith("codex (gpt-5.6-luna): ")
