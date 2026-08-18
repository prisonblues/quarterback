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
    assert int(argv[argv.index("--limit") + 1]) == qbdata.ISSUE_LIMIT + 1, \
        "one more than the cap, because the row nobody displays is the truncation evidence"


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


@pytest.mark.parametrize("junk", [None, "an issue", 44, ["nested"]])
def test_a_record_that_is_not_an_object_at_all_costs_only_itself(qbdata, monkeypatch, junk):
    """The per-record guard named `(KeyError, TypeError, ValueError)`, and a
    record that is not a dict raises AttributeError on the first `.get`. That
    escaped to the outer handler, which returns `({}, error)` — so one junk
    entry emptied the ENTIRE repo's blocker map, which reads as everything
    unblocked. A guard has to cover its own edges."""
    monkeypatch.setattr(qbdata, "subprocess",
                        _gh_returning([junk, _issue(8, [(9, "OPEN")])]))
    blocked, error = qbdata.fetch_blocked("owner/repo")
    assert blocked == {8: [9]}, "the good record must survive the bad one"
    assert "1 issue record(s)" in error


@pytest.mark.parametrize("field", ["surprise", 44, {"nodes": "surprise"}, {"nodes": 7}])
def test_a_blocked_by_in_a_shape_this_cannot_read_is_said_out_loud(qbdata, monkeypatch, field):
    """`[]` and "I could not read this" are different answers, and only one of
    them is safe: a genuinely blocked issue reported as unblocked with nothing
    on screen to suggest the record was junk is how somebody picks up work that
    is waiting on another."""
    monkeypatch.setattr(qbdata, "subprocess", _gh_returning(
        [{"number": 7, "blockedBy": field}, _issue(8, [(9, "OPEN")])]))
    blocked, error = qbdata.fetch_blocked("owner/repo")
    assert blocked == {8: [9]}
    assert "1 issue record(s)" in error


def test_an_issue_with_no_blocked_by_key_at_all_is_not_called_unreadable(qbdata, monkeypatch):
    """An issue with no edges is the normal case, not a malformed record — and a
    `bad` count that fires on every ordinary issue is a count nobody reads."""
    monkeypatch.setattr(qbdata, "subprocess", _gh_returning([{"number": 7}]))
    assert qbdata.fetch_blocked("owner/repo") == ({}, None)


def test_one_unreadable_edge_does_not_cost_the_issue_its_other_blockers(qbdata, monkeypatch):
    """`sorted(b["number"] for b in ...)` is a generator: the first raise aborts
    it, so the surrounding except dropped the whole issue — including the
    well-formed OPEN blocker standing beside the bad one. The item then read as
    unblocked, which is the direction that costs somebody an afternoon."""
    payload = [{"number": 7, "blockedBy": {"nodes": [
        {"state": "OPEN"},                        # no `number`
        {"number": 9, "state": "OPEN"},
    ]}}]
    monkeypatch.setattr(qbdata, "subprocess", _gh_returning(payload))
    blocked, error = qbdata.fetch_blocked("owner/repo")
    assert blocked == {7: [9]}, "the readable OPEN blocker must survive its neighbour"
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
                        _gh_returning([_issue(n) for n in range(1, qbdata.ISSUE_LIMIT + 2)]))
    blocked, error = qbdata.fetch_blocked("owner/repo")
    assert blocked == {}
    assert "showing the first" in error


def test_a_repo_sitting_exactly_on_the_issue_cap_is_not_called_partial(qbdata, monkeypatch):
    """`len(rows) >= limit` cannot tell "exactly this many" from "and more", so a
    repo with exactly 600 open issues got a false partial-answer error on EVERY
    run — which hedges every plan item as UNCERTAIN. The fetch asks for one more
    than it reports on, and the row nobody sees is what settles it."""
    monkeypatch.setattr(qbdata, "subprocess",
                        _gh_returning([_issue(n) for n in range(1, qbdata.ISSUE_LIMIT + 1)]))
    assert qbdata.fetch_blocked("owner/repo") == ({}, None)


def test_a_full_page_of_prs_says_it_is_only_a_page(qbdata, monkeypatch):
    """`OPEN PRS (100)` on a repo with 140 of them is a wrong count, not a short
    list — and a JSON consumer has no other way to tell."""
    monkeypatch.setattr(qbdata, "subprocess",
                        _gh_returning([{"number": n} for n in range(qbdata.PR_LIMIT + 1)]))
    prs, error = qbdata.fetch_prs("owner/repo")
    assert len(prs) == qbdata.PR_LIMIT, "the extra row is evidence, not a row to display"
    assert "showing the first" in error


def test_a_repo_sitting_exactly_on_the_pr_cap_is_not_called_partial(qbdata, monkeypatch):
    monkeypatch.setattr(qbdata, "subprocess",
                        _gh_returning([{"number": n} for n in range(qbdata.PR_LIMIT)]))
    prs, error = qbdata.fetch_prs("owner/repo")
    assert len(prs) == qbdata.PR_LIMIT and error is None


def test_a_gh_pr_list_that_is_not_there_says_which_exception_and_what_it_said(qbdata, monkeypatch):
    """`fetch_blocked` got this error format and a test; `fetch_prs` got the same
    format and no test, so the two could drift apart unnoticed."""
    monkeypatch.setattr(qbdata, "subprocess", _gh_raising(
        FileNotFoundError(2, "No such file or directory: 'gh'")))
    prs, error = qbdata.fetch_prs("owner/repo")
    assert prs == []
    assert error.startswith("FileNotFoundError: ") and "gh" in error


def test_gh_pr_list_answering_with_something_other_than_a_list_is_refused(qbdata, monkeypatch):
    """A JSON object where the array should be is not zero PRs. Reading it as an
    empty list prints `OPEN PRS (0)` off a read that failed."""
    monkeypatch.setattr(qbdata, "subprocess", _gh_returning({"message": "not found"}))
    prs, error = qbdata.fetch_prs("owner/repo")
    assert prs == [] and "not a list of PRs" in error


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
    """The endpoint's own default is 100 and its maximum is 1000. A claim past
    the page is not a shorter list, it is a claim this command cannot see — and
    an unseen claim is the thing it exists to report. The maximum is asked for
    precisely so it is one MORE than the 999 reported on: a page that comes back
    full is then unambiguous evidence of more, rather than a repo that happens
    to sit on the number."""
    fake = _FakeClient({"claims": []})
    qbdata.BoardClient.claims(fake)
    assert fake.paths == [f"/claims?limit={qbdata.CLAIM_LIMIT + 1}"]
    assert qbdata.CLAIM_LIMIT + 1 == 1000, "the endpoint refuses a larger limit"


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


@pytest.mark.parametrize("items", [{"one": 1}, "surprise", 44])
def test_fetch_plan_refuses_an_items_field_that_is_not_a_list(qbdata, items):
    """Only the TOP level was type-checked. `update` then copied a dict or a
    string into `items`, and the caller's `plan.get("items") or []` cannot fall
    back from a truthy one — so the join iterated the wrong object, every entry
    failed its isinstance guard, and an unusable plan source came out as a plan
    with nothing open in it and an exit code of 0."""
    got = qbdata.fetch_plan(_FakeClient({"items": items}), "owner/repo")
    assert got["items"] == []
    assert "for 'items', not a list" in got["error"]


def test_fetch_plan_refuses_a_counts_field_that_is_not_an_object(qbdata):
    got = qbdata.fetch_plan(_FakeClient({"items": [], "counts": ["surprise"]}), "owner/repo")
    assert got["counts"] == {} and "for 'counts', not an object" in got["error"]


class _SplitClient:
    """A client whose two GETs succeed and fail independently."""

    def __init__(self, active=None, claims=None, active_raises=None, claims_raises=None):
        self.active_answer, self.claims_answer = active, claims
        self.active_raises, self.claims_raises = active_raises, claims_raises

    def active(self):
        if self.active_raises:
            raise self.active_raises
        return self.active_answer

    def claims(self, limit=None):
        if self.claims_raises:
            raise self.claims_raises
        return self.claims_answer


def test_a_dead_active_does_not_throw_away_a_healthy_claims_answer(qbdata):
    """One try block around both GETs meant a failing /active jumped past the
    claims call before it was ever made. qb-next reads neither `agents` nor
    `subagents`, so it then hedged every plan item as unverifiable over a
    section it does not print."""
    got = qbdata.fetch_board(_SplitClient(
        active_raises=OSError("connection refused"),
        claims={"claims": [{"key": "owner/repo#1", "holder": "zeus/x"}]}))
    assert [c["key"] for c in got["claims"]] == ["owner/repo#1"]
    assert "OSError: connection refused" in got["error"]


def test_both_board_gets_failing_names_both_and_not_just_the_first(qbdata):
    """One line naming one of two dead sources sends somebody to check the half
    that was fine."""
    got = qbdata.fetch_board(_SplitClient(active_raises=OSError("active is down"),
                                          claims_raises=TimeoutError("claims timed out")))
    assert "active is down" in got["error"] and "claims timed out" in got["error"]


@pytest.mark.parametrize("answer,fragment", [
    ({"claims": {"one": {}}}, "for 'claims', not a list"),
    ({"claims": "surprise"}, "for 'claims', not a list"),
    (["surprise"], "/claims returned list, not an object"),
])
def test_an_unparseable_claims_answer_never_reads_as_nobody_has_claimed(
        qbdata, answer, fragment):
    """`rows = ....get("claims", []) or []` never fell back from a truthy
    non-list: the isinstance comprehension filtered the wrong object away to
    nothing and `truncated` measured that object's own len. The result was
    claims=[] with error=None — an unparseable source reading exactly like
    "nobody has claimed anything", which is the one answer this must not
    invent."""
    got = qbdata.fetch_board(_SplitClient(active={}, claims=answer))
    assert got["claims"] == []
    assert fragment in got["error"], "silence here is indistinguishable from an empty board"


def test_a_full_page_of_claims_says_it_is_only_a_page(qbdata):
    """The most dangerous of the three caps, because what a claims page hides is
    a HELD item — and an unseen claim reads as free work. The URL test proves
    the maximum is asked for; this proves a full page reaches the caller as an
    error rather than as a shorter list."""
    rows = [{"key": f"owner/repo#{n}", "holder": "zeus/x"}
            for n in range(1, qbdata.CLAIM_LIMIT + 2)]
    got = qbdata.fetch_board(_SplitClient(active={}, claims={"claims": rows}))
    assert "showing the first" in got["error"]
    assert len(got["claims"]) == qbdata.CLAIM_LIMIT


def test_a_board_sitting_exactly_on_the_claims_cap_is_not_called_partial(qbdata):
    rows = [{"key": f"owner/repo#{n}", "holder": "zeus/x"}
            for n in range(1, qbdata.CLAIM_LIMIT + 1)]
    got = qbdata.fetch_board(_SplitClient(active={}, claims={"claims": rows}))
    assert got["error"] is None and len(got["claims"]) == qbdata.CLAIM_LIMIT


# ---- display text is not a terminal instruction -----------------------------


def test_a_control_character_in_remote_text_never_reaches_the_terminal(qbdata):
    """A title, a holder and a `gh` error all come from somewhere else, and a
    terminal reads an ESC in any of them as an instruction — enough to redraw a
    section header or hide the line under it."""
    assert qbdata.plain("a\x1b[2Jb\nc\x07") == "a[2Jb c"
    assert qbdata.plain("one\ttwo\rthree") == "one two three", "a tab is a space, not nothing"
    assert qbdata.clip("x\x1b[31my", 40) == "x[31my"
    assert qbdata.plain(None) == "" and qbdata.plain(44) == "44"


@pytest.mark.parametrize("char", ["\x9b", "\x9d", "\x80", "\x9f"])
def test_a_c1_control_is_stripped_as_well_as_the_famous_c0_ones(qbdata, char):
    """U+009B is CSI: a terminal in an 8-bit mode acts on it exactly as it does
    on the two-character ESC-[, so a filter that stops at C0 stops one encoding
    short of the same thing it was written to block."""
    assert qbdata.plain(f"a{char}2Jb") == "a2Jb"


@pytest.mark.parametrize("char", ["‮", "‪", "⁦", "⁩", "‎", "؜"])
def test_a_bidi_override_cannot_reorder_what_is_rendered(qbdata, char):
    """These carry no escape sequence at all — they reorder the text a terminal
    draws, so a title can display in an order its characters are not in. The
    docstring calls this function "the one place remote text is sanitised", and
    that has to include the class that needs no ESC."""
    assert qbdata.plain(f"free{char}dlobkcolb") == "freedlobkcolb"


@pytest.mark.parametrize("char", ["\x0b", "\x0c", "\x85"])
def test_a_whitespace_control_becomes_a_space_rather_than_vanishing(qbdata, char):
    """The same argument the map already makes for \\n: deleting a line break
    JOINS the words either side of it, so "a\\vb" reading as the one word "ab"
    is the bug the comment above the map exists to argue against."""
    assert qbdata.plain(f"a{char}b") == "a b"


# ---- one client, four workers -----------------------------------------------


def test_one_board_client_is_safe_to_share_between_the_two_board_fetches(qbdata):
    """`collect` submits `fetch_board` and `fetch_plan` to a thread pool holding
    the SAME BoardClient, so "they share no state" was not true of the client.
    It is safe for a narrower reason, and this pins that reason: the client
    carries one immutable config and caches nothing, so every call builds its
    own request and two threads cannot see each other's.

    A cache added to `BoardClient` — a token, an opener, a session — makes this
    fail, which is the point of asserting it rather than believing it."""
    import concurrent.futures
    import threading

    seen = []
    lock = threading.Lock()

    class _Counting(qbdata.BoardClient):
        def get(self, path):
            with lock:
                seen.append(path)
            return {"claims": [], "agents": [], "subagents": [], "items": []}

    client = _Counting(qbdata.BoardConfig("https://board.invalid", "t", "host"))
    before = dict(vars(client))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        board = pool.submit(qbdata.fetch_board, client)
        plan = pool.submit(qbdata.fetch_plan, client, "owner/repo")
        assert board.result()["error"] is None
        assert plan.result()["error"] is None

    assert sorted(seen) == ["/active", "/claims?limit=1000", "/plan?repo=owner%2Frepo"]
    assert vars(client) == before, "a client that mutates under a fetch cannot be shared"


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
    {"kind": "issue", "value": "0"},     # GitHub has no issue 0
    {"kind": "issue", "value": "²"},     # isdigit() says yes; int() raises ValueError
    {"kind": "issue", "value": "４４"},   # isdigit() says yes; int() returns 44, silently
    {"kind": "issue", "value": "1" * 5000},   # past CPython's int-conversion digit limit
    {},
])
def test_anything_that_is_not_an_issue_number_reads_as_none(qbnext, ref):
    """`str.isdigit()` is not `int()`. A superscript raises ValueError from
    inside `collect`'s comprehension, where `main` discards the whole partial
    answer as one generic failure; a full-width digit string does not raise at
    all — it quietly becomes 44 and matches an issue nobody wrote."""
    assert qbnext._issue_of({"ref": ref}) is None


@pytest.mark.parametrize("value", ["##44", "²", "４４", "-44", "0", "abc", "1" * 5000])
def test_a_ref_means_the_same_thing_wherever_it_appears(qbnext, value):
    """The same malformed ref used to mean "issue 44" when it appeared as a
    BLOCKER on another item's line — `_blocker_label` stripped every leading `#`
    — and "no issue at all" when it was the item's own ref, which `_issue_of`
    parses strictly. One parse now, so the two cannot disagree."""
    assert qbnext._issue_of({"ref": {"kind": "issue", "value": value}}) is None
    label = qbnext._blocker_label({"ref": value, "title": "a title"}, "owner/repo")
    assert label == "a title", "not a number here either, so it is named by its title"


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
    ("44", "#44"),                                # a bare number, prefixed so the dedup works
    (44, "#44"),
    ("not a number", "not a number"),
    ({"ref": "##44", "title": "two hashes is not a spelling"}, "two hashes is not a spelling"),
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


def test_a_bare_number_blocker_dedups_against_githubs_own_edge(qbnext, monkeypatch):
    """A plan blocker that is not an object came back unprefixed — `44` — while
    GitHub's edge formats as `#44`, so the string-equality dedup missed and the
    line read "waits on #44, 44": one dependency printed as two."""
    _wire(qbnext, monkeypatch, [_item(1, 80)], blocked={80: [44]})
    data = qbnext.collect("owner/repo")
    data["items"][0]["blocked_by_plan"] = [qbnext._blocker_label("44", "owner/repo")]
    line = next(ln for ln in qbnext.render(data, 5).splitlines() if "waits on" in ln)
    assert line.split("waits on ")[1] == "#44"


@pytest.mark.parametrize("blocked_by", [{"item_id": "x"}, "44", 44])
def test_a_dependency_list_that_is_not_a_list_prints_no_phantom_blockers(
        qbnext, monkeypatch, blocked_by):
    """`item.get("blocked_by") or []` was iterated with no isinstance check,
    unlike every other field the join reads. A dict there walked its KEYS and
    printed `item_id` as a dependency that does not exist — and skipping it
    silently would be the other half of that mistake, so the item is marked
    unverifiable rather than quietly offered as free."""
    item = _item(1, 80)
    item["blocked_by"] = blocked_by
    _wire(qbnext, monkeypatch, [item])
    data = qbnext.collect("owner/repo")
    out = qbnext.render(data, 5)

    assert data["items"][0]["blocked_by_plan"] == []
    assert "unknown" in data["items"][0] and "blockers" in data["items"][0]["unknown"]
    assert "waits on" not in out and "item_id" not in out
    assert "  → #80" not in out, "a dependency list this cannot read is not an empty one"
    assert "dependency list is in a shape this cannot read" in out


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


@pytest.mark.parametrize("flag", ["released", "lapsed"])
def test_a_released_claim_on_the_item_does_not_hold_it(qbnext, monkeypatch, flag):
    """`fetch_board` filters `released` and `lapsed` off the /claims rows, but a
    claim attached to a plan ITEM never goes past that filter. Checking only the
    expiry meant a released-but-unexpired claim still read as live — work that
    is genuinely available, hidden."""
    _wire(qbnext, monkeypatch,
          [_item(1, 163, claim={"holder": "zeus/done", "expires": _stamp(60), flag: True})])
    data = qbnext.collect("owner/repo")
    assert data["items"][0]["claim"] is None
    assert "  → #163" in qbnext.render(data, 5)


def test_a_claim_with_an_unreadable_expiry_counts_as_live(qbnext):
    """Deliberate, and asserted because it is the asymmetry the whole file turns
    on: unreadable is not evidence of gone, and a claim wrongly dropped is work
    offered to a second agent."""
    assert qbnext._live({"expires": "not a timestamp"}) is True
    assert qbnext._live({"expires": None}) is True


def test_a_claim_expiring_this_very_minute_is_still_live(qbnext):
    """The boundary the docstring claims — `>= 0`, not `> 0`. A claim with zero
    whole minutes left has not expired, and rounding it down to gone is the
    direction that hands somebody work another agent is on."""
    assert qbnext._live({"expires": _stamp(0.5)}) is True
    assert qbnext._live({"expires": _stamp(-0.5)}) is False


def test_an_empty_claim_record_is_a_claim_and_not_an_absent_one(qbnext, monkeypatch):
    """`{}` where a claim should be passes `isinstance(own, dict) and _live(own)`
    — an empty dict has no expiry, so it reads as live — and is kept as the
    item's claim. But `{}` is FALSY, so every `if item["claim"]` treated it as no
    claim at all and the item went into the free list, under a header counting
    it as unheld. A claim this cannot read is still a claim."""
    _wire(qbnext, monkeypatch, [_item(1, 163, claim={})])
    data = qbnext.collect("owner/repo")
    out = qbnext.render(data, 5)

    assert data["items"][0]["claim"] == {}
    assert "  → #163" not in out, "a malformed-but-present claim must not read as free"
    assert "HELD (1)" in out and "PLAN — 1 open, 1 held, 0 blocked" in out
    assert "#163   ?" in out, "no holder in the record, so the fallback prints"


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


@pytest.mark.parametrize("number", [None, "seven", {"n": 1}, [], "0"])
def test_a_pr_number_that_is_present_and_unusable_does_not_crash_the_section(qbnext, number):
    """`key=lambda p: p.get("number", 0)` only defaults for an ABSENT key — the
    exact trap this file already tests for `claim.get("key")` and
    `claim.get("holder")`. An explicit `{"number": null}` put None into the sort
    and raised TypeError from `render`, which runs OUTSIDE `main`'s guard: a
    bare traceback in place of every section, over one PR record."""
    lines = qbnext._pr_lines([{"number": number, "title": "unusable"}, _pr(3)])
    assert len(lines) == 2
    assert lines[0].startswith("  · #3"), "a readable number sorts before an unreadable one"
    assert "—" in lines[1], "and the unreadable one says so the way a plan item does"


def test_a_pr_with_no_number_is_spelled_the_same_way_a_plan_item_is(qbnext, monkeypatch):
    """`#?` in one section and an em dash in the other are two spellings of
    "there is no reference here", which is one of them going stale."""
    _wire(qbnext, monkeypatch, [{"rank": 1, "title": "no ref", "state": "open", "ref": None}],
          prs=[{"title": "no number"}])
    out = qbnext.render(qbnext.collect("owner/repo"), 5)
    assert "#?" not in out
    assert out.count("—   ") >= 1


def test_the_open_prs_count_matches_the_rows_printed_under_it(qbnext):
    """The header counted `len(data["prs"])`, which includes records `_pr_lines`
    drops before printing — so a section headed "OPEN PRS (3)" could show two
    rows. A header disagreeing with the section under it is the thing the plan
    header's comment argues against."""
    data = {"repo": "owner/repo", "next": None, "items": [], "counts": {},
            "prs": [_pr(3), "not a pr", None], "errors": {}}
    out = qbnext.render(data, 5)
    assert "OPEN PRS (1)" in out
    assert len([ln for ln in out.splitlines() if ln.startswith("  · #")]) == 1


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


def test_a_dead_plan_source_never_says_the_plan_is_empty(qbnext, monkeypatch):
    """The hedging the UNCERTAIN mechanism does for claims and GitHub cannot
    reach a dead PLAN: it empties `items` outright, so there is nothing left to
    mark uncertain, and the renderer fell through to its ordinary empty-state
    text. The result was two confident sentences — "the plan has no open items"
    and "no open item is both free and unblocked" — sitting directly above a `!`
    line saying the plan had never been read."""
    _wire(qbnext, monkeypatch, [], errors={"plan": "URLError: refused"})
    out = qbnext.render(qbnext.collect("owner/repo"), 5)

    assert "the plan has no open items" not in out
    assert "no open item is both free and unblocked" not in out
    assert "PLAN — could not be read" in out
    assert "the board's next: unknown" in out
    assert "! plan: URLError: refused" in out


def test_a_plan_that_answered_with_an_error_hedges_the_items_it_did_send(qbnext, monkeypatch):
    """`out.update(got)` exists so an answered-but-refused board keeps its
    items — which means those items came off a source that reported a problem,
    and their `blocked_by` may be half of what it should be. They were printed
    as free and unblocked anyway."""
    _wire(qbnext, monkeypatch, [_item(1, 44)], errors={"plan": "that scope is not yours"})
    data = qbnext.collect("owner/repo")
    out = qbnext.render(data, 5)

    assert "plan" in data["items"][0]["unknown"]
    assert "  → #44" not in out
    assert "so what it did send may be partial" in out
    assert "PLAN — at least 1 open, at least 0 held, at least 0 blocked" in out, \
        "a payload that may be a subset of the truth undermines all three counts"


def test_a_capped_plan_page_counts_at_least_rather_than_hedging_every_item(qbnext, monkeypatch):
    """A cap and an error are different partial answers. The items a capped page
    DID send are whole — their dependencies and claims travel on them — so
    hedging every one would empty the free list for no reason. What the cap
    costs is the total, and the header says so instead."""
    monkeypatch.setattr(qbnext, "board_client", lambda: (object(), object()))
    monkeypatch.setattr(qbnext, "fetch_board", lambda _c: {"claims": [], "error": None})
    monkeypatch.setattr(qbnext, "fetch_plan", lambda _c, _r: {
        "items": [_item(1, 44)], "next": None, "counts": {}, "error": None, "truncated": True})
    monkeypatch.setattr(qbnext, "fetch_prs", lambda _r: ([], None))
    monkeypatch.setattr(qbnext, "fetch_blocked", lambda _r: ({}, None))

    data = qbnext.collect("owner/repo")
    out = qbnext.render(data, 5)

    assert data["items"][0]["unknown"] == []
    assert "PLAN — at least 1 open, 0 held, 0 blocked" in out
    assert "  → #44" in out
    assert "longer than one page" in out


def test_a_structured_error_from_the_board_does_not_take_the_answer_down(qbnext, monkeypatch):
    """`"; ".join(filter(None, [plan_error, ...]))` assumed `error` was a string.
    `fetch_plan`'s `update` will happily assign a structured one, and `join` then
    raised TypeError with all four answers already in hand."""
    monkeypatch.setattr(qbnext, "board_client", lambda: (object(), object()))
    monkeypatch.setattr(qbnext, "fetch_board", lambda _c: {"claims": [], "error": None})
    monkeypatch.setattr(qbnext, "fetch_plan", lambda _c, _r: {
        "items": [], "next": None, "counts": {},
        "error": {"code": 403, "detail": "that scope is not yours"}, "truncated": True})
    monkeypatch.setattr(qbnext, "fetch_prs", lambda _r: ([], None))
    monkeypatch.setattr(qbnext, "fetch_blocked", lambda _r: ({}, None))

    error = qbnext.collect("owner/repo")["errors"]["plan"]
    assert "that scope is not yours" in error and "longer than one page" in error


def test_the_header_says_at_least_rather_than_asserting_nobody_is_on_it(qbnext, monkeypatch):
    """With /claims down every item's claim ends up None regardless of reality,
    so the header printed "12 open, 0 held, 0 blocked" — a positive assertion
    that nobody is on any of it — directly above an UNCERTAIN section saying
    nothing was confirmed free. A header disagreeing with the section under it
    is bad; this one disagreed in the dangerous direction."""
    _wire(qbnext, monkeypatch, [_item(n, n * 10) for n in range(1, 4)],
          errors={"board": "URLError: refused"})
    out = qbnext.render(qbnext.collect("owner/repo"), 5)
    assert "PLAN — 3 open, at least 0 held, 0 blocked" in out
    assert "UNCERTAIN (3)" in out


def test_the_blocked_count_is_a_floor_when_githubs_graph_is_missing(qbnext, monkeypatch):
    _wire(qbnext, monkeypatch, [_item(1, 10)], errors={"github": "gh exit 1"})
    assert "PLAN — 1 open, 0 held, at least 0 blocked" in \
        qbnext.render(qbnext.collect("owner/repo"), 5)


def test_the_uncertain_section_honours_the_same_limit_as_the_free_list(qbnext, monkeypatch):
    """`--limit` is documented as how many rows print, and this section ignored
    it entirely — so a large plan meeting a claims outage made the HEDGE the
    longest thing on screen, unbounded, while the free list it replaced was
    capped at five."""
    _wire(qbnext, monkeypatch, [_item(n, n * 10) for n in range(1, 13)],
          errors={"board": "URLError: refused"})
    data = qbnext.collect("owner/repo")

    out = qbnext.render(data, 5)
    assert out.count("  ? #") == 5
    assert "UNCERTAIN (12)" in out, "the count is the total, the rows are the limit"
    assert "… and 7 more uncertain" in out

    assert qbnext._uncertain_lines(data["items"], 0) == \
        ["  … and 12 more uncertain — raise --limit to see them"]


def test_a_rank_cannot_forge_a_section_header(qbnext, monkeypatch):
    """Every other value on a free line goes through `plain` or `clip`; `rank`
    was interpolated raw. A plan response is remote text the same way a title or
    a holder is."""
    _wire(qbnext, monkeypatch, [_item("2\nUNCERTAIN (9)\n\x1b[2J", 10)])
    out = qbnext.render(qbnext.collect("owner/repo"), 5)
    assert "\x1b" not in out
    assert not any(ln.startswith("UNCERTAIN") for ln in out.splitlines())


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


def test_a_full_claims_page_makes_qb_next_hedge_every_unclaimed_item(qbnext, monkeypatch):
    """The cap has to travel the whole way: `fetch_board` reports a full page,
    `collect` reads that as "a claim taken outside the plan would not be
    visible", and the item leaves the free list. This is the most dangerous of
    the three caps — what a claims page hides is a HELD item — and it was the
    only one with no end-to-end test."""
    _wire(qbnext, monkeypatch, [_item(1, 44)],
          errors={"board": "showing the first 999 claims — there are more"})
    data = qbnext.collect("owner/repo")
    assert data["items"][0]["unknown"] == ["claims"]
    assert "  → #44" not in qbnext.render(data, 5)


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


def test_the_tool_breaking_is_a_different_exit_code_from_a_missing_config(
        qbnext, monkeypatch, capsys):
    """Both used to be 1, documented as "no board configured — a message for a
    person". A hook reading the code could not tell "fix your
    QUARTERBACK_BASE_URL" from "this tool broke on the data it was handed", and
    the two want opposite responses."""
    def explode(_repo, _client=None):
        raise KeyError("phase")

    monkeypatch.setattr(qbnext, "board_client", lambda: (object(), object()))
    monkeypatch.setattr(qbnext, "collect", explode)
    monkeypatch.setattr(sys, "argv", ["qb-next"])

    assert qbnext.main() == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "KeyError: 'phase'" in captured.err


def test_a_defect_in_the_rendering_is_a_message_and_not_a_traceback(qbnext, monkeypatch, capsys):
    """`render` and the JSON emit ran OUTSIDE `main`'s only try, so a formatting
    defect printed a raw traceback in place of the one-line message the exit
    codes promise — and lost every section that HAD been assembled with it."""
    def explode(_data, _limit):
        raise TypeError("'<' not supported between instances of 'NoneType' and 'int'")

    _wire(qbnext, monkeypatch, [_item(1, 44)])
    monkeypatch.setattr(qbnext, "render", explode)
    monkeypatch.setattr(sys, "argv", ["qb-next"])

    assert qbnext.main() == 4
    assert "TypeError: '<' not supported" in capsys.readouterr().err


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
