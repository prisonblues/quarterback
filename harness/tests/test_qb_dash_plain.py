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

Needs `rich` and nothing else — no textual, no board, no `gh`. That is deliberate:
the plain renderer is the one that must keep working on an interpreter carrying
only rich, so its test must not drag textual in to prove it.

Run: pytest harness/tests/test_qb_dash_plain.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))

pytest.importorskip("rich", reason="the printed renderer needs rich")

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

@pytest.mark.parametrize("scope,expected", [(NARROW, 4), (WIDE, 5)])
def test_the_fleet_panel_builds_the_columns_it_declared(dash, scope, expected):
    panel = dash.panel_agents(BOARD, 78, scope)
    assert len(_table(panel).columns) == expected


@pytest.mark.parametrize("scope,expected", [(NARROW, 4), (WIDE, 5)])
def test_an_empty_fleet_pads_to_the_same_width(dash, scope, expected):
    """"nobody home" is a hand-counted filler row, and a wrong count grows a
    column that every real row is then drawn against."""
    panel = dash.panel_agents({"agents": []}, 78, scope)
    assert len(_table(panel).columns) == expected


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
    # that is all this view has left to say so.
    assert [r[2] for r in _cells(fleet)] == ["? nowhere", "here"]
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
    assert [r[3] for r in _cells(fleet)] == ["nowhere", "elsewhere", "here"]
    assert "1 elsewhere" not in _titles(fleet)
    assert [r[2] for r in _cells(fleet)] == ["—", "prisonblue…", "quarterback"]
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


def test_a_checkout_argument_also_moves_where_the_tui_launches_work(watched, monkeypatch):
    """The P1 this closes: `--repo` used to redirect only what the panels DRAW, so
    the named repo's ⚒ rows were drawn takeable and then refused one by one."""
    tui = _tui()
    monkeypatch.setattr(tui, "Dash", Recorder)
    here = str(Path(__file__).resolve().parent.parent.parent)
    assert tui.main(["--repo", here]) == 0
    assert Recorder.seen["repo"] == here
    # A slug names a repo this process may have no checkout of, so it moves nothing.
    assert tui.main(["--repo", "someone/other"]) == 0
    assert Recorder.seen["repo"] is None


def test_both_renderers_refuse_a_repo_that_names_nothing(dash, watched, monkeypatch):
    """Exit 2 rather than a dashboard quietly watching the cwd instead."""
    tui = _tui()
    monkeypatch.setattr(tui, "Dash", Recorder)
    assert tui.main(["--repo", "quarterback"]) == 2
    assert dash.main(["--repo", "quarterback"]) == 2


def test_the_printed_renderer_maps_scope_and_pins_repos_too(dash, watched, monkeypatch):
    """Same two decisions, same order, in the renderer that has no keyboard to
    correct them with."""
    monkeypatch.delenv(qd.SCOPE_ENV, raising=False)
    seen: dict = {}
    monkeypatch.setattr(dash, "board_client", lambda: (None, object()))
    monkeypatch.setattr(dash, "fetch_state", lambda client: {"plan": [], "plan_err": None})
    monkeypatch.setattr(dash, "fetch_gh", lambda: {"prs": [], "pr_err": None,
                                                   "issues": [], "issue_err": None})
    monkeypatch.setattr(dash, "refresh_limits", lambda caps: caps)
    monkeypatch.setattr(dash, "frame", lambda cfg, data, gh, width, caps=None, scope=None:
                        seen.setdefault("scope", scope) or "")

    assert dash.main(["--once", "--width", "78", "--repo", "someone/other"]) == 0
    assert qd.resolve_repos() == ["someone/other"]
    assert seen["scope"].on is True and seen["scope"].names == {"other"}

    seen.clear()
    assert dash.main(["--once", "--width", "78", "--scope", "all"]) == 0
    assert seen["scope"].on is False
