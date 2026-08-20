#!/usr/bin/env python3
"""qb-dash-tui — the fleet dashboard, clickable.

Same three views as qb-dash (seats / fleet / work), but as a Textual app, so the
rows respond to the mouse.

WORK is ONE table. It used to be four — CLAIMED, PLANS, OPEN PRs and ISSUES —
which meant a single unit of work could be four rows on four panels with nothing
on the pane saying they were one thing (#272). The plan, the issues, the PRs and
the claims are joined on `owner/repo#n` and drawn in the plan's order, so a row
is a piece of work and the order is what the fleet agreed to do about it.

What a click does depends on which CELL of the row you hit:

  a seat        jump the tmux cursor to that seat's pane — the dashboard is a
                switcher, which is the whole reason to have it beside the seats.
                Its ✕ closes the pane; the ＋ row under the last seat adds one.
                Both go through qb-seat-click, so they mean exactly what the
                same widgets on the tmux seat bar mean
  an agent      its cwd, branch, model and session id, in the detail line
  the ⚖ or ⚒    START it: ⚖ panel-reviews a PR in a new pane of the seat row,
                beside the work it is about; ⚒ starts /fix-issue on an issue,
                including the issue behind a plan item
  the #ref      OPEN it on GitHub
  anything else EXPLAIN it in the detail line — the plan's note and what the item
                waits on (the reasoning behind its place in the order, which
                lives on the board and nowhere else), the claim note, the CI
                verdict. The default is the harmless one on purpose: a stray
                click on a 78-column pane should not spend money or take
                somebody else's work

Keys: r refresh now, o open the selected row on GitHub, p panel-review a PR,
f take an issue, s widen or narrow the scope, q quit.

It opens NARROW: the rows of the project this screen is for (`--repo`, else
QB_DASH_REPOS, else the cwd's origin), with the repo column dropped, because on a
one-project screen that column is the same word on every row and the pane is
78 columns wide (#261). `s` widens it to the whole fleet and brings the column
back; QB_DASH_SCOPE=all opens that way.

Textual requests mouse tracking from the terminal, and tmux forwards events to
a pane that asks for them — so this needs no tmux configuration beyond the
`mouse on` that makes borders draggable. Hold Shift to reach tmux's own mouse
behaviour (selecting text) instead of the app's.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.events import Click
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qbdata as qd                                             # noqa: E402


def _elsewhere(hidden: int) -> str:
    """What a narrowed panel adds to its own title, or nothing.

    Every panel that filters says so, because a panel that filtered silently is a
    panel lying about the fleet: "nothing claimed" and "nothing claimed HERE" are
    different facts, and the second is the one the reader is being shown.
    """
    return f" · {hidden} elsewhere" if hidden else ""


def holders(held: dict[str, dict]) -> dict[str, str]:
    """{'owner/repo#n' → who holds it}, which is all of a claim this dashboard shows."""
    return {n: (c.get("holder") or "?") for n, c in held.items()}


class Confirm(ModalScreen[bool]):
    """Yes/no before something expensive and outward-facing.

    A panel review costs real money, comments on a public PR and pushes a fix
    commit, so a stray click on a 78-column pane should not be able to start
    one. It shows the exact command, because the answer to "what will this do"
    should not require trusting a sentence about it. QB_DASH_CONFIRM=0 turns it
    off for anyone who wants the single click and means it.
    """

    BINDINGS = [("escape", "no", "cancel"), ("n", "no", "cancel"),
                ("y", "yes", "run"), ("enter", "yes", "run")]

    CSS = """
    Confirm { align: center middle; }
    #box { width: 90%; max-width: 70; height: auto; padding: 1 2;
           background: $panel; border: thick $accent; }
    #cmd { color: $text-muted; padding: 1 0; }
    """

    def __init__(self, prompt: str, command: str, cwd: str) -> None:
        super().__init__()
        self.prompt, self.command, self.cwd = prompt, command, cwd

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(Text(self.prompt, style="bold"))
            yield Static(Text(f"$ {self.command}\n  in {self.cwd}", style="dim"), id="cmd")
            yield Static(Text("enter/y — run     esc/n — cancel", style="bold $accent"))

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class ClickTable(DataTable):
    """A DataTable where a single click acts on the row under the pointer.

    Two things make this necessary. DataTable treats a click on any row but the
    cursor's as "move the cursor" and selects nothing, so a first click on a row
    does nothing visible. And it consumes the Click rather than letting it bubble,
    so a handler on the App never runs — this has to be on the widget itself.
    """

    def on_click(self, event: Click) -> None:
        if not self.row_count:
            return
        # The cell comes off the CLICK, not off hover_coordinate. Hover is set by
        # a separate mouse-move message, so reading it here is a race: a click
        # arriving without a preceding move — the first click into a pane, or any
        # click a test synthesises — reads whatever the last move left behind and
        # acts on the wrong cell. This is the same source DataTable itself uses.
        meta = event.style.meta
        if "row" not in meta or "column" not in meta:
            return
        row, column = meta["row"], meta["column"]
        if not 0 <= row < self.row_count:
            return                                  # the header, or past the last row
        key = self.coordinate_to_cell_key(Coordinate(row, 0)).row_key
        # The COLUMN goes with it: an action icon in its own column is how one
        # row offers more than one verb — click the ⚖ to review, the rest of the
        # row to open.
        self.app.dispatch_row(str(key.value), column)


class Dash(App):
    """Three tables and a detail line."""

    CSS = """
    Screen { background: $surface; }
    /* Hidden until the first fetch says there is something to show: an install
       with no subscription token gets no blank row. */
    #limits { height: 1; padding: 0 1; background: $panel; color: $text;
              display: none; }
    #head { height: 1; padding: 0 1; background: $panel; color: $text; }
    #detail { height: auto; min-height: 1; padding: 0 1; background: $panel;
              color: $text-muted; }
    .title { height: 1; padding: 0 1; background: $boost; color: $accent; }

    /* A share of the pane each, and each scrolls inside its share. With
       `height: auto` the tables simply stack past the bottom of a 42-row pane
       and the last of them cannot be clicked, because it is not on screen —
       which is how the click test caught it.

       WORK takes most of the pane, and should: it is the panel the dashboard is
       FOR, and merging four tables into one is what freed the rows to give it. */
    #seats { height: 1fr; }
    #fleet { height: 2fr; }
    #work  { height: 6fr; }
    """

    BINDINGS = [
        ("q", "quit", "quit"),
        ("r", "refresh_now", "refresh"),
        ("o", "open_pr", "open"),
        ("p", "panel_pr", "panel"),
        ("f", "fix_issue", "fix"),
        ("s", "toggle_scope", "scope"),
        ("question_mark", "help", "keys"),
    ]

    # WORK's columns, by index. The action icon has a column to itself so that
    # clicking it means something other than clicking the row, and the ref has one
    # so that "open this on GitHub" is a PLACE rather than a modifier: one row,
    # three verbs, each of them somewhere you can point at.
    STATE_COLUMN = 0
    KIND_COLUMN = 1
    ACTION_COLUMN = 2
    # Column 1 of the SEATS table is the ✕ that closes one — the same second cell
    # as WORK's action icon, on purpose, so "click the icon, not the row" is one
    # habit rather than two.
    KILL_COLUMN = 1
    # The ref's index MOVES, which is why it is not up here with the others: the
    # repo cell sits between the action icon and the ref and comes and goes with
    # the scope (#261). `self.ref_column` is set by build_columns, the one place
    # that knows how many columns the table has.

    def __init__(self, interval: float = 4.0, gh_interval: float = 90.0,
                 plan_interval: float = 15.0, scope: "qd.Scope | None" = None) -> None:
        super().__init__()
        # Which project's rows this is about, and whether the repo column is worth
        # its eleven columns — one object, because the two answers have to agree
        # (qbdata.Scope). `s` swaps it for its opposite; everything below asks it
        # rather than deciding for itself.
        self.scope = scope if scope is not None else qd.resolve_scope()
        self.interval = interval
        self.gh_interval = gh_interval
        # Its own clock, between the two: the plan is a call to the board rather
        # than to GitHub, so it is cheap, but it changes when a human reorders it
        # or an agent claims an item — neither of which happens every four
        # seconds.
        self.plan_interval = plan_interval
        # Slower again: a five-hour window does not move in four seconds, and
        # this one is a call to Anthropic rather than to the board.
        self.limits_interval = qd.LIMITS_EVERY
        self.limits: list[dict] = []
        self.limits_err: str | None = None
        self.client = None
        self.cfg = None
        self.rows: dict[str, dict] = {}       # row key → the record behind it
        # The last board answer, kept so `s` can redraw from it. A toggle that had
        # to wait for the next poll would look like a key that did nothing for four
        # seconds, and one that re-fetched would spend a request on a decision the
        # client had already made.
        self.board: dict = {}
        self.seats: list[dict] = []           # the seat PANES, off tmux
        # (machine, scope, seat number) -> the board's live agent. All three,
        # because neither of the first two is enough on its own: `list-panes -a` is
        # the whole tmux server and since #208 two screens can each hold a seat 1,
        # and the BOARD is the whole fleet, where two machines can each hold a
        # `seat-lexray-1`. Keyed on the number alone, one of those overwrites the
        # other and a pane is shown a state that belongs to something else.
        self.seat_states: dict[tuple[str | None, str | None, int], dict] = {}
        self.prs: list[dict] = []
        self.issues: list[dict] = []
        self.issue_err: str | None = None
        self.plan: list[dict] = []
        self.plan_err: str | None = None
        # WORK is fed by four sources on three clocks, so it is redrawn whenever
        # any of them answers — and this stops that being a REBUILD every four
        # seconds. A DataTable rebuilt between the mouse going down and the click
        # arriving moves the row out from under the pointer, and this is the panel
        # a reader dwells on.
        self.work_sig: tuple | None = None
        # Where the ref cell landed, so a click on it can be told from a click on
        # the title. build_columns owns it; this is only the value before mount.
        self.ref_column = 3
        self.held: dict = {}                  # 'owner/repo#n' → the claim on it
        self.detail_text = ""
        self.last_dispatch: tuple[str, float] | None = None
        # Where launched work runs, what it runs, and whether it asks first.
        self.repo = os.environ.get("QB_DASH_REPO") or os.getcwd()
        # WHICH repo that directory is, because /fix-issue takes a bare number
        # and resolves the repository from the checkout it runs in. The panels
        # list several repos now, so "issue #12" is not an address on its own.
        self.repo_slug = qd.repo_slug(self.repo)
        self.agent_bin = os.environ.get("QB_SEAT_AGENT", "claude")
        self.confirm = os.environ.get("QB_DASH_CONFIRM", "1") != "0"
        self.pr_err: str | None = None

    # ---- layout ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Above the board line, because it governs every pane below it: the seats
        # spend one subscription between them, and the window they are working
        # towards is the one number none of the tables can show.
        yield Static("", id="limits")
        yield Static("quarterback — connecting…", id="head")
        with Vertical():
            yield Static("SEATS", classes="title", id="t_seats")
            yield ClickTable(id="seats", cursor_type="row")
            yield Static("FLEET", classes="title", id="t_fleet")
            yield ClickTable(id="fleet", cursor_type="row", zebra_stripes=False)
            yield Static("WORK", classes="title", id="t_work")
            yield ClickTable(id="work", cursor_type="row")
        yield Static("click: seat→pane, ✕→close it, ＋→add one, #ref→GitHub, "
                     "⚖→panel review, ⚒→fix, anywhere else on a row→why   "
                     "? for keys",
                     id="detail")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#seats", DataTable).add_columns("", "✕", "seat", "state", "running", "where")
        self.build_columns()
        try:
            self.client, self.cfg = qd.board_client()
        except Exception as exc:                  # noqa: BLE001
            self.query_one("#head", Static).update(
                Text(f"no board configured: {type(exc).__name__}", style="bold red"))
            return
        self.refresh_seats()
        self.refresh_limits()
        self.refresh_board()
        self.refresh_plan()
        self.refresh_prs()
        self.refresh_issues()
        self.set_interval(self.limits_interval, self.refresh_limits)
        self.set_interval(self.interval, self.refresh_seats)
        self.set_interval(self.interval, self.refresh_board)
        self.set_interval(self.plan_interval, self.refresh_plan)
        self.set_interval(self.gh_interval, self.refresh_prs)
        self.set_interval(self.gh_interval, self.refresh_issues)

    def build_columns(self) -> None:
        """Give the two repo-bearing tables their columns, per the current scope.

        Called on mount and again on every `s`, because the repo cell is not a
        setting on a row — it is a whole column, and adding or removing one means
        rebuilding the table. `clear(columns=True)` first: without it the second
        call appends a duplicate set and every row is drawn against the wrong
        headers.

        THE ACTION ICON STAYS IN COLUMN 2, whatever the scope: `ACTION_COLUMN` and
        `KILL_COLUMN` are indices into these tables, so the column that comes and
        goes has to be the one AFTER them — click the ⚖ with the repo cell hidden
        and it must still mean review, not open. The ref is the one index that
        moves, and it is recorded here rather than worked out at click time,
        because here is where the number of columns is actually known.
        """
        repo = ("repo",) if self.scope.column else ()
        for table_id, columns in (
            ("#fleet", ("who", "state", *repo, "what", "ttl")),
            ("#work", ("", "kind", "⚒", *repo, "ref", "title", "who")),
        ):
            table = self.query_one(table_id, DataTable)
            table.clear(columns=True)
            table.add_columns(*columns)
        self.ref_column = 4 if self.scope.column else 3
        # The rows went with the columns, so nothing on screen answers to the old
        # signature any more and the next render must rebuild rather than skip.
        self.work_sig = None

    def repo_cell(self, repo: str) -> list[Text]:
        """The repo cell, or no cell at all — one place, so both tables agree.

        A list rather than an optional value because that is how it is spliced into
        a row: an empty one contributes nothing, and a row built by concatenation
        cannot get the cell count wrong the way a row built by `if` can.
        """
        return [] if not self.scope.column else [
            Text(qd.clip(repo, 11), style=qd.repo_colour(repo))]

    # ---- data (threads, so a slow board never freezes the ui) -----------

    @work(thread=True, exclusive=True, group="seats")
    def refresh_seats(self) -> None:
        seats = qd.tmux_seats()
        self.call_from_thread(self.render_seats, seats)

    @work(thread=True, exclusive=True, group="limits")
    def refresh_limits(self) -> None:
        limits, err = qd.fetch_limits()
        self.call_from_thread(self.render_limits, limits, err)

    @work(thread=True, exclusive=True, group="board")
    def refresh_board(self) -> None:
        data = qd.fetch_board(self.client)
        self.call_from_thread(self.render_board, data)

    @work(thread=True, exclusive=True, group="plan")
    def refresh_plan(self) -> None:
        items, err = qd.fetch_plan(self.client)
        self.call_from_thread(self.render_plan, items, err)

    @work(thread=True, exclusive=True, group="prs")
    def refresh_prs(self) -> None:
        prs, err = qd.fetch_prs()
        self.call_from_thread(self.render_prs, prs, err)

    @work(thread=True, exclusive=True, group="issues")
    def refresh_issues(self) -> None:
        issues, err = qd.fetch_issues()
        self.call_from_thread(self.render_issues, issues, err)

    # ---- rendering -------------------------------------------------------

    def render_seats(self, seats: list[dict]) -> None:
        """The panes of the screen this dashboard is sitting in.

        Deliberately not the same list as FLEET. FLEET is the board's answer to
        "which agents are live anywhere on the fleet"; this is tmux's answer to
        "which panes are on the screen in front of you", and only the second can
        be closed by clicking. A seat whose agent has exited still has a pane,
        shows here, and is exactly the one worth closing.
        """
        self.seats = seats
        table = self.query_one("#seats", DataTable)
        table.clear()
        # More than one screen on this server means more than one seat 1, so the
        # number stops being a name. Said only when it is true: on the ordinary
        # single-screen box "seat 1" is what the pane border and the seat bar both
        # call it, and renaming it here would be three spellings for one thing.
        screens = {s.get("session") for s in seats}
        for s in seats:
            # By PANE ID, not by seat number. Two screens each with a seat 1 gave
            # this table the same row key twice, and a DataTable raises DuplicateKey
            # rather than tolerating it — so the panel that exists to show the
            # second screen was the thing that could not survive one (#208).
            key = f"seat:{s['pane']}"
            self.rows[key] = s
            live = s.get("command") not in ("bash", "sh", "zsh", "fish", "")
            # A pane can be running an agent and still be doing nothing you want
            # to know about, or be waiting on you and look identical. `running`
            # is tmux's answer (is a process there); `state` is the agent's own.
            agent = self.seat_state(s)
            word, style = qd.agent_state(agent)
            scope = qd.pane_scope(s)
            label = f"{scope} {s['seat']}" if len(screens) > 1 and scope \
                else f"seat {s['seat']}"
            table.add_row(
                Text("●" if live else "·", style="green" if live else "grey50"),
                Text("✕", style="bold red"),                 # click to close it
                Text(qd.clip(label, 13), style="bold"),
                Text(word or "—", style=style),
                Text(qd.clip(s.get("command") or "—", 12),
                     style="white" if live else "grey50"),
                Text(qd.clip(os.path.basename(s.get("path") or "") or "—", 22),
                     style="grey50"),
                key=key,
            )
        # The ＋ is a ROW rather than a key, because the whole point of this
        # panel is that the mouse can do it. It carries a record of its own so
        # dispatch_row has something to look up — a row key with nothing behind
        # it is dropped on the floor.
        self.rows["seat:add"] = {"add": True}
        table.add_row(Text(""), Text("＋", style="bold cyan"),
                      Text("add seat", style="cyan"), Text(""), Text(""), Text(""),
                      key="seat:add")
        title = f"SEATS · {len(seats)}" if seats else "SEATS · none on this screen"
        self.query_one("#t_seats", Static).update(title)

    def render_limits(self, limits: list[dict], err: str | None) -> None:
        """Claude Code's own caps, as bars — `5h ████░░ 64% 3h57m  7d ██░ 41% 5d8h`.

        A failed call keeps the last figures rather than blanking the line: they
        are minutes old and still roughly true, and a line that vanished on every
        hiccup would read as "no limits", which is the opposite of what it means.
        An install with no subscription token has nothing here to show, and the
        row is hidden outright rather than left blank.
        """
        if limits:
            self.limits = limits
        self.limits_err = err
        try:
            bar = self.query_one("#limits", Static)
        except Exception:                         # noqa: BLE001 — a resize before mount
            return
        cells = qd.limit_cells(self.limits, max(20, self.size.width - 2))
        bar.display = bool(cells)
        if not cells:
            return
        text = Text()
        for i, (label, glyphs, pct, reset, colour) in enumerate(cells):
            if i:
                text.append("  ")
            text.append(label, style="bold grey70")
            if glyphs:
                text.append(f" {glyphs}", style=colour)
            text.append(f" {pct}", style=f"bold {colour}")
            if reset:
                text.append(f" {reset}", style="grey50")
        if err:
            text.append(" ?", style="grey50")
        bar.update(text)

    def on_resize(self) -> None:
        """Re-lay the bars to the new width — they are sized to the pane, and the
        dash pane is resized every time the screen is."""
        self.render_limits(self.limits, self.limits_err)

    def render_board(self, data: dict) -> None:
        self.board = data
        agents = sorted(data.get("agents", []),
                        key=lambda a: (a.get("repo") or "", a.get("holder") or ""))
        agents, elsewhere = qd.in_scope(agents, self.scope)

        head = self.query_one("#head", Static)
        if data.get("error"):
            head.update(Text(f"● board unreachable — {qd.clip(data['error'], 60)}",
                             style="bold red"))
        else:
            # COUNTED OVER THE ROWS BELOW, not over the whole board, and the scope
            # is named beside them so the number cannot be read as the fleet's.
            # "7 live · quarterback" next to a table holding two would be the one
            # place on this pane where the scope makes something read falsely — and
            # what is hidden is on FLEET's own title, which is where it belongs.
            seats = sum(1 for a in agents if qd.seat_number(a.get("holder")))
            head.update(Text(f"● {self.cfg.base_url}   {len(agents)} live · {seats} seats"
                             f"   {self.scope.label()}", style="green"))

        table = self.query_one("#fleet", DataTable)
        table.clear()
        for i, a in enumerate(agents):
            key = f"agent:{i}"
            self.rows[key] = a
            seat = qd.seat_number(a.get("holder"))
            who = (a.get("holder") or "?").split("/", 1)[-1]
            word, style = qd.agent_state(a)
            table.add_row(
                Text(qd.clip(who, 13), style="bold green" if seat else "bold"),
                Text(word or "—", style=style),
                *self.repo_cell(a.get("repo") or "—"),
                Text(qd.clip(a.get("title") or a.get("branch") or "—",
                             40 if self.scope.column else 52),
                     style="white" if seat else "grey70"),
                Text(qd.until(a.get("expires")), style="grey50"),
                key=key,
            )
        self.query_one("#t_fleet", Static).update(
            f"FLEET · {len(agents)}{_elsewhere(elsewhere)}")
        # Keep what the board said about each SEAT, keyed by seat number, so the
        # panel below can say what a pane is doing. The two panels answer
        # different questions from different sources — tmux knows which panes
        # exist, only the board knows what the agent in one is doing — and this
        # is the single point where they meet.
        # Stashed, not rendered: SEATS has its own refresh worker and re-entering
        # its table from this one raises DuplicateKey mid-rebuild. The state
        # appears on the next seats tick, which is seconds, and it is a state a
        # human is reading rather than a countdown.
        self.seat_states = {
            (qd.seat_machine(a.get("holder")), qd.seat_scope(a.get("holder")), n): a
            for a in agents if (n := qd.seat_number(a.get("holder"))) is not None}

        # Who holds which issue, kept for the confirmation that names the holder
        # before a click takes work somebody else already has. From EVERY claim,
        # not the ones this scope shows: an issue on this screen held by an agent
        # working out of another repo's checkout is still held, and narrowing here
        # would draw it as free and send the next seat into it.
        self.held = qd.claims_by_issue(data.get("claims", []))
        # The claims have no table of their own any more. A claim IS a row of WORK
        # — its `who` cell, or a row in its own right when what it holds is a lease
        # or a release rather than an issue — and it is redrawn from here because
        # the board clock is the fast one and a claim appearing is the change a
        # reader most wants to see arrive.
        self.render_work()

    def render_plan(self, items: list[dict], err: str | None) -> None:
        """Keep the board's plan; WORK is where it is drawn.

        The WHOLE plan is kept and the scope applied later, in render_work:
        `self.plan` is also what resolves a `plan:<uuid>` claim to a title and a
        repo, and a claim from another project must still resolve — otherwise
        widening the scope would show rows this client could no longer explain.
        """
        self.plan, self.plan_err = items, err
        self.render_work()

    def render_prs(self, prs: list[dict], err: str | None) -> None:
        self.prs, self.pr_err = prs, err
        self.render_work()

    def render_issues(self, issues: list[dict], err: str | None) -> None:
        self.issues, self.issue_err = issues, err
        self.render_work()

    def render_work(self) -> None:
        """THE table: the plan, the issues, the PRs and the claims, as one list.

        Called by all four of the things that fetch, because all four of them feed
        it, and it is the only place that knows what the pane should look like.
        The signature decides whether the call is a redraw or a rebuild, and what
        it covers is what a reader can SEE — the rows, their state, their verb,
        their right-hand cell — and NOT a claim's expiry, which changes on every
        renewal and lives on the detail line rather than in a cell. Without that,
        the board's four-second clock would rebuild the table under the pointer.

        Every source's error rides the title. Both `gh` calls and the board fail
        independently, and a table drawing three sources' rows while silently
        dropping the fourth's would be claiming to be the whole of the work when
        it was not.
        """
        rows = qd.work_rows(self.plan, self.issues, self.prs,
                            (self.board or {}).get("claims") or [],
                            qd.resolve_repos())
        rows, hidden = qd.in_scope(rows, self.scope)
        drawn = [(row["key"], qd.work_state(row), qd.work_kind(row),
                  qd.work_action(row), qd.work_ref(row, self.scope),
                  row["title"], qd.work_who(row)) for row in rows]
        errs = [e for e in (self.plan_err, self.pr_err, self.issue_err) if e]
        sig = (tuple(str(cell) for cell in drawn), self.scope.column, tuple(errs))
        if sig == self.work_sig:
            return
        self.work_sig = sig

        table = self.query_one("#work", DataTable)
        table.clear()
        width = 40 if self.scope.column else 52
        for row, (_, (glyph, colour), (kind, kind_colour), (icon, icon_colour, _),
                  ref, title, (who, who_colour)) in zip(rows, drawn):
            key = f"work:{row['key']}"
            self.rows[key] = row
            table.add_row(
                Text(glyph, style=colour),
                Text(kind, style=kind_colour),
                Text(icon, style=icon_colour),
                *self.repo_cell(qd.short_repo(row["repo"] or "fleet")),
                Text(ref, style="bold grey70"),
                Text(qd.clip(title or "—", width),
                     style="grey50" if qd.work_dim(row) else "white"),
                Text(qd.clip(who, 13), style=who_colour),
                key=key,
            )
        self.query_one("#t_work", Static).update(
            qd.work_title(rows, hidden, " · ".join(errs) or None))

    def say(self, text: str) -> None:
        # Kept on the app as well as in the widget: a Static does not hand back
        # what it was last given, and this line is the app's only visible answer
        # to "did that click do anything", so it has to be assertable.
        self.detail_text = text
        self.query_one("#detail", Static).update(Text(text, style="bold"))

    # ---- clicks ----------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """The keyboard path (Enter). Ignored when a click just did the same row."""
        key = str(event.row_key.value)
        if self.last_dispatch and self.last_dispatch[0] == key \
                and time.monotonic() - self.last_dispatch[1] < 0.5:
            return
        self.dispatch_row(key)

    def dispatch_row(self, key: str, column: int | None = None) -> None:
        record = self.rows.get(key)
        if record is None:
            return
        self.last_dispatch = (key, time.monotonic())
        kind = key.split(":", 1)[0]
        if kind == "seat":
            if record.get("add"):
                self.add_seat()
            elif column == self.KILL_COLUMN:
                self.close_seat(record)
            else:
                self.jump_pane(record)
        elif kind == "agent":
            self.click_agent(record)
        elif kind == "work":
            self.click_work(record, column)

    def click_work(self, row: dict, column: int | None) -> None:
        """One work row, three verbs, each of them in its own cell.

        The icon starts it, the ref opens it, everything else explains it. The
        EXPLANATION is the default rather than either verb, because it is the only
        one of the three that cannot cost anything: a stray click on a 78-column
        pane should not spend money on a review or take an issue somebody else is
        already on.
        """
        if column == self.ACTION_COLUMN:
            self.start_work(row)
        elif column == self.ref_column and row["number"] is not None:
            self.open_work(row)
        else:
            self.say(qd.work_detail(row))

    def start_work(self, row: dict) -> None:
        """The ⚖ or the ⚒: review the PR, or take the issue.

        Which of the two a row offers is qbdata's answer, not this method's —
        `work_action` is what drew the icon, so asking it again here is what keeps
        the icon and the click from ever disagreeing. A row offering neither says
        so rather than doing nothing, because a dim icon that swallows the click is
        indistinguishable from a broken one.
        """
        _, _, verb = qd.work_action(row)
        if verb == "panel":
            self.panel_pr(row)
            return
        if verb == "fix":
            # A plan item is not itself an issue: the number and the repo come off
            # its ref, which is the only thing /fix-issue can be pointed at.
            issue = row if row["number"] is not None \
                else qd.plan_issue(row["plan"] or {})
            if issue is not None:
                self.fix_issue(issue)
                return
        self.say(f"nothing to start on this row — {qd.work_detail(row)}")

    def open_work(self, row: dict) -> None:
        """The #ref, on GitHub. `pull` or `issues` — the number alone cannot say
        which, and GitHub redirects the wrong one to the right one only sometimes."""
        kind = "pull" if row["kind"] == "pr" else "issues"
        self.open_url(f"https://github.com/{row['repo'] or qd.REPO}"
                      f"/{kind}/{row['number']}")

    # ---- the seats ---------------------------------------------------------
    #
    # All three of these shell out to qb-seat-click rather than driving tmux
    # here, and that is the point: the seat bar in the status line and this
    # panel are two front ends onto ONE definition of what closing or adding a
    # seat means. Closing is kill-pane AND a reflow of the row; adding is
    # qb-seats --add, which knows not to reuse the number of a seat that was
    # closed. Two copies of that would drift, and the drift would show up as a
    # ragged layout or a board that thinks a new agent is an old one returning.

    def seat_click(self, tag: str, session: str, prompt: str) -> None:
        command = f"qb-seat-click {shlex.quote(tag)} {shlex.quote(session)}"
        if self.confirm:
            self.push_screen(
                Confirm(prompt, command, self.repo),
                lambda go: self.run_seat_click(tag, session) if go
                else self.say("cancelled"),
            )
        else:
            self.run_seat_click(tag, session)

    def run_seat_click(self, tag: str, session: str) -> None:
        if not os.environ.get("TMUX"):
            self.say(f"not inside tmux — nothing to click: qb-seat-click {tag}")
            return
        try:
            done = subprocess.run(["qb-seat-click", tag, session],
                                  capture_output=True, text=True, timeout=30)
        except Exception as exc:                   # noqa: BLE001
            self.say(f"could not run qb-seat-click ({type(exc).__name__})")
            return
        if done.returncode:
            self.say(qd.clip(done.stderr.strip() or f"qb-seat-click {tag} failed", 120))
        else:
            self.say(f"{tag} — done")
        self.refresh_seats()

    def seat_state(self, seat: dict) -> dict:
        """What the board says about the agent in this pane, or {}.

        NARROW, THEN NARROW AGAIN, AND NEVER GUESS. Start from every agent with
        this seat number; keep the ones in this pane's project, then the ones on
        this machine; take the survivor only if there is exactly one. Each step is
        skipped when it would leave nothing, which is what lets a pane that cannot
        say which project it is in — a screen built before `@qb_repo` — still match
        the only agent answering to its number.

        Both narrowings earn their place, and one of them is why this is not just a
        dict lookup. `list-panes -a` is the whole tmux server, so since #208 one box
        holds `zeus/seat-lexray-1` and `zeus/seat-nix-fleet-1` at once; and the
        BOARD is the whole fleet, so `zeus/seat-lexray-1` and `laptop/seat-lexray-1`
        are both on it. Either collision, resolved by taking the first, is a wrong
        answer that looks exactly like a right one.

        The machine is this host's name as the harness reads it, which is a GUESS —
        the board's machine name comes from the token map and need not be the
        hostname. It can only ever narrow a set that was already ambiguous, so a
        wrong guess costs the state cell and never fills it in with the wrong agent.
        """
        try:
            number = int(seat["seat"])
        except (KeyError, TypeError, ValueError):
            return {}
        here = getattr(self.cfg, "agent", None)
        found = [(k, a) for k, a in self.seat_states.items() if k[2] == number]
        scope = qd.pane_scope(seat)
        found = [c for c in found if c[0][1] == scope] or found
        found = [c for c in found if c[0][0] == here] or found
        return found[0][1] if len(found) == 1 else {}

    def seat_session(self) -> str | None:
        """Which screen to act on: the one the seats are in, not the cursor's.

        The dashboard is usually a pane of that same screen, but it does not
        have to be — and a --add aimed at the wrong session would build a second
        screen rather than growing this one.
        """
        for s in getattr(self, "seats", []):
            if s.get("session"):
                return s["session"]
        return None

    def close_seat(self, seat: dict) -> None:
        session = seat.get("session")
        if not session:
            self.say("that seat has no session — refresh and try again")
            return
        self.seat_click(f"kill{seat['seat']}", session,
                        f"close seat {seat['seat']}? the agent in it goes too")

    def add_seat(self) -> None:
        session = self.seat_session()
        if not session:
            self.say("no seat screen on this server — start one with qb-seats")
            return
        self.seat_click("add", session, f"add a seat to {session}?")

    def jump_pane(self, seat: dict) -> None:
        """Move the tmux cursor to a seat's pane, by pane id.

        By ID and not by seat number, because this row already knows the pane —
        jump_to_seat's search over `list-panes -a` is for the FLEET table, whose
        rows come off the board and only carry a holder name.
        """
        pane = seat.get("pane")
        if not pane or not os.environ.get("TMUX"):
            self.say(f"seat {seat.get('seat')} — {seat.get('path') or '?'}")
            return
        try:
            subprocess.run(["tmux", "select-window", "-t", pane], timeout=5)
            subprocess.run(["tmux", "select-pane", "-t", pane], timeout=5)
        except Exception as exc:                   # noqa: BLE001
            self.say(f"could not jump ({type(exc).__name__})")
            return
        self.say(f"jumped to seat {seat['seat']}")

    # ---- launching work ---------------------------------------------------

    def panel_pr(self, pr: dict) -> None:
        """Kick off /panel-review-pr for a PR, in a pane of the seat row.

        It used to open a window, on the argument that a review is not a seat
        and the row's widths mean something. Both halves are still true and the
        conclusion was still wrong: a review in a window is a review you go and
        find later, and the reason to click a PR here rather than open it on
        GitHub is to watch the thing happen. run_in_pane keeps it out of the
        seat machinery — see @qb_label there. It runs the agent the same way
        qb-seat does: the brief positionally, after `--`.
        """
        number = pr.get("number")
        command = f"{shlex.quote(self.agent_bin)} -- {shlex.quote(f'/panel-review-pr {number}')}"
        if self.confirm:
            self.push_screen(
                Confirm(f"panel-review PR #{number}?", command, self.repo),
                lambda go: self.run_in_pane(f"panel-{number}", command) if go else
                self.say("cancelled"),
            )
        else:
            self.run_in_pane(f"panel-{number}", command)

    def fix_issue(self, issue: dict) -> None:
        """Kick off /fix-issue for an issue, the same way ⚖ starts a review.

        The prompt names the holder when the board already has a claim on it:
        taking a held issue is somebody else's work redone, and that is worth a
        sentence before the click, not a rule against it — a lapsed session
        leaves a claim standing that somebody should pick up.
        """
        number = issue.get("number")
        # /fix-issue takes a bare number and reads the repository off the
        # checkout it runs in, so an issue from a repo this dashboard only
        # WATCHES cannot be started here: the number would land on whatever
        # issue wears it in the repo the window opens in, which is somebody
        # else's work being redone under the wrong title.
        repo = issue.get("repo")
        if repo and self.repo_slug and repo != self.repo_slug:
            self.say(f"#{number} is in {repo}; this dashboard runs in "
                     f"{self.repo_slug} — start it from that checkout")
            return
        command = f"{shlex.quote(self.agent_bin)} -- {shlex.quote(f'/fix-issue {number}')}"
        holder = holders(self.held).get(qd.issue_key(issue))
        prompt = f"start /fix-issue on #{number}?"
        if holder:
            prompt += f"  (held by {holder})"
        if self.confirm:
            self.push_screen(
                Confirm(prompt, command, self.repo),
                lambda go: self.run_in_window(f"fix-{number}", command) if go else
                self.say("cancelled"),
            )
        else:
            self.run_in_window(f"fix-{number}", command)

    def run_in_window(self, name: str, command: str) -> None:
        """A detached tmux window running `command`, dropping to a shell after.

        Detached (-d) so a review starting does not yank the screen away from
        whatever you were reading; `exec $SHELL` after it so the window survives
        the command and its output can still be read.
        """
        # Checked HERE, not before the confirmation: outside tmux there is still
        # a useful answer — the exact command — and a dialog that never appears
        # is also a dialog that cannot be tested.
        if not os.environ.get("TMUX"):
            self.say(f"not inside tmux — run it yourself: {command}")
            return
        shell = os.environ.get("SHELL", "/bin/bash")
        full = f"{command}; exec {shlex.quote(shell)} -i"
        try:
            done = subprocess.run(
                ["tmux", "new-window", "-d", "-n", name, "-c", self.repo, full],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as exc:                       # noqa: BLE001
            self.say(f"could not start {name}: {type(exc).__name__}")
            return
        if done.returncode:
            self.say(f"tmux refused: {qd.clip(done.stderr, 60)}")
        else:
            self.say(f"started window '{name}' — Ctrl-b n to watch it")

    def run_in_pane(self, name: str, command: str) -> None:
        """`command` in a new pane of the seat row, beside the work it is about.

        A review read in a window you have to switch to is a review you go and
        look at later; one in the row is one you watch while the seats carry on.
        That is the whole argument, and it costs the running seats some width —
        `select-layout -E` spreads the row evenly again, so the cost is shared
        rather than taken out of whichever seat happened to be split.

        The pane gets @qb_label and NOT @qb_seat, which is what keeps it out of
        the way of everything else: qb-seats' --add, qb-seat-click's reflow and
        the seat bar all select on @qb_seat, so none of them counts a review as
        a seat or offers to start an agent in it. @qb_label rather than a name of
        its own because qb-seats already labels the dash and the tape that way,
        and the pane border should read one option, not two.

        With no seat row to join — the dashboard run from a bare terminal, or a
        screen whose seats have all been closed — it falls back to a window
        rather than inventing a layout.
        """
        if not os.environ.get("TMUX"):
            self.say(f"not inside tmux — run it yourself: {command}")
            return
        seats = qd.tmux_seats()
        if not seats:
            self.run_in_window(name, command)
            return
        shell = os.environ.get("SHELL", "/bin/bash")
        full = f"{command}; exec {shlex.quote(shell)} -i"
        try:
            done = subprocess.run(
                ["tmux", "split-window", "-h", "-P", "-F", "#{pane_id}",
                 "-t", seats[0]["pane"], "-c", self.repo, full],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as exc:                       # noqa: BLE001
            self.say(f"could not start {name}: {type(exc).__name__}")
            return
        if done.returncode:
            self.say(f"tmux refused: {qd.clip(done.stderr, 60)}")
            return
        pane = done.stdout.strip()
        subprocess.run(["tmux", "set-option", "-p", "-t", pane, "@qb_label", name],
                       capture_output=True, timeout=5)
        subprocess.run(["tmux", "select-layout", "-t", pane, "-E"],
                       capture_output=True, timeout=5)
        self.say(f"'{name}' is in the seat row — Ctrl-b x closes it")

    def click_agent(self, agent: dict) -> None:
        seat = qd.seat_number(agent.get("holder"))
        if seat is not None and self.jump_to_seat(seat, qd.seat_scope(agent.get("holder"))):
            self.say(f"jumped to seat {seat} — {agent.get('holder')}")
            return
        self.say(
            f"{agent.get('holder')} · {agent.get('model') or '?'} · "
            f"{agent.get('repo') or '?'}@{agent.get('branch') or '?'} · "
            f"{agent.get('cwd') or '?'}"
        )

    def jump_to_seat(self, seat: int, scope: str | None = None) -> bool:
        """Move the tmux cursor to the pane wearing @qb_seat = seat.

        `scope` says which screen, and it has to: a FLEET row carries a board
        identity, two screens can each have a seat 1 (#208), and jumping to
        whichever tmux listed first is a jump to the wrong project half the time.
        Narrowed and never guessed, exactly as seat_state does it — a screen too
        old to carry `@qb_repo` still gets a working click when it is the only
        candidate, and two panes that cannot be told apart get none.

        No machine to narrow on here, and none wanted: every pane tmux lists is on
        this box by definition.

        Tab-separated, not space: `@qb_repo` is a filesystem path and a directory
        with a space in it made the previous split return four fields, which matched
        no seat at all.
        """
        if not os.environ.get("TMUX"):
            return False
        try:
            out = subprocess.run(
                ["tmux", "list-panes", "-a", "-F",
                 "#{pane_id}\t#{@qb_seat}\t#{@qb_repo}\t#{@qb_scope}"],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:                          # noqa: BLE001
            return False
        found = [p for p in (line.split("\t") for line in out.splitlines())
                 if len(p) == 4 and p[1] == str(seat)]
        found = [p for p in found
                 if qd.pane_scope({"repo": p[2], "scope": p[3]}) == scope] or found
        pane = found[0][0] if len(found) == 1 else None
        if pane is None:
            return False
        try:
            subprocess.run(["tmux", "select-pane", "-t", pane], timeout=5)
        except Exception:                          # noqa: BLE001
            return False
        return True

    def open_url(self, url: str) -> None:
        try:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            self.say(f"opened {url}")
        except Exception as exc:                   # noqa: BLE001
            self.say(f"could not open ({type(exc).__name__}): {url}")

    # ---- key actions -----------------------------------------------------

    def action_refresh_now(self) -> None:
        self.refresh_board()
        self.refresh_plan()
        self.refresh_prs()
        self.refresh_issues()
        self.say("refreshing…")

    def action_toggle_scope(self) -> None:
        """`s` — this project's rows, or the whole fleet's.

        Redrawn from what the client already has rather than re-fetched: the board
        answered four seconds ago and the scope is a decision about how to READ
        that answer, so a request here would spend a round trip to show the same
        rows. build_columns drops the work signature on its way past, which is what
        stops the redraw being skipped as "nothing changed" — the rows did not
        change, but which of them belong on the pane did.
        """
        self.scope = self.scope.widened()
        self.build_columns()
        if self.board:
            self.render_board(self.board)          # which redraws WORK in turn
        else:
            self.render_work()
        self.say(f"scope: {self.scope.label()}"
                 + ("" if self.scope.on else " — s to narrow to this screen's"))

    def action_panel_pr(self) -> None:
        """`p` — panel-review the selected row, if it is a PR."""
        row = self.selected_row("#work")
        if row is None:
            return
        if row["kind"] == "pr":
            self.panel_pr(row)
        else:
            self.say(f"not a PR — p reviews a PR, f takes an issue · "
                     f"{qd.work_detail(row)}")

    def action_fix_issue(self) -> None:
        """`f` — take the selected row's issue, whether the row came from the plan
        or from GitHub. A PR row has no issue to take, and says so."""
        row = self.selected_row("#work")
        if row is None:
            return
        if qd.work_action(row)[2] == "fix":
            self.start_work(row)
        else:
            self.say(f"nothing to fix on this row — {qd.work_detail(row)}")

    def action_help(self) -> None:
        self.say("o open the selected row on GitHub · p panel-review a PR · f take "
                 "an issue · s this project's rows or the whole fleet's · r refresh "
                 "· q quit · click ⚖ to review, ⚒ to fix, a #ref to open, anywhere "
                 "else on a row for why it is there, a seat to jump to its pane")

    def selected_row(self, table_id: str) -> dict | None:
        table = self.query_one(table_id, DataTable)
        if not table.row_count:
            return None
        row = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        return self.rows.get(str(row.value))

    def action_open_pr(self) -> None:
        """`o` — the selected row on GitHub. A row with no number is not a thing
        GitHub has a page for, so it explains itself instead."""
        row = self.selected_row("#work")
        if row is None:
            return
        if row["number"] is not None:
            self.open_work(row)
        else:
            self.say(qd.work_detail(row))


def main(argv: list[str] | None = None) -> int:
    """The flags, which are the same two the plain renderer takes.

    Both renderers are launched by the same `qb-dash` wrapper and put in a pane by
    the same `QB_SEATS_DASH`, so a screen that can be pointed at a project one way
    has to be pointable the other way too. Everything else about this app is keys.
    """
    ap = argparse.ArgumentParser(prog="qb-dash-tui",
                                 description="the fleet dashboard, clickable")
    ap.add_argument("--scope", choices=("repo", "all"), default=None,
                    help="repo (default): only this screen's repos, and no repo column; "
                         "all: every repo on the board. `s` toggles it live")
    ap.add_argument("--repo", action="append", metavar="PATH|OWNER/NAME",
                    help="the project this screen is for — a checkout or a slug, "
                         "repeatable. Overrides QB_DASH_REPOS and the cwd")
    args = ap.parse_args(argv)
    # Before the app reads it: resolve_repos is cached, and what asks it directly
    # (the plan's ordering, the `gh` calls, and the ⚒ that needs a slug to fix an
    # issue in) would otherwise still be watching the cwd.
    if args.repo:
        try:
            qd.set_repos([qd.repo_arg(r) for r in args.repo])
        except ValueError as exc:
            print(f"qb-dash-tui: --repo {exc}", file=sys.stderr)
            return 2
    scope = qd.resolve_scope(on=None if args.scope is None else args.scope == "repo")
    Dash(scope=scope).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
