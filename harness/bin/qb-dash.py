#!/usr/bin/env python3
"""qb-dash — the fleet at a glance, for a tall pane beside the seats.

The tape (`qb-board --follow`) answers "what just happened". This answers the
other question: who is alive right now, what have they claimed, and what is
waiting to land. State, not events.

  qb-dash              live, redrawing
  qb-dash --once       one frame and exit (what the tests and a pipe want)
  qb-dash --width 72   force a width instead of taking the terminal's

Board data comes from the same client the MCP server uses; PRs and issues come
from `gh`, on a slower clock because that is a network call per refresh and
neither moves every three seconds.
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
    ago, board_client, ci_state, clip, fetch_board, fetch_issues, fetch_prs,
    issue_claims, short_key, sort_issues, until,
)

BOARD_EVERY = 4.0       # seconds; presence changes on this order
GH_EVERY = 90.0         # gh is a network round trip, and PRs/issues are not live data
ISSUE_ROWS = 12         # a printed panel cannot scroll; the rest is a count

# Repo → colour, so the same project is the same colour everywhere on the panel.
REPO_COLOURS = ["cyan", "magenta", "green", "yellow", "blue", "red"]
_repo_colour: dict[str, str] = {}


def repo_colour(name: str) -> str:
    if name not in _repo_colour:
        _repo_colour[name] = REPO_COLOURS[len(_repo_colour) % len(REPO_COLOURS)]
    return _repo_colour[name]


# ---- panels ------------------------------------------------------------------

def panel_agents(data: dict, width: int) -> Panel:
    agents = sorted(data.get("agents", []), key=lambda a: (a.get("repo") or "", a.get("holder") or ""))
    seats = [a for a in agents if "/seat-" in (a.get("holder") or "")]

    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(width=13, no_wrap=True)          # who
    t.add_column(width=11, no_wrap=True)          # repo
    t.add_column(ratio=1, no_wrap=True)           # what
    t.add_column(width=5, justify="right", no_wrap=True)   # ttl

    body = max(18, width - 37)
    for a in agents:
        who = (a.get("holder") or "?").split("/", 1)[-1]
        repo = a.get("repo") or "—"
        title = a.get("title") or a.get("branch") or "—"
        is_seat = "/seat-" in (a.get("holder") or "")
        t.add_row(
            Text(clip(who, 13), style="bold white on dark_green" if is_seat else "bold"),
            Text(clip(repo, 11), style=repo_colour(repo)),
            Text(clip(title, body), style="white" if is_seat else "grey70"),
            Text(until(a.get("expires")), style="grey50"),
        )
    if not agents:
        t.add_row(Text("nobody home", style="grey50"), "", "", "")

    subs = len(data.get("subagents") or [])
    head = f"[bold]FLEET[/] [grey50]{len(agents)} live"
    if seats:
        head += f" · [green]{len(seats)} seat{'s' if len(seats) != 1 else ''}[/]"
    if subs:
        head += f" · {subs} sub"
    return Panel(t, title=head + "[/]", title_align="left", border_style="grey35", padding=(0, 1))


def panel_claims(data: dict, width: int) -> Panel:
    claims = sorted(data.get("claims", []), key=lambda c: c.get("expires") or "")
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(width=13, no_wrap=True)
    t.add_column(ratio=1, no_wrap=True)
    t.add_column(width=5, justify="right", no_wrap=True)

    for c in claims:
        who = (c.get("holder") or "?").split("/", 1)[-1]
        key = short_key(c.get("key") or "?")
        kind = c.get("kind") or ""
        left = until(c.get("expires"))
        t.add_row(
            Text(clip(who, 13), style="bold"),
            Text(clip(f"{key}", max(10, width - 27)),
                 style="yellow" if kind == "issue" else "grey70"),
            Text(left, style="red" if left.endswith("m") and left[:-1].isdigit()
                 and int(left[:-1]) < 10 else "grey50"),
        )
    if not claims:
        t.add_row(Text("nothing claimed", style="grey50"), "", "")
    return Panel(t, title=f"[bold]CLAIMED[/] [grey50]{len(claims)}[/]",
                 title_align="left", border_style="grey35", padding=(0, 1))


def panel_prs(prs: list[dict], err: str | None, width: int) -> Panel:
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(width=1, no_wrap=True)           # ci
    t.add_column(width=4, justify="right", no_wrap=True)   # number
    t.add_column(ratio=1, no_wrap=True)           # title
    t.add_column(width=5, justify="right", no_wrap=True)   # age

    red = 0
    for pr in sorted(prs, key=lambda p: -p.get("number", 0)):
        glyph, colour = ci_state(pr)
        red += colour == "red"
        title = pr.get("title") or ""
        t.add_row(
            Text(glyph, style=colour),
            Text(f"#{pr.get('number')}", style="bold grey70"),
            Text(clip(title, max(12, width - 21)),
                 style="grey50" if pr.get("isDraft") else "white"),
            Text(ago(pr.get("updatedAt")), style="grey50"),
        )
    if err:
        t.add_row(Text("!", style="red"), "", Text(clip(err, width - 12), style="red"), "")
    if not prs and not err:
        t.add_row("", "", Text("no open PRs", style="grey50"), "")

    head = f"[bold]OPEN PRs[/] [grey50]{len(prs)}"
    if red:
        head += f" · [red]{red} red[/]"
    return Panel(t, title=head + "[/]", title_align="left", border_style="grey35",
                 padding=(0, 1))


def panel_issues(issues: list[dict], held: dict[int, dict], err: str | None,
                 width: int) -> Panel:
    """Open issues, with the ones somebody already holds marked as such.

    The free ones are the point — an unheld issue is what the next seat takes —
    so they sort to the top, and a held one stays in the list but goes grey and
    gives its right-hand column over to the holder instead of its age.

    This panel is printed, not scrolled, and it is the last one on a pane that
    the others already share: past ISSUE_ROWS it stops listing and says how many
    it did not, rather than pushing the fleet off the top of the screen.
    """
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(width=1, no_wrap=True)                     # held marker
    t.add_column(width=4, justify="right", no_wrap=True)    # number
    t.add_column(ratio=1, no_wrap=True)                     # title
    t.add_column(width=9, justify="right", no_wrap=True)    # holder, or age

    ordered = sort_issues(issues, held)
    free = sum(1 for i in issues if i.get("number") not in held)
    for issue in ordered[:ISSUE_ROWS]:
        claim = held.get(issue.get("number"))
        who = (claim.get("holder") or "?").split("/", 1)[-1] if claim else ""
        t.add_row(
            Text("·" if claim else "○", style="grey50" if claim else "green"),
            Text(f"#{issue.get('number')}", style="bold grey70"),
            Text(clip(issue.get("title"), max(12, width - 25)),
                 style="grey50" if claim else "white"),
            Text(clip(who, 9) if claim else ago(issue.get("updatedAt")),
                 style="yellow" if claim else "grey50"),
        )
    if len(ordered) > ISSUE_ROWS:
        t.add_row("", "", Text(f"…and {len(ordered) - ISSUE_ROWS} more", style="grey50"), "")
    if err:
        t.add_row(Text("!", style="red"), "", Text(clip(err, width - 16), style="red"), "")
    if not issues and not err:
        t.add_row("", "", Text("no open issues", style="grey50"), "")

    head = f"[bold]ISSUES[/] [grey50]{len(issues)}"
    if issues:
        head += f" · [green]{free} free[/]"
    return Panel(t, title=head + "[/]", title_align="left", border_style="grey35",
                 padding=(0, 1))


def header(cfg, data: dict, width: int) -> Panel:
    host = (cfg.agent or "?").split("/", 1)[0]
    now = datetime.now().strftime("%H:%M:%S")
    state = Text("● board up", style="green")
    if data.get("error"):
        state = Text("● board unreachable", style="bold red")
    line = Table.grid(expand=True)
    line.add_column(ratio=1)
    line.add_column(justify="right")
    line.add_row(Text(f"quarterback · {host}", style="bold"), state)
    sub = Text(f"{cfg.base_url}   {now}", style="grey50")
    return Panel(Group(line, Align.left(sub)), border_style="grey35", padding=(0, 1))


def fetch_gh() -> dict:
    """The two `gh` calls, together: they share a clock and a failure mode."""
    prs, pr_err = fetch_prs()
    issues, issue_err = fetch_issues()
    return {"prs": prs, "pr_err": pr_err, "issues": issues, "issue_err": issue_err}


def frame(cfg, data: dict, gh: dict, width: int) -> Group:
    held = issue_claims(data.get("claims", []))
    parts = [header(cfg, data, width), panel_agents(data, width),
             panel_claims(data, width),
             panel_prs(gh["prs"], gh["pr_err"], width),
             panel_issues(gh["issues"], held, gh["issue_err"], width)]
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
    args = ap.parse_args()

    console = Console(width=args.width) if args.width else Console()
    width = console.width

    try:
        client, cfg = board_client()
    except Exception as exc:                      # noqa: BLE001
        console.print(f"[red]qb-dash: no board configured ({type(exc).__name__}: {exc})[/]")
        return 1

    data = fetch_board(client)
    gh = fetch_gh()

    if args.once:
        console.print(frame(cfg, data, gh, width))
        return 0

    last_gh = time.monotonic()
    with Live(frame(cfg, data, gh, width), console=console,
              screen=True, refresh_per_second=4) as live:
        while True:
            time.sleep(args.interval)
            data = fetch_board(client)
            if time.monotonic() - last_gh >= GH_EVERY:
                gh = fetch_gh()
                last_gh = time.monotonic()
            width = console.width          # the pane can be resized under us
            live.update(frame(cfg, data, gh, width))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
