#!/usr/bin/env python3
"""qb-dash-tui — the fleet dashboard, clickable.

Same five views as qb-dash (fleet / claims / plan / PRs / issues), but as a
Textual app, so rows respond to the mouse. What a click does depends on what
you clicked:

  a seat        jump the tmux cursor to that seat's pane — the dashboard is a
                switcher, which is the whole reason to have it beside the seats
  an agent      its cwd, branch, model and session id, in the detail line
  a claim       the claim note, which is where an agent says what it is doing
  a plan item   its phase, its note and what it waits on — the reasoning behind
                its place in the order, which lives on the board and nowhere
                else — or its ⚒, to start /fix-issue on the issue behind it
  a PR          open it on GitHub
  an issue      open it on GitHub — or its ⚒, to start /fix-issue on it

Keys: r refresh now, o open the selected PR, q quit.

Textual requests mouse tracking from the terminal, and tmux forwards events to
a pane that asks for them — so this needs no tmux configuration beyond the
`mouse on` that makes borders draggable. Hold Shift to reach tmux's own mouse
behaviour (selecting text) instead of the app's.
"""

from __future__ import annotations

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
                 plan_interval: float = 15.0) -> None:
        super().__init__()
        self.interval = interval
        self.gh_interval = gh_interval
        # Its own clock, between the two: the plan is a call to the board rather
        # than to GitHub, so it is cheap, but it changes when a human reorders it
        # or an agent claims an item — neither of which happens every four
        # seconds.
        self.plan_interval = plan_interval
        self.client = None
        self.cfg = None
        self.rows: dict[str, dict] = {}       # row key → the record behind it
        self.seats: list[dict] = []           # the seat PANES, off tmux
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
        self.query_one("#seats", DataTable).add_columns("", "✕", "seat", "running", "where")
        self.query_one("#fleet", DataTable).add_columns("who", "repo", "what", "ttl")
        self.query_one("#claims", DataTable).add_columns("who", "key", "left")
        self.query_one("#plan", DataTable).add_columns(
            "", "⚒", "repo", "ref", "title", "who")
        self.query_one("#prs", DataTable).add_columns("", "⚖", "repo", "pr", "title", "age")
        self.query_one("#issues", DataTable).add_columns("", "⚒", "repo", "issue", "title", "who")
        try:
            self.client, self.cfg = qd.board_client()
        except Exception as exc:                  # noqa: BLE001
            self.query_one("#head", Static).update(
                Text(f"no board configured: {type(exc).__name__}", style="bold red"))
            return
        self.refresh_seats()
        self.refresh_board()
        self.refresh_plan()
        self.refresh_prs()
        self.refresh_issues()
        self.set_interval(self.interval, self.refresh_seats)
        self.set_interval(self.interval, self.refresh_board)
        self.set_interval(self.plan_interval, self.refresh_plan)
        self.set_interval(self.gh_interval, self.refresh_prs)
        self.set_interval(self.gh_interval, self.refresh_issues)

    # ---- data (threads, so a slow board never freezes the ui) -----------

    @work(thread=True, exclusive=True, group="seats")
    def refresh_seats(self) -> None:
        seats = qd.tmux_seats()
        self.call_from_thread(self.render_seats, seats)

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
        for s in seats:
            key = f"seat:{s['seat']}"
            self.rows[key] = s
            live = s.get("command") not in ("bash", "sh", "zsh", "fish", "")
            table.add_row(
                Text("●" if live else "·", style="green" if live else "grey50"),
                Text("✕", style="bold red"),                 # click to close it
                Text(f"seat {s['seat']}", style="bold"),
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
                      Text("add seat", style="cyan"), Text(""), Text(""),
                      key="seat:add")
        title = f"SEATS · {len(seats)}" if seats else "SEATS · none on this screen"
        self.query_one("#t_seats", Static).update(title)

    def render_board(self, data: dict) -> None:
        head = self.query_one("#head", Static)
        if data.get("error"):
            head.update(Text(f"● board unreachable — {qd.clip(data['error'], 60)}",
                             style="bold red"))
        else:
            n = len(data.get("agents", []))
            seats = sum(1 for a in data["agents"] if qd.seat_number(a.get("holder")))
            head.update(Text(f"● {self.cfg.base_url}   {n} live · {seats} seats",
                             style="green"))

        table = self.query_one("#fleet", DataTable)
        table.clear()
        agents = sorted(data.get("agents", []),
                        key=lambda a: (a.get("repo") or "", a.get("holder") or ""))
        for i, a in enumerate(agents):
            key = f"agent:{i}"
            self.rows[key] = a
            seat = qd.seat_number(a.get("holder"))
            who = (a.get("holder") or "?").split("/", 1)[-1]
            table.add_row(
                Text(qd.clip(who, 13), style="bold green" if seat else "bold"),
                Text(qd.clip(a.get("repo") or "—", 11),
                     style=qd.repo_colour(a.get("repo") or "—")),
                Text(qd.clip(a.get("title") or a.get("branch") or "—", 40),
                     style="white" if seat else "grey70"),
                Text(qd.until(a.get("expires")), style="grey50"),
                key=key,
            )
        self.query_one("#t_fleet", Static).update(f"FLEET · {len(agents)}")

        claims = sorted(data.get("claims", []), key=lambda c: c.get("expires") or "")
        ctable = self.query_one("#claims", DataTable)
        ctable.clear()
        for i, c in enumerate(claims):
            key = f"claim:{i}"
            self.rows[key] = c
            left = qd.minutes_left(c.get("expires"))
            ctable.add_row(
                Text(qd.clip((c.get("holder") or "?").split("/", 1)[-1], 13), style="bold"),
                Text(qd.clip(qd.claim_label(c.get("key") or "?", self.plan), 34),
                     style="yellow" if c.get("kind") == "issue" else "grey70"),
                Text(qd.until(c.get("expires")),
                     style="red" if left is not None and left < 10 else "grey50"),
                key=key,
            )
        self.query_one("#t_claims", Static).update(f"CLAIMED · {len(claims)}")

        # Who holds which issue comes off the same claims, and only the holder
        # is displayed — so compare on that, not on the whole claim. A claim
        # renewing changes its expiry every time, and rebuilding the issue table
        # for that would move the cursor out from under a click.
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
        self.plan, self.plan_err = items, err
        repos = qd.resolve_repos()
        ordered = qd.sort_plan(items, repos)
        sig = tuple((i.get("item_id"), (i.get("claim") or {}).get("holder"),
                     len(i.get("blocked_by") or []), i.get("updated")) for i in ordered)
        if sig == self.plan_sig and not err:
            return
        self.plan_sig = sig

        table = self.query_one("#plan", DataTable)
        table.clear()
        for item in ordered:
            glyph, colour = qd.plan_state(item)
            who, who_colour = qd.plan_who(item)
            takeable = qd.plan_issue(item, repos) is not None and not item.get("claim")
            repo = qd.short_repo(item.get("repo") or "fleet")
            table.add_row(
                Text(glyph, style=colour),
                Text("⚒", style="bold cyan" if takeable else "grey30"),
                Text(qd.clip(repo, 11), style=qd.repo_colour(repo)),
                Text(qd.plan_ref(item), style="bold grey70"),
                Text(qd.clip(item.get("title"), 42),
                     style="grey50" if colour == "grey50" else "white"),
                Text(qd.clip(who, 13), style=who_colour),
                key=f"plan:{item.get('item_id')}",
            )
            self.rows[f"plan:{item.get('item_id')}"] = item
        running, blocked = qd.plan_counts(items)
        title = f"PLANS · {len(items)} open"
        if running:
            title += f" · {running} running"
        if blocked:
            title += f" · {blocked} blocked"
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
            key = f"pr:{pr.get('number')}"
            self.rows[key] = pr
            repo = qd.short_repo(pr.get("repo") or qd.REPO)
            table.add_row(
                Text(glyph, style=colour),
                Text("⚖", style="bold cyan"),          # click to panel-review
                Text(qd.clip(repo, 11), style=qd.repo_colour(repo)),
                Text(f"#{pr.get('number')}", style="bold grey70"),
                Text(qd.clip(pr.get("title"), 44),
                     style="grey50" if pr.get("isDraft") else "white"),
                Text(qd.ago(pr.get("updatedAt")), style="grey50"),
                key=key,
            )
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
            key = f"issue:{number}"
            self.rows[key] = issue
            repo = qd.short_repo(issue.get("repo") or qd.REPO)
            table.add_row(
                Text("·" if holder else "○", style="grey50" if holder else "green"),
                Text("⚒", style="grey50" if holder else "bold cyan"),
                Text(qd.clip(repo, 11), style=qd.repo_colour(repo)),
                Text(f"#{number}", style="bold grey70"),
                Text(qd.clip(issue.get("title"), 44),
                     style="grey50" if holder else "white"),
                Text(qd.clip(holder.split("/", 1)[-1], 13) if holder
                     else qd.ago(issue.get("updatedAt")),
                     style="yellow" if holder else "grey50"),
                key=key,
            )
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
        seat machinery — see @qb_review there. It runs the agent the same way
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
        claim = item.get("claim")
        if claim:
            self.say(f"#{issue['number']} is already being worked by "
                     f"{claim.get('holder') or '?'}")
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

        The pane gets @qb_review and NOT @qb_seat, which is what keeps it out of
        the way of everything else: qb-seats' --add, qb-seat-click's reflow and
        the seat bar all select on @qb_seat, so none of them counts a review as
        a seat or offers to start an agent in it.

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
        subprocess.run(["tmux", "set-option", "-p", "-t", pane, "@qb_review", name],
                       capture_output=True, timeout=5)
        subprocess.run(["tmux", "select-layout", "-t", pane, "-E"],
                       capture_output=True, timeout=5)
        self.say(f"'{name}' is in the seat row — Ctrl-b x closes it")

    def click_agent(self, agent: dict) -> None:
        seat = qd.seat_number(agent.get("holder"))
        if seat is not None and self.jump_to_seat(seat):
            self.say(f"jumped to seat {seat} — {agent.get('holder')}")
            return
        self.say(
            f"{agent.get('holder')} · {agent.get('model') or '?'} · "
            f"{agent.get('repo') or '?'}@{agent.get('branch') or '?'} · "
            f"{agent.get('cwd') or '?'}"
        )

    def jump_to_seat(self, seat: int) -> bool:
        """Move the tmux cursor to the pane wearing @qb_seat = seat."""
        if not os.environ.get("TMUX"):
            return False
        try:
            out = subprocess.run(
                ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{@qb_seat}"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == str(seat):
                    subprocess.run(["tmux", "select-pane", "-t", parts[0]], timeout=5)
                    return True
        except Exception:                          # noqa: BLE001
            return False
        return False

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
                 "plan item · r refresh · q quit · click ⚖ to review, ⚒ to fix, "
                 "a plan row for why it is there, a seat to jump to its pane")

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


if __name__ == "__main__":
    Dash().run()
