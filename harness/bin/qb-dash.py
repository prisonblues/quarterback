#!/usr/bin/env python3
"""qb-dash — the fleet at a glance, for a tall pane beside the seats.

The tape (`qb-board --follow`) answers "what just happened". This answers the
other question: who is alive right now, what have they claimed, and what is
waiting to land. State, not events.

  qb-dash              live, redrawing
  qb-dash --once       one frame and exit (what the tests and a pipe want)
  qb-dash --width 72   force a width instead of taking the terminal's
  qb-dash --scope all  every repo the BOARD knows, not just this screen's
  qb-dash --repo ~/src/nix-fleet    point it at a project other than the cwd's

By default it shows ONE project's rows — the repos of the checkout it was started
in — and drops the repo column, because a screen built for one project spends
eleven columns of a narrow pane restating its name (#261). `--scope all` widens the
three panels that come off the BOARD (FLEET, CLAIMED, PLANS); OPEN PRs, REVIEW
QUEUE and ISSUES cannot widen, because `gh` is only ever asked about the repos
this dashboard watches. The clickable renderer toggles with `s`, which this one
has no keyboard for.

REVIEW QUEUE is the one panel that is neither board state nor `gh` state but a
join of the two (#273): every open PR that review is not finished with, what it
is waiting for, and how long it has waited. Its depth and oldest age also sit on
the caps line, beside the budget they would be spent out of.

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
    LIMITS_EVERY, PR_ROWS, QUEUE_COLOUR, QUEUE_HOLD, QUEUE_VERB, Scope, agent_state, ago, board_client, ci_counts, ci_state,
    claim_label, claim_repo, clip, elsewhere, fetch_board, fetch_issues, fetch_limits, fetch_plan,
    fetch_prs, fetch_review_queue, claims_by_issue, in_scope, issue_key, limit_cells,
    plan_head_bits, plan_items, plan_next_id, plan_rank, plan_ref, plan_state, plan_who,
    queue_cell, queue_oldest, repo_arg, repo_colour,
    resolve_scope, scope_mark, set_repos, short_repo, sort_issues, until, waited,
)

BOARD_EVERY = 4.0       # seconds; presence changes on this order
GH_EVERY = 90.0         # gh is a network round trip, and PRs/issues are not live data
ISSUE_ROWS = 12         # a printed panel cannot scroll; the rest is a count
PLAN_ROWS = 10          # the same, for the plan: running items first, then a count
QUEUE_ROWS = 8          # the same again, for the review queue: oldest first

# Repo → colour, so the same project is the same colour everywhere on the panel.
# ---- panels ------------------------------------------------------------------


def panel_agents(data: dict, width: int, scope: Scope | None = None) -> Panel:
    agents = sorted(data.get("agents", []), key=lambda a: (a.get("repo") or "", a.get("holder") or ""))
    agents, hidden = in_scope(agents, scope)
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
            # The mark rides on the cell the dropped column widened: with no repo
            # cell, an agent working outside any checkout otherwise reads as one
            # working here (qbdata.scope_mark).
            Text(scope_mark(scope, a.get("repo")) + clip(title, body),
                 style="white" if is_seat else "grey70"),
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
    head += elsewhere(hidden)
    return Panel(t, title=head + "[/]", title_align="left", border_style="grey35", padding=(0, 1))


def panel_claims(data: dict, width: int, scope: Scope | None = None) -> Panel:
    claims = sorted(data.get("claims", []), key=lambda c: c.get("expires") or "")
    # A claim's repo is in its KEY, not in a field of its own, and a `plan:<uuid>`
    # key names an item rather than a repo — so the plan goes in with it, and a
    # claim neither can attribute stays (see qbdata.claim_repo).
    plan = plan_items(data.get("plan"))
    claims, hidden = in_scope(claims, scope, lambda c: claim_repo(c.get("key"), plan))
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(width=13, no_wrap=True)
    t.add_column(ratio=1, no_wrap=True)
    t.add_column(width=5, justify="right", no_wrap=True)

    for c in claims:
        who = (c.get("holder") or "?").split("/", 1)[-1]
        key = claim_label(c.get("key") or "?", plan, scope)
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
        # In the WIDE column, not the 13-wide holder one, which rendered this as
        # "nothing clai…" — a panel whose empty state is itself truncated.
        t.add_row("", Text("nothing claimed", style="grey50"), "")
    return Panel(t, title=f"[bold]CLAIMED[/] [grey50]{len(claims)}{elsewhere(hidden)}[/]",
                 title_align="left", border_style="grey35", padding=(0, 1))


def _title_room(width: int, show_repo: bool, show_rank: bool, who_width: int) -> int:
    """How many columns the title cell has left once the fixed ones have taken theirs.

    The panel's border and padding cost 4, and `Table.grid(padding=(0, 1))` puts a
    space after every column but the last.
    """
    fixed = [1] + ([11] if show_repo else []) + ([4] if show_rank else []) + [6, who_width]
    return width - 4 - sum(fixed) - len(fixed)


def panel_plan(plan: dict, err: str | None, width: int,
               scope: Scope | None = None) -> Panel:
    """What the fleet agreed to do next, in the board's own order.

    FLEET says who is here and CLAIMED says what they hold; neither says what
    the work is FOR. This is the board's plan — one ordered list per repo, plus
    the fleet-wide one.

    **The order is the board's and is not re-derived here.** A plan is an ordered
    list, that order is a human decision, and this panel used to re-band it
    locally — taken, then free, then blocked — which is a second answer about the
    plan computed against the plan's own answer, and the reason the two surfaces
    disagreed about what was next. What the banding was FOR was finding the row a
    seat can pick up, and the board answers that outright: `next` is in the title
    and wears the ◉ on its row.

    Printed, so it does not scroll: past PLAN_ROWS it says how many it left out
    rather than pushing the panels above it off the screen.
    """
    show_repo = scope is None or scope.column
    # WHAT A NARROW PANE GIVES UP, AND IN WHICH ORDER. Every column but the title
    # is fixed, so the title cell pays for all of them, and below a dozen
    # characters a title says nothing at all. Two give way rather than squeezing
    # it to nothing: the holder's machine first — back to the 13 columns it had
    # before this panel showed one, so a narrow pane is no worse off than it was
    # yesterday — and then the rank, whose headline (`~N unchosen`) stays in the
    # title either way. Squeezing instead is not a smaller version of this: rich
    # takes the room out of the fixed columns from the left, and at 45 columns the
    # state glyph itself came out blank.
    who_width = 17 if width >= 60 else 13
    show_rank = _title_room(width, show_repo, True, who_width) >= 12
    room = max(6, _title_room(width, show_repo, show_rank, who_width))

    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(width=1, no_wrap=True)                     # state
    if show_repo:
        t.add_column(width=11, no_wrap=True)                # repo
    if show_rank:
        t.add_column(width=4, justify="right", no_wrap=True)  # rank, and who chose it
    t.add_column(width=6, justify="right", no_wrap=True)    # ref, if there is one
    t.add_column(ratio=1, no_wrap=True)                     # title
    t.add_column(width=who_width, justify="right", no_wrap=True)  # holder, or its wait

    # Narrowed BEFORE the print limit, not after: this panel does not scroll, and
    # the whole point of a scoped screen is that another repo's items cannot push
    # this one's past PLAN_ROWS and into the "…and N more" line.
    items, hidden = in_scope(plan_items(plan), scope)
    next_id = plan_next_id(plan)
    filler = [""] * (2 + int(show_repo) + int(show_rank))
    for item in items[:PLAN_ROWS]:
        glyph, colour = plan_state(item, next_id)
        who, who_colour = plan_who(item)
        rank, rank_colour = plan_rank(item)
        repo = short_repo(item.get("repo") or "fleet")
        cells = [Text(glyph, style=colour)]
        if show_repo:
            cells.append(Text(clip(repo, 11), style=repo_colour(repo)))
        if show_rank:
            cells.append(Text(rank, style=rank_colour))
        cells += [
            Text(plan_ref(item), style="bold grey70"),
            # A fleet-wide item names no repo, and with the column gone it would
            # read as one of this project's (qbdata.scope_mark).
            Text(scope_mark(scope, item.get("repo")) + clip(item.get("title"), room),
                 style="white" if colour != "grey50" else "grey50"),
            Text(clip(who, who_width), style=who_colour),
        ]
        t.add_row(*cells)
    if len(items) > PLAN_ROWS:
        t.add_row(*filler, Text(f"…and {len(items) - PLAN_ROWS} more", style="grey50"), "")
    if err:
        t.add_row(Text("!", style="red"), *filler[1:], Text(clip(err, width - 16), style="red"), "")
    if not items and not err:
        t.add_row(*filler, Text("nothing on the plan", style="grey50"), "")

    # The room the title actually has: the panel's own width, less its border, the
    # word PLANS, and whatever `elsewhere` is about to add.
    room = max(20, width - 12 - len(elsewhere(hidden)))
    head = "[bold]PLANS[/] [grey50]" + " · ".join(
        f"[{colour}]{text}[/]" if colour else text
        for text, colour in plan_head_bits(plan, items, hidden, room))
    head += elsewhere(hidden)
    return Panel(t, title=head + "[/]", title_align="left", border_style="grey35",
                 padding=(0, 1))


#: How each non-green check state is called and coloured in the OPEN PRs title.
#: `none` and `unknown` are in here and are the reason this exists: before #324 the
#: title counted reds and said nothing at all about the PRs whose checks were absent,
#: so a branch whose runs were gated contributed to no number on the screen.
CI_TALLY = (("red", "red", "red"), ("blocked", "blocked", "magenta"),
            ("pending", "running", "yellow"), ("none", "untested", "grey62"),
            ("unknown", "unread", "yellow"))


def ci_tally(prs: list[dict]) -> str:
    """" · 2 red · 1 blocked" — every state worth looking at, none of them silent."""
    counts = ci_counts(prs)
    return "".join(f" · [{colour}]{counts[state]} {word}[/]"
                   for state, word, colour in CI_TALLY if counts.get(state))


def panel_prs(prs: list[dict], err: str | None, width: int,
              scope: Scope | None = None) -> Panel:
    """The watched repos' open PRs.

    Not narrowed, and it cannot be: `gh` was only ever asked about the repos this
    dashboard watches, so there is no other repo's PR here to hide and widening
    the scope cannot produce one. Only the repo cell answers to the scope — and
    for the same reason as everywhere else, which is that one repo makes it the
    same word on every row.
    """
    show_repo = scope is None or scope.column
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(width=1, no_wrap=True)           # ci
    if show_repo:
        t.add_column(width=11, no_wrap=True)      # repo
    t.add_column(width=4, justify="right", no_wrap=True)   # number
    t.add_column(ratio=1, no_wrap=True)           # title
    t.add_column(width=5, justify="right", no_wrap=True)   # age

    filler = [""] * (3 if show_repo else 2)
    ordered = sorted(prs, key=lambda p: -p.get("number", 0))
    for pr in ordered[:PR_ROWS]:
        glyph, colour = ci_state(pr)
        title = pr.get("title") or ""
        repo = short_repo(pr.get("repo") or "")
        cells = [Text(glyph, style=colour)]
        if show_repo:
            cells.append(Text(clip(repo, 11), style=repo_colour(repo)))
        cells += [
            Text(f"#{pr.get('number')}", style="bold grey70"),
            Text(clip(title, max(12, width - (32 if show_repo else 20))),
                 style="grey50" if pr.get("isDraft") else "white"),
            Text(ago(pr.get("updatedAt")), style="grey50"),
        ]
        t.add_row(*cells)
    if err:
        t.add_row(Text("!", style="red"), *filler[1:], Text(clip(err, width - 12), style="red"), "")
    if not prs and not err:
        t.add_row(*filler, Text("no open PRs", style="grey50"), "")

    # `ci_tally` counts over `prs` — every open PR, not the rows drawn above.
    # That is the property the truncated row list would otherwise have broken:
    # a count taken off the visible rows would say "2 red" for a repo with five.
    head = f"[bold]OPEN PRs[/] [grey50]{len(prs)}" + ci_tally(prs)
    if len(ordered) > PR_ROWS:
        head += f" · +{len(ordered) - PR_ROWS} more"
    return Panel(t, title=head + "[/]", title_align="left", border_style="grey35",
                 padding=(0, 1))


def panel_review_queue(queue: dict, width: int, scope: Scope | None = None) -> Panel:
    """What review is waiting on, and how long it has waited — #273.

    The panel this dashboard was missing. OPEN PRs above says a PR exists and CI
    is green; nothing said whether anybody had ever reviewed it, and on
    2026-08-20 six of eight open PRs had never been panelled while the newest
    round on the board was two and a half days old. Neither number was readable
    anywhere.

    Rows are oldest-drainable first, which is a READING order for a panel that
    cannot scroll and not a work order — the board refuses to rank the queue, and
    so does this. An entry nothing may act on keeps its place in the list and
    goes grey with the reason in its verb column, because a queue that hid its
    blocked entries would report a depth of zero for a repo where everything is
    stuck (#244).
    """
    show_repo = scope is None or scope.column
    entries = queue.get("entries") or []
    err = queue.get("error")
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(width=1, no_wrap=True)                     # state
    if show_repo:
        t.add_column(width=11, no_wrap=True)                # repo
    t.add_column(width=4, justify="right", no_wrap=True)    # number
    t.add_column(width=11, no_wrap=True)                    # verb, or why not
    t.add_column(width=6, justify="right", no_wrap=True)    # age
    t.add_column(ratio=1, no_wrap=True)                     # title

    filler = [""] * (4 if show_repo else 3)
    for e in entries[:QUEUE_ROWS]:
        state = e.get("state") or ""
        colour = QUEUE_COLOUR.get(state, "grey50")
        drains = bool(e.get("drainable"))
        holds = e.get("holds") or []
        hold = holds[0].get("code") if holds else state
        verb = (QUEUE_VERB.get(e.get("next_action"), e.get("next_action") or "")
                if drains else QUEUE_HOLD.get(hold, hold))
        repo = short_repo(e.get("repo") or "")
        cells = [Text("\u25cf", style=colour)]
        if show_repo:
            cells.append(Text(clip(repo, 11), style=repo_colour(repo)))
        cells += [
            Text(f"#{e.get('pr')}", style="bold grey70"),
            Text(clip(verb, 11), style=colour if drains else "grey50"),
            # An age that is the longest the wait COULD have been wears a `~`,
            # because nothing records when a head moved or when a branch started
            # conflicting, and a number nobody can rely on should say so.
            Text(("~" if e.get("age_is_upper_bound") else "")
                 + waited(e.get("age_seconds")), style="grey50"),
            Text(clip(e.get("title") or "", max(12, width - (43 if show_repo else 31))),
                 style="white" if drains else "grey50"),
        ]
        t.add_row(*cells)
    if err:
        t.add_row(Text("!", style="red"), *filler[1:],
                  Text(clip(err, width - 12), style="red"), "")
    if not entries and not err:
        t.add_row(*filler, Text(queue.get("idle") or "nothing waiting on review",
                                style="grey50"), "")

    depth = queue.get("depth") or 0
    held = max(0, (queue.get("open") or 0) - depth)
    head = f"[bold]REVIEW QUEUE[/] [grey50]{depth} waiting"
    if held:
        head += f" \u00b7 {held} held"
    age, oldest_held = queue_oldest(queue)
    if age:
        head += f" \u00b7 {'held' if oldest_held else 'oldest'} {age}"
    if len(entries) > QUEUE_ROWS:
        head += f" \u00b7 +{len(entries) - QUEUE_ROWS} more"
    return Panel(t, title=head + "[/]", title_align="left", border_style="grey35",
                 padding=(0, 1))


def panel_issues(issues: list[dict], held: dict[int, dict], err: str | None,
                 width: int, scope: Scope | None = None) -> Panel:
    """Open issues, with the ones somebody already holds marked as such.

    The free ones are the point — an unheld issue is what the next seat takes —
    so they sort to the top, and a held one stays in the list but goes grey and
    gives its right-hand column over to the holder instead of its age.

    This panel is printed, not scrolled, and it is the last one on a pane that
    the others already share: past ISSUE_ROWS it stops listing and says how many
    it did not, rather than pushing the fleet off the top of the screen.
    """
    show_repo = scope is None or scope.column
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(width=1, no_wrap=True)                     # held marker
    if show_repo:
        t.add_column(width=11, no_wrap=True)                # repo
    t.add_column(width=4, justify="right", no_wrap=True)    # number
    t.add_column(ratio=1, no_wrap=True)                     # title
    t.add_column(width=9, justify="right", no_wrap=True)    # holder, or age

    ordered = sort_issues(issues, held)
    free = sum(1 for i in issues if issue_key(i) not in held)
    filler = [""] * (3 if show_repo else 2)
    for issue in ordered[:ISSUE_ROWS]:
        claim = held.get(issue_key(issue))
        who = (claim.get("holder") or "?").split("/", 1)[-1] if claim else ""
        repo = short_repo(issue.get("repo") or "")
        cells = [Text("·" if claim else "○", style="grey50" if claim else "green")]
        if show_repo:
            cells.append(Text(clip(repo, 11), style=repo_colour(repo)))
        cells += [
            Text(f"#{issue.get('number')}", style="bold grey70"),
            Text(clip(issue.get("title"), max(12, width - (36 if show_repo else 24))),
                 style="grey50" if claim else "white"),
            Text(clip(who, 9) if claim else ago(issue.get("updatedAt")),
                 style="yellow" if claim else "grey50"),
        ]
        t.add_row(*cells)
    if len(ordered) > ISSUE_ROWS:
        t.add_row(*filler, Text(f"…and {len(ordered) - ISSUE_ROWS} more", style="grey50"), "")
    if err:
        t.add_row(Text("!", style="red"), *filler[1:], Text(clip(err, width - 16), style="red"), "")
    if not issues and not err:
        t.add_row(*filler, Text("no open issues", style="grey50"), "")

    head = f"[bold]ISSUES[/] [grey50]{len(issues)}"
    if issues:
        head += f" · [green]{free} free[/]"
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


def queue_line(queue: dict) -> Text:
    """`REVIEW 3 waiting oldest 2d12h` — the queue's depth and age, in one cell.

    Empty only when the queue was never fetched. A depth of zero still renders,
    because "nothing is waiting" is the answer this whole issue is trying to make
    reachable, and a line that vanished when it was true would leave a reader
    unable to tell it from a dashboard that never asked.
    """
    out = Text()
    if not queue:
        return out
    label, depth, age, colour = queue_cell(queue)
    out.append(label, style="bold grey70")
    out.append(f" {depth}", style=f"bold {colour}")
    if age:
        out.append(f" {age}", style="grey50")
    return out


def header(cfg, data: dict, width: int, limits: list[dict] | None = None,
           stale: bool = False, scope: Scope | None = None,
           queue: dict | None = None) -> Panel:
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
    # The queue cell shares the caps line because it is the same kind of number:
    # the caps say what the seats may spend, this says what is waiting to be
    # spent on. Measured first so the bars are sized for the room that is left.
    review = queue_line(queue or {})
    room = width - 4 - (len(review.plain) + 3 if review.plain else 0)
    caps = limits_line(limits or [], room, stale)
    if caps.plain and review.plain:
        caps.append("   ")
    caps.append_text(review)
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
    # The whole envelope under "plan", not its `items`: what the board CONCLUDED
    # about the list — next, how much of the order anybody chose, the counts,
    # whether this is all of it — is the half the panel could not say before.
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


def fetch_gh(client=None) -> dict:
    """The `gh` calls, together: they share a clock and a failure mode.

    The review queue rides this clock rather than the board one even though it is
    a board call, because it is a join over the PR list fetched right here — on
    the fast clock it would answer about PRs ninety seconds newer than the ones
    the panel above it is drawing, and the two would disagree about a PR that
    moved in between.
    """
    prs, pr_err = fetch_prs()
    issues, issue_err = fetch_issues()
    queue = (fetch_review_queue(client, prs, pr_err=pr_err)
             if client is not None else {})
    return {"prs": prs, "pr_err": pr_err, "issues": issues, "issue_err": issue_err,
            "queue": queue}


def frame(cfg, data: dict, gh: dict, width: int, caps: dict | None = None,
          scope: Scope | None = None) -> Group:
    caps = caps or {}
    # From the UNFILTERED claims, always: an issue this screen can see, held by an
    # agent working out of another repo's checkout, is still held. Narrowing this
    # would show that issue as free and send the next seat straight into it.
    held = claims_by_issue(data.get("claims", []))
    queue = gh.get("queue") or {}
    parts = [header(cfg, data, width, caps.get("limits"), bool(caps.get("error")), scope,
                    queue),
             panel_agents(data, width, scope),
             panel_claims(data, width, scope),
             panel_plan(data.get("plan") or {}, data.get("plan_err"), width, scope),
             panel_prs(gh["prs"], gh["pr_err"], width, scope),
             panel_review_queue(queue, width, scope),
             panel_issues(gh["issues"], held, gh["issue_err"], width, scope)]
    if data.get("error"):
        parts.append(Panel(Text(clip(data["error"], width * 2), style="red"),
                           title="[red]ERROR[/]", title_align="left", border_style="red"))
    return Group(*parts)


# ---- main --------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """`argv` so the flags can be driven by a test.

    The clickable renderer's `main` takes one for the same reason: the two
    decisions in here — which view `--scope` names, and pinning the repos BEFORE
    the scope is resolved off them — are wiring that fails silently, and a
    dashboard is the one program whose output nobody diffs.
    """
    ap = argparse.ArgumentParser(prog="qb-dash", description="fleet state, for a tall pane")
    ap.add_argument("--once", action="store_true", help="render one frame and exit")
    ap.add_argument("--width", type=int, default=None, help="force a width")
    ap.add_argument("--interval", type=float, default=BOARD_EVERY, help="board refresh seconds")
    ap.add_argument("--scope", choices=("repo", "all"), default=None,
                    help="repo (default): only this screen's repos, and no repo column; "
                         "all: every repo the board knows, in FLEET/CLAIMED/PLANS — "
                         "PRs and issues stay the watched repos' either way. "
                         "Overrides QB_DASH_SCOPE")
    ap.add_argument("--repo", action="append", metavar="PATH|OWNER/NAME",
                    help="the project this screen is for — a checkout or an owner/name "
                         "slug, repeatable. Overrides QB_DASH_REPOS, QB_DASH_REPO "
                         "and the cwd")
    args = ap.parse_args(argv)

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
    gh = fetch_gh(client)
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
                gh = fetch_gh(client)
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
