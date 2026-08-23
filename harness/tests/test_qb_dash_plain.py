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
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))

pytest.importorskip("rich", reason="the printed renderer needs rich")

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

PLAN = [
    {"item_id": "a", "repo": qd.REPO, "title": "ours",
     "ref": {"kind": "issue", "value": "261"}, "blocked_by": [], "claim": None},
    {"item_id": "b", "repo": "prisonblues/nix-fleet", "title": "theirs",
     "ref": None, "blocked_by": [], "claim": None},
    {"item_id": "c", "repo": None, "title": "fleet-wide",
     "ref": None, "blocked_by": [], "claim": None},
]

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
def test_the_fleet_panel_builds_the_columns_it_declared(dash, scope, expected):
    panel = dash.panel_agents(BOARD, 78, scope)
    assert len(_table(panel).columns) == expected


@pytest.mark.parametrize("scope,expected", [(NARROW, 5), (WIDE, 6)])
def test_an_empty_fleet_pads_to_the_same_width(dash, scope, expected):
    """"nobody home" is a hand-counted filler row, and a wrong count grows a
    column that every real row is then drawn against."""
    panel = dash.panel_agents({"agents": []}, 78, scope)
    assert len(_table(panel).columns) == expected


# ---- how far along, which is the only column that moves (#262) ---------------

def test_the_fleet_panel_shows_a_reported_stage(dash):
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
    """
    rows = _cells(dash.panel_agents(BOARD, 78, WIDE))
    stages = [row[2] for row in rows]
    assert stages == [qd.STAGE_UNREPORTED] * len(BOARD["agents"])
    assert not any(s.strip().isalnum() for s in stages)


@pytest.mark.parametrize("scope,expected", [(NARROW, 4), (WIDE, 5)])
def test_the_plan_panel_and_its_three_filler_rows_agree(dash, scope, expected):
    """Three shapes through one panel: rows, the "…and N more" line, and an error
    row that starts with a glyph and pads with `filler[1:]`."""
    assert len(_table(dash.panel_plan(PLAN, None, 78, scope)).columns) == expected
    assert len(_table(dash.panel_plan([], None, 78, scope)).columns) == expected
    assert len(_table(dash.panel_plan(PLAN, "board is down", 78, scope)).columns) == expected
    many = [dict(PLAN[0], item_id=str(n), title=f"item {n}") for n in range(30)]
    assert len(_table(dash.panel_plan(many, None, 78, scope)).columns) == expected


@pytest.mark.parametrize("scope,expected", [(NARROW, 4), (WIDE, 5)])
def test_the_pr_panel_and_its_filler_rows_agree(dash, scope, expected):
    assert len(_table(dash.panel_prs(PRS, None, 78, scope)).columns) == expected
    assert len(_table(dash.panel_prs([], None, 78, scope)).columns) == expected
    assert len(_table(dash.panel_prs([], "gh is down", 78, scope)).columns) == expected


@pytest.mark.parametrize("scope,expected", [(NARROW, 5), (WIDE, 6)])
def test_the_review_queue_panel_and_its_filler_rows_agree(dash, scope, expected):
    assert len(_table(dash.panel_review_queue(QUEUE, 78, scope)).columns) == expected
    assert len(_table(dash.panel_review_queue({}, 78, scope)).columns) == expected
    assert len(_table(dash.panel_review_queue(
        {"error": "board is down"}, 78, scope)).columns) == expected
    many = dict(QUEUE, entries=[dict(QUEUE["entries"][0], pr=n) for n in range(30)])
    assert len(_table(dash.panel_review_queue(many, 78, scope)).columns) == expected


def test_the_review_queue_shows_what_is_held_rather_than_hiding_it(dash):
    """A panel that dropped its blocked rows would report an empty queue for a
    repo where everything is stuck — the reading #273 exists to end."""
    panel = dash.panel_review_queue(QUEUE, 78, WIDE)
    rows = _cells(panel)
    assert [r[2] for r in rows] == ["#264", "#270", "#190"]
    # A drainable row shows the VERB; a held one shows why it is not offered.
    assert [r[3] for r in rows] == ["panel", "conflicting", "claimed"]
    # An age nothing recorded the start of wears a `~`.
    assert [r[4] for r in rows] == ["2d12h", "~1d01h", "1h00m"]
    title = _titles(panel)
    assert "1 waiting" in title and "2 held" in title and "oldest 2d12h" in title


def test_an_unasked_review_queue_does_not_render_as_a_drained_one(dash):
    """#244, on the one panel built to end it: the empty state has to say which
    empty it is."""
    drained = dash.panel_review_queue(
        {"open": 0, "depth": 0, "entries": [], "error": None,
         "idle": "quarterback: no open pull requests were supplied for this repo"},
        78, NARROW)
    assert "no open pull requests" in " ".join(c for row in _cells(drained) for c in row)

    broken = dash.panel_review_queue({"error": "board: TimeoutError"}, 78, NARROW)
    assert "TimeoutError" in " ".join(c for row in _cells(broken) for c in row)


def test_the_queue_rides_the_header_line_beside_the_caps(dash):
    line = dash.queue_line(QUEUE)
    assert "REVIEW" in line.plain and "1 waiting" in line.plain
    assert "oldest 2d12h" in line.plain
    # Never fetched is never rendered; a depth of zero still is.
    assert dash.queue_line({}).plain == ""
    assert "0 waiting" in dash.queue_line(
        {"open": 2, "depth": 0, "error": None}).plain


@pytest.mark.parametrize("scope,expected", [(NARROW, 4), (WIDE, 5)])
def test_the_issue_panel_and_its_filler_rows_agree(dash, scope, expected):
    assert len(_table(dash.panel_issues(ISSUES, {}, None, 78, scope)).columns) == expected
    assert len(_table(dash.panel_issues([], {}, None, 78, scope)).columns) == expected
    assert len(_table(dash.panel_issues([], {}, "gh is down", 78, scope)).columns) == expected
    many = [dict(ISSUES[0], number=n, title=f"issue {n}") for n in range(30)]
    assert len(_table(dash.panel_issues(many, {}, None, 78, scope)).columns) == expected


def test_the_claims_panel_keeps_its_three_columns_when_it_is_empty(dash):
    """Its empty state used to render as "nothing clai…" — the text was in the
    13-wide holder column."""
    panel = dash.panel_claims({"claims": []}, 78, NARROW)
    assert len(_table(panel).columns) == 3
    assert "nothing claimed" in " ".join(c for row in _cells(panel) for c in row)


# ---- and that the panels are actually scoped --------------------------------

def test_the_printed_panels_narrow_and_say_what_they_hid(dash):
    fleet = dash.panel_agents(BOARD, 78, NARROW)
    # Sorted by repo, so the row nothing could attribute leads — wearing the mark
    # that is all this view has left to say so. Column 3: who state stage what ttl.
    assert [r[3] for r in _cells(fleet)] == ["? nowhere", "here"]
    assert "1 elsewhere" in _titles(fleet)

    claims = dash.panel_claims(BOARD, 78, NARROW)
    assert [r[1] for r in _cells(claims)] == ["#261"]
    assert "1 elsewhere" in _titles(claims)

    plan = dash.panel_plan(PLAN, None, 78, NARROW)
    assert [r[2] for r in _cells(plan)] == ["ours", "? fleet-wide"]
    assert "1 elsewhere" in _titles(plan)


def test_the_printed_panels_go_wide_and_claim_to_hide_nothing(dash):
    """Sorted by repo, so the unattributed row leads — and it needs no mark here,
    because the cell that says so is back."""
    fleet = dash.panel_agents(BOARD, 78, WIDE)
    # who state stage repo what ttl.
    assert [r[4] for r in _cells(fleet)] == ["nowhere", "elsewhere", "here"]
    assert "1 elsewhere" not in _titles(fleet)
    assert [r[3] for r in _cells(fleet)] == ["—", "prisonblue…", "quarterback"]
    assert "quarterback#261" in [r[1] for r in _cells(dash.panel_claims(BOARD, 78, WIDE))]


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
    monkeypatch.setattr(dash, "frame", lambda cfg, data, gh, width, caps=None, scope=None:
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
    glyph, colour = dash.ci_state(PRS[0])
    assert (glyph, colour) != ("·", "grey50")
    assert (glyph, colour) == qd.CI_GLYPHS["unknown"]


def test_the_open_prs_title_counts_every_state_not_only_red(dash):
    """A gated PR contributed to no number on the screen: the title said "12 open, 0
    red" while one of them had failed and been buried behind an approval gate."""
    prs = [{"number": n, "title": "t", "repo": qd.REPO, "ci": qd.CiReport(state, state)}
           for n, state in enumerate(("green", "red", "blocked", "none", "unknown"))]
    title = dash.ci_tally(prs)
    for word in ("1 red", "1 blocked", "1 untested", "1 unread"):
        assert word in title, title
    assert "green" not in title


def test_a_state_with_nothing_in_it_is_left_out_of_the_title(dash):
    assert dash.ci_tally([{"ci": qd.CiReport("green", "g")}]) == ""
