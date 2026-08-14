"""Each panel member is timed, on every path it can leave by.

The board scores reviewers on what they find; duration is the other half of that
judgement, because the panel is a decision about where to spend wall-clock. The
column and the /panel page's "avg Ns per review" have existed since v2.10 — what
was missing was anyone measuring it, which made the average silently null and the
leaderboard a findings-only ranking.

The failure paths matter as much as the happy one: how long a reviewer took to
NOT produce findings is precisely the fact you want about one that times out, so
a skip that reports no duration would hide the worst case.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402

FINDINGS = '[{"severity":"P2","file":"a.py","line":1,"title":"t","detail":"d"}]'


def test_a_successful_review_reports_a_duration(monkeypatch):
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: (FINDINGS, None))
    run = panel.review_llm("claude", "opus", "p")
    finds, skip, ms = run.findings, run.skip, run.duration_ms
    assert skip is None and len(finds) == 1
    assert isinstance(ms, int) and ms >= 0


def test_a_dead_reviewer_is_still_timed(monkeypatch):
    """A timeout is the case the duration exists for — it must not report null
    just because the member produced nothing."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr(panel, "run_cli",
                        lambda *a, **k: (None, "codex: timed out after 600s"))
    run = panel.review_llm("codex", "gpt-5.6-luna", "p")
    finds, skip, ms = run.findings, run.skip, run.duration_ms
    assert finds == [] and "timed out" in skip
    assert isinstance(ms, int) and ms >= 0


def test_a_config_error_reports_a_duration_too(monkeypatch):
    """Refused before any process starts, so ~0 — but an int, never None: the
    board's average must never be skewed by a row that opted out of being
    measured."""
    monkeypatch.setattr(panel, "run_cli",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    run = panel.review_llm("claude", "sonnet", "p", effort="high")
    skip, ms = run.skip, run.duration_ms
    assert "takes no reasoning effort" in skip
    assert isinstance(ms, int) and ms >= 0
