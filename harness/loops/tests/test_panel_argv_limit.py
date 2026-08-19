"""A diff big enough to be worth a panel is big enough to break execve.

Linux caps ONE argv string at MAX_ARG_STRLEN = 131,072 bytes, so a prompt passed
as `["claude", "-p", prompt]` dies before the CLI starts once the diff crosses
it — and the panel used to report that as "LLM reviewers ran: none", which reads
like a clean PR. These tests pin the two halves of the fix: the prompt travels on
stdin wherever a CLI will take it there, and the one seat that cannot (agy) is
clamped to what the kernel will carry, with the truncation surfaced.
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


# ------------------------------------------------- kernel cap vs config budget

def test_the_kernel_ceiling_is_told_apart_from_a_budget_someone_typed():
    """`argv_clamp`'s second return value, which decides whether a truncated
    antigravity costs the round its confidence.

    The kernel cap is a constant — same box, same diff size, same result every
    round — so `coverage_veto` reports it without counting it. A config budget is
    a number somebody wrote and can raise, so it still counts. Conflating them
    either loses the standing-veto fix or lets a dropped zero (60_000 -> 6_000)
    hide behind the kernel, and `diff_budget` deliberately does not guard that
    slip on the grounds that the consequence gets surfaced instead. This is where
    it has to keep being surfaced."""
    target = "x" * 300_000
    render = lambda b: "TEMPLATE " * 50 + target[:b]      # noqa: E731

    # No budget at all: the machine is the only thing in the way, and it is short
    # of the target — structural.
    fitted, structural = panel.argv_clamp(render, len(target), None)
    assert structural is True
    assert fitted < len(target)
    assert len(render(fitted).encode()) <= panel.ARGV_PROMPT_MAX_BYTES

    # A budget BELOW what this box could carry is what truncated the seat, so it
    # is the config's doing and stays evidence about the round.
    fitted, structural = panel.argv_clamp(render, len(target), 6_000)
    assert structural is False and fitted == 6_000

    # A budget ABOVE the ceiling cannot be honoured; the kernel is binding again.
    _, structural = panel.argv_clamp(render, len(target), 250_000)
    assert structural is True


def test_a_seat_the_machine_can_hand_the_whole_target_is_not_structurally_capped():
    """The other half: `structural` is not "antigravity was truncated", it is "no
    budget could have shown it the target". A small diff clears the ceiling, so
    nothing about this box excuses anything — and a budget that cuts it there is
    an ordinary config truncation."""
    target = "y" * 2_000
    render = lambda b: "TEMPLATE " * 50 + target[:b]      # noqa: E731
    fitted, structural = panel.argv_clamp(render, len(target), None)
    assert structural is False and fitted == len(target)
    fitted, structural = panel.argv_clamp(render, len(target), 500)
    assert structural is False and fitted == 500


def test_a_seat_handed_no_diff_at_all_is_not_excused_as_structural():
    """The zero case, and it is reachable rather than theoretical.

    `fit_argv_budget` subtracts a BYTE overflow from a CHARACTER budget — it
    over-shrinks deliberately, to converge in one pass — and on three-byte
    characters it over-shrinks to nothing: a 200,000 em-dash diff clamps to 0, so
    the seat is handed an empty prompt. Calling that "structurally saw part of it"
    would exempt a seat that reviewed NOTHING from the veto, which is the wrong
    direction on the one case where the truncation is total. It falls through to the
    ordinary truncation veto instead, exactly as it did before the exemption
    existed."""
    target = "\u2014" * 200_000
    render = lambda b: target[:b]                         # noqa: E731
    fitted, structural = panel.argv_clamp(render, len(target), None)
    assert fitted == 0, "this is the over-shrink this test is about"
    assert structural is False, "a seat that got no diff must still veto"


def test_the_clamp_does_not_assume_fit_argv_budget_composes():
    """Why the rule asks "did the clamp cut what was asked?" rather than comparing
    against a ceiling derived from the whole material.

    The ceiling formulation is the one that reads best — "could any budget have
    shown this seat the target?" — and it is wrong, because `fit_argv_budget`'s
    overshoot depends on the budget it starts from. `min(asked, fit(sendable))` is
    therefore not `fit(asked)`: on multibyte material `fit(sendable)` collapses to 0
    and a perfectly deliverable small budget would be clamped to nothing. This test
    is the guard against that refactor being made again."""
    target = "\u2014" * 200_000
    render = lambda b: target[:b]                         # noqa: E731
    assert panel.fit_argv_budget(render, len(target)) == 0        # the trap
    assert panel.fit_argv_budget(render, 1_000) == 1_000          # the reality
    # A budget this box can plainly carry is handed over whole, and is a config
    # truncation rather than a kernel one.
    fitted, structural = panel.argv_clamp(render, len(target), 1_000)
    assert fitted == 1_000 and structural is False


def test_the_clamped_budget_still_fits_when_the_diff_is_multibyte():
    """`argv_clamp` computes its ceiling from `sendable` and then takes
    `min(asked, ceiling)`, where the previous code called `fit_argv_budget` on the
    asked budget directly. The two agree because `fit_argv_budget` is monotonic and
    does not shrink a budget that already fits — but the invariant that MATTERS is
    the one execve enforces, and it is counted in bytes while the budget is counted
    in characters. Em dashes are three bytes and one char, and this repo's diffs are
    full of them.

    Asserted against the real kernel for the same reason
    `test_a_clamped_prompt_actually_survives_execve` is: the whole bug was a number
    that looked fine and a syscall that disagreed."""
    target = "—" * 200_000
    render = lambda b: target[:b]                         # noqa: E731
    for asked in (None, 500_000, 200_000, 150_000):
        fitted, _ = panel.argv_clamp(render, len(target), asked)
        assert len(render(fitted).encode()) <= panel.ARGV_PROMPT_MAX_BYTES, asked
        assert subprocess.run(["true", render(fitted)],
                              capture_output=True).returncode == 0, asked


# ------------------------------------- the exemption is assembled from both halves

#: The pre-flight verdict is OFF here (#138), and both halves of it.
#:
#: These tests exist to measure the ARGV CLAMP and the truncation it produces, and
#: they get there with budgets far under the diff — `agy_budget=5_000` against a
#: 270 KB diff is the whole device. That is also, to the pre-flight verdict, a diff
#: 54x over the tightest ceiling: it would refuse the round, dispatch nobody, and
#: the payload would carry no `truncated` key at all for the test to read. A
#: move-shaped fixture would be replaced by a manifest for the same reason. So the
#: refusal is switched off with `0` and the manifest with `false`; the verdict has
#: its own suite, and what these tests need is for the round to happen.
AGY_CFG = {"github": "acme/board", "path": "/tmp/acme-board",
           "reviewers": {"claude": {"enabled": True, "model": "sonnet"},
                         "antigravity": {"enabled": True, "model": "m", "effort": ""}},
           "review_panel": {"refuse_over_cap_multiple": 0,
                            "manifest_moves": False}}


def _agy_round(monkeypatch, tmp_path, capsys, diff, agy_budget=None):
    """A whole `run` with antigravity on a `diff` of the caller's choosing, so the
    payload can be read back. Returns antigravity's `reviewer_meta` entry."""
    cfg = json.loads(json.dumps(AGY_CFG))
    if agy_budget is not None:
        cfg["reviewers"]["antigravity"]["max_diff_chars"] = agy_budget
    monkeypatch.setattr(panel, "load_repo_cfg", lambda _n: cfg)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "feat: x", "additions": 3, "deletions": 1, "headRefOid": "abc"},
        diff=diff))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 10, [],
                                                          code_blind=True))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate",
                        lambda *a, **k: ([], "", None))
    out = tmp_path / f"agy-{agy_budget}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False) == 0
    capsys.readouterr()
    return json.loads(Path(out).read_text())["reviewers"]["antigravity"]


def test_a_kernel_cut_seat_is_recorded_as_argv_capped_in_the_payload(
        monkeypatch, tmp_path, capsys):
    """The wiring, end to end: a diff far over the kernel's ceiling leaves
    antigravity both truncated and exempt, and the payload says which. Asserted
    through `run` because the exemption is an INTERSECTION assembled in two places —
    `argv_clamp` says who cut the budget, `truncated_for` says whether the target
    was actually lost — and a unit test of either half passes while the join is
    missing."""
    meta = _agy_round(monkeypatch, tmp_path, capsys,
                      "diff --git a/a.py b/a.py\n" + "+x\n" * 90_000)
    assert meta["truncated"] is True
    assert meta["argv_capped"] is True


def test_a_budget_somebody_typed_is_not_recorded_as_argv_capped(
        monkeypatch, tmp_path, capsys):
    """The other side of the same wiring. A `max_diff_chars` this box could carry
    easily is a number in a config file, so the seat is truncated and NOT exempt —
    it keeps costing the round its confidence, which is the whole reason
    `argv_capped` is a separate fact from `truncated`."""
    meta = _agy_round(monkeypatch, tmp_path, capsys,
                      "diff --git a/a.py b/a.py\n" + "+x\n" * 90_000,
                      agy_budget=5_000)
    assert meta["truncated"] is True
    assert meta["argv_capped"] is False


def test_argv_capped_is_never_claimed_without_measured_truncation(
        monkeypatch, tmp_path, capsys):
    """`argv_capped` is a subset of `truncated_for` by construction, and it has to
    be: the report's truncation footnote is the only place the exemption is visible
    to a reader, and it iterates the truncated seats. A seat marked exempt but not
    truncated would be exempted invisibly. A small diff clears the ceiling, so
    neither flag is set."""
    meta = _agy_round(monkeypatch, tmp_path, capsys,
                      "diff --git a/a.py b/a.py\n+x\n")
    assert meta["truncated"] is False
    assert meta["argv_capped"] is False
