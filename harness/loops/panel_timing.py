#!/usr/bin/env python3
"""Where a panel round's wall clock actually went (#192).

`/panel-review-pr` is slow and the working hypothesis has been that the serial
fixer is the reason. That hypothesis could not be checked, because roughly half a
cycle's wall clock was unattributable: `review_reviewers.duration_ms` said how
long each seat *thought*, and nothing anywhere said how long the round spent
before dispatching them, how long the judge took, or how long the fix phase
between two rounds ran for. A number for one phase and silence for the rest is
not a measurement — it is the shape that produces a hunch, and #192 exists
because a hunch was about to be optimised against.

So this module answers four questions about a completed round, and the payload
and the PR comment both carry the answers:

* **how long each seat took** — as a finish offset from the moment the round
  dispatched them, which is the grain the next question needs.
* **how long the judge took** — its own phase, because the judge runs *after*
  the whole executor has joined and so inherits the slowest seat's wait before
  it starts.
* **how much of the round was spent waiting on one thing** — `gated_ms`, the
  span in which every seat but one had finished and the round was still held.
  That is the number the "parallel but gated on its slowest seat" claim is
  about, and it was the one nothing measured.
* **how long the fix phase before this round took** — the span between round
  *r* finishing and round *r+1* starting, which is the fixer, the verify and the
  push together.

**Phases partition the round exactly.** `setup + seats + judge + wrapup ==
round_ms`, because each `mark()` closes at the previous one, so a reader can
tell "the judge took four minutes" from "four minutes went somewhere near the
judge". What is deliberately *outside* the partition is everything after the
payload is built — the report render and the board POST — and `measured_to`
says so rather than leaving an unexplained shortfall against a stopwatch.

**The fix phase has two sources and they are not equal**, so it records which
one it used rather than presenting one number:

* ``payload`` — the previous round's own recorded `finished_at` against this
  round's `started_at`. Exact, and it includes the push and any wait for CI,
  which is what a caller deciding whether the fixer is the slow part wants.
* ``commits`` — `git show -s --format=%ct` on the two rounds' `head_sha`, the
  derivation #192 proposes for the cycles already on the board. It needs no
  field that did not exist before, and it is a **lower bound**: it starts at the
  first fix commit rather than when the round ended, and stops at the last one
  rather than when the next round began. It also breaks in exactly the two
  places it would matter most — a round that pushed nothing leaves the head
  unmoved, and a rebase between rounds leaves the earlier commit unreachable —
  and both of those are reported as `ms: null` with a reason, never as a zero.

A null is never a zero here, for the reason the payload's own comments give
about `changed_files_total`: "nobody could say" and "it was none" are different
facts, and a fix phase reported as 0ms is a claim that the fixer was
instantaneous.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field

#: A commit id, checked before it is handed to `git`. The right-hand sha comes
#: from `gh` and the left-hand one from a baseline payload, and `load_baseline`
#: already refuses a value that is not a commit — this is the second check at the
#: point of use, because what is interpolated here is an argv element for a
#: subprocess and a value carrying a `-` would be read as a flag.
_SHA = re.compile(r"[0-9a-fA-F]{7,40}")

#: Seconds. A `git show` against the local checkout is a millisecond operation;
#: this bounds the case where the path is a stale network mount rather than the
#: case where the repo is large.
GIT_TIMEOUT = 15


def hms(ms: int | float | None) -> str:
    """`824000` -> `13m 44s`. `None` -> `not measured`.

    Seconds are kept below a minute and dropped above an hour: the numbers this
    renders are minutes-to-tens-of-minutes, and "1h 04m 12s" spends three
    characters on a precision nobody reading a review comment acts on."""
    if ms is None:
        return "not measured"
    total = int(ms) // 1000
    if total < 10:
        # One decimal below ten seconds. A round's phases are minutes, but its
        # `setup` is routinely under a second and "setup 0s" reads as a phase that
        # did not happen rather than one that was cheap.
        return f"{int(ms) / 1000:.1f}s"
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


@dataclass
class RoundClock:
    """The round's stopwatch: contiguous phases, plus each seat's finish offset.

    Constructed at the top of :func:`panel.run`, so `started_at` is the round's
    real beginning and not the beginning of the part that happened to be
    instrumented. Wall clock comes from `time.monotonic` (immune to the clock
    being stepped mid-round); `started_at`/`finished_at` are `time.time`, because
    those two are compared against *another process's* readings one round later
    and a monotonic reading means nothing outside the process that took it.
    """

    started_at: float = field(default_factory=time.time)
    _t0: float = field(default_factory=time.monotonic)
    #: name -> ms, in the order the marks were taken. Contiguous by construction.
    phases: dict[str, int] = field(default_factory=dict)
    #: seat -> ms from the most recent mark, which is the dispatch instant.
    seat_finish_ms: dict[str, int] = field(default_factory=dict)
    _last: float = field(default=0.0)

    def __post_init__(self) -> None:
        self._last = self._t0

    def elapsed_ms(self) -> int:
        """The whole round so far, from construction."""
        return int((time.monotonic() - self._t0) * 1000)

    def round_ms(self) -> int:
        """The round's total, as the phases account for it.

        The SUM of the phases wherever any were marked, not a fresh stopwatch
        read: the two differ by however long assembling the timing block itself
        takes, and a total that quietly exceeds its own parts is the shape that
        makes "the judge took four minutes" unfalsifiable. A path that marked no
        phase at all — the per-repo refusal, which returns before anything is
        fetched — has nothing to sum and reports elapsed time instead."""
        return sum(self.phases.values()) if self.phases else self.elapsed_ms()

    def mark(self, name: str) -> int:
        """Close a phase at now, measured from the previous mark.

        Re-marking a name ADDS to it rather than replacing it, so a phase entered
        twice reports the total time in it rather than only the last visit."""
        now = time.monotonic()
        span = int((now - self._last) * 1000)
        self.phases[name] = self.phases.get(name, 0) + span
        self._last = now
        return span

    def seat_done(self, name: str) -> int:
        """Record that a seat finished, offset from the most recent mark.

        Called between the mark that closes setup and the mark that closes the
        seat phase, so "the most recent mark" is the instant the executor was
        handed the work — which is what makes these offsets comparable with each
        other. The FIRST report for a seat wins: a seat cannot finish twice, and
        a second call would mean something re-entered the collection loop."""
        return self.seat_finish_ms.setdefault(
            name, int((time.monotonic() - self._last) * 1000))


def watch(clock: RoundClock, futures: dict, echo=None) -> None:
    """Block until every seat has finished, recording WHEN each one did.

    The collection loop below this reads the futures back in submission order,
    which is what keeps finding ids and the reported reviewer list deterministic
    across runs — two things a baseline chain depends on. That order is also why
    the round could not previously say which seat it was waiting for: by the time
    a slow seat's `.result()` returned, every faster seat had already been
    collected and nothing had recorded that they finished first.

    So this runs ahead of it and only observes. It never touches `.result()`, so
    a seat that raised still raises in the same place it always did, at the same
    point in the same loop — the failure semantics are untouched, and what is
    added is a finish time and a line saying who the round is still on.

    It does NOT make the round faster and is not meant to: the executor still
    joins on everything before the judge runs, because starting the judge on
    part of a panel is the one shortcut #192 rules out. What it makes possible is
    saying, afterwards and while it is happening, exactly how much the joining
    cost."""
    from concurrent.futures import as_completed

    pending = dict(futures)
    by_future = {f: n for n, f in futures.items()}
    for fut in as_completed(list(by_future)):
        name = by_future[fut]
        ms = clock.seat_done(name)
        pending.pop(name, None)
        if echo is not None:
            waiting = (f" — still waiting on {', '.join(sorted(pending))}" if pending
                       else " — all seats in")
            print(f"  · {name} finished in {hms(ms)}{waiting}", file=echo)


def seat_attribution(finish_ms: dict[str, int], round_ms: int) -> dict:
    """Which seat held the round, and how long it held it alone.

    `gated_ms` is the slowest seat's finish minus the *second* slowest's: the
    span in which every other seat had finished with its findings undelivered and
    the round — the judge, and the fix phase behind it — was waiting on one
    process. It is the measurement behind #192's "parallel but gated on its
    slowest seat", and it is deliberately not the slowest seat's whole duration:
    the time in which two seats are still running is not attributable to either.

    With fewer than two seats it is **0 rather than the seat's duration**. One
    seat running alone is not a seat holding up the others; there are no others.
    Reporting its full duration there would make a single-vendor `--reviewers
    codex` run look maximally gated, which is the reading that would send someone
    to parallelise a panel of one.

    `seat_idle_ms` is the complementary quantity — the seat-time (not wall clock)
    spent finished and undelivered, summed over the seats that were not last. Two
    numbers because they answer two questions: `gated_ms` is what a per-seat
    timeout would buy back, `seat_idle_ms` is how much work sat ready while the
    round could not use it."""
    timed = {n: ms for n, ms in finish_ms.items() if isinstance(ms, int)}
    if not timed:
        return {"slowest_seat": None, "slowest_seat_ms": None,
                "gated_ms": 0, "gated_pct": 0.0, "seat_idle_ms": 0}
    ranked = sorted(timed.items(), key=lambda kv: (-kv[1], kv[0]))
    slowest, slowest_ms = ranked[0]
    gated = slowest_ms - ranked[1][1] if len(ranked) > 1 else 0
    return {
        "slowest_seat": slowest,
        "slowest_seat_ms": slowest_ms,
        "gated_ms": gated,
        # Of the WHOLE round, not of the seat phase: the point of the number is
        # what it is worth against the round's total cost, and a share of the
        # seat phase alone would read as larger the more the panel was already
        # doing well.
        "gated_pct": round(100.0 * gated / round_ms, 1) if round_ms > 0 else 0.0,
        "seat_idle_ms": sum(slowest_ms - ms for _, ms in ranked[1:]),
    }


def _commit_time(repo_path: str, sha: str) -> int | None:
    """Unix commit time of `sha` in the checkout at `repo_path`, or None.

    None covers every way this can fail — no git, no checkout, a commit that is
    not in this clone (the rebase case), a `%ct` that is not an integer — because
    each of them means the same thing to the caller: this end of the range cannot
    be read, so the range cannot be reported."""
    if not repo_path or not _SHA.fullmatch(sha or ""):
        return None
    try:
        out = subprocess.run(["git", "-C", repo_path, "show", "-s", "--format=%ct", sha],
                             capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return int((out.stdout or "").strip().splitlines()[0])
    except (IndexError, ValueError):
        return None


def fix_phase(started_at: float, *, prior_finished_at: float | None = None,
              prior_round: int | None = None, prior_head_sha: str | None = None,
              head_sha: str | None = None, repo_path: str = "") -> dict:
    """How long the fix phase that ran into this round took.

    Two sources, tried in that order and always named in the result — see the
    module docstring for why they are not interchangeable. Every failure is a
    `ms: null` with a `note` saying which end could not be read, because the two
    cases that break the derivation (nothing pushed, rebased between rounds) are
    exactly the two a reader would otherwise take for "the fix phase was
    instant"."""
    out: dict = {"ms": None, "source": None, "from_round": prior_round, "note": None}
    if prior_round is None and prior_finished_at is None and not prior_head_sha:
        # Round 1, or a review-only run. No fix phase ran into this round at all,
        # which is a different statement from one that could not be measured — so
        # no note, and the report prints nothing rather than "not measured".
        return out
    if isinstance(prior_finished_at, (int, float)) and prior_finished_at > 0:
        gap = started_at - float(prior_finished_at)
        if gap >= 0:
            out.update(ms=int(gap * 1000), source="payload",
                       note="from the previous round's recorded finish to this round's "
                            "start — the fixer, the verification and the push together")
            return out
        # Negative means the two readings came off clocks that disagree, or the
        # baseline is not the round it claims to be. Reported, and RETURNED on —
        # not silently retried against the commit derivation as though nothing odd
        # had happened. The two sources disagree about which round came first, and
        # publishing whichever one still produces a number would present a fix
        # phase whose own evidence contradicts it, with nothing saying so.
        out["note"] = (f"the previous round records a finish {abs(gap):.0f}s AFTER this "
                       "round started — the two readings are not from one timeline, so "
                       "the gap between them is not a fix phase, and the commit-time "
                       "derivation was not tried either: something about this baseline "
                       "is wrong and a second number would not say so")
        return out
    if not (prior_head_sha and head_sha):
        out["note"] = out["note"] or ("the previous round recorded no head commit, so "
                                      "there is no earlier end to measure the fix phase "
                                      "from")
        return out
    if prior_head_sha == head_sha:
        out["note"] = ("the head did not move between the two rounds, so the fix phase "
                       "cannot be derived from commit times — a round that pushed "
                       "nothing is not a round that fixed nothing")
        return out
    was, now = _commit_time(repo_path, prior_head_sha), _commit_time(repo_path, head_sha)
    if was is None or now is None:
        missing = prior_head_sha[:8] if was is None else head_sha[:8]
        out["note"] = (f"commit {missing} could not be read from the local checkout, so "
                       "the fix phase could not be derived — a rebase between rounds "
                       "leaves the earlier round's head unreachable, which is the case "
                       "this derivation is least able to survive")
        return out
    if now < was:
        out["note"] = (f"the commit this round reviewed ({head_sha[:8]}) is older than the "
                       f"one the previous round did ({prior_head_sha[:8]}) — the branch "
                       "moved backwards, so the span between them is not a fix phase")
        return out
    out.update(ms=(now - was) * 1000, source="commits",
               note="derived from the two rounds' head commit times — a LOWER bound: it "
                    "starts at the first fix commit rather than when the last round "
                    "ended, and stops at the last one rather than when this round began")
    return out


def timing_block(clock: RoundClock, fix: dict, *, measured_to: str = "payload") -> dict:
    """The round's `timing` payload entry, assembled once and read by both consumers.

    One structure for the payload and the report line, for the same reason the
    run payload itself is built on every path: a report and a record that derive
    the same number separately are a report and a record free to disagree about
    it."""
    round_ms = clock.round_ms()
    return {
        # What the phases below add up TO. `payload` means the report render and
        # the board POST are after this reading and are not in any phase — stated
        # rather than left as an unexplained shortfall against a stopwatch.
        "measured_to": measured_to,
        "started_at": clock.started_at,
        "finished_at": time.time(),
        "round_ms": round_ms,
        "phases": dict(clock.phases),
        "seat_finish_ms": dict(clock.seat_finish_ms),
        **seat_attribution(clock.seat_finish_ms, round_ms),
        "fix": fix,
    }


def timing_line(timing: dict) -> str:
    """The one report line the PR comment carries.

    On the comment and not only in the payload because the operator deciding
    whether to go another round is the reader this measurement is for, and the
    payload is not where they are looking. It leads with the total, names the
    phases in the order they ran, and then says what held the round — which is
    the sentence #192 is about."""
    ph = timing.get("phases") or {}
    parts = [f"{k} {hms(ph[k])}" for k in ("setup", "seats", "judge", "wrapup")
             if ph.get(k) is not None]
    line = f"**Wall clock:** {hms(timing.get('round_ms'))}"
    if parts:
        line += " — " + ", ".join(parts)
    slowest = timing.get("slowest_seat")
    if slowest:
        line += (f". Slowest seat {slowest} at {hms(timing.get('slowest_seat_ms'))}"
                 f", holding the round alone for {hms(timing.get('gated_ms'))} "
                 f"({timing.get('gated_pct')}% of it)")
    fix = timing.get("fix") or {}
    if fix.get("ms") is not None:
        src = {"payload": "recorded", "commits": "derived from commit times"}.get(
            fix.get("source"), fix.get("source") or "")
        line += f". Fix phase before this round: {hms(fix['ms'])} ({src})"
    elif fix.get("note"):
        line += f". Fix phase before this round: not measured — {fix['note']}"
    return line + "."
