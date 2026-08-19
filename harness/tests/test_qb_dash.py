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

    opened: list[int] = []
    jumped: list[int] = []
    app.open_pr = lambda pr: (opened.append(pr.get("number")),
                              app.say(f"opened #{pr.get('number')}"))[1]
    app.jump_to_seat = lambda seat: (jumped.append(seat), True)[1]

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
        await pilot.click(prs, offset=(30, 1))
        await pilot.pause(0.2)
        if not opened:
            failures.append("a click on a PR row did not open it")

        await pilot.click(fleet, offset=(4, 1))
        await pilot.pause(0.2)
        if not app.detail_text or app.detail_text.startswith("click a row"):
            failures.append("a click on an agent row changed nothing")

        if claims.row_count:
            await pilot.click(claims, offset=(4, 1))
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

    started: list[tuple[str, str]] = []
    opened: list[int] = []
    app.run_in_window = lambda name, command: started.append((name, command))
    app.open_issue = lambda issue: opened.append(issue.get("number"))

    failures: list[str] = []
    async with app.run_test(size=(90, 50)) as pilot:
        for _ in range(40):
            await pilot.pause(0.25)
            if app.query_one("#issues").row_count:
                break
        issues = app.query_one("#issues")
        _need_rows(issues, "issues", app.issue_err)
        # Read the number off the RENDERED first row rather than re-deriving the
        # order here: what the click has to match is the row a human sees. Found
        # by its "#" rather than by column index — the panels grew a repo column
        # between the issue number and the icons, and a hardcoded index made this
        # fail with `int('quarterback')` rather than saying what moved.
        top = int(_numbered_cell(issues.get_row_at(0)))

        # The ⚒ column asks first, the same as the ⚖ does.
        await pilot.click(issues, offset=(app_module.Dash.FIX_COLUMN + 2, 1))
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
            elif f"/fix-issue {top}" not in started[0][1]:
                failures.append(f"wrong command launched: {started[0][1]}")
            elif started[0][0] != f"fix-{top}":
                failures.append(f"wrong window name: {started[0][0]}")

        # A click anywhere else on the row still means "open it on GitHub".
        started.clear()
        await pilot.click(issues, offset=(30, 1))
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

    started: list[tuple[str, str]] = []
    app.run_in_window = lambda name, command: started.append((name, command))

    failures: list[str] = []
    async with app.run_test(size=(100, 50)) as pilot:
        for _ in range(40):
            await pilot.pause(0.25)
            if app.query_one("#plan").row_count:
                break
        plan = app.query_one("#plan")
        _need_rows(plan, "plan items", app.plan_err)

        # Anywhere but the ⚒: the detail line, and it must name the row clicked.
        await pilot.click(plan, offset=(40, 1))
        await pilot.pause(0.3)
        title = str(plan.get_row_at(plan.scroll_offset.y)[4]).rstrip("…")
        if title and title not in app.detail_text:
            failures.append(f"the detail line does not describe the row clicked: "
                            f"{app.detail_text[:80]!r}")

        # The ⚒, on a row that actually has an issue behind it. Which row that is
        # depends on today's plan, so it is found rather than assumed — and what
        # it should do is read off the row the table actually scrolled to, not
        # off the index asked for: scrolling near the end of a list stops short.
        import qbdata as qd
        ordered = qd.sort_plan(app.plan)
        wanted = next((n for n, i in enumerate(ordered)
                       if qd.plan_issue(i) and not i.get("claim")), None)
        if wanted is None:
            pytest.skip("no free issue-backed item on the plan today — nothing to take")
        plan.scroll_to(y=wanted, animate=False)
        await pilot.pause(0.3)
        landed = ordered[plan.scroll_offset.y]

        await pilot.click(plan, offset=(app_module.Dash.FIX_COLUMN + 2, 1))
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
            elif f"/fix-issue {issue['number']}" not in started[0][1]:
                failures.append(f"wrong command launched: {started[0][1]}")

    return failures


async def _drive_panel() -> list[str]:
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600)
    app.refresh_limits = lambda: None

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

        # The ⚖ column asks first and starts nothing by itself.
        await pilot.click(prs, offset=(app_module.Dash.PANEL_COLUMN + 2, 1))
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

        # Cancelling starts nothing.
        started.clear()
        await pilot.click(prs, offset=(app_module.Dash.PANEL_COLUMN + 2, 2))
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.3)
        if started:
            failures.append("cancelling still started a review")

        # A click anywhere else on the row still means "open on GitHub".
        await pilot.click(prs, offset=(30, 1))
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
    def add_row(self, *a, **k): pass


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
