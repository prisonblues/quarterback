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
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load_app():
    """Import harness/bin/qb-dash-tui, which has no .py for spec inference."""
    loader = importlib.machinery.SourceFileLoader("qb_dash_tui", str(BIN / "qb-dash-tui"))
    spec = importlib.util.spec_from_loader("qb_dash_tui", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _why_skip() -> str | None:
    try:
        import textual  # noqa: F401
    except ImportError:
        return "textual is not importable (it lives in mcp/'s environment)"
    try:
        from mcp_server.board.config import resolve
    except ImportError:
        return "mcp_server is not importable"
    try:
        resolve()
    except Exception as exc:                       # noqa: BLE001
        return f"no board configured here ({type(exc).__name__})"
    return None


pytestmark = pytest.mark.skipif(_why_skip() is not None, reason=_why_skip() or "")


def test_a_single_click_acts_on_the_row_under_the_pointer():
    assert asyncio.run(_drive()) == []


def test_the_scales_icon_reviews_and_the_rest_of_the_row_opens():
    """One row, two verbs, told apart by which column was clicked.

    Also covers the confirmation: a panel review costs money and comments on a
    public PR, so the click must not start one on its own.
    """
    assert asyncio.run(_drive_panel()) == []


async def _drive() -> list[str]:
    app_module = _load_app()
    app = app_module.Dash(interval=3600, pr_interval=3600)   # no refresh mid-test

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
        if not prs.row_count:
            return ["no PR rows arrived — cannot test a click on them"]

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


async def _drive_panel() -> list[str]:
    app_module = _load_app()
    app = app_module.Dash(interval=3600, pr_interval=3600)

    started: list[tuple[str, str]] = []
    opened: list[int] = []
    app.run_in_window = lambda name, command: started.append((name, command))
    app.open_pr = lambda pr: opened.append(pr.get("number"))

    failures: list[str] = []
    async with app.run_test(size=(90, 44)) as pilot:
        for _ in range(40):
            await pilot.pause(0.25)
            if app.query_one("#prs").row_count:
                break
        prs = app.query_one("#prs")
        if not prs.row_count:
            return ["no PR rows arrived"]

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
