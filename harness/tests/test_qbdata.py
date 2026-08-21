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

QB_SEAT = BIN / "qb-seat"


# ---- reading a seat off the board -------------------------------------------
#
# #208 put the project in the name, so `zeus/seat-1` became `zeus/seat-lexray-1`
# — and everything that joins a board identity to a tmux pane goes through these
# three. The old spelling has to keep working: a seat whose scope slugged away to
# nothing, or one deliberately started with an empty QB_SEAT_SCOPE, is still a
# seat and the dashboard still has to say so.


def test_a_scoped_seat_reads_as_its_number_and_its_project():
    assert qd.seat_number("zeus/seat-lexray-1") == 1
    assert qd.seat_scope("zeus/seat-lexray-1") == "lexray"


def test_the_number_is_the_last_field_not_the_first():
    """A scope may carry hyphens of its own, and `seat-nix-fleet-3` is seat 3 of
    nix-fleet rather than anything at all about `nix`."""
    assert qd.seat_number("zeus/seat-nix-fleet-3") == 3
    assert qd.seat_scope("zeus/seat-nix-fleet-3") == "nix-fleet"


def test_a_seat_numbered_across_the_machine_is_still_a_seat():
    """The pre-#208 spelling, which QB_SEAT_SCOPE= still asks for on purpose."""
    assert qd.seat_number("zeus/seat-7") == 7
    assert qd.seat_scope("zeus/seat-7") is None


def test_an_agent_that_is_not_a_seat_is_not_read_as_one():
    for holder in (None, "", "zeus", "zeus/amber-otter", "zeus/seat-", "zeus/seats-1",
                   "zeus/seat-lexray-0", "zeus/seat-lexray-100", "zeus/seat-lexray"):
        assert qd.seat_number(holder) is None, holder
        assert qd.seat_scope(holder) is None, holder


def test_a_machine_is_read_off_a_seat_identity():
    """The board is the FLEET's: two machines can each hold a `seat-lexray-1`, so
    the machine half is part of what identifies one."""
    assert qd.seat_machine("zeus/seat-lexray-1") == "zeus"
    assert qd.seat_machine("seat-lexray-1") is None
    assert qd.seat_machine("zeus/amber-otter") is None


def test_a_pane_answers_with_the_scope_it_was_told_before_the_one_it_implies():
    """Two screens on ONE repository is what QB_SEAT_SCOPE exists for, and it is
    exactly the case @qb_repo cannot distinguish."""
    assert qd.pane_scope({"repo": "/x/lexray", "scope": "review"}) == "review"
    assert qd.pane_scope({"repo": "/x/lexray", "scope": "Re View"}) == "re-view"
    assert qd.pane_scope({"repo": "/x/lexray", "scope": ""}) == "lexray"
    assert qd.pane_scope({"repo": "", "scope": ""}) is None
    assert qd.pane_scope({}) is None


def test_a_repository_path_becomes_the_scope_its_seats_carry():
    assert qd.scope_of("/home/rich/lexray") == "lexray"
    assert qd.scope_of("/home/rich/lexray/") == "lexray"
    assert qd.scope_of("/home/rich/Foo.Bar_2") == "foo-bar-2"
    assert qd.scope_of("/x/" + "a" * 60) == "a" * 32
    assert qd.scope_of("/x/___") is None
    assert qd.scope_of("") is None
    assert qd.scope_of(None) is None


@pytest.mark.parametrize("dirname", [
    "lexray", "nix-fleet", "Foo.Bar_2", "dots...and___runs", "-leading-and-trailing-",
    "2024", "a" * 60, "abc-" * 9, "___",
])
def test_the_scope_rule_is_the_one_qb_seat_actually_applies(tmp_path, dirname):
    """Two implementations of one rule, pinned to each other.

    The dashboard joins a tmux pane to a board identity by turning the screen's
    repository into the scope qb-seat gave that seat — so a rule that differs in
    either direction shows one seat's state against another seat's pane, which is
    a wrong answer that looks exactly like a right one. qb-seat is the authority
    (it is what the board is told); this asks it, rather than asserting on either
    side's source.
    """
    d = tmp_path / dirname
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    # A runtime dir of our own: --dry-run reads the pane markers, and a suite that
    # read the developer's would refuse whenever a real seat held that number.
    run = tmp_path / "run"
    run.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("QB_SEAT_SCOPE", "QB_SEAT_REPO")}
    done = subprocess.run(
        [str(QB_SEAT), "1", "--dry-run"], cwd=str(d), capture_output=True, text=True,
        env={**env, "XDG_RUNTIME_DIR": str(run), "QB_SEAT_BRIEF": ""},
    )
    assert done.returncode == 0, done.stderr
    instance = next(line.split(":", 1)[1].strip() for line in done.stdout.splitlines()
                    if line.startswith("instance:"))
    scope = qd.scope_of(str(d))
    assert instance == (f"seat-{scope}-1" if scope else "seat-1")


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


def test_running_items_come_first_and_blocked_ones_last():
    """The panel's whole order: what is happening, what is free, what is stuck."""
    items = [item("free-a"), item("blocked", blocked=[{"ref": "9"}]),
             item("running", holder="zeus/one"), item("free-b")]
    assert [i["title"] for i in qd.sort_plan(items, [qd.REPO])] == [
        "running", "free-a", "free-b", "blocked"]


def test_an_item_in_somebody_elses_held_plan_does_not_sort_into_the_free_band():
    """The free band has one job — the rows a seat can pick up — and an item the
    plan's holder has reserved is the band failing at it."""
    items = [item("free"), item("running", holder="zeus/one"), covered("covered")]
    assert [i["title"] for i in qd.sort_plan(items, [qd.REPO])] == [
        "running", "covered", "free"]


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
    assert qd.plan_who(item(holder="zeus/badger-ember"))[0] == "badger-ember"
    assert qd.plan_who(item(blocked=[{"ref": "9"}]))[0] == "waits #9"
    assert qd.plan_who(item(blocked=[{"ref": None}, {"ref": None}]))[0] == "waits ×2"


def test_a_covered_item_shows_its_plans_holder_and_not_an_idle_age():
    """An age in that column is the strongest invitation on the pane — "nobody has
    touched this for four days" — and it was printed over work another agent had
    reserved as a unit. Who to talk to is what a reader needs there."""
    assert qd.plan_who(covered(holder="zeus/badger-ember"))[0] == "badger-ember"


def test_a_blocked_item_is_not_also_counted_as_running():
    items = [item(holder="zeus/one"), item("b", blocked=[{"ref": "9"}]),
             item("c", holder="zeus/two", blocked=[{"ref": "9"}])]
    assert qd.plan_counts(items) == (2, 1)


def test_a_covered_item_counts_as_running_rather_than_as_nothing():
    """The title is the number a reader takes in without reading the rows, and it
    counted a covered item as neither running nor blocked — which in a panel whose
    other number is "open" is the same as calling it free."""
    items = [item("running", holder="zeus/one"), covered("covered"),
             item("free"), item("blocked", blocked=[{"ref": "9"}])]
    assert qd.plan_counts(items) == (2, 1)


def test_a_covered_item_that_is_also_blocked_is_counted_once():
    """Both bands would otherwise claim it. Taken is the stronger fact — a blocked
    item is something a reader can do nothing about; a covered one is somebody to
    talk to."""
    assert qd.plan_counts([covered("both", blocked=[{"ref": "9"}])]) == (1, 0)


def test_the_detail_line_carries_the_note_the_panel_cannot_fit():
    line = qd.plan_detail(item("short title", ref=8, holder="zeus/one",
                               plan={"label": "stage one"},
                               note="because the order matters"))
    assert "#8" in line and "stage one" in line
    assert "zeus/one" in line and "because the order matters" in line


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


def test_a_checkout_is_asked_which_repo_it_is():
    slug = qd.repo_arg(str(Path(__file__).resolve().parent.parent.parent))
    assert slug.count("/") == 1 and slug.endswith("/quarterback")


def test_a_checkout_is_returned_absolute_because_tmux_resolves_it_elsewhere():
    """The path is handed to tmux as a `-c` start directory, and tmux resolves a
    relative one against the SERVER's cwd — where it was started, not where the
    dashboard is. `self.repo` was `os.getcwd()` and absolute by construction; a
    relative `--repo` would have launched work somewhere else entirely, while the
    guard beside it resolved the same path correctly in-process and hid it."""
    root = Path(__file__).resolve().parent.parent.parent
    for spelling in (".", "./"):
        slug, path = qd.repo_target(spelling if spelling == "." else str(root) + "/")
        assert os.path.isabs(path), f"{spelling} came back relative: {path}"
    slug, path = qd.repo_target("harness/../")
    assert os.path.isabs(path) and slug.endswith("/quarterback")


def test_a_bare_name_that_is_a_directory_is_that_directory(tmp_path, monkeypatch):
    """`--repo nix-fleet` beside a checkout of that name is not a guess about an
    owner, and it worked before the shape rule arrived."""
    # Asserted on the PATH, not on the slug: this suite runs in a worktree as
    # readily as in the main checkout, and a worktree's directory name is not its
    # repository's name — which is the whole reason a directory is asked for its
    # origin rather than read as one.
    root = Path(__file__).resolve().parent.parent.parent
    monkeypatch.chdir(root.parent)
    slug, path = qd.repo_target(root.name)
    assert path == str(root)
    assert slug.count("/") == 1 and " " not in slug


def test_a_checkout_argument_says_where_work_should_run_too():
    """`--repo <checkout>` moves the ⚒'s cwd, not only the rows the panels draw.

    A SLUG cannot: it names a repository this process may have no checkout of, so
    the second half of the answer is None and the guards refuse those rows out loud
    rather than launching `/fix-issue` on a number that means something else here.
    """
    here = str(Path(__file__).resolve().parent.parent.parent)
    slug, path = qd.repo_target(here)
    assert slug.endswith("/quarterback") and path == here
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


def test_a_tilde_is_expanded_because_the_help_text_advertises_one(monkeypatch):
    """Only an interactive shell expands `~`. Quoted, built into a QB_SEATS_DASH
    command or sent through `tmux send-keys`, it arrives intact — and was reported
    as a bad slug, which misdiagnoses it."""
    monkeypatch.setenv("HOME", str(Path(__file__).resolve().parent.parent.parent))
    slug, path = qd.repo_target("~")
    assert slug.endswith("/quarterback") and path == os.path.expanduser("~")


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
    assert qd.tmux_seats() == []


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
        "%0\t1\ts\t0\tclaude\t/repo\t/repo\t",
        "%2\t3\ts\t0\tclaude\t/repo\t/repo\t",
        "%1\t2\ts\t0\tbash\t/repo\t/repo\t",
        "%3\t\ts\t0\tqb-board\t/repo\t/repo\t",   # the board pane: no @qb_seat
    ])
    got = qd.tmux_seats()
    assert [s["seat"] for s in got] == ["1", "2", "3"]
    assert [s["pane"] for s in got] == ["%0", "%1", "%2"]
    assert all(s["command"] for s in got), "the board pane leaked into the seats"


def test_two_screens_come_back_grouped_by_screen(monkeypatch):
    """`list-panes -a` is the whole server, and since #208 two screens can each
    hold a seat 1. Sorted on the number alone they interleave, and the panel reads
    as one screen with every number twice."""
    _tmux_returning(monkeypatch, [
        "%0\t1\tseats-lexray\t0\tclaude\t/x/lexray\t/x/lexray\t",
        "%2\t1\tseats-nix-fleet\t0\tclaude\t/x/nix-fleet\t/x/nix-fleet\t",
        "%3\t2\tseats-nix-fleet\t0\tclaude\t/x/nix-fleet\t/x/nix-fleet\t",
        "%1\t2\tseats-lexray\t0\tclaude\t/x/lexray\t/x/lexray\t",
    ])
    got = qd.tmux_seats()
    assert [(s["session"], s["seat"]) for s in got] == [
        ("seats-lexray", "1"), ("seats-lexray", "2"),
        ("seats-nix-fleet", "1"), ("seats-nix-fleet", "2"),
    ]
    # The screen's repository, which is what joins a pane to a board identity.
    assert [s["repo"] for s in got[:2]] == ["/x/lexray", "/x/lexray"]


def test_a_screen_with_no_qb_repo_still_yields_its_seats(monkeypatch):
    """@qb_repo is newer than @qb_seat, so a screen built by an older qb-seats
    answers with an empty field. It must cost the scope and not the seat."""
    _tmux_returning(monkeypatch, ["%0\t1\ts\t0\tclaude\t/repo\t\t"])
    got = qd.tmux_seats()
    assert [s["seat"] for s in got] == ["1"]
    assert got[0]["repo"] == ""
    assert qd.pane_scope(got[0]) is None


def test_a_tmux_that_fails_is_an_empty_screen_not_a_crash(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/whatever,1,0")

    def boom(*a, **k):
        raise OSError("no tmux here")

    monkeypatch.setattr(qd.subprocess, "run", boom)
    assert qd.tmux_seats() == []


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
