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
