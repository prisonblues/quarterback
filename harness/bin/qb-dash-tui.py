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

The ⚒ goes through `qb-start` (#371), so what it starts is counted by
`qb-admit`, holds a claim taken before the process exists, is endable by session
id from the moment the pane appears, and is recorded on the board as `via dash`.
It therefore also inherits `qb-start`'s gate: on a machine that has not opted in
— which is every machine by default — the ⚒ refuses and names the one line of
nix that turns it on. It does not fall back to starting an uncounted session;
`Dash.spawn_refusal` is where that decision is argued.

The ⚖ still starts its review directly, and that is not an oversight: a panel
review lands in a PANE of the seat row, beside the work it is about, and
`qb-start` makes windows. Giving it a placement argument is a bigger change than
#371, and the ⚒ is where the loop needed a beginning.

Keys: r refresh now, o open the selected PR, s widen or narrow the scope, q quit.

It opens NARROW: the rows of the project this screen is for (`--repo`, else
QB_DASH_REPOS, else the cwd's origin), with the repo column dropped, because on a
one-project screen that column is the same word on every row and the pane is
78 columns wide (#261). `s` widens it to the whole fleet and brings the column
back; QB_DASH_SCOPE=all opens that way.

It also opens in ONE COLUMN, and lays the panels out in two above 157 of them —
which is two of the 78 a table wants before it wraps, plus the gutter. What the
second column buys is height: seven panels dividing one column's rows is why
CLAIMED and REVIEW QUEUE are two rows tall on a screen nobody would call short.
`QB_DASH_WIDE` moves the threshold; below it nothing about the layout changes.

Textual requests mouse tracking from the terminal, and tmux forwards events to
a pane that asks for them — so this needs no tmux configuration beyond the
`mouse on` that makes borders draggable. Hold Shift to reach tmux's own mouse
behaviour (selecting text) instead of the app's.
"""

from __future__ import annotations

import argparse
import json
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
from textual.events import Click, Resize
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Static

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qbdata as qd                                             # noqa: E402


def sibling(name: str) -> str:
    """`name` on PATH, or beside this file.

    `qb-start.sibling`'s resolution, for its reason: a home-manager install has
    both, a checkout has only the second, and a partial install has only the
    first. The dashboard has always called `qb-seat-click` by its bare name and
    got away with it because it is launched from a shell that has the harness on
    PATH — but the ⚒ now runs the one tool whose absence must not be mistaken for
    a machine that has not opted in, and `which` returning None is a different
    answer from `spawn.json` being absent.
    """
    from shutil import which
    return which(name) or os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def env_columns(name: str, fallback: int) -> int:
    """A column count from the environment, or the fallback — never an exception.

    The one knob here is a layout threshold, and a dashboard that refused to
    start over `QB_DASH_WIDE=wide` would be trading the panel somebody is trying
    to read for a typo in a tuning variable. A value that is not a positive
    number of columns is not honoured and is not fatal; it is simply not there.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        return fallback
    return int(raw)


def spawn_answer(name: str, done: "subprocess.CompletedProcess") -> str:
    """The detail line after `qb-start` has answered, in `qb-start`'s own words.

    Module level, and not a method, because it is the one part of the ⚒ that is
    pure: a completed process in, the sentence a human reads out. That is what
    makes "a refusal is reported rather than swallowed" testable without a board,
    a tmux server or a policy file.

    The LAST two lines of stderr, because that is where `qb-start` puts its
    verdict: the gates it ran print theirs first — `qb-claim` naming the holder,
    `qb-admit` listing the slots — and then it says `refused: …` and its detail.
    Taking the last two keeps the verdict and its remedy and drops the noise
    above them, and the whole of it is still on the pane the click came from.
    """
    try:
        answer = json.loads(done.stdout or "")
    except ValueError:
        answer = {}
    if done.returncode == 0 and answer.get("started"):
        session = str(answer.get("session") or "")
        return (f"started '{name}' — session {session[:8]} · Ctrl-b n to watch it · "
                f"`qb-end {session}` to stop it")
    lines = [ln.strip() for ln in (done.stderr or "").splitlines() if ln.strip()]
    return "⚒ " + (qd.clip(" — ".join(lines[-2:]), 240)
                   or f"qb-start exited {done.returncode} and said nothing")


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


class DialEdit(ModalScreen[dict | None]):
    """Set or clear one dial, from the pane — the write half of #477.

    The dashboard could always READ what was in force; turning one was a browser
    action, because `POST /dials` takes `app.auth.human` and this program
    authenticates with the machine bearer token every agent on the box holds.
    What changed is the credential, not the gate: :class:`qbdata.HumanClient`
    presents a signed-in session to the browser vhost, so the person at this
    keyboard writes as themselves and the board records `human/<user>` as it
    always has.

    **What that costs is written down rather than implied** — prisonblues/quarterback#479
    is the record. The session is readable by everything running as this user, so
    "the dash can set a dial" and "anything on this box can set a dial" are one
    fact, and the second is the one to design against.

    Four fields and no dropdowns, because a modal in a 78-column pane has room
    for labels or for widgets and not both:

      * **dial** — the dotted path. Fixed when editing a row that exists; a dial
        is identified by its name, so letting this be edited would silently
        create a second dial rather than change the one on screen.
      * **value** — JSON where it parses, the string it looks like otherwise
        (`qbdata.parse_dial_value`, and `dials.html` does the same).
      * **reason** — required, by the board and here. A dial whose argument was
        never written down is one nobody can decide to remove.
      * **for** — `30m`, `4h`, `7d`, or empty for a dial with no end. Empty is a
        real answer and not a missing one.
    """

    BINDINGS = [("escape", "cancel", "cancel"), ("ctrl+s", "save", "save"),
                ("ctrl+x", "clear", "clear")]

    CSS = """
    DialEdit { align: center middle; }
    #box { width: 90%; max-width: 76; height: auto; padding: 1 2;
           background: $panel; border: thick $accent; }
    #box Input { margin-bottom: 1; }
    #hint { color: $text-muted; }
    #warn { color: $warning; }
    """

    def __init__(self, row: dict | None = None, repo: str | None = None,
                 scope_label: str = "") -> None:
        super().__init__()
        self.row = row or {}
        self.repo = repo
        self.scope_label = scope_label

    def compose(self) -> ComposeResult:
        existing = bool(self.row.get("dial"))
        with Vertical(id="box"):
            yield Static(Text("set a dial" if not existing else
                              f"dial · {self.row.get('dial')}", style="bold"))
            if not existing:
                yield Input(placeholder="review_panel.fix_severity_floor", id="f_dial")
            yield Input(value=self._value_text(), placeholder="P3, 2, true, null",
                        id="f_value")
            yield Input(placeholder="why is this value in force?", id="f_reason")
            yield Input(placeholder="30m · 4h · 7d — empty for no end", id="f_expiry")
            # WHICH LAYER this will be written to, said before it is written and
            # not after. `fleet` and `this repo` are different settings with the
            # same name, and the one thing a person cannot recover from here is
            # setting the fleet's value while believing they set one repo's.
            yield Static(Text(f"scope: {self.scope_label or 'fleet (every repo)'}",
                              style="bold"), id="warn")
            yield Static(Text("ctrl+s save · ctrl+x clear this dial · esc cancel",
                              style="bold $accent"), id="hint")

    def _value_text(self) -> str:
        """The current value, spelled the way this box would accept it back."""
        if "value" not in self.row:
            return ""
        value = self.row.get("value")
        return value if isinstance(value, str) else json.dumps(value)

    def on_mount(self) -> None:
        # The field a person came here to change. Editing an existing dial that is
        # its value; creating one, it is the name.
        self.query_one("#f_dial" if not self.row.get("dial") else "#f_value",
                       Input).focus()

    def _field(self, name: str) -> str:
        found = self.query(f"#{name}")
        return found.first(Input).value if found else ""

    def action_save(self) -> None:
        self.dismiss({
            "dial": (self.row.get("dial") or self._field("f_dial")).strip(),
            "value": self._field("f_value"),
            "reason": self._field("f_reason"),
            "expiry": self._field("f_expiry"),
            "repo": self.repo,
        })

    def action_clear(self) -> None:
        """Take it off the board. Only for a dial that IS on the board — clearing
        one that was never set is a no-op the board accepts, and offering it while
        creating one would be a button that cannot mean anything."""
        if not self.row.get("dial"):
            self.app.bell()
            return
        self.dismiss({"dial": self.row["dial"], "repo": self.repo, "clear": True})

    def action_cancel(self) -> None:
        self.dismiss(None)


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
    /* Hidden until the first fetch says there is something to show — and since
       #426 "something" is the caps OR the review-queue cell that rides beside
       them, so an install with no subscription token still gets the queue depth
       rather than losing the row it sits on. See render_limits. */
    #limits { height: 1; padding: 0 1; background: $panel; color: $text;
              display: none; }
    #head { height: 1; padding: 0 1; background: $panel; color: $text; }
    #detail { height: auto; min-height: 1; padding: 0 1; background: $panel;
              color: $text-muted; }
    .title { height: 1; padding: 0 1; background: $boost; color: $accent; }

    /* THE SHARE IS ON THE PANEL, NOT ON THE TABLE. Each title and its table are
       one `.panel` so that the wide layout has something to place: a grid puts
       CELLS in columns, and a title in one column with its table in the other is
       what happens if the seven pairs are left loose in the container. The table
       then takes `1fr` of its own panel — everything the title left — so the
       shares below still read as shares of the pane. */
    #body { layout: vertical; }
    .panel { layout: vertical; }
    .panel > DataTable { height: 1fr; }

    /* A share of the pane each, and each scrolls inside its share. With
       `height: auto` the four tables simply stack past the bottom of a 42-row
       pane: the PRs then cannot be clicked, because they are not on screen —
       which is how the click test caught it. */
    /* SEATS sizes to its CONTENT, and it is the only panel here that may.
       Every other one is unbounded — the fleet, the plan and the issue list are
       as long as the board is — so `height: auto` on those is what put the PR
       table off the bottom of the pane and made its rows unclickable. This one
       is bounded by the seats in the row plus the ＋, so an fr share buys it
       nothing and costs it the ＋ the moment another panel appears: adding
       REVIEW QUEUE took the denominator from 10fr to 11fr and the ＋ row, the
       only way to add a seat with the mouse, fell off a 50-row screen.

       12 IS NOT A ROUND NUMBER, it is the tallest this table can be: the header,
       MAX_SEATS=10 from qb-seats, and the ＋. A smaller cap would scroll the
       ＋ out of view on a full screen and reintroduce the bug above four seats
       below the ceiling the script already enforces — a cap and a maximum have to
       be quoted from the same place or one of them silently wins. */
    #p_seats  { height: auto; }
    #seats    { height: auto; max-height: 12; }
    /* Sized to its CONTENT like SEATS, and for SEATS' reason: a fleet with nothing
       set is two rows, and an fr share would spend the rest on blank space that
       comes straight off ISSUES — already the panel that falls below the fold
       (#269). The cap is where it stops growing and starts scrolling, which is the
       right way round here: the printed renderer has to stop listing and count the
       rest (DIAL_ROWS), and this one does not, so nothing is hidden by it. 7 is
       four dials and the row that says where to turn one; a fleet with more than
       that in force has a configuration question rather than a layout one. */
    #p_dials  { height: auto; }
    #dials    { height: auto; max-height: 7; }
    #p_fleet  { height: 2fr; }
    #p_claims { height: 1fr; }
    #p_plan   { height: 2fr; }
    #p_prs    { height: 2fr; }
    /* 1fr, not 2: the queue is at most as deep as OPEN PRs above it and is
       usually shorter, and every row it takes here comes off ISSUES — which is
       already the panel that falls below the fold (#269). */
    #p_queue  { height: 1fr; }
    #p_issues { height: 2fr; }

    /* ---- and the same panels in two columns, when there are columns to spare.
       Textual has no media query, so the class is set from `on_resize` and the
       whole of the wide layout is this block. `layout` is a property like any
       other, which is why the narrow layout above says `vertical` explicitly
       rather than leaning on the default: the two rules have to be able to
       disagree.

       WHAT TWO COLUMNS BUY IS HEIGHT, not width. Seven panels sharing one
       column's rows is why CLAIMED and REVIEW QUEUE are two rows tall on a pane
       nobody would call short; the same seven over four grid rows are three to
       five times that, and no panel's share had to be taken from another's.

       DIALS AND SEATS SPAN BOTH, and for one reason said twice: they are the two
       panels whose height is their CONTENT, so a column of their own would buy
       them nothing and cost the panel beside them half its width. DIALS keeps
       its place at the top for the reason it was put there — it is the
       configuration every panel below is running under — and SEATS keeps the ＋
       findable, which is the one thing that panel has to do (see the cap above).
       They are also the only `auto` rows: the three under them divide what is
       left.

       THE WEIGHTS ARE THE NARROW ONES, PAIRED. A row is as tall as the taller of
       the two panels in it wants to be, and narrow that is 2fr for all three
       pairs — (FLEET, CLAIMED), (OPEN PRs, REVIEW QUEUE), (PLANS, ISSUES) — so
       equal thirds is the faithful translation. PLANS and ISSUES get the extra
       because they are the two panels that are always long: the plan is every
       repo's list and the issue list is every open issue, while OPEN PRs is
       usually under ten and is often zero. Both short panels ride with a long
       one rather than with each other, so no row is dead space on a quiet day. */
    #body.-wide { layout: grid; grid-size: 2; grid-rows: auto auto 2fr 2fr 3fr;
                  grid-gutter: 0 1; }
    #body.-wide .panel { height: 100%; }
    #body.-wide #p_dials { column-span: 2; height: auto; }
    #body.-wide #p_seats { column-span: 2; height: auto; }
    """

    BINDINGS = [
        ("q", "quit", "quit"),
        ("r", "refresh_now", "refresh"),
        ("o", "open_pr", "open"),
        ("p", "panel_pr", "panel"),
        ("f", "fix_issue", "fix"),
        ("s", "toggle_scope", "scope"),
        # The board's dials PAGE, which is still worth a key now that the panel
        # can write: the page shows every repo's dials at once and this panel
        # shows the screen's own, and a person who wants to compare two projects
        # wants the page. The ✎ on a row is the control; this is the map.
        ("d", "open_dials", "dials"),
        ("question_mark", "help", "keys"),
    ]

    # WHEN THE PANELS GO TWO ACROSS, in columns of the pane. Not a taste: 78 is
    # what one of these tables wants before it wraps — it is `QB_SEATS_DASH_SIZE`'s
    # default, and quoted from there — so two of them side by side plus the gutter
    # between is the narrowest screen on which the second column is not paid for
    # out of the first. `QB_DASH_WIDE` moves it, which is how a terminal whose
    # font makes 157 columns comfortable can have the wide layout sooner.
    WIDE_COLUMNS = 157

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
    # And column 1 of the DIALS table is the ✎ that opens the page where a dial
    # can actually be turned. Same column as the other three, because "the action
    # icon is always the second cell" is one habit rather than four — and the same
    # meaning as the ⚒ next door: this is the verb, the rest of the row is the
    # explanation.
    EDIT_COLUMN = 1

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
        # The derived review queue, kept for the same reason as `board`: `s`
        # redraws from it, and it rides the gh clock so a re-fetch on a toggle
        # would cost a `gh pr list` for a decision already made.
        self.queue: dict = {}
        # NONE UNTIL `gh` HAS ANSWERED, for the reason `self.held` is (#433): an
        # empty list is an answer this panel counts and sorts, so standing it in
        # for "not asked yet" is how a panel comes to state something it does not
        # know. `render_issues` paints when BOTH answers are in and not before.
        self.issues: list[dict] | None = None
        self.issue_err: str | None = None
        # What the board says is in force. `{}` UNTIL IT HAS ANSWERED, and
        # `fetch_dials` sets `asked` when it has — the header cell renders nothing
        # at all until then, because "no dial is set" and "nobody has asked" are
        # different facts and the first is the one a person acts on (#244).
        self.dials: dict = {}
        #: The credential the writes go out on, or None until `on_mount` has a
        #: config to build it from. See `qd.HumanClient` — and #479 for what it
        #: costs, which is not this file's to re-argue but is this file's to say.
        self.human: "qd.HumanClient | None" = None
        self.plan: dict = {}                      # the whole /plan envelope
        self.plan_err: str | None = None
        self.plan_sig: tuple | None = None
        # 'owner/repo#n' → the claim on it, and NONE UNTIL THE BOARD HAS ANSWERED:
        # `{}` means "the board says nothing is held", which is an answer this
        # panel sorts on. See `render_issues` for what the distinction buys and
        # for its limit — it is the FIRST paint that is protected, not every one.
        self.held: dict | None = None
        # Why `held` looks the way it does. Set while the last poll FAILED, so a
        # `{}` that means "the board is unreachable" is never read as a `{}` that
        # means "nothing is claimed" — see render_board.
        self.claims_err: str | None = None
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
        # The ⚒ runs this rather than the agent directly (#371). Resolved once
        # and kept, so that a test can point it somewhere and so that the two
        # calls the button makes — `--policy` before the click and the spawn
        # after it — cannot end up asking two different binaries.
        self.start_bin = sibling("qb-start")
        self.confirm = os.environ.get("QB_DASH_CONFIRM", "1") != "0"
        self.pr_err: str | None = None
        # WHICH LAYOUT IS UP, and it starts narrow because that is what the pane
        # `qb-seats` splits off is. `relayout` compares against this rather than
        # against the class, so a resize that does not cross the threshold — which
        # is most of them, since attaching a client resizes every pane on the
        # screen — costs a comparison and no reflow.
        self.wide = False
        self.wide_at = env_columns("QB_DASH_WIDE", self.WIDE_COLUMNS)

    # ---- layout ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Above the board line, because it governs every pane below it: the seats
        # spend one subscription between them, and the window they are working
        # towards is the one number none of the tables can show.
        yield Static("", id="limits")
        yield Static("quarterback — connecting…", id="head")
        # A PANEL PER TABLE, and the pairing is structural rather than visual: a
        # grid places cells, so a title loose in the container is a cell of its
        # own and lands in a different column from the table it names. Wrapping
        # them changes nothing about the narrow screen — one `Vertical` inside
        # another lays out identically — and is the whole of what the wide one
        # needed from the tree.
        with Vertical(id="body"):
            # Above the seats, and above everything the seats then do: this is the
            # configuration every panel below is running under, which is the caps
            # line's own argument for being at the top. It is two rows when nothing
            # is set — the printed renderer makes the same choice in the same place
            # (qb-dash.panel_dials).
            with Vertical(classes="panel", id="p_dials"):
                yield Static("DIALS", classes="title", id="t_dials")
                yield ClickTable(id="dials", cursor_type="row")
            with Vertical(classes="panel", id="p_seats"):
                yield Static("SEATS", classes="title", id="t_seats")
                yield ClickTable(id="seats", cursor_type="row")
            with Vertical(classes="panel", id="p_fleet"):
                yield Static("FLEET", classes="title", id="t_fleet")
                yield ClickTable(id="fleet", cursor_type="row", zebra_stripes=False)
            with Vertical(classes="panel", id="p_claims"):
                yield Static("CLAIMED", classes="title", id="t_claims")
                yield ClickTable(id="claims", cursor_type="row")
            with Vertical(classes="panel", id="p_plan"):
                yield Static("PLANS", classes="title", id="t_plan")
                yield ClickTable(id="plan", cursor_type="row")
            with Vertical(classes="panel", id="p_prs"):
                yield Static("OPEN PRs", classes="title", id="t_prs")
                yield ClickTable(id="prs", cursor_type="row")
            # Directly under OPEN PRs, which is where it answers the question that
            # panel raises and cannot: that one says a PR exists and CI is green,
            # this one says whether anybody has reviewed it (#273).
            with Vertical(classes="panel", id="p_queue"):
                yield Static("REVIEW QUEUE", classes="title", id="t_queue")
                yield ClickTable(id="queue", cursor_type="row")
            with Vertical(classes="panel", id="p_issues"):
                yield Static("ISSUES", classes="title", id="t_issues")
                yield ClickTable(id="issues", cursor_type="row")
        yield Static("click: seat→pane, ✕→close it, ＋→add one, PR→GitHub, "
                     "plan row→why, queue row→what it waits on, dial row→why it is "
                     "set, ⚖→panel review, ⚒→fix issue, ✎→set or clear a dial"
                     "   ? for keys",
                     id="detail")
        yield Footer()

    def on_mount(self) -> None:
        # BEFORE the board client, which is allowed to fail: a machine with no
        # board configured still gets a laid-out dashboard saying so, and a
        # `return` above this would leave a wide pane in the narrow layout for as
        # long as it stayed exactly that wide.
        self.relayout()
        self.query_one("#seats", DataTable).add_columns("", "✕", "seat", "state", "running", "where")
        self.query_one("#claims", DataTable).add_columns("who", "key", "left")
        # NOT in build_columns, and that is the one table here for which that is
        # right: its scope cell names the LAYER a value came from — fleet or this
        # repo — rather than a project, and that is half of what a dial's answer
        # is. See qbdata.dial_where.
        self.query_one("#dials", DataTable).add_columns(
            "", "✎", "dial", "value", "in force", "left", "why")
        self.build_columns()
        try:
            self.client, self.cfg = qd.board_client()
            # Built unconditionally and asked later whether it can do anything.
            # `why_not()` is configuration rather than a live check, so a box with
            # no session still gets an object that explains itself — which is what
            # lets the ✎ say why instead of going missing.
            self.human = qd.HumanClient(self.cfg)
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
            # `stage` goes between `state` and the toggling repo cell: it is a
            # fixed column, so it must sit ABOVE the one that comes and goes for
            # the same reason the action icons do (#262). `rank` goes after the
            # repo cell for the opposite reason: it is the plan's own column and
            # nothing indexes past it.
            ("#fleet", ("who", "state", "stage", *repo, "what", "ttl")),
            ("#plan", ("", "⚒", *repo, "rank", "ref", "title", "who")),
            ("#prs", ("", "⚖", *repo, "pr", "title", "age")),
            # `waiting for` before `age` and both before the title: the whole
            # point of the panel is the verb and the wait, and a title long
            # enough to be useful would push them off a 78-column pane.
            ("#queue", ("", "⚖", *repo, "pr", "waiting for", "age", "title")),
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
        """The plan and the dials, on one clock and in one worker.

        Both are board calls answering a question a PERSON changed — a reorder, a
        floor moved, a tempo turned down — rather than something the fleet does to
        itself every few seconds, so neither wants the four-second clock and both
        want the same one. `refresh_prs` pairs the review queue with the PR list
        for the same kind of reason.
        """
        plan, err = qd.fetch_plan(self.client)
        self.call_from_thread(self.render_plan, plan, err)
        self.call_from_thread(self.render_dials, qd.fetch_dials(self.client))

    @work(thread=True, exclusive=True, group="prs")
    def refresh_prs(self) -> None:
        """The PR list and the queue derived from it, in one worker.

        The queue is a BOARD call and still rides the gh clock, because it is a
        join over the very PR rows fetched on the line above. Given its own timer
        it would answer about PRs up to ninety seconds newer than the ones OPEN
        PRs is drawing, and the two panels would disagree about any PR that moved
        in between. `qb-dash.fetch_gh` makes the same choice for the same reason.

        The PR table is rendered BEFORE the queue is fetched rather than after,
        so a slow or dead board costs the queue panel and not the PR panel.
        """
        prs, err = qd.fetch_prs()
        self.call_from_thread(self.render_prs, prs, err)
        # `pr_err` and not a bare list: `fetch_prs` answers a failed `gh` with
        # ([], err), and deriving from that empty list would have the board
        # honestly report a drained queue for a repo with eight PRs waiting.
        queue = (qd.fetch_review_queue(self.client, prs, pr_err=err)
                 if self.client is not None else {})
        self.call_from_thread(self.render_queue, queue)

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

    def render_limits(self, limits: list[dict], err: str | None,
                      width: int | None = None) -> None:
        """Claude Code's own caps, as bars — `5h ████░░ 64% 3h57m  7d ██░ 41% 5d8h`.

        A failed call keeps the last figures rather than blanking the line: they
        are minutes old and still roughly true, and a line that vanished on every
        hiccup would read as "no limits", which is the opposite of what it means.

        THE ROW IS HIDDEN WHEN IT HAS NOTHING TO SAY, and since #426 that is both
        halves empty rather than the caps half. An install with no subscription
        token has no cells — and neither does a pane under 20 columns, which is
        `limit_cells`' own floor and one `C-q <` away — so gating the whole row on
        the caps took the review depth off the screen in exactly the two cases the
        queue cell was put here to survive.
        """
        if limits:
            self.limits = limits
        self.limits_err = err
        try:
            bar = self.query_one("#limits", Static)
        except Exception:                         # noqa: BLE001 — a resize before mount
            return
        # `width` when a resize handed one over — see on_resize for why the app's
        # own size is a resize behind in there. Everywhere else it is unset and
        # the app's size is the current one.
        pane = self.size.width if width is None else width
        cells = qd.limit_cells(self.limits, max(20, pane - 2))
        tempo = qd.tempo_cell(self.dials)
        bar.display = bool(cells or self.queue or tempo)
        if not (cells or self.queue or tempo):
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
        # The `?` annotates the caps, so it needs caps to annotate: on a line
        # carrying only the queue cell it would be a mark against a number that
        # is not there.
        if err and cells:
            text.append(" ?", style="grey50")
        # The queue's depth and age ride the caps line beside the budget they
        # would be spent out of — a panel round costs tokens, and "3 waiting" is
        # only actionable next to how much is left. A depth of ZERO still draws:
        # "nothing is waiting" is the answer this panel exists to make reachable,
        # and a cell that vanished when it was true could not be told apart from
        # a dashboard that never asked.
        if self.queue:
            label, depth, age, colour = qd.queue_cell(self.queue)
            if text.plain:                        # no caps to sit beside: no gap
                text.append("   ")
            text.append(label, style="bold grey70")
            text.append(f" {depth}", style=f"bold {colour}")
            if age:
                text.append(f" {age}", style="grey50")
        # And the throttle beside the budget it protects (#477). The caps say what
        # the seats MAY spend; this says whether they are supposed to be spending
        # it at all, and a reader glancing at one is asking about the other. Every
        # state draws once the board has answered, `unset` included — a cell that
        # vanished when nothing was set could not be told from a dashboard that
        # never asked, and "nothing is throttling the fleet" is exactly the answer
        # somebody is looking for at 94% of a window.
        if tempo:
            label, value, life, colour = tempo
            if text.plain:
                text.append("   ")
            text.append(label, style="bold grey70")
            text.append(f" {value}", style=f"bold {colour}")
            if life:
                text.append(f" {life}", style="grey50")
        bar.update(text)

    def on_resize(self, event: Resize) -> None:
        """Re-lay to the new width — the bars are sized to the pane, the panels
        are one column or two by it, and the dash pane is resized every time the
        screen is.

        THE WIDTH COMES OFF THE EVENT, NEVER OFF `self.size`, and that is not
        style. Measured on textual 8.2: the handler runs BEFORE the app's own
        size is updated, so a `self.size.width` read here is the width the pane
        had before the resize being handled. The caps bar has been laying itself
        out one resize behind ever since it was sized to the pane — invisible,
        because dragging a border emits a stream of them and the last-but-one is
        near enough — and a layout threshold read the same way is not invisible
        at all: it would take two crossings to go two-across, and a pane that
        crossed once and stopped would sit in the wrong layout indefinitely.
        """
        self.relayout(event.size.width)
        self.render_limits(self.limits, self.limits_err, width=event.size.width)

    def relayout(self, width: int | None = None) -> None:
        """One column or two, decided by the width the pane actually has.

        Textual has no media query, so this is the media query: `on_resize` fires
        on every width the pane is given — a client attaching, a seat closing, a
        `C-q >`, a zoom — and the class it sets is the whole of what `#body.-wide`
        keys off.

        THE ORDER IS NOT THE SAME IN BOTH LAYOUTS, and that is the only part of
        this that is not CSS. A grid fills row by row in DOM order, so the narrow
        order lays OPEN PRs and REVIEW QUEUE into different rows — and "the queue
        sits directly under the PRs" is the arrangement #273 asked for, not a
        coincidence of the order they were added in. Wide, `directly under`
        becomes `directly beside`: PLANS moves down one, which pairs PRs with the
        queue that reviews them and PLANS with the issues its items point at.

        `move_child` and not a remount, because the panels hold DataTables with a
        cursor, a scroll offset and the row keys every click resolves through —
        all of which a remove-and-mount would throw away, and the width crossing
        the threshold is not news the panel should lose its place over.
        """
        wide = (self.size.width if width is None else width) >= self.wide_at
        if wide == self.wide:
            return
        self.wide = wide
        body = self.query_one("#body", Vertical)
        body.set_class(wide, "-wide")
        plan = self.query_one("#p_plan", Vertical)
        if wide:
            body.move_child(plan, after=self.query_one("#p_queue", Vertical))
        else:
            body.move_child(plan, before=self.query_one("#p_prs", Vertical))

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
                # What `state` cannot say: `working` reads the same writing the
                # first cut and coming out of the third review round, and so do
                # repo, branch and title. This is the cell that moves (#262).
                Text(*qd.stage_cell(a)),
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
            claims, self.scope,
            lambda c: qd.claim_repo(c.get("key"), qd.plan_items(self.plan)))
        for i, c in enumerate(shown):
            key = f"claim:{i}"
            left = qd.minutes_left(c.get("expires"))
            key = ctable.add_row(
                Text(qd.clip((c.get("holder") or "?").split("/", 1)[-1], 13), style="bold"),
                Text(qd.clip(qd.claim_label(c.get("key") or "?",
                                            qd.plan_items(self.plan), self.scope), 34),
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
        # A FAILED POLL ANSWERS NOTHING ABOUT CLAIMS, and it arrives shaped exactly
        # like an answer: `fetch_board` reports an outage as `{"claims": [],
        # "error": …}`, which is indistinguishable here from "nobody holds
        # anything". Taking it as one overwrites a real prior answer with `{}`,
        # repaints every issue as free, and defeats the ⚒'s guard below — which
        # reads `held` and cannot see WHY it is empty. `● board unreachable` is on
        # the head line, but that is a different widget from the row being clicked.
        stale, self.claims_err = self.claims_err, data.get("error")
        if self.claims_err:
            # The last good answer stands; only its freshness changed. Except when
            # there is no last good answer — then this is the first one, and the
            # panel has to be released rather than left waiting on a board that is
            # down. `{}` is the only order available then, and `claims_err` is what
            # stops it being read as knowledge: the title says so and the ⚒ refuses.
            if self.held is None:
                self.held = {}
            self.render_issues(self.issues, self.issue_err)
            return
        # `self.held is None` is its own reason to render: the FIRST answer has
        # to reach the table even when it is empty, and comparing holders alone
        # cannot see that transition — {} and {} are equal. That is the case the
        # renewal guard above was never written for. A board that has just COME
        # BACK is the same shape of transition — the rows are unchanged and what
        # they are worth is not — hence `stale` here. `render_issues` decides
        # whether that answer is enough to paint on; it is not, on its own.
        if self.held is None or stale or holders(held) != holders(self.held):
            self.held = held
            self.render_issues(self.issues, self.issue_err)

    def render_plan(self, plan: dict, err: str | None) -> None:
        """The board's plan — every repo's list — in the board's own order.

        Rebuilt only when the plan actually changed. The other tables can be
        redrawn on a clock because their rows carry a countdown, but this one is
        the panel a reader dwells on, and a rebuild between the mouse going down
        and the click arriving moves the row out from under the pointer.

        The rows arrive ranked and are drawn ranked. Re-banding them here — taken,
        free, blocked — was a second answer about an ordered list, computed
        against that list's own order; `next` is what the banding was reaching for
        and the board sends it outright.
        """
        # The WHOLE envelope is kept, and the scope applied after: `self.plan` is
        # what resolves a `plan:<uuid>` claim to a title and a repo, and a claim
        # from another project must still resolve — otherwise widening the scope
        # would show rows this client can no longer explain.
        self.plan, self.plan_err = plan, err
        repos = qd.resolve_repos()
        items, hidden = qd.in_scope(qd.plan_items(plan), self.scope)
        next_id = qd.plan_next_id(plan)
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
        #
        # So is everything the title now reports that no row carries — `next`, the
        # counts, the unchosen tally — for the same reason the hidden count is:
        # the board's answer about what to pick up can move while every row on the
        # pane stays exactly as it was.
        #
        # And the ERROR is in it rather than being an exemption from it. The guard
        # used to read "unchanged and no error", which redraws while a board is
        # down and then, on the refresh that succeeds, returns early and leaves
        # `board: …` in the title of a panel that is no longer failing. It only
        # shows on a plan whose rows did not change across the outage — an empty
        # one, or a quiet minute — which is the case where nothing else will ever
        # move to clear it.
        sig = (hidden, next_id, err, tuple(sorted((plan.get("counts") or {}).items())),
               (plan.get("order_trust") or {}).get("unchosen"), plan.get("truncated"),
               tuple((i.get("item_id"), (i.get("claim") or {}).get("holder"),
                      (i.get("covered_by") or {}).get("holder"),
                      len(i.get("blocked_by") or []), i.get("rank"),
                      i.get("rank_source"), i.get("updated"))
                     for i in items))
        if sig == self.plan_sig:
            return
        self.plan_sig = sig

        table = self.query_one("#plan", DataTable)
        table.clear()
        for item in items:
            glyph, colour = qd.plan_state(item, next_id)
            who, who_colour = qd.plan_who(item)
            rank, rank_colour = qd.plan_rank(item)
            issue = qd.plan_issue(item, repos)
            takeable = (issue is not None and not qd.plan_holder(item)
                        and self.wrong_repo(issue.get("repo"), "") is None)
            key = table.add_row(
                Text(glyph, style=colour),
                Text("⚒", style="bold cyan" if takeable else "grey30"),
                *self.repo_cell(qd.short_repo(item.get("repo") or "fleet")),
                Text(rank, style=rank_colour),
                Text(qd.plan_ref(item), style="bold grey70"),
                # A fleet-wide item has no repo to name, and with the column gone
                # it would read as one of this project's (qbdata.scope_mark).
                Text(qd.scope_mark(self.scope, item.get("repo"))
                     + qd.clip(item.get("title"), 42 if self.scope.column else 52),
                     style="grey50" if colour == "grey50" else "white"),
                Text(qd.clip(who, 17), style=who_colour),
                key=f"plan:{item.get('item_id')}",
            ).value
            self.rows[str(key)] = item
        # The heading is one line and clips at the pane edge, so it is given the
        # room it has — none of which is known before the first layout, where the
        # width reads 0 and "no room" would drop every segment there is.
        room = self.query_one("#t_plan", Static).size.width - 12 - len(qd.elsewhere(hidden))
        title = "PLANS · " + " · ".join(
            text for text, _ in qd.plan_head_bits(plan, items, hidden,
                                                  room if room > 20 else None))
        title += qd.elsewhere(hidden)
        if err:
            title += f" · board: {qd.clip(err, 24)}"
        self.query_one("#t_plan", Static).update(title)

    def render_dials(self, dials: dict) -> None:
        """Which dials are in force, which layer answered, why, and for how long — #477.

        A dial is a setting: the repo supplies a default, the board states the
        value IN FORCE, and the layer that answered is part of the answer (#305).
        Nothing a person or an agent looks at showed one until this panel — the
        value governing every round on the fleet was set from a browser endpoint,
        read back by one function in `panel_seats.py`, and invisible everywhere
        else.

        **The last row is always the door.** `POST /dials` takes `app.auth.human`
        and this dashboard holds a machine bearer token, which is precisely the
        credential that gate exists to refuse — every agent on a box holds it, and
        nothing inside a request distinguishes one from a person. So the row that
        opens the board's dials page is drawn whether or not there is a dial above
        it, because the reader who most needs it is the one who has just found out
        that nothing is set.
        """
        self.dials = dials
        table = self.query_one("#dials", DataTable)
        table.clear()
        self.rows = {k: v for k, v in self.rows.items() if not k.startswith("dial:")}
        rows = dials.get("dials") or []
        # ASKED ONCE PER PAINT, not per row: it is the same answer for every one
        # of them, and it decides whether the ✎ is a control or an explanation.
        # A verb that looks available and fails on the click is the shape that
        # reads as a broken button — and this one would fail against a board that
        # is perfectly healthy, because what is missing is on this host.
        cannot = self.human.why_not() if self.human else "no board configured"
        pencil = "grey30" if cannot else "bold cyan"
        for row in rows:
            where, where_style = qd.dial_where(row)
            life, life_style = qd.dial_life(row)
            by = " · ".join(x for x in (row.get("set_by"), qd.ago(row.get("set_at"))) if x)
            key = f"dial:{row.get('repo') or 'fleet'}:{row.get('dial')}"
            key = table.add_row(
                Text("●", style="cyan"),
                Text("✎", style=pencil),
                Text(qd.clip(row.get("dial"), 34), style="bold white"),
                Text(qd.dial_value(row, 14), style="cyan"),
                Text(where, style=where_style),
                Text(life, style=life_style),
                # The argument for the value, which the board requires and which is
                # the difference between a dial somebody can decide to remove and
                # one nobody dares touch. Clipped here, in full on a click.
                Text(qd.clip((row.get("reason") or "") + (f"  ({by})" if by else ""), 40),
                     style="grey50"),
                key=key,
            ).value
            self.rows[str(key)] = row

        err = dials.get("error")
        if err:
            # A ROW and not a suffix on the title, for the same reason the review
            # queue puts its errors in one: the title is bounded by the pane and
            # would clip the one message that says why the panel is empty.
            table.add_row(Text("!", style="red"), Text(""), Text(""), Text(""),
                          Text(""), Text(""), Text(qd.clip(err, 40), style="red"),
                          key="dial:error")
        if not rows and not err:
            table.add_row(Text(""), Text(""),
                          Text("every dial at its repo default" if dials.get("asked")
                               else "asking the board…", style="grey50"),
                          Text(""), Text(""), Text(""), Text(""), key="dial:none")
        # The door, always. Registered in `self.rows` so a click reaches
        # `dispatch_row` — an unregistered key is dropped, which is right for a
        # row with no verb and wrong for the only row here that has one.
        # THE LAST ROW IS THE VERB THIS PANEL DID NOT USED TO HAVE, and it says
        # which one it is: a live ✎ sets a dial from here, a dead one still opens
        # the page that can. Drawn whether or not there is a dial above it,
        # because the reader who most needs it is the one who has just found out
        # that nothing is set.
        page = table.add_row(
            Text(""), Text("✎", style=pencil),
            Text(qd.clip(f"set a dial — {cannot}" if cannot else "set a dial",
                         92), style="grey50" if cannot else "cyan"),
            Text(""), Text(""), Text(""), Text(""), key="dial:page").value
        self.rows[str(page)] = {"page": True}

        # A COUNT IS A CLAIM: "0 in force" over a board that would not answer says
        # the fleet is running on its defaults, which is the one thing an
        # unanswered read cannot establish.
        if err:
            title = "DIALS · unreadable"
        elif not dials.get("asked"):
            title = "DIALS · asking"
        else:
            title = f"DIALS · {len(rows)} in force"
        shadowed = len(dials.get("shadowed") or [])
        if shadowed:
            # A fleet dial every repo on this screen overrides. Counted rather than
            # drawn, because it is NOT in force here — and silence would leave the
            # person who set it fleet-wide unable to see what became of it.
            title += f" · {shadowed} overridden"
        self.query_one("#t_dials", Static).update(title)
        # The header cell carries the tempo off this same answer, and it is drawn
        # on the limits clock — an hour long. Without this it would keep last
        # hour's value while the panel below showed this minute's.
        self.render_limits(self.limits, self.limits_err)

    def render_prs(self, prs: list[dict], err: str | None) -> None:
        self.prs, self.pr_err = prs, err
        table = self.query_one("#prs", DataTable)
        table.clear()
        for pr in sorted(prs, key=lambda p: -p.get("number", 0)):
            glyph, colour = qd.ci_state(pr)
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
        # Every non-green state, not just red. A PR whose runs are gated used to
        # contribute to no number here at all, which is #324's whole complaint:
        # the screen said "12 open PRs, 0 red" while one of them had failed and
        # been buried under an approval gate.
        counts = qd.ci_counts(prs)
        tally = "".join(f" · {counts[s]} {w}" for s, w in
                        (("red", "red"), ("blocked", "blocked"), ("pending", "running"),
                         ("none", "untested"), ("unknown", "unread")) if counts.get(s))
        title = f"OPEN PRs · {len(prs)}" + tally
        if err:
            title += f" · gh: {qd.clip(err, 24)}"
        self.query_one("#t_prs", Static).update(title)

    def render_queue(self, queue: dict) -> None:
        """What review is waiting on, and how long it has waited — #273.

        The panel OPEN PRs cannot be: that one says a PR exists and CI is green,
        and never said whether anybody had reviewed it. On 2026-08-20 six of
        eight open PRs had never been panelled while the newest round on the
        board was two and a half days old, and neither number was readable
        anywhere.

        Rows arrive oldest-drainable first and are drawn in that order. It is a
        READING order and not a work order — the board refuses to rank the queue
        (#232 owns that) — so this panel refuses too, and simply shows the top of
        the list it was handed.

        An entry nothing may act on KEEPS ITS PLACE, greyed, with the reason in
        its verb column instead of a verb. A queue that hid its blocked entries
        would report a depth of zero for a repo where everything is stuck (#244),
        which is the one reading this panel exists to prevent.
        """
        self.queue = queue
        table = self.query_one("#queue", DataTable)
        table.clear()
        entries = queue.get("entries") or []
        for e in entries:
            state = e.get("state") or ""
            colour = qd.QUEUE_COLOUR.get(state, "grey50")
            drains = bool(e.get("drainable"))
            holds = e.get("holds") or []
            hold = holds[0].get("code") if holds else state
            action = e.get("next_action")
            verb = (qd.QUEUE_VERB.get(action, action or "")
                    if drains else qd.QUEUE_HOLD.get(hold, hold))
            repo = e.get("repo") or qd.REPO
            # By repo AND number, like every other table here: two watched repos
            # both reach #42 eventually, and the bare number is what handed a
            # table the same row key twice (#209).
            key = f"queue:{qd.short_repo(repo)}#{e.get('pr')}"
            # The ⚖ is offered ONLY where a panel round is the thing this entry
            # is waiting for. `fix`, `rebase` and `land` are real next actions
            # with no button on this dashboard, and `answer` is owed by a human —
            # drawing a live ⚖ on any of them would start the wrong work, so they
            # get the same grey the unreachable-repo guard uses one panel over.
            offers_panel = drains and action in ("review", "re-review")
            reachable = offers_panel and self.wrong_repo(e.get("repo"), "") is None
            key = table.add_row(
                Text("●", style=colour),
                Text("⚖", style="bold cyan" if reachable else "grey30"),
                *self.repo_cell(qd.short_repo(repo)),
                Text(f"#{e.get('pr')}", style="bold grey70"),
                Text(qd.clip(verb, 11), style=colour if drains else "grey50"),
                # A `~` on an age that is the longest the wait COULD have been.
                # Nothing records when a head moved or when a branch started
                # conflicting, and a number nobody can rely on should say so.
                Text(("~" if e.get("age_is_upper_bound") else "")
                     + qd.waited(e.get("age_seconds")), style="grey50"),
                Text(qd.clip(e.get("title") or "", 30 if self.scope.column else 42),
                     style="white" if drains else "grey50"),
                key=key,
            ).value
            self.rows[str(key)] = e

        # THE TWO STATES THAT ARE NOT ENTRIES, both of which the plain renderer
        # draws as rows (qb-dash.py:377-382) and the first cut of this port did
        # not. Neither is registered in `self.rows`: a key with nothing behind it
        # is dropped by dispatch_row, which is what a row with no verb wants.
        #
        # An error is a ROW and no longer a suffix on the title. The title is
        # bounded by the pane's width and was clipping the message to 24
        # characters — a panel whose job is saying WHY something is waiting must
        # not truncate the one message that says why it cannot tell you.
        err = queue.get("error")
        blank = [Text("")] * (1 if self.scope.column else 0)
        if err:
            table.add_row(Text("!", style="red"), Text(""), *blank,
                          Text(""), Text(""), Text(""),
                          Text(qd.clip(err, 30 if self.scope.column else 42),
                               style="red"),
                          key="queue:error")
        # "Nothing is waiting" and "nothing could be fetched" are different
        # answers and the board supplies its own wording for the first, so the
        # fallback here is only for a board too old to send one.
        if not entries and not err:
            table.add_row(Text(""), Text(""), *blank,
                          Text(""), Text(""), Text(""),
                          Text(queue.get("idle") or "nothing waiting on review",
                               style="grey50"),
                          key="queue:idle")

        depth = queue.get("depth") or 0
        held = max(0, (queue.get("open") or 0) - depth)
        title = f"REVIEW QUEUE · {depth} waiting"
        if held:
            title += f" · {held} held"
        age, oldest_held = qd.queue_oldest(queue)
        if age:
            title += f" · {'held' if oldest_held else 'oldest'} {age}"
        self.query_one("#t_queue", Static).update(title)
        # The caps line carries this same depth, and it is drawn on the limits
        # clock — an hour long. Without this the cell up there would keep last
        # hour's number while the panel down here showed this minute's.
        self.render_limits(self.limits, self.limits_err)

    def render_issues(self, issues: list[dict] | None, err: str | None) -> None:
        """Open issues, free ones first, the held ones greyed and named.

        A free issue is the one a seat should take next, so it is what this
        panel is for: the ⚒ on its row starts /fix-issue on it.

        NOTHING IS PAINTED UNTIL BOTH ANSWERS ARE IN — `gh`'s issues and the
        board's claims — because either alone draws a table this panel is then
        about to rearrange, and the rearrangement is #433: a reader picks a row
        by looking at it, and it has to still be there when the click lands.

        WHAT THIS DOES NOT DO, since the title of the change reads wider than the
        code: it protects the FIRST paint. A claim taken or dropped later is a
        real change in the answer and still re-sorts the table, exactly as the
        renewal guard in `render_board` was written to allow. Holding an order
        that has gone stale would be the opposite mistake.

        Neither wait can hang. `fetch_board` returns a state rather than raising,
        so a board that is DOWN still arrives here and releases the paint, and a
        `gh` that fails answers with an empty list and an error — which is an
        answer, and is counted and shown as one.
        """
        self.issues, self.issue_err = issues, err
        if self.held is None or self.issues is None:
            # WHICH answer is missing, and the `gh` error if there is one: the
            # title is the only place a stalled panel explains itself, and
            # blaming the board for a `gh` failure it already knows about would
            # send a reader to the wrong end of the problem.
            waiting = " and ".join(w for w, missing in
                                   (("the board", self.held is None),
                                    ("gh", self.issues is None)) if missing)
            title = f"ISSUES · waiting for {waiting}"
            if err:
                title += f" · gh: {qd.clip(err, 24)}"
            # Text(), not str: `gh`'s stderr goes in here and a bracketed token
            # in it — `ConnectionRefusedError: [Errno 111] …`, which survives the
            # clip — is a Rich style tag to a Static that parses markup. The panel
            # that exists to explain a stalled state would raise MarkupError from
            # inside a call_from_thread instead.
            self.query_one("#t_issues", Static).update(Text(title))
            return
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
        title = f"ISSUES · {len(issues)}"
        # Not "N free" while the board is unreachable: `free` is counted off claims
        # that are stale or were never fetched, and stating it would be the same
        # collapse this change exists to undo, one panel up from the rows.
        if issues:
            title += (f" · claims unknown: {qd.clip(self.claims_err, 20)}"
                      if self.claims_err else f" · {free} free")
        if err:
            title += f" · gh: {qd.clip(err, 24)}"
        self.query_one("#t_issues", Static).update(Text(title))   # markup: see above

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
                # With the envelope, so the row the board named `next` can show the
                # caveat the board attached to that recommendation.
                self.say(qd.plan_detail(record, self.plan))
        elif kind == "queue":
            if column == self.PANEL_COLUMN:
                # Only where the row drew a live ⚖. Everything else says what it
                # is actually waiting for rather than starting a round that would
                # be spent on the wrong thing — a conflicting branch burns a whole
                # panel round to tell you it is conflicting (#271).
                action = record.get("next_action")
                if action in ("review", "re-review"):
                    self.panel_pr({"repo": record.get("repo"),
                                   "number": record.get("pr")})
                else:
                    self.say(qd.queue_detail(record))
            else:
                self.say(qd.queue_detail(record))
        elif kind == "dial":
            # The ✎ edits; anything else on the row says what the board said, in
            # full. With no credential on this host the ✎ is the door it always
            # was — the browser — and says so rather than opening a modal whose
            # save could only fail.
            if column == self.EDIT_COLUMN or record.get("page"):
                self.edit_dial(None if record.get("page") else record)
            else:
                self.say(qd.dial_detail(record))
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
        """Kick off /fix-issue for an issue — THROUGH `qb-start`, since #371.

        It used to compose `claude -- /fix-issue N` here and hand it to tmux, and
        what that started was a session nothing could count: outside `qb-admit`'s
        in-flight window, holding no claim, and known to the board only once the
        agent's own SessionStart hook got round to saying so. `qb-start` is the
        primitive that fixes all three (#277, #360) and until now nothing pulled
        it. This is its first caller: the cheapest possible trigger, because a
        click is still a human hand and so it needs no new safety at all — the
        gates, the machine cap, the allowlist and the claim are all at the
        primitive, and this only has to ask it.

        **AND ON A MACHINE THAT HAS NOT OPTED IN IT REFUSES, WITH THE REMEDY.**
        That is the obstacle #360 named and declined to walk into: `qb-start`
        ships off, so routing a working button through it makes the button stop
        working until somebody writes one line of nix. The alternative — fall
        back to the old direct spawn when the gate says no — was rejected, and
        the argument is in `spawn_refusal`.

        A held issue is now refused rather than warned about, which reverses this
        method's own previous sentence. That sentence was right about a click
        that took NO CLAIM: warning and letting you proceed cost nothing but your
        own judgement. This click takes the claim, so proceeding is `qb-claim`
        refusing at exit 8 — a dialog whose only possible outcome is no. What was
        a warning is now the answer, and it names the way to release a claim that
        has genuinely lapsed.

        **THIS ONE READS A SNAPSHOT, AND SAYS SO.** `self.held` is the board's
        answer from up to one poll ago, so a claim released two seconds back is
        still on it and this refuses work that is in fact free — the one thing
        the atomic claim would have got right. Which is why the message names its
        source and the key that re-reads it rather than stating the holder as a
        fact: `qb-claim` remains the authority, and it is still the one that
        settles the race in the other direction, where the panel shows an issue
        as free and the spawn is refused at exit 8. What is traded is a stale
        refusal a keypress fixes, against a confirmation dialog that could only
        ever end in the same no, three board round trips later.
        """
        number = issue.get("number")
        # Somebody else's work redone under the wrong title, if this is skipped —
        # see wrong_repo, which the ⚖ shares because the mistake is the same one.
        if (why := self.wrong_repo(issue.get("repo"), f"#{number}")):
            self.say(why)
            return
        # UNKNOWN IS NOT FREE. `self.held` is None until the board answers, and
        # `or {}` here would read that as "nothing is claimed" at the one click
        # that spends money — reachable, because the PLANS ⚒ arrives on its own
        # worker and can be live while ISSUES is still waiting. The wait is a
        # poll long and `r` shortens it.
        if self.held is None or self.claims_err:
            self.say(f"#{number}: the board has not answered"
                     + (f" ({qd.clip(self.claims_err, 40)})" if self.claims_err else " yet")
                     + ", so nothing here knows whether it is claimed — `r` re-reads it")
            return
        if (holder := holders(self.held).get(qd.issue_key(issue))):
            self.say(f"#{number} is claimed by {holder} (the board's last answer, "
                     f"`r` re-reads it) — the spawn takes that claim, so "
                     f"`qb-release issue {number}` is what frees it")
            return
        self.start_work("/fix-issue", number, f"start /fix-issue on #{number}?")

    # ---- the ⚒, through qb-start ------------------------------------------

    def start_work(self, command: str, number: int | str, prompt: str) -> None:
        """Ask this machine, then ask the human, then ask `qb-start`.

        In that order, and the first one is the point. A button that raises a
        confirmation, takes the click and THEN says the machine never opted in
        has spent somebody's attention telling them something it could have
        known before it drew itself — and #371 is explicit that a button which
        appears to work and does not is worse than one that is absent.
        """
        if (why := self.spawn_refusal(command)):
            self.say(why)
            return
        # The window `qb-start` will make, spelled its way rather than this
        # panel's old `fix-<n>`: the message this ends on tells you what to go
        # and look at, and a name only the dashboard uses is one you cannot find.
        name = f"{command.lstrip('/')}-{number}"
        argv = self.spawn_argv(command, number)
        shown = " ".join(shlex.quote(a) for a in argv)
        if self.confirm:
            self.push_screen(
                Confirm(prompt, shown, self.repo),
                lambda go: self.run_spawn(name, argv) if go else self.say("cancelled"),
            )
        else:
            self.run_spawn(name, argv)

    def spawn_argv(self, command: str, number: int | str) -> list[str]:
        """What the ⚒ runs. `--via dash` is the provenance #371 asks for: it lands
        on the claim note, on the board post and on the pane as `@qb_spawn_via`,
        so a session somebody finds running can be traced to the click that asked
        for it rather than guessed at."""
        return [self.start_bin, command, str(number), "--repo-path", self.repo,
                "--via", "dash", "--json"]

    def spawn_refusal(self, command: str) -> str | None:
        """Why this machine will not start `command`, or None if it will.

        `qb-start --policy` is the question, so this is not a second reading of
        `spawn.json` — one gate, asked rather than reimplemented. It costs one
        local process and reads one file: no board, no tmux, no network. Asked on
        every click rather than cached at mount, so opting a machine in takes
        effect on the next click instead of on the next dashboard.

        **AND IT DOES NOT FALL BACK TO THE OLD SPAWN.** The tempting shape is
        obvious — refuse through `qb-start`, and when the machine has not opted
        in start the session the way this button did last week — and it is wrong
        three times over. It would make "this machine has not opted in" a fact
        about which code path ran rather than about the machine. It would put two
        behaviours behind one icon, a counted, claimed, board-recorded session on
        one box and an uncounted one on another, with nothing on screen to say
        which you got. And it would set the precedent for the next trigger, which
        will not have a human behind it. A permission with a fallback is not a
        permission; the honest cost is one line of nix, once, on the machine
        somebody wants this on.
        """
        try:
            got = subprocess.run([self.start_bin, "--policy", "--json"],
                                 capture_output=True, text=True, timeout=15)
        except Exception as exc:                       # noqa: BLE001
            # Fails CLOSED, and says which failure it was. `qb-start` missing is
            # a broken install, not a machine that said no, and the two want
            # different things done about them.
            return (f"⚒ cannot ask {self.start_bin} whether this machine may "
                    f"spawn ({type(exc).__name__}) — the ⚒ goes through qb-start "
                    f"now, and a gate that cannot be asked has not said yes")
        try:
            answer = json.loads(got.stdout)
        except ValueError:
            answer = {}
        if not answer.get("enabled"):
            return "⚒ " + qd.clip(
                answer.get("reason") or got.stderr.strip()
                or f"qb-start --policy exited {got.returncode} and said nothing", 240)
        if command not in (answer.get("commands") or []):
            return (f"⚒ {command} is not on this machine's allowlist — name it in "
                    f"`programs.quarterback-harness.spawn.commands` "
                    f"({answer.get('policy')} allows: "
                    f"{', '.join(answer.get('commands') or []) or 'nothing'})")
        return None

    @work(thread=True, group="spawn")
    def run_spawn(self, name: str, argv: list[str]) -> None:
        """Run `qb-start` and report what it answered.

        In a thread, unlike every other subprocess this app runs from a click:
        those are tmux calls that return in milliseconds, and this one asks
        `qb-pace`, `qb-admit` and `qb-claim` in turn — three board round trips
        before a pane exists. On the ui thread that is a dashboard that stops
        redrawing while it starts a session.

        Every refusal is reported in `qb-start`'s OWN words rather than
        translated by exit code here. There are seven of them, each with a
        different remedy, and a second copy of those sentences in this file would
        be a second copy to keep true.
        """
        # Outside tmux, `run_in_window`'s answer and its reason: the useful thing
        # to say is the exact command, and it is said AFTER the confirmation
        # because a dialog that never appears is also a dialog that cannot be
        # tested. Before `qb-start` runs, though, and that part is new — it would
        # otherwise pass every gate, take the claim and post the spawn before
        # discovering it has nowhere to put a pane, then hand both back. A
        # refusal that costs three board round trips is worse than the same
        # refusal made here.
        if not os.environ.get("TMUX"):
            self.call_from_thread(
                self.say, "not inside tmux — run it yourself: "
                          + " ".join(shlex.quote(a) for a in argv))
            return
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        except Exception as exc:                       # noqa: BLE001
            self.call_from_thread(
                self.say, f"could not run qb-start ({type(exc).__name__})")
            return
        self.call_from_thread(self.say, spawn_answer(name, done))


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

    def edit_dial(self, row: dict | None) -> None:
        """Open the editor on one dial — or on a new one, when `row` is None.

        **Falls back to the browser rather than to a dead modal.** With no session
        on this host the write cannot succeed, and a form that took four fields
        and then said so would have spent the person's typing to tell them
        something it knew before they started. The page is still there and still
        works, which is the whole reason the read-only version shipped first.
        """
        cannot = self.human.why_not() if self.human else "no board configured"
        if cannot:
            self.say(f"{cannot} — opening the board's dials page instead")
            self.open_url(qd.dials_url(self.cfg))
            return
        # WHICH SCOPE a new dial lands in, decided here rather than in the modal:
        # this screen already knows which project it is about, and a scope picker
        # in a 78-column modal is a control that would be got wrong in a hurry.
        # Editing an existing row keeps that row's own scope, which is the only
        # answer that can mean "change what I am looking at".
        repo = (row or {}).get("repo") if row else self.new_dial_scope()
        label = repo or "fleet (every repo)"
        self.push_screen(DialEdit(row, repo, label), self.dial_written)

    def new_dial_scope(self) -> str | None:
        """Which repo a NEW dial belongs to — off the SCOPE, never off the cwd.

        The rows on this pane are `Scope`'s (`QB_DASH_REPOS`, or `--repo`, or the
        launch directory's origin, in that order). `self.repo_slug` is only the
        last of those, and it is where work is LAUNCHED rather than what is being
        shown: a pane started in one checkout with `QB_DASH_REPOS=owner/other`
        displays `other`'s dials and would have written the dial to the checkout's
        repo instead. Same name, different setting, and nothing on screen
        afterwards says which one took it — the one mistake this panel must not
        let a person make.

        None means the fleet, and it is the honest answer twice over: a wide pane
        is about several projects and cannot choose between them, and a sole repo
        this process knows only by a bare name is not one the board would accept
        (`owner/name` is its shape). The modal states whichever answer this gives
        in bold before anything is written.
        """
        if self.scope.column:
            return None                      # several projects, or the wide view
        named = [r for r in self.scope.repos if "/" in r]
        return named[0] if len(named) == 1 else None

    def dial_written(self, asked: dict | None) -> None:
        """What the modal came back with, turned into one board write.

        Runs on the UI thread and hands the HTTP off to a worker: `op read` can
        prompt and the board can be slow, and a dashboard that froze mid-write
        would look like the thing it is trying to avoid being.
        """
        if not asked or not asked.get("dial"):
            return
        if asked.get("clear"):
            self.run_dial_write(asked, None, None)
            return
        try:
            value = qd.parse_dial_value(asked.get("value", ""))
            expires = qd.parse_dial_expiry(asked.get("expiry", ""),
                                           (self.dials or {}).get("now"))
        except (ValueError, OverflowError) as exc:
            # Refused HERE, where the sentence can name the box that was wrong,
            # rather than at a 422 that names a field nobody typed.
            #
            # OverflowError as well as ValueError, and it is not defensive
            # padding: `timedelta` raises it rather than ValueError for a duration
            # past its range, so the bounded regex and this clause are two halves
            # of one fix. Escaping here is a crash inside a Textual callback,
            # which takes the dashboard down and every other panel with it.
            self.say(str(exc))
            return
        if not (asked.get("reason") or "").strip():
            self.say("a dial needs a reason — why is this value in force? "
                     "The board refuses one without, and so does this")
            return
        self.run_dial_write(asked, value, expires)

    @work(thread=True, exclusive=True, group="dialwrite")
    def run_dial_write(self, asked: dict, value, expires: str | None) -> None:
        """The write itself, off the UI thread. Never raises into Textual."""
        dial, repo = asked["dial"], asked.get("repo")
        try:
            if asked.get("clear"):
                got = self.human.clear_dial(dial, repo)
                cleared = got.get("cleared") or []
                said = (f"cleared {dial} — the repo's own default takes over"
                        if cleared else f"{dial} was already gone")
            else:
                got = self.human.set_dial(dial, value, asked["reason"], repo, expires)
                # WHAT IT REPLACED, said out loud: moving a dial without being told
                # what it was is how one gets nudged twice by two people who each
                # believed they were starting from the default. The endpoint
                # returns the old row for exactly this sentence.
                was = [f"{qd.dial_value(d, 40)} ({d.get('reason')})"
                       for d in (got.get("replaced") or [])]
                said = f"set {dial}" + (f" — it was {', '.join(was)}" if was else "")
        except Exception as exc:                  # noqa: BLE001 — show it, don't die
            said = f"{dial}: {exc}"
        self.call_from_thread(self.say, qd.clip(said, 400))
        # Straight back to the board rather than waiting out the plan clock: the
        # person is looking at the row they just changed, and a panel that showed
        # the old value for fifteen seconds would be read as a write that failed.
        self.call_from_thread(self.refresh_plan)

    def open_dials(self) -> None:
        """The board's dials page — the only surface that can actually turn one.

        Said out loud in the detail line rather than left to be inferred from a
        browser opening. #443 is the record of what the silent version costs: a
        person told the reorder was theirs to do, in a terminal, whose reply was
        "i don't know how to re-order". A door nobody can find is the same as no
        door.

        **Still worth a key now that the panel can write.** The page shows every
        repo's dials at once where this panel shows the screen's own, so a person
        comparing two projects wants it. The `✎` on a row is the control; this is
        the map. It is also the fallback when this host has no key.
        """
        self.open_url(qd.dials_url(self.cfg))
        self.say("the board's dials page — every repo's dials at once, where this "
                 "panel shows the screen's own. Setting one from here needs a "
                 "person's key (QUARTERBACK_HUMAN_KEY_CMD); the page needs a "
                 "browser the edge has vouched for.")

    def open_url(self, url: str) -> None:
        try:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            self.say(f"opened {url}")
        except Exception as exc:                   # noqa: BLE001
            self.say(f"could not open ({type(exc).__name__}): {url}")

    # ---- key actions -----------------------------------------------------

    def action_open_dials(self) -> None:
        """`d` — from any table, because a dial governs all of them."""
        self.open_dials()

    def action_refresh_now(self) -> None:
        self.refresh_board()
        self.refresh_plan()          # …which fetches the dials with it
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
                 "plan item · d the board's dials page · s this project's rows or the "
                 "whole fleet's · r refresh · q quit · click ⚖ to review, ⚒ to fix, "
                 "✎ to set or clear a dial (ctrl+s saves, ctrl+x clears), a plan row "
                 "for why it is there, a seat to jump to its pane")

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
