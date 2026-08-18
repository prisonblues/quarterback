"""Tests for qb-next, which answers "what can I pick up, and why" (#135 §3).

The command joins three sources that disagree in specific ways, and the joining
is the whole product — so the tests are about the seams rather than the
formatting. Nothing here touches the network: the board client and `gh` are both
substituted, because a test that needs a live board tests the board.

Two things are pinned harder than they look. The first is the DIRECTION of every
wrong answer: this command exists to say "nobody is on this", so every case where
a source is missing, truncated, case-shifted or malformed is asserted to produce
"I do not know" rather than "it is free". The second is the shape `gh` really
returns — `_GH_REAL` below is copied from an actual run, not written from the
same belief the code was.

Run: pytest harness/tests
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
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


def _stamp(minutes):
    """An ISO expiry `minutes` from now, negative for one already past."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


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


def test_a_gh_that_is_not_there_says_which_exception_and_what_it_said(qbdata, monkeypatch):
    """`! github unavailable: FileNotFoundError` names neither the binary nor the
    path. The class alone is the same line for a missing gh, a timeout and a
    parse failure, and this file's whole argument is that a failure nobody can
    read is worse than a loud one."""
    monkeypatch.setattr(qbdata, "subprocess", _gh_raising(
        FileNotFoundError(2, "No such file or directory: 'gh'")))
    blocked, error = qbdata.fetch_blocked("owner/repo")
    assert blocked == {}
    assert error.startswith("FileNotFoundError: ") and "gh" in error


# ---- what `gh` actually returns ---------------------------------------------


#: One record copied verbatim from `gh issue list --repo prisonblues/quarterback
#: --state all --limit 200 --json number,blockedBy` on gh 2.96.0. Recorded rather
#: than written, because a stub written from the same belief as the code cannot
#: contradict it — which is the one thing a test of an external shape is for.
_GH_REAL = [{
    "number": 141,
    "blockedBy": {
        "nodes": [{
            "id": "I_kwDOTORwf88AAAABM6ZXiQ",
            "number": 96,
            "state": "CLOSED",
            "title": "The pre-land gate is prose in one skill and absent from the other",
            "url": "https://github.com/prisonblues/quarterback/issues/96",
        }],
        "totalCount": 1,
    },
}, {
    "number": 186,
    "blockedBy": {"nodes": [], "totalCount": 0},
}]


def test_the_recorded_shape_gh_really_returns_is_read_correctly(qbdata, monkeypatch):
    monkeypatch.setattr(qbdata, "subprocess", _gh_returning(_GH_REAL))
    assert qbdata.fetch_blocked("owner/repo") == ({}, None)


def test_the_gh_command_asks_for_the_repo_the_state_and_the_two_fields(qbdata, monkeypatch):
    """The argv is the contract with a tool nobody here controls. A renamed flag
    or a dropped `--json` field makes production return no blocker data at all,
    which reads as every issue unblocked — so the call itself is asserted."""
    seen = {}
    monkeypatch.setattr(qbdata, "subprocess", _gh_recording(seen, []))

    qbdata.fetch_blocked("owner/repo")

    argv = seen["argv"]
    assert argv[:3] == ["gh", "issue", "list"]
    assert argv[argv.index("--repo") + 1] == "owner/repo"
    assert argv[argv.index("--state") + 1] == "open"
    assert argv[argv.index("--json") + 1] == "number,blockedBy"
    assert int(argv[argv.index("--limit") + 1]) == qbdata.ISSUE_LIMIT


def test_a_blocked_by_list_is_read_as_well_as_a_connection(qbdata, monkeypatch):
    """`blockedBy` is a GraphQL connection today. Read strictly, a bare list
    would raise AttributeError inside the fetch and come back as an EMPTY map —
    i.e. every issue in the repo reported unblocked, off one shape change."""
    payload = [{"number": 5, "blockedBy": [{"number": 6, "state": "OPEN"}]}]
    monkeypatch.setattr(qbdata, "subprocess", _gh_returning(payload))
    assert qbdata.fetch_blocked("owner/repo") == ({5: [6]}, None)


def test_a_null_nodes_does_not_take_the_whole_repo_down_with_it(qbdata, monkeypatch):
    """`{"nodes": null}` has the key, so a `.get(..., [])` default cannot fire and
    the comprehension iterates None. That raised inside the fetch, which reported
    the WHOLE read as failed — one malformed record costing every issue's edges."""
    payload = [{"number": 7, "blockedBy": {"nodes": None}},
               _issue(8, [(9, "OPEN")])]
    monkeypatch.setattr(qbdata, "subprocess", _gh_returning(payload))
    assert qbdata.fetch_blocked("owner/repo") == ({8: [9]}, None)


def test_one_unreadable_record_is_counted_and_the_rest_are_kept(qbdata, monkeypatch):
    payload = [{"blockedBy": {"nodes": [{"number": 1, "state": "OPEN"}]}},   # no `number`
               _issue(8, [(9, "OPEN")])]
    monkeypatch.setattr(qbdata, "subprocess", _gh_returning(payload))
    blocked, error = qbdata.fetch_blocked("owner/repo")
    assert blocked == {8: [9]}
    assert "1 issue record(s)" in error


def test_a_lowercase_state_still_blocks(qbdata, monkeypatch):
    """A strict `== "OPEN"` would not fail on a case change: it would match
    nothing, report every issue unblocked, and raise no error at all. That is the
    one failure here with nothing on screen to suggest it happened."""
    monkeypatch.setattr(qbdata, "subprocess", _gh_returning([_issue(3, [(4, "open")])]))
    assert qbdata.fetch_blocked("owner/repo")[0] == {3: [4]}


def test_a_full_page_of_issues_says_it_is_only_a_page(qbdata, monkeypatch):
    """An issue past the cap has no edges in the map, and no edges reads as
    unblocked. The cap is a fact about the read, not about the repo."""
    monkeypatch.setattr(qbdata, "subprocess",
                        _gh_returning([_issue(n) for n in range(qbdata.ISSUE_LIMIT)]))
    blocked, error = qbdata.fetch_blocked("owner/repo")
    assert blocked == {}
    assert "showing the first" in error


def test_a_full_page_of_prs_says_it_is_only_a_page(qbdata, monkeypatch):
    """`OPEN PRS (100)` on a repo with 140 of them is a wrong count, not a short
    list — and a JSON consumer has no other way to tell."""
    monkeypatch.setattr(qbdata, "subprocess",
                        _gh_returning([{"number": n} for n in range(qbdata.PR_LIMIT)]))
    prs, error = qbdata.fetch_prs("owner/repo")
    assert len(prs) == qbdata.PR_LIMIT
    assert "showing the first" in error


def test_fetch_prs_asks_about_the_repo_it_was_given(qbdata, monkeypatch):
    """The real function, not a stub of it: `--repo` used to be a constant read
    inside, so the heading named one repo and the rows came from another."""
    seen = {}
    monkeypatch.setattr(qbdata, "subprocess", _gh_recording(seen, []))
    assert qbdata.fetch_prs("owner/other") == ([], None)
    assert seen["argv"][seen["argv"].index("--repo") + 1] == "owner/other"


# ---- the board client -------------------------------------------------------


class _FakeClient:
    """A BoardClient with the HTTP removed and the paths recorded."""

    def __init__(self, answer=None, raises=None):
        self.paths, self.answer, self.raises = [], answer if answer is not None else {}, raises

    def get(self, path):
        self.paths.append(path)
        if self.raises:
            raise self.raises
        return self.answer

    def plan(self, repo=None):
        # The real URL construction is asserted separately; here the point is
        # what `fetch_plan` does with what comes back.
        return self.get("/plan")


def test_the_plan_url_carries_the_repo_as_a_query_parameter(qbdata):
    """The repo reaches the board as `?repo=owner/repo` and nothing else. A
    stubbed fetch_plan can only prove the argument arrived at a mock; this is the
    only test that would catch a malformed request."""
    fake = _FakeClient({"items": []})
    qbdata.BoardClient.plan(fake, "owner/repo")
    qbdata.BoardClient.plan(fake, None)
    assert fake.paths == ["/plan?repo=owner%2Frepo", "/plan"]


def test_the_claims_url_asks_for_the_maximum_page(qbdata):
    """The endpoint's own default is 100. A claim past the page is not a shorter
    list, it is a claim this command cannot see — and an unseen claim is the
    thing it exists to report."""
    fake = _FakeClient({"claims": []})
    qbdata.BoardClient.claims(fake)
    assert fake.paths == [f"/claims?limit={qbdata.CLAIM_LIMIT}"]


def test_fetch_plan_keeps_its_defaults_for_the_keys_a_response_omits(qbdata):
    """`update` is the merge, so a board that answers with half the object leaves
    the other half at the defaults rather than at KeyError."""
    got = qbdata.fetch_plan(_FakeClient({"items": [{"rank": 1}]}), "owner/repo")
    assert got == {"items": [{"rank": 1}], "next": None, "counts": {}, "error": None}


def test_fetch_plan_lets_the_boards_own_error_through(qbdata):
    got = qbdata.fetch_plan(_FakeClient({"error": "that scope is not yours"}), "owner/repo")
    assert got["error"] == "that scope is not yours"


def test_fetch_plan_reports_a_dead_board_rather_than_raising(qbdata):
    got = qbdata.fetch_plan(_FakeClient(raises=OSError("connection refused")), "owner/repo")
    assert got["items"] == [] and got["error"] == "OSError: connection refused"


def test_fetch_plan_refuses_a_response_that_is_not_an_object(qbdata):
    """`update` on a list raises, and the raise would be caught as `ValueError`
    somewhere with no clue that the board answered with the wrong type."""
    got = qbdata.fetch_plan(_FakeClient(["surprise"]), "owner/repo")
    assert "not an object" in got["error"]


# ---- display text is not a terminal instruction -----------------------------


def test_a_control_character_in_remote_text_never_reaches_the_terminal(qbdata):
    """A title, a holder and a `gh` error all come from somewhere else, and a
    terminal reads an ESC in any of them as an instruction — enough to redraw a
    section header or hide the line under it."""
    assert qbdata.plain("a\x1b[2Jb\nc\x07") == "a[2Jb c"
    assert qbdata.plain("one\ttwo\rthree") == "one two three", "a tab is a space, not nothing"
    assert qbdata.clip("x\x1b[31my", 40) == "x[31my"
    assert qbdata.plain(None) == "" and qbdata.plain(44) == "44"


# ---- the ref on a plan item -------------------------------------------------


@pytest.mark.parametrize("value,want", [("44", 44), ("#44", 44), (44, 44)])
def test_an_issue_ref_is_read_whichever_way_it_is_written(qbnext, value, want):
    assert qbnext._issue_of({"ref": {"kind": "issue", "value": value}}) == want


@pytest.mark.parametrize("ref", [
    {"kind": "pr", "value": "187"},     # a PR is not an issue and must not be looked up as one
    {"kind": "issue", "value": "abc"},
    {"kind": "issue", "value": "##44"},  # two hashes is not a spelling of anything
    {"kind": "issue", "value": "-44"},   # nor is a negative issue number
    {"kind": "issue", "value": None},
    {},
])
def test_anything_that_is_not_an_issue_number_reads_as_none(qbnext, ref):
    assert qbnext._issue_of({"ref": ref}) is None


@pytest.mark.parametrize("ref", [44, "44", [44]])
def test_a_ref_that_is_not_an_object_does_not_crash_the_command(qbnext, ref):
    """`ref.get("kind")` on an int raises AttributeError from inside collect,
    where the whole partial answer is discarded as one generic failure."""
    assert qbnext._issue_of({"ref": ref}) is None


# ---- how a blocker is written -----------------------------------------------


@pytest.mark.parametrize("blocker,want", [
    ({"ref": "44", "repo": "owner/repo"}, "#44"),
    ({"ref": "#44", "repo": "owner/repo"}, "#44"),                # not `##44`
    ({"ref": {"kind": "issue", "value": "44"}}, "#44"),           # not a dict repr
    ({"ref": None, "title": "a plan item with no issue"}, "a plan item with no issue"),
    ({"ref": None, "title": ""}, "an item with no ref"),
    ({"ref": "44", "repo": "owner/other"}, "owner/other#44"),     # somebody else's 44
    ({"ref": "44", "repo": "OWNER/Repo"}, "#44"),                 # the same repo, shouted
    ("44", "44"),
])
def test_every_shape_a_blocker_arrives_in_is_written_the_same_way(qbnext, blocker, want):
    """`/plan` sends `{"item_id", "title", "ref", "repo"}` with `ref` unprefixed
    and None for an item that refs nothing. Formatted raw, those printed `#None`,
    a dict repr and `##44` — the last of which also silently defeats the dedup
    against GitHub's edges, because those format as `#44`."""
    assert qbnext._blocker_label(blocker, "owner/repo") == want


# ---- the join ---------------------------------------------------------------


def _item(rank, issue, claim=None, blocked_by=(), title=None):
    return {"rank": rank, "title": title or f"item {rank}", "state": "open", "phase": "free",
            "ref": {"kind": "issue", "value": str(issue)},
            "claim": claim, "blocked_by": [{"ref": b} for b in blocked_by]}


def _wire(qbnext, monkeypatch, items, claims=(), blocked=None, prs=(), errors=None, nxt=None):
    errors = errors or {}

    def board(_client):
        return {"claims": list(claims), "error": errors.get("board")}

    def plan(_client, _repo):
        return {"items": list(items), "next": nxt, "counts": {}, "error": errors.get("plan")}

    monkeypatch.setattr(qbnext, "board_client", lambda: (object(), object()))
    monkeypatch.setattr(qbnext, "fetch_board", board)
    monkeypatch.setattr(qbnext, "fetch_plan", plan)
    monkeypatch.setattr(qbnext, "fetch_prs", lambda _r: (list(prs), errors.get("prs")))
    monkeypatch.setattr(qbnext, "fetch_blocked", lambda _r: (blocked or {}, errors.get("github")))


def test_a_github_edge_blocks_an_item_the_plan_does_not_know_about(qbnext, monkeypatch):
    """The two graphs are not the same graph. The plan holds dependencies an
    agent recorded; GitHub holds the ones on the issues. Either blocks."""
    _wire(qbnext, monkeypatch, [_item(1, 80)], blocked={80: [94, 101]})
    data = qbnext.collect("owner/repo")
    assert data["items"][0]["blocked_by_github"] == [94, 101]
    assert "#80    waits on #94, #101" in qbnext.render(data, 5)


def test_a_dependency_both_graphs_hold_is_printed_once(qbnext, monkeypatch):
    """The plan's `44` and GitHub's `44` are the same blocker. Printed from two
    sources with two spellings, the line said `waits on #44, ##44`."""
    _wire(qbnext, monkeypatch, [_item(1, 80, blocked_by=["#44", "51"])], blocked={80: [44]})
    line = next(ln for ln in qbnext.render(qbnext.collect("owner/repo"), 5).splitlines()
                if "waits on" in ln)
    assert line.split("waits on ")[1] == "#44, #51"


def test_a_plan_only_dependency_is_named_even_with_github_silent(qbnext, monkeypatch):
    _wire(qbnext, monkeypatch, [_item(1, 80, blocked_by=["44"])])
    assert "waits on #44" in qbnext.render(qbnext.collect("owner/repo"), 5)


def test_a_plain_claim_still_shows_the_item_as_held(qbnext, monkeypatch):
    """A claim taken with `claim` rather than `plan_claim` does not surface on
    the plan item — #172. Until that is fixed, falling back to /claims is the
    difference between "free" and "somebody is already on this"."""
    _wire(qbnext, monkeypatch, [_item(1, 163)],
          claims=[{"key": "owner/repo#163", "holder": "zeus/someone", "expires": None}])
    data = qbnext.collect("owner/repo")
    assert data["items"][0]["claim"]["holder"] == "zeus/someone"
    assert "→ #163" not in qbnext.render(data, 5), "a held item must not be offered as free"


def test_another_repos_claim_on_the_same_number_does_not_hold_this_one(qbnext, monkeypatch):
    """#163 exists in every repo the fleet touches. Joined on the trailing number
    alone, one repo's claim marks every repo's — and the next seat walks past the
    one issue it should have taken."""
    _wire(qbnext, monkeypatch, [_item(1, 163)],
          claims=[{"key": "other/repo#163", "holder": "zeus/someone", "expires": None}])
    data = qbnext.collect("owner/repo")
    assert data["items"][0]["claim"] is None
    assert "→ #163" in qbnext.render(data, 5)


@pytest.mark.parametrize("key", ["owner/repo", "owner/repo#", "owner/repo#abc", "163", None])
def test_a_claim_key_that_is_not_a_repo_and_a_number_is_skipped(qbnext, monkeypatch, key):
    """`.get("key", "")` returns None for an explicit `{"key": None}` — the
    default only fires for an absent key — and `.rpartition` on None took the
    whole command down with an AttributeError."""
    _wire(qbnext, monkeypatch, [_item(1, 163)],
          claims=[{"key": key, "holder": "zeus/someone", "expires": None}])
    assert qbnext.collect("owner/repo")["items"][0]["claim"] is None


def test_a_claim_written_in_a_different_case_still_holds_the_item(qbnext, monkeypatch):
    """GitHub repo names are case-insensitive; a claim key is a string. `--repo
    PrisonBlues/Quarterback` queried the plan and GitHub happily and matched no
    claim at all, so held work came back offered as free."""
    _wire(qbnext, monkeypatch, [_item(1, 163)],
          claims=[{"key": "owner/repo#163", "holder": "zeus/someone", "expires": None}])
    assert qbnext.collect("Owner/Repo")["items"][0]["claim"]["holder"] == "zeus/someone"


def test_the_first_claim_on_an_issue_is_the_one_reported(qbnext, monkeypatch):
    """/claims is ordered newest first, so first-wins is most-recent-wins."""
    _wire(qbnext, monkeypatch, [_item(1, 163)],
          claims=[{"key": "owner/repo#163", "holder": "zeus/first", "expires": None},
                  {"key": "owner/repo#163", "holder": "zeus/second", "expires": None}])
    assert qbnext.collect("owner/repo")["items"][0]["claim"]["holder"] == "zeus/first"


def test_a_claim_that_has_already_expired_does_not_hold_anything(qbnext, monkeypatch):
    """The board prunes these — `/claims` filters `expires_at > now` on the way
    past — so this should never fire. It is checked anyway because the direction
    is not symmetric: a stale claim hides work that is genuinely free, and
    `--repo` can point at a board older than that contract."""
    _wire(qbnext, monkeypatch, [_item(1, 163)],
          claims=[{"key": "owner/repo#163", "holder": "zeus/gone", "expires": _stamp(-90)}])
    data = qbnext.collect("owner/repo")
    assert data["items"][0]["claim"] is None
    assert "→ #163" in qbnext.render(data, 5)


def test_an_expired_claim_on_the_item_itself_is_ignored_too(qbnext, monkeypatch):
    _wire(qbnext, monkeypatch,
          [_item(1, 163, claim={"holder": "zeus/gone", "expires": _stamp(-5)})])
    assert qbnext.collect("owner/repo")["items"][0]["claim"] is None


def test_held_and_blocked_items_are_never_offered_as_free(qbnext, monkeypatch):
    _wire(qbnext, monkeypatch,
          [_item(1, 10, claim={"holder": "x", "expires": None}),
           _item(2, 20, blocked_by=["30"]),
           _item(3, 40)],
          blocked={})
    free = qbnext._free_lines(qbnext.collect("owner/repo")["items"], 5)
    assert len(free) == 1 and "#40" in free[0]


def test_a_malformed_plan_item_is_skipped_rather_than_fatal(qbnext, monkeypatch):
    """A schema drift used to raise inside the join, where `main` discards the
    three answers that DID arrive and prints one generic line."""
    _wire(qbnext, monkeypatch, ["not an item", None, _item(1, 40)])
    assert [i["issue"] for i in qbnext.collect("owner/repo")["items"]] == [40]


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


def test_an_item_that_is_both_held_and_blocked_is_counted_as_both(qbnext, monkeypatch):
    """Counting it as held alone made the header's word "blocked" quietly mean
    "blocked and unheld", so a plan reading `1 held, 0 blocked` could have a live
    dependency sitting in it."""
    _wire(qbnext, monkeypatch,
          [_item(1, 10, claim={"holder": "x", "expires": None}, blocked_by=["99"])])
    out = qbnext.render(qbnext.collect("owner/repo"), 5)
    assert "PLAN — 1 open, 1 held, 1 blocked" in out
    assert "waits on #99 (and held)" in out


def test_every_source_is_asked_about_the_repo_that_was_requested(qbnext, monkeypatch):
    """--repo used to change the plan and the heading while the PRs kept coming
    from the hardcoded default, so the output named one repo and listed
    another's work. Same class as #176: a repo that is assumed rather than
    passed."""
    asked = {}

    def plan(_client, repo):
        asked["plan"] = repo
        return {"items": [], "next": None, "counts": {}, "error": None}

    def prs(repo):
        asked["prs"] = repo
        return [], None

    def blocked(repo):
        asked["blocked"] = repo
        return {}, None

    monkeypatch.setattr(qbnext, "board_client", lambda: (object(), object()))
    monkeypatch.setattr(qbnext, "fetch_board", lambda _c: {"claims": [], "error": None})
    monkeypatch.setattr(qbnext, "fetch_plan", plan)
    monkeypatch.setattr(qbnext, "fetch_prs", prs)
    monkeypatch.setattr(qbnext, "fetch_blocked", blocked)

    qbnext.collect("owner/other")

    assert asked == {"plan": "owner/other", "prs": "owner/other", "blocked": "owner/other"}


# ---- the board's own answer -------------------------------------------------


def test_the_boards_next_is_printed_beside_the_list_this_command_recomputes(qbnext, monkeypatch):
    """`/plan` computes `next` from the dependencies it holds; the list adds
    GitHub's edges and claims taken outside the plan, so the two can differ — and
    the board's is the one another agent calling `plan_read` was handed."""
    _wire(qbnext, monkeypatch, [_item(1, 44)],
          nxt={"ref": {"kind": "issue", "value": "44"}, "title": "the board's pick"})
    out = qbnext.render(qbnext.collect("owner/repo"), 5)
    assert "the board's next: #44 the board's pick" in out


def test_a_board_with_nothing_free_says_so_rather_than_printing_none(qbnext, monkeypatch):
    _wire(qbnext, monkeypatch, [_item(1, 44)], nxt=None)
    assert "the board's next: nothing" in qbnext.render(qbnext.collect("owner/repo"), 5)


# ---- the PR section ---------------------------------------------------------


def _pr(number, checks=(), draft=False, title="a pull request"):
    return {"number": number, "title": title, "isDraft": draft,
            "statusCheckRollup": [{"conclusion": c} for c in checks]}


@pytest.mark.parametrize("checks,glyph,state", [
    (["SUCCESS", "SKIPPED"], "✓", "ready"),
    (["SUCCESS", "FAILURE"], "✗", "broken"),
    (["SUCCESS", None], "◐", "running"),
    ([], "·", "unchecked"),
    (["STALE"], "?", "unclear"),
])
def test_each_check_state_prints_the_reason_it_is_that_state(qbnext, checks, glyph, state):
    """The glyph is the thing you can already see in `gh pr list`; the words
    after it are what saves a second call to find out what it means."""
    line = qbnext._pr_lines([_pr(7, checks)])[0]
    assert line.startswith(f"  {glyph} #7")
    assert f"{state} — " in line


def test_a_rollup_state_this_does_not_model_says_so_once(qbnext):
    """The `?` glyph and the unmapped-glyph fallback were two near-identical
    spellings of one message, which is one of them going stale."""
    assert qbnext.CI_REASON.get("?") is None
    assert "checks in a state this does not model" in qbnext._pr_lines([_pr(7, ["STALE"])])[0]


def test_a_draft_is_reported_as_a_draft_whatever_its_checks_say(qbnext):
    """Deliberate: whether a draft's checks are green is not the question anybody
    is asking of it, so the flag wins and the glyph still shows the truth."""
    line = qbnext._pr_lines([_pr(7, ["FAILURE"], draft=True)])[0]
    assert line.startswith("  ✗ #7") and "draft — marked draft" in line


def test_prs_are_listed_in_number_order(qbnext):
    numbers = [ln.split("#")[1].split()[0] for ln in qbnext._pr_lines([_pr(9), _pr(2), _pr(40)])]
    assert numbers == ["2", "9", "40"]


def test_a_pr_missing_the_field_it_is_sorted_on_does_not_crash_the_section(qbnext):
    """`gh` is always asked for `number`, so this is the belt — but a KeyError
    here loses the plan section too, and the plan is the point of the command."""
    assert len(qbnext._pr_lines([{"title": "no number"}, _pr(3)])) == 2


def test_a_long_pr_title_is_clipped_rather_than_wrapped(qbnext):
    line = qbnext._pr_lines([_pr(7, title="x" * 200)])[0]
    assert "…" in line
    assert "x" * 58 not in line, "a long title must not push the reason off the end"


# ---- the free list and its limit --------------------------------------------


def test_the_free_list_says_how_many_it_left_out(qbnext, monkeypatch):
    """Truncating under a header that has already counted the items is the same
    disagreement the header comment argues against — five shown of twelve, with
    nothing saying seven exist."""
    _wire(qbnext, monkeypatch, [_item(n, n * 10) for n in range(1, 13)])
    out = qbnext.render(qbnext.collect("owner/repo"), 5)
    assert out.count("  → #") == 5
    assert "… and 7 more free" in out


def test_a_limit_of_zero_prints_no_items_at_all(qbnext, monkeypatch):
    """The cap was tested after the append, so `--limit 0` printed one."""
    _wire(qbnext, monkeypatch, [_item(1, 10), _item(2, 20)])
    lines = qbnext._free_lines(qbnext.collect("owner/repo")["items"], 0)
    assert lines == ["  … and 2 more free — raise --limit to see them"]


def test_a_negative_limit_is_refused_at_the_flag(qbnext, monkeypatch):
    """`--limit -1` is not a shorter list, it is a question with no answer — and
    it used to print exactly one item."""
    monkeypatch.setattr(sys, "argv", ["qb-next", "--limit", "-1"])
    with pytest.raises(SystemExit) as exit_code:
        qbnext.main()
    assert exit_code.value.code == 2, "argparse owns 2, which is why a dead source is 3"


def test_an_empty_plan_does_not_claim_everything_is_held_or_blocked(qbnext, monkeypatch):
    _wire(qbnext, monkeypatch, [])
    assert "the plan has no open items" in qbnext.render(qbnext.collect("owner/repo"), 5)


# ---- the held section -------------------------------------------------------


def test_a_held_item_shows_who_has_it_and_for_how_much_longer(qbnext, monkeypatch):
    _wire(qbnext, monkeypatch,
          [_item(1, 10, claim={"holder": "zeus/f5ca7491", "expires": _stamp(31)})])
    out = qbnext.render(qbnext.collect("owner/repo"), 5)
    assert "#10    zeus/f5ca7491" in out and "30m left" in out


def test_a_claim_with_no_expiry_says_so_rather_than_guessing(qbnext, monkeypatch):
    _wire(qbnext, monkeypatch, [_item(1, 10, claim={"holder": "zeus/x", "expires": None})])
    assert "no expiry" in qbnext.render(qbnext.collect("owner/repo"), 5)


def test_a_claim_with_no_holder_prints_the_fallback_and_not_the_word_none(qbnext, monkeypatch):
    """`.get("holder", "?")` returns None for an explicit `{"holder": None}` —
    the default only fires when the key is absent."""
    _wire(qbnext, monkeypatch, [_item(1, 10, claim={"holder": None, "expires": None})])
    out = qbnext.render(qbnext.collect("owner/repo"), 5)
    assert "#10    ?" in out and "None" not in out


def test_a_holder_cannot_forge_a_section_header(qbnext, monkeypatch):
    """Every value on these lines came from somewhere else. An embedded newline
    or an ANSI escape in one of them rewrites what a terminal shows."""
    _wire(qbnext, monkeypatch,
          [_item(1, 10, claim={"holder": "x\nBLOCKED (9)\n\x1b[2J", "expires": None},
                 title="t\x1b[31m")])
    out = qbnext.render(qbnext.collect("owner/repo"), 5)
    assert "\x1b" not in out
    assert not any(ln.startswith("BLOCKED") for ln in out.splitlines()), \
        "a section header must come from the renderer, not from a claim"


# ---- a source that died -----------------------------------------------------


def test_a_dead_source_is_stated_loudly_and_changes_the_exit_code(qbnext, monkeypatch, capsys):
    """An empty section because the board is down looks exactly like an empty
    section because there is no work. Only one of those is worth acting on."""
    _wire(qbnext, monkeypatch, [], errors={"board": "URLError: refused"})
    monkeypatch.setattr(sys, "argv", ["qb-next"])

    code = qbnext.main()

    assert code == 3, "3, not 2: argparse spends 2 on a flag that does not exist"
    assert "! board: URLError: refused" in capsys.readouterr().out


def test_work_is_not_offered_as_free_when_the_claims_source_is_down(qbnext, monkeypatch):
    """The dangerous half of a partial answer. With /claims dead, an item with no
    claim on it is indistinguishable from one nobody has taken — and the command
    used to print the second, leaving the `!` line at the bottom to be noticed."""
    _wire(qbnext, monkeypatch, [_item(1, 44)], errors={"board": "URLError: refused"})
    data = qbnext.collect("owner/repo")
    out = qbnext.render(data, 5)

    assert data["items"][0]["unknown"] == ["claims"]
    assert "  → #44" not in out, "an unverifiable item must not be offered as free"
    assert "UNCERTAIN (1)" in out
    assert "a claim taken outside the plan would not be visible" in out


def test_a_capped_github_read_is_treated_the_same_as_a_dead_one(qbnext, monkeypatch):
    """An issue past `gh --limit` has no edges in the map, and no edges reads as
    unblocked. A cap is a fact about the read, so it hedges the same way a
    failure does rather than being quietly rounded down to "fine"."""
    _wire(qbnext, monkeypatch, [_item(1, 44)],
          errors={"github": "showing the first 600 open issues — there are more"})
    data = qbnext.collect("owner/repo")
    assert data["items"][0]["unknown"] == ["github"]
    assert "  → #44" not in qbnext.render(data, 5)


def test_work_is_not_offered_as_unblocked_when_github_is_down(qbnext, monkeypatch):
    _wire(qbnext, monkeypatch, [_item(1, 44)], errors={"github": "gh exit 1"})
    data = qbnext.collect("owner/repo")
    assert data["items"][0]["unknown"] == ["github"]
    assert "  → #44" not in qbnext.render(data, 5)


def test_an_item_the_plan_itself_claims_is_still_held_with_the_board_down(qbnext, monkeypatch):
    """The /claims fallback is what goes missing, not the item's own claim, so
    the answer for that item is known and does not need hedging."""
    _wire(qbnext, monkeypatch,
          [_item(1, 44, claim={"holder": "zeus/x", "expires": None})],
          errors={"board": "URLError: refused"})
    data = qbnext.collect("owner/repo")
    assert data["items"][0]["unknown"] == []
    assert "HELD (1)" in qbnext.render(data, 5)


def test_a_truncated_plan_page_is_reported_as_a_partial_answer(qbnext, monkeypatch):
    """The endpoint says `truncated` itself rather than leaving it to be worked
    out by comparing lengths; passing it on keeps that true one hop further."""
    monkeypatch.setattr(qbnext, "board_client", lambda: (object(), object()))
    monkeypatch.setattr(qbnext, "fetch_board", lambda _c: {"claims": [], "error": None})
    monkeypatch.setattr(qbnext, "fetch_plan", lambda _c, _r: {
        "items": [], "next": None, "counts": {}, "error": None, "truncated": True})
    monkeypatch.setattr(qbnext, "fetch_prs", lambda _r: ([], None))
    monkeypatch.setattr(qbnext, "fetch_blocked", lambda _r: ({}, None))
    assert "longer than one page" in qbnext.collect("owner/repo")["errors"]["plan"]


def test_a_fetch_that_raises_anyway_loses_one_source_and_not_the_answer(qbnext, monkeypatch):
    """The fetches are written not to raise. This is the belt: an exception past
    them used to discard the three answers that did arrive."""
    def explode(_repo):
        raise RuntimeError("the belt")

    _wire(qbnext, monkeypatch, [_item(1, 44)])
    monkeypatch.setattr(qbnext, "fetch_blocked", explode)

    data = qbnext.collect("owner/repo")

    assert data["errors"]["github"] == "RuntimeError: the belt"
    assert data["items"][0]["issue"] == 44


def test_no_board_configured_is_a_sentence_on_stderr_and_exit_one(qbnext, monkeypatch, capsys):
    """A config error is a message for a person, not a section of a report —
    which is why `collect` does not catch it and `main` does."""
    def no_board():
        raise RuntimeError("no board configured (QUARTERBACK_BASE_URL is unset)")

    monkeypatch.setattr(qbnext, "board_client", no_board)
    monkeypatch.setattr(sys, "argv", ["qb-next"])

    assert qbnext.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no board configured" in captured.err


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


def _gh_raising(exc):
    class _Subprocess:
        @staticmethod
        def run(*_a, **_k):
            raise exc
    return _Subprocess


def _gh_recording(seen, payload):
    class _Subprocess:
        @staticmethod
        def run(argv, *_a, **_k):
            seen["argv"] = argv
            return _Run(0, json.dumps(payload))
    return _Subprocess
