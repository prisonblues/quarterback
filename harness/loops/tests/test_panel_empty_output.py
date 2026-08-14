"""A reviewer CLI that exits 0 and prints nothing has FAILED, and says why.

"Reviewed, found nothing" and "produced nothing" are opposite claims, and an
empty stdout returned as success cannot tell them apart. Observed against `agy`
1.1.12 on a real PR diff: exit 0, `status: SUCCESS`, `response: ""` — because a
tool needed a permission headless mode cannot prompt for, so it was auto-denied.
The CLI said exactly that on stderr; run_cli only read stderr on a non-zero exit,
so the one run that had a diagnosis was the one that threw it away.

The cost is not one lost review. The reviewer still appears in the report as
having run, `⋆consensus` quietly weakens with no indication why, and the board's
reviewer leaderboard is fed a false zero — the datum a reviewer comparison most
has to be able to trust.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402

# The real thing, from `agy` 1.1.12. It names the cause and both remedies — all
# of which used to be discarded because the exit code was zero.
DENIED = ('jetski: no output produced — a tool required the "command" permission '
          "that headless mode cannot prompt for, so it was auto-denied. Add an "
          "allow-rule under permissions.allow in settings.json.")

FINDINGS = '[{"severity":"P2","file":"a.py","line":1,"title":"t","detail":"d"}]'


def _fake_cli(monkeypatch, *runs):
    """Patch subprocess.run to replay `runs` — one (stdout, stderr, rc) per
    attempt, the last repeating once exhausted."""
    calls = []

    def fake_run(cmd, **kwargs):
        out, err, rc = runs[min(len(calls), len(runs) - 1)]
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------- run_cli

def test_exit_zero_with_no_output_is_a_failure(monkeypatch):
    """The whole bug in one assertion: this used to be ("", None) — a success."""
    _fake_cli(monkeypatch, ("", DENIED, 0))
    out, err = panel.run_cli(["agy"], "antigravity (gemini-3.7-flash-high)")
    assert out is None
    assert "produced no output" in err


def test_the_stderr_that_explains_it_is_surfaced_on_a_zero_exit(monkeypatch):
    """stderr was read only when the exit code was non-zero, which discarded the
    diagnosis on exactly the runs that needed it. The reason must name the cause
    and the reviewer, so the skip line is actionable without a re-run."""
    _fake_cli(monkeypatch, ("", DENIED, 0))
    _out, err = panel.run_cli(["agy"], "antigravity (gemini-3.7-flash-high)")
    assert err.startswith("antigravity (gemini-3.7-flash-high):")
    assert "permission" in err and "permissions.allow" in err


def test_whitespace_only_output_counts_as_no_output(monkeypatch):
    """A reply of "\\n" is no more a review than "" is, and the JSON parser makes
    the same nothing of both."""
    _fake_cli(monkeypatch, ("  \n\t\n", "", 0))
    out, err = panel.run_cli(["agy"], "antigravity (m)")
    assert out is None and "produced no output" in err


def test_a_run_that_produced_findings_is_untouched_by_chatty_stderr(monkeypatch):
    """The mirror error: stderr is read only when stdout is EMPTY. CLIs log
    warm-up noise on stderr constantly, and reporting it on a run that delivered
    its findings would turn every successful review into a warning."""
    _fake_cli(monkeypatch, (FINDINGS, "warning: cache is stale\n", 0))
    out, err = panel.run_cli(["agy"], "antigravity (m)")
    assert out == FINDINGS and err is None


def test_a_blank_reply_is_retried_and_a_later_attempt_wins(monkeypatch):
    """Unlike a rejected request, a blank reply is not self-evidently
    deterministic — losing a whole panel member to one costs more than the two
    extra attempts, which fail fast anyway."""
    calls = _fake_cli(monkeypatch, ("", DENIED, 0), (FINDINGS, "", 0))
    out, err = panel.run_cli(["agy"], "antigravity (m)")
    assert out == FINDINGS and err is None
    assert len(calls) == 2


def test_a_persistently_blank_reviewer_gives_up_after_its_attempts(monkeypatch):
    calls = _fake_cli(monkeypatch, ("", DENIED, 0))
    _out, err = panel.run_cli(["agy"], "antigravity (m)", attempts=3)
    assert len(calls) == 3 and "produced no output" in err


# ---------------------------------------------------------------- review_llm

def test_the_reviewer_is_reported_skipped_with_the_cause(monkeypatch):
    """End to end: no findings, and a skip line naming the permission rule to add
    — not a reviewer that silently contributed nothing."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/agy")
    _fake_cli(monkeypatch, ("", DENIED, 0))
    finds, skip, _ms = panel.review_llm("antigravity", "gemini-3.7-flash-high", "p")
    assert finds == []
    assert "produced no output" in skip and "permissions.allow" in skip


def test_an_empty_reply_never_becomes_an_unstructured_finding(monkeypatch):
    """The fallback that keeps an unparseable reply as a raw markdown finding is
    for a reviewer that said something. Handing the judge an empty one is how a
    dead reviewer used to look like a live one."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/agy")
    _fake_cli(monkeypatch, ("", DENIED, 0))
    finds, skip, _ms = panel.review_llm("antigravity", "m", "p")
    assert finds == [] and skip is not None


def test_prose_is_still_kept_as_a_raw_finding(monkeypatch):
    """The regression guard on the above: output that isn't JSON but IS a review
    still reaches the judge rather than being discarded as unparseable."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/agy")
    _fake_cli(monkeypatch, ("The retry loop double-counts on line 12.", "", 0))
    finds, skip, _ms = panel.review_llm("antigravity", "m", "p")
    assert skip is None and len(finds) == 1
    assert "double-counts" in finds[0].detail


# ---------------------------------------------------------------- judge

def test_the_judge_reports_no_output_rather_than_unparseable(monkeypatch):
    """The judge shared the bug: an empty verdict was reported as "unparseable",
    blaming the reply's shape for a run that produced no reply at all — and
    everything is kept when the judge can't rule, so the reason is the only
    signal that the adjudication silently stopped happening."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    _fake_cli(monkeypatch, ("", DENIED, 0))
    groups = panel.group_findings(panel.parse_findings("claude", FINDINGS))
    verdicts, skip = panel.judge(groups, "diff", "opus")
    assert verdicts == {}
    assert "produced no output" in skip and "unparseable" not in skip
