"""The joins and the parsing the dashboards share, tested without a dashboard.

qbdata is stdlib-only on purpose, so this runs anywhere `pytest` does — unlike
test_qb_dash.py, which wants textual and a configured board. The join between
board claims and `gh issue list` is the piece worth pinning: it decides which
issues the panel offers as free, and offering a held one sends two agents at the
same work.

Run: pytest harness/tests/test_qbdata.py
"""

from __future__ import annotations

import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))

import qbdata as qd                                       # noqa: E402


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


def test_running_items_come_first_and_blocked_ones_last():
    """The panel's whole order: what is happening, what is free, what is stuck."""
    items = [item("free-a"), item("blocked", blocked=[{"ref": "9"}]),
             item("running", holder="zeus/one"), item("free-b")]
    assert [i["title"] for i in qd.sort_plan(items, [qd.REPO])] == [
        "running", "free-a", "free-b", "blocked"]


def test_the_boards_own_order_survives_inside_a_band():
    """A plan is an ordered list; re-sorting it by anything else loses the point."""
    items = [item("first"), item("second"), item("third")]
    assert [i["title"] for i in qd.sort_plan(items, [qd.REPO])] == ["first", "second", "third"]


def test_a_watched_repos_items_come_before_a_repo_we_only_overhear():
    items = [item("theirs", repo="someone/other"), item("ours"),
             item("fleet-wide", repo=None)]
    assert [i["title"] for i in qd.sort_plan(items, [qd.REPO])] == [
        "ours", "fleet-wide", "theirs"]


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


def test_the_right_hand_column_holds_whichever_fact_is_true():
    assert qd.plan_who(item(holder="zeus/badger-ember"))[0] == "badger-ember"
    assert qd.plan_who(item(blocked=[{"ref": "9"}]))[0] == "waits #9"
    assert qd.plan_who(item(blocked=[{"ref": None}, {"ref": None}]))[0] == "waits ×2"


def test_a_blocked_item_is_not_also_counted_as_running():
    items = [item(holder="zeus/one"), item("b", blocked=[{"ref": "9"}]),
             item("c", holder="zeus/two", blocked=[{"ref": "9"}])]
    assert qd.plan_counts(items) == (2, 1)


def test_the_detail_line_carries_the_note_the_panel_cannot_fit():
    line = qd.plan_detail(item("short title", ref=8, holder="zeus/one",
                               phase="phase one", note="because the order matters"))
    assert "#8" in line and "phase one" in line
    assert "zeus/one" in line and "because the order matters" in line


def test_a_plan_claim_shows_the_item_it_holds_and_not_its_uuid():
    plan = [item("Give the annex a sloped roof", repo="65lowther", ref=None)]
    plan[0]["item_id"] = "ea9e1623"
    assert qd.claim_label("plan:ea9e1623", plan) == "plan Give the annex a sloped roof"


def test_an_unresolvable_plan_key_keeps_the_key():
    """A key nobody can look up still beats a blank cell."""
    assert qd.claim_label("plan:ea9e1623", []) == "plan:ea9e1623"


def test_an_ordinary_claim_key_is_still_shortened():
    assert qd.claim_label(f"{qd.REPO}#142", []) == "quarterback#142"
