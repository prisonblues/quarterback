"""Tests for qb-next, which answers "what can I pick up, and why" (#135 §3).

The command joins three sources that disagree in specific ways, and the joining
is the whole product — so the tests are about the seams rather than the
formatting. Nothing here touches the network: the board client and `gh` are both
substituted, because a test that needs a live board tests the board.

Run: pytest harness/tests
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name, path):
    # An explicit loader, because `spec_from_file_location` infers one from the
    # SUFFIX and returns None for a file that has none — which qb-next, being a
    # command rather than a module, deliberately does not have.
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def qbdata():
    return _load("qbdata", BIN / "qbdata.py")


@pytest.fixture(scope="module")
def qbnext(qbdata):
    # Loaded by path because the file has no .py suffix — it is a command, and
    # naming it qb_next.py would put a second spelling on PATH.
    return _load("qb_next", BIN / "qb-next")


def _issue(number, blockers=()):
    return {"number": number,
            "blockedBy": {"nodes": [{"number": n, "state": s} for n, s in blockers]}}


# ---- the closed-blocker trap ------------------------------------------------


def test_a_closed_blocker_does_not_block(qbdata, monkeypatch):
    """The edge is kept after the blocker closes, and that is correct — the
    dependency was real. Reporting it as live is what would be wrong, and two
    issues in this repo are in exactly that state right now."""
    payload = [_issue(78, [(84, "OPEN"), (77, "CLOSED")]),
               _issue(141, [(96, "CLOSED")])]
    monkeypatch.setattr(qbdata, "subprocess", _gh_returning(payload))

    blocked, error = qbdata.fetch_blocked("owner/repo")

    assert error is None
    assert blocked == {78: [84]}, "a CLOSED blocker must not appear, and 141 must not appear at all"


def test_an_issue_with_no_open_blockers_is_absent_rather_than_empty(qbdata, monkeypatch):
    """`blocked.get(n, [])` and `n in blocked` must agree, or the caller has to
    know which one this returns."""
    monkeypatch.setattr(qbdata, "subprocess", _gh_returning([_issue(1, [(2, "CLOSED")])]))
    assert qbdata.fetch_blocked("owner/repo")[0] == {}


def test_gh_failing_is_reported_and_not_raised(qbdata, monkeypatch):
    monkeypatch.setattr(qbdata, "subprocess", _gh_failing("gh: not authenticated"))
    blocked, error = qbdata.fetch_blocked("owner/repo")
    assert blocked == {}
    assert "not authenticated" in error


# ---- the ref on a plan item -------------------------------------------------


@pytest.mark.parametrize("value,want", [("44", 44), ("#44", 44), (44, 44)])
def test_an_issue_ref_is_read_whichever_way_it_is_written(qbnext, value, want):
    assert qbnext._issue_of({"ref": {"kind": "issue", "value": value}}) == want


@pytest.mark.parametrize("ref", [
    {"kind": "pr", "value": "187"},     # a PR is not an issue and must not be looked up as one
    {"kind": "issue", "value": "abc"},
    {},
])
def test_anything_that_is_not_an_issue_number_reads_as_none(qbnext, ref):
    assert qbnext._issue_of({"ref": ref}) is None


# ---- the join ---------------------------------------------------------------


def _item(rank, issue, claim=None, blocked_by=()):
    return {"rank": rank, "title": f"item {rank}", "state": "open", "phase": "free",
            "ref": {"kind": "issue", "value": str(issue)},
            "claim": claim, "blocked_by": [{"ref": b} for b in blocked_by]}


def _wire(qbnext, monkeypatch, items, claims=(), blocked=None, prs=(), errors=None):
    errors = errors or {}
    monkeypatch.setattr(qbnext, "board_client", lambda: (object(), object()))
    monkeypatch.setattr(qbnext, "fetch_board",
                        lambda _c: {"claims": list(claims), "error": errors.get("board")})
    monkeypatch.setattr(qbnext, "fetch_plan",
                        lambda _c, _r: {"items": list(items), "next": None, "counts": {},
                                        "error": errors.get("plan")})
    monkeypatch.setattr(qbnext, "fetch_prs", lambda _r: (list(prs), errors.get("prs")))
    monkeypatch.setattr(qbnext, "fetch_blocked", lambda _r: (blocked or {}, errors.get("github")))


def test_a_github_edge_blocks_an_item_the_plan_does_not_know_about(qbnext, monkeypatch):
    """The two graphs are not the same graph. The plan holds dependencies an
    agent recorded; GitHub holds the ones on the issues. Either blocks."""
    _wire(qbnext, monkeypatch, [_item(1, 80)], blocked={80: [94, 101]})
    data = qbnext.collect("owner/repo")
    assert data["items"][0]["blocked_by_github"] == [94, 101]
    assert "#80    waits on #94, #101" in qbnext.render(data, 5)


def test_a_plain_claim_still_shows_the_item_as_held(qbnext, monkeypatch):
    """A claim taken with `claim` rather than `plan_claim` does not surface on
    the plan item — #172. Until that is fixed, falling back to /claims is the
    difference between "free" and "somebody is already on this"."""
    _wire(qbnext, monkeypatch, [_item(1, 163)],
          claims=[{"key": "owner/repo#163", "holder": "zeus/someone", "expires": None}])
    data = qbnext.collect("owner/repo")
    assert data["items"][0]["claim"]["holder"] == "zeus/someone"
    assert "→ #163" not in qbnext.render(data, 5), "a held item must not be offered as free"


def test_held_and_blocked_items_are_never_offered_as_free(qbnext, monkeypatch):
    _wire(qbnext, monkeypatch,
          [_item(1, 10, claim={"holder": "x", "expires": None}),
           _item(2, 20, blocked_by=["30"]),
           _item(3, 40)],
          blocked={})
    free = qbnext._free_lines(qbnext.collect("owner/repo")["items"], 5)
    assert len(free) == 1 and "#40" in free[0]


def test_the_header_agrees_with_the_sections_below_it(qbnext, monkeypatch):
    """The plan's own `counts` cannot see GitHub's edges, so a header taken from
    it under-reports the moment one contributes — and a header that disagrees
    with the list under it is worse than no header."""
    _wire(qbnext, monkeypatch,
          [_item(1, 10, claim={"holder": "x", "expires": None}), _item(2, 20), _item(3, 30)],
          blocked={30: [99]})
    out = qbnext.render(qbnext.collect("owner/repo"), 5)
    assert "PLAN — 3 open, 1 held, 1 blocked" in out
    assert out.count("waits on") == 1


def test_every_source_is_asked_about_the_repo_that_was_requested(qbnext, monkeypatch):
    """--repo used to change the plan and the heading while the PRs kept coming
    from the hardcoded default, so the output named one repo and listed
    another's work. Same class as #176: a repo that is assumed rather than
    passed."""
    asked = {}
    monkeypatch.setattr(qbnext, "board_client", lambda: (object(), object()))
    monkeypatch.setattr(qbnext, "fetch_board", lambda _c: {"claims": [], "error": None})
    monkeypatch.setattr(qbnext, "fetch_plan",
                        lambda _c, repo: asked.setdefault("plan", repo) and None
                        or {"items": [], "next": None, "counts": {}, "error": None})
    monkeypatch.setattr(qbnext, "fetch_prs",
                        lambda repo: (asked.setdefault("prs", repo), []) and ([], None))
    monkeypatch.setattr(qbnext, "fetch_blocked",
                        lambda repo: (asked.setdefault("blocked", repo), {}) and ({}, None))

    qbnext.collect("owner/other")

    assert asked == {"plan": "owner/other", "prs": "owner/other", "blocked": "owner/other"}


# ---- a source that died -----------------------------------------------------


def test_a_dead_source_is_stated_loudly_and_changes_the_exit_code(qbnext, monkeypatch, capsys):
    """An empty section because the board is down looks exactly like an empty
    section because there is no work. Only one of those is worth acting on."""
    _wire(qbnext, monkeypatch, [], errors={"board": "URLError: refused"})
    monkeypatch.setattr(sys, "argv", ["qb-next"])

    code = qbnext.main()

    assert code == 2
    assert "! board unavailable: URLError: refused" in capsys.readouterr().out


def test_json_carries_the_join_rather_than_the_rendering(qbnext, monkeypatch, capsys):
    _wire(qbnext, monkeypatch, [_item(1, 44)], blocked={44: [55]})
    monkeypatch.setattr(sys, "argv", ["qb-next", "--json"])

    assert qbnext.main() == 0
    data = json.loads(capsys.readouterr().out)
    assert data["items"][0]["blocked_by_github"] == [55]


# ---- stubs ------------------------------------------------------------------


class _Run:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _gh_returning(payload):
    class _Subprocess:
        @staticmethod
        def run(*_a, **_k):
            return _Run(0, json.dumps(payload))
    return _Subprocess


def _gh_failing(stderr):
    class _Subprocess:
        @staticmethod
        def run(*_a, **_k):
            return _Run(1, "", stderr)
    return _Subprocess
