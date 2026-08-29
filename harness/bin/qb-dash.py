#!/usr/bin/env python3
"""qb-dash — the fleet at a glance, for a tall pane beside the seats.

The tape (`qb-board --follow`) answers "what just happened". This answers the
other question: who is alive right now, what have they claimed, and what is
waiting to land. State, not events.

  qb-dash              live, redrawing
  qb-dash --once       one frame and exit (what the tests and a pipe want)
  qb-dash --width 72   force a width instead of taking the terminal's
  qb-dash --scope all  every repo the BOARD knows, not just this screen's
  qb-dash --waiting    only what a person owes an answer about
  qb-dash --backlog    also the work nothing is waiting on
  qb-dash --repo ~/src/nix-fleet    point it at a project other than the cwd's

TWO TABLES, because there are two questions (#589):

  AGENTS  who is here, and how are they doing — live agents, what each holds,
          and the claims no live agent answers for.  Was FLEET + CLAIMED, and
          in the clickable renderer SEATS as well.
  WORK    what is in flight, and where has it got to — the board's plan in the
          board's order, with the review queue folded into the rows it is about
          and anything else that is open appended below.  Was PLANS + OPEN PRs +
          REVIEW QUEUE + ISSUES.

They were eight panels, and the split cost more than the borders. On the frame
this was measured against, `#578` was on the pane four times — a claim, a plan
rank, and twice more as its PR — and OPEN PRs and REVIEW QUEUE printed the same
three PRs in ten lines. That pair was never going to be two panels honestly: the
queue is DERIVED from the open-PR list (`qbdata.fetch_review_queue`), so it is a
subset by construction, and all the PR panel added was the CI glyph — which is
now the WORK table's state cell for a PR row.

WHAT IS NOT MERGED, and why: a plan item names its ISSUE and nothing anywhere
records which PR implements it (#396). So an issue and the PR that closes it are
two rows, `#578` goes from four rows to two rather than to one, and the missing
edge is a fact about the board rather than a shortcut taken here.

WHAT A PERSON OWES AN ANSWER TO IS ON THE TABLE (#328): a blocked row wears `⚑`
magenta — the glyph a gated PR already wore, because "nothing moves until somebody
acts" is the same sentence about a different subject — and `--waiting` shows only
those. Questions with no work to ride, which is what `qb-doctor` raises against a
repo, get rows of their own rather than being counted by the header and drawn
nowhere.

The two lists nothing is waiting on — open PRs review has finished with, and open
issues nobody has planned or taken — are behind `--backlog`. Their counts stay on
the header line either way: a toggled list that left no number behind would be a
way of forgetting the work exists.

By default it shows ONE project's rows — the repos of the checkout it was started
in — and drops the repo column, because a screen built for one project spends
eleven columns of a narrow pane restating its name (#261). `--scope all` widens
what comes off the BOARD (AGENTS, and the plan half of WORK); the `gh` rows
cannot widen, because `gh` is only ever asked about the repos this dashboard
watches. The clickable renderer toggles with `s`, which this one has no keyboard
for.

Board data comes from the same client the MCP server uses; PRs and issues come
from `gh`, on a slower clock because that is a network call per refresh and
neither moves every three seconds. The line across the top is Claude Code's own
usage caps — the ceiling every seat below it is working towards, on a slower
clock again — and the line under it is what is waiting to be spent on.
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
    LIMITS_EVERY, Scope, agent_rows, agent_tally, ago, board_client,
    clip, dial_life, dial_value, dial_where, dials_url, elsewhere,
    fetch_board, fetch_dials, fetch_issues, fetch_limits, fetch_plan,
    blocker_tally, fetch_blockers,
    fetch_prs, fetch_review_queue, claims_by_issue, in_scope, issue_key,
    limit_cells, plan_head_bits, plan_items, pr_tally,
    queue_cell, repo_arg, repo_colour,
    resolve_scope, scope_mark, set_repos, short_repo, tempo_cell,
    work_fold, work_kind, work_rows, work_tally,
)

BOARD_EVERY = 4.0       # seconds; presence changes on this order
GH_EVERY = 90.0         # gh is a network round trip, and PRs/issues are not live data
# A printed panel cannot scroll, so past this it stops listing and says how many
# it left out. ONE CAP WHERE THERE WERE FOUR (ISSUE_ROWS, PLAN_ROWS, QUEUE_ROWS
# and PR_ROWS), and it is bigger than any of them because it is now the only one:
# the plan, the review queue and whatever else is open share these rows instead of
# each holding a reservation against a pane none of them could see.
WORK_ROWS = 16
# The same again for the dials. Two lines each — the value and the argument for it
# — so this is a shorter cap than the panels above: a fleet with more than five
# dials in force has a config question, not a dashboard question.
DIAL_ROWS = 5

# Repo → colour, so the same project is the same colour everywhere on the panel.
# ---- panels ------------------------------------------------------------------


def panel_agents(data: dict, width: int, scope: Scope | None = None,
                 items: list[dict] | None = None) -> Panel:
    """Who is here and how they are doing — FLEET and CLAIMED as one table (#589).

    Two panels drew one subject. FLEET said who was live and CLAIMED said what
    they held, keyed on the same holder and split apart by nothing but a border,
    so a reader asking "what is jasper-moss doing" joined them by eye — and on
    the frame this change was measured against, jasper-moss was on the pane five
    times across three panels.

    The rows this could not draw before are the ones worth drawing: a claim whose
    holder no live agent answers for is work somebody holds that nobody is doing,
    and in CLAIMED it looked exactly like a live one (:data:`qbdata.CLAIM_ONLY_STATE`).

    No seats here. `tmux_seats()` is the clickable renderer's, because a pane you
    cannot click is a pane you cannot do anything about — this renderer has no
    keyboard at all, which is the whole of what `--once` and the printed panels
    are.
    """
    show_repo = scope is None or scope.column
    rows, hidden = agent_rows(data, scope, items)

    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(width=13, no_wrap=True)          # who
    t.add_column(width=7, no_wrap=True)           # state
    # Next to `state`, and narrow: `state` says whether the pane is moving, this
    # says where it has got to, and they are read together (#262). Six, because a
    # stage IS six characters at most — the shape qb-stage and the board both
    # enforce — so nothing here is ever truncated to make it fit.
    t.add_column(width=6, no_wrap=True)           # stage
    if show_repo:
        t.add_column(width=11, no_wrap=True)      # repo
    t.add_column(ratio=1, no_wrap=True)           # what
    t.add_column(width=5, justify="right", no_wrap=True)   # ttl

    body = max(18, width - (53 if show_repo else 41))
    for row in rows:
        what, what_style = row["what"]
        # `＋1` rather than a second line: a row is one agent, and an agent
        # holding two things is one agent holding two things. The rest is a
        # click away in the other renderer and on the board either way.
        if row.get("extra"):
            what = f"{what}  ＋{row['extra']}"
        cells = [Text(clip(row["who"], 13), style="bold"),
                 Text(*row["state"]),
                 Text(*row["stage"])]
        if show_repo:
            repo = row["repo"] or "—"
            cells.append(Text(clip(repo, 11), style=repo_colour(repo)))
        cells += [
            # The mark rides on the cell the dropped column widened: with no repo
            # cell, an agent working outside any checkout otherwise reads as one
            # working here (qbdata.scope_mark).
            Text(scope_mark(scope, row["repo"]) + clip(what, body), style=what_style),
            Text(row["ttl"],
                 style="red" if row.get("expiring") else "grey50"),
        ]
        t.add_row(*cells)
    if not rows:
        t.add_row(Text("nobody home", style="grey50"), *[""] * (5 if show_repo else 4))

    subs = len(data.get("subagents") or [])
    head = f"[bold]AGENTS[/] [grey50]{agent_tally(rows)}"
    if subs:
        head += f" · {subs} sub"
    head += elsewhere(hidden)
    return Panel(t, title=head + "[/]", title_align="left", border_style="grey35",
                 padding=(0, 1))


def _title_room(width: int, show_repo: bool, show_rank: bool, who_width: int) -> int:
    """How many columns the title cell has left once the fixed ones have taken theirs.

    The panel's border and padding cost 4, and `Table.grid(padding=(0, 1))` puts a
    space after every column but the last.
    """
    fixed = [1, 4] + ([11] if show_repo else []) + ([4] if show_rank else []) + [6, who_width]
    return width - 4 - sum(fixed) - len(fixed)


def panel_work(plan: dict, err: str | None, gh: dict, held: dict, width: int,
               scope: Scope | None = None, backlog: bool = False,
               claims_known: bool = True, blockers: dict | None = None,
               waiting_only: bool = False) -> Panel:
    """What is in flight, in the board's order — the plan, the review queue, and
    whatever else is open (#589).

    Four panels answered "what work is there" from four sources, and the same
    unit of work appeared on up to four of them at once. Worse, two of them were
    the same rows: the review queue is derived FROM the open-PR list, so OPEN PRs
    and REVIEW QUEUE printed an identical row set in ten lines of pane, and all
    the PR panel added was the CI glyph — which is now this table's state column,
    per #272.

    **The order is the board's and is not re-derived here.** Work the plan does
    not carry is appended below it, unranked, because the board refuses to rank
    the queue (#232) and inventing a position is exactly the second answer this
    file has spent three issues removing.

    Printed, so it does not scroll: past WORK_ROWS it says how many it left out
    rather than pushing the panels above it off the screen.
    """
    show_repo = scope is None or scope.column
    # THE `gh` HALF ARRIVES AS ONE DICT, which is what `fetch_gh` returns and what
    # this panel needs all of: four lists and three ways for them to have failed.
    # Threaded one at a time the signature ran to ten arguments and two of them —
    # the errors — were simply forgotten, which is Codex's finding on this change.
    prs, pr_err = gh.get("prs") or [], gh.get("pr_err")
    issues, issue_err = gh.get("issues") or [], gh.get("issue_err")
    queue = gh.get("queue") or {}
    # WHAT A NARROW PANE GIVES UP, AND IN WHICH ORDER. Every column but the title
    # is fixed, so the title cell pays for all of them, and below a dozen
    # characters a title says nothing at all. Two give way rather than squeezing
    # it to nothing: the holder's machine first — back to the 13 columns it had
    # before this panel showed one — and then the rank, whose headline
    # (`~N unchosen`) stays in the title either way. Squeezing instead is not a
    # smaller version of this: rich takes the room out of the fixed columns from
    # the left, and at 45 columns the state glyph itself came out blank.
    who_width = 17 if width >= 60 else 13
    show_rank = _title_room(width, show_repo, True, who_width) >= 12
    room = max(6, _title_room(width, show_repo, show_rank, who_width))

    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(width=1, no_wrap=True)                     # state
    # The kind, explicit rather than a sigil on the ref (#272): `PR#4` and `#4`
    # differ by two characters at the front of a right-aligned cell, which is not
    # a distinction a reader should have to make out.
    t.add_column(width=4, no_wrap=True)                     # kind
    if show_repo:
        t.add_column(width=11, no_wrap=True)                # repo
    if show_rank:
        t.add_column(width=4, justify="right", no_wrap=True)  # rank, and who chose it
    t.add_column(width=6, justify="right", no_wrap=True)    # ref, if there is one
    t.add_column(ratio=1, no_wrap=True)                     # title
    t.add_column(width=who_width, justify="right", no_wrap=True)  # holder, or its wait

    # Narrowed BEFORE the print limit, not after: this panel does not scroll, and
    # the whole point of a scoped screen is that another repo's items cannot push
    # this one's past WORK_ROWS and into the "…and N more" line.
    rows, hidden = work_rows(plan, prs, queue, issues, held, scope, backlog,
                             blockers=(blockers or {}).get("blockers"),
                             waiting_only=waiting_only)
    # The plan's own counts are the plan's own rows, not the merged table's:
    # `plan_head_bits` reports what the BOARD said about the list, and handing
    # it a row set with three PRs in it would have it count them as plan items.
    items, _ = in_scope(plan_items(plan), scope)
    filler = [""] * (3 + int(show_repo) + int(show_rank))
    folded = work_fold(rows, WORK_ROWS)

    def draw(row: dict) -> None:
        glyph, colour = row["glyph"]
        why, why_colour = row["why"]
        rank, rank_colour = row["rank"]
        cells = [Text(glyph, style=colour), Text(work_kind(row), style="grey50")]
        if show_repo:
            repo = short_repo(row["repo"] or "fleet")
            cells.append(Text(clip(repo, 11), style=repo_colour(repo)))
        if show_rank:
            cells.append(Text(rank, style=rank_colour))
        cells += [
            Text(row["ref"], style="bold grey70"),
            # A fleet-wide item names no repo, and with the column gone it would
            # read as one of this project's (qbdata.scope_mark).
            Text(scope_mark(scope, row["repo"]) + clip(row["title"], room),
                 style="grey50" if row["dim"] else "white"),
            Text(clip(why, who_width), style=why_colour),
        ]
        t.add_row(*cells)

    # Each section says what it left out WHERE IT LEFT IT, rather than one count
    # at the bottom: the plan is what usually gets cut, and a count under the
    # backlog would read as a statement about the backlog.
    for (section, dropped), more in zip(folded, ("more waiting on review",
                                                 "more on the plan",
                                                 "more not on the plan")):
        for row in section:
            draw(row)
        if dropped:
            t.add_row(*filler, Text(f"…and {dropped} {more}", style="grey50"), "")

    # THE STATES THAT ARE NOT ROWS, and every source that can fail gets one. The
    # message goes in the TITLE cell — the widest one — rather than in the title
    # of the panel, which is bounded by the pane: a table whose job is saying why
    # something is missing must not truncate the message that says why it cannot
    # tell you.
    # EVERY SOURCE THAT CAN FAIL, named separately. `panel_prs` and `panel_issues`
    # each drew their own error row before the merge, and a table that reported the
    # board and the queue but not `gh` would have lost the two that fail most.
    troubles = [(name, message) for name, message in
                (("board", err), ("queue", (queue or {}).get("error")),
                 ("blockers", (blockers or {}).get("error")),
                 ("prs", pr_err), ("issues", issue_err)) if message]
    for name, message in troubles:
        t.add_row(Text("!", style="red"), *filler[1:],
                  Text(clip(f"{name}: {message}", width - 16), style="red"), "")
    # "Nothing is in flight" and "nothing could be FETCHED" are different answers,
    # and the board supplies its own wording for a drained queue. Only when nothing
    # failed: a table that said both would be answering its own error message with
    # a claim it has no grounds for (#244).
    if not rows and not troubles:
        t.add_row(*filler, Text("nothing is waiting on a person" if waiting_only
                                else ((queue or {}).get("idle") or "nothing in flight"),
                                style="grey50"), "")

    # The room the title actually has: the panel's own width, less its border, the
    # word WORK, whatever `elsewhere` is about to add — and the tally bits, which
    # are measured FIRST because `plan_head_bits` is the only part of this title
    # that knows how to give something up. Sizing the elastic half for a line the
    # fixed half has already taken its room out of is how `+24 free h─` came to be
    # clipped by the panel border rather than by anything that could choose.
    # `claims_known` is what stops the hidden-backlog count being taken off claims
    # the board never sent: `fetch_board` reports an outage as `{"claims": []}`,
    # which counts every open issue as free — and "27 free hidden" over an
    # unreachable board is how a seat is sent into work somebody already holds.
    tally = work_tally(rows, prs, issues, held, backlog, claims_known, waiting_only)
    spent = sum(len(bit) + 3 for bit in tally)
    # `head_room` and not `room`: the title's room and the title CELL's room are
    # two different measurements, and `draw` above closes over the second one.
    # Reusing the name worked only because every `draw` call happens before this
    # line, which is not a property anybody should have to notice.
    head_room = max(20, width - 11 - spent - len(elsewhere(hidden)))
    # The plan's counts are about the PLAN, so a filtered view does not claim them:
    # `39 open · next #1620` over two rows a person owes an answer about is a title
    # describing a list the reader has just asked not to see.
    head = ("[bold]WAITING[/] [grey50]" if waiting_only else "[bold]WORK[/] [grey50]") + " · ".join(
        ([] if waiting_only else
         [f"[{colour}]{text}[/]" if colour else text
          for text, colour in plan_head_bits(plan, items, hidden, head_room)]) + tally)
    head += elsewhere(hidden)
    return Panel(t, title=head + "[/]", title_align="left", border_style="grey35",
                 padding=(0, 1))


def dial_row(row: dict, name_room: int, show_repo: bool = True) -> Text:
    """One dial as a single padded line: `tempo   eager   quarterback   1h28m`.

    Composed by hand rather than by `Table.grid`, which is the opposite of every
    other panel in this file and is the one place it is right. Rich has no
    colspan, so a grid whose columns fit four cells cannot also carry a line that
    runs the panel's whole width — and the two lines that MUST run the whole width
    are the two this panel exists for: the argument for a value being in force,
    and the URL a person has to type to change it. Clipped to 40 characters, a
    reason reads as a fragment and a link cannot be typed at all.

    So the table is one column wide and the alignment is done here. `name_room` is
    passed in rather than measured because every row has to agree about it.
    """
    where, where_style = dial_where(row, show_repo)
    life, life_style = dial_life(row)
    out = Text()
    out.append(f"{clip(row.get('dial'), name_room):<{name_room}}", style="bold white")
    out.append(f" {dial_value(row, 12):<12}", style="cyan")
    room = 11 if show_repo else 5
    out.append(f" {where:<{room}}", style=where_style)
    out.append(f" {life:>6}", style=life_style)
    return out


def panel_dials(dials: dict, width: int, cfg=None, scope: Scope | None = None) -> Panel:
    """Which dials are in force, which layer answered, why, and for how long — #477.

    The panel this dashboard was missing, and the one nothing anywhere had: a dial
    is set from a browser endpoint and read back by one function in
    `panel_seats.py`, so the values governing every round on the fleet were
    invisible on every screen a person or an agent actually looks at.

    **Above the fleet rather than below the issues**, and for the caps line's own
    reason: this is the configuration every panel underneath it is running under.
    It is short — a fleet with nothing set is two rows — and it is the one thing on
    the screen a reader wants BEFORE the rows rather than after them.

    **It always draws, even with nothing in force**, because the empty state is
    where the last row earns its place: the URL is most wanted by the person who
    has just discovered the tempo is not what they want, and a hint that appeared
    only once a dial existed would be missing exactly then.
    """
    show_repo = scope is None or scope.column
    rows = dials.get("dials") or []
    err = dials.get("error")
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(ratio=1, no_wrap=True)
    # The panel's border and padding cost 4, `padding=(0, 1)` costs nothing on a
    # single column, and the fixed cells take the rest.
    body = max(20, width - 4)
    name_room = max(10, body - (12 + (11 if show_repo else 5) + 6 + 3))

    for row in rows[:DIAL_ROWS]:
        t.add_row(dial_row(row, name_room, show_repo))
        # The REASON, on a line of its own under the value it argues for. A dial
        # whose argument nobody can read is one nobody can decide to remove —
        # which is why the board refuses to store a dial without one. Who set it
        # and when ride with it: "human/rich · 4h" is what turns a value somebody
        # has to decide about into one they can go and ask about.
        by = " · ".join(x for x in (row.get("set_by"), ago(row.get("set_at"))) if x)
        # Indented OUTSIDE the clip: `clip` collapses runs of whitespace, so an
        # indent built into its argument is one it eats — and the indent is what
        # makes this line read as belonging to the row above it rather than as
        # another dial.
        t.add_row(Text("  " + clip((row.get("reason") or "")
                                   + (f"  ({by})" if by else ""), body - 2),
                       style="grey50"))
    if len(rows) > DIAL_ROWS:
        t.add_row(Text(f"…and {len(rows) - DIAL_ROWS} more", style="grey50"))
    if err:
        t.add_row(Text(clip(f"! {err}", body), style="red"))
    if not rows and not err:
        # THREE empty states, and none of them is the others. A board that has not
        # been asked yet does not know that nothing is set; a board that answered
        # with nothing does. Collapsing them is the same mistake as a queue that
        # reports depth zero for a repo it never queried (#244).
        #
        # And the answered one is not "no dials": every dial HAS a value, and this
        # is the state where the repo's own default is the one in force. A panel
        # that said "none" would read as "nothing is configured", which is never
        # true of a harness that ships defaults for everything.
        t.add_row(Text("every dial at its repo default" if dials.get("asked")
                       else "asking the board…", style="grey50"))
    # WHERE TO TURN ONE, and it names both surfaces that can. THIS renderer cannot:
    # it has no keyboard at all — that is the whole of what `--once` and the printed
    # panels are — so the verb is the clickable renderer's `✎` or the board's page,
    # and saying only "a browser" would now be half true. #443 is the record of what
    # happens when a surface reads a thing, says the change is yours to make, and
    # does not say where: "i don't know how to re-order".
    t.add_row(Text(clip(f"set it: ✎ in qb-dash-tui, or {dials_url(cfg)}", body),
                   style="grey50"))

    # A COUNT IS A CLAIM. "0 in force" over an unreachable board says the fleet is
    # running on its defaults, which is the one thing an unanswered read cannot
    # establish — the dials may all be set and the reader has simply not been told.
    if err:
        # Left open, like the two branches below it: the `[/]` that closes this is
        # appended with the title, and a second one here closes nothing and takes
        # the whole panel down with a MarkupError.
        head = "[bold]DIALS[/] [red]unreadable"
    elif not dials.get("asked"):
        head = "[bold]DIALS[/] [grey50]asking"
    else:
        head = f"[bold]DIALS[/] [grey50]{len(rows)} in force"
    shadowed = len(dials.get("shadowed") or [])
    if shadowed:
        # A fleet dial this screen's repos all override. Counted rather than drawn,
        # because it is NOT in force here — but silence would leave a reader who
        # set it fleet-wide unable to see that anything had happened to it.
        head += f" · {shadowed} overridden"
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


def waiting_line(blockers: dict | None, scope: Scope | None = None) -> Text:
    """`WAITING 3` — how many questions are sitting unanswered, on the top line.

    FIRST OF THE TALLIES, ahead of the review depth and the caps. Every other
    number on that line is about what the fleet is doing to itself; this one is
    the only thing on the pane that is somebody's to act on, and #274's whole
    argument is that it needs one place a person always sees it. Ten correct
    escalations went unread for two days on this repo (#569) — not because nobody
    looked, but because looking meant opening something.
    """
    out = Text()
    cell = blocker_tally(blockers, scope)
    if cell is None:
        return out
    text, colour = cell
    label, _, count = text.partition(" ")
    out.append(label, style="bold grey70")
    out.append(f" {count}", style=f"bold {colour}")
    return out


def prs_line(prs: list[dict] | None, err: str | None) -> Text:
    """`PRs 3 · 1 red` — the open-PR count and every check state worth looking at.

    THE HALF OF THE OPEN PRs PANEL WORTH KEEPING (#589). Its rows were the review
    queue's rows — the queue is derived from this very list, so it was a subset by
    construction — but its TALLY is not derivable from the queue at all: a PR can
    be green and unreviewed, or red and already signed off. Dropping the panel
    without moving this number would have lost "is CI red right now", which is the
    one thing on that panel nothing else said.

    Counted over every open PR rather than over the rows drawn below, which is the
    property the row cap would otherwise break: a count taken off the visible rows
    says "2 red" for a repo with five (#324).
    """
    out = Text()
    if prs is None and not err:
        return out
    out.append("PRs", style="bold grey70")
    if err:
        out.append(" ?", style="bold red")
        return out
    out.append(f" {len(prs or [])}", style="bold grey70")
    for text, colour in pr_tally(prs):
        out.append(" · ", style="grey50")
        out.append(text, style=colour)
    return out


def issues_line(issues: list[dict] | None, held: dict | None, err: str | None,
                claims_known: bool = True) -> Text:
    """`ISSUES 30 · 25 free` — the backlog, as the number rather than the list.

    Twelve rows of catalogue was the biggest single consumer of the old frame, and
    the plan's `next` answers "what should I pick up" better than a
    free-issues-first sort does. What the panel was actually READ for is the two
    numbers, so they ride the header and the rows go behind `--backlog`.

    `free` is not stated while the board is unreachable: it is counted off claims
    that are stale or were never fetched, and a "25 free" computed from no claims
    at all is how a seat gets sent into work somebody already holds.
    """
    out = Text()
    if issues is None and not err:
        return out
    out.append("ISSUES", style="bold grey70")
    if err:
        out.append(" ?", style="bold red")
        return out
    out.append(f" {len(issues or [])}", style="bold grey70")
    if held is not None and claims_known and issues:
        free = sum(1 for i in issues if issue_key(i) not in held)
        out.append(" · ", style="grey50")
        out.append(f"{free} free", style="green")
    return out


def tempo_line(dials: dict | None) -> Text:
    """`TEMPO eager 40m` — how hard the fleet is meant to be working, in one cell.

    On the caps line rather than in the panel below because that is the question
    it answers: the caps say what the seats MAY spend and this says whether they
    are supposed to be spending it at all. A reader glancing at one is asking
    about the other.

    Empty only before the first fetch has answered. After that every state draws,
    including `unset` — a cell that vanished when no dial was in force could not
    be told apart from a dashboard that never asked, and "nothing is throttling
    the fleet" is precisely the answer somebody is looking for at 94% of a window.
    """
    out = Text()
    cell = tempo_cell(dials or {})
    if cell is None:
        return out
    label, value, life, colour = cell
    out.append(label, style="bold grey70")
    out.append(f" {value}", style=f"bold {colour}")
    if life:
        out.append(f" {life}", style="grey50")
    return out


def header(cfg, data: dict, width: int, limits: list[dict] | None = None,
           stale: bool = False, scope: Scope | None = None,
           queue: dict | None = None, dials: dict | None = None,
           gh: dict | None = None, held: dict | None = None) -> Panel:
    """The caps, the tallies, and where this pane is pointed.

    FOUR TALLY CELLS ON A LINE OF THEIR OWN, under the bars rather than beside
    them. Two of them are new — the open-PR count with its check states, and the
    issue backlog — because those are what the two panels that went behind
    `--backlog` were actually read for, and a toggled list has to leave its
    number behind or the toggle is a way of forgetting the work exists (#589).

    Beside the caps was where the review depth lived and the argument for it still
    holds — the caps say what the seats may spend, these say what is waiting to be
    spent on — but four cells and a pair of bars do not fit in 78 columns, and a
    line whose contents move to another line depending on the width is a line
    nobody learns to read. Directly underneath keeps the glance and drops the
    guesswork.
    """
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

    gh = gh or {}
    tallies = Text()
    for cell in (waiting_line(data.get("blockers"), scope),
                 queue_line(queue or {}),
                 prs_line(gh.get("prs"), gh.get("pr_err")),
                 issues_line(gh.get("issues"), held, gh.get("issue_err"),
                             claims_known=not data.get("error")),
                 # The throttle rides with the budget it protects (#477): the caps
                 # say what the seats MAY spend and this says whether they are
                 # supposed to be spending it at all.
                 tempo_line(dials)):
        if not cell.plain:
            continue
        if tallies.plain:
            tallies.append("   ")
        tallies.append_text(cell)

    caps = limits_line(limits or [], width - 4, stale)
    for row in (tallies, caps):
        if row.plain:
            parts.insert(0, Align.left(row))
    return Panel(Group(*parts), border_style="grey35", padding=(0, 1))


def fetch_state(client) -> dict:
    """The board's own answers in one dict: presence, claims, the plan, the dials.

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
    # On the board clock and not the `gh` one, like the plan and for a sharper
    # version of the plan's reason: a dial is a human decision that takes effect
    # the moment it is made, and a throttle a screen would not show for ninety
    # seconds is a throttle nobody watching that screen can trust.
    data["dials"] = fetch_dials(client)
    # On the board clock too, and for a sharper version of the dial's reason: a
    # blocker is raised and answered by people acting now, and a surface that
    # lagged it by ninety seconds would show work as stuck that somebody has just
    # unstuck — on the one surface #274 asks a person to trust.
    data["blockers"] = fetch_blockers(client)
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
          scope: Scope | None = None, backlog: bool = False,
          waiting: bool = False) -> Group:
    """Two tables, the dials above them, and the numbers above those.

    It was eight panels answering two questions, and 61 rows into the 38-row pane
    #269 measured — after that issue's per-panel caps, which is what makes 61 the
    floor rather than the excess (#589).
    """
    caps = caps or {}
    # From the UNFILTERED claims, always: an issue this screen can see, held by an
    # agent working out of another repo's checkout, is still held. Narrowing this
    # would show that issue as free and send the next seat straight into it.
    held = claims_by_issue(data.get("claims", []))
    queue = gh.get("queue") or {}
    dials = data.get("dials") or {}
    # The plan's items, for the two joins that need them: a claim key like
    # `item:<uuid>` says nothing without the item behind it, and an issue number
    # in a `what` cell is a number until the plan supplies the words.
    items = plan_items(data.get("plan"))
    parts = [header(cfg, data, width, caps.get("limits"), bool(caps.get("error")), scope,
                    queue, dials, gh, held),
             panel_dials(dials, width, cfg, scope),
             panel_agents(data, width, scope, items),
             panel_work(data.get("plan") or {}, data.get("plan_err"), gh, held,
                        width, scope, backlog,
                        claims_known=not data.get("error"),
                        blockers=data.get("blockers"), waiting_only=waiting)]
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
                         "all: every repo the board knows, in AGENTS and in the plan "
                         "half of WORK — PRs and issues stay the watched repos' "
                         "either way. Overrides QB_DASH_SCOPE")
    ap.add_argument("--repo", action="append", metavar="PATH|OWNER/NAME",
                    help="the project this screen is for — a checkout or an owner/name "
                         "slug, repeatable. Overrides QB_DASH_REPOS, QB_DASH_REPO "
                         "and the cwd")
    ap.add_argument("--waiting", action="store_true",
                    help="only the rows a person owes an answer about — the "
                         "terminal half of the one door (#274). The count is on "
                         "the header line either way")
    ap.add_argument("--backlog", action="store_true",
                    help="also list the work nothing is waiting on: open PRs review "
                         "has finished with, and open issues nobody has planned or "
                         "taken. Their counts are on the header line either way")
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
        console.print(frame(cfg, data, gh, width, caps, scope, args.backlog,
                            args.waiting))
        return 0

    last_gh = last_caps = time.monotonic()
    with Live(frame(cfg, data, gh, width, caps, scope, args.backlog, args.waiting),
              console=console,
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
            live.update(frame(cfg, data, gh, width, caps, scope, args.backlog,
                              args.waiting))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
