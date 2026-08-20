#!/usr/bin/env python3
"""qb-dash — the fleet at a glance, for a tall pane beside the seats.

The tape (`qb-board --follow`) answers "what just happened". This answers the
other question: who is alive right now, and what work is there. State, not events.

Three panels. SEATS and FLEET say who is here; WORK is everything there is to do,
as one table. WORK used to be four panels — CLAIMED, PLANS, OPEN PRs, ISSUES —
which meant one piece of work could be three or four rows with nothing on the
pane saying they were the same thing. The plan, the issues, the PRs and the claims
are joined on `owner/repo#n` and printed in the plan's order (#272).

  qb-dash              live, redrawing
  qb-dash --once       one frame and exit (what the tests and a pipe want)
  qb-dash --width 72   force a width instead of taking the terminal's
  qb-dash --scope all  every repo on the board, not just this screen's
  qb-dash --repo ~/src/nix-fleet    point it at a project other than the cwd's

By default it shows ONE project's rows — the repos of the checkout it was started
in — and drops the repo column, because a screen built for one project spends
eleven columns of a narrow pane restating its name (#261). `--scope all` is the
fleet-wide view; the clickable renderer toggles between the two with `s`, which
this one has no keyboard for.

Board data comes from the same client the MCP server uses; PRs and issues come
from `gh`, on a slower clock because that is a network call per refresh and
neither moves every three seconds. The line across the top is Claude Code's own
usage caps — the ceiling every seat below it is working towards, on a slower
clock again.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qbdata import (  # noqa: E402
    LIMITS_EVERY, Scope, agent_state, board_client, clip, fetch_board, fetch_issues,
    fetch_limits, fetch_plan, fetch_prs, in_scope, limit_cells, repo_arg, repo_colour,
    resolve_scope, set_repos, short_repo, until, work_dim, work_kind, work_ref,
    work_rows, work_state, work_title, work_who,
)

BOARD_EVERY = 4.0       # seconds; presence changes on this order
GH_EVERY = 90.0         # gh is a network round trip, and PRs/issues are not live data
# A printed panel cannot scroll, so past this it says how many it left out. One
# number where there were two (PLAN_ROWS and ISSUE_ROWS), because there is one
# table now — and the merge itself buys rows back, since an issue that is also a
# plan item and a claim used to be three of them (#272). Whether the FRAME as a
# whole fits the pane is a different defect, and it is #269's.
WORK_ROWS = 24

# Repo → colour, so the same project is the same colour everywhere on the panel.
# ---- panels ------------------------------------------------------------------


def _elsewhere(hidden: int) -> str:
    """What a narrowed panel adds to its own title, or nothing.

    Every panel that filters says so, because a panel that filtered silently is a
    panel lying about the fleet: "nothing claimed" and "nothing claimed HERE" are
    different facts, and the second one is the one a reader is being shown.
    """
    return f" · {hidden} elsewhere" if hidden else ""

def panel_agents(data: dict, width: int, scope: Scope | None = None) -> Panel:
    agents = sorted(data.get("agents", []), key=lambda a: (a.get("repo") or "", a.get("holder") or ""))
    agents, elsewhere = in_scope(agents, scope)
    seats = [a for a in agents if "/seat-" in (a.get("holder") or "")]
    show_repo = scope is None or scope.column

    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(width=13, no_wrap=True)          # who
    t.add_column(width=7, no_wrap=True)           # state
    if show_repo:
        t.add_column(width=11, no_wrap=True)      # repo
    t.add_column(ratio=1, no_wrap=True)           # what
    t.add_column(width=5, justify="right", no_wrap=True)   # ttl

    # The cell's width plus its padding goes back to `what`, which is the column
    # a reader is actually reading: what the agent in this seat is doing.
    body = max(18, width - (45 if show_repo else 33))
    for a in agents:
        who = (a.get("holder") or "?").split("/", 1)[-1]
        repo = a.get("repo") or "—"
        title = a.get("title") or a.get("branch") or "—"
        is_seat = "/seat-" in (a.get("holder") or "")
        word, style = agent_state(a)
        cells = [
            Text(clip(who, 13), style="bold white on dark_green" if is_seat else "bold"),
            Text(word or "—", style=style),
        ]
        if show_repo:
            cells.append(Text(clip(repo, 11), style=repo_colour(repo)))
        cells += [
            Text(clip(title, body), style="white" if is_seat else "grey70"),
            Text(until(a.get("expires")), style="grey50"),
        ]
        t.add_row(*cells)
    if not agents:
        t.add_row(Text("nobody home", style="grey50"), *[""] * (4 if show_repo else 3))

    subs = len(data.get("subagents") or [])
    head = f"[bold]FLEET[/] [grey50]{len(agents)} live"
    if seats:
        head += f" · [green]{len(seats)} seat{'s' if len(seats) != 1 else ''}[/]"
    if subs:
        head += f" · {subs} sub"
    head += _elsewhere(elsewhere)
    return Panel(t, title=head + "[/]", title_align="left", border_style="grey35", padding=(0, 1))


def panel_work(data: dict, gh: dict, width: int, scope: Scope | None = None) -> Panel:
    """Every unit of work the fleet can see, as ONE list, in the plan's order.

    This was four panels — CLAIMED, PLANS, OPEN PRs, ISSUES — and they were four
    answers to one question, drawn from four sources, about overlapping rows. An
    issue that was also a plan item and also claimed appeared three times, and
    nothing on the pane said the three rows were one piece of work: the reader did
    that join by eye, every time, and the panel borders were what made it
    necessary (#272). qbdata.work_rows does the join once; this prints it.

    NO ACTION COLUMN, unlike the clickable renderer. Nothing here can be clicked,
    and an icon offering a verb that this pane cannot perform would be furniture
    pretending to be a control.

    Printed, so it does not scroll: past WORK_ROWS it says how many it left out
    rather than pushing the panels above it off the top of the screen.
    """
    rows = work_rows(data.get("plan") or [], gh.get("issues") or [],
                     gh.get("prs") or [], data.get("claims") or [])
    rows, elsewhere = in_scope(rows, scope)
    show_repo = scope is None or scope.column

    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(width=1, no_wrap=True)                     # state
    t.add_column(width=5, no_wrap=True)                     # kind
    if show_repo:
        t.add_column(width=11, no_wrap=True)                # repo
    t.add_column(width=5, justify="right", no_wrap=True)    # ref
    t.add_column(ratio=1, no_wrap=True)                     # title
    t.add_column(width=13, justify="right", no_wrap=True)   # who

    filler = [""] * (4 if show_repo else 3)
    for row in rows[:WORK_ROWS]:
        glyph, colour = work_state(row)
        kind, kind_colour = work_kind(row)
        who, who_colour = work_who(row)
        repo = short_repo(row["repo"] or "fleet")
        cells = [Text(glyph, style=colour), Text(kind, style=kind_colour)]
        if show_repo:
            cells.append(Text(clip(repo, 11), style=repo_colour(repo)))
        cells += [
            Text(work_ref(row, scope), style="bold grey70"),
            Text(clip(row["title"] or "—", max(12, width - (40 if show_repo else 28))),
                 style="grey50" if work_dim(row) else "white"),
            Text(clip(who, 13), style=who_colour),
        ]
        t.add_row(*cells)
    if len(rows) > WORK_ROWS:
        t.add_row(*filler, Text(f"…and {len(rows) - WORK_ROWS} more", style="grey50"), "")

    # Every source's failure, named. Three of them can fail independently, and a
    # table that drew the other two while saying nothing about the third would be
    # claiming to be the whole of the work when it was not.
    errs = [e for e in (data.get("plan_err"), gh.get("pr_err"), gh.get("issue_err")) if e]
    for err in errs:
        t.add_row(Text("!", style="red"), *filler[1:],
                  Text(clip(err, max(12, width - 16)), style="red"), "")
    if not rows and not errs:
        t.add_row(*filler, Text("nothing to do — no plan, no issues, no PRs",
                                style="grey50"), "")

    name, _, counts = work_title(rows, elsewhere).partition(" · ")
    head = f"[bold]{name}[/] [grey50]{counts}"
    return Panel(t, title=head + "[/]", title_align="left", border_style="grey35",
                 padding=(0, 1))


def limits_line(limits: list[dict], width: int, stale: bool = False) -> Text:
    """The shared subscription's caps, as bars: `5h ████░░ 64% 3h57m  7d ██░ 41% 5d8h`.

    At the top because it is the one number that governs every pane below it —
    the seats spend one subscription between them, and a window they are about
    to exhaust is worth knowing before an agent stops mid-issue rather than
    after.
    """
    out = Text()
    for i, (label, bar, pct, reset, colour) in enumerate(limit_cells(limits, width)):
        if i:
            out.append("  ")
        out.append(label, style="bold grey70")
        if bar:
            out.append(f" {bar}", style=colour)
        out.append(f" {pct}", style=f"bold {colour}")
        if reset:
            out.append(f" {reset}", style="grey50")
    # A failed call keeps the last figures rather than blanking the line — they
    # are minutes old and still roughly true — but says so, because a bar frozen
    # at 64% while six seats work is the one reading that would mislead.
    if out.plain and stale:
        out.append(" ?", style="grey50")
    return out


def header(cfg, data: dict, width: int, limits: list[dict] | None = None,
           stale: bool = False, scope: Scope | None = None) -> Panel:
    host = (cfg.agent or "?").split("/", 1)[0]
    now = datetime.now().strftime("%H:%M:%S")
    state = Text("● board up", style="green")
    if data.get("error"):
        state = Text("● board unreachable", style="bold red")
    line = Table.grid(expand=True)
    line.add_column(ratio=1)
    line.add_column(justify="right")
    line.add_row(Text(f"quarterback · {host}", style="bold"), state)
    # The scope, said ONCE for the whole pane. That is the trade the panels below
    # are making: the repo column comes out of every row of every table, so the
    # one place that still names the project has to be somewhere a reader looks.
    where = f"   {scope.label()}" if scope is not None else ""
    sub = Text(f"{cfg.base_url}   {now}{where}", style="grey50")
    parts = [line, Align.left(sub)]
    caps = limits_line(limits or [], width - 4, stale)
    if caps.plain:
        parts.insert(0, Align.left(caps))
    return Panel(Group(*parts), border_style="grey35", padding=(0, 1))


def fetch_state(client) -> dict:
    """The board's own three answers in one dict: presence, claims, and the plan.

    The plan rides the board clock rather than the `gh` one — it is a call to the
    same host as /active, it changes when an agent claims an item, and a plan
    panel that lags a claim by ninety seconds shows work as free that somebody
    already took.
    """
    data = fetch_board(client)
    data["plan"], data["plan_err"] = fetch_plan(client)
    return data


def refresh_limits(caps: dict) -> dict:
    """Update `caps` in place from the usage endpoint, keeping the last good figures.

    A failed call must not blank the line. The caps move on the scale of hours,
    so figures a few minutes old are still the right ones to act on, and a line
    that vanished every time the network hiccuped would be read as "no limits" —
    the opposite of what it means. qbdata keeps the last answer too, across
    processes; this is the same rule inside one.
    """
    limits, err = fetch_limits()
    if limits:
        caps["limits"] = limits
    caps["error"] = err
    return caps


def fetch_gh() -> dict:
    """The two `gh` calls, together: they share a clock and a failure mode."""
    prs, pr_err = fetch_prs()
    issues, issue_err = fetch_issues()
    return {"prs": prs, "pr_err": pr_err, "issues": issues, "issue_err": issue_err}


def frame(cfg, data: dict, gh: dict, width: int, caps: dict | None = None,
          scope: Scope | None = None) -> Group:
    caps = caps or {}
    parts = [header(cfg, data, width, caps.get("limits"), bool(caps.get("error")), scope),
             panel_agents(data, width, scope),
             panel_work(data, gh, width, scope)]
    if data.get("error"):
        parts.append(Panel(Text(clip(data["error"], width * 2), style="red"),
                           title="[red]ERROR[/]", title_align="left", border_style="red"))
    return Group(*parts)


# ---- main --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(prog="qb-dash", description="fleet state, for a tall pane")
    ap.add_argument("--once", action="store_true", help="render one frame and exit")
    ap.add_argument("--width", type=int, default=None, help="force a width")
    ap.add_argument("--interval", type=float, default=BOARD_EVERY, help="board refresh seconds")
    ap.add_argument("--scope", choices=("repo", "all"), default=None,
                    help="repo (default): only this screen's repos, and no repo column; "
                         "all: every repo on the board. Overrides QB_DASH_SCOPE")
    ap.add_argument("--repo", action="append", metavar="PATH|OWNER/NAME",
                    help="the project this screen is for — a checkout or a slug, "
                         "repeatable. Overrides QB_DASH_REPOS and the cwd")
    args = ap.parse_args()

    console = Console(width=args.width) if args.width else Console()
    width = console.width

    # Before anything reads it: `resolve_repos` is cached and half the module asks
    # it directly (which repos to sort the plan by, which repos to ask `gh` about),
    # so --repo has to land in that cache rather than be passed around.
    if args.repo:
        try:
            set_repos([repo_arg(r) for r in args.repo])
        except ValueError as exc:
            console.print(f"[red]qb-dash: --repo {exc}[/]")
            return 2
    scope = resolve_scope(on=None if args.scope is None else args.scope == "repo")

    try:
        client, cfg = board_client()
    except Exception as exc:                      # noqa: BLE001
        console.print(f"[red]qb-dash: no board configured ({type(exc).__name__}: {exc})[/]")
        return 1

    data = fetch_state(client)
    gh = fetch_gh()
    caps = refresh_limits({"limits": [], "error": None})

    if args.once:
        console.print(frame(cfg, data, gh, width, caps, scope))
        return 0

    last_gh = last_caps = time.monotonic()
    with Live(frame(cfg, data, gh, width, caps, scope), console=console,
              screen=True, refresh_per_second=4) as live:
        while True:
            time.sleep(args.interval)
            data = fetch_state(client)
            if time.monotonic() - last_gh >= GH_EVERY:
                gh = fetch_gh()
                last_gh = time.monotonic()
            if time.monotonic() - last_caps >= LIMITS_EVERY:
                refresh_limits(caps)
                last_caps = time.monotonic()
            width = console.width          # the pane can be resized under us
            live.update(frame(cfg, data, gh, width, caps, scope))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
