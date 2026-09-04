"""Does a click on a dashboard row actually do the thing?

Drives the real app through Textual's pilot — real widgets, synthetic clicks —
with the browser and tmux calls stubbed, because a test that opened Chrome and
moved the cursor of a live seat screen would be its own bug.

SKIPPED unless the machine can actually run the dashboard: textual and the board
client come from mcp/'s environment. CI DOES run this file — `tests.yml` gives the
dashboard step the `tui` extra precisely so it executes, and asserts a non-zero
pass count so a silently-skipping module fails the build. What skips there is the
handful of tests that want a configured board and a `gh`, which CI deliberately
does not have.
Two defects came out of it that hand-testing had passed:

  * a single click did nothing. DataTable treats a click on any row but the
    cursor's as "move the cursor" and selects nothing, and it consumes the Click
    rather than letting it bubble to a handler on the App.
  * the PR table was not on screen at all under `height: auto`, so its rows could
    not be clicked — which looked identical to the click handler not working.

Run: pytest harness/tests/test_qb_dash.py
"""

from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import math
import os
import subprocess
import sys
import time
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load_app():
    """Import the dashboard module itself, not the launcher that finds a Python
    for it — harness/bin/qb-dash-tui is now bash."""
    loader = importlib.machinery.SourceFileLoader("qb_dash_tui", str(BIN / "qb-dash-tui.py"))
    spec = importlib.util.spec_from_loader("qb_dash_tui", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch):
    """The module docstring's promise, enforced rather than remembered.

    "With the browser and tmux calls stubbed" was already written at the top of
    this file when a test spent every run of the suite on a real `xdg-open` of
    another repo's PR — the convention was there, only nothing checked it. A
    test that reaches the launcher now fails loudly and names itself, which is
    the difference between a tab a person cannot trace and a red test.

    NARROW ON PURPOSE, and a pass-through for everything else: `Popen` is how
    the harness talks to `git` and to a private tmux server, and several tests
    here mean to. Only `xdg-open` — the one target that escapes the machine and
    lands on somebody's screen — is refused.
    """
    real_popen = subprocess.Popen

    def guarded(args, *rest, **kwargs):
        argv = args if isinstance(args, (list, tuple)) else [args]
        if argv and str(argv[0]) == "xdg-open":
            raise AssertionError(
                f"a test reached the real browser: xdg-open {' '.join(map(str, argv[1:]))}. "
                "Stub `app.open_url` — a suite that opens Chrome is its own bug.")
        return real_popen(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded)


# TWO conditions, and keeping them apart is what lets any of this run in CI.
#
# Every test here needs rich and textual, because every one of them drives the
# real widgets. Only SOME need a board and a `gh` that can see the repo: those
# read whatever is open today and click a row of it. When the two were one
# check, a machine without a board skipped the lot — including the tests whose
# data is a literal in this file — so the stubbed half never ran anywhere but a
# developer's laptop, which is the same as not having been written.
#
# It no longer wants mcp_server — the board client is stdlib now — so this
# passes under the interpreter the nix build carries, not only under a
# developer's venv.
def _why_no_tui() -> str | None:
    try:
        import rich
        import textual  # noqa: F401
    except ImportError:
        return "textual/rich are not importable"
    return None


def _why_no_board() -> str | None:
    sys.path.insert(0, str(BIN))
    try:
        import qbdata
        qbdata.resolve_config()
    except Exception as exc:                       # noqa: BLE001
        return f"no board configured here ({type(exc).__name__})"
    return None


_NO_TUI = _why_no_tui()
_NO_BOARD = _why_no_board() if _NO_TUI is None else "textual/rich are not importable"

pytestmark = pytest.mark.skipif(_NO_TUI is not None, reason=_NO_TUI or "")

#: For the tests that click whatever the repo and the board actually have open
#: today. They are still the ones that found the defects worth finding, so they
#: are not weakened — they are just no longer the reason the rest cannot run.
#:
#: OPT-IN, and #644 is the reason. These four read the live board and the live
#: repo, so their row indices are a function of what the fleet happens to be doing
#: while the suite runs — and this repo runs several agents that post to that board
#: and open PRs against that repo at once. Three of the four failures anyone has
#: recorded for this file are among these four tests, out of 107 in it.
#:
#: The value being protected is what a green local suite MEANS. CI already has it:
#: `tests.yml` runs the dashboard suites serially and with no board configured, so
#: these skip there and are meant to. A developer or an agent running
#: `pytest harness/tests` on a box that HAS a board was getting a different suite
#: from the one CI gates on, and finding out by way of a failure attributed to
#: whatever they had just changed. Now both mean the same thing.
#:
#: Not deleted, and not stubbed. They are the tests that found the defects worth
#: finding, and stubbing the data is how you keep the clicking and lose the point —
#: the claim is that the dashboard works on the board as it really is. Ask for them
#: by name: `QB_DASH_LIVE=1 pytest harness/tests/test_qb_dash.py`.
#: Read as a switch and not as a string, because `bool("0")` is True and this file
#: documents `QB_DASH_LIVE=1` in four places — so a reader who tried `=0` to turn the
#: live tests OFF would have turned them on. That is `low_severity_fix_lines`' own
#: lesson one repo over: every non-empty string is truthy in Python, `"false"`
#: included, and a setting whose job is to gate something stops doing it.
_LIVE_ON = os.environ.get("QB_DASH_LIVE", "").strip().lower() in {"1", "true", "yes", "on"}
_NO_LIVE = _NO_BOARD or (
    None if _LIVE_ON else
    "live board/repo data is opt-in (#644): set QB_DASH_LIVE=1 to run these")
needs_live_data = pytest.mark.skipif(_NO_LIVE is not None, reason=_NO_LIVE or "")


def _numbered_cell(row) -> str:
    """The '#123' cell of a rendered row, whatever column it currently sits in."""
    for cell in row:
        text = str(cell).strip()
        if text.startswith("#") and text[1:].isdigit():
            return text[1:]
    raise AssertionError(f"no #number cell in row: {[str(c) for c in row]}")


def _need_rows(table, what: str, err: str | None) -> None:
    """A click test needs something to click, and live data is not guaranteed.

    An empty table is two different things and they deserve different answers: a
    repo with no open PRs today is nothing to report, while `gh` refusing is a
    failure that must not go green — so the skip carries the error when there
    was one.
    """
    if table.row_count:
        return
    if err:
        raise AssertionError(f"gh could not list {what}: {err}")
    pytest.skip(f"no open {what} on the repo — nothing to click")


async def _settle_table(pilot, table, tries: int = 40) -> None:
    """Wait until `table`'s rows stop changing, or give up and let the assertion talk.

    Stubbing the workers is not enough on its own: one already in flight when the
    stub goes in still lands, and rebuilds the table after it. That is invisible
    with one table per source, which is what these drivers were written against —
    a live `gh` tick rewrote OPEN PRs while the driver was clicking ISSUES. WORK
    is rebuilt by all three (#589), so the late arrival lands on the very table
    under the pointer.

    On the CONTENTS and not on the region: `_click_row` already waits for the
    geometry to hold still, and the failure this one is for is a table that has
    not moved a pixel and is holding different work.
    """
    previous, still = None, 0
    for _ in range(tries):
        now = tuple(str(rk.value) for rk in table.rows)
        still = still + 1 if now and now == previous else 0
        if still >= 2:
            return
        previous = now
        await pilot.pause(0.1)


def _find_row(app, table, prefix: str) -> int:
    """The index of the first row whose key names a `prefix` kind, else -1.

    WORK holds four kinds of row where there were four tables (#589), so a driver
    that wants a PR can no longer take row 0 and know what it got — the review
    queue is drawn above the plan, and the plan is mostly issues. Asking by kind
    is what those drivers used to get for free by naming a table.
    """
    for i, rk in enumerate(table.rows):
        if str(rk.value).startswith(prefix):
            return i
    return -1


async def _click_row_index(pilot, table, index: int | str, x: int = 4,
                           scroll: bool = False, column: int | None = None) -> str:
    """Click a row of `table`, wherever on screen that row has ended up.

    `index` is a row number or a ROW KEY, and a key is what the live drivers pass.
    WORK is rebuilt by three workers (#589), so an index is only as good as the
    tick it was computed on — a rebuild between reading `wanted[0]` and clicking
    it hands the click a different piece of work, which showed up as a `/fix-issue`
    launched for the wrong number and, once, as a plan row where an issue row had
    been. A key is stable across a rebuild because it names the thing, not the
    place: `issue:owner/repo#12` is the same row wherever it has moved to.

    The offset-arithmetic version of this — "the ＋ is at `len(seats) + 1`" —
    counted the header and nothing else, and stopped being true when SEATS became
    a share of the pane instead of its own content-sized panel (#589): the same
    click then landed a row high, on the last seat, and reported closing it.

    `column` asks for a CELL rather than a column of pixels, and is what the verb
    clicks want. `VERB_COLUMN + 2` was a fair guess at where column 1 begins while
    every table started with a one-character glyph; WORK's cells are sized to their
    content, so the guess is a guess. Scanning for the cell the compositor says is
    there cannot be off by a character.

    Still a CLICK, and still the claim the ＋ tests make: the row has to be on
    screen and under the mouse. What it stops asserting is where the table chose
    to put it, which was never the point.

    Returns the key of the row it clicked, so a caller can assert against THAT
    rather than against an index it read a moment earlier — WORK is rebuilt by
    three workers, so an index is only as good as the tick it was computed on.
    """
    # `scroll` is OFF by default and that is the point of the ＋ tests: their claim
    # is that the row is reachable WITHOUT scrolling, so a helper that quietly
    # scrolled would turn them green against the defect they exist for. The live
    # drivers pass it, because a table holding the whole plan legitimately has the
    # row they want below the fold and a reader would scroll to it.
    # IMPORTED HERE, not at module scope. This module is meant to SKIP without
    # textual — `_why_no_tui` decides that below the imports — and a top-level
    # `from textual...` turns the skip into a collection ERROR on the CI job that
    # runs the harness with no dashboard extras. That job exists to prove the rest
    # of the harness needs neither textual nor rich, so breaking it is a red build
    # about a claim nobody made.
    from textual.coordinate import Coordinate

    if isinstance(index, str):
        # Resolved HERE and not by the caller, so it is resolved as late as
        # possible — after every pause above this line has already happened.
        wanted = index
        index = next((i for i, rk in enumerate(table.rows)
                      if str(rk.value) == wanted), -1)
        if index < 0:
            raise AssertionError(f"row {wanted!r} is not in {table.id} any more")
    if scroll:
        table.move_cursor(row=index, animate=False)
        # `_scroll_cursor_into_view` is private and is called by name on purpose:
        # `move_cursor` alone leaves the cursor on a row below the fold, and the
        # public `scroll_to` takes lines rather than rows, which is a second
        # arithmetic to keep in step with the header. If a Textual release renames
        # it this raises AttributeError here rather than clicking the wrong row.
        table._scroll_cursor_into_view(animate=False)

    # SCANNED UNTIL IT IS THERE, not judged on one read. `get_style_at` reads the
    # compositor, and a scroll that has not repainted yet answers with the layout
    # as it was — so a single pass either found no `y` at all (and raised about a
    # row that was on screen by the time anybody looked) or found one from the old
    # positions and handed the click whatever the new ones put there. Both were
    # seen on this suite, a few runs apart, which is the signature of a paint that
    # has not caught up rather than of anything the dashboard did.
    # FROM y=0 ON A TABLE WITH NO HEADER. Every table here had one when this was
    # written, so the scan started below it — and on the chip bar, which is
    # `show_header=False` and one line tall, `range(1, 1)` is empty and the helper
    # reported a row that was on screen and under the mouse as unreachable.
    top = 1 if table.show_header else 0
    for _ in range(40):
        for y in range(top, table.region.height):
            for dx in (range(table.region.width) if column is not None else (x,)):
                meta = table.screen.get_style_at(table.region.offset.x + dx,
                                                 table.region.offset.y + y).meta
                if meta.get("row") != index:
                    continue
                if column is not None and meta.get("column") != column:
                    continue
                key = str(table.coordinate_to_cell_key(
                    Coordinate(index, 0)).row_key.value)
                await _click_row(pilot, table, (dx, y), row=index)
                return key
        await pilot.pause(0.1)
    raise AssertionError(
        f"row {index}{'' if column is None else f' column {column}'} of {table.id} "
        f"is not on screen — a click cannot reach it "
        f"(region {table.region}, {table.row_count} rows)")


#: How many 0.05s reads `_click_row` spends waiting for a pane to stop moving.
#: The loop leaves the moment two consecutive reads agree, so on a healthy run this
#: costs 0.1s whatever it is set to and the ceiling is paid only when something is
#: genuinely wrong.
#:
#: A count of READS, and stated precisely because the imprecise version is
#: misleading: every unsuccessful read still costs a `pilot.pause(0.05)`, so this is
#: also a wall-clock floor of ~11.95s that STRETCHES under contention rather than a
#: quantity independent of time — 239 pauses and not 240, because the read that
#: succeeds does not pause after itself. The old 60 was ~2.95s by the same arithmetic.
#: The exact figure does not matter and the off-by-one is written down anyway: a
#: comment that rounds in its own favour is how the rest of this file's false claims
#: started.
#:
#: The distinction that survives is about what is guaranteed. A real deadline
#: (`while time.monotonic() < start + N`) expires while the app is descheduled, so a
#: loaded box buys the pane FEWER chances to settle exactly when it needs more. A
#: read count guarantees 240 observations however long the scheduler takes to
#: deliver them. That is why the shape is a count and not a deadline; the 4x is what
#: makes the count large enough to survive a box under load.
_SETTLE_READS = int(os.environ.get("QB_DASH_SETTLE_READS", "240"))


async def _click_row(pilot, table, offset, row: int | None = None) -> None:
    """Click `table` at `offset`, once the pane has stopped moving under it.

    `row_count` says the rows are IN the table; it does not say the pane has
    finished deciding where the table is. `Pilot.click` resolves the widget's
    position when it is CALLED, so a click aimed while something above is still
    arriving is delivered a row high — onto the header, which
    `ClickTable.on_click` refuses as `row: -1`. That reads as a click that did
    nothing, and it cost about two failures in six runs of
    `test_a_plan_row_explains_itself…` on main.

    THE REAL FIX IS UPSTREAM OF THIS AND IS IN EVERY DRIVER: whatever grows or
    appears mid-run is stubbed, so the layout is settled before the first click
    rather than settling around it. This is the backstop for what that cannot
    reach — the pane's own first layout pass — and it is deliberately not written
    against any particular mover. Waiting for the caps line specifically was the
    first cut and it was wrong twice over: it went the moment `display` flipped,
    which is a style flag and not a completed layout, and where no caps line was
    ever coming it spent its whole bound learning that.

    So: the same coordinate `Pilot.click` will compute, held still across two
    consecutive reads with a real row under it. Two rather than one because a
    single read cannot tell a settled pane from one between passes. Bounded at
    `_SETTLE_READS` reads and it RAISES when that runs out, and it costs 0.1s when
    nothing is moving, which is the normal case.

    It used to click anyway, on the argument that a table which never drew should
    fail on the assertion that names it rather than time out in here. That argument
    is wrong in the one direction that matters, and #644 is what it cost: the
    assertion that names it is downstream and is about something else, so a pane
    that never settled reported itself as a hammer that did not start a fix. Four
    failures were investigated across two agents and two worktrees before anyone
    read the `never settled` line in the captured output. A helper that cannot do
    its job must say so in its own words — which is what `_click_row_index` twenty
    lines up already does when the row it wants is off screen.
    """
    # A MOUSE MOVE FIRST, because the very first Click into a freshly mounted pane
    # is swallowed: no dispatch fires for it, and the same click a moment later
    # works. It cost the first assertion of `_drive_seats` on every run after the
    # panels merged — the driver clicks four times and only the first was lost,
    # which is the signature of a first-event problem rather than a layout one.
    # Harmless where it is not needed: a hover is what a real hand does on its way
    # to a click, and `on_click` still reads the cell off the CLICK rather than off
    # whatever the hover left behind.
    await pilot.hover(table, offset=offset)
    previous, still = None, 0
    for _ in range(_SETTLE_READS):
        region = table.region
        x, y = region.offset.x + offset[0], region.offset.y + offset[1]
        under = table.screen.get_style_at(x, y).meta.get("row", -1)
        # THE ROW, not merely A row, when the caller named one. `get_style_at`
        # reads the compositor, and a scroll that has not repainted yet answers
        # with the layout as it was — so a scan for "where is row 12" found a `y`
        # from the old positions and the click landed on whatever the new ones put
        # there. Twice in a row with the RIGHT row under the pointer is the only
        # reading that says the paint has caught up with the scroll.
        on_a_row = under >= 0 and (row is None or under == row)
        still = still + 1 if on_a_row and region == previous else 0
        if still >= 2:
            break
        previous = region
        await pilot.pause(0.05)
    else:
        # RAISES, naming what actually happened. The old branch printed this and
        # clicked regardless, which made a pane that never settled indistinguishable
        # from one that did — the click landed on the header, `ClickTable.on_click`
        # refused it as `row: -1`, and the failure surfaced hundreds of lines later
        # as whatever verb the test was checking. That is #644.
        #
        # The case the old branch was protecting is real and has not gone away: a
        # fixed `y=2` offset addresses no row on a repo with one open PR, so the
        # driver would find nothing under the pointer through no fault of the
        # dashboard. But that case belongs to the four `@needs_live_data` tests,
        # whose data is whatever the fleet has open this minute, and those are now
        # opt-in for the same issue. What is left here runs on literals, so a pane
        # that will not settle is a defect or a wedged event loop, and both want a
        # name rather than a click.
        under = table.screen.get_style_at(
            table.region.offset.x + offset[0],
            table.region.offset.y + offset[1]).meta.get("row", -1)
        raise AssertionError(
            f"{table.id} never settled with a row at {offset} after "
            f"{_SETTLE_READS} reads — the pane was still moving, or nothing is "
            f"drawn there. Row under the pointer: {under} (wanted "
            f"{'any' if row is None else row}); region {table.region}, "
            f"{table.row_count} rows. This is not a failure of whatever the test "
            f"went on to assert. If the box is heavily loaded, raise "
            f"QB_DASH_SETTLE_READS")
    await pilot.click(table, offset=offset)


@needs_live_data
def test_a_single_click_acts_on_the_row_under_the_pointer():
    assert asyncio.run(_drive()) == []


@needs_live_data
def test_the_scales_icon_reviews_and_the_rest_of_the_row_opens():
    """One row, two verbs, told apart by which column was clicked.

    Also covers the confirmation: a panel review costs money and comments on a
    public PR, so the click must not start one on its own.
    """
    assert asyncio.run(_drive_panel()) == []


@needs_live_data
def test_a_plan_row_explains_itself_and_its_hammer_takes_the_issue():
    """The plan panel's two verbs.

    A plan item's note is the reasoning behind its place in the order — it is on
    the board and nowhere else, so a click has to be able to reach it. And the ⚒
    is the shortest path from "what is next" to somebody doing it, which is the
    whole reason the plan is on this screen at all.
    """
    assert asyncio.run(_drive_plan()) == []


@needs_live_data
def test_the_hammer_starts_a_fix_and_the_rest_of_the_issue_row_opens():
    """The issue panel's two verbs, told apart the same way the PR panel's are.

    The panel exists so a free issue can be picked up in one click, so what it
    launches has to be `/fix-issue <n>` for the issue actually under the pointer
    — a review of the wrong PR wastes money, and a fix on the wrong issue writes
    code nobody asked for.
    """
    assert asyncio.run(_drive_issues()) == []


#: How long `_let_the_workers_finish` will wait for the in-flight fetches to drain.
#:
#: A DEADLINE and not a read count, unlike `_SETTLE_READS` above, because what is
#: being waited for is different in kind: that one waits for a pane to settle, which
#: takes as many chances as a loaded box needs to give it, and this one waits for
#: network calls whose own timeouts are the thing that bounds them. `fetch_prs`
#: shells out to `gh` and the board calls go over http; 90s is several times the
#: worst honest answer and short enough that a wedged worker fails a test rather
#: than hanging a CI job with no signal at all — which is the one outcome worse than
#: a red build.
_WORKER_DRAIN_DEFAULT = 90.0


def _worker_drain() -> float:
    """`QB_DASH_WORKER_DRAIN` as a number of seconds that can actually expire.

    **`nan` IS THE REASON THIS FUNCTION EXISTS.** `float("nan")` is a perfectly good
    float, and `time.monotonic() >= start + nan` is False forever — so a deadline
    built from it never expires, and the bound this knob was added to guarantee is
    gone. Typing a three-letter word into an environment variable restored the
    unbounded wait it replaced. `inf` does the same thing by a shorter route, and 0
    or a negative expires before the first poll, which is not a bound either but at
    least fails loudly.

    That is the `QB_DASH_LIVE=0` lesson one constant up, in a different costume: a
    knob that silently means something other than what it says. So an unusable value
    is refused rather than honoured, and the refusal is `warnings.warn` rather than
    `print` — pytest captures stdout and shows it only on failure, which is exactly
    the run where this would not surface, while it lists warnings on a green one.

    A value that is not a number at all is a typo and gets an error instead of a
    default: silently running a suite for 90s when somebody asked for `9O` teaches
    them nothing. Raised HERE and not at import, so it arrives as a readable failure
    in the test that wanted it rather than as a collection error over the file.

    Read at call time and not once at import, so `monkeypatch.setenv` in a test
    reaches it — a module-level read is fixed before any test can set anything.
    """
    raw = os.environ.get("QB_DASH_WORKER_DRAIN", "").strip()
    if not raw:
        return _WORKER_DRAIN_DEFAULT
    try:
        seconds = float(raw)
    except ValueError:
        raise AssertionError(
            f"QB_DASH_WORKER_DRAIN={raw!r} is not a number of seconds") from None
    if not math.isfinite(seconds) or seconds <= 0:
        warnings.warn(
            f"QB_DASH_WORKER_DRAIN={raw!r} is not a positive, finite number of "
            f"seconds — a deadline built from it would never expire, which is the "
            f"unbounded wait this setting exists to prevent. Using "
            f"{_WORKER_DRAIN_DEFAULT}s.", stacklevel=2)
        return _WORKER_DRAIN_DEFAULT
    return seconds


async def _let_the_workers_finish(app) -> None:
    """Freezing the refreshers is half of it — this is the other half (#678).

    Stubbing `app.refresh_board` does nothing to the worker that is already
    running it, and three of the four make a SECOND call after the one these
    drivers wait for: `refresh_board` fetches the claims and then the questions a
    person owes, `refresh_plan` the plan and then the dials, `refresh_prs` the PR
    list and then the queue. So the wait each driver does above ends one board
    round trip before the pane stops changing, and the late arrival lands wherever
    it lands — after `_settle_table` has pronounced the table still, or after
    `_click_row_index` has read the screen position of the row it wants.

    `_settle_table` cannot cover it. It watches row KEYS, and a question arriving
    on a row this table already draws rewrites that row's state, its verb and its
    last cell without changing a single key; the table is cleared and rebuilt
    either way, and a `clear()` used to take the scroll with it.

    MEASURED, not reasoned about — and the measurement is of a board on a day, not
    of this code, so nobody re-running it later will reproduce the numbers. On
    2026-09-01, against `prisonblues/quarterback` as the fleet had it that evening,
    unmodified main failed two of thirty runs of the four live drivers, reading
    `work never settled with a row at (3, 22)`; before #679 the same race clicked
    anyway and reported `the icon did not raise the confirmation`, which is what
    #678 was filed about. With the blockers call closed and the PR list still in
    flight it came back in a new costume: a ⚒ click that raised the PANEL
    confirmation, for a PR that had appeared on the board a moment earlier. What
    generalises is the shape — a rebuild between reading a row's position and
    clicking it — not the rate.

    BOUNDED, because `WorkerManager.wait_for_complete` is not: it gathers every
    worker's `wait()` and has no deadline of its own, so one wedged fetch would hang
    all four of these drivers for as long as the runner allows — and a job killed at
    its own timeout reports nothing at all about what it was doing, which is the one
    outcome worse than a red build. The refusal names the workers still running,
    because that is the only fact that makes a drain which did not finish
    actionable.

    POLLED RATHER THAN `asyncio.wait_for(app.workers.wait_for_complete(), …)`, and
    the reason is in textual 8.2.8's `Worker.wait`: it awaits the worker's task
    inside `except asyncio.CancelledError`, and that handler sets
    `self.state = WorkerState.CANCELLED` on the WORKER. So cancelling the waiter —
    which is all `wait_for` does on timeout — marks a worker that is still happily
    running as cancelled, and then raises `WorkerCancelled` out of the gather before
    `wait_for` can raise `TimeoutError`. Tried first, and it fails with
    `textual.worker.WorkerCancelled` rather than with the message above. Waiting
    without cancelling anything cannot corrupt the state it is reading.
    """
    # IMPORTED HERE, not at module scope, for the reason `_click_row_index` imports
    # `Coordinate` here: this module must SKIP without textual rather than fail to
    # collect, and the CI job that proves the rest of the harness needs no dashboard
    # extras is the one a top-level import turns red.
    from textual.worker import WorkerState

    unfinished = (WorkerState.PENDING, WorkerState.RUNNING)
    drain = _worker_drain()
    deadline = time.monotonic() + drain
    while True:
        still = [w for w in app.workers if w.state in unfinished]
        if not still:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"the dashboard's fetches did not drain within {drain}s — "
                f"still going: "
                + ", ".join(sorted(f"{w.group or w.name}={w.state.name}"
                                   for w in still))
                + ". Nothing below this line has been clicked, so this is not a "
                  "failure of whatever the test went on to assert. Raise "
                  "QB_DASH_WORKER_DRAIN if the board is genuinely that slow today")
        await asyncio.sleep(0.05)


def _no_confirm(app, what: str) -> str:
    """Say there was no confirmation, and say the only thing that explains why.

    Every refusal on this path — a machine that has not opted in, an issue the
    board says is claimed, a repo this dashboard only watches, a click that landed
    on a row whose icon is grey — ends in `say`, and `say` is the pane's one
    visible answer to "did that click do anything". The bare sentence is the same
    for all of them, and it was the whole of what #678 had to go on.
    """
    return (f"{what} did not raise the confirmation — the pane says "
            f"{app.detail_text!r}, screen {type(app.screen).__name__}")


async def _drive() -> list[str]:
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600)   # no refresh mid-test
    # The usage line is a live call to Anthropic and it appears as a ROW when
    # its first answer lands — which reflows everything under it, mid-click if
    # the click is already in flight. Off for every test here: none of them is
    # about the caps, and a test that reached the network would be its own bug.
    app.refresh_limits = lambda: None
    # THE THINGS THAT MOVE THIS PANE WITHOUT BEING ASKED TO, all off.
    # (`#detail` is a third and is left alone: the drivers move that one
    # themselves, by clicking, and several of them assert on what it says.)
    # The caps line APPEARS — `display: none` until the first answer — and SEATS
    # GROWS, being the one table sized to its content (`height: auto`), so tmux
    # answering adds a row per pane. Either reflows every panel below it, mid-click
    # if a click is already in flight. `refresh_limits` was already off for exactly
    # this reason; #426 gave the caps line a SECOND source — the review queue rides
    # the gh clock and `render_queue` calls `render_limits` — so that guard stopped
    # covering it. None of these tests is about the caps, the queue or the seats.
    app.refresh_seats = lambda: None
    app.render_queue = lambda *a, **k: None
    # …and DIALS, which is a fourth (#477). It is `height: auto` like SEATS, so it
    # grows from nothing to two rows the moment the board answers — and it rides
    # `refresh_plan`, which these drivers deliberately leave running because they
    # need the plan table live. Every panel below it reflows on that growth, which
    # is a click landing a row high on whatever was in flight at the time.
    app.render_dials = lambda *a, **k: None

    opened: list[int] = []
    jumped: list[str | None] = []
    app.open_pr = lambda pr: (opened.append(pr.get("number")),
                              app.say(f"opened #{pr.get('number')}"))[1]
    # THE STUB HAS TO KEEP THE REAL METHOD'S SIGNATURE. It did not when
    # `jump_to_seat` grew a scope parameter (#208), and every run of this test then
    # died on a TypeError from inside the lambda rather than on anything it
    # asserts. It takes a session id now (#540) and takes only that.
    app.jump_to_agent = lambda session: (jumped.append(session), True)[1]
    # WITH THE BACKLOG SHOWING. `render_queue` is stubbed above, so the only PR
    # rows this table can have are the ones review has finished with — and a PR
    # row is what this driver clicks (#589).
    app.backlog = True

    failures: list[str] = []
    async with app.run_test(size=(80, 44)) as pilot:
        for _ in range(40):                        # the first fetch is a network call
            await pilot.pause(0.25)
            if app.query_one("#work").row_count and app.query_one("#agents").row_count:
                break

        # FROZEN NOW THAT THE DATA IS IN. The workers are left running above
        # because these drivers want real rows; they cannot stay running below,
        # because WORK rebuilds from all three of them and a rebuild between
        # reading a row's number and clicking that row's index hands the click a
        # different issue. That was one table per source before #589, so a driver
        # could leave the two it did not care about alone.
        for name in ("refresh_board", "refresh_plan", "refresh_prs",
                     "refresh_issues"):
            setattr(app, name, lambda: None)
        await _let_the_workers_finish(app)
        work = app.query_one("#work")
        agents = app.query_one("#agents")
        await _settle_table(pilot, work)
        _need_rows(work, "work", app.pr_err)

        # ONE click, on a row that is not the cursor's, is the whole point.
        # x=30 is the title column: the first columns are the state glyph and the
        # verb, which mean something else and are covered by the test below.
        row = _find_row(app, work, "pr:")
        if row < 0:
            pytest.skip("no PR row on the board today — nothing to open")
        key = str(list(work.rows)[row].value)
        await _click_row_index(pilot, work, key, x=30, scroll=True)
        await pilot.pause(0.2)
        if not opened:
            failures.append("a click on a PR row did not open it")

        await _click_row(pilot, agents, (4, 1))
        await pilot.pause(0.2)
        if not app.detail_text or app.detail_text.startswith("click a row"):
            failures.append("a click on an agent row changed nothing")

        opened.clear()                             # and the keyboard path still works
        work.focus()
        work.move_cursor(row=_find_row(app, work, "pr:"))
        await pilot.press("o")
        await pilot.pause(0.2)
        if not opened:
            failures.append("'o' did not open the selected PR")

    return failures


async def _drive_issues() -> list[str]:
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600)
    app.refresh_limits = lambda: None
    # The caps line and SEATS both move everything under them — see _drive.
    app.refresh_seats = lambda: None
    app.render_queue = lambda *a, **k: None
    # …and DIALS, which is a fourth (#477). It is `height: auto` like SEATS, so it
    # grows from nothing to two rows the moment the board answers — and it rides
    # `refresh_plan`, which these drivers deliberately leave running because they
    # need the plan table live. Every panel below it reflows on that growth, which
    # is a click landing a row high on whatever was in flight at the time.
    app.render_dials = lambda *a, **k: None

    started: list[tuple[str, list]] = []
    opened: list[int] = []
    # The ⚒ goes through `qb-start` now (#371), and the machine this runs on has
    # almost certainly not opted in — so the gate is answered here rather than
    # asked. What this drive is about is which column was clicked, not which
    # machine it was clicked on; the gate has its own tests below.
    app.spawn_refusal = lambda command: None
    app.run_spawn = lambda name, argv: started.append((name, argv))
    app.run_in_window = lambda name, command: started.append((name, [command]))
    app.open_issue = lambda issue: opened.append(issue.get("number"))
    # An issue nobody has planned is a backlog row since #589, and an issue row is
    # what this driver is about.
    app.backlog = True

    failures: list[str] = []
    async with app.run_test(size=(90, 50)) as pilot:
        for _ in range(40):
            await pilot.pause(0.25)
            if (app.held is not None and app.issues is not None
                    and app.query_one("#work").row_count):
                break
        # FROZEN NOW THAT THE DATA IS IN. The workers are left running above
        # because these drivers want real rows; they cannot stay running below,
        # because WORK rebuilds from all three of them and a rebuild between
        # reading a row's number and clicking that row's index hands the click a
        # different issue. That was one table per source before #589, so a driver
        # could leave the two it did not care about alone.
        for name in ("refresh_board", "refresh_plan", "refresh_prs",
                     "refresh_issues"):
            setattr(app, name, lambda: None)
        await _let_the_workers_finish(app)
        issues = app.query_one("#work")
        await _settle_table(pilot, issues)
        # AN ISSUE ROW, found by kind rather than taken from the top: WORK draws
        # the review queue first and then the plan, so row 0 is whatever the fleet
        # has in flight today (#589). The plan's own rows are mostly issue-backed
        # too, but the ⚒ on one goes through `fix_plan_item`; this driver is about
        # the issue row's own verb.
        # AND ONE WHOSE ⚒ IS ACTUALLY LIVE, which is asked of `work_action` rather
        # than rebuilt here (#678). This driver had the weakest guard of the four —
        # it excluded only rows a person owes an answer about (#328/#522), and the
        # ⚒ is grey on two further kinds of issue row this board draws every day:
        # one already CLAIMED by an agent, and one belonging to a repo this
        # dashboard only watches. Clicking either is correct behaviour producing no
        # confirmation, which is indistinguishable in the failure list from the
        # dashboard being broken. Its three siblings each skip when the board holds
        # nothing they can act on; this now does the same.
        #
        # `work_action` and not a second opinion about what makes a ⚒ live: it is
        # the call the renderer makes when it decides whether to draw the icon in
        # cyan and the call `dispatch_row` makes when it decides what a click does,
        # so a third answer here is the one that can drift from both.
        wanted = [str(rk.value) for rk in issues.rows
                  if str(rk.value).startswith("issue:")
                  and (record := app.rows.get(str(rk.value)))
                  and not record.get("blocked")
                  and app.work_action(record)[1] == "fix"]
        # #433 GAVE AN EMPTY TABLE A THIRD MEANING and `_need_rows` knows two:
        # nothing open, or `gh` refusing. "The board has not answered, so nothing
        # has been painted yet" is neither, and it reaches `_need_rows` as an
        # empty table with no `gh` error — i.e. as `skip("no open issues")`,
        # which would report a board that was slow as a repo that was quiet and
        # silently skip the test this change is named for.
        if app.held is None or app.issues is None:
            raise AssertionError(
                f"the wait ran out with {'the board' if app.held is None else 'gh'} "
                f"still unanswered, so ISSUES never painted — this is not "
                f"'nothing to click'")
        _need_rows(issues, "issues", app.issue_err)
        # Read the number off the RENDERED first row rather than re-deriving the
        # order here: what the click has to match is the row a human sees. Found
        # by its "#" rather than by column index — the panels grew a repo column
        # between the issue number and the icons, and a hardcoded index made this
        # fail with `int('quarterback')` rather than saying what moved.
        if not wanted:
            pytest.skip("no issue row offers a live ⚒ on the board today — every "
                        "open issue is on the plan, claimed, or in a watched repo")

        # The ⚒ column asks first, the same as the ⚖ does. `top` comes off the row
        # the click actually landed on rather than off the index read above: a
        # worker already in flight when the freeze went in can still rebuild the
        # table once, and then the index names a different issue.
        key = await _click_row_index(pilot, issues, wanted[0], scroll=True,
                                     column=app_module.Dash.VERB_COLUMN)
        top = app.rows[key]["issue"]["number"]
        # THE STATE THIS DRIVE CLAIMS TO BE IN, asked again after the click and not
        # only before it. The guard above reads the table to choose a row; if that
        # row went grey in between — an agent claimed the issue, a question was
        # raised on it — then "no confirmation" is the dashboard being right, and a
        # failure list that cannot tell that from a broken ⚒ is what left #678
        # undiagnosed.
        if app.work_action(app.rows[key])[1] != "fix":
            pytest.skip(f"{key} stopped offering a live ⚒ between the read and the "
                        f"click — the board moved under the test, not the dashboard")
        await pilot.pause(0.3)
        if started:
            failures.append("the icon started a fix with no confirmation")
        if not isinstance(app.screen, app_module.Confirm):
            failures.append(_no_confirm(app, "the icon"))
        else:
            await pilot.press("enter")
            await pilot.pause(0.3)
            if not started:
                failures.append(f"confirming did not start the fix — the pane "
                                f"says {app.detail_text!r}")
            elif ["/fix-issue", str(top)] != list(started[0][1][1:3]):
                failures.append(f"wrong command launched: {started[0][1]}")
            elif started[0][0] != f"fix-issue-{top}":
                failures.append(f"wrong window name: {started[0][0]}")

        # A click anywhere else on the row still means "open it on GitHub".
        started.clear()
        await _click_row_index(pilot, issues, wanted[0], x=30, scroll=True)
        await pilot.pause(0.3)
        if opened != [top]:
            failures.append(f"clicking the title opened {opened}, expected [{top}]")
        if started:
            failures.append("clicking the title started a fix")

        # And the keyboard route to the same verb.
        issues.focus()
        issues.move_cursor(row=_find_row(app, issues, "issue:"), animate=False)
        await pilot.press("f")
        await pilot.pause(0.3)
        if not isinstance(app.screen, app_module.Confirm):
            failures.append(_no_confirm(app, "'f'"))
        else:
            await pilot.press("escape")
            await pilot.pause(0.2)

    return failures


async def _drive_plan() -> list[str]:
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600)
    app.refresh_limits = lambda: None
    # The caps line and SEATS both move everything under them — see _drive.
    app.refresh_seats = lambda: None
    app.render_queue = lambda *a, **k: None
    # …and DIALS, which is a fourth (#477). It is `height: auto` like SEATS, so it
    # grows from nothing to two rows the moment the board answers — and it rides
    # `refresh_plan`, which these drivers deliberately leave running because they
    # need the plan table live. Every panel below it reflows on that growth, which
    # is a click landing a row high on whatever was in flight at the time.
    app.render_dials = lambda *a, **k: None

    started: list[tuple[str, list]] = []
    app.spawn_refusal = lambda command: None
    app.run_spawn = lambda name, argv: started.append((name, argv))
    app.run_in_window = lambda name, command: started.append((name, [command]))

    failures: list[str] = []
    async with app.run_test(size=(100, 50)) as pilot:
        for _ in range(40):
            await pilot.pause(0.25)
            if app.query_one("#work").row_count:
                break
        plan = app.query_one("#work")
        # FROZEN NOW THAT THE DATA IS IN, and settled before anything is read off
        # it. The workers are left running above because this driver wants a real
        # plan; they cannot stay running below, because WORK is rebuilt by all of
        # them (#589) and a rebuild between resolving a row key to an index and
        # clicking that index hands the click a different piece of work — which is
        # exactly what it did, clicking #589 while the assertion read the row it
        # had asked for.
        for name in ("refresh_board", "refresh_plan", "refresh_prs",
                     "refresh_issues"):
            setattr(app, name, lambda: None)
        await _let_the_workers_finish(app)
        await _settle_table(pilot, plan)
        _need_rows(plan, "plan items", app.plan_err)

        # A PLAN ROW, found in the table rather than re-derived from the plan.
        # WORK draws the review queue and the questions a person owes ABOVE the
        # plan (#589/#328), so the plan's own index stopped being the table's the
        # day this stopped being the PLANS panel. Free and issue-backed, because
        # the ⚒ below is what it is here to press — and not one a person owes an
        # answer about, where that icon is grey by design (#522) and has its own
        # test.
        import qbdata as qd
        wanted = next(
            (str(rk.value) for rk in plan.rows
             if str(rk.value).startswith("plan:")
             and (record := app.rows.get(str(rk.value)))
             and qd.plan_issue(record["item"]) and not record["item"].get("claim")
             and not record.get("blocked")), None)
        if wanted is None:
            pytest.skip("no free issue-backed item on the plan today — nothing to take")
        landed = app.rows[wanted]["item"]

        # Anywhere but the ⚒: the detail line, and it must name the row clicked.
        await _click_row_index(pilot, plan, wanted, x=40, scroll=True)
        await pilot.pause(0.3)
        # BY THE SCOPE, not by a fixed index: the repo cell comes and goes with
        # `scope.column` (#261). The `? ` mark on an unattributable row is part of
        # the cell but not of the title.
        title_at = 6 if app.scope.column else 5
        title = str(plan.get_row(wanted)[title_at]).removeprefix("? ").rstrip("…")
        if title and title not in app.detail_text:
            failures.append(f"the detail line does not describe the row clicked: "
                            f"{title!r} is not in {app.detail_text[:100]!r}")

        await _click_row_index(pilot, plan, wanted, scroll=True,
                               column=app_module.Dash.VERB_COLUMN)
        await pilot.pause(0.3)
        issue = qd.plan_issue(landed)
        if started:
            failures.append("the icon started a fix with no confirmation")
        elif not isinstance(app.screen, app_module.Confirm):
            failures.append(_no_confirm(app, "the icon"))
        else:
            await pilot.press("enter")
            await pilot.pause(0.3)
            if not started:
                failures.append(f"confirming did not start the fix — the pane "
                                f"says {app.detail_text!r}")
            elif ["/fix-issue", str(issue["number"])] != list(started[0][1][1:3]):
                failures.append(f"wrong command launched: {started[0][1]}")

    return failures


async def _drive_panel() -> list[str]:
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600)
    app.refresh_limits = lambda: None
    # THE THINGS THAT MOVE THIS PANE WITHOUT BEING ASKED TO, all off.
    # (`#detail` is a third and is left alone: the drivers move that one
    # themselves, by clicking, and several of them assert on what it says.)
    # The caps line APPEARS — `display: none` until the first answer — and SEATS
    # GROWS, being the one table sized to its content (`height: auto`), so tmux
    # answering adds a row per pane. Either reflows every panel below it, mid-click
    # if a click is already in flight. `refresh_limits` was already off for exactly
    # this reason; #426 gave the caps line a SECOND source — the review queue rides
    # the gh clock and `render_queue` calls `render_limits` — so that guard stopped
    # covering it. None of these tests is about the caps, the queue or the seats.
    app.refresh_seats = lambda: None
    app.render_queue = lambda *a, **k: None
    # …and DIALS, which is a fourth (#477). It is `height: auto` like SEATS, so it
    # grows from nothing to two rows the moment the board answers — and it rides
    # `refresh_plan`, which these drivers deliberately leave running because they
    # need the plan table live. Every panel below it reflows on that growth, which
    # is a click landing a row high on whatever was in flight at the time.
    app.render_dials = lambda *a, **k: None

    started: list[tuple[str, str]] = []
    windowed: list[tuple[str, str]] = []
    opened: list[int] = []
    # run_in_PANE, not run_in_window: a review now lands in the seat row, beside
    # the work it is about. Both are stubbed so that a review quietly reverting
    # to a window shows up here as a failure rather than as a passing test.
    app.run_in_pane = lambda name, command: started.append((name, command))
    app.run_in_window = lambda name, command: windowed.append((name, command))
    app.backlog = True                    # `render_queue` is stubbed: see _drive
    app.open_pr = lambda pr: opened.append(pr.get("number"))

    failures: list[str] = []
    async with app.run_test(size=(90, 44)) as pilot:
        for _ in range(40):
            await pilot.pause(0.25)
            if app.query_one("#work").row_count:
                break
        # FROZEN NOW THAT THE DATA IS IN. The workers are left running above
        # because these drivers want real rows; they cannot stay running below,
        # because WORK rebuilds from all three of them and a rebuild between
        # reading a row's number and clicking that row's index hands the click a
        # different issue. That was one table per source before #589, so a driver
        # could leave the two it did not care about alone.
        for name in ("refresh_board", "refresh_plan", "refresh_prs",
                     "refresh_issues"):
            setattr(app, name, lambda: None)
        await _let_the_workers_finish(app)
        prs = app.query_one("#work")
        await _settle_table(pilot, prs)
        _need_rows(prs, "work", app.pr_err)
        rows = [str(rk.value) for rk in prs.rows
                if str(rk.value).startswith("pr:")
                and not app.rows.get(str(rk.value), {}).get("blocked")]
        if not rows:
            pytest.skip("no reviewable PR row on the board today")

        # This test is about the ⚖ MECHANICS — confirm, cancel, pane not window
        # — on whatever PR happens to be newest. The cross-repo refusal has its
        # own test below; left live here it would make this one pass or fail on
        # which of QB_DASH_REPOS holds the highest-numbered PR today. So the guard
        # is STUBBED, like the launchers above it, rather than blinded: clearing
        # `repo_slug` used to stand it aside, and now it is the one state the guard
        # refuses on, since a dashboard that cannot name its own repo cannot aim a
        # review from it.
        app.wrong_repo = lambda repo, what: None

        # The ⚖ column asks first and starts nothing by itself.
        await _click_row_index(pilot, prs, rows[0], scroll=True,
                               column=app_module.Dash.VERB_COLUMN)
        await pilot.pause(0.3)
        if started:
            failures.append("the icon started a review with no confirmation")
        if not isinstance(app.screen, app_module.Confirm):
            failures.append(_no_confirm(app, "the icon"))
        else:
            await pilot.press("enter")               # …and confirming starts it
            await pilot.pause(0.3)
            if not started:
                failures.append(f"confirming did not start the review — the pane "
                                f"says {app.detail_text!r}")
            elif "/panel-review-pr" not in started[0][1]:
                failures.append(f"wrong command launched: {started[0][1]}")
            if windowed:
                failures.append("the review opened a window, not a seat-row pane")

        # Cancelling starts nothing — and the dialog has to have been THERE, or
        # this passes on a click that missed: a click that lands on nothing leaves
        # `started` empty and the escape a no-op, which reads exactly like a cancel
        # that worked.
        #
        # ON A ROW THAT EXISTS, which is the half the first cut of that assertion
        # left out. It clicked row 2 unconditionally, and a second row is not
        # something this test can arrange — the panel shows what the fleet has open,
        # so on a day with one PR in scope the click went past the last row, no
        # dialog could appear, and the suite went red about the fleet's state rather
        # than about this dashboard's behaviour. Row 2 when there is one, row 1
        # otherwise: cancelling is worth testing on any day, and the row it happens
        # on is not what is under test.
        started.clear()
        cancel_on = rows[1] if len(rows) >= 2 else rows[0]
        await _click_row_index(pilot, prs, cancel_on, scroll=True,
                               column=app_module.Dash.VERB_COLUMN)
        await pilot.pause(0.3)
        if not isinstance(app.screen, app_module.Confirm):
            failures.append(f"the ⚖ on row {cancel_on} raised no confirmation to cancel")
        else:
            await pilot.press("escape")
            await pilot.pause(0.3)
            if started:
                failures.append("cancelling still started a review")

        # A click anywhere else on the row still means "open on GitHub".
        await _click_row_index(pilot, prs, rows[0], x=30, scroll=True)
        await pilot.pause(0.3)
        if not opened:
            failures.append("clicking the title did not open the PR")
        if started:
            failures.append("clicking the title started a review")

        # And the keyboard route to the same verb.
        prs.focus()
        prs.move_cursor(row=_find_row(app, prs, "pr:"), animate=False)
        await pilot.press("p")
        await pilot.pause(0.3)
        if not isinstance(app.screen, app_module.Confirm):
            failures.append(_no_confirm(app, "'p'"))
        else:
            await pilot.press("escape")
            await pilot.pause(0.2)

    return failures


async def _drive_seats() -> list[str]:
    """The SEATS panel: jump, ✕, ＋ — with tmux replaced by a recorder.

    The seats come from a stub rather than from the real server for the obvious
    reason: a test that closed a pane would close one of the developer's own
    seats, and the agent working in it would go with it.
    """
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600)
    app.refresh_limits = lambda: None

    fake = [
        {"pane": "%7", "seat": "1", "session": "seats-demo", "window": "0",
         "command": "claude", "path": "/tmp/demo"},
        {"pane": "%8", "seat": "2", "session": "seats-demo", "window": "0",
         "command": "bash", "path": "/tmp/demo"},
    ]
    clicked: list[tuple[str, str]] = []
    jumped: list[str] = []
    app.run_seat_click = lambda tag, session: clicked.append((tag, session))
    app.jump_pane = lambda seat: jumped.append(seat["pane"])
    # The real reader is switched off, not just overwritten afterwards: it runs
    # on mount AND on a timer, so a fixture that only re-rendered would race it
    # and the panel under the pointer would be the developer's own seats.
    app.refresh_seats = lambda: None
    # AND EVERYTHING ELSE, which since #589 all draws into this one pane: the
    # fleet shares this very table, so a live agent arriving moves the ＋ down a
    # row; and the caps line appears the moment the queue answers, which moves the
    # whole table down one. Both land a click aimed at one row on the row above
    # it. Same class of race as the DIALS one below, and neither is hypothetical.
    for name in ("refresh_board", "refresh_plan", "refresh_prs", "refresh_issues"):
        setattr(app, name, lambda: None)
    # And DIALS, which sits DIRECTLY ABOVE this panel and is `height: auto` (#477).
    # It grows from nothing to two rows when the board answers, on `refresh_plan`'s
    # clock, and every row of SEATS moves down with it — mid-click, since this
    # driver clicks three of them. Not hypothetical: it is what failed
    # `test_the_seats_panel_jumps_closes_and_adds` on the run that added this line.
    app.render_dials = lambda *a, **k: None

    failures: list[str] = []
    async with app.run_test(size=(90, 50)) as pilot:
        app.render_seats(fake)
        await pilot.pause(0.2)
        seats = app.query_one("#agents")

        # Two seats plus the ＋ row. The ＋ has to be a row, not a key, or the
        # panel cannot be driven by the mouse alone.
        if seats.row_count != len(fake) + 1:
            failures.append(f"expected {len(fake) + 1} rows, got {seats.row_count}")

        # The ✕ column asks first and closes nothing by itself.
        # THROUGH `_click_row`, which the ＋ and the jump below also now need:
        # SEATS was `height: auto` at the top of the pane and settled instantly,
        # and AGENTS is an `fr` share that settles after everything above it has
        # (#589). A click aimed while the table is still moving lands a row high.
        await _click_row(pilot, seats, (app_module.Dash.VERB_COLUMN + 2, 1))
        await pilot.pause(0.3)
        if clicked:
            failures.append("the ✕ closed a seat with no confirmation")
        if not isinstance(app.screen, app_module.Confirm):
            failures.append("the ✕ did not raise the confirmation")
        else:
            await pilot.press("enter")
            await pilot.pause(0.3)
            if clicked != [("kill1", "seats-demo")]:
                failures.append(f"wrong close dispatched: {clicked}")

        # Cancelling closes nothing.
        clicked.clear()
        await _click_row(pilot, seats, (app_module.Dash.VERB_COLUMN + 2, 2))
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.3)
        if clicked:
            failures.append("cancelling still closed a seat")

        # The ＋ row adds one, to the session the SEATS came from.
        await _click_row_index(pilot, seats, len(fake))
        await pilot.pause(0.3)
        if not isinstance(app.screen, app_module.Confirm):
            failures.append("the ＋ did not raise the confirmation")
        else:
            await pilot.press("enter")
            await pilot.pause(0.3)
            if clicked != [("add", "seats-demo")]:
                failures.append(f"the ＋ dispatched {clicked}")

        # Anywhere else on a seat row is still "take me to that pane".
        clicked.clear()
        await _click_row(pilot, seats, (20, 1))
        await pilot.pause(0.3)
        if jumped != ["%7"]:
            failures.append(f"clicking a seat row jumped to {jumped}")
        if clicked:
            failures.append("clicking a seat row closed something")

    return failures


def test_the_seats_panel_jumps_closes_and_adds():
    """The three verbs the tmux seat bar has, in the panel, meaning the same.

    Closing is the one that matters: it kills a pane with a working agent in it,
    so a stray click on a 78-column panel must not be able to do it, and the
    thing it dispatches has to name the seat actually under the pointer.
    """
    assert asyncio.run(_drive_seats()) == []


def test_the_add_row_is_still_clickable_on_a_screen_that_is_full():
    """The ＋ survives the CAP, not just the panel that made the cap necessary.

    `#seats` stopped sharing the pane in `fr` because a seventh panel took the ＋
    off the bottom — but a `max-height` small enough to scroll it out of view
    reintroduces exactly that, only at a seat count instead of a panel count. The
    cap has to be quoted from the same place as the ceiling `qb-seats` enforces:
    MAX_SEATS=10, plus the header, plus this row.

    Driven by clicking rather than by measuring, because "on screen" is not the
    claim — the claim is that the mouse can still reach it, and a row scrolled
    below the fold answers a click at that offset with a different row.
    """
    async def drive() -> list:
        app_module = _load_app()
        app = app_module.Dash(interval=3600, gh_interval=3600)
        # EVERY WORKER OFF, not just the seats'. The convention `_click_row`'s
        # docstring states — stub whatever grows or appears mid-run, so the layout
        # is settled before the first click rather than settling around it — and
        # the merge gave it two more things to reach: the fleet shares this table
        # (#589), so a live agent arriving moves the ＋ down a row; and the caps
        # line appears the moment the queue answers, which moves the whole table
        # down one and lands a click aimed at the ＋ on the last seat instead.
        for name in ("refresh_limits", "refresh_seats", "refresh_board",
                     "refresh_plan", "refresh_prs", "refresh_issues"):
            setattr(app, name, lambda: None)
        app.render_dials = lambda *a, **k: None
        clicked: list = []
        app.run_seat_click = lambda tag, session: clicked.append((tag, session))
        app.jump_pane = lambda seat: clicked.append(("jump", seat["pane"]))
        # MAX_SEATS in qb-seats — the most a screen can hold, so the most this
        # table can ever be asked to draw.
        full = [{"pane": f"%{n}", "seat": str(n), "session": "seats-demo",
                 "window": "0", "command": "claude", "path": "/tmp/demo"}
                for n in range(1, 11)]
        async with app.run_test(size=(90, 50)) as pilot:
            app.render_seats(full)
            await pilot.pause(0.2)
            seats = app.query_one("#agents")
            assert seats.row_count == len(full) + 1, seats.row_count
            await _click_row_index(pilot, seats, len(full))
            await pilot.pause(0.3)
            if isinstance(app.screen, app_module.Confirm):
                await pilot.press("enter")
                await pilot.pause(0.3)
            return clicked

    assert asyncio.run(drive()) == [("add", "seats-demo")], \
        "the ＋ row was not what a click at its offset reached"


# ---- one column or two ------------------------------------------------------
#
# The complaint the wide layout answers is HEIGHT: seven panels sharing one
# column's rows leave CLAIMED and REVIEW QUEUE two rows tall on a pane nobody
# would call short. So these measure heights and positions rather than reading
# the class back off the widget — a class that is set while the grid does
# nothing is exactly the bug worth catching, and it would pass an assertion
# about the class.


def _panels(app) -> dict:
    """Every panel's region, by id. Position and size, which is the whole claim."""
    return {pid: app.query_one("#" + pid).region
            for pid in ("p_dials", "p_agents", "p_work")}


def _stub_fetches(app) -> None:
    """Nothing is fetched because nothing here depends on the rows: an empty
    table still occupies its share of the pane, and a layout test that waited on
    `gh` would be a layout test that skips in CI."""
    for stub in ("refresh_limits", "refresh_seats", "refresh_board",
                 "refresh_plan", "refresh_prs", "refresh_issues"):
        setattr(app, stub, lambda: None)


async def _laid_out(width: int, height: int = 50) -> dict:
    """Drive the app at one size with every fetch stubbed out, and measure."""
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600)
    _stub_fetches(app)
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.pause(0.2)
        return {"wide": app.wide, "panels": _panels(app)}


def test_a_narrow_pane_is_one_column_and_a_wide_one_is_two():
    """The threshold is a width, and below it nothing changes at all.

    78 columns is what one of these tables wants before it wraps, so the pane
    `qb-seats` splits off must come out EXACTLY as it did before this existed —
    a dash that went two-across at 78 would be two columns of 39, which is worse
    than the problem being solved.
    """
    narrow = asyncio.run(_laid_out(90))
    assert narrow["wide"] is False
    assert {r.x for r in narrow["panels"].values()} == {0}, \
        "a 90-column pane put panels in more than one column"

    wide = asyncio.run(_laid_out(200))
    assert wide["wide"] is True
    lefts = {r.x for r in wide["panels"].values()}
    assert len(lefts) == 2, f"a 200-column pane did not use two columns: {lefts}"


def test_two_columns_is_bought_for_height_not_for_width():
    """The point of the second column, asserted as the thing it is for.

    Both tables have to come out TALLER wide than narrow. If they do not, the grid
    is drawing two columns and the rows are still being divided, which looks like
    a success and fixes nothing. DIALS is exempt because it is its content in both
    layouts, which is also why it spans: a column of its own would buy it nothing
    and cost the table beside it half its width.
    """
    narrow = asyncio.run(_laid_out(90))["panels"]
    wide = asyncio.run(_laid_out(200))["panels"]
    shorter = {pid: (narrow[pid].height, wide[pid].height)
               for pid in narrow if pid != "p_dials"
               and wide[pid].height <= narrow[pid].height}
    assert not shorter, f"no taller in two columns (narrow, wide): {shorter}"
    assert wide["p_dials"].width > narrow["p_dials"].width, \
        "DIALS did not span both columns"


def test_the_two_tables_sit_side_by_side_when_there_is_room():
    """What the second column is FOR, now that there is no pairing to arrange.

    Seven panels over a two-column grid had to be re-paired by hand — a grid fills
    row by row in DOM order — and #273's "the queue sits directly under the PRs"
    was one of the arrangements that took. With AGENTS and WORK there is one row
    of tables and DOM order is already right, so `relayout` is the class and
    nothing else (#589); this is what it has to produce.
    """
    narrow = asyncio.run(_laid_out(90))["panels"]
    assert narrow["p_work"].y == narrow["p_agents"].y + narrow["p_agents"].height, \
        "narrow: WORK is not directly under AGENTS"

    wide = asyncio.run(_laid_out(200))["panels"]
    assert wide["p_work"].y == wide["p_agents"].y, "wide: WORK is not in AGENTS' row"
    assert wide["p_work"].x > wide["p_agents"].x, "wide: WORK is not beside AGENTS"
    assert wide["p_dials"].y < wide["p_agents"].y, "DIALS is not above both"


def test_crossing_the_threshold_and_coming_back_restores_the_narrow_order():
    """A resize is not a one-way door, and the reorder has to undo exactly.

    `>` and `<` nudge the pane by 8 columns at a time, so a dash crossing the
    threshold twice in ten seconds is an ordinary afternoon rather than an edge
    case — and `move_child` mutates the tree, so an undo that is not exact
    leaves a screen whose panels are in an order nobody chose. Measured by the
    positions, which is where a wrong order shows up.
    """
    async def drive() -> tuple:
        app_module = _load_app()
        app = app_module.Dash(interval=3600, gh_interval=3600)
        _stub_fetches(app)
        async with app.run_test(size=(90, 50)) as pilot:
            await pilot.pause(0.2)
            first = _panels(app)
            await pilot.resize_terminal(200, 50)
            await pilot.pause(0.2)
            wide = app.wide
            await pilot.resize_terminal(90, 50)
            await pilot.pause(0.2)
            return first, wide, app.wide, _panels(app)

    first, went_wide, came_back, again = asyncio.run(drive())
    assert went_wide is True and came_back is False
    assert again == first, "the narrow layout did not come back the way it went"


def test_the_add_row_is_still_clickable_in_two_columns():
    """The ＋ again, at the width that rearranges everything around it.

    The last two defects in this panel were both a click reaching the wrong row
    or no row, and both came from a layout change that read fine. A grid that
    spans SEATS across two columns and reorders the panels under it is the same
    class of change, so it is checked the same way: by clicking, not by
    measuring.
    """
    async def drive() -> list:
        app_module = _load_app()
        app = app_module.Dash(interval=3600, gh_interval=3600)
        # EVERY WORKER OFF, not just the seats'. The convention `_click_row`'s
        # docstring states — stub whatever grows or appears mid-run, so the layout
        # is settled before the first click rather than settling around it — and
        # the merge gave it two more things to reach: the fleet shares this table
        # (#589), so a live agent arriving moves the ＋ down a row; and the caps
        # line appears the moment the queue answers, which moves the whole table
        # down one and lands a click aimed at the ＋ on the last seat instead.
        for name in ("refresh_limits", "refresh_seats", "refresh_board",
                     "refresh_plan", "refresh_prs", "refresh_issues"):
            setattr(app, name, lambda: None)
        app.render_dials = lambda *a, **k: None
        clicked: list = []
        app.run_seat_click = lambda tag, session: clicked.append((tag, session))
        app.jump_pane = lambda seat: clicked.append(("jump", seat["pane"]))
        full = [{"pane": f"%{n}", "seat": str(n), "session": "seats-demo",
                 "window": "0", "command": "claude", "path": "/tmp/demo"}
                for n in range(1, 11)]
        async with app.run_test(size=(200, 50)) as pilot:
            assert app.wide is True, "200 columns did not reach the wide layout"
            app.render_seats(full)
            await pilot.pause(0.2)
            seats = app.query_one("#agents")
            await _click_row_index(pilot, seats, len(full))
            await pilot.pause(0.3)
            if isinstance(app.screen, app_module.Confirm):
                await pilot.press("enter")
                await pilot.pause(0.3)
            return clicked

    assert asyncio.run(drive()) == [("add", "seats-demo")], \
        "the ＋ row was not what a click at its offset reached in two columns"


# ---- the scope (#261) -------------------------------------------------------
#
# Board data as literals, so this runs wherever textual does: the question is not
# what the fleet is doing today, it is whether `s` narrows the rows AND drops the
# column AND leaves the action icons where a click expects them.

SCOPED_BOARD = {
    "agents": [
        {"holder": "daedalus/seat-quarterback-1", "repo": "quarterback",
         "title": "here", "branch": "main"},
        {"holder": "zeus/amber-otter", "repo": "prisonblues/nix-fleet",
         "title": "elsewhere", "branch": "main"},
        {"holder": "zeus/hazel-dune", "repo": None, "title": "nowhere", "branch": None},
    ],
    "claims": [
        {"holder": "daedalus/one", "kind": "issue", "key": "prisonblues/quarterback#261"},
        {"holder": "zeus/two", "kind": "issue", "key": "prisonblues/nix-fleet#3"},
    ],
}

SCOPED_PLAN = {
    "items": [
        {"item_id": "a", "repo": "prisonblues/quarterback", "title": "ours", "rank": 1,
         "rank_source": "ordered",
         "ref": {"kind": "issue", "value": "261"}, "blocked_by": [], "claim": None},
        {"item_id": "b", "repo": "prisonblues/nix-fleet", "title": "theirs", "rank": 1,
         "rank_source": "appended",
         "ref": None, "blocked_by": [], "claim": None},
    ],
    "counts": {"open": 2, "claimed": 0, "covered": 0, "blocked": 0, "stale": 0},
    "order_trust": {"trusted": False, "unchosen": 1},
    "next": {"item_id": "a", "repo": "prisonblues/quarterback",
             "ref": {"kind": "issue", "value": "261"}, "caveat": None},
    "truncated": False,
}


def _text(widget) -> str:
    """The plain text of a one-line Static, across textual versions.

    `renderable` on 0.x/1.x, `content` on 8.x — and this suite is run by whatever
    `uv --extra tui` resolves, so it pins the assertion rather than the version.
    """
    for attr in ("content", "renderable", "_renderable"):
        if (value := getattr(widget, attr, None)) is not None:
            return str(value)
    return str(widget.render())


def _titles(app) -> dict[str, str]:
    """The table headings, which are where a narrowed table admits it narrowed."""
    return {name: _text(app.query_one(f"#t_{name}")) for name in ("agents", "work")}


def _cells(table, row: int) -> list[str]:
    return [str(c) for c in table.get_row_at(row)]


async def _drive_scope() -> list[str]:
    app_module = _load_app()
    qd = app_module.qd
    app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600,
                          scope=qd.Scope([qd.REPO]))
    # Every fetch off: this test is about how the client READS an answer, and the
    # answer is a literal above. A live refresh landing mid-assert would repaint
    # the tables from whatever the fleet happens to be doing.
    app.refresh_limits = lambda: None
    app.refresh_seats = lambda: None
    app.refresh_board = lambda: None
    app.refresh_plan = lambda: None
    app.refresh_prs = lambda: None
    app.refresh_issues = lambda: None

    failures: list[str] = []

    def agent_rows(table):
        """Every row but the ＋, which is a control and not an agent."""
        return [_cells(table, i) for i in range(table.row_count - 1)]

    async with app.run_test(size=(80, 44)) as pilot:
        # on_mount sets cfg from the board when there is one; there need not be.
        app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid", agent="host")
        app.render_board(SCOPED_BOARD)
        app.render_plan(SCOPED_PLAN, None)
        await pilot.pause()

        agents, work = app.query_one("#agents"), app.query_one("#work")
        titles = _titles(app)

        # NARROW: this project's rows, the unattributable row kept, no repo cell.
        # Asserted on the `what` cell, which is the one the dropped column widened.
        # dot ✕ who state stage what ttl — `what` is index 5.
        shown = sorted(row[5] for row in agent_rows(agents))
        if shown != ["#261 ours", "? nowhere", "here"]:
            failures.append(f"narrow AGENTS holds {shown}, not this repo's row, the "
                            "one the board could not attribute — marked, because the "
                            "cell that used to say so is the cell this view drops — "
                            "and the claim nobody answers for")
        if len(agents.columns) != 7:
            failures.append(f"narrow AGENTS has {len(agents.columns)} columns, not 7")
        if "1 elsewhere" not in titles["agents"]:
            failures.append(f"narrow AGENTS does not say what it hid: {titles['agents']!r}")
        if "1 elsewhere" not in titles["work"]:
            failures.append(f"narrow WORK does not say what it hid: {titles['work']!r}")
        if "quarterback" not in _text(app.query_one("#head")):
            failures.append("the header does not name the scope it is showing")

        # The icon a click acts on must not have moved with the column that went.
        if work.row_count and _cells(work, 0)[app.VERB_COLUMN] != "⚒":
            failures.append(f"the ⚒ moved out of column {app.VERB_COLUMN}: "
                            f"{_cells(work, 0)}")

        await pilot.press("s")
        await pilot.pause()

        agents, work = app.query_one("#agents"), app.query_one("#work")
        titles = _titles(app)
        # Three agents and the two claims neither of them answers for.
        if len(agent_rows(agents)) != 5:
            failures.append(f"the wide view holds {len(agent_rows(agents))} rows, not 5")
        if len(agents.columns) != 8:
            failures.append(f"the wide view has {len(agents.columns)} columns, not 8")
        if "elsewhere" in titles["agents"] or "elsewhere" in titles["work"]:
            failures.append(f"the wide view still claims to hide rows: {titles}")
        # The claim keeps its repo where two watched repos could share a number.
        keys = [row[6] for row in agent_rows(agents)]
        if "quarterback#261 ours" not in keys:
            failures.append(f"the wide view's claim rows read {keys}")
        if "all repos" not in _text(app.query_one("#head")):
            failures.append("the header does not say the pane went wide")
        if any(cell.startswith("? ") for row in agent_rows(agents) for cell in row):
            failures.append(f"the wide view still marks an unattributed row: "
                            f"{agent_rows(agents)}")
        if work.row_count and _cells(work, 0)[app.VERB_COLUMN] != "⚒":
            failures.append(f"the ⚒ moved when the column came back: {_cells(work, 0)}")

        await pilot.press("s")                     # and back, from cache
        await pilot.pause()
        if len(agent_rows(app.query_one("#agents"))) != 3:
            failures.append("`s` did not narrow again from the cached answer")

    return failures
async def _drive_stage() -> list[str]:
    """FLEET's stage cells, with the two shapes that matter side by side."""
    app_module = _load_app()
    qd = app_module.qd
    app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600,
                          scope=qd.Scope([qd.REPO]))
    for name in ("refresh_limits", "refresh_seats", "refresh_board",
                 "refresh_plan", "refresh_prs", "refresh_issues"):
        setattr(app, name, lambda: None)

    board = {"agents": [
        {"holder": "zeus/one", "repo": "quarterback", "title": "third round",
         "branch": "main", "stage": "R2"},
        {"holder": "zeus/two", "repo": "quarterback", "title": "said nothing",
         "branch": "main"},
    ]}
    async with app.run_test(size=(80, 44)) as pilot:
        app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid", agent="host")
        app.render_board(board)
        await pilot.pause()
        agents = app.query_one("#agents")
        # dot ✕ who state stage … — and minus the ＋, which is a control rather
        # than an agent and has no stage to report.
        return [_cells(agents, i)[4] for i in range(agents.row_count - 1)]


#: A queue with one of each shape that matters: a row review can act on, and a
#: row it cannot. Both are open PRs and both belong on screen (#244).
QUEUE = {
    "open": 2, "depth": 1, "error": None, "idle": None,
    "oldest": {"age_seconds": 216000, "pr": 270, "repo": "prisonblues/quarterback"},
    "oldest_held": None,
    "entries": [
        {"repo": "prisonblues/quarterback", "pr": 264, "title": "a first round",
         "state": "unreviewed", "next_action": "review", "drainable": True,
         "holds": [], "age_seconds": 3600, "since_basis": "pr_opened",
         "age_is_upper_bound": False, "reason": "no run for this PR"},
        {"repo": "prisonblues/quarterback", "pr": 270, "title": "conflicting",
         "state": "blocked", "next_action": "integrate", "drainable": False,
         "holds": [{"code": "conflicting"}, {"code": "draft"}],
         "age_seconds": 216000, "since_basis": "pr_opened",
         "age_is_upper_bound": True, "reason": "the branch conflicts with its base"},
    ],
}


async def _drive_queue(offset) -> tuple[list[list[str]], str, list, str]:
    """Render the review queue, then click it at `offset`."""
    app_module = _load_app()
    qd = app_module.qd
    app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600,
                          scope=qd.Scope([qd.REPO]))
    for name in ("refresh_limits", "refresh_seats", "refresh_board",
                 "refresh_plan", "refresh_prs", "refresh_issues"):
        setattr(app, name, lambda: None)
    started: list = []
    app.spawn_refusal = lambda command: None
    app.run_spawn = lambda name, argv: started.append((name, argv))
    app.run_in_window = lambda name, command: started.append((name, [command]))

    async with app.run_test(size=(100, 50)) as pilot:
        app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid", agent="host")
        app.render_queue(QUEUE)
        await pilot.pause()
        table = app.query_one("#work")
        rows = [_cells(table, i) for i in range(table.row_count)]
        title = str(app.query_one("#t_work").content)
        await pilot.click(table, offset=offset)
        await pilot.pause(0.3)
        return rows, title, started, app.detail_text


def test_the_review_queue_keeps_the_rows_nothing_can_act_on():
    """#273 in the clickable renderer, and #244's rule inside it.

    The panel OPEN PRs cannot be: that one says a PR exists and CI is green and
    never said whether anybody had reviewed it. It was in the plain renderer only
    until #426, so flipping the seat pane's default would have taken it off the
    screen — which is how it stops being read a second time.

    A blocked entry KEEPS ITS PLACE, with the hold where its verb would go. A
    queue that hid what it could not act on would report a depth of zero for a
    repo where everything is stuck, and that reading is the one this panel exists
    to prevent.
    """
    rows, title, _, _ = asyncio.run(_drive_queue(offset=(60, 1)))
    # Narrow: state, ⚖, ▥, kind, rank, ref, title, why. The ▥ arrived after this
    # test did (#250) and moved every cell below it along one, which is the cost
    # the issue weighed against the row click it chose not to take over.
    assert [r[5] for r in rows] == ["#264", "#270"], rows
    assert [r[3] for r in rows] == ["pr", "pr"], rows
    # The verb and the wait share the `why` cell, which is the column every row in
    # this table gives to why it is where it is — a plan item's holder, a PR's
    # round. Four panels had four shapes of that column and now there is one.
    assert rows[0][7].startswith("panel"), rows[0]
    assert rows[1][7].startswith("conflicting"), rows[1]
    # The board's own word, abbreviated for the column: `integrate` is what a
    # DRAINABLE row would show, and this row is not one.
    assert "integrate" not in rows[1][7]
    # `~` on an age that is the longest the wait could have been — nothing
    # records when a branch started conflicting.
    assert "~" in rows[1][7], rows[1]
    assert "~" not in rows[0][7], rows[0]
    assert "1 waiting" not in title, "the depth moved to the header line"


def test_the_queues_scales_icon_is_live_only_where_a_round_is_what_is_wanted():
    """`fix`, `rebase` and `land` are real next actions with no button here.

    Drawing a live ⚖ on a conflicting branch would spend a whole panel round to
    be told it conflicts (#271), so the icon is grey there and the click explains
    the row instead of starting anything. Same dimming the unreachable-repo guard
    uses one panel over, so "grey means not offered" is one habit and not two.
    """
    rows, _, started, detail = asyncio.run(
        _drive_queue(offset=(_load_app().Dash.VERB_COLUMN + 2, 2)))
    assert rows[1][1] == "⚖", rows[1]
    assert not started, "the ⚖ started a round on a row it cannot drain"
    assert "conflicting" in detail, detail
    assert "would be: integrate" in detail, detail


def test_a_queue_row_says_every_reason_it_is_waiting_not_just_the_first():
    """`holds` is a list on purpose, and the detail line is where they all fit.

    "It is a draft" and "somebody holds the claim" are two facts; a reader shown
    only the first would act the moment the draft flag cleared and be wrong. The
    row above has room for one hold, so this line carries the rest — along with
    the board's own `reason` sentence, which exists nowhere else.
    """
    _, _, started, detail = asyncio.run(_drive_queue(offset=(60, 2)))
    assert not started
    assert "conflicting, draft" in detail, detail
    assert "the branch conflicts with its base" in detail, detail
    assert "at most 2d12h" in detail, detail
    assert "since it was opened" in detail, detail


def test_the_caps_line_carries_the_queue_depth():
    """The depth rides beside the budget it would be spent out of.

    A panel round costs tokens, so "1 waiting" is only actionable next to how
    much is left. Drawn on the limits clock, which is an hour long — so a new
    queue has to redraw it, or the cell up there keeps last hour's number while
    the panel below shows this minute's.
    """
    async def drive() -> str:
        app_module = _load_app()
        qd = app_module.qd
        app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600,
                              scope=qd.Scope([qd.REPO]))
        for name in ("refresh_limits", "refresh_seats", "refresh_board",
                     "refresh_plan", "refresh_prs", "refresh_issues"):
            setattr(app, name, lambda: None)
        async with app.run_test(size=(100, 50)) as pilot:
            app.render_limits(
                [{"label": "5h", "percent": 8, "resets": None, "severity": "ok"}],
                None)
            app.render_queue(QUEUE)
            await pilot.pause()
            return str(app.query_one("#limits").content)

    line = asyncio.run(drive())
    assert "REVIEW" in line and "1 waiting" in line, line


async def _drive_caps(limits, queue) -> tuple[bool, str]:
    """The caps line with whatever halves it is given — `(is it shown, what it says)`."""
    app_module = _load_app()
    qd = app_module.qd
    app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600,
                          scope=qd.Scope([qd.REPO]))
    for name in ("refresh_limits", "refresh_seats", "refresh_board",
                 "refresh_plan", "refresh_prs", "refresh_issues"):
        setattr(app, name, lambda: None)
    async with app.run_test(size=(100, 50)) as pilot:
        app.render_queue(queue)
        app.render_limits(limits, None)
        await pilot.pause()
        bar = app.query_one("#limits")
        return bar.display, str(bar.content)


def test_the_queue_cell_survives_a_box_with_no_caps_to_show():
    """The row is hidden when it has NOTHING to say, not when the caps are empty.

    `render_limits` gated the whole line on `limit_cells`, which returns nothing
    for an install with no subscription token — and, less obviously, for any pane
    under 20 columns, which is one `C-q <` away. Either took the review depth off
    the screen with the caps, in the two cases the cell was put on this line to
    survive. The panel's own reasoning says a depth of zero still draws because a
    cell that vanished when it was true could not be told apart from a dashboard
    that never asked; a cell that vanished with the caps is the same failure.
    """
    shown, line = asyncio.run(_drive_caps([], QUEUE))
    assert shown, "the caps line was hidden, taking the queue cell with it"
    assert "REVIEW" in line and "1 waiting" in line, line
    # No caps to sit beside means no gap to sit after.
    assert not line.startswith(" "), repr(line)


def test_with_neither_caps_nor_queue_the_row_is_still_hidden():
    """The half of the old rule that was right: an empty line is not a line."""
    shown, _ = asyncio.run(_drive_caps([], {}))
    assert not shown, "a row with nothing in it was drawn anyway"


def test_a_queue_that_could_not_be_fetched_says_so_in_a_row():
    """A board failure is a ROW, at the width of the panel — not a title suffix.

    The first cut of this port put the error in `#t_queue`, clipped to 24
    characters, and drew no row at all. The title is bounded by the pane, and a
    panel whose entire job is saying WHY something is waiting must not truncate
    the one message that says why it cannot tell you. The now-retired plain
    renderer drew it as a row from #273, and matching that was the whole argument
    for flipping the default in #426.
    """
    err = "board unreachable: HTTPConnectionPool(host='board.invalid', port=80)"
    rows, title, _, _ = asyncio.run(
        _drive_queue_state({**QUEUE, "entries": [], "depth": 0, "error": err}))
    assert len(rows) == 1, rows
    assert "board unreachable" in rows[0][-2], rows[0]
    # The message is in the row now, so the title is back to being a count.
    assert "HTTPConnectionPool" not in title, title


def test_a_drained_queue_says_it_is_drained_rather_than_going_blank():
    """"Nothing waiting" and "nothing fetched" are different answers.

    An empty table said neither: the title's `0 waiting` is a count, and a reader
    who cannot tell a quiet queue from a broken one will read the wrong one as
    the other. The board supplies its own wording in `idle`, which is why the
    literal here is only the fallback for a board too old to send one.
    """
    rows, _, _, _ = asyncio.run(
        _drive_queue_state({**QUEUE, "entries": [], "depth": 0, "error": None,
                            "idle": "every open PR has had a round"}))
    assert len(rows) == 1, rows
    assert "every open PR has had a round" in rows[0][-2], rows[0]


def test_a_queue_with_neither_entries_nor_a_board_falls_back_to_words():
    """A board too old to send `idle` still gets a sentence, not a blank.

    The sentence is the merged table's rather than the queue's — with the plan and
    the PRs in here too, "nothing waiting on review" would be answering for four
    sources on the strength of one.
    """
    rows, _, _, _ = asyncio.run(
        _drive_queue_state({**QUEUE, "entries": [], "depth": 0, "error": None,
                            "idle": None}))
    assert len(rows) == 1, rows
    assert "nothing in flight" in rows[0][-2], rows[0]


async def _drive_queue_state(queue) -> tuple[list[list[str]], str, list, str]:
    """`_drive_queue` without the click — the states that offer nothing to click."""
    app_module = _load_app()
    qd = app_module.qd
    app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600,
                          scope=qd.Scope([qd.REPO]))
    for name in ("refresh_limits", "refresh_seats", "refresh_board",
                 "refresh_plan", "refresh_prs", "refresh_issues"):
        setattr(app, name, lambda: None)
    async with app.run_test(size=(100, 50)) as pilot:
        app.render_queue(queue)
        await pilot.pause()
        table = app.query_one("#work")
        return ([_cells(table, i) for i in range(table.row_count)],
                str(app.query_one("#t_work").content), [], app.detail_text)


async def _drive_held_race() -> tuple[list[str], list[str]]:
    """`gh` answering before the board — the order the race can actually take.

    Both are started from `on_mount` with nothing sequencing them, and `gh issue
    list` is the slow one, so the board usually wins and the bug usually hides.
    Driven by hand here rather than waited for, because a test that reproduced
    this by racing two real calls would be the flake it is meant to replace.
    """
    app_module = _load_app()
    qd = app_module.qd
    app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600,
                          scope=qd.Scope([qd.REPO]))
    for name in ("refresh_limits", "refresh_seats", "refresh_board",
                 "refresh_plan", "refresh_prs", "refresh_issues"):
        setattr(app, name, lambda: None)

    app.backlog = True                       # the issue rows are behind `b` (#589)
    issues = [{"number": n, "title": f"issue {n}", "repo": qd.REPO,
               "updatedAt": "2026-08-25T00:00:00Z"} for n in (427, 426, 422)]
    # The newest issue is the held one, so a sort that knows about claims puts it
    # LAST and a sort that does not puts it FIRST. Any weaker fixture and the two
    # orders coincide and the test proves nothing.
    claims = [{"kind": "work", "key": f"{qd.REPO}#427", "holder": "hermes/x",
               "expires": None}]

    async with app.run_test(size=(100, 50)) as pilot:
        app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid", agent="host")
        app.render_issues(issues, None)
        await pilot.pause()
        table = app.query_one("#work")
        first = [_numbered_cell(table.get_row_at(i)) for i in range(table.row_count)]
        app.render_board({"agents": [], "claims": claims})
        await pilot.pause()
        after = [_numbered_cell(table.get_row_at(i)) for i in range(table.row_count)]
        return first, after


#: The same four shapes the printed suite uses: one riding a subject this table
#: draws, two on a subject it does not (#576 made several per subject possible on
#: purpose), and one on another repo.
BLOCKERS = [
    {"id": "b1", "repo": "prisonblues/quarterback",
     "subject": {"kind": "issue", "value": "427"}, "kind": "taste", "condition": "",
     "question": "which shade of blue?", "owner": "human/rich",
     "raised_by": "zeus/one", "raised_at": "2026-08-27T00:00:00+00:00"},
    {"id": "b2", "repo": "prisonblues/quarterback",
     "subject": {"kind": "repo", "value": "prisonblues/quarterback"},
     "kind": "environment", "condition": "landed", "question": "4 PRs ready to land",
     "owner": None, "raised_by": "zeus/doctor",
     "raised_at": "2026-08-27T00:00:00+00:00"},
    {"id": "b3", "repo": "prisonblues/quarterback",
     "subject": {"kind": "repo", "value": "prisonblues/quarterback"},
     "kind": "environment", "condition": "harness", "question": "8 scripts not on zeus",
     "owner": None, "raised_by": "zeus/doctor",
     "raised_at": "2026-08-27T00:00:00+00:00"},
]


async def _drive_waiting(press_w: bool = False) -> tuple[list[list[str]], str, str, str]:
    """Render the questions a person owes, optionally filter to them, and click one."""
    app_module, app = _quiet_dash()
    async with app.run_test(size=(110, 46)) as pilot:
        app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                             agent="host")
        app.render_issues(_issues_for(427, 426), None)
        app.render_board({"agents": [], "claims": []})
        app.render_blockers({"blockers": BLOCKERS, "counts": {}, "error": None})
        await pilot.pause()
        if press_w:
            await pilot.press("w")
            await pilot.pause(0.3)
        table = app.query_one("#work")
        rows = [_cells(table, i) for i in range(table.row_count)]
        blocked = next((str(rk.value) for rk in table.rows
                        if str(rk.value).startswith("blocker:")), None)
        if blocked:
            app.dispatch_row(blocked, column=99)
            await pilot.pause(0.2)
        return (rows, str(app.query_one("#t_work", app_module.Static).content),
                str(app.query_one("#limits").content), app.detail_text)


def test_the_questions_a_person_owes_reach_the_terminal():
    """#328's row, on the surface it never had one.

    The board has held a blocker as a first-class row since #274, and the web
    board and the plan page have both drawn it. The terminal never did: `/plan`
    served `waiting_on_a_human` on every item and `qbdata` referenced it zero
    times, so the field was being served and dropped.
    """
    rows, title, limits, detail = asyncio.run(_drive_waiting())
    assert any(r[0] == "⚑" for r in rows), f"nothing is marked as waiting: {rows}"
    # The repo's two questions have no work to ride, so they get a row — the half
    # that would otherwise be counted by the header and drawn nowhere (#274).
    repo_rows = [r for r in rows if "＋1" in r[-1]]
    assert len(repo_rows) == 1, f"the repo's questions reached no row: {rows}"
    assert "3 waiting on a human" in title, title
    assert "WAITING 3" in limits, limits
    # The question itself is the payload: a person reads it and answers it.
    assert "4 PRs ready to land" in detail and "unowned" in detail, detail


def test_nothing_is_offered_on_a_row_a_person_owes_an_answer_about():
    """Two rules, and the second is the one worth having.

    A row that is ONLY a question gets no icon: the state cell already wears the
    ⚑, and a second beside it says the same thing twice.

    A row that is a piece of work AND a question keeps its icon and goes grey.
    Taking an issue whose shape is still being decided is work done before the
    answer that governs it — the waste #522 is about, arriving through a button —
    and an icon that vanished would stop saying what the row would otherwise be.
    """
    async def drive() -> tuple[list, list]:
        app_module, app = _quiet_dash()
        async with app.run_test(size=(110, 46)) as pilot:
            app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                                 agent="host")
            app.repo_slug = app_module.qd.REPO
            app.render_issues(_issues_for(427, 426), None)
            app.render_board({"agents": [], "claims": []})
            app.render_blockers({"blockers": BLOCKERS, "counts": {}, "error": None})
            await pilot.pause()
            table = app.query_one("#work")
            out = []
            for i in range(table.row_count):
                cells = table.get_row_at(i)
                out.append((str(cells[0]), str(cells[1]),
                            str(getattr(cells[1], "style", ""))))
            return out, [app.work_action(app.rows[str(rk.value)])
                         for rk in table.rows
                         if app.rows.get(str(rk.value), {}).get("blocked")]

    rows, actions = asyncio.run(drive())
    only_questions = [r for r in rows if r[0] == "⚑" and r[1] == ""]
    assert only_questions, f"a row that is only a question drew an icon: {rows}"
    both = [r for r in rows if r[0] == "⚑" and r[1] != ""]
    assert both, f"a blocked issue lost the icon that says what it is: {rows}"
    assert all("cyan" not in r[2] for r in both), \
        f"a blocked row still offers to start work on it: {both}"
    assert actions and all(verb is None for _, verb in actions), actions


def test_w_shows_only_what_is_waiting_on_a_person():
    """The one door, in the terminal. Separate from `b`: the backlog is work nobody
    has started and this is work nobody CAN start, and a reader looking for one is
    not looking for the other."""
    rows, title, _, _ = asyncio.run(_drive_waiting(press_w=True))
    assert rows and all(r[0] == "⚑" for r in rows), rows
    assert title.startswith("WAITING · "), title
    # The plan's own counts describe a list the reader has just filtered away.
    assert "open" not in title and "next" not in title, title


async def _drive_scroll_across_a_rebuild() -> tuple:
    """Scroll WORK down, then let a poll land that changes a row it already draws.

    The rebuild is the point and it has to be a REAL one: `render_work` returns
    early when its signature has not moved, so a driver whose second render
    changed nothing would leave the scroll alone for the wrong reason and pass
    against the defect. A question arriving on an issue already on screen changes
    that issue's state glyph and its verb without adding or removing a row, which
    is the smallest rebuild there is — and the returned glyphs are asserted on so
    that "the table was rebuilt" is a fact this test checks rather than assumes.
    """
    app_module, app = _quiet_dash()
    async with app.run_test(size=(100, 20)) as pilot:
        app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                             agent="host")
        app.repo_slug = app_module.qd.REPO
        # Enough rows to overflow a 20-line screen several times over, so there is
        # somewhere to scroll TO.
        app.render_issues(_issues_for(*range(460, 400, -1)), None)
        app.render_board({"agents": [], "claims": []})
        await pilot.pause(0.2)
        table = app.query_one("#work")
        table.move_cursor(row=table.row_count - 1, animate=False)
        table._scroll_cursor_into_view(animate=False)
        await pilot.pause(0.2)
        before = table.scroll_y
        top_before = (str(list(table.rows)[int(before)].value)
                      if before >= 1 else None)
        glyphs_before = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        # ONE question, on a subject this table is already drawing. The repo-subject
        # blockers in BLOCKERS would each take a row of their own at the TOP, which
        # is a different claim (rows inserted above) and not the one being made here.
        app.render_blockers({"blockers": [dict(BLOCKERS[0],
                                               subject={"kind": "issue",
                                                        "value": "430"})],
                             "counts": {}, "error": None})
        await pilot.pause(0.4)
        after = table.scroll_y
        top_after = (str(list(table.rows)[int(after)].value)
                     if after >= 1 else None)
        glyphs_after = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        return before, after, top_before, top_after, glyphs_before, glyphs_after


def test_a_poll_that_lands_keeps_the_reader_where_they_had_scrolled_to():
    """A background rebuild must not throw the reader back to the top.

    `DataTable.clear()` resets `scroll_y`, so every poll that changed anything at
    all took a reader thirty rows up a seventy-row table and took the row they
    were reaching for with it. `work_sig` (#433) already limits this to the polls
    that changed something — a reader picks a row by looking at it — and this is
    what the polls that DID change something were still costing.

    It is also half of what made the live `_drive_issues` fail as "the icon did not
    raise the confirmation" — two runs in thirty on unmodified main, measured on one
    board on 2026-09-01 and not a rate anybody will reproduce.
    `refresh_board` fetches the claims and THEN the questions a person owes, the
    driver freezes the workers as soon as the claims land, and the second call
    arrives from the worker already in flight: after the driver has scrolled its
    row into view and read the screen position of its ⚒. The rebuild sent the table
    back to the top, and the click went to whatever row the top put under that
    position — usually one whose ⚒ is grey by design. `_let_the_workers_finish` is
    the other half.
    """
    (before, after, top_before, top_after,
     glyphs_before, glyphs_after) = asyncio.run(_drive_scroll_across_a_rebuild())
    # THE STATE THIS TEST CLAIMS TO BE IN, checked rather than assumed: it has to
    # have scrolled, and the poll has to have rebuilt the table.
    assert before >= 1, f"the table never scrolled, so nothing was at stake: {before}"
    assert glyphs_after != glyphs_before, \
        "the poll changed nothing, so no rebuild happened and this test proves nothing"
    assert after == before, \
        f"a poll threw the reader from row {before} back to row {after}"
    assert top_after == top_before, \
        f"the row at the top of the view changed: {top_before} -> {top_after}"


async def _drive_scroll_when_the_anchor_goes() -> tuple:
    """Two rebuilds from the same scrolled position: one that keeps the row the
    reader is anchored on, and one that does not.

    The pair is the test. "Back at the top when the anchor is gone" is also what an
    unpatched renderer does with every rebuild, so on its own it would pass against
    the defect — the half that cannot is the rebuild which KEEPS the row, and the
    two only mean something read together.
    """
    app_module, app = _quiet_dash()
    numbers = list(range(460, 400, -1))
    async with app.run_test(size=(100, 20)) as pilot:
        app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                             agent="host")
        app.repo_slug = app_module.qd.REPO
        app.render_issues(_issues_for(*numbers), None)
        app.render_board({"agents": [], "claims": []})
        await pilot.pause(0.2)
        table = app.query_one("#work")
        table.move_cursor(row=table.row_count - 1, animate=False)
        table._scroll_cursor_into_view(animate=False)
        await pilot.pause(0.2)
        before = table.scroll_y
        anchor = str(list(table.rows)[int(before)].value)

        # A rebuild that keeps the anchor row — a question landing on a row this
        # table already draws, which changes its state and its verb and no keys.
        app.render_blockers({"blockers": [dict(BLOCKERS[0],
                                               subject={"kind": "issue",
                                                        "value": "430"})],
                             "counts": {}, "error": None})
        await pilot.pause(0.4)
        kept = table.scroll_y
        top_kept = (str(list(table.rows)[int(kept)].value) if kept >= 1 else None)

        # And one that loses it: the issue the reader was anchored on is closed
        # between polls, which is the ordinary way a row leaves this table.
        gone = int(anchor.rsplit("#", 1)[1])
        app.render_issues(_issues_for(*[n for n in numbers if n != gone]), None)
        await pilot.pause(0.4)
        lost = table.scroll_y
        return (before, kept, top_kept, anchor, lost,
                [str(rk.value) for rk in table.rows])


def test_a_rebuild_that_loses_the_readers_row_goes_back_to_the_top():
    """The one case where keeping the reader's place has no right answer.

    An anchor that has been merged, closed or filtered away is not somewhere the
    table can be put back to, and a position computed from a row that no longer
    exists would be a guess wearing the shape of the reader's place. So the view
    stays where `clear()` left it, at the top — deliberately, and now checked,
    because a deliberate behaviour nothing tests is one revision away from being an
    accidental one.
    """
    before, kept, top_kept, anchor, lost, keys = asyncio.run(
        _drive_scroll_when_the_anchor_goes())
    # THE HALF THAT CANNOT PASS AGAINST THE DEFECT, and the state the other half
    # depends on: the table really did scroll, and a rebuild really did keep it.
    assert before >= 1, f"the table never scrolled, so nothing was at stake: {before}"
    assert kept == before, \
        f"a poll that kept the anchor row still moved the view: {before} -> {kept}"
    assert top_kept == anchor, f"the row at the top changed: {anchor} -> {top_kept}"
    # And the row really is gone, so "back to the top" is about a missing anchor
    # rather than about a rebuild that quietly did nothing.
    assert anchor not in keys, f"{anchor} is still in the table"
    assert lost == 0, \
        f"the view was put somewhere computed from a row that no longer exists: {lost}"


async def _drive_restore_guards() -> tuple:
    """Call `restore_work_scroll` four ways: live, superseded, and twice against a
    reader who has scrolled since — once away from the top, and once BACK to it.

    Called directly rather than raced into existence. Both guards are about the gap
    that deferring the restore opens — a second rebuild, or a hand on the wheel —
    and a test that tried to land either inside that gap would be timing-dependent
    about the very thing it is asserting.

    The fourth case is the one a position check cannot see. A reader who scrolled
    away and came deliberately back to the top leaves `scroll_y` at 0, which is
    exactly what `clear()` leaves it at, so only a flag set by their own wheel or
    key can tell the two apart.
    """
    app_module, app = _quiet_dash()
    async with app.run_test(size=(100, 20)) as pilot:
        app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                             agent="host")
        app.render_issues(_issues_for(*range(460, 400, -1)), None)
        app.render_board({"agents": [], "claims": []})
        await pilot.pause(0.2)
        table = app.query_one("#work")

        async def restore(generation, from_row, touched=False):
            table.scroll_to(y=from_row, animate=False)
            await pilot.pause(0.2)
            # AFTER the scroll above, which is this test moving the view and not the
            # reader — `scroll_to` sets no flag, so the two cannot be conflated.
            table.reader_scrolled = touched
            app.restore_work_scroll(generation, 20)
            await pilot.pause(0.2)
            return table.scroll_y

        # THE CONTROL FIRST: with nothing in the way it moves the view, so the three
        # refusals below are this method declining rather than this method being
        # unable to do anything at all.
        live = await restore(app.work_scroll, 0)
        stale = await restore(app.work_scroll - 1, 0)
        readers = await restore(app.work_scroll, 5)
        back_at_top = await restore(app.work_scroll, 0, touched=True)
        return live, stale, readers, back_at_top


def test_a_superseded_scroll_restore_and_a_readers_own_scroll_both_win():
    """The restore is deferred a refresh, and both guards are about that gap.

    A NEWER REBUILD WINS, and the reason is the row index rather than the ordering.
    Callbacks run in the order they were queued, so an older one runs FIRST — which
    would be harmless if a newer one were behind it to correct the view. The case
    that is not harmless is the older one running alone: its index belongs to the
    row list of the rebuild that queued it, and a second rebuild renumbers every row
    while capturing no anchor of its own (it reads `scroll_y` as 0, because the
    first restore has not run yet) and so queues no correction. The generation is
    what stops the stale index landing unopposed.

    AND THE READER WINS, asked of a flag their own wheel or key sets rather than of
    where the view happens to be. `clear()` leaves `scroll_y` at 0, so a reader who
    scrolled away and came deliberately back to the top is indistinguishable by
    position from one who never touched it — and the restore would then override a
    real choice, which is this change's own defect arriving from the other side.
    """
    live, stale, readers, back_at_top = asyncio.run(_drive_restore_guards())
    assert live == 20, f"the restore did not move the view at all: {live}"
    assert stale == 0, f"a superseded restore scrolled anyway: {stale}"
    assert readers == 5, f"the restore overwrote the reader's own scroll: {readers}"
    assert back_at_top == 0, \
        (f"a reader who scrolled back to the top was treated as one who never "
         f"scrolled, and the restore moved them to {back_at_top}")


@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "0", "-5"])
def test_a_drain_that_can_never_expire_is_refused_and_says_so(monkeypatch, value):
    """`nan` in this variable is the unbounded wait, back, spelled as three letters.

    The bound exists because a wedged worker that hangs the drivers forever gets a
    CI job killed with no signal at all. `float("nan")` is a valid float and
    `time.monotonic() >= start + nan` is False FOREVER, so a deadline built from it
    never expires and the guarantee is gone — reachable by typing a word into an
    environment variable. `inf` gets there by a shorter route, and 0 or a negative
    is not a bound either, it is a deadline in the past.

    The property asserted is the one that matters and not the specific number: what
    comes back is finite and positive, so a deadline built on it can expire.
    """
    monkeypatch.setenv("QB_DASH_WORKER_DRAIN", value)
    with pytest.warns(UserWarning, match="QB_DASH_WORKER_DRAIN"):
        drain = _worker_drain()
    assert math.isfinite(drain) and drain > 0, \
        f"{value!r} produced a deadline that cannot expire: {drain}"
    assert drain == _WORKER_DRAIN_DEFAULT, drain
    # The thing being prevented, stated as the arithmetic rather than trusted: this
    # is what the loop's own test does with the deadline it is given.
    assert not (time.monotonic() >= time.monotonic() + float("nan")), \
        "a nan deadline is expected to be one that never expires"


def test_a_drain_that_is_not_a_number_says_which_setting_and_what_was_in_it(monkeypatch):
    """A typo gets an error, not the default.

    Silently waiting 90s for somebody who asked for `9O` teaches them nothing, and
    the failure it eventually produces is attributed to whatever the test went on to
    do. Raised where it is read rather than at import, so it lands as a readable
    failure inside the test that wanted it instead of as a collection error over the
    whole file.
    """
    monkeypatch.setenv("QB_DASH_WORKER_DRAIN", "9O")
    with pytest.raises(AssertionError, match="QB_DASH_WORKER_DRAIN='9O'"):
        _worker_drain()


def test_a_drain_that_is_a_usable_number_of_seconds_is_taken_as_given(monkeypatch):
    """The knob still works, which is what makes the three refusals above refusals
    rather than the setting being ignored."""
    monkeypatch.setenv("QB_DASH_WORKER_DRAIN", "12.5")
    assert _worker_drain() == 12.5
    monkeypatch.delenv("QB_DASH_WORKER_DRAIN")
    assert _worker_drain() == _WORKER_DRAIN_DEFAULT
    monkeypatch.setenv("QB_DASH_WORKER_DRAIN", "   ")
    assert _worker_drain() == _WORKER_DRAIN_DEFAULT


#: A plan with three open items in one scope, one in another and one fleet-wide —
#: enough to prove a move stays inside its own list.
REORDER_PLAN = {
    "items": [
        {"item_id": "a", "repo": "prisonblues/quarterback", "state": "open",
         "title": "first", "rank": 1, "rank_source": "ordered", "ref": None,
         "blocked_by": [], "claim": None},
        {"item_id": "b", "repo": "prisonblues/quarterback", "state": "open",
         "title": "second", "rank": 3, "rank_source": "appended", "ref": None,
         "blocked_by": [], "claim": None},
        {"item_id": "c", "repo": "prisonblues/quarterback", "state": "open",
         "title": "third", "rank": 9, "rank_source": "appended", "ref": None,
         "blocked_by": [], "claim": None},
    ],
    "counts": {"open": 3}, "order_trust": {}, "next": None, "truncated": False,
}


async def _drive_reorder(keys=(), mark_rows=(), before=None, inside=None):
    """Render a plan, press some keys, and record what would have been sent.

    `run_reorder` is replaced rather than the client, so nothing reaches the board:
    what is under test is the array this screen computes and the refusals it makes
    before computing one.
    """
    app_module, app = _quiet_dash()
    sent: list = []
    async with app.run_test(size=(110, 46)) as pilot:
        app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                             agent="host")
        # A key that resolves, so the write path is not refused for the one reason
        # this driver is not about. `why_not()` is what the code asks.
        app.human = SimpleNamespace(why_not=lambda: None, NO_KEY="no key")
        app.run_reorder = lambda scope, order, moved: sent.append(
            {"scope": scope, "order": order, "moved": moved})
        app.render_plan(REORDER_PLAN, None)
        await pilot.pause(0.2)
        table = app.query_one("#work")
        by_key = {str(rk.value): i for i, rk in enumerate(table.rows)}
        if before:
            before(app, table, by_key)
        for want in mark_rows:
            table.move_cursor(row=by_key[f"plan:{want}"], animate=False)
            await pilot.pause(0.1)
            await pilot.press("m")
            await pilot.pause(0.1)
        for key in keys:
            await pilot.press(key)
            await pilot.pause(0.3)
        # ANYTHING THAT NEEDS THE APP ALIVE runs here. `app.screen` raises
        # `ScreenStackError` once `run_test` has exited, so a driver that handed
        # the app back and let the test look at a modal was asking a torn-down
        # object what was on screen.
        looked = inside(app, table, pilot) if inside else None
        return app, sent, table, looked


def test_a_nudge_sends_the_whole_order_for_one_scope():
    """`POST /plan/reorder` takes an ORDER, never a move — so up-one, jump-five and
    go-to-the-top differ only in the index handed to `reorder_ids` (#388's finding
    on the web board, reused rather than rediscovered)."""
    async def drive():
        _, sent, _, _ = await _drive_reorder(
            keys=("j",),
            before=lambda a, t, by: t.move_cursor(row=by["plan:a"], animate=False))
        return sent

    sent = asyncio.run(drive())
    assert len(sent) == 1, sent
    assert sent[0]["scope"] == "prisonblues/quarterback"
    assert sent[0]["order"] == ["b", "a", "c"], "down one did not move one place"
    assert sent[0]["moved"] == 1


def test_marked_rows_move_together_and_the_rank_cell_says_so():
    """The mark goes on the RANK cell, which is the column marking is about: a row
    is marked so the next move takes it, and the rank is where a move shows up."""
    async def drive():
        app, sent, table, _ = await _drive_reorder(mark_rows=("a", "b"))
        # Narrow: glyph verb read kind rank ref title why.
        ranks = [str(table.get_row_at(i)[4]) for i in range(table.row_count)]
        return app, sent, ranks

    app, sent, ranks = asyncio.run(drive())
    assert [r for r in ranks if r.startswith("▪")] == ["▪1", "▪~3"], ranks
    assert len(app.marked) == 2


def test_moving_marked_rows_keeps_the_order_they_are_shown_in():
    async def drive():
        _, sent, _, _ = await _drive_reorder(mark_rows=("a", "b"), keys=("]",))
        return sent

    sent = asyncio.run(drive())
    assert sent and sent[0]["order"] == ["c", "a", "b"], sent
    assert sent[0]["moved"] == 2


def test_a_move_that_changes_nothing_is_not_sent():
    """The endpoint stamps `rank_source` on every item it is handed, so an
    unchanged order would write "a human chose this" onto rows nobody moved."""
    async def drive():
        app, sent, _, _ = await _drive_reorder(
            keys=("k",),
            before=lambda a, t, by: t.move_cursor(row=by["plan:a"], animate=False))
        return app, sent

    app, sent = asyncio.run(drive())
    assert sent == [], "a no-op was sent to the board"
    assert "already there" in app.detail_text, app.detail_text


def test_a_row_with_no_order_says_so_rather_than_doing_nothing():
    """A control that silently does nothing is indistinguishable from a broken one,
    which is the rule the dim ⚒ already keeps one column over."""
    async def drive():
        app_module, app = _quiet_dash()
        sent: list = []
        async with app.run_test(size=(110, 46)) as pilot:
            app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                                 agent="host")
            app.human = SimpleNamespace(why_not=lambda: None, NO_KEY="no key")
            app.run_reorder = lambda *a: sent.append(a)
            app.render_prs(_TWO_REPOS_PRS[:1], None)
            app.render_board({"agents": [], "claims": []})
            await pilot.pause(0.2)
            table = app.query_one("#work")
            table.move_cursor(row=0, animate=False)
            await pilot.pause(0.1)
            await pilot.press("k")
            await pilot.pause(0.3)
            return app.detail_text, sent

    said, sent = asyncio.run(drive())
    assert sent == []
    assert "only the plan has an order" in said, said


def test_the_box_asks_for_a_position_and_says_which_one_it_is_now():
    """Ranks go non-contiguous as work finishes — `prisonblues/quarterback` was on
    `1, 3, 4, 5, 10, …` with 37 open items — so "move it to 10" means two different
    rows depending on which number a person meant. The box states the position and
    the length, and reads the typed number against those."""
    async def drive():
        _, _, _, looked = await _drive_reorder(
            keys=("g",),
            before=lambda a, t, by: t.move_cursor(row=by["plan:b"], animate=False),
            inside=lambda a, t, _p: (type(a.screen).__name__,
                                    getattr(a.screen, "here", None),
                                    getattr(a.screen, "total", None),
                                    getattr(a.screen, "scope", "?")))
        return looked

    name, here, total, scope = asyncio.run(drive())
    assert name == "MoveTo", name
    assert (here, total) == (2, 3), f"the box says position {here} of {total}"
    assert scope == "prisonblues/quarterback"


def test_the_box_refuses_a_position_that_is_not_one_before_spending_a_round_trip():
    async def drive():
        def refuse(app, table, pilot):
            box, out = app.screen, []
            for typed in ("nine", "0", "99"):
                box.query_one("#to").value = typed
                box.action_save()
                out.append(str(box.query_one("#why").content))
            return out, type(app.screen).__name__

        _, sent, _, looked = await _drive_reorder(
            keys=("g",),
            before=lambda a, t, by: t.move_cursor(row=by["plan:a"], animate=False),
            inside=refuse)
        said, still_open = looked
        return said, still_open, sent

    said, still_open, sent = asyncio.run(drive())
    assert "not a position" in said[0], said[0]
    assert "outside this plan" in said[1] and "outside this plan" in said[2], said
    assert still_open == "MoveTo", "a refused number closed the box"
    assert sent == []


def test_a_second_move_while_one_is_in_flight_is_refused_not_stacked():
    """`dial_writing`'s rule (#577) and for its reason: the worker is `exclusive`,
    so a second press would cancel the write that was about to report — and seeing
    nothing is exactly what makes a person press again."""
    async def drive():
        app, sent, _, _ = await _drive_reorder(
            keys=("j", "j"),
            before=lambda a, t, by: t.move_cursor(row=by["plan:a"], animate=False))
        return app.detail_text, sent

    said, sent = asyncio.run(drive())
    assert len(sent) == 1, f"a second move was sent while one was in flight: {sent}"
    assert "already going" in said, said


def test_a_mark_the_filter_hides_is_still_moved():
    """Reading the marks off the visible TABLE meant a row the `w` filter or the
    `s` scope had since hidden was silently left out — so a person who marked
    three rows and pressed a key was told "moved 2". That is the silent-narrowing
    defect arriving at the one control that rewrites the fleet's shared intent."""
    def hide_then_move(app, table, pilot):
        # Hide everything: nothing here is waiting on a person, so `w` empties the
        # table while the marks stay on this client.
        app.waiting = True
        app.plan_sig = None
        app.render_work()
        app.reorder("bottom")
        table = app.query_one("#work")
        return [str(rk.value) for rk in table.rows]

    async def drive():
        _, sent, _, showing = await _drive_reorder(mark_rows=("a", "b"),
                                                   inside=hide_then_move)
        return sent, showing

    sent, showing = asyncio.run(drive())
    assert not [k for k in showing if k.startswith("plan:")], \
        f"the filter did not actually hide the marked rows: {showing}"
    assert sent and sent[0]["moved"] == 2, \
        f"a hidden mark was dropped from the move: {sent}"
    assert sent[0]["order"] == ["c", "a", "b"], sent


def test_a_mark_on_work_that_is_no_longer_open_refuses_the_whole_move():
    """Every mark or none. An item somebody finished cannot be placed, and moving
    the rest would quietly do less than was asked while reporting success."""
    def finish_one_then_move(app, table, pilot):
        # `b` is gone from the board's answer — done, or dropped.
        app.plan = {**REORDER_PLAN,
                    "items": [i for i in REORDER_PLAN["items"] if i["item_id"] != "b"]}
        app.reorder("down")
        return app.detail_text

    async def drive():
        _, sent, _, said = await _drive_reorder(mark_rows=("a", "b"),
                                                inside=finish_one_then_move)
        return sent, said

    sent, said = asyncio.run(drive())
    assert sent == [], "a partial move was sent"
    assert "no longer open" in said, said


def test_the_selection_survives_a_move_so_a_block_can_be_moved_again():
    """Clearing the marks after a move looked like housekeeping and made the block
    a one-shot: the rows come back, the cursor lands on the head of them, and the
    next `j` moves that ONE row while the person believes they are still moving
    three. Silently changing what a key acts on is the worse mistake and the one
    they cannot see; the marks they can, because every marked row wears a ▪."""
    async def drive():
        app_module, app = _quiet_dash()
        # No pilot: this asserts on state a keypress does not reach, so there is
        # nothing to drive — the app only has to be alive for `query_one`.
        async with app.run_test(size=(110, 46)):
            app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                                 agent="host")
            app.marked = {"plan:a", "plan:b"}
            app.clear_reordering(moved=False)
            after_failure = set(app.marked)
            app.clear_reordering(moved=True)
            return after_failure, set(app.marked)

    after_failure, after_success = asyncio.run(drive())
    assert after_failure == {"plan:a", "plan:b"}, \
        "a failed move threw the selection away — the moment a person most wants " \
        "to press the key again"
    assert after_success == {"plan:a", "plan:b"}, \
        "a move that landed dropped the marks, so the next key moved one row " \
        "while the person believed they were still moving the block"


def test_a_moved_row_stays_under_the_cursor():
    """Press `j` four times and one thing moves four places.

    A move rewrites the whole table and a DataTable's cursor is an INDEX, so
    without following the key the row slides out from under the person and the
    next press moves whatever has taken its place — which is the same wrong thing
    the mark rules guard against, arriving through the redraw instead.
    """
    async def drive():
        app_module, app = _quiet_dash()
        async with app.run_test(size=(110, 46)) as pilot:
            app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                                 agent="host")
            app.human = SimpleNamespace(why_not=lambda: None, NO_KEY="no key")
            items = {i["item_id"]: i for i in REORDER_PLAN["items"]}
            seen: list = []

            def board_applies(scope, order, moved):
                """What the board does: renumber the scope and answer with it."""
                seen.append(list(order))
                app.plan = {**REORDER_PLAN,
                            "items": [{**items[i], "rank": n + 1}
                                      for n, i in enumerate(order)]}
                app.clear_reordering(True)
                app.render_work()

            app.run_reorder = board_applies
            app.render_plan(REORDER_PLAN, None)
            await pilot.pause(0.2)
            table = app.query_one("#work")
            table.move_cursor(row=0, animate=False)
            await pilot.pause(0.1)
            under = [app.selected_work()["title"]]
            for _ in range(2):
                await pilot.press("j")
                await pilot.pause(0.3)
                under.append(app.selected_work()["title"])
            return under, seen

    under, seen = asyncio.run(drive())
    assert under == ["first", "first", "first"], \
        f"the row moved out from under the cursor: {under}"
    assert seen == [["b", "a", "c"], ["b", "c", "a"]], \
        f"two presses did not move one row two places: {seen}"


async def _drive_optimistic(answer):
    """Press `j` and look at the table BEFORE and AFTER the board answers.

    `answer` is what the write does: a dict to return, or an exception to raise.
    It sleeps first, so the paint that happens before the round trip is
    observable — which is the whole of what "optimistic" means and the only way
    to tell it from a fast one.
    """
    app_module, app = _quiet_dash()
    items = {i["item_id"]: i for i in REORDER_PLAN["items"]}

    def write(path, body):
        time.sleep(0.6)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def titles(app):
        table = app.query_one("#work")
        return [str(table.get_row_at(i)[6]) for i in range(table.row_count)]

    async with app.run_test(size=(110, 46)) as pilot:
        app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                             agent="host")
        app.human = SimpleNamespace(why_not=lambda: None, post=write, NO_KEY="no key")
        app.render_plan(REORDER_PLAN, None)
        await pilot.pause(0.2)
        table = app.query_one("#work")
        table.move_cursor(row=0, animate=False)
        await pilot.pause(0.1)
        before = titles(app)
        await pilot.press("j")
        during = titles(app)                      # the board has not answered yet
        await pilot.pause(1.4)
        return before, during, titles(app), app.detail_text


def test_a_move_is_painted_before_it_is_posted():
    """A key press should move the row NOW. The write is a board call over the
    network, and a pane that sat still for the length of it would be pressed
    again — which is a second move, not a repeat of the first."""
    before, during, after, said = asyncio.run(_drive_optimistic(
        {"reordered": 1, "by": "human/rich", "appended": []}))
    assert before == ["first", "second", "third"], before
    assert during == ["second", "first", "third"], \
        f"the row did not move until the board answered: {during}"
    assert after == during, f"the confirmed order bounced: {after}"
    assert "moved 1" in said, said


def test_a_refused_move_puts_the_rows_back():
    """The optimistic paint is a guess at what the board will say, and a refusal is
    the board saying otherwise — so the guess is dropped whole rather than left on
    screen for somebody to act on."""
    before, during, after, said = asyncio.run(_drive_optimistic(
        RuntimeError("board says no")))
    assert during == ["second", "first", "third"], during
    assert after == before, f"a refused move was left on screen: {after}"
    assert "could not move" in said and "board says no" in said, said


def test_a_poll_does_not_repaint_over_a_move_that_has_not_landed():
    """The plan rides a fifteen-second clock and a reorder takes a round trip, so a
    tick arriving in between carries the order the board still has — the one
    WITHOUT the move. Applied, it puts the row back and then moves it again when
    the write answers: two jumps for one keypress. The web board keeps the same
    guard and calls it `busy`."""
    async def drive():
        app_module, app = _quiet_dash()
        async with app.run_test(size=(110, 46)) as pilot:
            app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                                 agent="host")
            app.human = SimpleNamespace(why_not=lambda: None,
                                        post=lambda p, b: time.sleep(0.6) or
                                        {"reordered": 1, "by": "human/rich",
                                         "appended": []}, NO_KEY="k")
            app.render_plan(REORDER_PLAN, None)
            await pilot.pause(0.2)
            table = app.query_one("#work")
            table.move_cursor(row=0, animate=False)
            await pilot.pause(0.1)
            await pilot.press("j")
            # The 15s tick lands mid-write, carrying the board's pre-move order.
            app.render_plan(REORDER_PLAN, None)
            during = [str(table.get_row_at(i)[6]) for i in range(table.row_count)]
            await pilot.pause(1.4)
            return during

    during = asyncio.run(drive())
    assert during == ["second", "first", "third"], \
        f"a poll repainted the move away before it had landed: {during}"


def _quiet_dash():
    """A Dash with every background worker off, for panels driven by hand.

    The scaffolding three #433 tests were each carrying a copy of. It is the
    convention the rest of this file already follows for its `_drive_*` families;
    a fourth copy would only make the next one cheaper to write than to share.
    """
    app_module = _load_app()
    qd = app_module.qd
    app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600,
                          scope=qd.Scope([qd.REPO]))
    for name in ("refresh_limits", "refresh_seats", "refresh_board",
                 "refresh_plan", "refresh_prs", "refresh_issues"):
        setattr(app, name, lambda: None)
    # The issue rows are behind `b` since #589 — they are a catalogue rather than
    # state — and these tests are ABOUT the issue rows, so they ask for them.
    app.backlog = True
    return app_module, app


def _issues_for(*numbers):
    """The panel's issue fixture: newest first, so a claims-aware sort moves them."""
    return [{"number": n, "title": f"issue {n}", "repo": _load_app().qd.REPO,
             "updatedAt": "2026-08-25T00:00:00Z"} for n in numbers]


def test_a_board_outage_does_not_turn_every_held_issue_free():
    """A failed poll is not an answer, and it arrives shaped like one.

    `fetch_board` reports an outage as `{"claims": [], "error": …}` — the same
    shape as "nobody holds anything". Taking it as one overwrites a real prior
    answer with `{}`, re-sorts every held issue up into the free rows, and
    defeats the ⚒'s guard, which reads `held` and cannot see WHY it is empty. The
    head line does say `● board unreachable`, but that is a different widget from
    the row under the pointer.

    So: the rows do not move, the title stops claiming a free count it cannot
    know, and the ⚒ refuses. Three assertions because the collapse shows up in
    three places and a fix that only reached the rows would still spend a claim.
    """
    async def drive() -> tuple[list[str], str, bool, list]:
        app_module, app = _quiet_dash()
        qd = app_module.qd
        started: list = []
        app.spawn_refusal = lambda command: None
        app.run_spawn = lambda name, argv: started.append((name, argv))
        claims = [{"kind": "work", "key": f"{qd.REPO}#427", "holder": "hermes/x",
                   "expires": None}]
        async with app.run_test(size=(100, 50)) as pilot:
            app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                                 agent="host")
            app.repo_slug = qd.REPO
            app.render_issues(_issues_for(427, 426), None)
            app.render_board({"agents": [], "claims": claims})
            await pilot.pause()
            app.render_board({"agents": [], "claims": [], "error": "HTTPError: 502"})
            await pilot.pause()
            table = app.query_one("#work")
            rows = [_numbered_cell(table.get_row_at(i)) for i in range(table.row_count)]
            app.fix_issue({"number": 426, "repo": qd.REPO})
            await pilot.pause()
            return (rows, str(app.query_one("#t_work", app_module.Static).content),
                    isinstance(app.screen, app_module.Confirm), started)

    rows, title, confirmed, started = asyncio.run(drive())
    assert rows == ["426", "427"], f"the outage re-sorted the held issue as free: {rows}"
    assert "claims unknown" in title and "free" not in title, title
    assert started == [] and not confirmed, "the ⚒ spent a claim during an outage"


def test_a_board_that_is_down_from_the_start_still_releases_the_panel():
    """The gate may not hang, and an outage is the case that could make it.

    Holding the paint until the board answers is only safe because every answer
    releases it, and the emptiest one — a board that has never answered and is
    down — has no last-good claims to fall back on. It paints anyway, in `gh`'s
    order, and says the claims are unknown rather than counting them as free.
    """
    async def drive() -> tuple[list[str], str]:
        app_module, app = _quiet_dash()
        async with app.run_test(size=(100, 50)) as pilot:
            app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                                 agent="host")
            app.render_issues(_issues_for(427, 426), None)
            await pilot.pause()
            app.render_board({"agents": [], "claims": [], "error": "HTTPError: 502"})
            await pilot.pause()
            table = app.query_one("#work")
            return ([_numbered_cell(table.get_row_at(i)) for i in range(table.row_count)],
                    str(app.query_one("#t_work", app_module.Static).content))

    rows, title = asyncio.run(drive())
    assert rows == ["427", "426"], f"a board outage left the panel waiting: {rows}"
    assert "claims unknown" in title, title


def test_the_issue_panel_does_not_count_issues_gh_has_not_listed_yet():
    """The board answering FIRST must not paint a confident zero.

    The other half of #433, and the same rule read from the other end. `gh` has
    its own "not asked yet", and the board usually wins — that is the ordinary
    order, a board POST against `gh issue list` — so the first answer used to
    reach `render_issues` with `self.issues` still empty and paint `ISSUES · 0`.
    Nothing was wrong with the repo; `gh` had not spoken. An absent signal drawn
    as a present good one is exactly what the claims side was fixed for, and
    qbdata states the rule for #324/#244 in as many words.

    The title has to say which answer is missing, so the assertion is on the
    word and on the absence of a count — a zero here is the defect itself.
    """
    async def drive() -> str:
        app_module, app = _quiet_dash()
        async with app.run_test(size=(100, 50)) as pilot:
            app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                                 agent="host")
            app.render_board({"agents": [], "claims": []})
            await pilot.pause()
            return str(app.query_one("#t_work", app_module.Static).content)

    title = asyncio.run(drive())
    assert "waiting for gh" in title, title
    # The plan's own `0 open` is a different number and is legitimately there —
    # what must not appear is a count of the list nobody has answered about.
    assert "free" not in title and "issues" not in title.lower(), \
        f"a count painted before gh answered: {title!r}"


def test_the_wait_for_the_board_does_not_swallow_a_gh_failure():
    """Two things can be wrong at once, and the wait has to report both.

    The `gh` failure is a ROW now rather than a title suffix clipped to 24
    characters (#589), for the reason the queue's error became one: a table whose
    job is saying why something is missing must not truncate the one message that
    says why it cannot tell you. The title still names what is OUTSTANDING, which
    is a different fact from what failed — and naming the board for a `gh` failure
    it already knows about would send a reader to the wrong end of the problem.
    """
    async def drive() -> tuple[str, list]:
        app_module, app = _quiet_dash()
        async with app.run_test(size=(100, 50)) as pilot:
            app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                                 agent="host")
            app.render_issues([], "HTTPError: 502")
            await pilot.pause()
            table = app.query_one("#work")
            return (str(app.query_one("#t_work", app_module.Static).content),
                    [_cells(table, i) for i in range(table.row_count)])

    title, rows = asyncio.run(drive())
    assert "waiting for the board" in title, title
    assert any("HTTPError" in c for row in rows for c in row), \
        f"the gh failure was dropped: {rows}"


def test_the_hammer_refuses_while_the_board_has_not_said_what_is_claimed():
    """Unknown is not free, at the one click that spends money.

    `fix_issue` read `self.held or {}`, which is the collapse #433 is about
    arriving at the worst place for it: `{}` means nobody holds this, and a click
    on that reading starts a session and takes a claim. It is reachable rather
    than theoretical — the PLANS ⚒ comes through here on its own worker, and that
    panel can be live while ISSUES is still waiting.

    The refusal is the whole assertion: no confirmation is raised, and the line
    says what is missing rather than reporting the issue as free.
    """
    async def drive() -> tuple[bool, str]:
        app_module, app = _quiet_dash()
        started: list = []
        app.spawn_refusal = lambda command: None
        app.run_spawn = lambda name, argv: started.append((name, argv))
        async with app.run_test(size=(100, 50)) as pilot:
            app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                                 agent="host")
            app.repo_slug = app_module.qd.REPO
            app.fix_issue({"number": 433, "repo": app_module.qd.REPO})
            await pilot.pause()
            return isinstance(app.screen, app_module.Confirm), app.detail_text, started

    confirmed, said, started = asyncio.run(drive())
    # NOTHING WAS LAUNCHED is the statement worth making, and `not confirmed` is
    # not it: QB_DASH_CONFIRM=0 is supported, and on a box with it set a fix_issue
    # that fell through would reach `run_spawn` with no dialog ever raised — which
    # this test would have called a pass, under its own name.
    assert started == [], f"the hammer started work: {started}"
    assert not confirmed, "the hammer asked to start work it could not know was free"
    assert "has not answered" in said, said


def test_the_first_paint_waits_rather_than_drawing_an_order_it_will_rearrange():
    """#433: the FIRST paint waits rather than drawing an order it will rearrange.

    `self.held` was `{}` before the board answered and `{}` when the board said
    nothing is held, so the panel could not tell "not asked yet" from an answer.
    When `gh` won the race it painted every issue as free and re-sorted the moment
    the claims arrived — #427 drawn at the top, then moved to the bottom, and a
    click on the top row taking #426 instead.

    The fix is not a faster board or a lock: it is that an order this panel is
    about to rearrange is worth less than no order at all, so it draws nothing
    until it can draw the right one. Hence the two assertions — the first paint is
    EMPTY, and the paint that follows is already final.

    The renewal guard on `render_board` cannot cover this and is not at fault: it
    compares holders, and the transition here is `{}` → `{}` whenever nothing is
    held, which compares equal.

    WHAT IS PINNED IS THE FIRST PAINT, which is what the name now says. A claim
    taken or dropped later re-sorts the table and is meant to: that is a real
    change in the answer, and the renewal guard exists to let the real ones
    through while stopping a renewal's moving expiry from doing the same. An
    earlier name here read as a promise that rows never move at all, which is
    neither true nor wanted.
    """
    first, after = asyncio.run(_drive_held_race())
    assert first == [], f"the table painted before it knew what was claimed: {first}"
    assert after == ["426", "422", "427"], after


def test_the_first_board_answer_paints_the_issues_even_when_nothing_is_held():
    """The empty answer is still an answer: no claims must paint, not stall.

    The other half of #433. Holding the paint until the board answers is only safe
    if EVERY answer releases it, and the emptiest one is the easy one to miss:
    `render_board` compares holders, and with nothing claimed anywhere that is `{}`
    against `{}`, which is not a change. A fix that released the paint on a holder
    change alone would trade an intermittent wrong row for an ISSUES panel that a
    quiet fleet never sees at all.

    Kept separate from the reorder test because it fails for a different reason and
    would otherwise be masked by it: this one is red against the naive fix and green
    against no fix at all, which is exactly the pairing that says the release
    condition — not merely the gate — is the part being pinned.
    """
    async def drive() -> tuple[int, list[str]]:
        app_module, app = _quiet_dash()
        qd = app_module.qd
        issues = [{"number": n, "title": f"issue {n}", "repo": qd.REPO,
                   "updatedAt": "2026-08-25T00:00:00Z"} for n in (427, 426)]
        async with app.run_test(size=(100, 50)) as pilot:
            app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                                 agent="host")
            app.render_issues(issues, None)
            await pilot.pause()
            table = app.query_one("#work")
            before = table.row_count
            app.render_board({"agents": [], "claims": []})
            await pilot.pause()
            return before, [_numbered_cell(table.get_row_at(i))
                            for i in range(table.row_count)]

    # BEFORE, then after. With no claims in the fixture the sorted order and the
    # plain `gh` order are the same list, so the final rows alone say nothing
    # about whether the empty answer released anything — an implementation that
    # never repainted would produce them too, and so would the code from before
    # #433. The empty first paint is the only part that can tell those apart.
    before, after = asyncio.run(drive())
    assert before == 0, f"the table painted before the board answered: {before} rows"
    assert after == ["427", "426"], after


def test_the_clickable_fleet_shows_how_far_along_each_agent_is():
    """#262: the stage cell, beside `state` in the merged AGENTS table.

    `state` says whether the pane is moving; this says where it has got to. The
    agent that reported nothing gets the fleet's glyph for an unsaid value, which
    is not alphanumeric and so cannot be read as a stage — the value space is 1-6
    alphanumerics, at the board's edge and in `qb-stage` before it.
    """
    stages = asyncio.run(_drive_stage())
    assert stages == ["R2", _load_app().qd.STAGE_UNREPORTED]


def test_a_qualified_repo_does_not_fill_the_repo_cell_with_its_owner():
    """#714 made a lease report `owner/name`, and this cell is eleven columns.

    The width is deliberate — `qbdata.short_repo`'s docstring is "the owner never
    distinguishes", on a fleet whose repos share one — so an unfolded slug clips to
    the half that distinguishes nothing and every AGENTS row reads `prisonblue…`.
    The plan table folded before calling and AGENTS did not, because until #714 a
    lease reported the checkout basename and had nothing to fold; the fold lives in
    `repo_cell` now so no caller can be the one that forgets.

    The colour is keyed on the same fold, so a PR row and the agent working it stay
    the same colour whichever spelling each of them arrived in.
    """
    app_module = _load_app()
    app = app_module.Dash.__new__(app_module.Dash)          # no screen, no board
    app.scope = SimpleNamespace(column=True)

    cell = app.repo_cell("prisonblues/quarterback")
    assert [str(t) for t in cell] == ["quarterback"]
    assert cell[0].style == app_module.qd.repo_colour("quarterback")

    app.scope = SimpleNamespace(column=False)
    assert app.repo_cell("prisonblues/quarterback") == [], \
        "a narrowed pane spends no columns saying the same word on every row"


def test_the_scope_narrows_the_rows_and_drops_the_column_together():
    """#261: one keypress, both halves.

    Narrowing without dropping the column leaves the waste in place; dropping it
    without narrowing leaves rows whose repo nothing states. And each panel has to
    say what it hid — a filtered pane that reads like the whole fleet is worse
    than an unfiltered one, because it is the same picture with fewer facts.
    """
    assert asyncio.run(_drive_scope()) == []


def test_a_guard_that_cannot_tell_which_repo_it_is_in_refuses(monkeypatch):
    """The one click on this pane that cannot be taken back, and the guard on it.

    `repo_slug` returns None for any checkout whose remote is not `origin` — the
    fork case this feature's slug comparison was written for — and reading that as
    "nothing to check" turned the guard off for EVERY row while `gh` and
    `git push` went on resolving a default remote of their own. A review would
    still have commented on, and pushed a fix commit to, whatever PR wore that
    number there.
    """
    app_module = _load_app()
    app = app_module.Dash.__new__(app_module.Dash)          # no screen, no board
    app.repo, app.repo_slug = "/somewhere", None
    assert app.wrong_repo("prisonblues/quarterback", "PR #1") is not None
    assert app.wrong_repo(None, "PR #1") is None, "a row naming no repo is not a mismatch"

    app.repo_slug = "PrisonBlues/quarterback"
    # Case-folded, like every other repo comparison here: `gh` reports GitHub's
    # canonical casing and the origin URL carries whatever was typed.
    assert app.wrong_repo("prisonblues/quarterback", "PR #1") is None
    assert app.wrong_repo("someone/else", "PR #1") is not None


async def _drive_icons() -> list[str]:
    """The verb column on rows this dashboard cannot act on.

    One table now, so one pass: the ⚖ a PR row wears and the ⚒ an issue row wears
    are the same column, and `work_action` is the single answer both the renderer
    and `dispatch_row` read. That is the property worth pinning — an icon drawn
    live and then refused is the "drawn takeable, refused one by one" the scope
    work exists to end, and it is now unreachable by construction rather than by
    four panels agreeing.
    """
    app_module = _load_app()
    qd = app_module.qd
    app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600,
                          scope=qd.Scope([qd.REPO, "someone/else"]))
    for name in ("refresh_limits", "refresh_seats", "refresh_board",
                 "refresh_plan", "refresh_prs", "refresh_issues"):
        setattr(app, name, lambda: None)
    # Reviewed PRs and unplanned issues are backlog rows since #589, and they are
    # what this test is about.
    app.backlog = True

    failures: list[str] = []
    async with app.run_test(size=(90, 44)) as pilot:
        app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid", agent="host")
        app.repo_slug = qd.REPO                            # what this checkout is
        # The board has answered and nothing is held. Said rather than assumed:
        # `held` is None until it answers and the backlog deliberately draws
        # nothing then (#433), so a driver that renders by hand has to stand in
        # for the board as well.
        app.held = {}
        app.render_prs([{"number": 1, "title": "ours", "repo": qd.REPO,
                         "isDraft": False, "statusCheckRollup": []},
                        {"number": 2, "title": "theirs", "repo": "someone/else",
                         "isDraft": False, "statusCheckRollup": []}], None)
        app.render_issues([{"number": 3, "title": "ours", "repo": qd.REPO},
                           {"number": 4, "title": "theirs", "repo": "someone/else"}], None)
        await pilot.pause()

        table = app.query_one("#work")
        styles = {}
        for row in range(table.row_count):
            cells = table.get_row_at(row)
            number = next(str(c).lstrip("#") for c in cells if str(c).startswith("#"))
            styles[number] = str(getattr(cells[app.VERB_COLUMN], "style", ""))
        for ours, theirs, what in (("1", "2", "the ⚖"), ("3", "4", "the ⚒")):
            if "cyan" not in styles.get(ours, ""):
                failures.append(f"{what}: this repo's icon is not live ({styles})")
            if "cyan" in styles.get(theirs, ""):
                failures.append(f"{what}: another repo's icon still looks "
                                f"clickable ({styles})")
    return failures


def test_an_icon_this_dashboard_cannot_act_on_says_so_before_the_click():
    """Dimmed, not merely refused afterwards.

    A bright ⚖ on a row from a repo this checkout is not is the same "drawn
    takeable, refused one by one" the scope work exists to end — and the README
    promises the icon itself says so.
    """
    assert asyncio.run(_drive_icons()) == []


async def _drive_review_pane(seats: list[dict]) -> tuple[list[list[str]], list[str]]:
    """run_in_pane against a recorder, so the tmux argv itself is the assertion.

    Checked against a real screen by hand as well; what this pins is the shape,
    because every part of it is load-bearing and none of it is obvious from
    reading the call: the split is anchored on a SEAT pane, the new pane is
    marked @qb_label and not @qb_seat, and the row is reflowed with -E.
    """
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600)
    app.refresh_limits = lambda: None
    app.refresh_seats = lambda: None
    # DIALS too — same reason as `_drive_seats`: it grows above the seat row this
    # asserts on, and a row that moved between the render and the read is a row
    # this reads at the wrong index.
    app.render_dials = lambda *a, **k: None

    calls: list[list[str]] = []
    windowed: list[str] = []

    class Done:
        returncode = 0
        stdout = "%9\n"
        stderr = ""

    app.run_in_window = lambda name, command: windowed.append(name)
    # qd is the SHARED qbdata module and subprocess is the real one, so both are
    # put back: a recorder left installed would silently disarm every later test
    # that expects a real call, and the suite would go green on nothing.
    real_seats, real_run = app_module.qd.tmux_seats, app_module.subprocess.run
    async with app.run_test(size=(90, 44)):
        app_module.qd.tmux_seats = lambda: (seats, None)
        app_module.subprocess.run = lambda argv, **kw: (calls.append(argv), Done())[1]
        app_module.os.environ["TMUX"] = "/tmp/whatever,1,0"
        try:
            app.run_in_pane("panel-42", "claude -- '/panel-review-pr 42'")
        finally:
            app_module.os.environ.pop("TMUX", None)
            app_module.qd.tmux_seats = real_seats
            app_module.subprocess.run = real_run
    return calls, windowed


def test_a_review_lands_in_the_seat_row_and_is_not_mistaken_for_a_seat():
    """The pane has to be reachable AND invisible to the seat machinery.

    Invisible matters as much as reachable: qb-seats --add, qb-seat-click's
    reflow and the tmux seat bar all select on @qb_seat, so a review wearing one
    would be offered an agent, counted as a seat, and given a ✕ that closes the
    wrong thing.
    """
    seats = [{"pane": "%7", "seat": "1", "session": "s", "window": "0",
              "command": "claude", "path": "/tmp/demo"}]
    calls, windowed = asyncio.run(_drive_review_pane(seats))
    assert not windowed, "it opened a window instead of joining the row"

    split = next(c for c in calls if c[:2] == ["tmux", "split-window"])
    assert "-t" in split and split[split.index("-t") + 1] == "%7", (
        f"the split is not anchored on a seat pane: {split}")
    assert "/panel-review-pr 42" in " ".join(split), split

    marked = next(c for c in calls if "set-option" in c)
    assert "@qb_label" in marked and "panel-42" in marked, marked
    assert "@qb_seat" not in " ".join(marked), (
        "the review pane was marked as a seat")

    layout = next(c for c in calls if "select-layout" in c)
    assert "-E" in layout, f"the row was not reflowed: {layout}"


def test_with_no_seat_row_to_join_a_review_still_gets_somewhere():
    """The dashboard runs outside the screen too — a window beats nothing."""
    calls, windowed = asyncio.run(_drive_review_pane([]))
    assert windowed == ["panel-42"]
    assert not any("split-window" in c for c in calls), calls


# ---- joining a pane to the board, on the session id (#540) -------------------
#
# `list-panes -a` is the whole tmux server and the board is the whole fleet, so a
# seat NUMBER identified a pane in neither: two screens can each hold a seat 1
# (#208) and two machines can each hold a `seat-lexray-1`. The pane carries the
# conversation in it instead, and the board returns the same id, so this is one
# lookup. Everything below is synchronous: it exercises the join, not the widgets,
# and does not need a pilot.


def _dash():
    return _load_app().Dash(interval=3600, gh_interval=3600)


def _agents(*holders):
    """A board answer. The session is derived from the holder so a test can name
    the one it means without a second table to keep in step."""
    return {"agents": [{"holder": h, "session": f"sess-{h}",
                        "state": "working", "reported": None}
                       for h in holders], "claims": []}


def _stated(app, **seat):
    """A pane record as tmux_seats returns one, filled in only where it matters."""
    return app.seat_state({"pane": "%0", "agent": "", **seat})


def _seat_labels(app, seats):
    """The label cell SEATS draws for each pane, without a pilot.

    `render_seats` writes into a DataTable, so the labels are read back off the
    stub's calls rather than off a widget — this is about which words it chooses,
    which the panel's own driver above does not assert on.
    """
    written: list = []

    class Table:
        def clear(self, *a, **k): pass

        def add_row(self, *cells, key=None, **k):
            written.append([str(getattr(c, "plain", c)) for c in cells])
            return SimpleNamespace(value=key)

    app.query_one = lambda sel, *a, **k: Table() if sel == "#agents" else _Sink()
    app.render_seats(seats)
    # Minus the ＋, which render_seats appends as a row of its own so the panel can
    # be driven by the mouse alone. It is not a seat and has no label to assert on.
    return [row[2] for row in written[:len(seats)]]


def test_one_screen_calls_a_pane_what_the_bar_and_the_border_call_it():
    """`seat 1`, in the three places a human reads it. Renaming it here when there
    is nothing to disambiguate would be a third spelling for one thing."""
    app = _dash()
    labels = _seat_labels(app, [{"pane": "%0", "seat": "1", "session": "seats-lexray",
                                 "agent": "", "command": "bash", "path": "/x"}])
    assert labels[0] == "seat 1"


def test_two_screens_are_told_apart_by_the_screen_they_are_in():
    """More than one screen on the server means more than one seat 1, so the number
    stops being a name.

    It was the seat's board SCOPE, carried on two pane options that existed to
    derive it. The screen has always known its own tmux name, and `qb-seats resume`
    takes that same name — minus the `seats-` prefix its own default naming puts on
    every screen, which distinguishes none of them and costs six of the thirteen
    columns this cell has (#540).
    """
    app = _dash()
    labels = _seat_labels(app, [
        {"pane": "%0", "seat": "1", "session": "seats-lexray", "agent": "",
         "command": "bash", "path": "/x"},
        {"pane": "%1", "seat": "1", "session": "qbseats", "agent": "",
         "command": "bash", "path": "/y"},
    ])
    assert labels == ["lexray 1", "qbseats 1"]


def test_a_seat_pane_is_matched_to_the_agent_whose_session_it_holds():
    """The join that replaced three narrowings and a guess.

    Two panes, both seat 1, on two screens of one box — the #208 collision that
    made a number useless as a name. Their sessions differ, so nothing has to be
    told apart.
    """
    app = _dash()
    app.seat_states = {"sess-lex": {"holder": "zeus/thorn-sumac"},
                       "sess-nix": {"holder": "zeus/amber-otter"}}
    assert _stated(app, seat="1", agent="sess-lex")["holder"] == "zeus/thorn-sumac"
    assert _stated(app, seat="1", agent="sess-nix")["holder"] == "zeus/amber-otter"


def test_another_machines_agent_is_not_shown_against_a_local_pane():
    """The board is the whole FLEET. Two machines could each hold a seat of the
    same name, and the old join broke that tie on a GUESS at this host's board
    name — narrowing that cost the state cell whenever the guess was wrong. A
    session id is unique across the fleet, so there is no tie and no guess."""
    app = _dash()
    app.seat_states = {"sess-here": {"holder": "zeus/thorn-sumac"},
                       "sess-there": {"holder": "laptop/cedar-flint"}}
    assert _stated(app, seat="1", agent="sess-here")["holder"] == "zeus/thorn-sumac"


def test_a_pane_with_no_agent_in_it_gets_no_state_rather_than_someone_elses():
    """A seat holding a bare shell — closed agent, or a screen built with an empty
    initial command — carries an empty @qb_session. It must not collide with an
    agent the board could not attribute either."""
    app = _dash()
    app.seat_states = {"sess-lex": {"holder": "zeus/thorn-sumac"}}
    assert _stated(app, seat="2", agent="") == {}
    assert _stated(app, seat="2") == {}


def test_an_agent_the_board_lists_without_a_session_is_not_filed_at_all():
    """`""` is what a pane with no agent looks up, so a bucket under it would
    answer that pane with somebody else's agent."""
    app = _dash()
    app.query_one = lambda *a, **k: _Sink()
    app.cfg = SimpleNamespace(base_url="https://board.example", agent="zeus")
    app.render_board({"agents": [{"holder": "zeus/thorn-sumac", "session": None,
                                  "state": "working", "reported": None}],
                      "claims": []})
    assert app.seat_states == {}
    assert _stated(app, seat="1", agent="") == {}


def test_the_fleet_table_stashes_every_agent_under_its_session():
    """render_board is what fills seat_states, and the key it uses is the join.

    Every agent, not only the ones that named themselves a seat: a pane running a
    session this screen did not start resolves now, where a name-derived seat
    number could never have seen it.
    """
    app = _dash()
    app.query_one = lambda *a, **k: _Sink()
    # The header line names the board, and there is not one configured in CI.
    app.cfg = SimpleNamespace(base_url="https://board.example", agent="zeus")
    app.render_board(_agents("zeus/thorn-sumac", "laptop/cedar-flint",
                             "zeus/amber-otter"))
    assert set(app.seat_states) == {"sess-zeus/thorn-sumac",
                                    "sess-laptop/cedar-flint",
                                    "sess-zeus/amber-otter"}


def test_the_seat_count_is_the_agents_sitting_in_a_pane_on_this_box():
    """"2 live · 1 seat · 1 idle" is about panes a click can reach.

    It counted holders whose NAME parsed as a seat, so an agent called
    `seat-lexray-1` on another machine counted towards this box's total and
    highlighted a row no click could land on.

    It is counted in ONE place now (#589). The head line stated a live count and a
    seat count worked out from its own join while the table below stated the same
    two off the rows it had drawn, and on the first frame with a seat in it they
    disagreed — "4 live · 1 seats" over a table headed "5 live · 3 seats". The
    pane with no agent in it gets a number of its own rather than being folded
    into either: those are the seats free to be given something to do.
    """
    app = _dash()
    said: list[str] = []
    app.query_one = lambda *a, **k: _Sink(said)
    app.cfg = SimpleNamespace(base_url="https://board.example", agent="zeus")
    app.seats = [{"pane": "%0", "seat": "1", "agent": "sess-zeus/thorn-sumac",
                  "command": "claude", "path": "/x"},
                 {"pane": "%1", "seat": "2", "agent": "", "command": "bash",
                  "path": "/y"}]
    app.render_board(_agents("zeus/thorn-sumac", "laptop/cedar-flint"))
    assert any("2 live · 1 seat · 1 idle" in t for t in said), said
    assert not any("live" in t and t.startswith("● ") for t in said), \
        "the head line is still counting, and the two answers can disagree"


class _Sink:
    """Every widget render_board reaches for, doing nothing. The join is what is
    under test here; the widgets have their own pilot-driven tests above.

    `said` collects what was written to it, for the one test whose subject is the
    header text rather than a join. Optional, so every other call site stays the
    bare `_Sink()` it was."""

    row_count = 0
    #: A table that was never laid out is at the top of itself, and `render_work`
    #: reads this to decide whether the reader had scrolled anywhere worth putting
    #: them back (#678). Zero is both the honest answer and the one that leaves the
    #: rest of that method on the path these tests are about.
    scroll_y = 0
    #: The title Statics are measured for the room their tally has left, so a
    #: stand-in for one has to have a size. Zero is the honest answer for a widget
    #: that was never laid out, and the renderer treats anything under 20 as "no
    #: room known yet" rather than as "no room" (render_work).
    size = SimpleNamespace(width=0)

    def __init__(self, said: list | None = None) -> None:
        self.said = said

    def update(self, *a, **k):
        if self.said is not None and a:
            self.said.append(str(getattr(a[0], "plain", a[0])))

    def clear(self, *a, **k): pass

    #: The chip bar rebuilds its columns rather than updating them — the columns
    #: ARE the chips — and hides itself when there is nothing to choose between.
    #: A stub missing either is a stub that turns a repo filter into an
    #: AttributeError inside render_agents, which every seat test goes through.
    def set_class(self, *a, **k): pass

    def add_columns(self, *a, **k): return []

    #: Keyed, singular — `render_chips` names each column after its repo so that a
    #: click resolves by identity rather than by position. Every seat test reaches
    #: render_agents, and render_agents draws the chip bar.
    def add_column(self, label, key=None, **k): return key

    def add_row(self, *a, key=None, **k):
        """Hands back a key like the real one does.

        `DataTable.add_row` returns the RowKey it used, and since #209 the
        panels file their record under that rather than under the key they
        asked for — so a stub returning None models a widget that does not
        exist, and would have hidden the caller getting it wrong."""
        return SimpleNamespace(value=key)


def test_a_fleet_click_jumps_to_the_pane_holding_that_conversation(monkeypatch):
    """A FLEET row carries a board identity and a session id, and the pane carries
    the same session id. Two panes both called seat 1 need no telling apart."""
    module = _load_app()
    # Built BEFORE the stub goes in: Dash.__init__ shells out to git for the repo
    # slug, and `module.subprocess` is the stdlib module itself, so patching .run
    # on it patches it for every caller in the process.
    app = module.Dash(interval=3600, gh_interval=3600)
    monkeypatch.setenv("TMUX", "/tmp/whatever,1,0")
    panes = ["%0\tsess-lex", "%9\tsess-nix"]
    selected: list[str] = []

    class Done:
        @property
        def stdout(self):
            return "\n".join(panes) + "\n"

    def fake_run(argv, **kw):
        if "select-pane" in argv:
            selected.append(argv[-1])
        return Done()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert app.jump_to_agent("sess-nix") is True
    assert selected == ["%9"]

    # An agent on another machine, or one whose pane has gone: no jump, and the
    # caller falls through to printing what it knows about the row.
    selected.clear()
    assert app.jump_to_agent("sess-elsewhere") is False
    assert selected == []

    # A row the board could not attribute a session to asks tmux nothing at all.
    assert app.jump_to_agent(None) is False
    assert app.jump_to_agent("") is False
    assert selected == []


def test_a_pane_holding_no_agent_is_never_jumped_to(monkeypatch):
    """Every bare shell on the box answers `@qb_session` with the empty string, so
    a lookup that did not guard the empty case would match all of them, find more
    than one candidate, and — with exactly one seat open — jump to it."""
    module = _load_app()
    app = module.Dash(interval=3600, gh_interval=3600)      # before the stub; see above
    monkeypatch.setenv("TMUX", "/tmp/whatever,1,0")
    selected: list[str] = []

    class Done:
        stdout = "%4\t\n"

    def fake_run(argv, **kw):
        if "select-pane" in argv:
            selected.append(argv[-1])
        return Done()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert app.jump_to_agent("") is False
    assert selected == []


# ---- one number, two repos (#209) --------------------------------------------
#
# `add_row` raises DuplicateKey rather than tolerating a repeated key, so a row
# key that is not unique does not degrade the panel — it takes the whole
# dashboard down, which is the worst thing this particular component can do:
# it is what you look at when something is already wrong.
#
# #208 fixed the reported instance by keying SEATS on the pane id. It did not
# fix the class. PRS and ISSUES are multi-repo — `_gh_list_many` concatenates
# `gh` output across every repo in QB_DASH_REPOS and tags each row with its
# origin — and both keyed their rows by the bare number, which two repos share
# as soon as they have both reached it.
#
# `qbdata.issue_key` already states the rule these panels were breaking: "The
# identity of an issue is the repo AND the number. Once the panels show more
# than one repo, a bare number stops being unique."
#
# The crash is only the louder half. `self.rows` is keyed the same way, so a
# collision that did NOT raise would silently point one row's click at the
# other repo's record — the ⚖ starting a paid panel review on the wrong PR.
# Both halves are asserted here.

#: Two repos, each with a #42 and a #7. The numbers match; nothing else does.
_TWO_REPOS_PRS = [
    {"number": 42, "title": "the quarterback one", "repo": "prisonblues/quarterback",
     "updatedAt": "2026-08-20T10:00:00Z"},
    {"number": 42, "title": "the lexray one", "repo": "prisonblues/lexray",
     "updatedAt": "2026-08-20T11:00:00Z"},
]
_TWO_REPOS_ISSUES = [
    {"number": 7, "title": "the quarterback one", "repo": "prisonblues/quarterback",
     "updatedAt": "2026-08-20T10:00:00Z"},
    {"number": 7, "title": "the lexray one", "repo": "prisonblues/lexray",
     "updatedAt": "2026-08-20T11:00:00Z"},
]


async def _drive_two_repos() -> list[str]:
    """Two repos' rows with a colliding number, in one table, clicked one by one.

    The render is wrapped rather than left to propagate: a test that dies of
    DuplicateKey reports an ERROR and names no assertion, and this suite's own
    convention — every `_drive_*` returns the failures it found — is what turns
    the crash into a statement about the defect.

    ONE TABLE MAKES THE COLLISION WORSE, not better, which is why this test is
    kept whole through the merge: a PR and an issue that share a number now share
    a table as well, and the key has to carry the repo AND the kind or four rows
    render as two.
    """
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600)
    # Every background fetch off: this table is being driven by hand, and a live
    # `gh` tick landing mid-test would rewrite the rows under the clicks.
    app.refresh_limits = lambda: None
    app.refresh_seats = lambda: None
    app.refresh_board = lambda: None
    app.refresh_plan = lambda: None
    app.refresh_prs = lambda: None
    app.refresh_issues = lambda: None
    app.backlog = True                    # reviewed PRs and free issues (#589)

    # As above: the board answered, nothing is held (#433).
    app.held = {}

    opened: list[str] = []
    app.open_pr = lambda pr: opened.append(f"{pr.get('repo')}#{pr.get('number')}")
    app.open_issue = lambda issue: opened.append(f"{issue.get('repo')}#{issue.get('number')}")

    failures: list[str] = []
    async with app.run_test(size=(100, 44)):
        try:
            app.render_prs(_TWO_REPOS_PRS, None)
            app.render_issues(_TWO_REPOS_ISSUES, None)
        except Exception as exc:                   # noqa: BLE001 — the defect itself
            return [f"two repos sharing a number took the dashboard down with "
                    f"{type(exc).__name__} — a duplicate row must degrade, not crash"]

        table = app.query_one("#work")
        want = _TWO_REPOS_PRS + _TWO_REPOS_ISSUES
        if table.row_count != len(want):
            return [f"{len(want)} rows from two repos rendered as {table.row_count}"
                    " — a row was dropped"]

        # The TABLE's own key, not the one it was rescued into. Asserted exactly,
        # and this is the assertion that makes the test about the defect:
        # ClickTable.add_row suffixes a repeat rather than raising, so with the
        # bare-number keys restored these rows STILL render as four, still carry
        # distinct keys (`pr:42` and `pr:42~2`) and still click through to their
        # own records — everything below passes and the bug is untouched. Only the
        # key itself tells the two fixes apart.
        keys = [rk.value for rk in table.rows]
        want_keys = sorted([f"pr:{r['repo']}#{r['number']}" for r in _TWO_REPOS_PRS]
                           + [f"issue:{r['repo']}#{r['number']}"
                              for r in _TWO_REPOS_ISSUES])
        if sorted(keys) != want_keys:
            failures.append(
                f"row keys are {sorted(keys)}, not {want_keys} — the table is not "
                "keying on the repo, whatever the backstop did after")

        # The click half. Each row must reach the record it displays; a shared key
        # means the second write wins and both rows open it.
        if len(set(keys)) != len(keys):
            failures.append(f"two rows share the row key {keys!r}")
        for key in keys:
            app.dispatch_row(key, column=None)
        if sorted(opened) != sorted(f"{r['repo']}#{r['number']}" for r in want):
            failures.append(f"clicking each row opened {sorted(opened)}")
    return failures


def test_two_repos_sharing_a_number_render_and_click_independently():
    """#209: a bare number is not an identity once the dashboard watches two repos.

    Asserted on both panels because they broke the same way for the same reason,
    and fixing one is exactly the shape of fix that leaves the other.
    """
    assert asyncio.run(_drive_two_repos()) == []


class _CapturedLog:
    """A widget's Textual logger, with `warning` kept where a test can read it.

    Everything else is swallowed rather than left to the real logger: this
    stands in for the whole `log` property while an app is running, and Textual
    reaches for `self.log(...)` and `self.log.debug(...)` on its own account.
    """

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def __call__(self, *a, **k) -> None: pass
    def __getattr__(self, name): return lambda *a, **k: None

    def warning(self, *a, **k) -> None:
        self._sink.append(" ".join(str(x) for x in a))


async def _drive_duplicate_keys() -> list[str]:
    """The backstop, through a real panel: two plan items with no item_id.

    PLAN keys on the board's `item_id`, which is not something this end can
    guarantee — two items arriving without one keyed every such row `plan:None`.
    That is the shape of duplicate nobody predicts, which is the shape the
    dashboard has to survive, so it is asserted on the panel rather than on the
    widget in isolation.
    """
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600)
    app.refresh_limits = lambda: None
    app.refresh_seats = lambda: None
    app.refresh_board = lambda: None
    app.refresh_plan = lambda: None
    app.refresh_prs = lambda: None
    app.refresh_issues = lambda: None

    nameless = {"items": [
        {"title": "first with no id", "repo": "prisonblues/quarterback"},
        {"title": "second with no id", "repo": "prisonblues/quarterback"},
    ]}
    failures: list[str] = []
    logged: list[str] = []
    with mock.patch.object(app_module.ClickTable, "log", _CapturedLog(logged)):
        async with app.run_test(size=(100, 44)):
            try:
                app.render_plan(nameless, None)
            except Exception as exc:               # noqa: BLE001 — the defect itself
                return [f"PLAN: two items with no item_id took the dashboard down with "
                        f"{type(exc).__name__} — an unforeseen duplicate must degrade"]
            table = app.query_one("#work")
            if table.row_count != 2:
                failures.append(
                    f"PLAN: two rows rendered as {table.row_count} — one was swallowed "
                    "rather than kept under a distinct key")
            keys = [rk.value for rk in table.rows]
            if len(set(keys)) != len(keys):
                failures.append(f"PLAN: the duplicate survived into the table as {keys!r}")
            # Degrading is only useful if the row still reaches its own record.
            seen = [app.rows.get(k, {}).get("title") for k in keys]
            if sorted(x for x in seen if x) != ["first with no id", "second with no id"]:
                failures.append(
                    f"PLAN: the rows point at {seen} — a suffixed row lost its record, "
                    "so it would render fine and do nothing when clicked")
    # And it has to be REPORTED, which is a separate claim from degrading. A row
    # key is never rendered, so the `~2` is invisible; and two plan rows is also
    # what correct data looks like. Absorb the collision without a word and a
    # keying bug that used to crash the dashboard now produces nothing at all.
    if not any("plan:None" in line for line in logged):
        failures.append(
            f"PLAN: the duplicate was absorbed in silence — the log holds {logged}, "
            "so nothing about this row would ever reach anybody")
    return failures


async def _drive_plan_fields() -> list[str]:
    """What a plan row and the PLANS title say, on data that is a literal here.

    The clickable renderer drew the same five cells as the printed one and none of
    the response envelope, so at the desk a reader could not answer "who chose this
    order", "did I get the whole list" or "what does the board say is next" — none
    of which is a layout question, and all of which were already on the wire.
    """
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600)
    for name in ("refresh_limits", "refresh_seats", "refresh_board", "refresh_plan",
                 "refresh_prs", "refresh_issues"):
        setattr(app, name, lambda: None)

    plan = {
        "items": [
            {"item_id": "a", "repo": "prisonblues/quarterback", "title": "chosen",
             "rank": 1, "rank_source": "ordered", "blocked_by": [], "claim": None,
             "ref": {"kind": "issue", "value": "394"}},
            {"item_id": "b", "repo": "prisonblues/quarterback", "title": "a pr to land",
             "rank": 2, "rank_source": "appended", "blocked_by": [], "claim": None,
             "ref": {"kind": "pr", "value": "397"}},
            {"item_id": "c", "repo": "prisonblues/quarterback", "title": "stuck and held",
             "rank": 3, "rank_source": "appended", "blocked_by": [{"ref": "9"}],
             "ref": None, "claim": {"holder": "zeus/jasper-moss"}},
        ],
        "counts": {"open": 40, "claimed": 1, "covered": 2, "blocked": 1, "stale": 4},
        "order_trust": {"trusted": False, "unchosen": 2},
        "next": {"item_id": "a", "repo": "prisonblues/quarterback",
                 "ref": {"kind": "issue", "value": "394"},
                 "caveat": "two of 40 open items sit where they were appended"},
        "truncated": True,
    }
    failures: list[str] = []
    async with app.run_test(size=(120, 44)):
        app.scope = app.scope.toggled() if not app.scope.column else app.scope
        app.build_columns()
        app.plan_sig = None
        app.render_plan(plan, None)
        table = app.query_one("#work")
        rows = [_cells(table, i) for i in range(table.row_count)]
        # Wide: glyph ⚒ ▥ kind repo rank ref title who — the `kind` cell is what
        # four panels used to say by being four panels (#589/#272), and the ▥ came
        # in beside the verb with #250.
        if [r[3] for r in rows] != ["iss", "pr", "plan"]:
            failures.append(f"PLAN: the kind cells read {[r[3] for r in rows]} — an "
                            "item takes the kind of what it references, and one with "
                            "no ref at all is a line of plan and nothing else")
        if [r[5] for r in rows] != ["1", "~2", "~3"]:
            failures.append(f"PLAN: the rank cells read {[r[5] for r in rows]} — the "
                            "human's order reaches the pane as row position alone, "
                            "with nothing saying which positions anybody chose")
        if [r[6] for r in rows] != ["#394", "PR#397", ""]:
            failures.append(f"PLAN: the ref cells read {[r[6] for r in rows]} — a PR "
                            "and an issue render the same, so nothing on the row says "
                            "why one ⚒ works and the other does not")
        if rows[0][0] != "◉":
            failures.append(f"PLAN: the board's own `next` is not marked: {rows[0]}")
        if rows[2][8] != "⊘zeus/jasper-moss":
            failures.append(f"PLAN: the who cell reads {rows[2][8]!r} — the machine or "
                            "the wait is missing, and both are facts about the row")
        title = _text(app.query_one("#t_work"))
        for wanted in ("40 open", "1 running", "2 covered", "1 blocked", "4 stale",
                       "~2 unchosen", "next #394", "truncated"):
            if wanted not in title:
                failures.append(f"PLAN: the title does not say {wanted!r}: {title!r}")
        # The click detail is where a sentence fits, and the caveat is a sentence.
        app.dispatch_row("plan:a", column=99)
        if "two of 40 open items" not in app.detail_text:
            failures.append(f"PLAN: clicking `next` does not show the board's caveat "
                            f"about it: {app.detail_text!r}")
        if "rank 1 (ordered)" not in app.detail_text:
            failures.append(f"PLAN: the detail line drops the provenance a row has no "
                            f"room for: {app.detail_text!r}")
        app.dispatch_row("plan:b", column=99)
        if "two of 40 open items" in app.detail_text:
            failures.append("PLAN: the caveat about `next` was shown on another row, "
                            "where it reads as a warning about that row")

        # A board that came back has to stop being reported as down. The redraw is
        # skipped when nothing changed, and "nothing changed" was true of a plan
        # whose rows did not move across the outage — so the report outlived the
        # error, in the one case where nothing else would ever clear it.
        #
        # It is a ROW rather than a title suffix since #589, for the reason the
        # queue's error became one: the title is bounded by the pane and was
        # clipping the message to 24 characters. The property under test is the
        # same either way — it has to go away when the board comes back.
        def down(app) -> bool:
            table = app.query_one("#work")
            return any("HTTPError" in str(c)
                       for i in range(table.row_count) for c in table.get_row_at(i))

        empty = {**plan, "items": [], "counts": {}, "next": None, "truncated": False}
        app.render_plan(empty, "HTTPError: 502")
        if not down(app):
            failures.append("PLAN: a dead board is not reported at all")
        app.render_plan(empty, None)
        if down(app):
            failures.append("PLAN: the board came back and the table still says it "
                            "is down — the report outlived the error")
    return failures


def test_the_plan_row_and_title_carry_what_the_board_sent():
    """#394: `fetch_plan` kept five of a plan item's fields and none of the
    response envelope, so the terminal could not say what the web page says."""
    assert asyncio.run(_drive_plan_fields()) == []


def test_an_unforeseen_duplicate_degrades_instead_of_taking_the_dash_down():
    """#209's general half: DataTable raises on a repeated key, and this is the
    component you look at when something is already wrong."""
    assert asyncio.run(_drive_duplicate_keys()) == []


async def _drive_a_watched_repos_pr() -> list[str]:
    """⚖ on a PR belonging to a repo this dashboard only WATCHES.

    The ⚒ on an issue row has refused this since the panels went multi-repo:
    `/fix-issue` takes a bare number and resolves the repository from the
    checkout it runs in, so starting one from the wrong pane lands that number
    on whatever issue wears it here. `/panel-review-pr` reads its repo the same
    way and does more with it — it spends money, comments on a public PR and
    pushes a fix commit — and the ⚖ was not making the same check.

    Reachable only since #209. Two repos sharing a PR number used to crash the
    panel before either row rendered, so the wrong-repo ⚖ was a click nobody
    could make; keying on the repo is what puts both rows on the screen.
    """
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600)
    app.refresh_limits = lambda: None
    app.refresh_seats = lambda: None
    app.refresh_board = lambda: None
    app.refresh_plan = lambda: None
    app.refresh_prs = lambda: None
    app.refresh_issues = lambda: None
    # No dialog in the way: the refusal has to come BEFORE the confirmation,
    # since a confirmation naming the right number and the wrong repo is a
    # human being asked to approve the mistake.
    app.confirm = False
    app.repo_slug = "prisonblues/quarterback"
    app.backlog = True                    # a reviewed PR is a backlog row (#589)

    started: list[tuple[str, str]] = []
    app.run_in_pane = lambda name, command: started.append((name, command))
    app.run_in_window = lambda name, command: started.append((name, command))

    # AND THE BROWSER, which is the other thing a click on this row can reach.
    # A dim ⚖ falls through to "say what the row is", and for a PR row that is
    # `open_pr` — so an unstubbed `open_url` here spends every run of this suite
    # on a real `xdg-open` of another repo's PR. That tab is the test's own
    # doing, and once the fleet ran this suite in parallel worktrees it arrived
    # on a person's screen every few minutes with nothing to say why.
    #
    # STUBBED AT `open_url` rather than lower, so `open_pr` still runs and the
    # URL it builds is still the thing asserted below. A stub on `open_pr`
    # would prevent the tab and stop covering the `/pull/<number>` it names.
    opened: list[str] = []
    app.open_url = lambda url: opened.append(url)

    failures: list[str] = []
    async with app.run_test(size=(100, 44)):
        app.render_prs(_TWO_REPOS_PRS, None)
        for rk in list(app.query_one("#work").rows):
            row = app.rows[str(rk.value)]
            named = f"{row['repo']}{row['ref']}"
            # What the fall-through must reach: THIS row's PR, not merely
            # something with this row's repo in it. `open_pr` builds it the same
            # way, so the assertion pins the URL construction as well as the
            # routing — a number taken from the wrong record still reads as the
            # right repo, and that is the confusion the whole test is about.
            expected = f"https://github.com/{row['repo']}/pull/{row['pr']['number']}"
            started.clear()
            opened.clear()
            app.detail_text = ""
            app.dispatch_row(str(rk.value), column=app_module.Dash.VERB_COLUMN)
            if row["repo"] == app.repo_slug:
                if not started:
                    failures.append(
                        "⚖ on this dashboard's OWN PR started nothing — the guard "
                        f"is refusing everything, not just another repo's ({app.detail_text})")
                elif opened:
                    failures.append(
                        f"⚖ on {named} started the review and ALSO opened {opened!r} — "
                        "the verb acted, so the row should not have fallen through")
            elif started:
                failures.append(
                    f"⚖ on {named} launched {started[0][1]!r} — a paid review, in "
                    f"{app.repo_slug}, of whatever wears that number there")
            elif opened != [expected]:
                failures.append(
                    f"⚖ on {named} refused but opened {opened!r}, not [{expected!r}] — "
                    "a dim icon that swallows the click is indistinguishable from a "
                    "broken one, and the one it does not swallow must be its own")
    return failures


def test_the_scales_refuse_a_pr_from_a_repo_this_dashboard_only_watches():
    """The ⚖ makes the same check the ⚒ does, and #209 is what makes it reachable."""
    assert asyncio.run(_drive_a_watched_repos_pr()) == []


# ------------------------------------------------- the ⚒, and the gate under it
#
# NO BOARD AND NO LIVE DATA. Every test below hands the dashboard a `qb-start`
# that answers however the test needs and an issue that is a literal, so they run
# in CI under the `tui` extra rather than only on a developer's laptop — which
# matters here more than anywhere else in this file, because what they are about
# is a button that must refuse on a machine that has not opted in, and CI is such
# a machine.
#
# They also do not run the app. `fix_issue` is a decision — refuse, ask, or start
# — and the decision is reachable with `say` and `push_screen` stubbed, so none of
# these needs an event loop to say what the button does.

def _fake_qb_start(tmp_path, *, answer: str = "{}", policy_exit: int = 0,
                   spawn_exit: int = 0, spawn_out: str = "", spawn_err: str = "") -> str:
    """A `qb-start` that answers `--policy` one way and a spawn another.

    `#!/bin/sh`, never `#!/usr/bin/env` — a runtime-written stub is past
    `patchShebangs` and there is no `/usr/bin/env` inside a nix build (#177,
    and `test_runtime_stub_shebangs.py` enforces it).
    """
    script = tmp_path / "qb-start"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {tmp_path / "ran.log"}\n'
        'if [ "$1" = "--policy" ]; then\n'
        f"  printf '%s' '{answer}'\n"
        f"  exit {policy_exit}\n"
        "fi\n"
        f"printf '%s' '{spawn_out}'\n"
        f"printf '%s' '{spawn_err}' >&2\n"
        f"exit {spawn_exit}\n")
    script.chmod(0o755)
    return str(script)


ENABLED_ANSWER = ('{"enabled": true, "commands": ["/fix-issue"], '
                  '"max_sessions": 1, "policy": "/home/x/.config/quarterback/spawn.json"}')
OFF_ANSWER = ('{"enabled": false, "commands": [], "reason": '
              '"spawning is not enabled on this machine - there is no '
              '/home/x/.config/quarterback/spawn.json. A machine opts in by setting '
              '`programs.quarterback-harness.spawn.enable = true`"}')


class _Clicked:
    """One dashboard with its three outward edges recorded instead of taken."""

    def __init__(self, tmp_path, start_bin: str, held: dict | None = None):
        module = _load_app()
        self.module = module
        self.app = module.Dash(interval=3600, gh_interval=3600)
        self.app.start_bin = start_bin
        self.app.held = held or {}
        self.said: list[str] = []
        self.dialogs: list[tuple] = []
        self.spawned: list[tuple] = []
        self.windowed: list[tuple] = []
        self.app.say = self.said.append
        self.app.push_screen = lambda screen, cb=None: self.dialogs.append((screen, cb))
        self.app.run_spawn = lambda name, argv: self.spawned.append((name, argv))
        # Stubbed so that the ⚒ quietly reverting to the old direct spawn shows up
        # here as a failure rather than as a passing test.
        self.app.run_in_window = lambda name, command: self.windowed.append((name, command))

    def confirm(self) -> None:
        """Say yes to the dialog the click raised."""
        assert self.dialogs, "no confirmation was raised"
        self.dialogs[-1][1](True)


def test_the_hammer_refuses_on_a_machine_that_has_not_opted_in(tmp_path):
    """The obstacle #360 named and declined to walk into: `qb-start` ships off, so
    routing a working button through it makes the button stop working until
    somebody writes one line of nix. The answer is to refuse with the reason and
    the remedy — and to do it INSTEAD of the dialog rather than after it, since
    the machine's answer is knowable before the click is spent."""
    box = _Clicked(tmp_path, _fake_qb_start(tmp_path, answer=OFF_ANSWER, policy_exit=3))
    box.app.fix_issue({"number": 7})
    assert box.dialogs == [], "a machine that cannot spawn still asked whether to"
    assert box.spawned == []
    assert box.said and "programs.quarterback-harness.spawn.enable" in box.said[-1], \
        f"the refusal must name the remedy: {box.said}"


def test_the_hammer_does_not_fall_back_to_the_old_uncounted_spawn(tmp_path):
    """The tempting shape, and the one thing this must not do. A fallback would
    make "this machine has not opted in" a fact about which code path ran rather
    than about the machine, and would put two behaviours behind one icon — a
    counted, claimed, board-recorded session on one box and an uncounted one on
    another, with nothing on screen to say which you got."""
    box = _Clicked(tmp_path, _fake_qb_start(tmp_path, answer=OFF_ANSWER, policy_exit=3))
    box.app.fix_issue({"number": 7})
    assert box.windowed == [], "the ⚒ started a session the gate had refused"
    # And it did not get as far as offering to: a dialog raised on a machine that
    # cannot spawn is the fallback's first half, whether or not the second half
    # is there yet.
    assert box.dialogs == []


def test_a_qb_start_that_will_not_run_fails_closed(tmp_path):
    """A broken install is not a machine that said no, and the two want different
    things done about them — but neither of them starts a session."""
    box = _Clicked(tmp_path, str(tmp_path / "not-installed"))
    box.app.fix_issue({"number": 7})
    assert box.dialogs == [] and box.spawned == [] and box.windowed == []
    assert "not-installed" in box.said[-1]


def test_a_command_this_machine_did_not_name_says_which_option_names_it(tmp_path):
    """`spawn.commands` is the second lock — turning spawning on is one decision
    and saying what may come through it is another — so it is a refusal an
    operator meets while opting in, and it has to name the key."""
    box = _Clicked(tmp_path, _fake_qb_start(
        tmp_path, answer='{"enabled": true, "commands": [], "policy": "/tmp/spawn.json"}'))
    box.app.fix_issue({"number": 7})
    assert box.dialogs == [] and box.spawned == []
    assert "spawn.commands" in box.said[-1]


def test_an_enabled_machine_asks_first_and_then_runs_qb_start(tmp_path):
    """The confirmation is not weakened by any of this: /fix-issue writes a branch
    and opens a PR, so a stray click on a 78-column pane still must not start one."""
    box = _Clicked(tmp_path, _fake_qb_start(tmp_path, answer=ENABLED_ANSWER))
    box.app.fix_issue({"number": 7})
    assert box.spawned == [], "the icon started a fix with no confirmation"
    assert len(box.dialogs) == 1
    box.confirm()
    assert len(box.spawned) == 1, "confirming did not start the fix"
    name, argv = box.spawned[0]
    assert argv[0] == box.app.start_bin, f"the ⚒ did not go through qb-start: {argv}"
    assert argv[1:3] == ["/fix-issue", "7"]
    assert "--via" in argv and argv[argv.index("--via") + 1] == "dash", \
        f"a spawn with no provenance cannot be traced back to the click: {argv}"
    assert argv[argv.index("--repo-path") + 1] == box.app.repo
    assert name == "fix-issue-7", "the window named should be the one qb-start makes"


def test_cancelling_the_confirmation_starts_nothing(tmp_path):
    box = _Clicked(tmp_path, _fake_qb_start(tmp_path, answer=ENABLED_ANSWER))
    box.app.fix_issue({"number": 7})
    box.dialogs[-1][1](False)
    assert box.spawned == [] and box.windowed == []
    assert box.said[-1] == "cancelled"


def test_a_held_issue_is_refused_with_the_release_that_would_free_it(tmp_path):
    """The reversal of this method's own previous sentence, and the claim is what
    reverses it. Warning and proceeding cost nothing while the click took no
    claim; now it takes one, so proceeding is `qb-claim` refusing at exit 8 — a
    dialog whose only possible outcome is no."""
    import qbdata as qd
    issue = {"number": 7, "repo": None}
    box = _Clicked(tmp_path, _fake_qb_start(tmp_path, answer=ENABLED_ANSWER),
                   held={qd.issue_key(issue): {"holder": "zeus/seat-1"}})
    box.app.fix_issue(issue)
    assert box.dialogs == [] and box.spawned == []
    assert "zeus/seat-1" in box.said[-1] and "qb-release issue 7" in box.said[-1]


def test_the_machine_is_asked_on_every_click_rather_than_once_at_mount(tmp_path):
    """So that opting a machine in takes effect on the next click instead of on
    the next dashboard. It costs one local process that reads one file."""
    box = _Clicked(tmp_path, _fake_qb_start(tmp_path, answer=ENABLED_ANSWER))
    box.app.fix_issue({"number": 7})
    box.app.fix_issue({"number": 8})
    log = tmp_path / "ran.log"                 # never written is an answer, not an error
    asked = [ln for ln in (log.read_text().splitlines() if log.exists() else [])
             if ln.startswith("--policy")]
    assert len(asked) == 2, f"the gate was asked {len(asked)} times for two clicks"


def test_the_plan_hammer_goes_through_the_same_gate(tmp_path):
    """A plan row's ⚒ and an issue row's ⚒ are one verb — that is why they are in
    the same column — so a gate on one of them and not the other would be the
    worst of both."""
    box = _Clicked(tmp_path, _fake_qb_start(tmp_path, answer=OFF_ANSWER, policy_exit=3))
    box.app.fix_plan_item({"item_id": "abc", "title": "do the thing",
                           "ref": {"kind": "issue", "value": "7"},
                           "repo": box.app.repo_slug})
    assert box.dialogs == [] and box.spawned == [] and box.windowed == []
    assert "programs.quarterback-harness.spawn.enable" in box.said[-1]


# ---- what qb-start answered, as a line somebody reads --------------------------

def _done(returncode: int, stdout: str = "", stderr: str = ""):
    from subprocess import CompletedProcess
    return CompletedProcess(["qb-start"], returncode, stdout, stderr)


def test_a_started_session_is_reported_with_the_way_to_stop_it():
    module = _load_app()
    line = module.spawn_answer("fix-issue-7", _done(
        0, '{"started": true, "session": "0f2c1d5e-aaaa-bbbb-cccc-ddddeeeeffff"}'))
    assert "fix-issue-7" in line and "0f2c1d5e" in line and "qb-end" in line


def test_a_refusal_is_reported_in_qb_starts_own_words():
    """Seven refusals with seven different remedies, and a second copy of those
    sentences in the dashboard would be a second copy to keep true. The last two
    lines are the verdict and its remedy; the gates print theirs above them."""
    module = _load_app()
    line = module.spawn_answer("fix-issue-7", _done(
        8, "", "qb-claim: held by zeus/seat-1\n"
               "refused: issue 7 is already claimed\n"
               "  run `qb-claimed` to see who has it\n"))
    assert "already claimed" in line and "qb-claimed" in line


def test_an_answer_with_no_words_at_all_still_names_the_exit():
    module = _load_app()
    assert "9" in module.spawn_answer("fix-issue-7", _done(9, "not json", ""))


# ---- the dials, in the clickable renderer (#477) ------------------------------
#
# The panel this dashboard did not have, and the one nothing anywhere had: a dial
# was set from an endpoint and read back by one function in `panel_seats.py`, so
# the values governing every round on the fleet were invisible on every screen a
# person or an agent actually looks at.
#
# What these pin is the half the panel CANNOT do. `POST /dials` takes
# `app.auth.human` and this program holds the machine bearer token every agent on
# the box holds, so the ✎ is a door and not a control — and a door nobody can find
# is the same as no door (#443).

DIALS = {
    "asked": True,
    "error": None,
    "shadowed": [{"dial": "tempo", "value": "eager", "repo": None, "scope": "fleet",
                  "reason": "fleet default", "set_by": "human/rich",
                  "set_at": "2026-08-25T10:00:00+00:00", "expires_at": None}],
    "dials": [
        {"dial": "tempo", "value": "held", "repo": "prisonblues/quarterback",
         "scope": "repo", "reason": "this repo is mid-release", "set_by": "human/rich",
         "set_at": "2026-08-25T10:00:00+00:00", "expires_at": None},
        {"dial": "review_panel.max_rounds", "value": 2, "repo": None, "scope": "fleet",
         "reason": "the window is at 94%", "set_by": "human/rich",
         "set_at": "2026-08-25T10:00:00+00:00",
         "expires_at": "2099-01-01T00:00:00+00:00"},
    ],
}


class FakeHuman:
    """A human credential that records instead of writing (#479's, stubbed).

    `why_not` is the one method the panel asks on every paint, and returning None
    is what makes the ✎ a control — so a driver that wants the read-only shape
    passes `why=<a sentence>` and gets the fallback back.
    """

    def __init__(self, why: str | None = None, fail: str | None = None):
        self.why, self.fail = why, fail
        self.set: list = []
        self.cleared: list = []

    def why_not(self):
        return self.why

    def set_dial(self, dial, value, reason, repo=None, expires_at=None):
        if self.fail:
            raise RuntimeError(self.fail)
        self.set.append((dial, value, reason, repo, expires_at))
        return {"replaced": [{"value": "P4", "reason": "the old argument"}]}

    def clear_dial(self, dial, repo=None):
        if self.fail:
            raise RuntimeError(self.fail)
        self.cleared.append((dial, repo))
        return {"cleared": [{"dial": dial}]}


async def _drive_dials(offset=None, key=None, human=None, keys=(), pause=0.3):
    """Render the dials, then click at `offset` / press `key` / type `keys`."""
    app_module = _load_app()
    qd = app_module.qd
    app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600,
                          scope=qd.Scope([qd.REPO]))
    for name in ("refresh_limits", "refresh_seats", "refresh_board",
                 "refresh_plan", "refresh_prs", "refresh_issues"):
        setattr(app, name, lambda: None)
    opened: list = []
    app.open_url = lambda url: opened.append(url)

    async with app.run_test(size=(100, 50)) as pilot:
        app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid", agent="host")
        # AFTER the app has mounted, and that is not a detail: `on_mount` builds
        # the real `HumanClient` from the resolved config, so a stub installed
        # before `run_test` is replaced by whatever credential the box running
        # the suite happens to have — which is how a test comes to pass on a
        # laptop and fail in CI, or worse, write to a real board.
        #
        # NO CREDENTIAL by default, which is every box on the fleet today: the
        # panel reads and the ✎ is the door to the page. A driver that wants the
        # write path asks for it, so "what happens with nothing configured" stays
        # the case nobody has to remember to cover.
        app.human = human if human is not None else FakeHuman(why="no session here")
        app.render_dials(DIALS)
        await pilot.pause()
        table = app.query_one("#dials")
        rows = [_cells(table, i) for i in range(table.row_count)]
        title = str(app.query_one("#t_dials").content)
        bar = str(app.query_one("#limits").content)
        if offset is not None:
            await _click_row(pilot, table, offset)
            await pilot.pause(0.3)
        if key is not None:
            await pilot.press(key)
            await pilot.pause(0.3)
        for press in keys:
            await pilot.press(press)
            await pilot.pause(0.05)
        if keys:
            await pilot.pause(pause)
        return rows, title, opened, app.detail_text, bar


def test_the_dials_panel_says_which_layer_answered_and_for_how_long():
    """Three cells and each is a distinct fact: the value, the layer it came from,
    and whether anything will ever take it off. A repo dial beats a fleet dial, so
    the overridden one is counted in the title rather than drawn as in force."""
    rows, title, _, _, _ = asyncio.run(_drive_dials())
    assert rows[0][2] == "tempo" and rows[0][3] == "held", rows[0]
    assert rows[0][4] == "quarterback", rows[0]
    # The two expiry states, side by side, not rendering alike (#244's rule).
    assert rows[0][5] == "no end", rows[0]
    assert rows[1][5] not in ("no end", ""), rows[1]
    assert "2 in force" in title and "1 overridden" in title, title


def test_the_dials_panel_always_offers_the_verb():
    """The last row is how a dial gets set, whether or not there is one above it —
    the reader who most needs it is the one who has just found out that nothing is
    in force. What that row DOES depends on the credential; that it is there does
    not."""
    rows, _, _, _, _ = asyncio.run(_drive_dials())
    assert any("set a dial" in "".join(r) for r in rows), rows


def test_with_no_session_the_pencil_is_the_door_it_always_was():
    """The read-only shape, which is every box with no cookie — and it must not be
    a modal whose save could only fail. A form that took four fields and then said
    so would have spent the person's typing to tell them something it knew before
    they started."""
    module = _load_app()
    _, _, opened, detail, _ = asyncio.run(
        _drive_dials(offset=(module.Dash.VERB_COLUMN + 2, 1)))
    assert opened and "/dials/view" in opened[0], opened
    assert "no session here" in detail, detail


def test_the_row_says_why_the_pencil_is_dead_before_it_is_pressed():
    """Asked once per paint and drawn into the row, so the refusal arrives before
    the click rather than after it."""
    rows, _, _, _, _ = asyncio.run(_drive_dials())
    assert any("no session here" in "".join(r) for r in rows), rows


def test_with_a_session_the_pencil_opens_the_editor_on_that_dial():
    """The verb this panel did not used to have. Prefilled from the row, and with
    the dial's NAME fixed: a dial is identified by its name, so an editable one
    would create a second dial rather than change the one on screen."""
    module = _load_app()

    async def go():
        app_module = _load_app()
        qd = app_module.qd
        app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600,
                              scope=qd.Scope([qd.REPO]))
        for name in ("refresh_limits", "refresh_seats", "refresh_board",
                     "refresh_plan", "refresh_prs", "refresh_issues"):
            setattr(app, name, lambda: None)
        async with app.run_test(size=(100, 50)) as pilot:
            app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                                 agent="host")
            app.human = FakeHuman()          # after mount — see `_drive_dials`
            app.render_dials(DIALS)
            await pilot.pause()
            await _click_row(pilot, app.query_one("#dials"),
                             (module.Dash.VERB_COLUMN + 2, 1))
            await pilot.pause(0.3)
            screen = app.screen
            return (type(screen).__name__,
                    screen.row.get("dial") if hasattr(screen, "row") else None,
                    [w.value for w in screen.query("Input")] if hasattr(screen, "row") else [])

    name, dial, values = asyncio.run(go())
    assert name == "DialEdit", name
    assert dial == "tempo", dial
    # The value comes back spelled the way the box would accept it again, and
    # there is no name field on an existing dial.
    assert "held" in values, values


def test_a_dial_row_explains_itself_rather_than_opening_anything():
    """The row is six narrow cells and the argument does not fit in one of them —
    the board requires a reason on every write precisely so there is one."""
    _, _, opened, detail, _ = asyncio.run(_drive_dials(offset=(60, 1)))
    assert not opened, "clicking the row itself is a read, not a door"
    assert "this repo is mid-release" in detail, detail
    assert "set indefinitely" in detail, detail


def test_d_opens_the_dials_page_from_wherever_you_are():
    """A dial governs every table on the screen, so the key is not tied to one."""
    _, _, opened, _, _ = asyncio.run(_drive_dials(key="d"))
    assert opened and "/dials/view" in opened[0], opened


def test_the_tempo_rides_the_caps_line_in_the_clickable_renderer():
    """Drawn on the limits clock, which is an hour long — so a new dials answer
    has to redraw it, or the cell up there keeps last hour's tempo while the panel
    below shows this minute's."""
    _, _, _, _, bar = asyncio.run(_drive_dials())
    assert "TEMPO" in bar and "held" in bar, bar


# ---- and the write itself (#479's credential, stubbed) ------------------------

async def _written(asked: dict, human=None, dials=None):
    """Hand `dial_written` what a modal would have returned, and see what went out.

    Driven at that seam rather than through four Input widgets because that is
    where the decisions are: parse the value, parse the expiry, refuse a blank
    reason, and only then spend a request. The keystroke path has its own test.
    """
    app_module = _load_app()
    qd = app_module.qd
    app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600,
                          scope=qd.Scope([qd.REPO]))
    for name in ("refresh_limits", "refresh_seats", "refresh_board",
                 "refresh_plan", "refresh_prs", "refresh_issues"):
        setattr(app, name, lambda: None)
    async with app.run_test(size=(100, 50)) as pilot:
        app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid", agent="host")
        app.human = human or FakeHuman()
        app.dials = dials if dials is not None else {"asked": True, "now": None}
        app.dial_written(asked)
        await pilot.pause(0.3)
        return app.human, app.detail_text


def test_a_saved_dial_goes_out_as_a_value_and_not_as_its_spelling():
    """`2` is a number, `P3` is a string, and `null` is the documented off switch
    for three dials — `qbdata.parse_dial_value`, the same table `dials.html`
    implements in the browser."""
    human, said = asyncio.run(_written(
        {"dial": "review_panel.max_rounds", "value": "2", "reason": "window at 94%",
         "expiry": "", "repo": None}))
    assert human.set == [("review_panel.max_rounds", 2, "window at 94%", None, None)]
    # WHAT IT REPLACED, said out loud: moving a dial without being told what it
    # was is how one gets nudged twice by two people who each believed they were
    # starting from the default.
    assert "it was P4" in said and "the old argument" in said, said


def test_an_empty_expiry_is_a_dial_with_no_end_rather_than_a_missing_field():
    human, _ = asyncio.run(_written(
        {"dial": "tempo", "value": "eager", "reason": "draining", "expiry": "",
         "repo": "prisonblues/quarterback"}))
    assert human.set[0][4] is None, human.set


def test_an_expiry_is_measured_from_the_boards_clock():
    """A box whose clock is an hour slow otherwise writes "in one hour" as a time
    already past, which `POST /dials` refuses at the door — in words about a field
    the person never filled in."""
    human, _ = asyncio.run(_written(
        {"dial": "tempo", "value": "eager", "reason": "draining", "expiry": "4h",
         "repo": None},
        dials={"asked": True, "now": "2026-08-26T00:00:00+00:00"}))
    assert human.set[0][4].startswith("2026-08-26T04:00:00"), human.set


def test_a_duration_nobody_can_parse_is_refused_before_anything_is_spent():
    """Named where the sentence can point at the box that was wrong, rather than
    at a 422 about a field nobody typed."""
    human, said = asyncio.run(_written(
        {"dial": "tempo", "value": "eager", "reason": "draining", "expiry": "soon",
         "repo": None}))
    assert human.set == [], "a request went out on an expiry that would be refused"
    assert "30m" in said and "4h" in said, said


def test_a_dial_with_no_argument_is_refused_here_too():
    """The board refuses one without a reason — "a dial nobody can read an argument
    for is a dial nobody can decide to remove" — and so does this, in the same
    words and without spending the request."""
    human, said = asyncio.run(_written(
        {"dial": "tempo", "value": "eager", "reason": "   ", "expiry": "", "repo": None}))
    assert human.set == []
    assert "reason" in said, said


def test_clearing_returns_the_repo_to_its_own_default():
    human, said = asyncio.run(_written(
        {"dial": "tempo", "repo": "prisonblues/quarterback", "clear": True}))
    assert human.cleared == [("tempo", "prisonblues/quarterback")]
    assert human.set == []
    assert "default takes over" in said, said


def test_a_refused_write_is_reported_and_does_not_take_the_dashboard_down():
    """The interesting refusals come from an auth proxy in front of the board, and
    a dashboard is the one program whose crash costs the reader every other panel
    on the screen as well."""
    human = FakeHuman(fail="the edge refused this session before the board saw it")
    _, said = asyncio.run(_written(
        {"dial": "tempo", "value": "eager", "reason": "draining", "expiry": "",
         "repo": None}, human=human))
    assert "edge refused" in said, said


# ---- and how loudly it is reported (#577) ------------------------------------
#
# The write above could fail for a year without anybody noticing, and did: `/dials`
# was empty fleet-wide when somebody finally went looking, because a credential
# that cannot resolve reported itself in muted grey on a line the eye reads as
# chrome — under a modal that had already dismissed as though it had worked. These
# pin the three halves of being told: before, loudly, and not instead of.


async def _writing(asked: dict, human=None, then=None, pause=0.4):
    """`_written`, but handing back the app so the STYLE and the bell can be read.

    Both matter here and neither is in the text: a sentence nobody's eye is drawn
    to is the failure this issue is about, so asserting the words alone would pass
    on the exact bug.
    """
    app_module = _load_app()
    qd = app_module.qd
    app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600,
                          scope=qd.Scope([qd.REPO]))
    for name in ("refresh_limits", "refresh_seats", "refresh_board",
                 "refresh_plan", "refresh_prs", "refresh_issues"):
        setattr(app, name, lambda: None)
    rung = []
    async with app.run_test(size=(100, 50)) as pilot:
        app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid", agent="host")
        app.human = human or FakeHuman()
        app.dials = {"asked": True, "now": None}
        app.bell = lambda: rung.append(True)
        app.dial_written(asked)
        if then is not None:
            then(app)
        await pilot.pause(pause)
        # Off the widget rather than off `detail_text`, because the STYLE is the
        # half this issue is about and the text is identical either way.
        style = repr(app.query_one("#detail").visual)
        return app, app.detail_text, style, rung


def test_a_credential_that_cannot_resolve_is_red_and_rings():
    """The one error a person cannot otherwise tell apart from success, so it gets
    the treatment `DialEdit._refuse` gives a refusal one screen up. They are the
    same event — this side could not do what was asked."""
    human = FakeHuman(fail="the human-key command failed: account is not signed in")
    _, said, style, rung = asyncio.run(_writing(
        {"dial": "review_panel.budget.tokens_per_day", "value": "400000",
         "reason": "the window", "expiry": "", "repo": None}, human=human))
    assert "not signed in" in said, said
    assert "red" in style, f"a failed write drew in {style!r}"
    assert rung, "a write that did not happen made no sound"


def test_the_failure_leads_with_the_verb_and_not_with_the_dial_name():
    """`{dial}: {exc}` spent 34 characters of
    `review_panel.budget.tokens_per_day` before the only words that mattered — on
    a line that has to survive a 78-column pane."""
    human = FakeHuman(fail="account is not signed in")
    _, said, _, _ = asyncio.run(_writing(
        {"dial": "review_panel.budget.tokens_per_day", "value": "400000",
         "reason": "the window", "expiry": "", "repo": None}, human=human))
    assert said.startswith("could not set "), said


def test_a_clear_that_failed_does_not_report_itself_as_a_set():
    """The verb is the one the announcement used, so the two lines are visibly
    about the same act."""
    human = FakeHuman(fail="account is not signed in")
    _, said, _, _ = asyncio.run(_writing(
        {"dial": "tempo", "repo": None, "clear": True}, human=human))
    assert said.startswith("could not clear tempo"), said


def test_a_write_that_lands_is_not_dressed_as_an_alarm():
    """The other half of the same rule: if everything is loud then nothing is."""
    _, said, style, rung = asyncio.run(_writing(
        {"dial": "review_panel.max_rounds", "value": "2", "reason": "window at 94%",
         "expiry": "", "repo": None}))
    assert "set review_panel.max_rounds" in said, said
    assert "red" not in style, f"a write that worked drew in {style!r}"
    assert not rung, "a successful write rang the bell"


def test_the_wait_announces_itself_before_it_can_block():
    """The hole #577 was actually reported as. Between the modal dismissing and the
    worker returning this screen said nothing, and on a host whose key command
    blocks that gap is the full 30s of the subprocess timeout — a dismissed modal
    over an unchanged pane, indistinguishable from a write that landed."""
    seen = []
    _, _, _, _ = asyncio.run(_writing(
        {"dial": "tempo", "value": "eager", "reason": "draining", "expiry": "",
         "repo": None},
        then=lambda app: seen.append(app.detail_text)))
    assert seen and "setting tempo" in seen[0], seen
    # NAMED, because the wait is almost always `op` and a person who reads the
    # word has the answer before the timeout does.
    assert "op" in seen[0] and "30s" in seen[0], seen


def test_a_second_press_is_refused_rather_than_cancelling_the_first():
    """`run_dial_write` is `exclusive=True`, so a second press cancelled the first
    — and the first was the one holding the answer, thirty seconds into a key
    command that had not returned. A person who sees nothing presses again, which
    was the one input that guaranteed they went on seeing nothing."""
    import threading
    held = threading.Event()

    class Slow(FakeHuman):
        def set_dial(self, *a, **kw):
            held.wait(timeout=5)
            return super().set_dial(*a, **kw)

    slow = Slow()
    asked = {"dial": "tempo", "value": "eager", "reason": "draining",
             "expiry": "", "repo": None}
    refused = []

    def press_again(app):
        app.dial_written(dict(asked, dial="review_panel.max_rounds", value="2"))
        refused.append(app.detail_text)
        # RELEASED HERE, while the app is still running, so the first write
        # actually completes inside `run_test` and this test proves what it says.
        # Setting the event after `asyncio.run` returned proved nothing: the
        # worker was finishing on its own timeout, after the app had gone.
        held.set()

    app, said, style, rung = asyncio.run(
        _writing(asked, human=slow, then=press_again, pause=1.5))
    # The refusal, seen at the moment of the second press rather than inferred
    # from whatever the line said once everything had settled.
    assert "still writing tempo" in refused[0], refused
    assert "review_panel.max_rounds" not in [row[0] for row in slow.set], slow.set
    # And the FIRST write was allowed to finish, which is the point of refusing
    # the second rather than superseding it.
    assert [row[0] for row in slow.set] == ["tempo"], slow.set
    assert "set tempo" in said, said
    # The flag is released, so the next press is not refused for ever.
    assert app.dial_writing is None, app.dial_writing


def test_ctrl_s_in_the_editor_is_what_sends_it():
    """The keystroke path, end to end: the ✎ opens the modal, the fields are
    typed, and ctrl+s is the only thing that spends a request.

    TWICE, because the dial on this screen is `tempo` — which both dashboards draw
    and `harness_rules.BOARD_DIALS` does not hold, so nothing this box knows applies
    it. #539 warns on the first ctrl+s and writes on the second: the vocabulary is
    the harness beside THIS dashboard, and the two are installed separately, so a
    hard refusal would make a box one release behind a box that cannot set a dial
    the rest of the fleet already applies. The first press spending nothing is the
    half this test used to assert on its own, and it still holds."""
    module = _load_app()

    async def go():
        app_module = _load_app()
        qd = app_module.qd
        app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600,
                              scope=qd.Scope([qd.REPO]))
        for name in ("refresh_limits", "refresh_seats", "refresh_board",
                     "refresh_plan", "refresh_prs", "refresh_issues"):
            setattr(app, name, lambda: None)
        human = FakeHuman()
        async with app.run_test(size=(100, 50)) as pilot:
            app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                                 agent="host")
            app.human = human
            app.dials = {"asked": True, "now": None}
            app.render_dials(DIALS)
            await pilot.pause()
            await _click_row(pilot, app.query_one("#dials"),
                             (module.Dash.VERB_COLUMN + 2, 1))
            await pilot.pause(0.3)
            # The value box holds the focus on an existing dial, so what is typed
            # replaces it only after it is cleared — `ctrl+a` is not a Textual
            # binding, so this deletes what is there the way a person would.
            for _ in range(8):
                await pilot.press("backspace")
            for ch in "eager":
                await pilot.press(ch)
            await pilot.press("tab")
            for ch in "draining":
                await pilot.press(ch)
            assert not human.set, "a keystroke wrote before ctrl+s did"
            await pilot.press("ctrl+s")
            await pilot.pause(0.4)
            assert not human.set, (
                "an unrecognised dial was written without being confirmed")
            await pilot.press("ctrl+s")
            await pilot.pause(0.4)
            return human.set

    written = asyncio.run(go())
    assert written, "ctrl+s sent nothing"
    dial, value, reason, repo, _ = written[0]
    assert (dial, value, reason) == ("tempo", "eager", "draining"), written
    # The row's own scope, kept: editing an existing dial is changing the one on
    # screen, and a write that silently moved it to the fleet would be a different
    # setting with the same name.
    assert repo == "prisonblues/quarterback", written


def test_a_new_dial_takes_its_scope_from_the_rows_on_screen_not_the_cwd():
    """The mistake a person cannot see afterwards, and the one a second opinion
    found: `repo_slug` is where work is LAUNCHED, `Scope` is what is being SHOWN.
    A pane started in one checkout with `QB_DASH_REPOS=owner/other` draws `other`'s
    dials — and used to offer to write the dial to the checkout's repo instead.
    Same dial name, different setting, and nothing on screen says which took it.
    """
    module = _load_app()
    qd = module.qd
    app = module.Dash(scope=qd.Scope(["prisonblues/other"]))
    app.repo_slug = "prisonblues/quarterback"          # the checkout it launched in
    assert app.new_dial_scope() == "prisonblues/other"


def test_a_wide_pane_writes_to_the_fleet_because_it_cannot_choose():
    module = _load_app()
    qd = module.qd
    app = module.Dash(scope=qd.Scope(["prisonblues/one", "prisonblues/two"]))
    app.repo_slug = "prisonblues/one"
    assert app.new_dial_scope() is None


def test_a_repo_known_only_by_a_bare_name_is_not_offered_as_a_scope():
    """`owner/name` is the board's shape for a repo scope; a bare `quarterback` is
    refused there. Fleet is the honest answer, and the modal says so in bold."""
    module = _load_app()
    qd = module.qd
    app = module.Dash(scope=qd.Scope(["quarterback"]))
    app.repo_slug = None
    assert app.new_dial_scope() is None


def test_a_duration_that_would_overflow_is_answered_not_raised():
    """`timedelta` raises OverflowError rather than ValueError past its range, and
    an escape here is a crash inside a Textual callback — which takes the whole
    dashboard, not just this panel."""
    human, said = asyncio.run(_written(
        {"dial": "tempo", "value": "eager", "reason": "draining",
         "expiry": "99999999999999999999d", "repo": None}))
    assert human.set == []
    assert "not a duration" in said, said


# ---- a panel that cannot see says so -----------------------------------------
#
# `tmux_seats()` used to answer `[]` whether the screen had no seats or tmux could
# not be run at all, and the dashboard reported the first while the second was
# true: "no seat screen on this server" beside a screen with three seats in it,
# and a ＋ that declined to add one. These pin the two places the difference has
# to reach a reader — the title of the panel whose rows are missing, and the verbs
# that refuse.


def test_a_tmux_we_cannot_reach_rides_the_agents_title(monkeypatch):
    """On the AGENTS title, not a status line: the failure IS that this panel
    looked complete when it was not, and a line that scrolls away does not fix a
    panel that lies while you are reading it."""
    app = _dash()
    said: list[str] = []

    class Table:
        def clear(self, *a, **k): pass

        def add_row(self, *cells, key=None, **k):
            return SimpleNamespace(value=key)

    app.query_one = lambda sel, *a, **k: (
        Table() if sel == "#agents" else _Sink(said) if sel == "#t_agents"
        else _Sink())
    app.render_seats([], "tmux exited 127")
    assert any("tmux: tmux exited 127" in s for s in said), said


def test_a_screen_that_really_has_no_seats_says_nothing_about_tmux():
    """The other half, and the one that decides whether the first is noise: no
    error means the empty list is the truth, and a title that complained anyway
    would fire on every dashboard run in a bare terminal."""
    app = _dash()
    said: list[str] = []

    class Table:
        def clear(self, *a, **k): pass

        def add_row(self, *cells, key=None, **k):
            return SimpleNamespace(value=key)

    app.query_one = lambda sel, *a, **k: (
        Table() if sel == "#agents" else _Sink(said) if sel == "#t_agents"
        else _Sink())
    app.render_seats([], None)
    assert not any("tmux:" in s for s in said), said


def test_the_plus_refuses_by_naming_the_machine_and_not_the_screen():
    """It advised starting a screen you were already sitting in. The remedy for a
    broken tmux is not `qb-seats`, and sending a reader there is the whole cost of
    the two states having looked alike."""
    app = _dash()
    said: list[str] = []
    app.say = said.append
    app.seats_error = "tmux is not on PATH"
    app.add_seat()
    assert said and "blind, not empty" in said[0], said
    assert "start one with qb-seats" not in said[0], said


def test_expanding_refuses_the_same_way():
    """`z` reads the same seat list, so it inherited the same wrong sentence."""
    app = _dash()
    said: list[str] = []
    app.say = said.append
    app.seats_error = "tmux could not be run (OSError)"
    app.action_expand()
    assert said and "blind, not empty" in said[0], said


# ---- the chip bar ------------------------------------------------------------
#
# Sixteen agents across three repos is a list you read rather than one you scan,
# and the dashboard had no way to narrow it: `s` is binary — this screen's repos
# or every repo — and `--repo` is a command-line flag you cannot reach from a
# running dashboard.


def _bar(app, board, seats=None):
    """Drive `render_agents` and read back the bar, the title and whether it hid.

    The chips are read off the stub's `add_row` rather than off a widget, for the
    reason `_seat_labels` reads labels that way: which repos it offers and which
    one is lit are decisions, and driving a pilot to find them out would test
    Textual's layout instead.
    """
    chips: list[list[str]] = []
    hidden: list[bool] = []
    said: list[str] = []

    class Bar:
        #: Zero, so the signature guard in `render_chips` reads this stub as a bar
        #: that has not been drawn yet and rebuilds it — which is what every test
        #: through this helper wants to observe.
        row_count = 0

        def set_class(self, on, *names): hidden.append(bool(on))
        def clear(self, **k): pass
        def add_column(self, label, key=None, **k): return key

        def add_row(self, *cells, key=None, **k):
            chips.append([str(getattr(c, "plain", c)).strip() for c in cells])
            return SimpleNamespace(value=key)

    class Rows(Bar):
        def add_row(self, *cells, key=None, **k):
            return SimpleNamespace(value=key)

    app.board = board
    app.seats = seats or []
    app.query_one = lambda sel, *a, **k: (
        Bar() if sel == "#chips" else Rows() if sel == "#agents"
        else _Sink(said) if sel == "#t_agents" else _Sink())
    app.render_agents()
    return (chips[0] if chips else []), (hidden[0] if hidden else False), said


def _wide(app):
    """Fleet-wide scope, and `say` sent nowhere.

    `Scope.on` is TRUE when the scope is NARROWED to this screen's repos — the
    reading that cost the first cut of these tests an hour — so the fleet-wide
    one is the toggle of a scope that is on. Several repos is the only state a
    chip bar has anything to do in, and the narrow scope is what hides them.

    `say` writes to the running app's screen, and there is no screen here.
    """
    if app.scope.on:
        app.scope = app.scope.toggled()
    app.say = lambda *a, **k: None
    return app


def _fleet(*repos):
    """A board answer with one live agent per named repo."""
    return {"agents": [{"holder": f"zeus/a{i}", "session": f"s{i}", "state": "working",
                        "repo": r, "reported": None}
                       for i, r in enumerate(repos)], "claims": []}


def test_the_bar_offers_a_chip_for_each_repo_the_fleet_is_in():
    app = _wide(_dash())
    chips, hid, _ = _bar(app, _fleet("quarterback", "lexray", "quarterback"))
    assert not hid
    assert chips == ["lexray", "quarterback"]


def test_one_repo_is_not_a_choice_so_the_bar_hides():
    """A line of a 78-column pane is worth more than a control that cannot change
    what you are looking at."""
    app = _wide(_dash())
    _, hid, _ = _bar(app, _fleet("quarterback", "quarterback"))
    assert hid, "the bar drew a single chip"


def test_a_chip_filters_and_the_title_keeps_the_unfiltered_count():
    """`3 of 16 · lexray` is a fact about what you are looking at. `3 live` alone,
    on a screen whose bar has scrolled out of a short pane, is a fact about the
    fleet — and a wrong one."""
    app = _wide(_dash())
    _bar(app, _fleet("quarterback", "lexray", "quarterback"))
    app.filter_repo("lexray")
    _, _, said = _bar(app, _fleet("quarterback", "lexray", "quarterback"))
    assert any("1 of 3" in s and "lexray" in s for s in said), said


def test_the_same_chip_is_the_on_and_the_off_switch():
    """A separate `clear` chip is one more thing to find, and in a pane narrow
    enough to clip the bar it is the one that gets clipped."""
    app = _wide(_dash())
    app.render_agents = lambda: None
    app.filter_repo("lexray")
    assert app.repo_filter == "lexray"
    app.filter_repo("lexray")
    assert app.repo_filter is None


def test_the_bar_keeps_every_chip_while_one_of_them_is_filtered_to():
    """Built from the rows left AFTER a filter, it would lose every chip but the
    active one the moment you used it — a filter you cannot leave."""
    app = _wide(_dash())
    app.repo_filter = "lexray"
    chips, hid, _ = _bar(app, _fleet("quarterback", "lexray", "quarterback"))
    assert not hid
    assert chips == ["lexray", "quarterback"], chips


def test_a_filter_whose_repo_goes_quiet_is_dropped_rather_than_stranding_you():
    """The last agent in `lexray` exits, so its chip stops being drawn — and a
    filter that survived that is an empty table with no visible control to clear
    it."""
    app = _wide(_dash())
    app.repo_filter = "lexray"
    _bar(app, _fleet("quarterback", "selfhost"))
    assert app.repo_filter is None


def _columned(app, *names):
    """A `#chips` widget carrying columns keyed by repo, as `render_chips` builds
    them. The keys are the subject: a click resolves through them."""
    cols = [SimpleNamespace(key=SimpleNamespace(value=n)) for n in names]
    app.render_agents = lambda: None
    app.query_one = lambda sel, *a, **k: (
        SimpleNamespace(ordered_columns=cols) if sel == "#chips" else _Sink())
    return app


def test_a_click_on_the_bar_reaches_the_chip_under_the_pointer():
    """The bar is one row, so the COLUMN is the whole of which chip was hit — and
    a chip has no record in `self.rows`, which everything below this reaches for."""
    app = _columned(_wide(_dash()), "lexray", "quarterback")
    app.dispatch_row("chips", 1)
    assert app.repo_filter == "quarterback"
    app.dispatch_row("chips", 0)
    assert app.repo_filter == "lexray"


def test_a_click_past_the_last_chip_does_nothing():
    """A stale column index — the bar rebuilt between the render and the click —
    must not index into the columns it no longer matches."""
    app = _columned(_wide(_dash()), "lexray")
    app.dispatch_row("chips", 7)
    assert app.repo_filter is None


def test_the_chip_a_click_lands_on_is_read_off_the_widget_not_off_self_chips():
    """Two lists that have to stay in step is two lists that come apart, and this
    pair already did: the bar hides and empties `self.chips` while the widget
    keeps its columns until the next rebuild. The column KEY is the repo, so what
    a click resolves to is what that column is."""
    app = _columned(_wide(_dash()), "lexray", "quarterback")
    app.chips = []                      # out of step with the widget, as it gets
    app.dispatch_row("chips", 1)
    assert app.repo_filter == "quarterback"


def test_a_tmux_that_failed_does_not_send_a_pane_to_a_window_instead(monkeypatch):
    """#675's distinction, in the caller that still discarded it. Falling through
    to `run_in_window` decides the topology from a query that failed — and then
    runs the same broken tmux to make the window, failing again with a second and
    less useful message."""
    app = _dash()
    said: list[str] = []
    windowed: list[str] = []
    app.say = said.append
    app.run_in_window = lambda name, command: windowed.append(name)
    app_module = _load_app()
    real = app_module.qd.tmux_seats
    monkeypatch.setenv("TMUX", "/tmp/whatever,1,0")
    try:
        app_module.qd.tmux_seats = lambda: ([], "tmux exited 127")
        app.run_in_pane("panel-42", "claude -- '/panel-review-pr 42'")
    finally:
        app_module.qd.tmux_seats = real
    assert not windowed, "a failed query was read as a screen with no seats"
    assert said and "cannot reach tmux" in said[0], said


def test_the_bar_hiding_takes_the_filter_with_it():
    """The stranding case the obvious one hides.

    Filter to `lexray` while three repos are live, then watch every OTHER repo go
    quiet. `lexray` is still on the bar, so the "its repo went quiet" rule does
    not fire — and the bar hides anyway, because one chip is not a choice. Filter
    set, no chip drawn, nothing to click.
    """
    app = _wide(_dash())
    app.repo_filter = "lexray"
    _, hid, _ = _bar(app, _fleet("lexray", "lexray"))
    assert hid, "the bar drew a single chip"
    assert app.repo_filter is None, "a hidden bar left its filter on"


def test_the_bar_is_not_rebuilt_when_it_would_look_the_same():
    """`render_agents` runs on the board's timer. An unguarded rebuild is
    `clear(columns=True)` every four seconds — throwing away the row a click is
    being dispatched against, and the cursor and hover with it."""
    app = _wide(_dash())
    builds: list[int] = []

    class Bar:
        row_count = 1
        def set_class(self, *a, **k): pass
        def clear(self, **k): builds.append(1)
        def add_column(self, label, key=None, **k): return key
        def add_row(self, *cells, key=None, **k): return SimpleNamespace(value=key)

    class Rows(Bar):
        row_count = 0
        def clear(self, **k): pass

    app.board = _fleet("quarterback", "lexray")
    app.seats = []
    app.query_one = lambda sel, *a, **k: (
        Bar() if sel == "#chips" else Rows() if sel == "#agents" else _Sink())
    app.render_agents()
    app.render_agents()
    app.render_agents()
    assert builds == [1], f"the bar was rebuilt {len(builds)} times for one state"


def test_a_changed_filter_does_redraw_the_bar():
    """The active chip is drawn differently from the others, so the filter is part
    of what the bar looks like — a signature of the names alone would leave the
    lit chip on the repo you just stopped filtering to."""
    app = _wide(_dash())
    builds: list[int] = []

    class Bar:
        row_count = 1
        def set_class(self, *a, **k): pass
        def clear(self, **k): builds.append(1)
        def add_column(self, label, key=None, **k): return key
        def add_row(self, *cells, key=None, **k): return SimpleNamespace(value=key)

    class Rows(Bar):
        row_count = 0
        def clear(self, **k): pass

    app.board = _fleet("quarterback", "lexray")
    app.seats = []
    app.query_one = lambda sel, *a, **k: (
        Bar() if sel == "#chips" else Rows() if sel == "#agents" else _Sink())
    app.render_agents()
    app.repo_filter = "lexray"
    app.render_agents()
    assert len(builds) == 2, builds


# ---- the chip bar, against the real widget -----------------------------------
#
# Every test above stubs the table, and a second opinion put the cost plainly:
# they all pass while the real widget resets its scroll on every poll, throws
# during column reconstruction, or hands a click metadata from a rendering that
# is gone. That was not hypothetical — switching the columns to keyed ones broke
# a seat test the stubs could not see, because only those route `#chips` through
# `_Sink`. These drive a pilot instead, and they are the ones that would catch it.


async def _chip_pilot(repos, click: int | None = None):
    """The dashboard with a fixture fleet, and optionally a real click on a chip.

    THE FIXTURE IS RE-ASSERTED after the pause. The workers started at mount reach
    the live board, and one landing here replaces the fleet under the assertion —
    which is what made a hand-run of this print three chips once and two the next
    time. Stubbing them out before `run_test` is not enough; they are already in
    flight by the time the context manager yields.
    """
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600)
    async with app.run_test(size=(100, 30)) as pilot:
        for name in ("refresh_board", "refresh_plan", "refresh_prs",
                     "refresh_issues", "refresh_seats", "refresh_limits"):
            setattr(app, name, lambda: None)
        # AND the render the workers call back into. Stubbing the fetchers is not
        # enough: one started at mount is already in flight by the time this line
        # runs, and it lands as `render_board(live_data)` — which replaces the
        # fixture fleet and redraws the bar from whatever repos the real board is
        # busy with. This test passed alone and failed in the file for exactly
        # that reason, which is the shape of a race rather than of a defect.
        app.render_board = lambda *a, **k: None
        if app.scope.on:
            app.scope = app.scope.toggled()
            app.build_columns()
        app.board = _fleet(*repos)
        app.render_agents()
        await pilot.pause(0.2)
        app.board = _fleet(*repos)
        app.render_agents()
        await pilot.pause(0.1)
        bar = app.query_one("#chips")
        keys = [str(c.key.value) for c in bar.ordered_columns]
        if click is not None:
            await _click_row_index(pilot, bar, "chips", column=click)
            await pilot.pause(0.2)
        return keys, bar.row_count, bar.has_class("empty"), app.repo_filter


def test_the_real_bar_draws_a_column_per_repo_keyed_by_its_name():
    """`add_column(name, key=name)` against the actual DataTable, which the stubs
    cannot check: they accept any signature and return whatever they like."""
    keys, rows, hidden, _ = asyncio.run(_chip_pilot(
        ["quarterback", "lexray", "prisonblues/quarterback", "selfhost"]))
    assert not hidden
    assert rows == 1
    assert keys == ["lexray", "quarterback", "selfhost"], keys


def test_a_real_click_on_a_real_chip_filters_to_it():
    """Through `ClickTable.on_click` and the cell metadata, rather than by calling
    `dispatch_row` with a number a test chose. The click has to land on the chip
    the compositor actually drew."""
    _, _, _, filtered = asyncio.run(_chip_pilot(
        ["quarterback", "lexray", "selfhost"], click=2))
    assert filtered == "selfhost", filtered


def test_the_real_bar_hides_itself_when_the_fleet_is_one_repo():
    _, _, hidden, _ = asyncio.run(_chip_pilot(["quarterback", "quarterback"]))
    assert hidden


# ---- reading a row here instead of in a browser (#250) ----------------------
#
# NOTHING BELOW RUNS `gh-dash`, and the reason is a trap rather than a preference:
# its detail view nil-derefs in `markdown.GetMarkdownRenderer` whenever the
# terminal is not real — under tmux with no client attached, under `script -qec`,
# and under a bare `pty.fork()` — so the whole program dies with `Caught panic:
# invalid memory address`. A test that asserted on the sidebar by running it would
# be red on every machine with nobody watching, which is all of them in CI. What
# quarterback controls is the generated config and the `tmux split-window` it
# invokes, so those are what these assert on. The sidebar itself was checked by
# hand in a real pane of `seats-qb-dev`.

#: One issue-backed plan item, and one line of plan that references nothing. The
#: pair is the whole claim about the ▥: it is live where there is something to
#: open and grey where there is not.
READ_PLAN = {
    "items": [
        {"item_id": "a", "repo": "prisonblues/quarterback", "state": "open",
         "title": "git and the board should work without a forge", "rank": 1,
         "rank_source": "ordered", "ref": {"kind": "issue", "value": "327"},
         "blocked_by": [], "claim": None},
        {"item_id": "b", "repo": "prisonblues/quarterback", "state": "open",
         "title": "a line of plan that is not an issue", "rank": 2,
         "rank_source": "ordered", "ref": None, "blocked_by": [], "claim": None},
    ],
    "counts": {"open": 2}, "order_trust": {}, "next": None, "truncated": False,
}


async def _drive_read(click=None, key=None, installed=True, home=None, real_click=False):
    """Render two plan rows, then reach the ▥ by mouse or by key.

    `installed` answers the `PATH` scan for `gh-dash` — the dashboard asks it per
    render, so a box without one is a rendering difference and not only a refusal.
    """
    app_module, app = _quiet_dash()
    panes: list[tuple[str, str]] = []
    app.run_in_pane = lambda name, command: panes.append((name, command))
    opened: list[str] = []
    app.open_url = lambda url: opened.append(url)

    real_shutil = app_module.shutil
    # The module's REFERENCE is swapped rather than the stdlib function patched, so
    # nothing here can leak into a later test that expects a real `PATH` scan.
    app_module.shutil = SimpleNamespace(
        which=lambda name: f"/usr/bin/{name}" if installed else None)
    if home is not None:
        app_module.os.environ["XDG_RUNTIME_DIR"] = str(home)
    try:
        async with app.run_test(size=(110, 46)) as pilot:
            app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                                 agent="host")
            app.plan_sig = None
            app.render_plan(READ_PLAN, None)
            await pilot.pause(0.2)
            table = app.query_one("#work")
            await _settle_table(pilot, table)
            rows = [_cells(table, i) for i in range(table.row_count)]
            styles = [str(getattr(table.get_row_at(i)[app.READ_COLUMN], "style", ""))
                      for i in range(table.row_count)]
            if click is not None:
                if real_click:
                    # THROUGH THE COMPOSITOR, because "a button" is a claim about
                    # what the mouse can reach and `dispatch_row` cannot make it:
                    # a cell nothing renders would still dispatch perfectly.
                    await _click_row_index(pilot, table, click, scroll=True,
                                           column=app.READ_COLUMN)
                else:
                    app.dispatch_row(click, column=app.READ_COLUMN)
                await pilot.pause(0.2)
            if key is not None:
                table.move_cursor(row=0, animate=False)
                await pilot.pause(0.1)
                await pilot.press(key)
                await pilot.pause(0.2)
            return rows, styles, panes, opened, app.detail_text
    finally:
        app_module.shutil = real_shutil
        if home is not None:
            app_module.os.environ.pop("XDG_RUNTIME_DIR", None)


def test_the_read_icon_sits_beside_the_verb_and_greys_with_nothing_to_open():
    """A column of its own, next to the verb rather than past the title.

    The issue weighed this against taking the row click over, and a column is what
    it cost: a fifth glyph to learn, and a character of width in a pane that is
    sometimes 45 wide. Beside the verb because that is where the habit already
    points — "the action icons are at the front of the row" stays one thing to
    learn rather than becoming two.
    """
    rows, styles, panes, _, _ = asyncio.run(_drive_read())
    assert [r[_load_app().Dash.READ_COLUMN] for r in rows] == ["▥", "▥"], rows
    # Live on the issue-backed item, grey on the line of plan that references
    # nothing — the same "grey means not offered" the ⚒ and the ⚖ keep.
    assert "green" in styles[0], styles
    assert "grey30" in styles[1], styles
    assert not panes, "rendering the icon started something"


def test_the_read_icon_opens_gh_dash_in_the_seat_row_pinned_to_that_row(tmp_path):
    """The pane, the config, and the one row in it.

    `run_in_pane` is the same call the ⚖ makes and is reused rather than copied —
    it splits the SEAT ROW, marks the pane `@qb_label` and not `@qb_seat`, and
    re-equalises with `select-layout -E`, all of which has its own test above. What
    is new here is what gets run in it.
    """
    rows, _, panes, opened, detail = asyncio.run(
        _drive_read(click="plan:a", home=tmp_path, real_click=True))
    assert len(panes) == 1, panes
    name, command = panes[0]
    assert name == "issue-327", name
    assert command.startswith("gh-dash --config "), command
    assert not opened, "the button that exists to stay in the terminal opened a browser"

    path = command.split("--config ", 1)[1].strip("'")
    assert path.startswith(str(tmp_path)), path
    text = Path(path).read_text()
    assert "prSections: []" in text
    assert 'in:title "git and the board should work without a forge"' in text
    # The sidebar is the whole reason this beats the table row already on screen.
    assert "open: true" in text


def test_a_second_click_on_the_same_row_rewrites_one_config(tmp_path):
    """Named for the ITEM, so a morning of clicking one PR leaves one file. `/tmp`
    is the fallback when `$XDG_RUNTIME_DIR` is unset and is not cleared at logout,
    so the accumulating version would be permanent on exactly the machines that
    can least afford it."""
    asyncio.run(_drive_read(click="plan:a", home=tmp_path))
    asyncio.run(_drive_read(click="plan:a", home=tmp_path))
    assert [p.name for p in (tmp_path / "qb-dash").iterdir()] == \
        ["issue-prisonblues-quarterback-327.yml"]


def test_with_no_gh_dash_installed_the_column_greys_and_the_click_says_why():
    """Degrade, do not guard — and SAY it, which is the one place this table breaks
    its own falls-through-and-explains rule.

    Everywhere else a dim icon means "no verb here" and the row's own explanation
    is the better answer. Here one of the two reasons is a missing binary, a fact
    about the machine that nothing else on this dashboard reports, so a click that
    quietly explained the row would leave a reader pressing a button they have no
    way to discover is unbuilt.
    """
    rows, styles, panes, opened, detail = asyncio.run(
        _drive_read(click="plan:a", installed=False))
    assert [r[_load_app().Dash.READ_COLUMN] for r in rows] == ["▥", "▥"], rows
    assert all("grey30" in s for s in styles), styles
    assert not panes and not opened
    assert "gh-dash is not installed" in detail, detail
    # …and where to go instead, because the browser has not gone anywhere.
    assert "`o`" in detail, detail


def test_a_row_with_nothing_to_open_says_that_rather_than_doing_nothing():
    """A dim icon that swallows the click is indistinguishable from a broken one —
    the rule `fix_plan_item` already keeps for the ⚒."""
    _, _, panes, _, detail = asyncio.run(_drive_read(click="plan:b"))
    assert not panes
    assert "nothing to open here" in detail, detail
    assert "a line of plan that is not an issue" in detail, detail


def test_the_row_click_and_o_are_unchanged_by_the_new_button():
    """The issue settled this: a new icon, and the row click keeps what it did.

    `o` and the rest of the row are what a reader already has in their fingers,
    and a button that quietly redefined them would be a second change nobody asked
    for — the reason the alternative (row click → gh-dash, browser demoted to `o`)
    was not taken.
    """
    async def drive():
        app_module, app = _quiet_dash()
        opened, panes = [], []
        app.open_url = lambda url: opened.append(url)
        app.run_in_pane = lambda name, command: panes.append(name)
        async with app.run_test(size=(110, 46)) as pilot:
            app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                                 agent="host")
            app.plan_sig = None
            app.render_plan(READ_PLAN, None)
            await pilot.pause(0.2)
            table = app.query_one("#work")
            await _settle_table(pilot, table)
            # Anywhere but the two icon columns: the row explains itself.
            app.dispatch_row("plan:a", column=99)
            explained = app.detail_text
            table.move_cursor(row=0, animate=False)
            await pilot.pause(0.1)
            await pilot.press("o")
            await pilot.pause(0.2)
            return explained, opened, panes

    explained, opened, panes = asyncio.run(drive())
    assert "rank 1" in explained, explained
    assert opened == ["https://github.com/prisonblues/quarterback/issues/327"], opened
    assert not panes, "`o` stopped opening a browser"


def test_v_does_from_the_keyboard_what_the_icon_does_from_the_mouse():
    """Every other verb on this table has a key — `o`, `p`, `f` — and a button
    reachable only by mouse would be the one that does not."""
    _, _, panes, _, _ = asyncio.run(_drive_read(key="v"))
    assert [n for n, _ in panes] == ["issue-327"], panes


def test_both_lines_that_enumerate_the_clicks_name_the_new_one():
    """The hint under the tables and `?` are the only two places a reader is told
    what an icon does, and a fifth glyph that appears in neither is a glyph nobody
    finds. They are asserted together because they have drifted apart before.
    """
    async def drive():
        app_module, app = _quiet_dash()
        async with app.run_test(size=(110, 46)) as pilot:
            hint = _text(app.query_one("#detail"))
            app.action_help()
            await pilot.pause(0.1)
            return hint, app.detail_text

    hint, keys = asyncio.run(drive())
    assert "▥" in hint, hint
    assert "▥" in keys, keys
    assert "v " in keys and "read it here" in keys, keys
