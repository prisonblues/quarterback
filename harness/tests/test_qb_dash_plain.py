"""The printed renderer's scoped panels, and both renderers' `main()`.

Two gaps this closes, both found by the panel on PR #265.

`test_qb_dash.py` — despite the name — only ever loads `qb-dash-tui.py`, so the
plain renderer's five `add_row` call sites had no test at all. They are the
fiddliest thing in either file now: each builds a variable-length `cells` list
whose length has to track `show_repo`, and **Rich silently appends a column** when
`add_row` is given more renderables than the table has. An off-by-one there is a
misaligned panel, not an exception — nothing in CI would have said a word.

And neither `main()` was tested, though `main(argv)` exists in both files
precisely to be test-driven: the `--scope` → narrow/wide mapping, the
load-bearing order of `set_repos()` before `resolve_scope()`, and the exit-2 path
for a `--repo` that names nothing. That order is not a style point — `resolve_repos`
caches, so reversing it aims the scope filter at the cwd's repo while the `gh`
calls watch the pinned one, which is the shape of the P1 this same review found.

The printed half needs `rich` and nothing else — no textual, no board, no `gh`.
That is deliberate: the plain renderer is the one that must keep working on an
interpreter carrying only rich (`usable()` in `harness/bin/qb-dash` will pick such
a one), so its test must not drag textual in to prove it. The handful of tests that
drive the CLICKABLE renderer's `main` do import it, and are skipped without it
rather than erroring — `needs_textual` below.

Run: pytest harness/tests/test_qb_dash_plain.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))

pytest.importorskip("rich", reason="the printed renderer needs rich")

# AFTER the importorskip, and that is the whole of why it is down here rather than
# with the others. This module is meant to SKIP on an interpreter without rich —
# CI runs the harness once with no dashboard extras precisely to prove the rest of
# it needs none — and a module-scope `from rich...` above the guard turns that skip
# into a collection ERROR, which is a red job about a dependency nobody claimed to
# have. It cost exactly that on the first push of #589.
from rich.console import Console                            # noqa: E402

#: The clickable renderer imports textual at module scope, so `_tui()` cannot even
#: be loaded without it. A bare ImportError here would be an ERROR rather than a
#: skip on exactly the rich-only interpreter this file's own docstring says is
#: legitimate — the sibling suite guards the same way, via `_why_no_tui`.
needs_textual = pytest.mark.skipif(
    importlib.util.find_spec("textual") is None,
    reason="qb-dash-tui.py imports textual at module scope")

import qbdata as qd                                       # noqa: E402


def _load(name: str, filename: str):
    """Import a hyphenated script as a module — the same trick test_qb_dash uses."""
    loader = importlib.machinery.SourceFileLoader(name, str(BIN / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dash():
    return _load("qb_dash_plain", "qb-dash.py")


@pytest.fixture
def watched():
    """Restore the process-wide repo cache, whatever a test does to it."""
    before = qd._repos
    yield
    qd._repos = before


NARROW = qd.Scope([qd.REPO])
WIDE = NARROW.toggled()

BOARD = {
    "agents": [
        {"holder": "daedalus/seat-quarterback-1", "repo": "quarterback",
         "title": "here", "branch": "main"},
        {"holder": "zeus/amber-otter", "repo": "prisonblues/nix-fleet",
         "title": "elsewhere", "branch": "main"},
        {"holder": "zeus/hazel-dune", "repo": None, "title": "nowhere", "branch": None},
    ],
    "claims": [
        {"holder": "daedalus/one", "kind": "issue", "key": f"{qd.REPO}#261"},
        {"holder": "zeus/two", "kind": "issue", "key": "prisonblues/nix-fleet#3"},
    ],
    "plan": [],
}

PLAN = {
    "items": [
        {"item_id": "a", "repo": qd.REPO, "title": "ours", "rank": 1,
         "rank_source": "ordered",
         "ref": {"kind": "issue", "value": "261"}, "blocked_by": [], "claim": None},
        {"item_id": "b", "repo": "prisonblues/nix-fleet", "title": "theirs", "rank": 2,
         "rank_source": "appended",
         "ref": None, "blocked_by": [], "claim": None},
        {"item_id": "c", "repo": None, "title": "fleet-wide", "rank": 1,
         "rank_source": "appended",
         "ref": None, "blocked_by": [], "claim": None},
    ],
    "counts": {"open": 3, "claimed": 0, "covered": 0, "blocked": 0, "stale": 0},
    "order_trust": {"trusted": False, "unchosen": 2},
    "next": {"item_id": "a", "repo": qd.REPO, "ref": {"kind": "issue", "value": "261"},
             "caveat": "nobody chose two of these positions"},
    "truncated": False,
}

PRS = [{"number": 265, "title": "a pr", "isDraft": False, "updatedAt": None,
        "statusCheckRollup": [], "repo": qd.REPO}]
ISSUES = [{"number": 261, "title": "an issue", "updatedAt": None, "repo": qd.REPO}]

#: One of each shape the REVIEW QUEUE panel has to draw: something to do,
#: something nothing may be done to, and an age nobody can pin down.
QUEUE = {
    "open": 3, "depth": 1, "error": None, "idle": None,
    "oldest": {"pr": 270, "age_seconds": 216_000},
    "entries": [
        {"pr": 264, "title": "a first round is owed", "state": "unreviewed",
         "next_action": "review", "drainable": True, "holds": [],
         "age_seconds": 216_000, "age_is_upper_bound": False, "repo": qd.REPO},
        {"pr": 270, "title": "conflicting, so no round", "state": "blocked",
         "next_action": "integrate", "drainable": False,
         "holds": [{"code": "conflicting", "detail": "…"}],
         "age_seconds": 90_000, "age_is_upper_bound": True, "repo": qd.REPO},
        {"pr": 190, "title": "somebody else has it", "state": "unresolved",
         "next_action": "fix", "drainable": False,
         "holds": [{"code": "claimed", "detail": "zeus/otter holds it"}],
         "age_seconds": 3_600, "age_is_upper_bound": False, "repo": "other/repo"},
    ],
}


#: Open questions a person owes an answer to, one of each shape that matters: one
#: riding a plan item's ref, two on a subject that is not on this table at all
#: (#576 made several per subject possible on purpose), and one on another repo.
BLOCKERS = [
    {"id": "b1", "repo": qd.REPO, "subject": {"kind": "issue", "value": "261"},
     "kind": "taste", "condition": "", "question": "which shade of blue?",
     "owner": "human/rich", "raised_by": "zeus/one",
     "raised_at": "2026-08-27T00:00:00+00:00"},
    {"id": "b2", "repo": qd.REPO, "subject": {"kind": "repo", "value": qd.REPO},
     "kind": "environment", "condition": "landed", "question": "4 PRs ready to land",
     "owner": None, "raised_by": "zeus/doctor",
     "raised_at": "2026-08-27T00:00:00+00:00"},
    {"id": "b3", "repo": qd.REPO, "subject": {"kind": "repo", "value": qd.REPO},
     "kind": "environment", "condition": "harness", "question": "8 scripts are not on zeus",
     "owner": None, "raised_by": "zeus/doctor",
     "raised_at": "2026-08-27T00:00:00+00:00"},
    {"id": "b4", "repo": "prisonblues/nix-fleet", "subject": {"kind": "pr", "value": "3"},
     "kind": "decision", "condition": "", "question": "land it or revert?",
     "owner": None, "raised_by": "zeus/two",
     "raised_at": "2026-08-27T00:00:00+00:00"},
]


def _table(panel):
    return panel.renderable


def _cells(panel) -> list[list[str]]:
    """The panel's rows, read back off the columns Rich actually built.

    Read this way round on purpose: `Table.grid` grows a column when a row is
    given one cell too many, so counting the COLUMNS is what catches the defect
    this file exists for — a row list would report the count it was given.
    """
    columns = [[str(c) for c in col.cells] for col in _table(panel).columns]
    return [list(row) for row in zip(*columns)] if columns else []


def _titles(panel) -> str:
    return str(panel.title)


# ---- the column count, which is the whole risk -------------------------------

# who state stage [repo] what ttl — five narrow, six wide.
@pytest.mark.parametrize("scope,expected", [(NARROW, 5), (WIDE, 6)])
def test_the_agents_panel_builds_the_columns_it_declared(dash, scope, expected):
    panel = dash.panel_agents(BOARD, 78, scope)
    assert len(_table(panel).columns) == expected


@pytest.mark.parametrize("scope,expected", [(NARROW, 5), (WIDE, 6)])
def test_an_empty_fleet_pads_to_the_same_width(dash, scope, expected):
    """"nobody home" is a hand-counted filler row, and a wrong count grows a
    column that every real row is then drawn against."""
    panel = dash.panel_agents({"agents": []}, 78, scope)
    assert len(_table(panel).columns) == expected


# ---- how far along, which is the only column that moves (#262) ---------------

def test_the_agents_panel_shows_a_reported_stage(dash):
    """`who state stage repo what ttl` — beside `state`, because the two are read
    together and answer different questions: whether the pane is moving, and
    where it has got to."""
    board = {**BOARD, "agents": [{**BOARD["agents"][0], "stage": "R1F"}]}
    assert "R1F" in [c for row in _cells(dash.panel_agents(board, 78, WIDE)) for c in row]


def test_a_lease_that_reported_no_stage_is_not_drawn_as_one(dash):
    """The majority case, and the one that must not lie.

    Every agent in BOARD predates the field, exactly as most leases will for a
    while. A cell that came out empty would read as a clipped column or a
    rendering fault; `qbdata.STAGE_UNREPORTED` is not alphanumeric, so it cannot
    be confused with a stage somebody actually said — a stage is 1-6
    alphanumerics by construction, at the board's edge and in `qb-stage`.

    Five rows and not three: BOARD's two claims name holders no agent answers
    for, so each is a row of its own. Neither has a stage either, and for a
    stronger reason — there is no agent there to have reported one.
    """
    rows = _cells(dash.panel_agents(BOARD, 78, WIDE))
    stages = [row[2] for row in rows]
    assert stages == [qd.STAGE_UNREPORTED] * 5
    assert not any(s.strip().isalnum() for s in stages)


# ---- the join three panels used to leave to the reader's eye (#589) ----------

def test_a_claim_rides_the_row_of_the_agent_holding_it(dash):
    """CLAIMED and FLEET were keyed on the same holder and split by a border.

    The `what` cell is the join: an agent with a claim says what the claim is ON,
    with the words off the plan item behind it rather than a bare `#261` — which
    was the whole of what CLAIMED could say, and the reason a reader joined three
    panels by eye to find out what somebody was doing.
    """
    board = {**BOARD,
             "agents": [{"holder": "daedalus/one", "repo": qd.REPO, "title": "here"}],
             "claims": [BOARD["claims"][0]]}
    rows = _cells(dash.panel_agents(board, 78, WIDE, qd.plan_items(PLAN)))
    assert len(rows) == 1, "the claim grew a row of its own beside its holder"
    # `quarterback#261` and not `#261`: the wide view keeps the repo, because
    # there it is what tells two claims apart (qbdata.claim_label).
    assert rows[0][0] == "one" and rows[0][4] == "quarterback#261 ours"


def test_two_claims_on_one_agent_stay_one_row(dash):
    """A row is an agent, and an agent holding two things is one agent."""
    board = {**BOARD,
             "agents": [{"holder": "daedalus/one", "repo": qd.REPO, "title": "here"}],
             "claims": [BOARD["claims"][0],
                        {"holder": "daedalus/one", "kind": "issue",
                         "key": f"{qd.REPO}#262"}]}
    rows = _cells(dash.panel_agents(board, 78, WIDE))
    assert len(rows) == 1
    assert "＋1" in rows[0][4]


def test_a_claim_nobody_answers_for_says_which_kind_of_nobody(dash):
    """#444 arriving where it costs something.

    Two claims with no live agent, and they are not the same fact: `hermes` never
    named an agent at all, so nothing can say which of that box's agents holds it;
    `zeus/gone` named one that presence no longer lists, so that agent finished
    and its claim outlived it. A merged table either tells them apart or invents
    an attribution, and the second is how a reader gets sent to the wrong agent.
    """
    board = {"agents": [{"holder": "zeus/here", "repo": qd.REPO, "title": "t"}],
             "claims": [{"holder": "hermes", "kind": "issue", "key": f"{qd.REPO}#1"},
                        {"holder": "zeus/gone", "kind": "issue", "key": f"{qd.REPO}#2"}]}
    rows = _cells(dash.panel_agents(board, 78, WIDE))
    assert [r[0] for r in rows] == ["here", "hermes", "zeus/gone"]
    assert [r[1] for r in rows] == ["—", "machine", "gone"]


def test_a_claim_whose_holder_the_scope_hid_keeps_its_row(dash):
    """The claim is in scope and its holder is not, and the merged table dropped
    it: it is not unheld, so it earns no row of its own, and its holder has no row
    to ride. CLAIMED and FLEET each answered half of this and neither noticed.

    A claim that vanishes when a pane is narrowed is the panel-that-filtered-
    silently defect (#176) applied to the one fact that stops duplicated work. It
    is drawn `elsewhere` rather than `gone` — the agent is alive, just not here —
    and it is not counted as a loose end in the title.
    """
    board = {"agents": [{"holder": "zeus/away", "repo": "other/repo", "title": "t"}],
             "claims": [{"holder": "zeus/away", "kind": "issue",
                         "key": f"{qd.REPO}#9"}]}
    panel = dash.panel_agents(board, 78, NARROW)
    rows = _cells(panel)
    assert [r[0] for r in rows] == ["zeus/away"]
    assert rows[0][1] == "elsewhere"
    assert "unheld" not in _titles(panel)


def test_the_agents_title_counts_the_unheld_separately(dash):
    """Never folded into "live": an unattributed claim is the opposite of live,
    and it is the row a reader is being asked to do something about."""
    title = _titles(dash.panel_agents(BOARD, 78, WIDE))
    assert "3 live" in title and "2 unheld" in title


# ---- WORK: four panels, one question ----------------------------------------

def _work(dash, width=78, scope=WIDE, plan=None, err=None, prs=None, queue=None,
          issues=None, held=None, backlog=False, claims_known=True,
          pr_err=None, issue_err=None, blockers=None, waiting_only=False):
    gh = {"prs": PRS if prs is None else prs,
          "queue": QUEUE if queue is None else queue,
          "issues": ISSUES if issues is None else issues,
          "pr_err": pr_err, "issue_err": issue_err}
    return dash.panel_work(PLAN if plan is None else plan, err, gh,
                           {} if held is None else held, width, scope, backlog,
                           claims_known, blockers, waiting_only)


# glyph kind [repo] rank ref title why — six narrow, seven wide.
@pytest.mark.parametrize("scope,expected", [(NARROW, 6), (WIDE, 7)])
def test_the_work_panel_and_its_filler_rows_agree(dash, scope, expected):
    """Four shapes through one panel: rows, the "…and N more" lines, an error row
    that starts with a glyph and pads with `filler[1:]`, and the empty state."""
    assert len(_table(_work(dash, scope=scope)).columns) == expected
    assert len(_table(_work(dash, scope=scope, plan={}, prs=[], queue={},
                            issues=[])).columns) == expected
    assert len(_table(_work(dash, scope=scope, err="board is down")).columns) == expected
    assert len(_table(_work(dash, scope=scope, queue={"error": "board is down"},
                            prs=[])).columns) == expected
    many = {**PLAN, "items": [dict(PLAN["items"][0], item_id=str(n), title=f"item {n}")
                              for n in range(30)]}
    assert len(_table(_work(dash, scope=scope, plan=many)).columns) == expected
    assert len(_table(_work(dash, scope=scope, backlog=True)).columns) == expected


def test_the_review_queue_is_drawn_above_the_plan(dash):
    """A PR waiting on a review is finished work that needs somebody, and the
    board's order is an order over plan ITEMS — it says nothing about where a PR
    sits among them, because PRs are not in it.

    Appending was the first cut and it read badly on real data: forty-two plan
    rows above five review rows, in a table showing twenty, is a review queue
    that is technically present and practically invisible — the shape of the
    defect #273 was filed about.
    """
    rows = _cells(_work(dash))
    assert [r[4] for r in rows][:3] == ["#264", "#270", "#190"]
    assert [r[1] for r in rows][:3] == ["pr"] * 3
    assert [r[5] for r in rows][3:] == ["ours", "theirs", "fleet-wide"]


def test_the_work_panel_keeps_the_queue_rows_nothing_can_act_on(dash):
    """A panel that dropped its blocked rows would report an empty queue for a
    repo where everything is stuck — the reading #273 exists to end. The verb
    column says why instead of a verb, with the age beside it."""
    rows = _cells(_work(dash))[:3]
    # Clipped to the 17 the column has, which is why the middle one ends in a `…`.
    assert [r[6] for r in rows] == ["panel 2d12h", "conflicting ~1d0…",
                                    "claimed 1h00m"]


def test_work_that_is_not_on_the_plan_is_unranked_rather_than_last(dash):
    """The board deliberately refuses to rank the queue (#232 owns the order), so
    a number here would be this panel inventing one."""
    rows = _cells(_work(dash))
    assert [r[3] for r in rows][:3] == [qd.UNRANKED] * 3
    assert [r[3] for r in rows][3:] == ["1", "~2", "~1"]


def test_a_pr_wears_its_ci_rollup_where_a_plan_row_wears_its_state(dash):
    """#272's rule: a PR's state is its checks, because that is the fact a PR row
    is read for — and it is the only thing the OPEN PRs panel contributed that
    the review queue could not."""
    queue = dict(QUEUE, entries=[dict(QUEUE["entries"][0], pr=265)])
    prs = [dict(PRS[0], ci=qd.CiReport("red", "1 failing"))]
    rows = _cells(_work(dash, prs=prs, queue=queue))
    assert rows[0][0] == qd.CI_GLYPHS["red"][0]


def test_the_backlog_is_hidden_but_counted(dash):
    """A toggled list that left no number behind would be a way of forgetting the
    work exists — the same silent narrowing `elsewhere` exists to prevent."""
    # An issue the plan does NOT carry, so hiding it really does hide something.
    spare = [dict(ISSUES[0], number=999, title="nobody has planned this")]
    drained = {"open": 1, "depth": 0, "entries": [], "error": None}
    off = _work(dash, queue=drained, issues=spare)
    assert "#265" not in [r[4] for r in _cells(off)], "a reviewed PR is not in flight"
    assert "#999" not in [r[4] for r in _cells(off)]
    assert "+1 pr" in _titles(off) and "1 free" in _titles(off)

    on = _work(dash, queue=drained, issues=spare, backlog=True)
    assert {"#265", "#999"} <= set(r[4] for r in _cells(on))
    assert "reviewed" in [r[6] for r in _cells(on)]
    assert "hidden" not in _titles(on)


def test_the_hidden_count_is_not_taken_from_claims_nobody_sent(dash):
    """`fetch_board` reports an outage as `{"claims": []}`, which is the same shape
    as "nobody holds anything" — and counting free issues off it says `27 free
    hidden` over an unreachable board, which is how a seat is sent into work
    somebody already holds."""
    spare = [dict(ISSUES[0], number=999, title="nobody has planned this")]
    known = _work(dash, issues=spare, prs=[], queue={})
    assert "1 free" in _titles(known)
    unknown = _work(dash, issues=spare, prs=[], queue={}, claims_known=False)
    assert "free" not in _titles(unknown), _titles(unknown)


def test_an_issue_the_plan_already_carries_is_not_a_second_row(dash):
    """The dedupe the four panels could not make: `#261` is a plan item AND an
    open issue, and it was two rows on two panels with nothing saying they were
    one thing."""
    rows = _cells(_work(dash, backlog=True, prs=[], queue={}))
    assert [r[4] for r in rows].count("#261") == 1


def test_the_fold_gives_the_review_rows_a_share_of_a_long_plan(dash):
    """A cap that always eats the same section is a section that is never drawn.
    Under a straight `rows[:cap]` the title said `1 waiting` and showed none of
    it — #273's hole reopened by a display limit rather than a data model."""
    many = {**PLAN, "items": [dict(PLAN["items"][0], item_id=str(n), title=f"item {n}")
                              for n in range(60)]}
    rows = _cells(_work(dash, plan=many))
    assert [r[1] for r in rows][:3] == ["pr"] * 3
    assert "more on the plan" in " ".join(c for row in rows for c in row)


def test_the_states_that_are_not_rows_are_still_rows(dash):
    """A failure and an empty table are facts, and neither is an absence of rows.

    The message goes in the TITLE cell — the widest one — rather than in the
    panel's title, which is bounded by the pane and was clipping it to 24
    characters. A table whose job is saying why something is missing must not
    truncate the one message that says why it cannot tell you.

    And "nothing is in flight" is only said when nothing FAILED: a table that said
    both would be answering its own error message with a claim it has no grounds
    for (#244).
    """
    err = "board unreachable: HTTPConnectionPool(host='board.invalid', port=80)"
    down = _work(dash, plan={}, err=err, prs=[], queue={}, issues=[])
    said = " ".join(c for row in _cells(down) for c in row)
    assert "board unreachable" in said and "HTTPConnectionPool" in said
    assert "nothing in flight" not in said, \
        "an empty table with a dead board reported itself as drained"

    every = _work(dash, plan={}, err=err, prs=[], issues=[],
                  queue={"error": "queue: TimeoutError"},
                  pr_err="gh: pr list failed", issue_err="gh: issue list failed")
    rows = [r for r in _cells(every) if r[0] == "!"]
    assert len(rows) == 4, f"a source's failure hid another's: {_cells(every)}"

    quiet = _work(dash, plan={}, prs=[], issues=[],
                  queue={"open": 0, "depth": 0, "entries": [], "error": None,
                         "idle": "every open PR has had a round"})
    assert "every open PR has had a round" in \
        " ".join(c for row in _cells(quiet) for c in row), \
        "the board's own wording for a drained queue was dropped"


# ---- and that the panels are actually scoped --------------------------------

def test_the_printed_panels_narrow_and_say_what_they_hid(dash):
    agents = dash.panel_agents(BOARD, 78, NARROW)
    # Sorted by repo, so the row nothing could attribute leads — wearing the mark
    # that is all this view has left to say so. Column 3: who state stage what ttl.
    assert [r[3] for r in _cells(agents)][:2] == ["? nowhere", "here"]
    assert "1 elsewhere" in _titles(agents)
    # And the claim from another repo is gone with it, while ours keeps its row.
    assert [r[0] for r in _cells(agents)][2:] == ["daedalus/one"]

    work = _work(dash, scope=NARROW, prs=[], queue={}, issues=[])
    assert [r[4] for r in _cells(work)] == ["ours", "? fleet-wide"]
    assert "1 elsewhere" in _titles(work)


def test_the_work_row_carries_the_rank_and_who_chose_it(dash):
    """The human's order used to reach the terminal as row position and nothing
    else — the one presentation that cannot tell a chosen priority from where an
    add happened to land (#183). `~` is the mark, and the title counts them."""
    rows = _cells(_work(dash, prs=[], queue={}))
    assert [r[3] for r in rows] == ["1", "~2", "~1"]
    assert [r[4] for r in rows] == ["#261", "", ""]


def test_the_printed_work_draws_the_boards_order_and_not_its_own(dash):
    """`sort_plan` re-banded the rows here — taken, free, blocked — which is a
    second answer about an ordered list computed against that list's own order,
    and the reason the two surfaces disagreed about what was next."""
    plan = {**PLAN, "items": [dict(PLAN["items"][0], item_id="held", title="held",
                                   claim={"holder": "zeus/one"}),
                              dict(PLAN["items"][0], item_id="free", title="free")]}
    rows = _cells(_work(dash, plan=plan, prs=[], queue={}))
    assert [r[5] for r in rows] == ["held", "free"]


def test_a_narrow_pane_gives_up_a_column_rather_than_the_glyph(dash):
    """Every column but the title is fixed, so the title cell pays for all of
    them — and rich takes the shortfall out of the fixed columns from the LEFT, so
    at 45 columns the state glyph itself came out blank. The rank goes instead,
    and its headline (`~N unchosen`) is in the title either way."""
    wide = _table(_work(dash, width=100, prs=[], queue={}))
    narrow = _table(_work(dash, width=45, prs=[], queue={}))
    assert len(wide.columns) == 7 and len(narrow.columns) == 6
    assert [r[0] for r in _cells(_work(dash, width=45, prs=[], queue={}))] == \
        ["◉", "○", "○"], "the state glyph was squeezed out of a narrow pane"
    assert "next #261" in _titles(_work(dash, width=45, prs=[], queue={})), \
        "the answer went before the tally that qualifies it"


def test_the_printed_work_title_carries_the_envelope(dash):
    """Six of the board's answers reached no terminal surface at all. They are
    facts about the LIST, so they go in the title — #269 measured 55 rows drawn
    into a 38-row pane, and nothing here may add a row."""
    title = _titles(_work(dash, plan={**PLAN, "truncated": True}, prs=[], queue={},
                          issues=[]))
    assert "3 open" in title and "next #261" in title
    assert "~2 unchosen" in title and "truncated at 3" in title


def test_the_printed_work_marks_the_row_the_board_would_take_next(dash):
    rows = _cells(_work(dash, prs=[], queue={}))
    assert [r[0] for r in rows] == ["◉", "○", "○"]


def test_the_printed_panels_go_wide_and_claim_to_hide_nothing(dash):
    """Sorted by repo, so the unattributed row leads — and it needs no mark here,
    because the cell that says so is back."""
    agents = dash.panel_agents(BOARD, 78, WIDE)
    # who state stage repo what ttl.
    assert [r[4] for r in _cells(agents)][:3] == ["nowhere", "elsewhere", "here"]
    assert "1 elsewhere" not in _titles(agents)
    assert [r[3] for r in _cells(agents)][:3] == ["—", "prisonblue…", "quarterback"]
    assert "quarterback#261" in [r[4] for r in _cells(agents)]


# ---- what a person owes an answer to (#328) ----------------------------------

def _blocked(dash, **kw):
    kw.setdefault("blockers", {"blockers": BLOCKERS, "counts": {}, "error": None})
    return _work(dash, prs=[], queue={}, **kw)


def test_a_question_rides_the_row_its_subject_names(dash):
    """`/plan` has served `waiting_on_a_human` on every item since #328's row
    landed, and this dashboard referenced it zero times — the field was being
    served and dropped. A question about #261 belongs on the row for #261.

    The glyph WINS over the item's own state and the cell over its holder, because
    both describe something that is not going to happen until a person answers.
    """
    # Wide: glyph kind repo rank ref title why.
    rows = _cells(_blocked(dash))
    ours = [r for r in rows if r[4] == "#261"]
    assert len(ours) == 1, f"the question grew a row beside its subject: {rows}"
    assert ours[0][0] == "⚑", ours[0]
    assert ours[0][6].startswith("⚑taste"), ours[0]
    assert ours[0][3] == "1", "the item lost its place in the plan"


def test_the_frame_says_a_person_is_being_waited_on(dash):
    """The field that was served and dropped, asserted through the whole frame.

    `/plan` has carried `waiting_on_a_human` per item since #328's row landed —
    `app/api/plan.py` builds it, `plan.html` draws a chip from it — and this
    dashboard referenced it **zero times** across three files. So an item nobody
    can proceed on rendered as `○`, in the cyan the panel uses for *free to take*,
    which is the strongest possible invitation to pick up work that is stuck.

    Through `frame` rather than the panel, because that is the version that runs
    against the renderer as it was: the field was on the wire and the frame drew
    it as free, so this fails on the assertion rather than on an import.
    """
    plan = {**PLAN, "items": [dict(PLAN["items"][0], waiting_on_a_human=[
        {"blocker_id": "z", "class": "taste", "question": "which shade of blue?",
         "owner": None, "raised_by": "zeus/one",
         "raised": "2026-08-27T00:00:00+00:00"}])]}
    frame = dash.frame(SimpleNamespace(agent="host", base_url="http://board.invalid"),
                       {"agents": [], "claims": [], "plan": plan, "plan_err": None,
                        "dials": {}},
                       {"prs": [], "pr_err": None, "issues": [], "issue_err": None,
                        "queue": {}},
                       78, {}, WIDE)
    console = Console(width=78, force_terminal=False)
    with console.capture() as caught:
        console.print(frame)
    drawn = caught.get()
    assert "⚑" in drawn, "an item waiting on a person is not marked as one"
    assert "⚑taste" in drawn, "the row does not say what kind of answer is owed"
    assert "◉ " not in drawn, \
        "a blocked item is still drawn as the one the board would take next"


def test_a_question_with_no_row_to_ride_gets_one(dash):
    """The half that would otherwise be invisible, and it is the half #274 is about.

    A blocker's subject is one of item, issue, pr or repo, and three of those can
    name something this table is not drawing: `qb-doctor` raises `landed` and
    `harness` against the REPO, and those are the "the fleet is stuck" ones. Left
    out they are counted by the header and drawn nowhere, and a number that
    disagrees with the rows under it is the one thing a surface a person is asked
    to trust cannot do.
    """
    rows = _cells(_blocked(dash))
    repo_rows = [r for r in rows if r[1] == "repo"]
    assert len(repo_rows) == 1, f"the repo's questions reached no row: {rows}"
    assert repo_rows[0][0] == "⚑"
    assert repo_rows[0][3] == qd.UNRANKED, "a question was given a place in the plan"


def test_several_questions_on_one_subject_are_counted_not_collapsed(dash):
    """#576 made `condition` part of the uniqueness key so one producer could ask
    several different things about one subject. A cell that showed the first would
    be that issue's undercount reintroduced one surface further on."""
    rows = _cells(_blocked(dash))
    cell = next(r[6] for r in rows if r[1] == "repo")
    assert "＋1" in cell, cell
    assert "environment" in cell


def test_the_count_survives_a_cell_too_narrow_for_the_owner(dash):
    """What goes first when it does not all fit: the class, then `＋N`, then the
    owner or the age. A cell that spent its last characters on a name and clipped
    the count would undercount the surface #576 exists to have counting right."""
    many = [dict(BLOCKERS[1], id=str(n)) for n in range(4)]
    assert qd.blocker_cell(many)[0] == "⚑environment ＋3", \
        "the count was clipped to make room for something else"
    # And where even the age will not fit beside the longest class name, the class
    # is what survives — dropped WHOLE rather than clipped to `⚑environmen…`, so
    # the cell never shows half a word from a closed vocabulary.
    assert qd.blocker_cell(many[:1])[0] == "⚑environment"
    # A short class leaves room for both. The owner is preferred over the age:
    # an unowned question is everyone's to answer and nobody's to be asked about.
    assert qd.blocker_cell([BLOCKERS[0]])[0] == "⚑taste rich"


def test_the_waiting_view_shows_only_what_is_waiting_and_counts_it_the_same_way(dash):
    """The filter, and the two numbers agreeing.

    The header counts QUESTIONS and the title used to count ROWS, which is two
    numbers about one thing in two units — `WAITING 4` over a table saying `1
    waiting on a human`. `＋N` on a row is what reconciles them by eye.
    """
    panel = _blocked(dash, waiting_only=True)
    rows = _cells(panel)
    assert all(r[0] == "⚑" for r in rows), rows
    assert len(rows) == 3, "the filter kept a row nobody owes an answer about"
    # FOUR questions over three rows — two of them share the repo — and the title
    # counts questions, not rows, exactly as the header cell does. `＋1` on the
    # repo row is what reconciles them by eye.
    assert "4 waiting on a human" in _titles(panel)
    assert dash.waiting_line({"blockers": BLOCKERS}, WIDE).plain == "WAITING 4"


def test_the_waiting_view_counts_only_questions_as_elsewhere(dash):
    """`hidden` is plan items the scope dropped, which in this view is a count
    about a list the reader has just asked not to see. Here the only thing that
    can be elsewhere is a question on another project's work."""
    assert "1 elsewhere" in _titles(_blocked(dash, waiting_only=True, scope=NARROW))


def test_the_waiting_title_does_not_claim_the_plans_counts(dash):
    """`39 open · next #1620` over two rows a person owes an answer about is a
    title describing a list the reader has just filtered away."""
    title = _titles(_blocked(dash, waiting_only=True))
    assert "open" not in title and "next" not in title, title
    assert "hidden" not in title, "the backlog toggle's count survived the filter"


def test_a_board_that_cannot_be_asked_does_not_report_nothing_waiting(dash):
    """#244 on the one panel a person is asked to trust: a read that failed is not
    a count of zero, and `WAITING 0` over an unreachable board is the sentence
    that stops somebody looking."""
    assert dash.waiting_line({"error": "board: TimeoutError"}).plain == "WAITING ?"
    assert dash.waiting_line(None).plain == "", "a cell was drawn before anyone asked"
    assert dash.waiting_line({"blockers": []}).plain == "WAITING 0", \
        "the answer somebody opens this to get was the one it would not say"
    said = " ".join(c for row in _cells(
        _blocked(dash, blockers={"error": "board: TimeoutError"})) for c in row)
    assert "TimeoutError" in said


# ---- the frame, which is what #589 was measured on ---------------------------

def test_the_frame_answers_two_questions_in_two_tables(dash):
    """The headline claim, asserted the way it was found: by counting rows.

    A frame of eight panels spends sixteen of them on borders and headings before
    a single fact is drawn, and one measured frame of this dashboard came to 61
    rows — into the 38-row pane #269 measured, and AFTER that issue's per-panel
    caps had already stood in for 48 rows nobody could see.

    The budget here is deliberately loose. What it pins is the shape — two tables
    and the dials, not eight panels — and a frame that grew a ninth border would
    fail it while a frame that grew a legitimate row would not. The fixtures are
    the same literals the panel tests use, so the number moves only when the
    layout does.
    """
    frame = dash.frame(SimpleNamespace(agent="host", base_url="http://board.invalid"),
                       {"agents": BOARD["agents"], "claims": BOARD["claims"],
                        "plan": PLAN, "plan_err": None, "dials": {}},
                       {"prs": PRS, "pr_err": None, "issues": ISSUES,
                        "issue_err": None, "queue": QUEUE},
                       78, {}, WIDE)
    console = Console(width=78, force_terminal=False)
    with console.capture() as caught:
        console.print(frame)
    lines = caught.get().rstrip("\n").split("\n")
    borders = [line for line in lines if line.startswith("╭")]
    assert len(borders) == 4, \
        f"the frame draws {len(borders)} panels, not the header, the dials and two tables"
    assert len(lines) <= 30, (
        f"the frame is {len(lines)} rows — #269 measured the pane it goes in at 38, "
        f"and this fixture is three plan items, one PR, one issue and three queue "
        f"entries")


# ---- the tallies the toggled lists left behind ------------------------------

def test_the_queue_rides_the_header_line_beside_the_caps(dash):
    line = dash.queue_line(QUEUE)
    assert "REVIEW" in line.plain and "1 waiting" in line.plain
    assert "oldest 2d12h" in line.plain
    # Never fetched is never rendered; a depth of zero still is.
    assert dash.queue_line({}).plain == ""
    assert "0 waiting" in dash.queue_line(
        {"open": 2, "depth": 0, "error": None}).plain


def test_the_pr_count_and_its_checks_ride_the_header_too(dash):
    """The half of the OPEN PRs panel worth keeping: its rows were the review
    queue's rows, but a PR can be green and unreviewed or red and already signed
    off, so the tally is not derivable from the queue that replaced them."""
    assert dash.prs_line(None, None).plain == ""
    assert dash.prs_line([], None).plain == "PRs 0"
    red = dash.prs_line([dict(PRS[0], ci=qd.CiReport("red", "1 failing"))], None)
    assert "PRs 1" in red.plain and "1 red" in red.plain
    assert dash.prs_line([], "gh is down").plain == "PRs ?"


def test_the_issue_count_says_free_only_when_the_board_answered(dash):
    """`free` is counted off claims that may be stale or were never fetched, and a
    count taken from no claims at all is how a seat is sent into work somebody
    already holds."""
    assert dash.issues_line(None, None, None).plain == ""
    assert "30" in dash.issues_line([dict(ISSUES[0], number=n) for n in range(30)],
                                    {}, None).plain
    assert "1 free" in dash.issues_line(ISSUES, {}, None).plain
    assert "free" not in dash.issues_line(ISSUES, None, None).plain
    # `fetch_board` reports an outage as `{"claims": []}`, which counts every open
    # issue as free — the same collapse the table's own title refuses, and the
    # header line was making it.
    assert "free" not in dash.issues_line(ISSUES, {}, None, claims_known=False).plain
    assert dash.issues_line([], "gh is down", None).plain in ("ISSUES ?", "ISSUES 0")


# ---- main(): the wiring that had no test ------------------------------------

def _tui():
    return _load("qb_dash_tui_main", "qb-dash-tui.py")


class Recorder:
    """Stands in for the app, so main()'s decisions can be read without a screen."""

    seen: dict = {}

    def __init__(self, **kwargs):
        Recorder.seen = dict(kwargs)

    def run(self):
        Recorder.seen["ran"] = True


@needs_textual
@pytest.mark.parametrize("argv,narrow", [
    ([], True),                              # the default, which is the point of it
    (["--scope", "repo"], True),
    (["--scope", "all"], False),
])
def test_the_tui_maps_scope_to_the_view_it_names(argv, narrow, watched, monkeypatch):
    """Invert this mapping and `--scope all` narrows, with nothing to notice."""
    monkeypatch.delenv(qd.SCOPE_ENV, raising=False)
    tui = _tui()
    monkeypatch.setattr(tui, "Dash", Recorder)
    assert tui.main(argv) == 0
    assert Recorder.seen["ran"] is True
    assert Recorder.seen["scope"].on is narrow


@needs_textual
def test_the_tui_pins_the_repos_before_it_resolves_the_scope(watched, monkeypatch):
    """The order is load-bearing: `resolve_repos` caches, so resolving the scope
    first would filter on the cwd's repo while `gh` and the plan watch the pinned
    one — two panels disagreeing about which project the screen is."""
    monkeypatch.delenv(qd.SCOPE_ENV, raising=False)
    tui = _tui()
    monkeypatch.setattr(tui, "Dash", Recorder)
    assert tui.main(["--repo", "someone/other"]) == 0
    assert qd.resolve_repos() == ["someone/other"]
    assert Recorder.seen["scope"].names == {"other"}, "the scope resolved before the pin"


@pytest.fixture
def checkout(tmp_path):
    """A git checkout with an origin remote, BUILT here rather than borrowed.

    NOT `Path(__file__).parents[2]`, which is this repo when a developer runs the
    suite and `/build` when the `worktree-tests` sandbox does — and that sandbox
    holds `harness/bin` and `harness/tests` and is not a git repository at all.
    `--repo <that>` would raise "not a git checkout with an origin remote" and the
    test would fail on the environment rather than on the code. It does not fail
    there today only because textual is absent and this test skips, which makes it
    a trap set for whoever adds textual to that check rather than a test that
    passes. The sibling fixture in test_qbdata.py is the same thing for the same
    reason; what is under test is `repo_target`'s reading of *a* checkout.
    """
    root = tmp_path / "wt-review"
    root.mkdir()
    for args in (["init", "-q"],
                 ["remote", "add", "origin",
                  "https://github.com/prisonblues/quarterback.git"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root


@needs_textual
def test_a_checkout_argument_also_moves_where_the_tui_launches_work(
        watched, monkeypatch, checkout):
    """The P1 this closes: `--repo` used to redirect only what the panels DRAW, so
    the named repo's ⚒ rows were drawn takeable and then refused one by one."""
    tui = _tui()
    monkeypatch.setattr(tui, "Dash", Recorder)
    here = str(checkout)
    assert tui.main(["--repo", here]) == 0
    assert Recorder.seen["repo"] == here
    # A slug names a repo this process may have no checkout of, so it moves nothing.
    assert tui.main(["--repo", "someone/other"]) == 0
    assert Recorder.seen["repo"] is None


def test_the_printed_renderer_refuses_a_repo_that_names_nothing(dash, watched):
    """Exit 2 rather than a dashboard quietly watching the cwd instead."""
    assert dash.main(["--repo", "no-such-name"]) == 2


@needs_textual
def test_the_clickable_renderer_refuses_one_too(watched, monkeypatch):
    tui = _tui()
    monkeypatch.setattr(tui, "Dash", Recorder)
    assert tui.main(["--repo", "no-such-name"]) == 2


def test_the_printed_renderer_maps_scope_and_pins_repos_too(dash, watched, monkeypatch):
    """Same two decisions, same order, in the renderer that has no keyboard to
    correct them with."""
    monkeypatch.delenv(qd.SCOPE_ENV, raising=False)
    seen: dict = {}
    monkeypatch.setattr(dash, "board_client", lambda: (None, object()))
    monkeypatch.setattr(dash, "fetch_state", lambda client: {"plan": [], "plan_err": None})
    monkeypatch.setattr(dash, "fetch_gh", lambda client=None: {
        "prs": [], "pr_err": None, "issues": [], "issue_err": None, "queue": {}})
    monkeypatch.setattr(dash, "refresh_limits", lambda caps: caps)
    monkeypatch.setattr(dash, "frame",
                        lambda cfg, data, gh, width, caps=None, scope=None,
                        backlog=False, waiting=False:
                        seen.setdefault("scope", scope) or "")

    assert dash.main(["--once", "--width", "78", "--repo", "someone/other"]) == 0
    assert qd.resolve_repos() == ["someone/other"]
    assert seen["scope"].on is True and seen["scope"].names == {"other"}

    seen.clear()
    assert dash.main(["--once", "--width", "78", "--scope", "all"]) == 0
    assert seen["scope"].on is False


# ---- a PR whose checks are absent (#324) --------------------------------------


def test_a_pr_with_no_check_result_is_not_drawn_as_a_quiet_grey_dot(dash):
    """The dot is the rendering a reader scrolls past, and it used to be what an
    absent check result got. PR #282 wore it for two days over a suite that had gone
    red and two commits whose runs were gated. A row nobody has established anything
    about renders as unread, in a colour that asks to be looked at."""
    glyph, colour = qd.ci_state(PRS[0])
    assert (glyph, colour) != ("·", "grey50")
    assert (glyph, colour) == qd.CI_GLYPHS["unknown"]


def test_the_pr_tally_counts_every_state_not_only_red(dash):
    """A gated PR contributed to no number on the screen: the title said "12 open, 0
    red" while one of them had failed and been buried behind an approval gate.

    It rides the header line rather than a panel title now (#589), and it is the
    one thing the OPEN PRs panel said that the review queue which replaced its rows
    cannot: a PR can be green and unreviewed, or red and already signed off.
    """
    prs = [{"number": n, "title": "t", "repo": qd.REPO, "ci": qd.CiReport(state, state)}
           for n, state in enumerate(("green", "red", "blocked", "none", "unknown"))]
    tally = dash.prs_line(prs, None).plain
    for word in ("1 red", "1 blocked", "1 untested", "1 unread"):
        assert word in tally, tally
    assert "green" not in tally


def test_a_state_with_nothing_in_it_is_left_out_of_the_tally(dash):
    assert qd.pr_tally([{"ci": qd.CiReport("green", "g")}]) == []
