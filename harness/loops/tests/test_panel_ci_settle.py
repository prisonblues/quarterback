"""A round waits for a PENDING build rather than telling the seats it is unknown (#501).

Measured fleet-wide over five days before this existed: 19 panel rounds, `ci_status`
PENDING on 9 of them, and `stop_confident` true on **none**. A round takes 20-40
minutes here and a build about four and a half, so the panel reliably told reviewers
"CI is still running" about a build that finished during the round — and a reviewer
told so declares "could not assess: CI result is unknown", which `coverage_veto`
counts and `round_stop` turns into `confident: false`.

**Why the cause is removed rather than the symptom filtered.** The obvious fix is a
second CI read when the round ends. It cannot work: the veto is not the panel's own,
it is a REVIEWER's free-form prose, and `coverage_veto`'s standing rule is that
exemptions come off recorded state and *"never off the wording of a message or a
declaration"*. Answering it afterwards would mean pattern-matching model text for
something CI-shaped — which that rule forbids, because a regex over prose exempts a
genuine round-specific gap whose wording happens to match and misses the structural
one that does not.

Run: uv run --with pytest pytest harness/loops/tests/test_panel_ci_settle.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import panel_scope  # noqa: E402


class Clock:
    """A monotonic clock the test drives, so a ten-minute budget costs no seconds."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def reader(*answers):
    """A `review_ci` that returns each answer in turn, then repeats the last."""
    calls: list[int] = []

    def read(_repo, _pr):
        calls.append(1)
        i = min(len(calls) - 1, len(answers) - 1)
        return answers[i]

    read.calls = calls  # type: ignore[attr-defined]
    return read


SETTLED = ("PASS", [], None)
PENDING = ("PENDING", [], None)
FAILED = ("FAIL", ["app suite"], None)


def settle(read, **kw):
    clock = Clock()
    return panel_scope.review_ci_settled(
        "acme/one", 1, read=read, now=clock.now, sleep=clock.sleep, **kw)


def test_a_settled_build_is_not_waited_on_at_all():
    """The common case must cost nothing — most rounds start against a build that
    has already finished, and this must not add a poll to those."""
    read = reader(SETTLED)
    status, _failing, _skip, waited = settle(read)
    assert (status, waited) == ("PASS", 0.0)
    assert len(read.calls) == 1, "a settled build was polled more than once"


def test_a_pending_build_is_waited_for_and_the_wait_is_reported():
    """The whole point: the seats are told PASS, so no reviewer has a gap to
    declare and nothing needs exempting afterwards."""
    read = reader(PENDING, PENDING, SETTLED)
    status, _failing, _skip, waited = settle(read, poll=20, budget=600)
    assert status == "PASS"
    assert waited == 40.0, "the wait must be reported so a round can account for it"


def test_a_failing_build_stops_the_wait_immediately():
    """FAIL is an answer, not a reason to keep asking — and it is the answer that
    most refutes a class of finding, so the seats should have it at once."""
    read = reader(PENDING, FAILED)
    status, failing, _skip, _waited = settle(read, poll=5, budget=600)
    assert status == "FAIL"
    assert failing == ["app suite"]


def test_a_build_that_never_settles_falls_back_to_today_s_behaviour():
    """Bounded, and it fails in the honest direction: still PENDING at the end is
    reported exactly as it is now, veto and all. Waiting can only ever turn an
    unknown into a fact — never a fact into a nicer one."""
    read = reader(PENDING)
    status, _failing, _skip, waited = settle(read, poll=100, budget=250)
    assert status == "PENDING"
    assert waited >= 250, "the budget must actually be spent before giving up"


def test_the_budget_is_not_overrun_by_a_long_poll():
    """A poll longer than the remaining budget must not push the wait past it —
    the last sleep is clamped, so a 10-minute budget cannot become 15."""
    read = reader(PENDING)
    _status, _failing, _skip, waited = settle(read, poll=400, budget=500)
    assert waited <= 500, f"overran the budget by {waited - 500:.0f}s"


def test_the_reader_is_injected_so_a_stubbed_round_stays_off_the_network():
    """A dozen suites stub `panel.review_ci`. A wrapper resolving this module's own
    binding instead would slip past every one of them — shelling out to `gh` in
    tests and, on a PENDING answer, sitting on the whole budget."""
    import inspect
    assert "read" in inspect.signature(panel_scope.review_ci_settled).parameters
    import panel
    src = inspect.getsource(panel.run)
    assert "review_ci_settled(" in src and "read=review_ci" in src, (
        "run() must pass its own review_ci, or every existing stub is bypassed")
