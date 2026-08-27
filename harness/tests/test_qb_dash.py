"""Does a click on a dashboard row actually do the thing?

Drives the real app through Textual's pilot — real widgets, synthetic clicks —
with the browser and tmux calls stubbed, because a test that opened Chrome and
moved the cursor of a live seat screen would be its own bug.

SKIPPED unless the machine can actually run the dashboard: textual and the board
client come from mcp/'s environment, and the fetches want a configured board. In
CI today that means this skips; it is written to run where the thing itself runs.
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
import sys
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
        import rich, textual  # noqa: F401
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
needs_live_data = pytest.mark.skipif(_NO_BOARD is not None, reason=_NO_BOARD or "")


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


async def _click_row(pilot, table, offset) -> None:
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
    3s and it clicks anyway when that runs out — a table that genuinely never
    drew should fail on the assertion that names it, not time out in here — and
    it costs 0.1s when nothing is moving, which is the normal case.
    """
    previous, still = None, 0
    for _ in range(60):
        region = table.region
        x, y = region.offset.x + offset[0], region.offset.y + offset[1]
        on_a_row = table.screen.get_style_at(x, y).meta.get("row", -1) >= 0
        still = still + 1 if on_a_row and region == previous else 0
        if still >= 2:
            break
        previous = region
        await pilot.pause(0.05)
    else:
        # It clicks anyway — but a click that never found a settled row is not the
        # same event as one that did, and silence makes them the same in the log.
        # Reachable on an ordinary day: a fixed y=2 offset addresses no row on a
        # repo with one open PR, and the driver then reports whatever the empty
        # click did or did not do, under the name of the thing it meant to test.
        print(f"_click_row: {table.id} never settled with a row at {offset} "
              f"— clicking anyway, region {table.region}")
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
    jumped: list[int] = []
    app.open_pr = lambda pr: (opened.append(pr.get("number")),
                              app.say(f"opened #{pr.get('number')}"))[1]
    # `scope` too, and not as decoration: `jump_to_seat` grew a second parameter
    # when seat identity became per-project (#208), and this stub did not — so
    # every run of this test died on a TypeError from inside the lambda rather
    # than on anything it asserts. A stub of a real method has to keep that
    # method's signature or it stops standing in for it.
    app.jump_to_seat = lambda seat, scope=None: (jumped.append(seat), True)[1]

    failures: list[str] = []
    async with app.run_test(size=(80, 44)) as pilot:
        for _ in range(40):                        # the first fetch is a network call
            await pilot.pause(0.25)
            if app.query_one("#prs").row_count and app.query_one("#fleet").row_count:
                break

        prs = app.query_one("#prs")
        fleet = app.query_one("#fleet")
        claims = app.query_one("#claims")
        _need_rows(prs, "PRs", app.pr_err)

        # ONE click, on a row that is not the cursor's, is the whole point.
        # x=30 is the title column: the first columns are the CI glyph and the
        # ⚖, which mean something else and are covered by the test below.
        await _click_row(pilot, prs, (30, 1))
        await pilot.pause(0.2)
        if not opened:
            failures.append("a click on a PR row did not open it")

        await _click_row(pilot, fleet, (4, 1))
        await pilot.pause(0.2)
        if not app.detail_text or app.detail_text.startswith("click a row"):
            failures.append("a click on an agent row changed nothing")

        if claims.row_count:
            await _click_row(pilot, claims, (4, 1))
            await pilot.pause(0.2)

        opened.clear()                             # and the keyboard path still works
        prs.focus()
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

    failures: list[str] = []
    async with app.run_test(size=(90, 50)) as pilot:
        for _ in range(40):
            await pilot.pause(0.25)
            if (app.held is not None and app.issues is not None
                    and app.query_one("#issues").row_count):
                break
        issues = app.query_one("#issues")
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
        top = int(_numbered_cell(issues.get_row_at(0)))

        # The ⚒ column asks first, the same as the ⚖ does.
        await _click_row(pilot, issues, (app_module.Dash.FIX_COLUMN + 2, 1))
        await pilot.pause(0.3)
        if started:
            failures.append("the icon started a fix with no confirmation")
        if not isinstance(app.screen, app_module.Confirm):
            failures.append("the icon did not raise the confirmation")
        else:
            await pilot.press("enter")
            await pilot.pause(0.3)
            if not started:
                failures.append("confirming did not start the fix")
            elif ["/fix-issue", str(top)] != list(started[0][1][1:3]):
                failures.append(f"wrong command launched: {started[0][1]}")
            elif started[0][0] != f"fix-issue-{top}":
                failures.append(f"wrong window name: {started[0][0]}")

        # A click anywhere else on the row still means "open it on GitHub".
        started.clear()
        await _click_row(pilot, issues, (30, 1))
        await pilot.pause(0.3)
        if opened != [top]:
            failures.append(f"clicking the title opened {opened}, expected [{top}]")
        if started:
            failures.append("clicking the title started a fix")

        # And the keyboard route to the same verb.
        issues.focus()
        await pilot.press("f")
        await pilot.pause(0.3)
        if not isinstance(app.screen, app_module.Confirm):
            failures.append("'f' did not raise the confirmation")
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
            if app.query_one("#plan").row_count:
                break
        plan = app.query_one("#plan")
        _need_rows(plan, "plan items", app.plan_err)

        # Anywhere but the ⚒: the detail line, and it must name the row clicked.
        await _click_row(pilot, plan, (40, 1))
        await pilot.pause(0.3)
        # BY THE SCOPE, not by a fixed index: the repo cell comes and goes with
        # `scope.column` (#261), so column 4 is the title on a wide pane and the
        # holder on a narrow one — and a narrow pane is now the default. The `? `
        # mark on an unattributable row is part of the cell but not of the title.
        title_at = 5 if app.scope.column else 4
        title = str(plan.get_row_at(plan.scroll_offset.y)[title_at]).removeprefix("? ") \
            .rstrip("…")
        if title and title not in app.detail_text:
            failures.append(f"the detail line does not describe the row clicked: "
                            f"{app.detail_text[:80]!r}")

        # The ⚒, on a row that actually has an issue behind it. Which row that is
        # depends on today's plan, so it is found rather than assumed — and what
        # it should do is read off the row the table actually scrolled to, not
        # off the index asked for: scrolling near the end of a list stops short.
        import qbdata as qd
        # The board's order, narrowed the way the panel narrows it — the panel no
        # longer re-derives an order of its own, so neither may this.
        ordered, _ = qd.in_scope(qd.plan_items(app.plan), app.scope)
        wanted = next((n for n, i in enumerate(ordered)
                       if qd.plan_issue(i) and not i.get("claim")), None)
        if wanted is None:
            pytest.skip("no free issue-backed item on the plan today — nothing to take")
        plan.scroll_to(y=wanted, animate=False)
        await pilot.pause(0.3)
        landed = ordered[plan.scroll_offset.y]

        await _click_row(pilot, plan, (app_module.Dash.FIX_COLUMN + 2, 1))
        await pilot.pause(0.3)
        issue = qd.plan_issue(landed)
        if started:
            failures.append("the icon started a fix with no confirmation")
        elif issue is None or landed.get("claim"):
            # A line of plan with no issue, or somebody's current work: the icon
            # has to SAY so. Doing nothing is indistinguishable from being broken.
            if not app.detail_text:
                failures.append("the icon on an unfixable row said nothing")
        elif not isinstance(app.screen, app_module.Confirm):
            failures.append("the icon did not raise the confirmation")
        else:
            await pilot.press("enter")
            await pilot.pause(0.3)
            if not started:
                failures.append("confirming did not start the fix")
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
    app.open_pr = lambda pr: opened.append(pr.get("number"))

    failures: list[str] = []
    async with app.run_test(size=(90, 44)) as pilot:
        for _ in range(40):
            await pilot.pause(0.25)
            if app.query_one("#prs").row_count:
                break
        prs = app.query_one("#prs")
        _need_rows(prs, "PRs", app.pr_err)

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
        await _click_row(pilot, prs, (app_module.Dash.PANEL_COLUMN + 2, 1))
        await pilot.pause(0.3)
        if started:
            failures.append("the icon started a review with no confirmation")
        if not isinstance(app.screen, app_module.Confirm):
            failures.append("the icon did not raise the confirmation")
        else:
            await pilot.press("enter")               # …and confirming starts it
            await pilot.pause(0.3)
            if not started:
                failures.append("confirming did not start the review")
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
        cancel_on = 2 if prs.row_count >= 2 else 1
        await _click_row(pilot, prs, (app_module.Dash.PANEL_COLUMN + 2, cancel_on))
        await pilot.pause(0.3)
        if not isinstance(app.screen, app_module.Confirm):
            failures.append(f"the ⚖ on row {cancel_on} raised no confirmation to cancel")
        else:
            await pilot.press("escape")
            await pilot.pause(0.3)
            if started:
                failures.append("cancelling still started a review")

        # A click anywhere else on the row still means "open on GitHub".
        await _click_row(pilot, prs, (30, 1))
        await pilot.pause(0.3)
        if not opened:
            failures.append("clicking the title did not open the PR")
        if started:
            failures.append("clicking the title started a review")

        # And the keyboard route to the same verb.
        prs.focus()
        await pilot.press("p")
        await pilot.pause(0.3)
        if not isinstance(app.screen, app_module.Confirm):
            failures.append("'p' did not raise the confirmation")
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
        seats = app.query_one("#seats")

        # Two seats plus the ＋ row. The ＋ has to be a row, not a key, or the
        # panel cannot be driven by the mouse alone.
        if seats.row_count != len(fake) + 1:
            failures.append(f"expected {len(fake) + 1} rows, got {seats.row_count}")

        # The ✕ column asks first and closes nothing by itself.
        await pilot.click(seats, offset=(app_module.Dash.KILL_COLUMN + 2, 1))
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
        await pilot.click(seats, offset=(app_module.Dash.KILL_COLUMN + 2, 2))
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.3)
        if clicked:
            failures.append("cancelling still closed a seat")

        # The ＋ row adds one, to the session the SEATS came from.
        await pilot.click(seats, offset=(4, len(fake) + 1))
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
        await pilot.click(seats, offset=(20, 1))
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
        app.refresh_limits = lambda: None
        app.refresh_seats = lambda: None
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
            seats = app.query_one("#seats")
            assert seats.row_count == len(full) + 1, seats.row_count
            await pilot.click(seats, offset=(4, len(full) + 1))
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
            for pid in ("p_dials", "p_seats", "p_fleet", "p_claims", "p_plan",
                        "p_prs", "p_queue", "p_issues")}


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

    Every panel but SEATS — which is its content in both layouts, and spans both
    columns for that reason — has to come out TALLER wide than narrow. If it
    does not, the grid is drawing two columns and the rows are still being cut
    into sevenths, which looks like a success and fixes nothing.
    """
    narrow = asyncio.run(_laid_out(90))["panels"]
    wide = asyncio.run(_laid_out(200))["panels"]
    shorter = {pid: (narrow[pid].height, wide[pid].height)
               for pid in narrow if pid not in ("p_seats", "p_dials")
               and wide[pid].height <= narrow[pid].height}
    assert not shorter, f"no taller in two columns (narrow, wide): {shorter}"
    assert wide["p_seats"].width > narrow["p_seats"].width, \
        "SEATS did not span both columns — the ＋ is in half a pane"
    # DIALS spans for the same reason and is asserted the same way: it is the
    # other content-sized panel, so a column of its own would buy it nothing and
    # cost the panel beside it half its width.
    assert wide["p_dials"].width > narrow["p_dials"].width, \
        "DIALS did not span both columns"


def test_the_review_queue_stays_with_the_prs_it_reviews():
    """#273's arrangement survives the second column, by moving with it.

    Narrow, the queue is directly UNDER OPEN PRs: that one says a PR exists and
    CI is green, this one says whether anybody has reviewed it, and they are
    read together. A grid fills row by row in DOM order, so leaving the order
    alone would have put the queue in the row below PLANS and a column away from
    the panel it exists to answer. Wide, `under` becomes `beside` — same row,
    next column — which is `relayout`'s one job that CSS could not do.
    """
    narrow = asyncio.run(_laid_out(90))["panels"]
    assert narrow["p_queue"].y == narrow["p_prs"].y + narrow["p_prs"].height, \
        "narrow: REVIEW QUEUE is not directly under OPEN PRs"

    wide = asyncio.run(_laid_out(200))["panels"]
    assert wide["p_queue"].y == wide["p_prs"].y, \
        "wide: REVIEW QUEUE is not in OPEN PRs' row"
    assert wide["p_queue"].x > wide["p_prs"].x, \
        "wide: REVIEW QUEUE is not beside OPEN PRs"


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
        app.refresh_limits = lambda: None
        app.refresh_seats = lambda: None
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
            seats = app.query_one("#seats")
            await pilot.click(seats, offset=(4, len(full) + 1))
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
    """The panel headings, which are where a narrowed panel admits it narrowed."""
    return {name: _text(app.query_one(f"#t_{name}")) for name in ("fleet", "claims", "plan")}


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
    async with app.run_test(size=(80, 44)) as pilot:
        # on_mount sets cfg from the board when there is one; there need not be.
        app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid", agent="host")
        app.render_board(SCOPED_BOARD)
        app.render_plan(SCOPED_PLAN, None)
        await pilot.pause()

        fleet, claims, plan = (app.query_one(f"#{n}") for n in ("fleet", "claims", "plan"))
        titles = _titles(app)

        # NARROW: this project's rows, the unattributable row kept, no repo cell.
        # Asserted on the `what` cell, which is the one the dropped column widened.
        # who state stage what ttl — `what` is index 3 since the stage column (#262).
        shown = sorted(_cells(fleet, i)[3] for i in range(fleet.row_count))
        if shown != ["? nowhere", "here"]:
            failures.append(f"narrow FLEET holds {shown}, not this repo's row and the "
                            "one the board could not attribute — marked, because the "
                            "cell that used to say so is the cell this view drops")
        if len(fleet.columns) != 5:
            failures.append(f"narrow FLEET has {len(fleet.columns)} columns, not 5")
        if "1 elsewhere" not in titles["fleet"]:
            failures.append(f"narrow FLEET does not say what it hid: {titles['fleet']!r}")
        if "1 elsewhere" not in titles["claims"]:
            failures.append(f"narrow CLAIMED does not say what it hid: {titles['claims']!r}")
        if "1 elsewhere" not in titles["plan"]:
            failures.append(f"narrow PLANS does not say what it hid: {titles['plan']!r}")
        if claims.row_count and _cells(claims, 0)[1] != "#261":
            failures.append(f"the claim key still carries its repo: {_cells(claims, 0)}")
        if "quarterback" not in _text(app.query_one("#head")):
            failures.append("the header does not name the scope it is showing")

        # The icons a click acts on must not have moved with the column that went.
        if plan.row_count and _cells(plan, 0)[app.FIX_COLUMN] != "⚒":
            failures.append(f"the ⚒ moved out of column {app.FIX_COLUMN}: {_cells(plan, 0)}")

        await pilot.press("s")
        await pilot.pause()

        fleet, claims = app.query_one("#fleet"), app.query_one("#claims")
        titles = _titles(app)
        if fleet.row_count != 3:
            failures.append(f"the wide view holds {fleet.row_count} agents, not 3")
        if len(fleet.columns) != 6:
            failures.append(f"the wide view has {len(fleet.columns)} columns, not 6")
        if "elsewhere" in titles["fleet"] or "elsewhere" in titles["plan"]:
            failures.append(f"the wide view still claims to hide rows: {titles}")
        if claims.row_count != 2 or _cells(claims, 0)[1] != "quarterback#261":
            failures.append(f"the wide view's claims read {[_cells(claims, i) for i in range(claims.row_count)]}")
        if "all repos" not in _text(app.query_one("#head")):
            failures.append("the header does not say the pane went wide")
        wide_rows = [_cells(fleet, i) for i in range(fleet.row_count)]
        if any(cell.startswith("? ") for row in wide_rows for cell in row):
            failures.append(f"the wide view still marks an unattributed row: {wide_rows}")
        if plan.row_count and _cells(plan, 0)[app.FIX_COLUMN] != "⚒":
            failures.append(f"the ⚒ moved when the column came back: {_cells(plan, 0)}")

        await pilot.press("s")                     # and back, from cache
        await pilot.pause()
        if app.query_one("#fleet").row_count != 2:
            failures.append("narrowing again did not redraw from what the client had")

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
        fleet = app.query_one("#fleet")
        return [_cells(fleet, i)[2] for i in range(fleet.row_count)]


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
        table = app.query_one("#queue")
        rows = [_cells(table, i) for i in range(table.row_count)]
        title = str(app.query_one("#t_queue").content)
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
    assert [r[2] for r in rows] == ["#264", "#270"], rows
    # Column 3 is the verb on a narrow pane: state, ⚖, pr, verb, age, title.
    assert rows[0][3] == "panel", rows[0]
    assert rows[1][3] == "conflicting", rows[1]
    # The board's own word, abbreviated for the column: `integrate` is what a
    # DRAINABLE row would show, and this row is not one.
    assert "integrate" not in rows[1][3]
    # `~` on an age that is the longest the wait could have been — nothing
    # records when a branch started conflicting.
    assert rows[1][4].startswith("~"), rows[1]
    assert not rows[0][4].startswith("~"), rows[0]
    assert "1 waiting" in title and "1 held" in title, title


def test_the_queues_scales_icon_is_live_only_where_a_round_is_what_is_wanted():
    """`fix`, `rebase` and `land` are real next actions with no button here.

    Drawing a live ⚖ on a conflicting branch would spend a whole panel round to
    be told it conflicts (#271), so the icon is grey there and the click explains
    the row instead of starting anything. Same dimming the unreachable-repo guard
    uses one panel over, so "grey means not offered" is one habit and not two.
    """
    rows, _, started, detail = asyncio.run(
        _drive_queue(offset=(_load_app().Dash.PANEL_COLUMN + 2, 2)))
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
    the one message that says why it cannot tell you. The plain renderer has
    drawn this as a row since #273 (qb-dash.py:377) and parity is the whole
    argument for flipping the default in #426.
    """
    err = "board unreachable: HTTPConnectionPool(host='board.invalid', port=80)"
    rows, title, _, _ = asyncio.run(
        _drive_queue_state({**QUEUE, "entries": [], "depth": 0, "error": err}))
    assert len(rows) == 1, rows
    assert "board unreachable" in rows[0][-1], rows[0]
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
    assert "every open PR has had a round" in rows[0][-1], rows[0]


def test_a_queue_with_neither_entries_nor_a_board_falls_back_to_words():
    """A board too old to send `idle` still gets a sentence, not a blank."""
    rows, _, _, _ = asyncio.run(
        _drive_queue_state({**QUEUE, "entries": [], "depth": 0, "error": None,
                            "idle": None}))
    assert len(rows) == 1, rows
    assert "nothing waiting on review" in rows[0][-1], rows[0]


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
        table = app.query_one("#queue")
        return ([_cells(table, i) for i in range(table.row_count)],
                str(app.query_one("#t_queue").content), [], app.detail_text)


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
        table = app.query_one("#issues")
        first = [_numbered_cell(table.get_row_at(i)) for i in range(table.row_count)]
        app.render_board({"agents": [], "claims": claims})
        await pilot.pause()
        after = [_numbered_cell(table.get_row_at(i)) for i in range(table.row_count)]
        return first, after


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
            table = app.query_one("#issues")
            rows = [_numbered_cell(table.get_row_at(i)) for i in range(table.row_count)]
            app.fix_issue({"number": 426, "repo": qd.REPO})
            await pilot.pause()
            return (rows, str(app.query_one("#t_issues", app_module.Static).content),
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
            table = app.query_one("#issues")
            return ([_numbered_cell(table.get_row_at(i)) for i in range(table.row_count)],
                    str(app.query_one("#t_issues", app_module.Static).content))

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
            return str(app.query_one("#t_issues", app_module.Static).content)

    title = asyncio.run(drive())
    assert "waiting for gh" in title, title
    assert "0" not in title, f"a count painted before gh answered: {title!r}"


def test_the_wait_for_the_board_does_not_swallow_a_gh_failure():
    """`gh` failing while the board is slow must still say `gh` failed.

    The error is stored before the gate and the gate writes its own title, so the
    one thing a reader needs — which end is broken — was the thing dropped. A
    panel that blames the board for `gh`'s failure sends them to the wrong end of
    it, and this is the window in which somebody is most likely to be looking at
    a stalled panel and wondering.

    `gh` answering with an empty list AND an error is an answer, so only the
    board is outstanding here: the title names the board as what it waits for and
    carries the `gh` error beside it.
    """
    async def drive() -> str:
        app_module, app = _quiet_dash()
        async with app.run_test(size=(100, 50)) as pilot:
            app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid",
                                                 agent="host")
            app.render_issues([], "HTTPError: 502")
            await pilot.pause()
            return str(app.query_one("#t_issues", app_module.Static).content)

    title = asyncio.run(drive())
    assert "waiting for the board" in title, title
    assert "HTTPError" in title, f"the gh failure was dropped: {title!r}"


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
            table = app.query_one("#issues")
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
    """#262: `who state stage what ttl`, and the stage cell is column 2.

    `state` says whether the pane is moving; this says where it has got to. The
    agent that reported nothing gets the fleet's glyph for an unsaid value, which
    is not alphanumeric and so cannot be read as a stage — the value space is 1-6
    alphanumerics, at the board's edge and in `qb-stage` before it.
    """
    stages = asyncio.run(_drive_stage())
    assert stages == ["R2", _load_app().qd.STAGE_UNREPORTED]


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
    """The ⚖ and the ⚒ on a row this dashboard cannot act on."""
    app_module = _load_app()
    qd = app_module.qd
    app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600,
                          scope=qd.Scope([qd.REPO, "someone/else"]))
    for name in ("refresh_limits", "refresh_seats", "refresh_board",
                 "refresh_plan", "refresh_prs", "refresh_issues"):
        setattr(app, name, lambda: None)

    failures: list[str] = []
    async with app.run_test(size=(90, 44)) as pilot:
        app.cfg = app.cfg or SimpleNamespace(base_url="http://board.invalid", agent="host")
        app.repo_slug = qd.REPO                            # what this checkout is
        # The board has answered and nothing is held. Said rather than assumed:
        # `held` is None until it answers and ISSUES deliberately paints nothing
        # then (#433), so a driver that renders the table by hand has to stand in
        # for the board as well.
        app.held = {}
        app.render_prs([{"number": 1, "title": "ours", "repo": qd.REPO,
                         "isDraft": False, "statusCheckRollup": []},
                        {"number": 2, "title": "theirs", "repo": "someone/else",
                         "isDraft": False, "statusCheckRollup": []}], None)
        app.render_issues([{"number": 3, "title": "ours", "repo": qd.REPO},
                           {"number": 4, "title": "theirs", "repo": "someone/else"}], None)
        await pilot.pause()

        for table_id, column in (("#prs", app.PANEL_COLUMN), ("#issues", app.FIX_COLUMN)):
            table = app.query_one(table_id)
            styles = {}
            for row in range(table.row_count):
                cells = table.get_row_at(row)
                number = next(str(c).lstrip("#") for c in cells
                              if str(c).startswith("#"))
                styles[number] = str(getattr(cells[column], "style", ""))
            ours, theirs = ("1", "2") if table_id == "#prs" else ("3", "4")
            if "cyan" not in styles.get(ours, ""):
                failures.append(f"{table_id}: this repo's icon is not live ({styles})")
            if "cyan" in styles.get(theirs, ""):
                failures.append(f"{table_id}: another repo's icon still looks "
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
        app_module.qd.tmux_seats = lambda: seats
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


# ---- two screens on one machine (#208) ---------------------------------------
#
# `list-panes -a` is the whole tmux server, so since seats became per-project the
# dashboard sees two seat 1s and has to tell them apart. Everything below is
# synchronous: it exercises the joins, not the widgets, and does not need a pilot.


def _dash():
    return _load_app().Dash(interval=3600, gh_interval=3600)


def _agents(*holders):
    return {"agents": [{"holder": h, "state": "working", "reported": None}
                       for h in holders], "claims": []}


def _stated(app, **seat):
    """A pane record as tmux_seats returns one, filled in only where it matters."""
    return app.seat_state({"pane": "%0", "repo": "", "scope": "", **seat})


def test_a_seat_pane_is_matched_to_its_own_screens_agent():
    """The number alone is no longer a name. Matching on it shows one screen's
    agent against the other screen's pane — a wrong answer that looks exactly
    like a right one."""
    app = _dash()
    app.seat_states = {("zeus", "lexray", 1): {"holder": "zeus/seat-lexray-1"},
                       ("zeus", "nix-fleet", 1): {"holder": "zeus/seat-nix-fleet-1"}}
    assert _stated(app, seat="1", repo="/home/rich/lexray")["holder"] \
        == "zeus/seat-lexray-1"
    assert _stated(app, seat="1", repo="/home/rich/nix-fleet")["holder"] \
        == "zeus/seat-nix-fleet-1"


def test_two_screens_on_one_repository_are_told_apart_by_the_scope_they_were_given():
    """The case QB_SEAT_SCOPE exists for, and the one @qb_repo cannot answer."""
    app = _dash()
    app.seat_states = {("zeus", "review", 1): {"holder": "zeus/seat-review-1"},
                       ("zeus", "build", 1): {"holder": "zeus/seat-build-1"}}
    assert _stated(app, seat="1", repo="/home/rich/lexray", scope="review")["holder"] \
        == "zeus/seat-review-1"
    assert _stated(app, seat="1", repo="/home/rich/lexray", scope="build")["holder"] \
        == "zeus/seat-build-1"


def test_another_machines_seat_is_not_shown_against_a_local_pane():
    """The board is the whole FLEET, so `zeus/seat-lexray-1` and
    `laptop/seat-lexray-1` are both on it and the scope cannot separate them."""
    app = _dash()
    app.cfg = SimpleNamespace(agent="zeus")
    app.seat_states = {("zeus", "lexray", 1): {"holder": "zeus/seat-lexray-1"},
                       ("laptop", "lexray", 1): {"holder": "laptop/seat-lexray-1"}}
    assert _stated(app, seat="1", repo="/home/rich/lexray")["holder"] \
        == "zeus/seat-lexray-1"

    # The machine is the harness's guess at this host's board name, and it may be
    # wrong. Then the set stays ambiguous and the cell stays empty — a wrong guess
    # costs the state, it never fills it in with somebody else's agent.
    app.cfg = SimpleNamespace(agent="not-this-host")
    assert _stated(app, seat="1", repo="/home/rich/lexray") == {}


def test_a_screen_too_old_to_say_its_repo_still_matches_when_it_can():
    """@qb_repo is newer than @qb_seat. One agent with that number is not a
    guess; two is, and a coin toss is the bug this join exists to avoid."""
    app = _dash()
    app.seat_states = {("zeus", "lexray", 1): {"holder": "zeus/seat-lexray-1"}}
    assert _stated(app, seat="1")["holder"] == "zeus/seat-lexray-1"

    app.seat_states[("zeus", "nix-fleet", 1)] = {"holder": "zeus/seat-nix-fleet-1"}
    assert _stated(app, seat="1") == {}


def test_a_pane_with_no_agent_on_the_board_is_not_given_someone_elses():
    app = _dash()
    app.seat_states = {("zeus", "lexray", 1): {"holder": "zeus/seat-lexray-1"}}
    assert _stated(app, seat="2", repo="/home/rich/lexray") == {}
    assert _stated(app, seat="", repo="/home/rich/lexray") == {}


def test_the_fleet_table_stashes_a_seat_under_its_machine_and_project():
    """render_board is what fills seat_states, and the key it uses is the join."""
    app = _dash()
    app.query_one = lambda *a, **k: _Sink()
    # The header line names the board, and there is not one configured in CI.
    app.cfg = SimpleNamespace(base_url="https://board.example", agent="zeus")
    app.render_board(_agents("zeus/seat-lexray-1", "laptop/seat-lexray-1",
                             "zeus/seat-3", "zeus/amber-otter"))
    assert set(app.seat_states) == {("zeus", "lexray", 1), ("laptop", "lexray", 1),
                                    ("zeus", None, 3)}


class _Sink:
    """Every widget render_board reaches for, doing nothing. The join is what is
    under test here; the widgets have their own pilot-driven tests above."""

    row_count = 0

    def update(self, *a, **k): pass
    def clear(self, *a, **k): pass

    def add_row(self, *a, key=None, **k):
        """Hands back a key like the real one does.

        `DataTable.add_row` returns the RowKey it used, and since #209 the
        panels file their record under that rather than under the key they
        asked for — so a stub returning None models a widget that does not
        exist, and would have hidden the caller getting it wrong."""
        return SimpleNamespace(value=key)


def test_a_fleet_click_jumps_to_the_pane_in_the_same_project(monkeypatch):
    """A FLEET row carries a board identity. Jumping to whichever pane tmux
    listed first is a jump to the wrong project half the time."""
    module = _load_app()
    # Built BEFORE the stub goes in: Dash.__init__ shells out to git for the repo
    # slug, and `module.subprocess` is the stdlib module itself, so patching .run
    # on it patches it for every caller in the process.
    app = module.Dash(interval=3600, gh_interval=3600)
    monkeypatch.setenv("TMUX", "/tmp/whatever,1,0")
    panes = ["%0\t1\t/home/rich/lexray\t", "%9\t1\t/home/rich/nix-fleet\t"]
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
    assert app.jump_to_seat(1, "nix-fleet") is True
    assert selected == ["%9"]

    # No scope to match and more than one candidate: no jump rather than a guess.
    selected.clear()
    assert app.jump_to_seat(1, None) is False
    assert selected == []

    # One candidate and nothing to match it on: the click still works.
    panes[:] = ["%0\t1\t\t"]
    assert app.jump_to_seat(1, "lexray") is True
    assert selected == ["%0"]


def test_a_repository_path_with_a_space_in_it_still_finds_its_pane(monkeypatch):
    """The pane list is tab-separated because @qb_repo is a filesystem path — a
    space-split returned three fields and matched no seat at all."""
    module = _load_app()
    app = module.Dash(interval=3600, gh_interval=3600)      # before the stub; see above
    monkeypatch.setenv("TMUX", "/tmp/whatever,1,0")
    selected: list[str] = []

    class Done:
        stdout = "%4\t2\t/home/rich/my repos/lexray\t\n"

    def fake_run(argv, **kw):
        if "select-pane" in argv:
            selected.append(argv[-1])
        return Done()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert app.jump_to_seat(2, "lexray") is True
    assert selected == ["%4"]


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
    """Render both multi-repo panels with a colliding number, then click each row.

    The render is wrapped rather than left to propagate: a test that dies of
    DuplicateKey reports an ERROR and names no assertion, and this suite's own
    convention — every `_drive_*` returns the failures it found — is what turns
    the crash into a statement about the defect.
    """
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600)
    # Every background fetch off: these panels are being driven by hand, and a
    # live `gh` tick landing mid-test would rewrite the rows under the clicks.
    app.refresh_limits = lambda: None
    app.refresh_seats = lambda: None
    app.refresh_board = lambda: None
    app.refresh_plan = lambda: None
    app.refresh_prs = lambda: None
    app.refresh_issues = lambda: None

    # As above: the board answered, nothing is held (#433).
    app.held = {}

    opened: list[str] = []
    app.open_pr = lambda pr: opened.append(f"{pr.get('repo')}#{pr.get('number')}")
    app.open_issue = lambda issue: opened.append(f"{issue.get('repo')}#{issue.get('number')}")

    failures: list[str] = []
    async with app.run_test(size=(100, 44)):
        for label, render, rows, table_id, prefix in (
            ("PRS", app.render_prs, _TWO_REPOS_PRS, "#prs", "pr:"),
            ("ISSUES", app.render_issues, _TWO_REPOS_ISSUES, "#issues", "issue:"),
        ):
            try:
                render(rows, None)
            except Exception as exc:               # noqa: BLE001 — the defect itself
                failures.append(
                    f"{label}: two repos sharing a number took the dashboard down with "
                    f"{type(exc).__name__} — a duplicate row must degrade, not crash")
                continue

            table = app.query_one(table_id)
            if table.row_count != len(rows):
                failures.append(
                    f"{label}: {len(rows)} rows from two repos rendered as "
                    f"{table.row_count} — one repo's row was dropped")
                continue

            # The PANEL's own key, not the one it was rescued into. Asserted
            # exactly, and this is the assertion that makes the test about the
            # defect: ClickTable.add_row suffixes a repeat rather than raising,
            # so with the bare-number keys restored these rows STILL render as
            # two, still carry distinct keys (`pr:42` and `pr:42~2`) and still
            # click through to their own records — everything below passes and
            # the bug is untouched. Only the key itself tells the two fixes
            # apart, so only the key can pin the one this test is named for.
            keys = [rk.value for rk in table.rows]
            want_keys = sorted(f"{prefix}{r['repo']}#{r['number']}" for r in rows)
            if sorted(keys) != want_keys:
                failures.append(
                    f"{label}: row keys are {sorted(keys)}, not {want_keys} — the "
                    "panel is not keying on the repo, whatever the backstop did after")

            # The click half. Each row must reach the record it displays; a
            # shared key means the second write wins and both rows open it.
            opened.clear()
            if len(set(keys)) != len(keys):
                failures.append(f"{label}: two rows share the row key {keys!r}")
            for key in keys:
                app.dispatch_row(key, column=None)
            want = sorted(f"{r['repo']}#{r['number']}" for r in rows)
            if sorted(opened) != want:
                failures.append(
                    f"{label}: clicking each row opened {sorted(opened)}, not {want} — "
                    "a row is pointing at the other repo's record")
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
            table = app.query_one("#plan")
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
        table = app.query_one("#plan")
        rows = [_cells(table, i) for i in range(table.row_count)]
        if [r[3] for r in rows] != ["1", "~2", "~3"]:
            failures.append(f"PLAN: the rank cells read {[r[3] for r in rows]} — the "
                            "human's order reaches the pane as row position alone, "
                            "with nothing saying which positions anybody chose")
        if [r[4] for r in rows] != ["#394", "PR#397", ""]:
            failures.append(f"PLAN: the ref cells read {[r[4] for r in rows]} — a PR "
                            "and an issue render the same, so nothing on the row says "
                            "why one ⚒ works and the other does not")
        if rows[0][0] != "◉":
            failures.append(f"PLAN: the board's own `next` is not marked: {rows[0]}")
        if rows[2][6] != "⊘zeus/jasper-moss":
            failures.append(f"PLAN: the who cell reads {rows[2][6]!r} — the machine or "
                            "the wait is missing, and both are facts about the row")
        title = _text(app.query_one("#t_plan"))
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
        # whose rows did not move across the outage — so the error text outlived
        # the error, in the one case where nothing else would ever clear it.
        empty = {**plan, "items": [], "counts": {}, "next": None, "truncated": False}
        app.render_plan(empty, "HTTPError: 502")
        if "board:" not in _text(app.query_one("#t_plan")):
            failures.append("PLAN: a dead board is not reported in the title")
        app.render_plan(empty, None)
        if "board:" in _text(app.query_one("#t_plan")):
            failures.append("PLAN: the board came back and the title still says it "
                            "is down — the error outlived the error")
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

    started: list[tuple[str, str]] = []
    app.run_in_pane = lambda name, command: started.append((name, command))
    app.run_in_window = lambda name, command: started.append((name, command))

    failures: list[str] = []
    async with app.run_test(size=(100, 44)):
        app.render_prs(_TWO_REPOS_PRS, None)
        for rk in list(app.query_one("#prs").rows):
            pr = app.rows[str(rk.value)]
            started.clear()
            app.detail_text = ""
            app.dispatch_row(str(rk.value), column=app_module.Dash.PANEL_COLUMN)
            if pr["repo"] == app.repo_slug:
                if not started:
                    failures.append(
                        "⚖ on this dashboard's OWN PR started nothing — the guard "
                        f"is refusing everything, not just another repo's ({app.detail_text})")
            elif started:
                failures.append(
                    f"⚖ on {pr['repo']}#{pr['number']} launched {started[0][1]!r} — a paid "
                    f"review, in {app.repo_slug}, of whatever wears that number there")
            elif pr["repo"] not in app.detail_text:
                failures.append(
                    f"⚖ on {pr['repo']}#{pr['number']} refused but said {app.detail_text!r} — "
                    "a dim icon that swallows the click is indistinguishable from a broken one")
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
        _drive_dials(offset=(module.Dash.EDIT_COLUMN + 2, 1)))
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
                             (module.Dash.EDIT_COLUMN + 2, 1))
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
                             (module.Dash.EDIT_COLUMN + 2, 1))
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
