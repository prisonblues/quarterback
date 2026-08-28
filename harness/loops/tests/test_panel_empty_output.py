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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_seats  # noqa: E402  — run_cli lives here since #129

# The real thing, from `agy` 1.1.12. It names the cause and both remedies — all
# of which used to be discarded because the exit code was zero.
DENIED = ('jetski: no output produced — a tool required the "command" permission '
          "that headless mode cannot prompt for, so it was auto-denied. Add an "
          "allow-rule under permissions.allow in settings.json.")

# A blank run whose stderr is a SERVER refusal rather than a local permission —
# the other settled cause, and the reason the short-circuit asks
# is_deterministic_failure rather than either predicate alone.
REJECTED = ('{"type":"error","error":{"type":"invalid_request_error",'
            '"message":"The model `gemini-3.9-nope` does not exist"}}')

# Warm-up noise, on a blank run that carries no diagnosis at all. Nothing here
# says the next attempt would also come back blank, so this one IS retried.
FLAKE = "loaded 3 plugins\n"

FINDINGS = '[{"severity":"P2","file":"a.py","line":1,"title":"t","detail":"d"}]'


def _fake_cli(monkeypatch, *runs):
    """Patch subprocess.run to replay `runs` — one (stdout, stderr, rc) per
    attempt, the last repeating once exhausted."""
    assert runs, "give _fake_cli at least one (stdout, stderr, rc) to replay"
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


def test_a_blank_reply_with_no_diagnosis_is_retried_and_a_later_attempt_wins(monkeypatch):
    """A blank reply that says nothing about WHY may well be a flake, and losing
    a whole panel member to one costs more than the extra attempts."""
    calls = _fake_cli(monkeypatch, ("", FLAKE, 0), (FINDINGS, "", 0))
    out, err = panel.run_cli(["agy"], "antigravity (m)")
    assert out == FINDINGS and err is None
    assert len(calls) == 2


def test_a_persistently_blank_reviewer_gives_up_after_its_attempts(monkeypatch):
    calls = _fake_cli(monkeypatch, ("", FLAKE, 0))
    _out, err = panel.run_cli(["agy"], "antigravity (m)", attempts=3)
    assert len(calls) == 3 and "produced no output" in err


# ------------------------------------------- blank output, but a settled cause

def test_an_auto_denied_permission_is_not_retried(monkeypatch):
    """A missing `permissions.allow` rule is as fixed as a bad model pin: the
    second and third attempts are auto-denied by the same rule, in the same way.

    Retrying is not free here. A blank run does NOT fail fast the way a non-zero
    exit does — the observed one burned its whole model call — so three of them
    is up to three whole `panel.CLI_TIMEOUT`s held against the joined futures of
    the entire panel, 3x the duration_ms the board's leaderboard ranks this
    member on, and on the metered `pi` seat, three bills for one answer nobody
    can use. (Named rather than restated: the number has drifted once already.)"""
    calls = _fake_cli(monkeypatch, ("", DENIED, 0), (FINDINGS, "", 0))
    out, err = panel.run_cli(["agy"], "antigravity (m)", attempts=3)
    assert len(calls) == 1
    assert out is None
    # Short-circuiting must not cost the diagnosis — that is the whole PR.
    assert "produced no output" in err and "permissions.allow" in err


def test_a_blank_reply_the_server_refused_is_not_retried(monkeypatch):
    """The other settled cause, and the one is_rejection already short-circuited
    on a non-zero exit. A CLI that swallows the refusal into a zero exit must not
    buy itself two more attempts by doing so."""
    calls = _fake_cli(monkeypatch, ("", REJECTED, 0), (FINDINGS, "", 0))
    out, err = panel.run_cli(["agy"], "antigravity (m)", attempts=3)
    assert len(calls) == 1 and out is None
    assert "does not exist" in err


def test_an_auto_denied_permission_is_not_retried_on_a_NON_zero_exit_either(monkeypatch):
    """The predicate is about whether another attempt would differ, not about the
    exit code that carried the news. A CLI that auto-denies a tool AND exits
    non-zero is as settled as one that swallows it into a zero exit — retrying it
    three times only spends the reviewer's slot on the same refusal."""
    calls = _fake_cli(monkeypatch, ("", DENIED, 1), (FINDINGS, "", 0))
    out, err = panel.run_cli(["agy"], "antigravity (m)", attempts=3)
    assert len(calls) == 1 and out is None
    assert "exited 1" in err and "permissions.allow" in err


def test_a_non_zero_exit_with_no_settled_cause_is_still_retried(monkeypatch):
    """The other side of it: rate limits and blips are why the retry exists."""
    calls = _fake_cli(monkeypatch, ("", "429 rate limited\n", 1), (FINDINGS, "", 0))
    out, err = panel.run_cli(["agy"], "antigravity (m)", attempts=3)
    assert len(calls) == 2 and out == FINDINGS and err is None


# A rate limit, on a run whose stderr ALSO mentions permissions somewhere — the
# normal state of a chatty CLI, and the shape a loose predicate turns into a
# lost vendor. Each of these used to match is_permission_denied outright.
UNRELATED = [
    # A real filesystem error, and a transient one as often as not.
    "EACCES: permission denied, open '/tmp/agy-cache/models.json'\n429 rate limited",
    # The CLI echoing its own config at startup.
    'settings: {"permissions.allow": ["Bash(ls:*)"]}\nerror: 429 rate limited',
    # One optional tool auto-denied, on a run that then dies of something else.
    "tool WebFetch was auto-denied by policy\nerror: 429 rate limited",
]


@pytest.mark.parametrize("stderr", UNRELATED)
def test_an_unrelated_permission_error_stays_retryable(monkeypatch, stderr):
    """The predicate has to prove another attempt is futile, and none of these
    do. Matching on `permission` and `denied` anywhere in the stream — or a bare
    `permissions.allow` — claimed all three, and because the short-circuit now
    also applies to non-zero exits, a reviewer failing on a 429 whose stderr
    merely MENTIONS a permission lost every remaining attempt: three before that
    change, one after. That is a whole vendor dropped from the round for a log
    line."""
    assert not panel.is_permission_denied(stderr)
    assert not panel.is_deterministic_failure(stderr)
    calls = _fake_cli(monkeypatch, ("", stderr, 1), (FINDINGS, "", 0))
    out, _err = panel.run_cli(["agy"], "antigravity (m)", attempts=3)
    assert len(calls) == 2 and out == FINDINGS


def test_the_denial_must_be_about_a_permission_on_the_same_line():
    """What the predicate does claim: the observed shape, and the sentence
    variants of it. A permission word and a headless-denial word, together."""
    assert panel.is_permission_denied(DENIED)
    assert panel.is_permission_denied(
        'a tool required the "write" permission and was auto-denied')
    assert panel.is_permission_denied(
        "warming up\nthe run needed a permission headless mode cannot prompt for\ndone")
    # Both words present, but on different lines and about different things.
    assert not panel.is_permission_denied(
        "checking permissions.allow\nunrelated: the socket was auto-denied by the proxy")


# ------------------------------------------- a blank reply that was not cheap

class _Clock:
    """A monotonic clock the fake CLI advances, so an attempt can "take" time
    without the test taking any."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


def _slow_cli(monkeypatch, seconds, *runs):
    clock = _Clock()
    monkeypatch.setattr(panel_seats, "time", clock)
    calls = _fake_cli(monkeypatch, *runs)

    real_run = subprocess.run

    def timed_run(cmd, **kwargs):
        clock.now += seconds
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", timed_run)
    return calls


def test_a_slow_blank_reply_is_not_retried(monkeypatch):
    """The retry is for a blank that came back FAST — one that plausibly never
    reached a model, which is the flake it exists to recover. A blank that spent
    real time thinking and still said nothing will spend it again, and three of
    those is the cost the timeout branch already refuses to pay ("it already
    burned the whole budget"). The elapsed time is the only thing that tells the
    two apart, and it was not being looked at."""
    calls = _slow_cli(monkeypatch, panel.BLANK_RETRY_MAX_S + 1,
                      ("", FLAKE, 0), (FINDINGS, "", 0))
    out, err = panel.run_cli(["agy"], "antigravity (m)", attempts=3)
    assert len(calls) == 1 and out is None
    assert "produced no output" in err and "not retried" in err


def test_a_fast_blank_reply_is_still_retried(monkeypatch):
    """The other side: the flake recovery is the point, and it survives."""
    calls = _slow_cli(monkeypatch, 1, ("", FLAKE, 0), (FINDINGS, "", 0))
    out, err = panel.run_cli(["agy"], "antigravity (m)", attempts=3)
    assert len(calls) == 2 and out == FINDINGS and err is None


def test_a_slow_NON_zero_exit_is_still_retried(monkeypatch):
    """The cap is about blank replies only. A non-zero exit fails fast by
    definition — whatever the clock says, the CLI decided, so the rate limit
    that ate one attempt is not a reason to skip the other two."""
    calls = _slow_cli(monkeypatch, panel.BLANK_RETRY_MAX_S + 1,
                      ("", "429 rate limited\n", 1), (FINDINGS, "", 0))
    out, _err = panel.run_cli(["agy"], "antigravity (m)", attempts=3)
    assert len(calls) == 2 and out == FINDINGS


def test_the_two_settled_causes_stay_distinguishable():
    """They are fixed in different files — a model pin in `.harness-rules`, a
    permission rule in the CLI's own settings.json — so a report that conflated
    them would send you to the wrong one."""
    assert panel.is_rejection(REJECTED) and not panel.is_permission_denied(REJECTED)
    assert panel.is_permission_denied(DENIED) and not panel.is_rejection(DENIED)
    assert panel.is_deterministic_failure(DENIED)
    assert panel.is_deterministic_failure(REJECTED)
    assert not panel.is_deterministic_failure(FLAKE)


# ---------------------------------------------------------------- review_llm

def test_the_reviewer_is_reported_skipped_with_the_cause(monkeypatch):
    """End to end: no findings, and a skip line naming the permission rule to add
    — not a reviewer that silently contributed nothing."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/agy")
    _fake_cli(monkeypatch, ("", DENIED, 0))
    got = panel.review_llm("antigravity", "gemini-3.7-flash-high", "p")
    assert got.findings == []
    assert "produced no output" in got.skip and "permissions.allow" in got.skip


def test_an_empty_reply_never_becomes_an_unstructured_finding(monkeypatch):
    """The fallback that keeps an unparseable reply as a raw markdown finding is
    for a reviewer that said something. Handing the judge an empty one is how a
    dead reviewer used to look like a live one — and `unstructured` is the flag
    the coverage veto reads, so a run that produced nothing must not set it
    either: "returned no structured reply" and "did not run" are different
    accounts, and only the second one is true here."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/agy")
    _fake_cli(monkeypatch, ("", DENIED, 0))
    got = panel.review_llm("antigravity", "m", "p")
    assert got.findings == [] and got.skip is not None
    assert got.unstructured is False


def test_review_llm_keeps_its_own_guard_against_a_blank_raw_finding(monkeypatch):
    """The LOCAL half of the guard, tested against a run_cli that breaks the
    invariant. Today it cannot: run_cli refuses to return whitespace-only stdout.
    But that invariant lives ~350 lines away in a docstring, and the day it is
    relaxed — a new caller, a check_output=False variant, a mocked run_cli in
    some future test — this is all that stands between the judge and an empty
    finding flagged `unstructured`: a dead reviewer wearing a live one's
    clothes, which is the failure this whole file exists to kill."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/agy")
    monkeypatch.setattr(panel_seats, "run_cli", lambda *a, **k: ("   \n", None))
    got = panel.review_llm("antigravity", "m", "p")
    assert got.findings == [] and got.unstructured is False
    assert "produced no output" in got.skip


def test_prose_is_still_kept_as_a_raw_finding(monkeypatch):
    """The regression guard on the above: output that isn't JSON but IS a review
    still reaches the judge rather than being discarded as unparseable — and is
    flagged `unstructured`, because nothing it might have declared about its own
    coverage survived the parse."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/agy")
    _fake_cli(monkeypatch, ("The retry loop double-counts on line 12.", "", 0))
    got = panel.review_llm("antigravity", "m", "p")
    assert got.skip is None and len(got.findings) == 1
    assert "double-counts" in got.findings[0].detail
    assert got.unstructured is True


# The other half of the same reviewer failure, from the issue's follow-up: exit
# 0 with stdout that is neither empty nor a review. `agy` had assembled 18
# findings — the transcript shows them — but the print-mode turn ended while the
# agent was narrating a wait, so the answer never reached stdout.
NARRATION = ("I have launched the pytest suite in the background to inspect test results "
             "while analyzing the diff. I will wait for it to complete.\n"
             "I am waiting for the test suite task to finish execution.")


def test_narration_passes_the_emptiness_guard_but_is_still_not_a_clean_review(monkeypatch):
    """This one the emptiness guard cannot catch, and deliberately is not made to:
    "no parseable findings array" would also discard the reviewer that answered in
    prose because it had something to say, and losing real findings to a formatting
    miss is the more expensive of the two errors.

    What it must never do is read as a reviewer that engaged and found little. It
    does not: the reply is flagged `unstructured`, which the coverage veto turns
    into a stated reason the round is not evidence of a quiet PR, and the judge
    rules on the text rather than the panel silently counting it."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/agy")
    _fake_cli(monkeypatch, (NARRATION, "", 0))
    got = panel.review_llm("antigravity", "m", "p")
    assert got.unstructured is True
    # None of its own coverage survived the parse, so it declared nothing — which
    # is null (never said), not [] (asked, and had no gap).
    assert got.could_not_assess is None
    veto = panel.coverage_veto(
        {"antigravity": {"ran": True, "unstructured": True}}, None, [], len(NARRATION), ci_status="PASS")
    assert any("no structured reply" in v for v in veto)


# ---------------------------------------------------------------- judge

def test_the_judge_reports_no_output_rather_than_unparseable(monkeypatch):
    """The judge shared the bug: an empty verdict was reported as "unparseable",
    blaming the reply's shape for a run that produced no reply at all — and
    everything is kept when the judge can't rule, so the reason is the only
    signal that the adjudication silently stopped happening."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    _fake_cli(monkeypatch, ("", DENIED, 0))
    findings, _gaps = panel.parse_reply("claude", FINDINGS)
    out, skip, _note = panel.adjudicate(panel.cluster_findings(findings),
                                        "diff", "opus", 19)
    # Nothing is suppressed when the judge cannot rule — the finding survives,
    # unjudged, and the skip reason is the only account of why.
    assert [c.verdict for c in out] == ["unjudged"]
    assert "produced no output" in skip and "unparseable" not in skip
    assert "permissions.allow" in skip
