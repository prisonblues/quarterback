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


def _x_of(table, column: int) -> int:
    """The x offset to click for a cell in `column`.

    COMPUTED, not hardcoded. The columns are auto-sized to their contents, and the
    repo cell comes and goes with the scope (#261) — so a literal `offset=(30, 1)`
    means a different column on every pane. That is not hypothetical: while these
    tests were being rewritten for the merged table, a hardcoded 9 landed on the ⚒
    rather than the ref, raised the confirmation, and every later click in the run
    went to the modal instead of the table. Two "the click did nothing" failures
    that were entirely the test's fault.
    """
    x = 0
    for i, col in enumerate(table.ordered_columns):
        width = col.get_render_width(table)
        if i == column:
            return x + width // 2
        x += width
    raise AssertionError(f"no column {column}: the table has {len(table.ordered_columns)}")


def _record_at(app, table, index: int) -> dict | None:
    """The record behind a rendered row — the join, not the cells drawn from it."""
    rows = table.ordered_rows
    if not 0 <= index < len(rows):
        return None
    return app.rows.get(str(rows[index].key.value))


def _find_row(app, table, want) -> int | None:
    """The first rendered row whose record satisfies `want`.

    Found rather than assumed: which row is a PR, and which is a free issue,
    depends on what the fleet has open today, and the whole point of the merged
    table is that a row's kind is a property of the row rather than of the panel
    it is in.
    """
    for i in range(table.row_count):
        record = _record_at(app, table, i)
        if record and want(record):
            return i
    return None


async def _click(pilot, app, table, index: int, column: int):
    """Click a cell of row `index`, and answer with the record actually clicked.

    Scrolled to first, and the row is then read back off the table rather than
    from `index`: scrolling near the end of a list stops short, so the row under
    the pointer is whichever one the table settled on.
    """
    table.scroll_to(y=index, animate=False)
    await pilot.pause(0.2)
    landed = _record_at(app, table, table.scroll_offset.y)
    await pilot.click(table, offset=(_x_of(table, column), 1))
    await pilot.pause(0.3)
    return landed


@needs_live_data
def test_a_single_click_acts_on_the_row_under_the_pointer():
    assert asyncio.run(_drive()) == []


@needs_live_data
def test_one_row_three_verbs_told_apart_by_the_cell_clicked():
    """The merged table's whole click contract, on whatever is open today.

    Three verbs per row, because there is one table now and it has to carry every
    verb the four panels had between them: the icon starts the work, the ref opens
    it on GitHub, and anything else explains it. Told apart by COLUMN, which is
    what makes them one habit rather than three.

    Also covers the confirmation: a panel review costs real money and comments on
    a public PR, so a click must not start one on its own.
    """
    assert asyncio.run(_drive_verbs()) == []


@needs_live_data
def test_the_hammer_starts_a_fix_on_the_issue_actually_under_the_pointer():
    """The ⚒ is the shortest path from "what is next" to somebody doing it.

    What it launches has to be `/fix-issue <n>` for the row clicked — a fix on the
    wrong issue writes code nobody asked for. It has to work on a row that reached
    the table as a plan item just as much as on one that reached it from `gh`,
    which is the merge doing its job: after it, there is no such thing as "the
    issue panel's ⚒" and "the plan panel's ⚒".
    """
    assert asyncio.run(_drive_fix()) == []


@needs_live_data
def test_a_merged_row_still_explains_why_it_is_where_it_is():
    """The plan's note has to survive being merged into a GitHub row.

    It is the reasoning behind the item's place in the order, it is on the board
    and nowhere else, and the merge is exactly where it could have been lost: the
    row is drawn from the issue, so the click has to reach past it to the plan
    item the issue was joined to.
    """
    assert asyncio.run(_drive_detail()) == []


async def _drive() -> list[str]:
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600)   # no refresh mid-test
    # The usage line is a live call to Anthropic and it appears as a ROW when
    # its first answer lands — which reflows everything under it, mid-click if
    # the click is already in flight. Off for every test here: none of them is
    # about the caps, and a test that reached the network would be its own bug.
    app.refresh_limits = lambda: None

    opened: list[str] = []
    jumped: list[int] = []
    app.open_url = lambda url: (opened.append(url), app.say(f"opened {url}"))[1]
    app.jump_to_seat = lambda seat, scope=None: (jumped.append(seat), True)[1]

    failures: list[str] = []
    async with app.run_test(size=(80, 44)) as pilot:
        await _settle(app, pilot)
        work = app.query_one("#work")
        fleet = app.query_one("#fleet")
        _need_rows(work, "work", app.pr_err or app.issue_err or app.plan_err)

        # ONE click, on a row that is not the cursor's, is the whole point.
        row = await _click(pilot, app, work, 0, app.ref_column)
        if row is None:
            failures.append("the top work row has no record behind it")
        elif row["number"] is None:
            if not app.detail_text:
                failures.append("a numberless row's ref said nothing")
        elif not opened:
            failures.append("a click on the ref did not open it")
        elif str(row["number"]) not in opened[-1]:
            failures.append(f"opened {opened[-1]}, but the row was #{row['number']}")

        await pilot.click(fleet, offset=(4, 1))
        await pilot.pause(0.2)
        if not app.detail_text or app.detail_text.startswith("click a row"):
            failures.append("a click on an agent row changed nothing")

        opened.clear()                             # and the keyboard path still works
        work.focus()
        work.scroll_to(y=0, animate=False)
        await pilot.pause(0.2)
        top = _record_at(app, work, 0)
        await pilot.press("o")
        await pilot.pause(0.2)
        if top and top["number"] is not None and not opened:
            failures.append("'o' did not open the selected row")

    return failures


async def _settle(app, pilot, tries: int = 40) -> None:
    """Wait for the board AND `gh` — three fetches on three clocks feed one table.

    Waiting only for rows was not enough once they merged: the board answers first,
    so a table with rows in it can still be the plan alone, and a test that started
    clicking there would be testing a table with no PRs in it and calling that a
    pass.
    """
    for _ in range(tries):
        await pilot.pause(0.25)
        if app.query_one("#work").row_count and app.prs and app.query_one("#fleet").row_count:
            return


async def _drive_verbs() -> list[str]:
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600)
    app.refresh_limits = lambda: None

    started: list[tuple[str, str]] = []
    windowed: list[tuple[str, str]] = []
    opened: list[str] = []
    # run_in_PANE, not run_in_window: a review lands in the seat row, beside the
    # work it is about. Both are stubbed so a review quietly reverting to a window
    # shows up here as a failure rather than as a passing test.
    app.run_in_pane = lambda name, command: started.append((name, command))
    app.run_in_window = lambda name, command: windowed.append((name, command))
    app.open_url = lambda url: opened.append(url)

    failures: list[str] = []
    async with app.run_test(size=(90, 44)) as pilot:
        await _settle(app, pilot)
        work = app.query_one("#work")
        _need_rows(work, "work", app.pr_err or app.issue_err)
        wanted = _find_row(app, work, lambda r: r["kind"] == "pr")
        if wanted is None:
            pytest.skip("no open PR on the board today — nothing to review")

        # The ⚖ asks first, and starts nothing by itself.
        row = await _click(pilot, app, work, wanted, app.ACTION_COLUMN)
        if row["kind"] != "pr":
            pytest.skip("the table scrolled to a non-PR row — nothing to review here")
        if started:
            failures.append("the icon started a review with no confirmation")
        if not isinstance(app.screen, app_module.Confirm):
            failures.append("the icon did not raise the confirmation")
        else:
            await pilot.press("enter")               # …and confirming starts it
            await pilot.pause(0.3)
            if not started:
                failures.append("confirming did not start the review")
            elif f"/panel-review-pr {row['number']}" not in started[0][1]:
                failures.append(f"wrong command launched: {started[0][1]}")
            if windowed:
                failures.append("the review opened a window, not a seat-row pane")

        # Cancelling starts nothing.
        started.clear()
        await _click(pilot, app, work, wanted, app.ACTION_COLUMN)
        await pilot.press("escape")
        await pilot.pause(0.3)
        if started:
            failures.append("cancelling still started a review")

        # The ref opens it — and as a PULL, not an issue: the number alone cannot
        # say which, and a link to the wrong one is a 404 or somebody else's page.
        await _click(pilot, app, work, wanted, app.ref_column)
        if not opened:
            failures.append("clicking the ref did not open the PR")
        elif f"/pull/{row['number']}" not in opened[-1]:
            failures.append(f"the ref opened {opened[-1]}, not the PR's own page")
        if started:
            failures.append("clicking the ref started a review")

        # And anything else on the row explains it rather than doing anything.
        started.clear()
        opened.clear()
        title_column = app.ref_column + 1
        await _click(pilot, app, work, wanted, title_column)
        if opened or started:
            failures.append("clicking the title opened or started something")
        if not app.detail_text:
            failures.append("clicking the title explained nothing")

        # And the keyboard route to the same verb.
        work.focus()
        await pilot.press("p")
        await pilot.pause(0.3)
        if not isinstance(app.screen, app_module.Confirm):
            failures.append("'p' did not raise the confirmation")
        else:
            await pilot.press("escape")
            await pilot.pause(0.2)

    return failures


async def _drive_fix() -> list[str]:
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600)
    app.refresh_limits = lambda: None

    started: list[tuple[str, str]] = []
    app.run_in_window = lambda name, command: started.append((name, command))

    failures: list[str] = []
    async with app.run_test(size=(90, 50)) as pilot:
        await _settle(app, pilot)
        work = app.query_one("#work")
        _need_rows(work, "work", app.issue_err)
        import qbdata as qd
        wanted = _find_row(app, work,
                           lambda r: qd.work_action(r)[2] == "fix" and not r["claim"])
        if wanted is None:
            pytest.skip("no free issue on the board today — nothing to take")

        row = await _click(pilot, app, work, wanted, app.ACTION_COLUMN)
        verb = qd.work_action(row)[2]
        if started:
            failures.append("the icon started a fix with no confirmation")
        elif verb != "fix":
            # A row with nothing to start has to SAY so: an icon that swallows the
            # click is indistinguishable from a broken one.
            if not app.detail_text:
                failures.append("the icon on an unfixable row said nothing")
        elif not isinstance(app.screen, app_module.Confirm):
            failures.append("the icon did not raise the confirmation")
        else:
            number = row["number"] if row["number"] is not None \
                else qd.plan_issue(row["plan"] or {})["number"]
            await pilot.press("enter")
            await pilot.pause(0.3)
            if not started:
                failures.append("confirming did not start the fix")
            elif f"/fix-issue {number}" not in started[0][1]:
                failures.append(f"wrong command launched: {started[0][1]}")
            elif started[0][0] != f"fix-{number}":
                failures.append(f"wrong window name: {started[0][0]}")

        # And the keyboard route to the same verb.
        work.focus()
        work.scroll_to(y=wanted, animate=False)
        await pilot.pause(0.2)
        await pilot.press("f")
        await pilot.pause(0.3)
        if isinstance(app.screen, app_module.Confirm):
            await pilot.press("escape")
            await pilot.pause(0.2)
        elif not app.detail_text:
            failures.append("'f' neither asked nor explained why it could not")

    return failures


async def _drive_detail() -> list[str]:
    app_module = _load_app()
    app = app_module.Dash(interval=3600, gh_interval=3600, plan_interval=3600)
    app.refresh_limits = lambda: None

    failures: list[str] = []
    async with app.run_test(size=(100, 50)) as pilot:
        await _settle(app, pilot)
        work = app.query_one("#work")
        _need_rows(work, "work", app.plan_err)
        wanted = _find_row(app, work, lambda r: r["plan"] is not None)
        if wanted is None:
            pytest.skip("nothing on the plan today — no order to explain")

        row = await _click(pilot, app, work, wanted, app.ref_column + 1)
        if row["plan"] is None:
            pytest.skip("the table scrolled to an unplanned row")
        # The PLAN's title, not the row's: a merged row is drawn with the GitHub
        # title, so finding the plan item's own words in the detail line is what
        # proves the click reached through the join rather than stopping at it.
        title = (row["plan"].get("title") or "").strip()
        if title and title[:24] not in app.detail_text:
            failures.append(f"the detail line does not carry the plan item's own "
                            f"words: {app.detail_text[:100]!r}")

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
        # A claim on something that is NOT an issue or a PR, which is the row shape
        # that only exists because the four panels became one: it has no GitHub
        # page and no plan item, and it is still work somebody is holding.
        {"holder": "daedalus/three", "kind": "release",
         "key": "prisonblues/quarterback:2.41"},
    ],
}

SCOPED_PLAN = [
    {"item_id": "a", "repo": "prisonblues/quarterback", "title": "ours",
     "ref": {"kind": "issue", "value": "261"}, "blocked_by": [], "claim": None},
    {"item_id": "b", "repo": "prisonblues/nix-fleet", "title": "theirs",
     "ref": None, "blocked_by": [], "claim": None},
]


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
    return {name: _text(app.query_one(f"#t_{name}")) for name in ("fleet", "work")}


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

        fleet, work = app.query_one("#fleet"), app.query_one("#work")
        titles = _titles(app)

        # NARROW: this project's rows, the unattributable row kept, no repo cell.
        # Asserted on the `what` cell, which is the one the dropped column widened.
        shown = sorted(_cells(fleet, i)[2] for i in range(fleet.row_count))
        if shown != ["here", "nowhere"]:
            failures.append(f"narrow FLEET holds {shown}, not this repo's row and the "
                            "one the board could not attribute")
        if len(fleet.columns) != 4:
            failures.append(f"narrow FLEET has {len(fleet.columns)} columns, not 4")
        if "1 elsewhere" not in titles["fleet"]:
            failures.append(f"narrow FLEET does not say what it hid: {titles['fleet']!r}")
        if "2 elsewhere" not in titles["work"]:
            failures.append(f"narrow WORK does not say what it hid: {titles['work']!r}")
        if "quarterback" not in _text(app.query_one("#head")):
            failures.append("the header does not name the scope it is showing")

        # THE MERGE, on a literal: one plan item, one claim, ONE row — and the row
        # carries both, the issue number off the item's ref and the holder off the
        # claim. Two rows here would be the defect this table exists to remove.
        refs = [_cells(work, i) for i in range(work.row_count)]
        merged = [r for r in refs if "#261" in r]
        if len(merged) != 1:
            failures.append(f"the plan item and the claim on it are not one row: {refs}")
        elif "one" not in merged[0][-1]:
            failures.append(f"the merged row does not name its holder: {merged[0]}")

        # A release claim has no GitHub page to be a number, and the narrow view
        # trims the repo the header already states.
        release = [r for r in refs if "2.41" in " ".join(r)]
        if len(release) != 1:
            failures.append(f"the release claim lost its row: {refs}")
        elif "quarterback:2.41" in " ".join(release[0]):
            failures.append(f"the claim key still carries its repo: {release[0]}")

        # The icon a click acts on must not have moved with the column that went.
        if work.row_count and _cells(work, 0)[app.ACTION_COLUMN] not in ("⚒", "⚖", "·"):
            failures.append(f"the action icon moved out of column "
                            f"{app.ACTION_COLUMN}: {_cells(work, 0)}")

        await pilot.press("s")
        await pilot.pause()

        fleet, work = app.query_one("#fleet"), app.query_one("#work")
        titles = _titles(app)
        if fleet.row_count != 3:
            failures.append(f"the wide view holds {fleet.row_count} agents, not 3")
        if len(fleet.columns) != 5:
            failures.append(f"the wide view has {len(fleet.columns)} columns, not 5")
        if "elsewhere" in titles["fleet"] or "elsewhere" in titles["work"]:
            failures.append(f"the wide view still claims to hide rows: {titles}")
        if work.row_count != 4:
            failures.append(f"the wide view holds {work.row_count} work rows, not 4: "
                            f"{[_cells(work, i) for i in range(work.row_count)]}")
        if "all repos" not in _text(app.query_one("#head")):
            failures.append("the header does not say the pane went wide")
        if app.ref_column != 4:
            failures.append(f"the ref did not move with the repo column: {app.ref_column}")
        if work.row_count and _cells(work, 0)[app.ACTION_COLUMN] not in ("⚒", "⚖", "·"):
            failures.append(f"the action icon moved when the column came back: "
                            f"{_cells(work, 0)}")
        # And the repo the narrow view trimmed is back, because out here it is the
        # cell that tells two claims apart.
        wide = " ".join(" ".join(_cells(work, i)) for i in range(work.row_count))
        if "quarterback:2.41" not in wide:
            failures.append(f"the wide view does not name the claim's repo: {wide}")

        await pilot.press("s")                     # and back, from cache
        await pilot.pause()
        if app.query_one("#fleet").row_count != 2:
            failures.append("narrowing again did not redraw from what the client had")
        if app.query_one("#work").row_count != 2:
            failures.append("narrowing again did not redraw WORK from what it had")

    return failures


def test_the_scope_narrows_the_rows_and_drops_the_column_together():
    """#261: one keypress, both halves.

    Narrowing without dropping the column leaves the waste in place; dropping it
    without narrowing leaves rows whose repo nothing states. And each panel has to
    say what it hid — a filtered pane that reads like the whole fleet is worse
    than an unfiltered one, because it is the same picture with fewer facts.
    """
    assert asyncio.run(_drive_scope()) == []


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
