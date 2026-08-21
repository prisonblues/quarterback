#!/usr/bin/env python3
"""qb-dash-tui — the fleet dashboard, clickable.

Same five views as qb-dash (fleet / claims / plan / PRs / issues), but as a
Textual app, so rows respond to the mouse. What a click does depends on what
you clicked:

  a seat        jump the tmux cursor to that seat's pane — the dashboard is a
                switcher, which is the whole reason to have it beside the seats.
                Its ✕ closes the pane; the ＋ row under the last seat adds one.
                Both go through qb-seat-click, so they mean exactly what the
                same widgets on the tmux seat bar mean
  an agent      its cwd, branch, model and session id, in the detail line
  a claim       the claim note, which is where an agent says what it is doing
  a plan item   its plan, its note and what it waits on — the reasoning behind
                its place in the order, which lives on the board and nowhere
                else — or its ⚒, to start /fix-issue on the issue behind it
  a PR          open it on GitHub — or its ⚖, to start /panel-review-pr on it
                in a new pane of the seat row, beside the work it is about
  an issue      open it on GitHub — or its ⚒, to start /fix-issue on it

Keys: r refresh now, o open the selected PR, s widen or narrow the scope, q quit.

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

    def add_row(self, *cells, key: str | None = None, **kwargs):
        """`DataTable.add_row`, except a key this table already holds is
        suffixed rather than raised on.

        DataTable answers a repeated key with DuplicateKey, which does not
        degrade the row — it takes the whole dashboard down, and this is the
        component a human looks at when something is ALREADY wrong. So it is
        the one that must survive unexpected input rather than replace six
        panels with a traceback (#209).

        Every panel below computes a key it believes is unique, and after #208
        and #209 those keys are right. This is the backstop for the next panel,
        whose duplicates nobody has thought of yet: two rows that collide are
        kept as two rows, the second under a `~2` key.

        **Degrading is not the same as reporting, and this had to be said out
        loud.** A row key is never rendered, so the `~2` is invisible; and in
        the case the backstop was written for — two plan items arriving with no
        `item_id` — two rows is also exactly what CORRECT data looks like. Left
        at that, a keying bug this once crashed loudly would now produce nothing
        at all. So the collision is written to the app log, which is the only
        place it can be reported from.

        Returns the key actually used. Callers must file their record under
        THAT — `dispatch_row` looks a row up by key, so a suffixed row would
        otherwise display fine and do nothing when clicked.
        """
        if key is not None:
            # Compared on the string rather than by constructing a RowKey: the
            # key type is Textual's private business and this survives it
            # changing. The tables here are tens of rows, not thousands.
            taken = {rk.value for rk in self.rows}
            if key in taken:
                n = 2
                while f"{key}~{n}" in taken:
                    n += 1
                asked, key = key, f"{key}~{n}"
                try:
                    self.log.warning(
                        f"{self.id or type(self).__name__}: duplicate row key "
                        f"{asked!r} — kept as {key!r}. The panel's key is not "
                        f"unique across every repo and screen it can show.")
                except Exception:                   # noqa: BLE001
                    # The backstop exists so this table cannot take the
                    # dashboard down. A logger that is not there (no running
                    # app) must not be the thing that finally does.
                    pass
        return super().add_row(*cells, key=key, **kwargs)

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
    """Five tables and a detail line."""

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
       `height: auto` the four tables simply stack past the bottom of a 42-row
       pane: the PRs then cannot be clicked, because they are not on screen —
       which is how the click test caught it. */
    #seats  { height: 1fr; }
    #fleet  { height: 2fr; }
    #claims { height: 1fr; }
    #plan   { height: 2fr; }
    #prs    { height: 2fr; }
    #issues { height: 2fr; }
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

    # The ⚖ lives in its own column so that clicking it means something
    # different from clicking the row. Column 1 of the PR table.
    PANEL_COLUMN = 1
    # And column 1 of the SEATS table is the ✕ that closes one. Same column
    # everywhere on purpose: the action icon is always the second cell, so
    # "click the icon, not the row" is one habit rather than five.
    KILL_COLUMN = 1
    # The same trick on the issue table: column 1 is the ⚒ that takes the issue.
    # The plan table puts its ⚒ in the same column, for the same reason and with
    # the same meaning — a plan item that points at an issue is an issue you can
    # take, and having it in one place means one thing to learn.
    FIX_COLUMN = 1

    def __init__(self, interval: float = 4.0, gh_interval: float = 90.0,
                 plan_interval: float = 15.0, scope: "qd.Scope | None" = None,
                 repo: str | None = None) -> None:
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
        self.plan_sig: tuple | None = None
        self.held: dict = {}                  # 'owner/repo#n' → the claim on it
        self.detail_text = ""
        self.last_dispatch: tuple[str, float] | None = None
        # Where launched work runs, what it runs, and whether it asks first.
        # A DIRECTORY, and that is why `--repo` cannot simply be assigned to it:
        # the flag takes a slug as readily as a checkout, and a slug names a repo
        # this process may have no checkout of. `--repo <checkout>` therefore moves
        # this too — the point of pointing a screen at a project is that the ⚒
        # starts work IN it — while `--repo <slug>` leaves it where it was and the
        # guards below refuse the rows it cannot reach, out loud.
        self.repo = repo or os.environ.get("QB_DASH_REPO") or os.getcwd()
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
            yield Static("CLAIMED", classes="title", id="t_claims")
            yield ClickTable(id="claims", cursor_type="row")
            yield Static("PLANS", classes="title", id="t_plan")
            yield ClickTable(id="plan", cursor_type="row")
            yield Static("OPEN PRs", classes="title", id="t_prs")
            yield ClickTable(id="prs", cursor_type="row")
            yield Static("ISSUES", classes="title", id="t_issues")
            yield ClickTable(id="issues", cursor_type="row")
        yield Static("click: seat→pane, ✕→close it, ＋→add one, PR→GitHub, "
                     "plan row→why, ⚖→panel review, ⚒→fix issue   ? for keys",
                     id="detail")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#seats", DataTable).add_columns("", "✕", "seat", "state", "running", "where")
        self.query_one("#claims", DataTable).add_columns("who", "key", "left")
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
        """Give the four repo-bearing tables their columns, per the current scope.

        Called on mount and again on every `s`, because the repo cell is not a
        setting on a row — it is a whole column, and adding or removing one means
        rebuilding the table. `clear(columns=True)` first: without it the second
        call appends a duplicate set and every row is drawn against the wrong
        headers.

        THE ACTION ICONS STAY IN COLUMN 1. `PANEL_COLUMN`, `FIX_COLUMN` and
        `KILL_COLUMN` are indices into these tables, so the column that comes and
        goes has to be the one AFTER them — click the ⚖ with the repo cell hidden
        and it must still mean review, not open.
        """
        repo = ("repo",) if self.scope.column else ()
        for table_id, columns in (
            ("#fleet", ("who", "state", *repo, "what", "ttl")),
            ("#plan", ("", "⚒", *repo, "ref", "title", "who")),
            ("#prs", ("", "⚖", *repo, "pr", "title", "age")),
            ("#issues", ("", "⚒", *repo, "issue", "title", "who")),
        ):
            table = self.query_one(table_id, DataTable)
            table.clear(columns=True)
            table.add_columns(*columns)

    def repo_cell(self, repo: str) -> list[Text]:
        """The repo cell, or no cell at all — one place, so the four tables agree.

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
            live = s.get("command") not in ("bash", "sh", "zsh", "fish", "")
            # A pane can be running an agent and still be doing nothing you want
            # to know about, or be waiting on you and look identical. `running`
            # is tmux's answer (is a process there); `state` is the agent's own.
            agent = self.seat_state(s)
            word, style = qd.agent_state(agent)
            scope = qd.pane_scope(s)
            label = f"{scope} {s['seat']}" if len(screens) > 1 and scope \
                else f"seat {s['seat']}"
            key = table.add_row(
                Text("●" if live else "·", style="green" if live else "grey50"),
                Text("✕", style="bold red"),                 # click to close it
                Text(qd.clip(label, 13), style="bold"),
                Text(word or "—", style=style),
                Text(qd.clip(s.get("command") or "—", 12),
                     style="white" if live else "grey50"),
                Text(qd.clip(os.path.basename(s.get("path") or "") or "—", 22),
                     style="grey50"),
                key=key,
            ).value
            self.rows[str(key)] = s
        # The ＋ is a ROW rather than a key, because the whole point of this
        # panel is that the mouse can do it. It carries a record of its own so
        # dispatch_row has something to look up — a row key with nothing behind
        # it is dropped on the floor. Filed under the key add_row RETURNS like
        # every other row here: `seat:add` cannot collide with a `seat:%12`
        # today, but "this one call site is the exception" is how the rule
        # above stops being a rule.
        add_key = table.add_row(Text(""), Text("＋", style="bold cyan"),
                                Text("add seat", style="cyan"),
                                Text(""), Text(""), Text(""),
                                key="seat:add").value
        self.rows[str(add_key)] = {"add": True}
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
        every = sorted(data.get("agents", []),
                       key=lambda a: (a.get("repo") or "", a.get("holder") or ""))
        agents, elsewhere = qd.in_scope(every, self.scope)

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
            seat = qd.seat_number(a.get("holder"))
            who = (a.get("holder") or "?").split("/", 1)[-1]
            word, style = qd.agent_state(a)
            key = table.add_row(
                Text(qd.clip(who, 13), style="bold green" if seat else "bold"),
                Text(word or "—", style=style),
                *self.repo_cell(a.get("repo") or "—"),
                # The mark goes on the cell the dropped column widened, and marks
                # the row the scope KEPT without being able to attribute it: with
                # the repo cell gone, an agent outside any checkout otherwise reads
                # as one working here (qbdata.scope_mark).
                Text(qd.scope_mark(self.scope, a.get("repo"))
                     + qd.clip(a.get("title") or a.get("branch") or "—",
                               40 if self.scope.column else 50),
                     style="white" if seat else "grey70"),
                Text(qd.until(a.get("expires")), style="grey50"),
                key=key,
            ).value
            self.rows[str(key)] = a
        self.query_one("#t_fleet", Static).update(
            f"FLEET · {len(agents)}{qd.elsewhere(elsewhere)}")
        # Keep what the board said about each SEAT, keyed by seat number, so the
        # panel below can say what a pane is doing. The two panels answer
        # different questions from different sources — tmux knows which panes
        # exist, only the board knows what the agent in one is doing — and this
        # is the single point where they meet.
        # Stashed, not rendered: SEATS has its own refresh worker and re-entering
        # its table from this one raises DuplicateKey mid-rebuild. The state
        # appears on the next seats tick, which is seconds, and it is a state a
        # human is reading rather than a countdown.
        # FROM EVERY AGENT, not the ones this scope shows. SEATS is deliberately not
        # FLEET: `tmux_seats()` lists every seat pane on the whole tmux server, so a
        # pane belonging to another project's screen is on that panel either way, and
        # narrowing here would leave its `state` cell reading `—` — which is how a
        # reader sees which seat is waiting on them.
        self.seat_states = {
            (qd.seat_machine(a.get("holder")), qd.seat_scope(a.get("holder")), n): a
            for a in every if (n := qd.seat_number(a.get("holder"))) is not None}

        claims = sorted(data.get("claims", []), key=lambda c: c.get("expires") or "")
        ctable = self.query_one("#claims", DataTable)
        ctable.clear()
        # A claim's repo is in its KEY and nowhere else, and `plan:<uuid>` names an
        # item rather than a repo — hence the plan alongside it, and hence a claim
        # neither can attribute staying put (see qbdata.claim_repo).
        shown, claims_elsewhere = qd.in_scope(
            claims, self.scope, lambda c: qd.claim_repo(c.get("key"), self.plan))
        for i, c in enumerate(shown):
            key = f"claim:{i}"
            left = qd.minutes_left(c.get("expires"))
            key = ctable.add_row(
                Text(qd.clip((c.get("holder") or "?").split("/", 1)[-1], 13), style="bold"),
                Text(qd.clip(qd.claim_label(c.get("key") or "?", self.plan, self.scope), 34),
                     style="yellow" if c.get("kind") == "issue" else "grey70"),
                Text(qd.until(c.get("expires")),
                     style="red" if left is not None and left < 10 else "grey50"),
                key=key,
            ).value
            self.rows[str(key)] = c
        self.query_one("#t_claims", Static).update(
            f"CLAIMED · {len(shown)}{qd.elsewhere(claims_elsewhere)}")

        # Who holds which issue comes off the same claims, and only the holder
        # is displayed — so compare on that, not on the whole claim. A claim
        # renewing changes its expiry every time, and rebuilding the issue table
        # for that would move the cursor out from under a click.
        # From every claim, NOT the ones this scope shows: an issue on this screen,
        # held by an agent working out of another repo's checkout, is still held.
        # Narrowing here would draw it as free and send the next seat into it.
        held = qd.claims_by_issue(claims)
        if holders(held) != holders(self.held):
            self.held = held
            self.render_issues(self.issues, self.issue_err)

    def render_plan(self, items: list[dict], err: str | None) -> None:
        """The board's plan — every repo's list — running items at the top.

        Rebuilt only when the plan actually changed. The other tables can be
        redrawn on a clock because their rows carry a countdown, but this one is
        the panel a reader dwells on, and a rebuild between the mouse going down
        and the click arriving moves the row out from under the pointer.
        """
        # The WHOLE plan is kept, and the scope applied after: `self.plan` is what
        # resolves a `plan:<uuid>` claim to a title and a repo, and a claim from
        # another project must still resolve — otherwise widening the scope would
        # show rows this client can no longer explain.
        self.plan, self.plan_err = items, err
        repos = qd.resolve_repos()
        items, hidden = qd.in_scope(items, self.scope)
        ordered = qd.sort_plan(items, repos)
        # THE HIDDEN COUNT IS PART OF THE SIGNATURE, because it is part of the title.
        # Computed from the visible rows alone, the signature cannot see another
        # repo's items being added or removed, and the early return then leaves a
        # stale "N elsewhere" on a panel whose own rows really are unchanged — the
        # same defect `action_toggle_scope` drops `plan_sig` to avoid, on the poll
        # path rather than the keypress one.
        #
        # The covering holder is in it as well as the item's own: a plan claim
        # landing changes the glyph, the band and the whole right-hand column of
        # every item in that plan, and a signature blind to it would leave those
        # rows advertising free work until something else moved.
        sig = (hidden, tuple((i.get("item_id"), (i.get("claim") or {}).get("holder"),
                              (i.get("covered_by") or {}).get("holder"),
                              len(i.get("blocked_by") or []), i.get("updated"))
                             for i in ordered))
        if sig == self.plan_sig and not err:
            return
        self.plan_sig = sig

        table = self.query_one("#plan", DataTable)
        table.clear()
        for item in ordered:
            glyph, colour = qd.plan_state(item)
            who, who_colour = qd.plan_who(item)
            issue = qd.plan_issue(item, repos)
            takeable = (issue is not None and not qd.plan_holder(item)
                        and self.wrong_repo(issue.get("repo"), "") is None)
            key = table.add_row(
                Text(glyph, style=colour),
                Text("⚒", style="bold cyan" if takeable else "grey30"),
                *self.repo_cell(qd.short_repo(item.get("repo") or "fleet")),
                Text(qd.plan_ref(item), style="bold grey70"),
                # A fleet-wide item has no repo to name, and with the column gone
                # it would read as one of this project's (qbdata.scope_mark).
                Text(qd.scope_mark(self.scope, item.get("repo"))
                     + qd.clip(item.get("title"), 42 if self.scope.column else 52),
                     style="grey50" if colour == "grey50" else "white"),
                Text(qd.clip(who, 13), style=who_colour),
                key=f"plan:{item.get('item_id')}",
            ).value
            self.rows[str(key)] = item
        running, blocked = qd.plan_counts(items)
        title = f"PLANS · {len(items)} open"
        if running:
            title += f" · {running} running"
        if blocked:
            title += f" · {blocked} blocked"
        title += qd.elsewhere(hidden)
        if err:
            title += f" · board: {qd.clip(err, 24)}"
        self.query_one("#t_plan", Static).update(title)

    def render_prs(self, prs: list[dict], err: str | None) -> None:
        self.prs, self.pr_err = prs, err
        table = self.query_one("#prs", DataTable)
        table.clear()
        red = 0
        for pr in sorted(prs, key=lambda p: -p.get("number", 0)):
            glyph, colour = qd.ci_state(pr)
            red += colour == "red"
            # By repo AND number. Two watched repos both reach #42 eventually,
            # and the bare number handed this table the same row key twice (#209).
            key = f"pr:{qd.repo_ref(pr)}"
            # Dimmed where the guard would refuse it: an icon that looks clickable
            # and then explains itself is the "drawn takeable, refused one by one"
            # this scope work exists to end, one panel over.
            reachable = self.wrong_repo(pr.get("repo"), "") is None
            key = table.add_row(
                Text(glyph, style=colour),
                Text("⚖", style="bold cyan" if reachable else "grey30"),
                *self.repo_cell(qd.short_repo(pr.get("repo") or qd.REPO)),
                Text(f"#{pr.get('number')}", style="bold grey70"),
                Text(qd.clip(pr.get("title"), 44 if self.scope.column else 56),
                     style="grey50" if pr.get("isDraft") else "white"),
                Text(qd.ago(pr.get("updatedAt")), style="grey50"),
                key=key,
            ).value
            self.rows[str(key)] = pr
        title = f"OPEN PRs · {len(prs)}" + (f" · {red} red" if red else "")
        if err:
            title += f" · gh: {qd.clip(err, 24)}"
        self.query_one("#t_prs", Static).update(title)

    def render_issues(self, issues: list[dict], err: str | None) -> None:
        """Open issues, free ones first, the held ones greyed and named.

        A free issue is the one a seat should take next, so it is what this
        panel is for: the ⚒ on its row starts /fix-issue on it.
        """
        self.issues, self.issue_err = issues, err
        table = self.query_one("#issues", DataTable)
        table.clear()
        free = 0
        for issue in qd.sort_issues(issues, self.held):
            number = issue.get("number")
            claim = self.held.get(qd.issue_key(issue))
            holder = (claim.get("holder") or "?") if claim else None
            free += holder is None
            key = f"issue:{qd.repo_ref(issue)}"     # repo AND number (#209)
            reachable = self.wrong_repo(issue.get("repo"), "") is None
            key = table.add_row(
                Text("·" if holder else "○", style="grey50" if holder else "green"),
                Text("⚒", style="bold cyan" if reachable and not holder else "grey30"),
                *self.repo_cell(qd.short_repo(issue.get("repo") or qd.REPO)),
                Text(f"#{number}", style="bold grey70"),
                Text(qd.clip(issue.get("title"), 44 if self.scope.column else 56),
                     style="grey50" if holder else "white"),
                Text(qd.clip(holder.split("/", 1)[-1], 13) if holder
                     else qd.ago(issue.get("updatedAt")),
                     style="yellow" if holder else "grey50"),
                key=key,
            ).value
            self.rows[str(key)] = issue
        title = f"ISSUES · {len(issues)}" + (f" · {free} free" if issues else "")
        if err:
            title += f" · gh: {qd.clip(err, 24)}"
        self.query_one("#t_issues", Static).update(title)

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
        elif kind == "claim":
            self.say(qd.clip(record.get("note") or "(no note on this claim)", 400))
        elif kind == "pr":
            if column == self.PANEL_COLUMN:
                self.panel_pr(record)
            else:
                self.open_pr(record)
        elif kind == "plan":
            if column == self.FIX_COLUMN:
                self.fix_plan_item(record)
            else:
                self.say(qd.plan_detail(record))
        elif kind == "issue":
            if column == self.FIX_COLUMN:
                self.fix_issue(record)
            else:
                self.open_issue(record)

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

    def wrong_repo(self, repo: str | None, what: str) -> str | None:
        """Why ``what`` cannot be started from this dashboard, or None if it can.

        BOTH launchers need this and for the same reason. `/fix-issue` and
        `/panel-review-pr` take a bare number and resolve the repository from the
        checkout the pane opens in, while this dashboard WATCHES repos it may have
        no checkout of — so a number off another repo's row lands on whatever issue
        or PR wears it here. For a review that is a comment and a pushed fix commit
        on a stranger's pull request, which is the one click on this pane that
        cannot be taken back; `--repo <checkout>` is how a screen gets to start
        that project's work, because it moves the directory too.

        **A guard that cannot tell refuses.** `self.repo_slug` is None whenever
        `git remote get-url origin` came back empty — a checkout whose remote is
        `upstream` (the fork case this feature's own slug comparison was written
        for), no `git` on PATH, a timeout — and reading that as "nothing to check"
        made this return None for EVERY row, silently. `gh` and `git push` resolve
        a default remote without consulting `origin`, so the review would have gone
        out anyway, against whatever that remote points at. The cost of failing
        closed is a message on a click; the cost of failing open is a comment and a
        commit on a stranger's PR.
        """
        if not repo:
            return None
        if not self.repo_slug:
            return (f"cannot tell which repo {self.repo} is — no origin remote, so "
                    f"{what} cannot be aimed from here. Set QB_DASH_REPO, or start "
                    "it from that checkout")
        # Case-folded, like every other repo comparison this feature added: `repo`
        # arrives in GitHub's canonical casing and `repo_slug` in whatever the origin
        # URL was typed as, and `PrisonBlues/quarterback` is a working remote.
        if repo.strip().lower() == self.repo_slug.strip().lower():
            return None
        return (f"{what} is in {repo}; this dashboard runs in {self.repo_slug} "
                "— start it from that checkout")

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
        # The same refusal the ⚒ on an issue row makes, for the same reason and
        # with more at stake. /panel-review-pr takes a bare number and resolves
        # the repository from the checkout it runs in, so a PR from a repo this
        # dashboard only WATCHES would review whatever wears that number HERE —
        # and this one spends money, comments on a public PR and pushes a fix
        # commit to it. Until #209 that click was unreachable, because a second
        # repo sharing a number crashed the panel before anything rendered; now
        # both rows are there, so the guard has to be too.
        if (why := self.wrong_repo(pr.get("repo"), f"PR #{number}")):
            self.say(why)
            return
        command = f"{shlex.quote(self.agent_bin)} -- {shlex.quote(f'/panel-review-pr {number}')}"
        if self.confirm:
            self.push_screen(
                Confirm(f"panel-review PR #{number}?", command, self.repo),
                lambda go: self.run_in_pane(f"panel-{number}", command) if go else
                self.say("cancelled"),
            )
        else:
            self.run_in_pane(f"panel-{number}", command)

    def fix_plan_item(self, item: dict) -> None:
        """The ⚒ on a plan row: take the issue the item points at.

        Most plan items point at nothing — a line of plan is not an issue — and
        one that is already claimed is somebody's current work. Both say so
        rather than doing nothing, because a dim icon that swallows the click is
        indistinguishable from a broken one.
        """
        issue = qd.plan_issue(item)
        if issue is None:
            self.say(f"no issue behind this item — {qd.clip(item.get('title'), 60)}")
            return
        # An item inside somebody else's HELD PLAN is taken too, and this is the
        # click that spends money on it. `claim` alone let the ⚒ start /fix-issue
        # on a line of a plan another agent had reserved as a unit — the exact
        # duplicated work the plan claim exists to prevent, from the panel that
        # exists to show who is on what.
        holder = qd.plan_holder(item)
        if holder:
            who = holder.get("holder") or "?"
            self.say(f"#{issue['number']} is already being worked by {who}"
                     if item.get("claim") else
                     f"#{issue['number']} is inside a plan {who} holds — talk to "
                     f"them rather than taking one line out of it")
            return
        self.fix_issue(issue)

    def fix_issue(self, issue: dict) -> None:
        """Kick off /fix-issue for an issue, the same way ⚖ starts a review.

        The prompt names the holder when the board already has a claim on it:
        taking a held issue is somebody else's work redone, and that is worth a
        sentence before the click, not a rule against it — a lapsed session
        leaves a claim standing that somebody should pick up.
        """
        number = issue.get("number")
        # Somebody else's work redone under the wrong title, if this is skipped —
        # see wrong_repo, which the ⚖ shares because the mistake is the same one.
        if (why := self.wrong_repo(issue.get("repo"), f"#{number}")):
            self.say(why)
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

    def open_pr(self, pr: dict) -> None:
        self.open_url(f"https://github.com/{pr.get('repo') or qd.REPO}/pull/{pr.get('number')}")

    def open_issue(self, issue: dict) -> None:
        self.open_url(f"https://github.com/{issue.get('repo') or qd.REPO}"
                      f"/issues/{issue.get('number')}")

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
        answered four seconds ago and the scope is a decision about how to READ that
        answer, so a request here would spend a round trip to show the same rows.
        The plan is the exception in one detail — it redraws only when its contents
        changed, so its signature has to be dropped or the toggle would leave the
        one panel that was already on screen exactly as it was.
        """
        self.scope = self.scope.toggled()
        self.build_columns()
        self.plan_sig = None
        if self.board:
            self.render_board(self.board)
        self.render_plan(self.plan, self.plan_err)
        self.render_prs(self.prs, self.pr_err)
        self.render_issues(self.issues, self.issue_err)
        self.say(f"scope: {self.scope.label()}"
                 + ("" if self.scope.on else " — s to narrow to this screen's"))

    def action_panel_pr(self) -> None:
        record = self.selected_pr()
        if record:
            self.panel_pr(record)

    def action_fix_issue(self) -> None:
        """`f` takes whatever the table you are in offers: an issue, or the issue
        behind a plan item."""
        if getattr(self.focused, "id", None) == "plan":
            record = self.selected_row("#plan")
            if record:
                self.fix_plan_item(record)
            return
        record = self.selected_row("#issues")
        if record:
            self.fix_issue(record)

    def action_help(self) -> None:
        self.say("o open on GitHub · p panel-review · f fix the selected issue or "
                 "plan item · s this project's rows or the whole fleet's · r refresh · "
                 "q quit · click ⚖ to review, ⚒ to fix, a plan row for why it is "
                 "there, a seat to jump to its pane")

    def selected_row(self, table_id: str) -> dict | None:
        table = self.query_one(table_id, DataTable)
        if not table.row_count:
            return None
        row = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        return self.rows.get(str(row.value))

    def selected_pr(self) -> dict | None:
        return self.selected_row("#prs")

    def action_open_pr(self) -> None:
        """`o` opens whatever is selected in the table you are in."""
        if getattr(self.focused, "id", None) == "issues":
            record = self.selected_row("#issues")
            if record:
                self.open_issue(record)
            return
        record = self.selected_pr()
        if record:
            self.open_pr(record)


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
                         "all: every repo the board knows, in FLEET/CLAIMED/PLANS — "
                         "PRs and issues stay the watched repos' either way. "
                         "`s` toggles it live; QB_DASH_SCOPE sets the opening view")
    ap.add_argument("--repo", action="append", metavar="PATH|OWNER/NAME",
                    help="the project this screen is for — a checkout, which also "
                         "becomes where the ⚒ and ⚖ start work, or an owner/name "
                         "slug, which only filters. Repeatable; overrides "
                         "QB_DASH_REPOS, QB_DASH_REPO and the cwd")
    args = ap.parse_args(argv)
    # Two answers out of one flag, and BOTH are needed. `set_repos` redirects what
    # the panels draw (resolve_repos is cached, and the plan's ordering and the `gh`
    # calls ask it directly). `repo` redirects where the ⚒ and ⚖ RUN — a checkout
    # can move that and a slug cannot, and getting only the first half meant the
    # rows of the named repo were drawn as takeable and then refused one by one.
    repo = None
    if args.repo:
        try:
            targets = [qd.repo_target(r) for r in args.repo]
        except ValueError as exc:
            print(f"qb-dash-tui: --repo {exc}", file=sys.stderr)
            return 2
        qd.set_repos([slug for slug, _ in targets])
        # The first checkout named wins, because the launchers take one directory
        # and `--repo` is repeatable: a screen watching three repos still runs its
        # work somewhere, and the first one asked for is the one to mean it about.
        repo = next((path for _, path in targets if path), None)
    scope = qd.resolve_scope(on=None if args.scope is None else args.scope == "repo")
    Dash(scope=scope, repo=repo).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
