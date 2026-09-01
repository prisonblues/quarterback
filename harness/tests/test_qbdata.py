"""The joins and the parsing the dashboards share, tested without a dashboard.

qbdata is stdlib-only on purpose, so this runs anywhere `pytest` does — unlike
test_qb_dash.py, which wants textual and a configured board. The join between
board claims and `gh issue list` is the piece worth pinning: it decides which
issues the panel offers as free, and offering a held one sends two agents at the
same work.

Run: pytest harness/tests/test_qbdata.py
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))

import qbdata as qd                                       # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_sandbox                                      # noqa: E402

# ---- reading a seat off the board: deleted with the seat name (#540) --------
#
# Seven tests lived here, over `seat_number`, `seat_machine`, `seat_scope`,
# `slug_scope`, `scope_of` and `pane_scope` — the vocabulary that recovered a tmux
# pane from a board identity spelled `seat-<scope>-<n>`. The last of them started
# the real `qb-seat` under nine directory names and compared the instance it
# reported against `qbdata`'s idea of the same rule, because two implementations
# of one slug were exactly how a dashboard came to show one seat's state against
# another seat's pane.
#
# There is no rule to pin now and no second implementation to pin it to: the pane
# carries the agent's session id and the board returns it, so the join is an
# equality. What replaces them is `test_a_seat_carries_the_session_of_the_agent_
# in_it` below, in the tmux_seats block, and the state-cell join in
# `test_qb_dash.py`.


def claim(key: str, holder: str = "zeus/seat-1", kind: str = "issue") -> dict:
    return {"key": key, "holder": holder, "kind": kind}


def test_an_issue_claim_joins_on_its_number():
    held = qd.issue_claims([claim(f"{qd.REPO}#175", holder="zeus/badger-ember")])
    assert list(held) == [175]
    assert held[175]["holder"] == "zeus/badger-ember"


def test_another_repos_issue_does_not_mark_ours_held():
    """Two repos both have a #12; joining on the bare number would confuse them."""
    assert qd.issue_claims([claim("someone/other#12")]) == {}


def test_claims_that_are_not_issues_are_ignored():
    keys = [f"{qd.REPO}:main", f"{qd.REPO}:2.44", "", "#7", f"{qd.REPO}#abc"]
    assert qd.issue_claims([claim(k, kind="merge") for k in keys]) == {}


def test_free_issues_sort_above_held_ones_newest_first():
    issues = [{"number": n} for n in (170, 169, 168, 167)]
    held = qd.issue_claims([claim(f"{qd.REPO}#170"), claim(f"{qd.REPO}#168")])
    assert [i["number"] for i in qd.sort_issues(issues, held)] == [169, 167, 170, 168]


def test_the_first_claim_on_an_issue_is_the_one_shown():
    held = qd.issue_claims([claim(f"{qd.REPO}#9", holder="zeus/one"),
                            claim(f"{qd.REPO}#9", holder="laptop/two")])
    assert held[9]["holder"] == "zeus/one"


# ---- the plan ----------------------------------------------------------------

def _in(seconds: int) -> str:
    """An ISO timestamp `seconds` from now, for a claim that has not lapsed."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def item(title: str = "do the thing", repo: str | None = qd.REPO, ref: int | None = None,
         holder: str | None = None, blocked: list[dict] | None = None, **extra) -> dict:
    """A /plan item, shaped the way the board returns one."""
    return {
        "item_id": f"id-{title}", "repo": repo, "title": title,
        "ref": {"kind": "issue", "value": str(ref)} if ref else None,
        "blocked_by": blocked or [],
        "claim": {"holder": holder, "note": "on it"} if holder else None,
        **extra,
    }


def covered(title: str = "a line of a held plan", holder: str = "zeus/two",
            **extra) -> dict:
    """An item inside somebody ELSE's held plan — free itself, and not takeable.

    The board decides this: `covered_by` is only ever another agent's plan claim,
    because your own covers nothing from you (that is what lets a holder work
    through its own list). So every presentation function has to read it, and #172
    is what happens when four of them do not.
    """
    return item(title, covered_by={"holder": holder, "note": "the whole list"},
                plan={"label": "stage one"}, **extra)


def test_the_envelope_survives_the_fetch_and_not_just_the_items(monkeypatch):
    """RED/GREEN: `fetch_plan` kept `data["items"]` and dropped everything the
    board had CONCLUDED about that list — what is next, how much of the order
    anybody chose, whether the page was all of it, and the board's own counts.
    Six answers, none of them obtainable by a second call, thrown away on every
    four-second refresh."""
    body = json.dumps({
        "items": [item("one")], "next": {"item_id": "id-one", "caveat": "watch out"},
        "order_trust": {"trusted": False, "unchosen": 3}, "truncated": True,
        "counts": {"open": 9, "claimed": 2, "covered": 1, "blocked": 1, "stale": 4},
    }).encode()
    client, _ = _client(monkeypatch, body)
    plan, err = qd.fetch_plan(client)
    assert err is None
    assert [i["title"] for i in qd.plan_items(plan)] == ["one"]
    assert plan["next"]["caveat"] == "watch out"
    assert plan["order_trust"]["unchosen"] == 3
    assert plan["truncated"] is True
    assert plan["counts"]["covered"] == 1


def test_the_plan_read_says_which_session_is_asking(monkeypatch):
    """RED/GREEN, and the defect is a wrong answer rather than a missing one.
    `GET /plan` resolves `covered_by` by MACHINE when the caller does not say
    which session it is, and this machine runs seven agents at once — so a plan
    held by the agent in the next pane came back as the reader's own and the panel
    drew every item of it with the cyan "free to take" glyph."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    client, sent = _client(monkeypatch, b'{"items": []}')
    qd.fetch_plan(client)
    assert "session=abc-123" in sent[0].full_url
    assert f"limit={qd.PLAN_LIMIT}" in sent[0].full_url


def test_a_dashboard_outside_a_session_still_reads_as_somebody(monkeypatch):
    """A dashboard holds no claims, so a session of its own that matches nothing
    is the honest answer — every co-tenant's plan claim is then somebody else's,
    which is what it is. Reading with no session at all is what restored the
    duplicated work on the read path."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    client, sent = _client(monkeypatch, b'{"items": []}')
    qd.fetch_plan(client)
    assert f"session=qb-dash%3A{os.getpid()}" in sent[0].full_url


def test_a_caller_may_name_the_session_it_reads_as(monkeypatch):
    client, sent = _client(monkeypatch, b'{"items": []}')
    qd.fetch_plan(client, session="explicit")
    assert "session=explicit" in sent[0].full_url


def test_a_dead_board_answers_with_the_shape_a_renderer_reads(monkeypatch):
    """The error path returns an envelope, not a bare list: a panel asking a dead
    board what is next gets "nothing", rather than a KeyError three panels on."""
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(qd.urllib.request, "urlopen", boom)
    client = qd.BoardClient(qd.BoardConfig("https://board.example", "t", "host"))
    plan, err = qd.fetch_plan(client)
    assert "connection refused" in err
    assert qd.plan_items(plan) == [] and plan["next"] is None
    assert plan["counts"] == {} and plan["truncated"] is False


def test_a_key_the_board_sent_as_null_reads_as_the_empty_one(monkeypatch):
    """A key present and null is the same to a renderer as a key that is absent,
    and only one of the two survives a dict merge."""
    client, _ = _client(monkeypatch, b'{"items": null, "counts": null, "order_trust": null}')
    plan, _ = qd.fetch_plan(client)
    assert qd.plan_items(plan) == [] and plan["counts"] == {} and plan["order_trust"] == {}


def test_an_item_that_points_at_no_issue_offers_nothing_to_fix():
    assert qd.plan_issue(item("just a line of plan"), [qd.REPO]) is None


def test_an_issue_backed_item_carries_its_number_and_its_repo():
    got = qd.plan_issue(item("fix it", ref=142), [qd.REPO])
    assert got == {"number": 142, "repo": qd.REPO, "title": "fix it"}


def test_a_bare_repo_name_resolves_against_the_repos_we_watch():
    """The fleet keeps lists under bare names as well as slugs; only a watched
    one can be turned back into an address."""
    assert qd.plan_issue(item(repo="quarterback", ref=7), [qd.REPO])["repo"] == qd.REPO
    assert qd.plan_issue(item(repo="somewhere-else", ref=7), [qd.REPO]) is None


def test_a_pr_ref_is_not_something_to_fix():
    row = item("land it", ref=12)
    row["ref"]["kind"] = "pr"
    assert qd.plan_issue(row, [qd.REPO]) is None


def test_the_state_glyph_says_running_blocked_or_free():
    assert qd.plan_state(item(holder="zeus/one"))[0] == "▶"
    assert qd.plan_state(item(blocked=[{"ref": "9"}]))[0] == "⊘"
    assert qd.plan_state(item())[0] == "○"


def test_the_board_s_own_next_is_the_one_row_marked_as_next():
    """`next` is by definition open, unclaimed, unblocked and uncovered, so it can
    only ever be a row this would otherwise draw ○ — and the panel could not point
    at it at all, which is why both dashboards re-derived one for themselves."""
    glyph, colour = qd.plan_state(item("take me"), next_id="id-take me")
    assert glyph == "◉" and "cyan" in colour
    assert qd.plan_state(item("not me"), next_id="id-take me")[0] == "○"


def test_nothing_but_the_next_row_wears_the_mark():
    """A held or blocked row cannot be next, so the state it already has wins."""
    assert qd.plan_state(item("x", holder="zeus/one"), next_id="id-x")[0] == "▶"
    assert qd.plan_state(item("x", blocked=[{"ref": "9"}]), next_id="id-x")[0] == "⊘"


def test_a_pr_backed_item_does_not_render_as_an_issue():
    """RED/GREEN: `ref.kind` was never read, so an item pointing at a PR and one
    pointing at an issue both drew `#123` — while `plan_issue` DID read the kind
    and refused the PR, leaving a dim ⚒ with nothing on the row explaining why the
    item beside it was takeable and this one was not."""
    row = item("land it", ref=12)
    row["ref"]["kind"] = "pr"
    assert qd.plan_ref(row) == "PR#12"
    assert qd.plan_ref(item("fix it", ref=12)) == "#12"
    assert qd.plan_ref(item("a line of plan")) == ""


def test_a_rank_nobody_chose_is_marked_as_one():
    """#183: a rank that was merely where the add landed and a rank somebody chose
    are different facts, and the terminal presented both as row position."""
    assert qd.plan_rank({"rank": 12, "rank_source": "ordered"})[0] == "12"
    assert qd.plan_rank({"rank": 12, "rank_source": "appended"})[0] == "~12"
    assert qd.plan_rank({"rank": 12})[0] == "~12", "no source is not a chosen one"
    assert qd.plan_rank({})[0] == ""
    # #427: a claim put it there, which is a position somebody's ACTION decided.
    # The tilde counts the same rows the panel title calls unchosen, and the board
    # counts only `appended` — so a tilde here would put a number in the column
    # that the title beside it contradicts.
    assert qd.plan_rank({"rank": 1, "rank_source": "picked-up"})[0] == "1"


def test_a_covered_item_does_not_get_the_free_to_take_glyph():
    """The reported failure. The cyan ○ is the panel's invitation to pick something
    up, and it was drawn over every item of somebody else's held plan — the exact
    duplicated work `covered_by` and the `next` filter exist to prevent, on the
    panel a seat reads to choose its work."""
    glyph, colour = qd.plan_state(covered())
    assert glyph != "○" and colour != "cyan"
    assert glyph == "▷", "distinct from ▶ too: the holder is on the list, not the line"


def test_an_items_own_claim_still_outranks_the_plans_glyph():
    """A holder on the line is the more specific fact, and the one that says the
    item itself is not free."""
    row = covered("held outright")
    row["claim"] = {"holder": "zeus/one", "note": "on it"}
    assert qd.plan_state(row)[0] == "▶"


def test_the_right_hand_column_holds_whichever_fact_is_true():
    assert qd.plan_who(item(holder="zeus/badger-ember"))[0] == "zeus/badger-ember"
    assert qd.plan_who(item(blocked=[{"ref": "9"}]))[0] == "waits #9"
    assert qd.plan_who(item(blocked=[{"ref": None}, {"ref": None}]))[0] == "waits ×2"


def test_the_holder_keeps_the_machine_it_is_on():
    """RED/GREEN: the machine half was dropped, and a name alone is not an
    identity — names are short, memorable and RECYCLED when an agent finishes, so
    two agents on two boxes read as one agent working twice."""
    assert qd.plan_who(item(holder="zeus/badger-ember"))[0] != \
        qd.plan_who(item(holder="laptop/badger-ember"))[0]


def test_a_held_row_that_also_waits_on_something_says_both():
    """The premise was that only one of the three facts is ever true of a row, and
    it is false for exactly this pair: an agent holding an item that waits on
    something else is stuck, which is the combination worth acting on."""
    assert qd.plan_who(item(holder="zeus/one", blocked=[{"ref": "9"}]))[0] == "⊘zeus/one"


def test_a_covered_item_shows_its_plans_holder_and_not_an_idle_age():
    """An age in that column is the strongest invitation on the pane — "nobody has
    touched this for four days" — and it was printed over work another agent had
    reserved as a unit. Who to talk to is what a reader needs there."""
    assert qd.plan_who(covered(holder="zeus/badger-ember"))[0] == "zeus/badger-ember"


def _envelope(items, **over) -> dict:
    """A `/plan` answer around some items, defaulted the way the board sends one."""
    return {**qd.EMPTY_PLAN, "items": items, **over}


def test_the_title_reports_the_boards_own_counts(monkeypatch):
    """RED/GREEN: the title recounted the page locally and folded covered into
    running, so the pane could not make the distinction the board keeps those two
    numbers apart to make — a blocked item needs work finishing, a covered one
    needs a word with its holder. `stale` it could not report at all."""
    plan = _envelope([item("one")],
                     counts={"open": 30, "claimed": 4, "covered": 2, "blocked": 1,
                             "stale": 5, "done": 12, "dropped": 3})
    assert qd.plan_tally(plan, qd.plan_items(plan)) == {
        "open": 30, "claimed": 4, "covered": 2, "blocked": 1, "stale": 5}


def test_a_scope_that_hid_rows_counts_the_rows_it_is_showing():
    """The board's counts are over the whole open set, and a title that counts
    rows the pane refuses to draw is a title lying about the pane."""
    items = [item("mine", holder="zeus/one"), covered("covered"),
             item("blocked", blocked=[{"ref": "9"}]), item("old", stale=True)]
    plan = _envelope(items, counts={"open": 99, "claimed": 99, "covered": 99,
                                    "blocked": 99, "stale": 99})
    assert qd.plan_tally(plan, items, hidden=7) == {
        "open": 4, "claimed": 1, "covered": 1, "blocked": 1, "stale": 1}


def test_the_recount_overlaps_exactly_where_the_boards_counts_do():
    """An item both held and blocked is in both of the board's numbers. A local
    recount that quietly deduplicated would be a third answer about the plan."""
    both = item("both", holder="zeus/one", blocked=[{"ref": "9"}])
    got = qd.plan_tally(_envelope([both]), [both], hidden=1)
    assert got["claimed"] == 1 and got["blocked"] == 1


def test_the_title_carries_what_the_board_says_is_next():
    plan = _envelope([item("one")], counts={"open": 1},
                     next={"item_id": "id-one", "ref": {"kind": "issue", "value": "78"}})
    assert ("next #78", "cyan") in qd.plan_head_bits(plan, qd.plan_items(plan))


def test_a_next_the_scope_has_hidden_is_named_by_its_repo():
    """"next #78" over a list that does not contain #78 reads as a rendering fault
    rather than as an answer about the whole plan."""
    plan = _envelope([item("one")], counts={"open": 1},
                     next={"item_id": "elsewhere", "repo": "prisonblues/nix-fleet",
                           "ref": {"kind": "issue", "value": "3"}})
    assert ("next nix-fleet #3", "cyan") in qd.plan_head_bits(plan, qd.plan_items(plan), 1)


def test_the_title_says_how_much_of_the_order_nobody_chose():
    """#183's minimum fix, on the surface that could not show it: the terminal
    had no way to tell a chosen priority from where an add happened to land."""
    plan = _envelope([item("one")], counts={"open": 1},
                     order_trust={"trusted": False, "unchosen": 5})
    assert ("~5 unchosen", None) in qd.plan_head_bits(plan, qd.plan_items(plan))


def test_a_trusted_order_says_nothing_about_being_trusted():
    plan = _envelope([item("one")], counts={"open": 1},
                     order_trust={"trusted": True, "unchosen": 0})
    assert not any("unchosen" in text for text, _ in
                   qd.plan_head_bits(plan, qd.plan_items(plan)))


def test_a_title_with_no_room_drops_the_tally_before_the_answer():
    """A panel title is clipped at the border and clipped from the END, so an
    overflowing title loses `next` and `truncated` — the two answers the line
    exists to carry — without saying so. Dropping a segment nobody would miss says
    the same thing in less room; being cut off mid-word does not."""
    plan = _envelope([item("one")], truncated=True,
                     counts={"open": 40, "claimed": 1, "covered": 2, "blocked": 1,
                             "stale": 4},
                     order_trust={"trusted": False, "unchosen": 2},
                     next={"item_id": "id-one", "ref": {"kind": "issue", "value": "394"}})
    texts = [text for text, _ in qd.plan_head_bits(plan, qd.plan_items(plan), room=50)]
    assert texts == ["40 open", "~2 unchosen", "next #394", "truncated at 1"]
    texts = [text for text, _ in qd.plan_head_bits(plan, qd.plan_items(plan), room=40)]
    assert texts == ["40 open", "next #394", "truncated at 1"]
    # Tighter still: the count of what was truncated is detail, the fact is not.
    texts = [text for text, _ in qd.plan_head_bits(plan, qd.plan_items(plan), room=31)]
    assert texts == ["40 open", "next #394", "truncated"]
    # And a pane too narrow even for that gives up whole segments rather than
    # having the last one cut off in the middle of a word.
    texts = [text for text, _ in qd.plan_head_bits(plan, qd.plan_items(plan), room=25)]
    assert texts == ["40 open", "next #394"]
    assert [text for text, _ in qd.plan_head_bits(plan, qd.plan_items(plan),
                                                  room=12)] == ["next #394"]


def test_a_scoped_title_counts_the_unchosen_positions_it_is_showing():
    """The board's `unchosen` is about the whole plan. A title claiming five
    unchosen positions over two tildes on screen sends a reader looking for three
    rows that are not there — the same rule the counts beside it follow."""
    items = [item("chosen", rank=1, rank_source="ordered"),
             item("landed there", rank=2, rank_source="appended")]
    plan = _envelope(items, counts={"open": 2}, order_trust={"unchosen": 5})
    assert ("~5 unchosen", None) in qd.plan_head_bits(plan, items)
    assert ("~1 unchosen", None) in qd.plan_head_bits(plan, items, hidden=3)


def test_a_truncated_plan_says_so_rather_than_ending_quietly():
    """`PLAN_LIMIT` is 200 and a truncated plan rendered silently in both
    dashboards — the board says it out loud precisely so it need not be worked
    out by comparing lengths."""
    plan = _envelope([item("one")], counts={"open": 400}, truncated=True)
    assert ("truncated at 1", "red") in qd.plan_head_bits(plan, qd.plan_items(plan))


def test_the_detail_line_carries_the_note_the_panel_cannot_fit():
    line = qd.plan_detail(item("short title", ref=8, holder="zeus/one",
                               plan={"label": "stage one"},
                               note="because the order matters"))
    assert "#8" in line and "stage one" in line
    assert "zeus/one" in line and "because the order matters" in line


def test_the_detail_line_carries_the_provenance_a_row_has_no_room_for():
    """Where the item sits, who put it there, how long it has sat and when the
    claim on it lapses — fifteen fields do not fit a 45-column pane, and the
    argument is about which get promoted onto the row, not about cramming them
    all on. These are the ones that belong behind a click."""
    line = qd.plan_detail(item("a line", rank=4, rank_source="placed",
                               placed_for="after #7", stale=True, idle_days=9.5,
                               added_by="zeus/one",
                               claim={"holder": "zeus/two", "expires": _in(90)}))
    assert "rank 4 (placed after #7)" in line
    assert "stale, idle 9.5d" in line and "added by zeus/one" in line
    assert "left" in line, "a claim with no time on it is one nobody need chase"


def test_the_caveat_rides_on_the_row_the_board_named_next():
    """The board's own statement of how much its recommendation is worth. It is a
    sentence, so the title gets the count and this gets the argument — and it is
    pinned to `next` alone, because on any other row it would read as a warning
    about that row."""
    plan = {**qd.EMPTY_PLAN, "items": [item("one")],
            "next": {"item_id": "id-one", "caveat": "nobody chose this order"}}
    assert "nobody chose this order" in qd.plan_detail(item("one"), plan)
    assert "nobody chose this order" not in qd.plan_detail(item("two"), plan)
    assert "nobody chose this order" not in qd.plan_detail(item("one"))


def test_an_item_covered_by_somebody_elses_PLAN_claim_says_so():
    """#172: a plan-level claim over an item nobody has taken individually. Worded
    differently from "held", because the remedy is different — the whole plan is
    somebody's, so talk to them rather than lifting one line out of it."""
    line = qd.plan_detail(item("a line of a held plan", ref=9,
                               plan={"label": "stage one"},
                               covered_by={"holder": "zeus/two",
                                           "note": "working the whole list"}))
    assert "in stage one held by zeus/two" in line
    assert "working the whole list" in line


def test_an_items_OWN_claim_wins_over_the_plans():
    """Both would otherwise print, and the item's own holder is the specific fact:
    a covered item is free to take from its plan's holder, a claimed one is not."""
    line = qd.plan_detail(item("held outright", ref=10, holder="zeus/one",
                               plan={"label": "stage one"},
                               covered_by={"holder": "zeus/two", "note": "the plan"}))
    assert "held by zeus/one" in line
    assert "zeus/two" not in line


def test_an_item_claim_shows_the_item_it_holds_and_not_its_uuid():
    """`item:<uuid>` since #172 — it was `plan:<uuid>`, and that spelling now means
    a claim on the WHOLE plan, so the old lookup compared a plan id against item
    ids and the CLAIMED pane showed a bare uuid for every claim the plan takes."""
    plan = [item("Give the annex a sloped roof", repo="65lowther", ref=None)]
    plan[0]["item_id"] = "ea9e1623"
    assert qd.claim_label("item:ea9e1623", plan) == "plan Give the annex a sloped roof"


def test_a_whole_plan_claim_is_named_as_the_plan_not_as_its_first_item():
    """It is not the first row's work, it is all of it — and the label is what the
    holder called the list."""
    plan = [item("first line", plan={"plan_id": "aa11", "label": "the annex"}),
            item("second line", plan={"plan_id": "aa11", "label": "the annex"})]
    assert qd.claim_label("plan:aa11", plan) == "plan the annex"


def test_an_unresolvable_board_object_key_keeps_the_key():
    """A key nobody can look up still beats a blank cell."""
    assert qd.claim_label("item:ea9e1623", []) == "item:ea9e1623"
    assert qd.claim_label("plan:aa11", [item("x")]) == "plan:aa11"


def test_an_ordinary_claim_key_is_still_shortened():
    assert qd.claim_label(f"{qd.REPO}#142", []) == "quarterback#142"


# ---- the scope: which project's rows a dashboard is about (#261) -------------
#
# Two decisions, and they have to agree: which rows are kept, and whether the
# repo cell is worth eleven columns of a 78-column pane. Tested together for that
# reason — a column dropped from rows that were not narrowed shows nothing, and
# rows narrowed with the column still there is the waste the scope exists to end.

ONE = qd.Scope([qd.REPO])
TWO = qd.Scope([qd.REPO, "prisonblues/nix-fleet"])


def test_one_repo_spends_no_column_saying_which_one():
    assert ONE.column is False
    assert ONE.label() == "quarterback"


def test_two_watched_repos_keep_the_cell_that_tells_them_apart():
    assert TWO.column is True


def test_the_wide_view_always_names_the_repo_because_that_is_why_it_is_wide():
    wide = ONE.toggled()
    assert wide.on is False
    assert wide.column is True
    assert wide.label() == "all repos"
    assert wide.keeps("someone/else") is True


def test_toggling_goes_both_ways_which_is_why_it_is_not_called_widened():
    """`widened()` narrowed on every other press — the name promised one direction
    and the method delivered two, which is the whole of the rename."""
    assert ONE.toggled().on is False
    assert ONE.toggled().toggled().on is True
    assert ONE.toggled().repos == ONE.repos


def test_two_owners_of_one_name_are_two_repositories():
    """A fork and its upstream share a bare name and are not the same repo.

    Folded to the bare name they collapsed into a single entry, and both of the
    things that read `len(names) == 1` then went wrong at once: the column dropped
    (nothing left to tell the two apart) and `keeps` accepted both repos' rows.
    """
    fork = qd.Scope(["myuser/quarterback"])
    assert fork.keeps("myuser/quarterback")
    assert not fork.keeps("prisonblues/quarterback")
    # A row that gives only a bare name can only be compared as one, and is kept.
    assert fork.keeps("quarterback")

    both = qd.Scope(["myuser/quarterback", "prisonblues/quarterback"])
    assert both.column is True, "no cell left to tell a fork from its upstream"
    assert both.label() == "myuser/quarterback, prisonblues/quarterback"
    # CLAIMED has no repo column for the scope to restore, so the OWNER is what
    # tells two claims apart — dropping it here would put the ambiguity back one
    # panel further on.
    assert qd.claim_label("myuser/quarterback#3", [], both) == "myuser/quarterback#3"
    assert qd.claim_label("prisonblues/quarterback#3", [], both) \
        == "prisonblues/quarterback#3"


def test_one_repository_named_twice_is_still_one_repository():
    """`QB_DASH_REPOS=quarterback,prisonblues/quarterback` is one project.

    `keeps` has always read it that way; counting the two spellings separately put
    the eleven-column cell back on a single-project pane, which is the waste the
    scope removes.
    """
    twice = qd.Scope(["quarterback", f"{qd.REPO}"])
    assert twice.column is False
    assert twice.label() == "quarterback"
    assert qd.claim_label(f"{qd.REPO}#209", [], twice) == "#209"


def test_an_unattributable_row_is_marked_where_the_column_is_gone():
    """The repo cell was the only thing that said "nothing could name this".

    `keeps` deliberately holds on to such a row, and narrow mode is exactly the
    mode that drops the cell — so without a mark an agent working outside any
    checkout reads as one working here.
    """
    assert qd.scope_mark(ONE, None) == "? "
    assert qd.scope_mark(ONE, "") == "? "
    assert qd.scope_mark(ONE, "quarterback") == ""
    # The wide view has the repo itself, which says more than a mark can.
    assert qd.scope_mark(ONE.toggled(), None) == ""
    assert qd.scope_mark(TWO, None) == ""
    assert qd.scope_mark(None, None) == ""


def test_the_three_spellings_of_one_repository_are_one_repository():
    """A lease reports the checkout's directory; the plan and `gh` report a slug.

    Comparing the spellings would put a seat's own FLEET row outside its own
    scope — the board says `quarterback`, the plan says `prisonblues/quarterback`,
    and neither is wrong.
    """
    assert ONE.keeps("quarterback")
    assert ONE.keeps("prisonblues/quarterback")
    assert ONE.keeps("Quarterback")
    assert not ONE.keeps("prisonblues/nix-fleet")


def test_a_row_the_board_cannot_attribute_stays_on_the_pane():
    """No repo is not evidence of ANOTHER repo.

    An agent working outside a checkout reports no repo, and hiding it would drop
    a live peer on the strength of a missing field. The narrow view is a way to
    read the fleet, not a claim to have accounted for all of it.
    """
    assert ONE.keeps(None)
    assert ONE.keeps("")


def test_a_narrowed_panel_can_say_how_many_rows_it_hid():
    """The count is the whole reason in_scope returns two things.

    A panel that filtered silently is a panel lying about the fleet: "nothing
    claimed" and "nothing claimed here" are different facts.
    """
    rows = [{"repo": "quarterback"}, {"repo": "prisonblues/nix-fleet"},
            {"repo": None}, {"repo": "someone/other"}]
    kept, hidden = qd.in_scope(rows, ONE)
    assert [r["repo"] for r in kept] == ["quarterback", None]
    assert hidden == 2


def test_no_scope_at_all_hides_nothing():
    rows = [{"repo": "a/one"}, {"repo": "b/two"}]
    assert qd.in_scope(rows, None) == (rows, 0)


def test_a_claim_names_its_repo_in_its_key_or_not_at_all():
    """The three key shapes in use, of which two carry a repo."""
    assert qd.claim_repo("prisonblues/quarterback#209") == "prisonblues/quarterback"
    assert qd.claim_repo("prisonblues/quarterback:2.40") == "prisonblues/quarterback"
    assert qd.claim_repo("merge-queue") is None
    assert qd.claim_repo("") is None
    assert qd.claim_repo(None) is None


def test_a_plan_claim_gets_its_repo_from_the_plan_or_stays_unattributed():
    """`plan:<uuid>` names an ITEM, not a repo, so the plan is what resolves it.

    Unattributed when the plan has not been fetched — and that keeps the claim on
    the pane, which is right: hiding it would drop the one row saying somebody
    already holds the work you were about to pick up.
    """
    plan = [item("roof", repo="65lowther")]
    plan[0]["item_id"] = "ea9e1623"
    assert qd.claim_repo("plan:ea9e1623", plan) == "65lowther"
    assert qd.claim_repo("plan:ea9e1623", []) is None
    assert qd.claim_repo("plan:ea9e1623") is None


def test_the_claim_key_drops_the_repo_only_when_the_header_states_it():
    """`quarterback#209` is twelve columns to say `#209` — on a pane showing one
    project. On a pane showing several, the repo is what tells two claims apart."""
    assert qd.claim_label(f"{qd.REPO}#209", [], ONE) == "#209"
    assert qd.claim_label(f"{qd.REPO}:2.40", [], ONE) == "2.40"
    assert qd.claim_label(f"{qd.REPO}#209", [], ONE.toggled()) == "quarterback#209"
    assert qd.claim_label(f"{qd.REPO}#209", [], TWO) == "quarterback#209"
    assert qd.claim_label("prisonblues/nix-fleet#3", [], ONE) == "nix-fleet#3"
    assert qd.claim_label(f"{qd.REPO}#209", []) == "quarterback#209"


def test_the_scope_opens_narrow_and_the_env_is_how_a_pane_opens_wide(monkeypatch):
    """Narrow by default, because that is what a screen is FOR."""
    monkeypatch.delenv(qd.SCOPE_ENV, raising=False)
    assert qd.resolve_scope([qd.REPO]).on is True
    monkeypatch.setenv(qd.SCOPE_ENV, "all")
    assert qd.resolve_scope([qd.REPO]).on is False
    monkeypatch.setenv(qd.SCOPE_ENV, "ALL")
    assert qd.resolve_scope([qd.REPO]).on is False
    # Anything unrecognised is the default rather than an error: a typo in a
    # tmux env should cost a wide pane, not a dashboard that will not start.
    monkeypatch.setenv(qd.SCOPE_ENV, "quarterback")
    assert qd.resolve_scope([qd.REPO]).on is True


# ---- pointing a dashboard at a project --------------------------------------

@pytest.fixture
def watched():
    """Restore the process-wide repo cache, whatever a test does to it."""
    before = qd._repos
    yield
    qd._repos = before


def test_repo_reaches_what_reads_resolve_repos_for_itself(watched):
    """--repo has to land in the CACHE, not be passed around.

    The plan's ordering, the `gh` calls and the ⚒ that needs a slug to start work
    all reach resolve_repos() directly. Threading a list through the callers that
    do take one would leave whichever was missed silently watching the cwd, which
    is #176 again.
    """
    qd.set_repos(["prisonblues/nix-fleet", " ", "me/app"])
    assert qd.resolve_repos() == ["prisonblues/nix-fleet", "me/app"]


def test_clearing_the_pin_falls_back_to_the_environment(watched, monkeypatch):
    qd.set_repos([])
    monkeypatch.setenv("QB_DASH_REPOS", "me/app")
    assert qd.resolve_repos() == ["me/app"]


@pytest.fixture
def checkout(tmp_path):
    """A real git checkout with an origin remote, BUILT here rather than borrowed.

    The tests below used to reach for this suite's own repo root
    (`Path(__file__).parents[2]`), which is a checkout when a developer runs them
    and `/build` when the `worktree-tests` sandbox does — that sandbox holds
    `harness/bin` and `harness/tests` and is not a git repository at all. So they
    did not skip there, they FAILED, on "not a git checkout with an origin
    remote": a suite asserting about a thing its environment does not hold, which
    is #163's mechanism wearing different clothes.

    A checkout the test makes is one every environment has, so these assert in the
    sandbox instead of only on a laptop. It is also the more honest fixture: what
    is under test is `repo_target`'s reading of *a* checkout, never this one.

    THE DIRECTORY IS NOT NAMED AFTER THE REPOSITORY, and that is the fixture's one
    piece of deliberate awkwardness. `repo_target` asks a directory for its ORIGIN
    rather than reading its name — which is the whole reason it shells out to git
    at all, and what makes `--repo <a worktree>` report the repository instead of
    the branch the worktree is named for. A fixture whose directory and origin both
    said `quarterback` could not tell the two implementations apart, so it says
    `wt-review` on disk and `prisonblues/quarterback` at the remote.
    """
    root = tmp_path / "wt-review"
    (root / "sub").mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True,
                       capture_output=True)

    git("init", "-q")
    git("remote", "add", "origin",
        "https://github.com/prisonblues/quarterback.git")
    return root


def test_a_checkout_is_asked_which_repo_it_is(checkout):
    slug = qd.repo_arg(str(checkout))
    assert slug == "prisonblues/quarterback"


def test_a_checkout_is_returned_absolute_because_tmux_resolves_it_elsewhere(
        checkout, monkeypatch):
    """The path is handed to tmux as a `-c` start directory, and tmux resolves a
    relative one against the SERVER's cwd — where it was started, not where the
    dashboard is. `self.repo` was `os.getcwd()` and absolute by construction; a
    relative `--repo` would have launched work somewhere else entirely, while the
    guard beside it resolved the same path correctly in-process and hid it."""
    monkeypatch.chdir(checkout)
    for spelling in (".", str(checkout) + "/", "sub/../"):
        slug, path = qd.repo_target(spelling)
        assert os.path.isabs(path), f"{spelling} came back relative: {path}"
        assert slug.endswith("/quarterback")


def test_a_bare_name_that_is_a_directory_is_that_directory(checkout, monkeypatch):
    """`--repo nix-fleet` beside a checkout of that name is not a guess about an
    owner, and it worked before the shape rule arrived."""
    monkeypatch.chdir(checkout.parent)
    slug, path = qd.repo_target(checkout.name)
    assert path == str(checkout)
    # BOTH halves, and the slug is the half a directory name cannot supply. The
    # fixture's directory is `wt-review` and its origin is `prisonblues/quarterback`
    # — this suite runs in a worktree as readily as in the main checkout, and a
    # worktree's directory name is not its repository's name.
    assert slug == "prisonblues/quarterback", (
        f"{checkout.name!r} was read as a name rather than asked for its origin")


def test_a_checkout_argument_says_where_work_should_run_too(checkout):
    """`--repo <checkout>` moves the ⚒'s cwd, not only the rows the panels draw.

    A SLUG cannot: it names a repository this process may have no checkout of, so
    the second half of the answer is None and the guards refuse those rows out loud
    rather than launching `/fix-issue` on a number that means something else here.
    """
    slug, path = qd.repo_target(str(checkout))
    assert slug.endswith("/quarterback") and path == str(checkout)
    assert qd.repo_target("prisonblues/nix-fleet") == ("prisonblues/nix-fleet", None)


def test_a_slug_is_read_as_a_slug_wherever_it_is_typed(tmp_path, monkeypatch):
    """Shape first, the filesystem second.

    Under a `~/src/<owner>/<repo>` layout, `--repo prisonblues/quarterback` matched
    `os.path.isdir` on a directory that is not itself a checkout and died as "not a
    git checkout" — and it made the bare-name test below pass only because no
    `./quarterback` happened to exist wherever pytest ran.
    """
    (tmp_path / "prisonblues" / "quarterback").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    assert qd.repo_target("prisonblues/quarterback") == ("prisonblues/quarterback", None)
    # ...and the relative path is still reachable, with the ./ that says so.
    with pytest.raises(ValueError):
        qd.repo_target("./prisonblues/quarterback")


def test_a_tilde_is_expanded_because_the_help_text_advertises_one(
        checkout, monkeypatch):
    """Only an interactive shell expands `~`. Quoted, built into a QB_SEATS_DASH
    command or sent through `tmux send-keys`, it arrives intact — and was reported
    as a bad slug, which misdiagnoses it."""
    monkeypatch.setenv("HOME", str(checkout))
    slug, path = qd.repo_target("~")
    assert slug.endswith("/quarterback") and path == str(checkout)


def test_a_malformed_slug_is_refused_rather_than_handed_to_gh():
    """It used to validate on the STRIPPED parts and return the RAW value, so a
    slug's internal space reached `gh` inside a repository name.

    Padding is trimmed, since that is what a repo list does with it everywhere else
    (`set_repos`); a character no repository name may contain is refused, because
    the alternative is `gh` being asked about `na@me` and answering about nothing.
    """
    assert qd.repo_target("owner/ repo") == ("owner/repo", None)
    for bad in ("owner/name with space", "owner/na@me", "owner/repo/extra"):
        # THE MESSAGE, not just the raise: a malformed slug used to fall through to
        # the checkout branch, spend a `git -C` subprocess on it and come back "not
        # a git checkout with an origin remote", which diagnoses the wrong thing.
        with pytest.raises(ValueError, match="not an owner/name slug"):
            qd.repo_target(bad)
    # `owner/..` is a path, not a repository whose name happens to be dots.
    with pytest.raises(ValueError):
        qd.repo_target("owner/..")
    # A trailing slash is stripped before anything looks at the shape, so this is
    # the bare name `owner` and gets the bare name's message.
    with pytest.raises(ValueError, match="needs its owner"):
        qd.repo_target("owner/")


def test_a_bare_name_is_refused_rather_than_given_an_owner():
    """`gh` needs an owner, and the fleet works in repos whose owner is not ours.

    Inventing one aims the PR panel — and the ⚒ that starts work off it — at
    somebody else's repository of the same name.
    """
    with pytest.raises(ValueError):
        qd.repo_arg("quarterback")
    with pytest.raises(ValueError):
        qd.repo_arg("/nowhere/at/all/really")
    assert qd.repo_arg("prisonblues/nix-fleet") == "prisonblues/nix-fleet"


def test_a_branch_key_is_not_mistaken_for_a_board_object():
    """A merge key has a colon in it too, and the half in front of it is a repo:
    looking that up in the plan would be looking up `prisonblues/quarterback`."""
    assert qd.claim_label(f"{qd.REPO}:feat/x", [item("x")]) == f"{qd.REPO}:feat/x"


# ---- the tmux screen ---------------------------------------------------------

def test_no_tmux_means_no_seats_rather_than_an_exception(monkeypatch):
    """The dashboard runs in a bare terminal as often as in the screen.

    An empty SEATS panel is the honest answer there, and it must not cost a
    traceback on every refresh — this is called on a four-second timer.
    """
    monkeypatch.delenv("TMUX", raising=False)
    assert qd.tmux_seats() == ([], None)


def _tmux_returning(monkeypatch, rows):
    """Stand in for `tmux list-panes -a`, which is where every seat comes from."""
    monkeypatch.setenv("TMUX", "/tmp/whatever,1,0")

    class Done:
        returncode = 0
        stdout = "\n".join(rows) + "\n"

    monkeypatch.setattr(qd.subprocess, "run", lambda *a, **k: Done())


def test_seats_come_back_in_seat_order_not_pane_order(monkeypatch):
    """--add splits off the LEFTMOST pane, so a grown screen runs 1, 3, 2.

    Sorting on the seat number is what keeps the panel's ✕ next to the seat a
    human is reading, rather than next to whichever pane tmux listed third.
    """
    _tmux_returning(monkeypatch, [
        "%0\t1\ts\t0\tclaude\t/repo\tsess-a",
        "%2\t3\ts\t0\tclaude\t/repo\tsess-c",
        "%1\t2\ts\t0\tbash\t/repo\t",
        "%3\t\ts\t0\tqb-board\t/repo\t",   # the board pane: no @qb_seat
    ])
    got, err = qd.tmux_seats()
    assert err is None
    assert [s["seat"] for s in got] == ["1", "2", "3"]
    assert [s["pane"] for s in got] == ["%0", "%1", "%2"]
    assert all(s["command"] for s in got), "the board pane leaked into the seats"


def test_two_screens_come_back_grouped_by_screen(monkeypatch):
    """`list-panes -a` is the whole server, and since #208 two screens can each
    hold a seat 1. Sorted on the number alone they interleave, and the panel reads
    as one screen with every number twice."""
    _tmux_returning(monkeypatch, [
        "%0\t1\tseats-lexray\t0\tclaude\t/x/lexray\tsess-l1",
        "%2\t1\tseats-nix-fleet\t0\tclaude\t/x/nix-fleet\tsess-n1",
        "%3\t2\tseats-nix-fleet\t0\tclaude\t/x/nix-fleet\tsess-n2",
        "%1\t2\tseats-lexray\t0\tclaude\t/x/lexray\tsess-l2",
    ])
    got, _ = qd.tmux_seats()
    assert [(s["session"], s["seat"]) for s in got] == [
        ("seats-lexray", "1"), ("seats-lexray", "2"),
        ("seats-nix-fleet", "1"), ("seats-nix-fleet", "2"),
    ]


def test_a_seat_carries_the_session_of_the_agent_in_it(monkeypatch):
    """`@qb_session` is the whole join to the board, so it has to come back.

    Two screens can each hold a seat 1 and two machines can each hold an agent
    called the same thing; a conversation id collides with neither, which is why
    the state cell reads off this and not off the seat number any more (#540).
    """
    _tmux_returning(monkeypatch, [
        "%0\t1\ts\t0\tclaude\t/repo\t7f3c9a21-1111-4222-8333-444455556666",
    ])
    assert qd.tmux_seats()[0][0]["agent"] == "7f3c9a21-1111-4222-8333-444455556666"


def test_a_pane_with_no_agent_in_it_is_still_a_seat(monkeypatch):
    """An empty `@qb_session` is a pane holding a shell — a seat someone closed
    the agent in, or a screen built with an empty initial command. It is a seat
    with no state, not a row to drop: those are the ones free to be given work."""
    _tmux_returning(monkeypatch, ["%0\t1\ts\t0\tbash\t/repo\t"])
    got, _ = qd.tmux_seats()
    assert [s["seat"] for s in got] == ["1"]
    assert got[0]["agent"] == ""


def test_a_tmux_that_fails_is_an_empty_screen_not_a_crash(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/whatever,1,0")

    def boom(*a, **k):
        raise OSError("no tmux here")

    monkeypatch.setattr(qd.subprocess, "run", boom)
    seats, err = qd.tmux_seats()
    assert seats == []
    assert err, "a failure came back indistinguishable from an empty screen"


# ---- the machine, told apart from the screen ---------------------------------
#
# Every one of these used to be `[]`, which is also what a screen with no seats
# returns — so the dashboard said "no seat screen on this server" beside a screen
# with three seats in it, and its ＋ declined to add one. The seats half is not
# what these pin; the ERROR half is.


def test_a_tmux_that_exits_nonzero_says_so_rather_than_reporting_no_seats(monkeypatch):
    """The shape this was written for: a shim on PATH ahead of the real tmux,
    exiting 127 on every call, on a box with a screen up."""
    monkeypatch.setenv("TMUX", "/tmp/whatever,1,0")

    class Done:
        returncode = 127
        stdout = ""
        stderr = "/nix/store/gone/bin/tmux: No such file or directory"

    monkeypatch.setattr(qd.subprocess, "run", lambda *a, **k: Done())
    seats, err = qd.tmux_seats()
    assert seats == []
    assert "No such file" in err, err


def test_the_exit_code_is_the_fallback_and_not_the_answer(monkeypatch):
    """stderr first, because it names WHICH tmux broke. An exit code alone is the
    answer only when there was nothing else to say."""
    monkeypatch.setenv("TMUX", "/tmp/whatever,1,0")

    class Done:
        returncode = 3
        stdout = ""
        stderr = "   \n"

    monkeypatch.setattr(qd.subprocess, "run", lambda *a, **k: Done())
    assert qd.tmux_seats()[1] == "tmux exited 3"


def test_a_missing_tmux_binary_is_named_as_such(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/whatever,1,0")

    def gone(*a, **k):
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(qd.subprocess, "run", gone)
    assert qd.tmux_seats()[1] == "tmux is not on PATH"


def test_outside_tmux_is_not_reported_as_a_failure(monkeypatch):
    """A deliberate departure from the first cut of this fix, which called this
    one an error too.

    The dashboard full-screen in a bare terminal is a first-class way to run it.
    An error here would fire on every such run and bury the failures this change
    exists to surface — a permanent complaint that means nothing is wrong.
    """
    monkeypatch.delenv("TMUX", raising=False)
    seats, err = qd.tmux_seats()
    assert seats == []
    assert err is None, "a bare terminal was reported as a broken machine"


# --- what an agent is doing, and when that answer goes off ---------------------
# The board stores what a holder reported; `stalled` is concluded here, from how
# old the report is. That conclusion is drawn in two places — this helper and the
# footer in nix-fleet — and the failure that matters is them disagreeing about
# one seat, so the threshold is a named constant on both sides and these cases
# pin the behaviour it drives.

def agent(state: str | None, age_s: int | None = 0) -> dict:
    a: dict = {"holder": "zeus/seat-1", "state": state}
    if age_s is not None and state is not None:
        when = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        a["state_at"] = when.isoformat()
    return a


def test_a_lease_that_never_reported_says_nothing():
    word, _ = qd.agent_state(agent(None))
    assert word == ""


def test_a_fresh_working_is_working():
    assert qd.agent_state(agent("working", 30))[0] == "working"


def test_a_working_nobody_has_refreshed_is_stalled():
    """The whole point: a pane that said `working` and then went quiet looks
    exactly like a busy one from the outside."""
    word, style = qd.agent_state(agent("working", qd.STALL_AFTER + 60))
    assert word == "stalled"
    assert "red" in style


def test_waiting_never_goes_stale():
    """A pane that has been waiting on a human since lunch is still waiting on
    that human. Ageing it into `stalled` would hide the state being scanned for."""
    assert qd.agent_state(agent("waiting", 6 * 3600))[0] == "waiting"


def test_input_never_goes_stale_either():
    assert qd.agent_state(agent("input", 6 * 3600))[0] == "input"


def test_a_state_with_no_timestamp_is_not_promoted_to_stalled():
    """Unparseable or missing `state_at` is unknown age, and unknown is not
    evidence of being stuck."""
    assert qd.agent_state({"state": "working", "state_at": None})[0] == "working"
    assert qd.agent_state({"state": "working", "state_at": "not a date"})[0] == "working"


# ---- the subscription's own limits -------------------------------------------

USAGE = {
    "limits": [
        {"kind": "session", "group": "session", "percent": 62, "severity": "normal",
         "resets_at": "2026-08-19T02:10:00+00:00", "scope": None, "is_active": True},
        {"kind": "weekly_all", "group": "weekly", "percent": 41, "severity": "normal",
         "resets_at": "2026-08-24T07:00:00+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 0, "severity": "normal",
         "resets_at": None, "scope": {"model": {"display_name": "Fable"}}, "is_active": False},
    ],
}


def test_the_two_headline_caps_are_named_by_their_window():
    got = qd.parse_limits(USAGE)
    assert [l["label"] for l in got] == ["5h", "7d"]
    assert [l["percent"] for l in got] == [62, 41]


def test_an_untouched_scoped_cap_is_not_worth_a_column():
    """Every model on the plan has one; listing them all buries the two being spent."""
    assert all(l["label"] != "Fable" for l in qd.parse_limits(USAGE))


def test_a_scoped_cap_being_spent_shows_up_under_its_model_name():
    usage = {"limits": [dict(USAGE["limits"][2], percent=18)]}
    got = qd.parse_limits(usage)
    assert [(l["label"], l["percent"]) for l in got] == [("Fable", 18)]


def test_a_headline_cap_at_zero_still_shows():
    """The start of a window is information; a blank line there is not."""
    usage = {"limits": [dict(USAGE["limits"][0], percent=0)]}
    assert [l["percent"] for l in qd.parse_limits(usage)] == [0]


def test_an_answer_with_no_limits_in_it_is_empty_rather_than_an_exception():
    assert qd.parse_limits({}) == []
    assert qd.parse_limits({"limits": [{"kind": "session", "percent": None}]}) == []


@pytest.fixture
def alone(monkeypatch, tmp_path):
    """A cache of this test's own.

    The cache is a FILE, shared by every dash pane on the machine, so a test that
    used the real one would read the developer's live figures and pass on them.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    return tmp_path


def test_no_token_is_nothing_to_show_and_not_a_failure(alone, monkeypatch, tmp_path):
    """An API-key install has no subscription caps. That is a missing line, not an error."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert qd.fetch_limits() == ([], None)


def test_a_failed_call_says_so_rather_than_reporting_zero_usage(alone, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-whatever")

    def boom(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(qd.urllib.request, "urlopen", boom)
    limits, err = qd.fetch_limits()
    assert limits == [] and err


def test_a_failure_keeps_the_last_good_figures_rather_than_emptying_the_line(alone, monkeypatch):
    """Minutes-old caps are still worth acting on. A line that vanished on a
    hiccup would read as 'no limits', which is the opposite of what it means."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-whatever")
    calls = _serve(monkeypatch, USAGE)
    assert [l["percent"] for l in qd.fetch_limits()[0]] == [62, 41]

    monkeypatch.setattr(qd, "LIMITS_EVERY", 0.0)       # the next call is allowed
    monkeypatch.setattr(qd.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
    limits, err = qd.fetch_limits()
    assert [l["percent"] for l in limits] == [62, 41], "the last good answer was dropped"
    assert err is None, "figures this fresh are not worth flagging"
    assert len(calls) == 1


def test_a_second_pane_asking_a_moment_later_reuses_the_answer(alone, monkeypatch):
    """Three seat screens are three dash processes. The endpoint rate-limits —
    it answered 429 while this was being built — so the interval is enforced in
    a file all three can see, not in each one's timer."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-whatever")
    calls = _serve(monkeypatch, USAGE)
    first, _ = qd.fetch_limits()
    second, err = qd.fetch_limits()
    assert [l["percent"] for l in second] == [l["percent"] for l in first]
    assert err is None
    assert len(calls) == 1, "the cached answer was not used"


def test_a_rate_limited_dash_backs_further_off_than_a_merely_failed_one(alone, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-whatever")

    def limited(*a, **k):
        raise qd.urllib.error.HTTPError(qd.USAGE_URL, 429, "slow down", {}, None)

    monkeypatch.setattr(qd.urllib.request, "urlopen", limited)
    before = time.time()
    qd.fetch_limits()
    assert qd._read_cache()["next"] - before > qd.LIMITS_EVERY * 2


def test_the_bar_fills_in_proportion_and_never_reads_empty_when_spending_has_started():
    assert qd.limit_bar(0, 10) == "░" * 10
    assert qd.limit_bar(50, 10) == "█" * 5 + "░" * 5
    assert qd.limit_bar(100, 10) == "█" * 10
    assert qd.limit_bar(3, 10).startswith("█"), "3% must not look like 0%"


def test_the_colour_escalates_on_the_number_and_on_the_endpoints_own_severity():
    assert qd.limit_colour(20) == "green"
    assert qd.limit_colour(75) == "yellow"
    assert qd.limit_colour(95) == "red"
    assert qd.limit_colour(20, "warning") == "yellow"
    assert qd.limit_colour(20, "critical") == "red"


def test_a_weekly_reset_is_days_rather_than_a_three_digit_hour_count():
    from datetime import datetime, timedelta, timezone

    def ahead(**kw):
        return (datetime.now(timezone.utc) + timedelta(**kw)).isoformat()

    assert qd.limit_reset(ahead(days=5, hours=8, minutes=1)) == "5d8h"
    assert qd.limit_reset(ahead(hours=3, minutes=57, seconds=30)) == "3h57m"
    assert qd.limit_reset(ahead(minutes=44, seconds=30)) == "44m"
    assert qd.limit_reset("2026-01-01T00:00:00+00:00") == "now"     # long past
    assert qd.limit_reset(None) == ""


def test_the_line_gives_up_its_bars_before_it_overflows_a_narrow_pane():
    limits = qd.parse_limits(USAGE)
    wide = qd.limit_cells(limits, 78)
    narrow = qd.limit_cells(limits, 32)
    assert all(bar for _, bar, _, _, _ in wide)
    assert not any(bar for _, bar, _, _, _ in narrow)
    for cells, width in ((wide, 78), (narrow, 32)):
        line = "  ".join(" ".join(x for x in cell[:4] if x) for cell in cells)
        assert len(line) <= width, line


def test_a_pane_too_narrow_for_even_the_numbers_gets_no_line_at_all():
    assert qd.limit_cells(qd.parse_limits(USAGE), 12) == []
    assert qd.limit_cells([], 78) == []


def _serve(monkeypatch, payload: dict) -> list:
    """Answer the usage endpoint with `payload`, recording how often it is asked."""
    calls: list = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(*a, **k):
        calls.append(1)
        return Response(json.dumps(payload).encode())

    monkeypatch.setattr(qd.urllib.request, "urlopen", urlopen)
    return calls


# ---- when `gh` fails ---------------------------------------------------------

def _gh_failing(monkeypatch, stderr: str, code: int = 1) -> None:
    """Stand in for the `gh <kind> list` every PR and issue row comes from."""
    class Done:
        returncode = code
        stdout = ""

    Done.stderr = stderr
    monkeypatch.setattr(qd.subprocess, "run", lambda *a, **k: Done())


def test_a_failing_gh_reports_the_repo_and_what_it_said(monkeypatch):
    """One repo of three failing is reported, not fatal — so the line has to name
    which one, and the panels have no other place to say it."""
    _gh_failing(monkeypatch, "HTTP 403: Resource not accessible by integration\n")
    rows, err = qd.fetch_issues(["prisonblues/quarterback"])
    assert rows == []
    assert err == "quarterback: HTTP 403: Resource not accessible by integration"


def test_a_failing_gh_that_said_nothing_still_names_its_exit_code(monkeypatch):
    """`quarterback: ` and nothing after it is the one error a reader cannot act on.

    A non-zero exit with an empty stderr is rare and real — killed by a signal, or
    a failure `gh` wrote to stdout — and the fallback that covers it used to sit
    outside the f-string, where `or` tested a string holding `": "` and therefore
    never fired. This pins the fallback rather than the punctuation, which is the
    part that regressed.
    """
    _gh_failing(monkeypatch, "", code=2)
    rows, err = qd.fetch_issues(["prisonblues/quarterback"])
    assert rows == []
    assert err == "quarterback: gh exit 2"


# ---- the board client --------------------------------------------------------
#
# `qb-reconcile` is the first caller to WRITE through this class, and the write
# changed the read: `_request` was factored out for it. Nothing in this file
# exercised either — the only cover the POST had was a hand-rolled fake in
# test_qb_reconcile.py that never touches the real class, so nothing pinned the
# Authorization header being sent, the Content-Type, the JSON encoding of the body,
# or the empty-body branch, which is the one most likely to be wrong.


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client(monkeypatch, body: bytes, token: str = "s3cret") -> list:
    """A BoardClient whose urlopen answers `body`, recording the requests made."""
    sent: list = []

    def urlopen(req, *a, **k):
        sent.append(req)
        return _Response(body)

    monkeypatch.setattr(qd.urllib.request, "urlopen", urlopen)
    return qd.BoardClient(qd.BoardConfig("https://board.example", token, "host")), sent


def test_a_get_carries_the_bearer_token_and_returns_the_parsed_body(monkeypatch):
    client, sent = _client(monkeypatch, b'{"agents": []}')
    assert client.get("/active") == {"agents": []}
    assert sent[0].full_url == "https://board.example/active"
    assert sent[0].get_header("Authorization") == "Bearer s3cret"
    assert sent[0].get_method() == "GET"


def test_a_board_with_no_token_sends_no_authorization_header(monkeypatch):
    """An unauthenticated board is a configuration, not an error — and a literal
    `Bearer ` with nothing after it is a 401 that reads as a dead board."""
    client, sent = _client(monkeypatch, b"{}", token="")
    client.get("/active")
    assert sent[0].get_header("Authorization") is None


def test_a_post_sends_json_with_its_content_type_and_the_token(monkeypatch):
    client, sent = _client(monkeypatch, b'{"id": 4207}')
    assert client.post("/post", {"type": "finding", "summary": "hi"}) == {"id": 4207}
    req = sent[0]
    assert req.get_method() == "POST"
    assert req.get_header("Content-type") == "application/json"
    assert req.get_header("Authorization") == "Bearer s3cret"
    assert json.loads(req.data) == {"type": "finding", "summary": "hi"}


def test_a_post_whose_200_carries_no_body_is_not_an_error(monkeypatch):
    """The reason the empty-body branch exists at all: a write's 200 legitimately
    says nothing, and the caller wants an empty mapping rather than an exception."""
    client, _ = _client(monkeypatch, b"")
    assert client.post("/post", {"type": "status"}) == {}
    client, _ = _client(monkeypatch, b"   \n")
    assert client.post("/post", {"type": "status"}) == {}


def test_a_get_with_an_empty_body_raises_rather_than_reading_as_nothing_there(monkeypatch):
    """RED/GREEN: `_request` returned `{}` for an empty body on both verbs, so "the
    board said nothing" arrived at every dashboard as "the board said nothing is
    there" — a proxy's contentless 502, a 204 from a board mid-deploy, a truncated
    response. `qb-reconcile` would print "the plan agrees with GitHub and the board
    on everything checked" over a plan it never received, and `fetch_board` would
    render an empty fleet as a healthy one because nothing raised. The tolerance
    belongs to the write path alone."""
    client, _ = _client(monkeypatch, b"")
    with pytest.raises(json.JSONDecodeError):
        client.get("/plan")


def test_an_empty_get_reaches_fetch_board_as_an_error_rather_than_an_empty_fleet(monkeypatch):
    """The consumer side of the same thing: `fetch_board` sets `error` from an
    exception, so a `{}` would have left it None and drawn an empty fleet as a
    healthy one."""
    client, _ = _client(monkeypatch, b"")
    out = qd.fetch_board(client)
    assert out["error"] is not None
    assert out["agents"] == []


# ---- the pacing verdict (#275) -----------------------------------------------
#
# The caps were drawn and read by nothing. These pin the four answers, and in
# particular the two that are easy to get wrong in the direction that costs
# money: a ceiling nobody could read must not report as clear, and a window
# nearly spent must say so before something spends the rest of it.

def _cap(label="5h", percent=10, severity="normal", resets=None):
    return {"label": label, "percent": percent, "severity": severity, "resets": resets}


def test_a_ceiling_that_could_not_be_read_is_unknown_and_never_go():
    """RED/GREEN: the whole of #244 applied to the budget. A governor that reports
    `go` on figures it never obtained is worse than one that does not exist — it is
    believed. `unknown` is also not `hold`, because a dropped network is not a spent
    window and parking the fleet over one would be a claim about the wrong thing."""
    got = qd.pace(([], "HTTP 500"))
    assert got["verdict"] == "unknown"
    assert got["source"] == "unreadable"
    assert "HTTP 500" in got["reason"]


def test_no_token_is_go_and_says_why():
    """An API-key install has no subscription caps at all. That is a `go` with a
    reason, exactly as the dash's answer to the same state is one line fewer rather
    than an error — and it must not read as `unknown`, which means "there is a
    ceiling and I could not see it"."""
    got = qd.pace(([], None))
    assert got["verdict"] == "go"
    assert got["source"] == "absent"
    assert "no OAuth token" in got["reason"]


def test_a_cap_near_exhaustion_holds_and_carries_when_it_comes_back():
    """`hold` is a WAIT, not a stop. The resumption time is the fact that makes it
    survivable, so it travels with the verdict rather than being looked up again."""
    soon = (datetime.now(timezone.utc) + timedelta(minutes=47)).isoformat()
    got = qd.pace(([_cap(percent=96, resets=soon)], None))
    assert got["verdict"] == "hold"
    assert got["cap"] == "5h" and got["percent"] == 96
    assert 2700 <= got["resets_in_s"] <= 2820


def test_the_verdicts_are_the_bars_own_thresholds_and_its_severity_rule():
    """Not restated with numbers of their own: the display and the decision
    disagreeing about what 88% means is precisely the failure nobody can see."""
    assert qd.pace(([_cap(percent=69)], None))["verdict"] == "go"
    assert qd.pace(([_cap(percent=70)], None))["verdict"] == "slow"
    assert qd.pace(([_cap(percent=89)], None))["verdict"] == "slow"
    assert qd.pace(([_cap(percent=90)], None))["verdict"] == "hold"
    # The endpoint's own severity knows about caps this fleet has not modelled, so
    # it may escalate a percentage that looks comfortable.
    assert qd.pace(([_cap(percent=4, severity="critical")], None))["verdict"] == "hold"
    assert qd.pace(([_cap(percent=4, severity="warning")], None))["verdict"] == "slow"


def test_the_binding_cap_is_the_worst_one_rather_than_the_first():
    """A 7d window at 12% does not buy back a 5h window at 94%. The reported cap is
    the one about to stop the work, whatever order the endpoint listed them in."""
    got = qd.pace(([_cap("7d", 12), _cap("5h", 94)], None))
    assert (got["verdict"], got["cap"]) == ("hold", "5h")
    # Same verdict on both: the one nearer its ceiling is the one to name.
    got = qd.pace(([_cap("7d", 72), _cap("5h", 81)], None))
    assert (got["verdict"], got["cap"]) == ("slow", "5h")


def test_figures_that_could_not_be_refreshed_lose_the_right_to_say_go():
    """A `go` on figures nobody could refresh is the one verdict that can be
    confidently wrong about a window that emptied while the network was down."""
    got = qd.pace(([_cap(percent=8)], "stale"))
    assert got["verdict"] == "slow"
    assert got["source"] == "stale"
    assert "could not be refreshed" in got["reason"]


def test_stale_figures_do_not_promote_a_slow_into_a_hold():
    """Staleness is uncertainty about the number. Parking work over a network
    hiccup would be a claim about the window made on the strength of the weather."""
    got = qd.pace(([_cap(percent=75)], "stale"))
    assert got["verdict"] == "slow"
    assert qd.pace(([_cap(percent=95)], "stale"))["verdict"] == "hold"


def test_pacing_asks_the_endpoint_no_more_often_than_the_dashboard_does(alone, monkeypatch):
    """The acceptance criterion in as many words: a verdict must never cost an extra
    call. It comes off the same machine-wide cache behind the same three-minute
    floor, so a fleet that consulted it before every unit of work would not be the
    thing that gets the endpoint to answer 429."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-whatever")
    calls = _serve(monkeypatch, USAGE)
    first = qd.pace()
    for _ in range(20):
        qd.pace()
    assert len(calls) == 1, "pacing asked the usage endpoint again"
    assert first["cap"] == "5h" and first["percent"] == 62


def test_the_line_names_the_verdict_and_the_reset_it_carries():
    # Half a minute of slack: the countdown floors to whole minutes, so a bare 47
    # renders as 46 the instant the clock has moved at all.
    soon = (datetime.now(timezone.utc) + timedelta(minutes=47, seconds=30)).isoformat()
    line = qd.pace_line(qd.pace(([_cap(percent=96, resets=soon)], None)))
    assert line.startswith("pace: HOLD — 5h at 96%")
    assert "resets in 47m" in line
    # Nothing to wait for at `go`, so nothing about waiting.
    assert "resets" not in qd.pace_line(qd.pace(([_cap(percent=4)], None)))


# ---- what a job costs, and the fit this deliberately will not predict ---------

def _stats(rows: list[dict]) -> bytes:
    return json.dumps({"by_model": rows}).encode()


def test_only_the_seats_billing_to_this_subscription_are_counted(monkeypatch):
    """RED/GREEN: the five-hour and weekly caps are the ANTHROPIC subscription's.
    `codex` bills to OpenAI, `antigravity` to a Google account and `pi` to
    OpenRouter, so counting a four-seat panel as four seats of pressure on this
    window overstates it by the three seats that are not on it."""
    client, _ = _client(monkeypatch, _stats([
        {"reviewer": "claude", "model": "opus", "total_tokens": 2_000_000,
         "billable_runs": 5},
        {"reviewer": "codex", "model": "gpt-5.6-luna", "total_tokens": 9_000_000,
         "billable_runs": 5},
    ]))
    cost, err = qd.subscription_cost(client)
    assert err is None
    assert cost["tokens_per_run"] == 400_000, "a seat on another vendor was counted"
    assert cost["runs"] == 5


def test_the_average_is_over_runs_rather_than_over_groups(monkeypatch):
    """The groups are (reviewer, model, effort) and they have wildly different run
    counts, so a mean of the rows' own means weights one opus run like forty
    sonnet ones."""
    client, _ = _client(monkeypatch, _stats([
        {"reviewer": "claude", "model": "opus", "total_tokens": 900_000, "billable_runs": 1},
        {"reviewer": "claude", "model": "sonnet", "total_tokens": 900_000, "billable_runs": 9},
    ]))
    cost, _ = qd.subscription_cost(client)
    assert cost["tokens_per_run"] == 180_000


def test_a_board_with_no_token_history_says_so_rather_than_estimating_zero(monkeypatch):
    client, _ = _client(monkeypatch, _stats([
        {"reviewer": "claude", "model": "opus", "total_tokens": None, "billable_runs": 0},
    ]))
    cost, err = qd.subscription_cost(client)
    assert cost is None
    assert "no measured token history" in err


def test_a_board_that_will_not_answer_is_reported_rather_than_raised(monkeypatch):
    def boom(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(qd.urllib.request, "urlopen", boom)
    client = qd.BoardClient(qd.BoardConfig("https://board.example", "t", "host"))
    cost, err = qd.subscription_cost(client)
    assert cost is None and "did not answer" in err


def test_the_estimate_states_the_job_and_the_headroom_and_refuses_the_fit():
    """The two halves are measured and the multiplication between them is not.
    Nothing records how much of a five-hour window a seat-run actually spends —
    that is #275's own first sequencing step, and it belongs to whatever drives the
    run. A fit predicted from a rate nobody measured would arrive in the same
    sentence as two real numbers and be believed."""
    verdict = qd.pace(([_cap(percent=62)], None))
    est = qd.pace_estimate(verdict, {"tokens_per_run": 283_795, "runs": 45}, seats=5, rounds=2)
    assert est["tokens"] == 283_795 * 10
    assert est["headroom_pct"] == 38
    assert est["fits"] is None
    assert "without guessing" in est["why"]


def test_an_estimate_with_no_history_is_no_number_rather_than_a_guess():
    est = qd.pace_estimate(qd.pace(([_cap(percent=62)], None)), None, seats=4)
    assert est["tokens"] is None and est["per_run"] is None
    assert est["headroom_pct"] == 38, "the half that IS known is still reported"


# ---- the review queue (#273) -------------------------------------------------
#
# The dashboard is where "six of eight open PRs have never been panelled" was
# supposed to be visible and was not. These pin the two halves that make it so:
# the PR rows go to the board unchanged (so nothing translates on the way), and a
# board that cannot be reached reports as unreachable rather than as an empty
# queue.

@pytest.mark.parametrize("secs,expected", [
    (0, "0m"),
    (90, "1m"),
    (3600, "1h00m"),
    (22_200, "6h10m"),
    (216_000, "2d12h"),
    (1_000_000, "11d13h"),
    (None, ""),
    ("nonsense", ""),
])
def test_a_wait_is_reported_in_days_once_it_is_days(secs, expected):
    """`ago` stops at hours, and this queue's whole complaint is measured in days:
    "60h13m" is a number a reader has to convert before it means anything."""
    assert qd.waited(secs) == expected


def test_the_pr_rows_reach_the_board_in_githubs_own_words(monkeypatch):
    client, sent = _client(monkeypatch, json.dumps({
        "counts": {"open": 1, "drainable": 1},
        "oldest": {"pr": 9, "age_seconds": 216_000},
        "entries": [{"pr": 9, "state": "unreviewed", "drainable": True,
                     "age_seconds": 216_000}],
        "idle_reason": None,
    }).encode())
    prs = [{"number": 9, "title": "a pr", "headRefOid": "f" * 40,
            "mergeable": "MERGEABLE", "createdAt": "2026-08-17T00:00:00Z",
            "isDraft": False, "statusCheckRollup": [{"noise": "x"} for _ in range(50)],
            "repo": "acme/one"}]

    queue = qd.fetch_review_queue(client, prs, repos=["acme/one"])
    body = json.loads(sent[0].data.decode())
    assert body["repo"] == "acme/one"
    # GitHub's own field names, so nothing has to translate on the way in...
    assert body["prs"] == [{"number": 9, "title": "a pr", "headRefOid": "f" * 40,
                            "mergeable": "MERGEABLE", "createdAt": "2026-08-17T00:00:00Z",
                            "isDraft": False}]
    # ...and the check rollup, which is most of the row and none of the question,
    # does not travel.
    assert "statusCheckRollup" not in body["prs"][0]
    assert queue["depth"] == 1 and queue["open"] == 1
    assert queue["entries"][0]["repo"] == "acme/one"
    assert queue["oldest"]["age_seconds"] == 216_000


def test_a_repo_with_no_open_prs_is_still_asked(monkeypatch):
    """Otherwise a repo whose queue drained and a repo nobody asked about are the
    same blank, which is #244's confusion in the one panel built to end it."""
    client, sent = _client(monkeypatch, json.dumps({
        "counts": {"open": 0, "drainable": 0}, "entries": [], "oldest": None,
        "idle_reason": "no open pull requests were supplied for this repo"}).encode())
    queue = qd.fetch_review_queue(client, [], repos=["acme/quiet"])
    assert len(sent) == 1
    assert queue["open"] == 0 and queue["depth"] == 0
    assert "no open pull requests" in queue["idle"]


def test_a_board_that_cannot_be_reached_is_a_state_not_an_exception(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(qd.urllib.request, "urlopen", boom)
    client = qd.BoardClient(qd.BoardConfig("https://board.example", "t", "host"))
    queue = qd.fetch_review_queue(client, [], repos=["acme/one"])
    assert queue["error"] and "acme" not in queue["error"].split(":")[0]
    assert queue["entries"] == []
    # And it must NOT read as a drained queue on the header line.
    assert qd.queue_cell(queue)[1] == "?"


def test_one_failing_repo_does_not_cost_the_others_their_queue(monkeypatch):
    answers = {
        "acme/good": {"counts": {"open": 2, "drainable": 2},
                      "entries": [{"pr": 1, "drainable": True, "age_seconds": 10},
                                  {"pr": 2, "drainable": True, "age_seconds": 20}],
                      "oldest": {"pr": 2, "age_seconds": 20}, "idle_reason": None},
    }

    def urlopen(req, *a, **k):
        repo = json.loads(req.data.decode())["repo"]
        if repo not in answers:
            raise OSError("no")
        return _Response(json.dumps(answers[repo]).encode())

    monkeypatch.setattr(qd.urllib.request, "urlopen", urlopen)
    client = qd.BoardClient(qd.BoardConfig("https://board.example", "t", "host"))
    queue = qd.fetch_review_queue(client, [], repos=["acme/good", "acme/bad"])
    assert queue["depth"] == 2
    assert "bad" in queue["error"]
    # Oldest first, and the drainable ones ahead of the held ones.
    assert [e["pr"] for e in queue["entries"]] == [2, 1]


def test_a_queue_where_everything_is_stuck_still_reports_its_age():
    """Reporting only the drainable wait renders a repo whose every PR has been
    blocked for five days as a queue with no age at all — the reading this panel
    exists to surface, hidden by the panel built to surface it."""
    stuck = {"open": 4, "depth": 0, "oldest": None, "error": None,
             "oldest_held": {"pr": 270, "age_seconds": 432_000}}
    assert qd.queue_oldest(stuck) == ("5d00h", True)
    assert qd.queue_cell(stuck) == ("REVIEW", "0 waiting", "held 5d00h", "green")
    live = {"open": 4, "depth": 2, "oldest": {"age_seconds": 3600},
            "oldest_held": {"age_seconds": 432_000}, "error": None}
    assert qd.queue_oldest(live) == ("1h00m", False)


def test_the_header_cell_tells_an_empty_queue_from_an_unasked_one():
    empty = qd.queue_cell({"open": 3, "depth": 0, "oldest": None, "error": None})
    assert empty == ("REVIEW", "0 waiting", "", "green")
    deep = qd.queue_cell({"open": 9, "depth": 6,
                          "oldest": {"age_seconds": 216_000}, "error": None})
    assert deep[1] == "6 waiting" and deep[2] == "oldest 2d12h" and deep[3] == "red"


def test_the_entries_are_sorted_for_reading_and_the_held_ones_keep_their_place():
    """A panel that hid its blocked entries would report a depth of zero for a
    repo where everything is stuck — which is the reading this exists to end."""
    answer = {"counts": {"open": 3, "drainable": 1},
              "entries": [{"pr": 1, "drainable": False, "age_seconds": 900_000},
                          {"pr": 2, "drainable": True, "age_seconds": 100},
                          {"pr": 3, "drainable": False, "age_seconds": 50}],
              "oldest": {"pr": 2, "age_seconds": 100}, "idle_reason": None}

    class C:
        def post(self, path, body):
            return answer

    queue = qd.fetch_review_queue(C(), [], repos=["acme/one"])
    assert [e["pr"] for e in queue["entries"]] == [2, 1, 3]
    assert queue["depth"] == 1 and queue["open"] == 3
    assert queue["idle"] is None, "a queue with depth is not idle"


def test_a_pr_list_that_failed_is_not_an_empty_pr_list(monkeypatch):
    """`gh` failing gives `([], err)`, and sending that empty list would have the
    board honestly answer "no open pull requests" for a repo with eight waiting."""
    asked: list = []

    class C:
        def post(self, path, body):
            asked.append(body)
            return {"counts": {"open": 0, "drainable": 0}, "entries": [],
                    "oldest": None, "idle_reason": "no open pull requests"}

    queue = qd.fetch_review_queue(C(), [], repos=["acme/one"], pr_err="one: gh exit 1")
    assert asked == [], "nothing may be derived from a listing nobody could read"
    assert "pr list unavailable" in queue["error"]
    assert qd.queue_cell(queue)[1] == "?"


def test_an_empty_board_response_is_an_error_and_not_an_empty_queue():
    """`BoardClient.post` reads an empty body as `{}` because a WRITE's 200 may
    carry none. This is a read through that method, so the tolerance is undone."""
    class C:
        def post(self, path, body):
            return {}

    queue = qd.fetch_review_queue(C(), [], repos=["acme/one"])
    assert "empty answer" in queue["error"]
    assert queue["open"] == 0 and queue["entries"] == []
    assert qd.queue_cell(queue)[1] == "?"


def test_one_repos_failed_listing_does_not_cost_the_others_their_queue(monkeypatch):
    """The conservative fix for "an empty list is not an empty repo" was to refuse
    every repo, which throws away two good queues because a third's token expired."""
    asked: list = []

    class C:
        def post(self, path, body):
            asked.append(body["repo"])
            return {"counts": {"open": 1, "drainable": 1},
                    "entries": [{"pr": 5, "drainable": True, "age_seconds": 60}],
                    "oldest": {"pr": 5, "age_seconds": 60}, "idle_reason": None}

    queue = qd.fetch_review_queue(
        C(), [{"number": 5, "repo": "acme/good"}],
        repos=["acme/good", "acme/bad"], pr_err="bad: HTTP 401")
    assert asked == ["acme/good"]
    assert queue["depth"] == 1
    assert "bad: pr list unavailable" in queue["error"]


def test_an_unattributable_listing_error_makes_every_repo_suspect():
    class C:
        def post(self, path, body):     # pragma: no cover - must not be reached
            raise AssertionError("nothing may be derived")

    queue = qd.fetch_review_queue(C(), [], repos=["acme/one"], pr_err="something broke")
    assert "something broke" in queue["error"]
    assert qd.queue_cell(queue)[1] == "?"


def test_the_error_format_and_the_parse_of_it_agree(monkeypatch):
    """`gh_error_repos` reads the string `_gh_list` writes. Pinned together,
    because a drift between them silently restores the bug above."""
    class Done:
        returncode = 1
        stdout = ""
        stderr = "HTTP 401: Bad credentials"

    monkeypatch.setattr(qd.subprocess, "run", lambda *a, **k: Done())
    rows, err = qd.fetch_prs(["acme/alpha", "acme/beta"])
    assert rows == []
    assert qd.gh_error_repos(err) == {"alpha", "beta"}


def test_a_repo_at_the_fetch_limit_reports_its_depth_as_a_floor():
    """"60 open PRs" and "at least 60" are different facts, and only one is a depth."""
    class C:
        def post(self, path, body):
            return {"counts": {"open": qd.PR_LIMIT, "drainable": qd.PR_LIMIT},
                    "entries": [], "oldest": None, "idle_reason": None}

    prs = [{"number": n, "repo": "acme/busy"} for n in range(qd.PR_LIMIT)]
    queue = qd.fetch_review_queue(C(), prs, repos=["acme/busy"])
    assert queue["depth"] == qd.PR_LIMIT
    assert "depth is a floor" in queue["error"]


def test_two_watched_repos_with_one_short_name_are_both_withheld(monkeypatch):
    """`org1/api` and `org2/api` produce the same error prefix, so while a listing
    error is outstanding neither can be shown to have succeeded. A third repo with
    a name of its own is unaffected."""
    asked: list = []

    class C:
        def post(self, path, body):
            asked.append(body["repo"])
            return {"counts": {"open": 0, "drainable": 0}, "entries": [],
                    "oldest": None, "idle_reason": None}

    queue = qd.fetch_review_queue(
        C(), [], repos=["org1/api", "org2/api", "org3/web"], pr_err="api: HTTP 401")
    assert asked == ["org3/web"]
    assert "org1/api: which `api` failed is ambiguous" in queue["error"]
    assert "org2/api" in queue["error"]


def test_a_shared_short_name_costs_nothing_when_no_listing_failed():
    class C:
        def post(self, path, body):
            return {"counts": {"open": 0, "drainable": 0}, "entries": [],
                    "oldest": None, "idle_reason": None}

    queue = qd.fetch_review_queue(C(), [], repos=["org1/api", "org2/api"])
    assert queue["error"] is None


# ---- what the checks actually say (#324) --------------------------------------
#
# `gh pr checks 282` printed nothing for two days and every reader took that for
# "CI has not run yet". CI had run and gone RED; the two commits pushed to fix it
# came back `action_required` — GitHub's workflow-approval gate — so they executed
# nothing, contributed no check runs, and the check list went empty. Absent read as
# benign. These pin the six answers apart.


@pytest.fixture(autouse=True)
def _no_probe_cache(monkeypatch):
    """A module-level TTL cache would otherwise leak one test's answer into the next.

    `raising=False` so this fixture survives a qbdata without the cache — which is
    what the red half of red/green runs these against.
    """
    monkeypatch.setattr(qd, "_ci_cache", {}, raising=False)


def _run(conclusion="success", status="completed", sha="0123456789abcdef"):
    return {"conclusion": conclusion, "status": status, "head_sha": sha}


def _pr(rollup=None, **over):
    body = {"repo": "o/r", "headRefOid": "a" * 40, "headRefName": "feat/x",
            "statusCheckRollup": rollup if rollup is not None else []}
    return {**body, **over}


def _stub_runs(monkeypatch, head=(), branch=(), err=None):
    """Answer the head-sha lookup and the branch-history lookup separately.

    Which one is being asked is the `sha` argument: `ci_report` passes it for the
    head's own runs and passes `branch=` for the history it reads the last executed
    conclusion out of.
    """
    head_runs, branch_runs = list(head), list(branch)

    def fake(repo, sha="", branch=""):
        if err:
            return [], err
        return (head_runs if sha else branch_runs), None

    monkeypatch.setattr(qd, "workflow_runs", fake, raising=False)


@pytest.mark.parametrize("rollup,state", [
    ([{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}], "green"),
    ([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}], "red"),
    ([{"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"}], "pending"),
    ([{"conclusion": "ACTION_REQUIRED"}], "blocked"),
    ([{"conclusion": "STALE"}], "blocked"),
])
def test_the_rollup_answers_four_of_the_six_on_its_own(rollup, state):
    assert qd.classify_rollup(rollup) == state


def test_an_empty_rollup_is_unknown_and_never_none():
    """The single most important line in the module. An empty rollup is either an
    untested head or a run so gated it contributed nothing, and this function cannot
    tell them apart — so it must not answer with the one that reads as benign.
    `none` is reachable only by ASKING, in ci_report."""
    assert qd.classify_rollup([]) == "unknown"
    assert qd.classify_rollup(None) == "unknown"
    assert "none" not in {qd.classify_rollup(r) for r in ([], None, [{}])}


def test_a_gated_run_is_reported_as_blocked_with_the_last_executed_conclusion(monkeypatch):
    """#324's sequence, replayed. The head's own runs are `action_required` and the
    branch's newest EXECUTED run is a failure two commits back — the fact that was two
    clicks away in a place nobody looks."""
    _stub_runs(monkeypatch,
               head=[_run(conclusion="action_required")],
               branch=[_run(conclusion="action_required"),
                       _run(conclusion="failure", sha="843c506aaaa")])
    rep = qd.ci_report(_pr(), "o/r")
    assert rep.state == "blocked" and rep.blocking
    assert rep.last_executed == "failure at 843c506"
    assert "waiting on a human" in rep.reason
    assert "failure at 843c506" in rep.reason
    assert qd.ci_state({"ci": rep}) == qd.CI_GLYPHS["blocked"]


def test_a_head_with_no_run_at_all_is_none_and_says_it_is_untested(monkeypatch):
    _stub_runs(monkeypatch, head=[], branch=[])
    rep = qd.ci_report(_pr(), "o/r")
    assert rep.state == "none" and rep.blocking
    assert "untested" in rep.reason


def test_a_failed_probe_is_unknown_and_not_none(monkeypatch):
    """qb-reconcile's Unknown, one module over: "I looked and found nothing" and "I
    could not look" are different answers, and the whole complaint behind #244 and
    #255 is consumers that report the second as the first."""
    _stub_runs(monkeypatch, err="HTTP 502")
    rep = qd.ci_report(_pr(), "o/r")
    assert rep.state == "unknown" and rep.blocking
    assert "502" in rep.reason


def test_a_probe_is_never_sent_to_the_fallback_repo(monkeypatch):
    """REPO is the dashboard's placeholder, not an answer. Probing it about another
    repo's commit would ask the wrong API and read the reply as fact."""
    def boom(*a, **k):
        raise AssertionError("a probe was sent with no repo named")
    monkeypatch.setattr(qd, "workflow_runs", boom)
    rep = qd.ci_report({"headRefOid": "b" * 40, "statusCheckRollup": []})
    assert rep.state == "unknown" and "no repository was named" in rep.reason


def test_completed_runs_that_contributed_no_checks_are_not_green(monkeypatch):
    """The one direction this must never fail in. A head whose runs all completed
    while producing no check runs is a head nothing verified."""
    _stub_runs(monkeypatch, head=[_run(conclusion="success")], branch=[])
    rep = qd.ci_report(_pr(), "o/r")
    assert rep.state == "unknown"
    assert "not a pass" in rep.reason


def test_probe_false_leaves_the_empty_case_unknown_and_fetches_nothing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("probe=False still reached the network")
    monkeypatch.setattr(qd, "workflow_runs", boom)
    rep = qd.ci_report(_pr(), "o/r", probe=False)
    assert rep.state == "unknown"


def test_the_probe_answer_is_cached_so_a_redraw_costs_nothing(monkeypatch):
    calls = []

    def counted(repo, sha="", branch=""):
        calls.append(sha or branch)
        return [], None
    monkeypatch.setattr(qd, "workflow_runs", counted)
    for _ in range(3):
        qd.ci_report(_pr(), "o/r")
    assert len(calls) == 2, calls


def test_every_state_has_a_glyph_and_only_none_is_quiet():
    """The grey dot is the one rendering that reads as "nothing to see", so it must
    belong to exactly one state — the one that was actually established by asking."""
    assert set(qd.CI_GLYPHS) == set(qd.CI_STATES)
    quiet = [s for s, (_g, colour) in qd.CI_GLYPHS.items() if colour == "grey50"]
    assert quiet == ["none"]


def test_an_unresolved_pr_renders_as_unread_rather_than_as_a_quiet_dot():
    """A row nobody probed. Before #324 this was the grey dot that meant "no news"."""
    assert qd.ci_state({"statusCheckRollup": []}) != ("·", "grey50")
    assert qd.ci_state({"statusCheckRollup": []}) == qd.CI_GLYPHS["unknown"]


def test_ci_counts_names_every_state_a_panel_title_has_to_show():
    prs = [{"ci": qd.CiReport("green", "g")}, {"ci": qd.CiReport("blocked", "b")},
           {"statusCheckRollup": []}]
    counts = qd.ci_counts(prs)
    assert counts["green"] == 1 and counts["blocked"] == 1 and counts["unknown"] == 1
    assert set(counts) >= set(qd.CI_STATES)


@pytest.mark.parametrize("status", ["requested", "queued", "in_progress"])
def test_a_run_on_its_way_to_starting_is_pending_and_not_blocked(monkeypatch, status):
    """`requested` is GitHub's word for a run created and not yet started, so calling
    it gated would say a human is needed when nobody is — a false alarm in the
    direction that teaches people to stop reading the alarms. Found by Codex."""
    _stub_runs(monkeypatch, head=[_run(conclusion=None, status=status)], branch=[])
    assert qd.ci_report(_pr(), "o/r").state == "pending"


def test_a_check_run_that_has_only_been_requested_is_pending():
    assert qd.classify_rollup([{"status": "REQUESTED"}]) == "pending"


def test_a_200_that_is_not_the_runs_document_is_an_error_and_not_an_empty_list(monkeypatch):
    """A reply nobody understood must not settle the state as `none`. "No workflow
    runs" and "the lookup did not happen" are the two answers this whole module
    exists to keep apart."""
    monkeypatch.setattr(qd, "_gh_api", lambda path, timeout=30: ({"message": "?"}, None))
    runs, err = qd.workflow_runs("o/r", sha="a" * 40)
    assert runs == [] and err


def test_the_cache_does_not_grow_without_bound(monkeypatch):
    """A dashboard runs for days and every push gives a PR a new head."""
    monkeypatch.setattr(qd, "CI_CACHE_MAX", 4)
    monkeypatch.setattr(qd, "workflow_runs", lambda repo, sha="", branch="": ([], None))
    for n in range(20):
        qd.ci_report(_pr(headRefOid=f"{n:040d}"), "o/r")
    assert len(qd._ci_cache) <= 4


# ---- putting the plan in order (#443) ----------------------------------------
#
# The arithmetic only, here: what a move DOES to a list. The keys, the modal and
# the write are `test_qb_dash.py`'s, because those need a running app and this
# does not — and the array is the half both renderers and every verb share.

REORDER_PLAN = {"items": [
    {"item_id": "a", "repo": "acme/one", "state": "open", "title": "first"},
    {"item_id": "b", "repo": "acme/one", "state": "open", "title": "second"},
    {"item_id": "c", "repo": "acme/one", "state": "open", "title": "third"},
    {"item_id": "d", "repo": "acme/two", "state": "open", "title": "another plan"},
    {"item_id": "e", "repo": None, "state": "open", "title": "fleet-wide"},
    {"item_id": "f", "repo": "acme/one", "state": "dropped", "title": "gone"},
], "truncated": False}


def test_a_scope_is_exact_and_a_null_one_is_a_real_scope():
    """`POST /plan/reorder` renumbers ONE exact scope, so the list handed to it has
    to be that scope and nothing else — a loose match would renumber another
    plan's items behind the caller's back.

    Not `plan_repo`: that resolves a free-text repo to an `owner/name` a `gh`
    command can use, and answers None for a fleet-wide item and a `project:` scope
    alike — so grouping by it would put three scopes in one bucket.
    """
    assert [i["item_id"] for i in qd.plan_scope_items(REORDER_PLAN, "acme/one")] \
        == ["a", "b", "c"], "the exact scope picked up another plan's items"
    assert [i["item_id"] for i in qd.plan_scope_items(REORDER_PLAN, None)] == ["e"], \
        "the fleet-wide scope is a real scope, not a missing one"
    assert [i["item_id"] for i in qd.plan_scope_items(REORDER_PLAN, "acme/two")] == ["d"]


def test_only_open_items_have_a_place():
    """A dropped item is not in the order and the endpoint refuses it by id: `order
    is for open work: finished and dropped items keep no place`."""
    assert "f" not in [i["item_id"] for i in qd.plan_scope_items(REORDER_PLAN, "acme/one")]


@pytest.mark.parametrize("how,expected", [
    ("up",     ["a", "c", "b", "d"]),
    ("down",   ["a", "b", "d", "c"]),
    ("top",    ["c", "a", "b", "d"]),
    ("bottom", ["a", "b", "d", "c"]),
    # Clamped and never wrapped, which is the web board's rule kept identical: a
    # jump past the end lands on the end rather than reappearing at the other one.
    ("up5",    ["c", "a", "b", "d"]),
    ("down5",  ["a", "b", "d", "c"]),
])
def test_a_verb_is_an_index_and_the_ends_clamp(how, expected):
    ids = ["a", "b", "c", "d"]
    assert qd.reorder_ids(ids, ["c"], qd.nudge_index(ids, ["c"], how)) == expected


def test_a_move_that_changes_nothing_is_not_a_move():
    """It must not be SENT, and that is not tidiness: the endpoint stamps
    `rank_source` on every item it is handed, so posting an unchanged order would
    write "a human chose this position" onto rows nobody touched (#183)."""
    ids = ["a", "b", "c"]
    assert qd.reorder_ids(ids, ["a"], 0) is None
    assert qd.reorder_ids(ids, ["a"], qd.nudge_index(ids, ["a"], "up")) is None
    assert qd.reorder_ids(ids, ["c"], qd.nudge_index(ids, ["c"], "bottom")) is None
    assert qd.reorder_ids(ids, ["zz"], 0) is None, "an id not in the list moved nothing"


def test_marked_rows_move_together_and_keep_their_order():
    """"These three, as you see them, starting there" — any other answer would mean
    the mark had also to record a sequence, which is a second order to keep in step
    with the board's."""
    ids = list("abcdef")
    assert qd.reorder_ids(ids, ["b", "d"], 3) == ["a", "c", "e", "b", "d", "f"]
    # And a block nudge measures from where the block STARTS, against the list it
    # is being put back into — measuring against one that still contains the moved
    # rows is how a one-place nudge becomes a no-op.
    assert qd.reorder_ids(ids, ["b", "c"], qd.nudge_index(ids, ["b", "c"], "down")) \
        == ["a", "d", "b", "c", "e", "f"]


def test_a_reorder_that_cannot_be_computed_says_why():
    """Every refusal names the remedy: the alternative on a 78-column pane is a
    control that does nothing and a person who cannot tell a rule from a bug."""
    plan_row = {"kind": "plan", "item": {"repo": "acme/one"}}
    other = {"kind": "plan", "item": {"repo": "acme/two"}}
    assert "nothing selected" in qd.reorder_refusal(REORDER_PLAN, [])
    assert "only the plan has an order" in qd.reorder_refusal(
        REORDER_PLAN, [{"kind": "pr"}])
    assert "different plans" in qd.reorder_refusal(REORDER_PLAN, [plan_row, other])
    assert qd.reorder_refusal(REORDER_PLAN, [plan_row]) is None


def test_a_truncated_plan_is_refused_rather_than_reordered_from():
    """The endpoint renumbers every open item in the scope and appends the ones the
    caller did not list. An order computed from a partial list would therefore move
    everything the pane was never sent — silently, and in one request."""
    why = qd.reorder_refusal({**REORDER_PLAN, "truncated": True},
                             [{"kind": "plan", "item": {"repo": "acme/one"}}])
    assert why and "truncated" in why and "board page" in why


OPTIMISTIC_PLAN = {"items": [
    {"item_id": "a", "repo": "acme/one", "state": "open", "rank": 1,
     "rank_source": "ordered", "title": "A"},
    {"item_id": "z1", "repo": "acme/two", "state": "open", "rank": 1,
     "rank_source": "ordered", "title": "Z1"},
    {"item_id": "b", "repo": "acme/one", "state": "open", "rank": 3,
     "rank_source": "appended", "title": "B"},
    {"item_id": "z2", "repo": "acme/two", "state": "open", "rank": 2,
     "rank_source": "ordered", "title": "Z2"},
    {"item_id": "c", "repo": "acme/one", "state": "open", "rank": 9,
     "rank_source": "appended", "title": "C"},
], "next": {"item_id": "a"}, "counts": {"open": 5}, "truncated": False}


def test_an_optimistic_reorder_renumbers_the_way_the_endpoint_will():
    """A row drawn in its new place still carrying its old rank is a table
    disagreeing with itself, so the local guess renumbers `1..n` and stamps
    `ordered` exactly as `POST /plan/reorder` does."""
    out = qd.plan_reordered(OPTIMISTIC_PLAN, "acme/one", ["c", "a", "b"])
    ours = [i for i in out["items"] if i["repo"] == "acme/one"]
    assert [(i["item_id"], i["rank"]) for i in ours] == [("c", 1), ("a", 2), ("b", 3)]
    assert {i["rank_source"] for i in ours} == {"ordered"}


def test_reordering_one_scope_leaves_every_other_row_where_it_was():
    """Sorting the whole list by "is this the scope being reordered" put every one
    of that scope's rows above every other scope's — so reordering one project
    silently hoisted all of it over another's in the wide view, which is a change
    to rows the caller never named. The permutation happens inside the positions
    the scope already occupies."""
    out = qd.plan_reordered(OPTIMISTIC_PLAN, "acme/one", ["c", "a", "b"])
    where = {i["item_id"]: n for n, i in enumerate(out["items"])}
    assert where["z1"] == 1 and where["z2"] == 3, \
        f"another scope's rows moved: {[i['item_id'] for i in out['items']]}"
    assert [i["item_id"] for i in out["items"] if i["repo"] == "acme/two"] == ["z1", "z2"]


def test_an_optimistic_reorder_drops_next_rather_than_guessing_it():
    """`next` is the board's answer to "what should somebody take", worked out from
    ranks, claims, blocks and cover. A pane that computed its own would be the
    second-answer-about-the-plan defect this module has spent three issues
    removing — and a ◉ left on a row that has just moved is not honest either."""
    out = qd.plan_reordered(OPTIMISTIC_PLAN, "acme/one", ["c", "a", "b"])
    assert out["next"] is None
    # Everything the board did not have to recompute is carried through: a reorder
    # does not change which items are open.
    assert out["counts"] == {"open": 5}
    assert out["truncated"] is False


# ---- what the chip bar offers, and what a chip does --------------------------
#
# Sixteen agents across three repos is a list you read rather than one you scan.
# These are the two decisions behind narrowing it, kept out of the renderer so
# that finding out which repos a chip bar would show does not require a Textual
# app to ask.


def test_the_chips_are_the_repos_the_rows_are_actually_in():
    """Not the repos the board knows, and not the ones this checkout watches. A
    chip with nobody behind it filters to an empty table — a control that can only
    disappoint."""
    rows = [{"repo": "quarterback"}, {"repo": "quarterback"}, {"repo": "lexray"}]
    assert qd.chip_repos(rows) == ["lexray", "quarterback"]


def test_one_repo_spelled_three_ways_is_one_chip():
    """A lease reports a bare `quarterback`; the plan and `gh` report
    `prisonblues/quarterback`. Two chips for one repo would be two filters that
    each hide half of it."""
    rows = [{"repo": "prisonblues/quarterback"}, {"repo": "quarterback"},
            {"repo": "Quarterback"}]
    assert qd.chip_repos(rows) == ["quarterback"]


def test_a_row_with_no_repo_offers_no_chip():
    assert qd.chip_repos([{"repo": None}, {"repo": ""}, {}]) == []


def test_the_chips_are_alphabetical_and_not_by_size():
    """The bar is a place your eye goes back to. An order that reshuffles whenever
    an agent starts or stops is one you have to re-read every tick."""
    rows = [{"repo": "zulu"}] * 5 + [{"repo": "alpha"}]
    assert qd.chip_repos(rows) == ["alpha", "zulu"]


def test_filtering_to_a_repo_keeps_its_rows_however_they_spell_it():
    rows = [{"repo": "prisonblues/quarterback", "n": 1}, {"repo": "quarterback", "n": 2},
            {"repo": "lexray", "n": 3}]
    assert [r["n"] for r in qd.only_repo(rows, "quarterback")] == [1, 2]


def test_no_filter_is_every_row_and_not_an_empty_one():
    rows = [{"repo": "quarterback"}, {"repo": "lexray"}]
    assert qd.only_repo(rows, None) == rows
    assert qd.only_repo(rows, "") == rows


def test_a_row_with_no_repo_is_dropped_by_a_filter_rather_than_kept():
    """It reads the other way round at first — an unknown repo is not a known
    mismatch. But a row that survives every filter is one the chip bar cannot
    explain, and the `N of M` beside the chip would count rows the chip does not
    describe."""
    rows = [{"repo": "quarterback"}, {"repo": None}]
    assert qd.only_repo(rows, "quarterback") == [{"repo": "quarterback"}]
