"""A diff big enough to be worth a panel is big enough to break execve.

Linux caps ONE argv string at MAX_ARG_STRLEN = 131,072 bytes, so a prompt passed
as `["claude", "-p", prompt]` dies before the CLI starts once the diff crosses
it — and the panel used to report that as "LLM reviewers ran: none", which reads
like a clean PR. These tests pin the two halves of the fix: the prompt travels on
stdin wherever a CLI will take it there, and the one seat that cannot (agy) is
clamped to what the kernel will carry, with the truncation surfaced.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402

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
             on_output=None, replied=None):
        # The session-pinned seats pass a thunk, because each attempt needs its
        # own id; run_cli calls it per attempt, so this does the same.
        seen["args"], seen["stdin"] = (args() if callable(args) else args), stdin_text
        return "[]", None

    monkeypatch.setattr(panel, "run_cli", fake)
    monkeypatch.setattr(panel.shutil, "which", lambda _c: "/usr/bin/" + _c)
    monkeypatch.setattr(panel, "claude_usage", lambda _sids: None)
    prompt = "REVIEW THIS DIFF " * 100
    panel.review_llm(name, model, prompt)
    assert seen["stdin"] == prompt
    assert not any(prompt in a for a in seen["args"])


def test_the_judge_prompt_is_fed_on_stdin_too(monkeypatch):
    """The judge is the prompt with an UNBUDGETED component — the findings
    listing grows with the panel — and a judge that dies takes every finding
    through unadjudicated, which reads like triage rather than like failure."""
    seen = {}

    def fake(args, label, timeout=panel.CLI_TIMEOUT, attempts=3, stdin_text=None):
        seen["args"], seen["stdin"] = args, stdin_text
        return "[]", None

    monkeypatch.setattr(panel, "run_cli", fake)
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


# ------------------------------------------------------- agy is clamped, not killed

def test_a_budget_over_the_argv_limit_is_truncated_not_fatal():
    """diff_budget's contract is that a positive budget is honoured and the
    CONSEQUENCE is surfaced. Above the kernel's ceiling it cannot be honoured,
    so it becomes ordinary truncation rather than an E2BIG with nothing in it."""
    diff = "x" * 500_000
    render = lambda b: "TEMPLATE " * 50 + diff[:b]        # noqa: E731
    fitted = panel.fit_argv_budget(render, 400_000)
    assert fitted < 400_000
    assert len(render(fitted).encode()) <= panel.ARGV_PROMPT_MAX_BYTES
    # and it does not shrink a budget that already fits
    assert panel.fit_argv_budget(render, 1_000) == 1_000


def test_the_ceiling_is_bytes_not_characters():
    """This repo's own comments are full of em dashes: three bytes, one char. A
    char-counted budget clears a byte-counted limit and still dies at execve."""
    diff = "—" * 200_000
    render = lambda b: diff[:b]                           # noqa: E731
    fitted = panel.fit_argv_budget(render, 200_000)
    assert len(render(fitted).encode()) <= panel.ARGV_PROMPT_MAX_BYTES
    assert fitted < panel.ARGV_PROMPT_MAX_BYTES           # fewer chars than bytes allowed


def test_a_clamped_prompt_actually_survives_execve():
    """End to end against the real kernel, because the whole bug was a number
    that looked fine and a syscall that disagreed."""
    diff = "—" * 200_000
    render = lambda b: diff[:b]                           # noqa: E731
    fitted = panel.fit_argv_budget(render, 200_000)
    proc = subprocess.run(["true", render(fitted)], capture_output=True)
    assert proc.returncode == 0


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
