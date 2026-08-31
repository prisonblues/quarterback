"""Caps — the round ceiling and the spend ceiling, as policy rather than advice (#55).

Four claims are pinned here, and they are #55's four acceptance criteria:

* a cycle cannot exceed the board's ``max_rounds``, whoever is driving it;
* a repo at its ceiling stops being reviewed VISIBLY — on the board and the PR,
  with ``stop_confident: false`` so a budget stop cannot read as convergence;
* the ceiling cannot be raised from inside the repo, by ``--max-rounds``, or by
  ``--force``;
* one setting turns a repo's reviews off, effective on the next resolution.

And a fifth that is not an acceptance criterion but is the condition this landed
under: **with no dial set it does nothing**, including making no board call at
all. :func:`test_a_fleet_that_has_set_no_number_makes_no_board_call` is that one,
and it is written so it fails loudly rather than quietly if the dormant path ever
starts reaching for the network.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness_rules  # noqa: E402
import panel  # noqa: E402
import panel_caps  # noqa: E402
import panel_core  # noqa: E402
import panel_preflight  # noqa: E402
import panel_seats  # noqa: E402
from conftest import gh_stub, pr_meta  # noqa: E402

PR_DIFF = ("diff --git a/a.py b/a.py\n"
           "--- a/a.py\n"
           "+++ b/a.py\n"
           "@@ -1,0 +1,1 @@\n"
           "+first\n")

CFG = {"github": "acme/board", "name": "board", "path": "/tmp/repo",
       "_rules_baseline": ".harness-rules.sample",
       "review_panel": {},
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}}}


def spend(**windows) -> str:
    """A `GET /review/spend` body, for `$QUARTERBACK_REVIEW_SPEND`.

    Every window defaults to the shape an empty board returns, so a test names
    only the number it is about.
    """
    empty = {"runs": 0, "rows": 0, "measured_rows": 0, "tokens": None,
             "cost_usd": None}
    body = {"now": "2026-08-22T12:00:00+00:00", "repo": "acme/board", "pr": 77,
            "window_hours": 24, "since": "2026-08-21T12:00:00+00:00",
            "repo_window": dict(empty), "fleet_window": dict(empty),
            "pr_total": dict(empty)}
    for name, got in windows.items():
        body[name] = {**empty, **got}
    return json.dumps(body)


def _run(monkeypatch, tmp_path, *, panel_cfg=None, cfg_extra=None, force=False,
         max_rounds=2, round_no=1, spend_body=None, record=False, calls=None):
    """One panel run, reporting the payload and which seats were dispatched.

    A seat that runs at all under a reached ceiling is this issue's defect, so
    `ran` is the assertion most of these make.
    """
    cfg = {**CFG, **(cfg_extra or {}),
           "review_panel": {**CFG["review_panel"], **(panel_cfg or {})}}
    monkeypatch.setenv(panel_caps.SPEND_ENV, spend_body if spend_body is not None
                       else "")
    ran: list[str] = []
    fake_sh = gh_stub(meta=pr_meta(title="feat: a thing", head="aaa111"),
                      diff=PR_DIFF, calls=calls)

    def fake_review(name, model, prompt, effort="", **_kw):
        ran.append(name)
        return panel.ReviewerRun([], None, 800, None)

    recorded: list[dict] = []
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(panel_core, "sh", fake_sh)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, panel.CoverageRuling()))
    monkeypatch.setattr(panel, "record_run",
                        lambda payload: recorded.append(payload) or "")
    out = tmp_path / "r.json"
    assert panel.run("e2e", 77, post=False, json_file=str(out), record=record,
                     round_no=round_no, max_rounds=max_rounds, force=force) == 0
    return json.loads(out.read_text()), ran, recorded


# --------------------------------------------------------------------------
# It does nothing until somebody sets a number.
# --------------------------------------------------------------------------

def test_a_fleet_that_has_set_no_number_makes_no_board_call(monkeypatch):
    """The condition this shipped under, and the one worth a test of its own.

    A ceiling that consulted the board on every round whether or not one was set
    would be a new dependency and a new failure mode bought for nothing — and it
    would arrive on a fleet that had asked for no change at all. So the dormant
    path is asserted by making the board read EXPLODE: if `check` ever reaches for
    it with every ceiling unset, this fails with the exception rather than passing
    quietly on a network call nobody noticed.
    """
    def explode(*a, **k):
        raise AssertionError("the dormant path read the board")

    monkeypatch.setattr(panel_caps, "fetch_spend", explode)
    notes: list[str] = []
    assert panel_caps.check(CFG, {}, 77, notes).stop is False
    assert notes == []


def test_a_pr_the_panel_would_skip_on_its_title_is_never_checked_against_a_ceiling(
        monkeypatch, tmp_path):
    """A title-skipped PR spends nothing, so a ceiling has nothing to say about it.

    Ordering, not economy. The unattended branch REFUSES on a ceiling it could not
    verify — so checking before the title skip turns a release-merge that costs
    zero into a refusal, on a board outage, for a PR nobody was going to review.
    `skip_title_patterns` exists because one release-merge came to about $750; the
    ceiling must not undo the cheapest guard in the system.
    """
    reached = []
    monkeypatch.setattr(panel_caps, "fetch_spend",
                        lambda *a, **k: reached.append(a) or (None, "no board"))
    payload, ran, _ = _run(
        monkeypatch, tmp_path,
        panel_cfg={"skip_title_patterns": ["^feat"],
                   "budget": {"runs_per_day": 1}})
    assert ran == []
    assert "skip pattern" in payload["skip_reason"]
    assert reached == [], "a title-skipped PR was measured against a ceiling"


def test_a_ceiling_this_harness_cannot_read_dies_before_the_pr_is_fetched(
        monkeypatch, tmp_path):
    """The other half of the same split. Validation is cheap and belongs beside the
    other bad-dial refusals; the board read is not and waits. A typo'd ceiling that
    only surfaced after two `gh` calls would be a config error reported from the
    wrong end of the run."""
    calls: list = []
    with pytest.raises(SystemExit) as raised:
        _run(monkeypatch, tmp_path, calls=calls,
             panel_cfg={"budget": {"runs_per_day": "lots"}})
    assert "budget.runs_per_day" in str(raised.value)
    assert calls == [], f"the PR was fetched before the ceiling was validated: {calls}"


def test_no_ceiling_ships_set_at_all_and_the_repo_agrees_with_it():
    """Two halves of the same claim, and both have to hold to make it true.

    EVERY ceiling ships absent, and the one exception this file used to record is the
    reason the rule is written as an absolute. #621 set `tokens_per_pr` to 20,000,000
    on 2026-08-30, beside `max_rounds: 6`, so the later rounds could be afforded — and
    it came back to `None` on 2026-08-31, because the cost was not in the number.

    `Budget.dormant` holds only while every one of these is `None`, and a dormant budget
    returns before any board call. Setting ONE key wakes the block for every repo on the
    fleet, and a `fetch_spend` that cannot answer then refuses the round — before any
    seat runs, and `--force` cannot move it. `run-loop.sh` exports `HARNESS_UNATTENDED=1`,
    so an unreachable board would have stopped autonomous review rather than letting it
    spend uncapped. Dormant already meant "no ceiling", so the key bought a ceiling
    nobody had asked for and a hard board dependency on the one path that cannot ask a
    human.

    So this asserts the absolute, not "all but one": a number written into `DEFAULTS`
    puts every repo on the fleet under a ceiling nobody chose AND wakes a code path that
    can refuse. The repo's own tracked policy has to agree — a sample that disagreed with
    the built-in would govern this repo differently from a repo with no rules file at
    all, silently.
    """
    built_in = harness_rules.DEFAULTS["review_panel"]["budget"]
    assert set(built_in) == set(panel_caps.CEILINGS)
    assert all(v is None for v in built_in.values()), built_in

    root = Path(__file__).resolve().parents[3]
    sample = json.loads((root / ".harness-rules.sample").read_text())
    written = harness_rules.strip_comments(sample)["review_panel"].get("budget", {})
    assert written == built_in, written


def test_a_dormant_budget_says_nothing_and_a_live_one_says_so():
    """#52's "never silent" applies to the round that RAN, not only to the one
    that was stopped. A ceiling in force is a fact about how the review was
    governed, and a reader six weeks later has the report and not the dial."""
    assert panel_caps.resolve_budget({}, notes := []).dormant and notes == []
    budget = panel_caps.resolve_budget({"budget": {"tokens_per_day": 4_000_000}},
                                       notes := [])
    assert not budget.dormant
    assert any("spend ceiling in force" in n and "4,000,000" in n for n in notes)


# --------------------------------------------------------------------------
# The round ceiling: a cap the caller cannot step over.
# --------------------------------------------------------------------------

def _dialled(value, layer="board"):
    return {**CFG, "_dials": {"review_panel.max_rounds": {
        "value": value, "layer": layer, "source": "https://qb/dials",
        "scope": "repo", "reason": "spend", "set_by": "human/rich",
        "expires_at": None}}}


def test_the_board_is_the_only_layer_that_states_a_ceiling():
    """`max_rounds` has four layers and only one of them is a ceiling. DEFAULTS
    and the repo's own file are what this repo would LIKE; the board is what a
    person decided, which is the only one a caller may not step over."""
    assert panel_caps.round_ceiling(_dialled(3))[0] == 3
    assert panel_caps.round_ceiling(_dialled(3, layer="sample"))[0] is None
    assert panel_caps.round_ceiling(_dialled(3, layer="defaults"))[0] is None
    assert panel_caps.round_ceiling(CFG)[0] is None


def test_a_ceiling_this_harness_could_not_apply_is_not_a_ceiling():
    """It fails OPEN, deliberately. A board dial with a junk value never reaches
    `_dials` as a board layer — `_dial_problem` refuses it and names it — so this
    is the belt on that brace, and inventing a ceiling out of a value nothing
    validated would be worse than having none."""
    for junk in ("lots", True, 0, -1, None, 2.5):
        assert panel_caps.round_ceiling(_dialled(junk))[0] is None, junk


def test_the_callers_cap_may_lower_the_board_ceiling_and_not_raise_it():
    """#55's first criterion, at the one seam that let a caller through.
    `--max-rounds` used to win outright — "the caller's cap, honoured by a human
    reading a markdown file". Unattended there is no such reader."""
    assert panel_seats.resolve_max_rounds(5, {}, [], 2) == 2
    assert panel_seats.resolve_max_rounds(1, {}, [], 2) == 1
    assert panel_seats.resolve_max_rounds(2, {}, [], 2) == 2


def test_the_clamp_says_which_number_bound_and_why_it_cannot_be_raised():
    """A cap that silently halves a caller's request is a cap nobody can debug.
    The note has to name both numbers and say where the remedy is NOT."""
    panel_seats.resolve_max_rounds(5, {}, notes := [], 2)
    assert len(notes) == 1
    assert "lowered to 2" in notes[0] and "asked for 5" in notes[0]
    assert "cannot be raised from inside the repo" in notes[0]


def test_with_no_board_ceiling_the_caller_still_wins_exactly_as_before():
    """The unchanged half, and the reason this could land on a fleet that had set
    nothing. `None` is what every repo passes until a dial exists."""
    assert panel_seats.resolve_max_rounds(5, {"max_rounds": 2}, notes := []) == 5
    assert notes == []
    assert panel_seats.resolve_max_rounds(5, {"max_rounds": 2}, [], None) == 5
    assert panel_seats.resolve_max_rounds(None, {"max_rounds": 7}, []) == 7


def test_a_round_past_the_board_ceiling_names_the_remedy_it_does_not_have(
        monkeypatch, tmp_path):
    """`--round 4` against a cap of 2 already exited; what it printed was "raise
    the cap", which is advice a reader of a fleet ceiling cannot take. The two
    refusals differ by which remedy exists."""
    with pytest.raises(SystemExit) as raised:
        _run(monkeypatch, tmp_path, cfg_extra=_dialled(2), round_no=3,
             max_rounds=9)
    said = str(raised.value)
    assert "set on the board" in said
    assert "cannot be raised from inside the repo being reviewed" in said


# --------------------------------------------------------------------------
# The spend ceiling: checked against a measurement, before the spend.
# --------------------------------------------------------------------------

def test_a_reached_token_ceiling_refuses_before_any_seat_is_dispatched(
        monkeypatch, tmp_path):
    """The seats are the minutes and the money. A ceiling enforced after them has
    cost everything it exists to save."""
    payload, ran, _ = _run(
        monkeypatch, tmp_path,
        panel_cfg={"budget": {"tokens_per_day": 1_000_000}},
        spend_body=spend(repo_window={"runs": 4, "rows": 8, "measured_rows": 8,
                                      "tokens": 1_200_000}))
    assert ran == [], "a seat was dispatched past the repo's spend ceiling"
    assert payload["reviewed"] is False
    assert payload["preflight"]["verdict"] == "refuse"
    assert "1,200,000 of 1,000,000 tokens" in payload["skip_reason"]


def test_spend_below_the_ceiling_reviews_as_usual(monkeypatch, tmp_path):
    """The other half, and the one that says the ceiling is a ceiling rather than
    an off switch."""
    _, ran, _ = _run(
        monkeypatch, tmp_path,
        panel_cfg={"budget": {"tokens_per_day": 1_000_000}},
        spend_body=spend(repo_window={"runs": 1, "rows": 2, "measured_rows": 2,
                                      "tokens": 12_000}))
    assert ran == ["claude"]


def test_the_ceiling_is_reached_AT_the_number_not_past_it():
    """A ceiling of N means N is all there is. Spending exactly N and then one
    more round is how a budget is exceeded by one round for ever."""
    at = panel_caps.check(CFG, {"budget": {"runs_per_day": 3}}, 77, [],
                          headless=False)
    assert at.stop is False  # nothing recorded yet


@pytest.mark.parametrize("used,stopped", [(2, False), (3, True), (9, True)])
def test_a_run_ceiling_counts_rows_the_board_holds(monkeypatch, used, stopped):
    monkeypatch.setenv(panel_caps.SPEND_ENV,
                       spend(repo_window={"runs": used, "rows": used * 2,
                                          "measured_rows": used * 2,
                                          "tokens": 10}))
    got = panel_caps.check(CFG, {"budget": {"runs_per_day": 3}}, 77, [],
                           headless=False)
    assert got.stop is stopped


def test_a_per_pr_run_ceiling_binds_a_caller_that_renumbers_its_rounds(monkeypatch):
    """The hole a round cap alone cannot close. Rounds are driven by the caller —
    `--round N` is an argument — so a driver that always says `--round 1` never
    reaches the round cap at all. A run is a row on the board whatever it called
    itself, which is why `runs_per_pr` is here beside `tokens_per_pr`."""
    monkeypatch.setenv(panel_caps.SPEND_ENV,
                       spend(pr_total={"runs": 6, "rows": 12, "measured_rows": 12,
                                       "tokens": 50}))
    got = panel_caps.check(CFG, {"budget": {"runs_per_pr": 4}}, 77, notes := [],
                           headless=False)
    assert got.stop
    assert "runs per pr: 6 of 4 recorded review runs" in got.refusal
    assert "pr total" in got.refusal
    assert notes  # the ceiling in force is reported whether or not it bound


def test_a_ceiling_of_zero_stops_everything_and_is_not_the_same_as_absent(
        monkeypatch):
    """"This repo is stopped" is a thing an operator may want to say, and `null`
    — clear the dial — is how they say the other one. Folding 0 into absent would
    leave one of the two unsayable."""
    monkeypatch.setenv(panel_caps.SPEND_ENV, spend())
    stopped = panel_caps.check(CFG, {"budget": {"runs_per_day": 0}}, 77, [],
                               headless=False)
    assert stopped.stop
    absent = panel_caps.check(CFG, {"budget": {"runs_per_day": None}}, 77, [],
                              headless=False)
    assert absent.stop is False


def test_the_fleet_ceiling_is_measured_over_every_repo(monkeypatch):
    """#55 asks for per-repo AND fleet-wide. A repo that has spent nothing today
    still stops when the fleet's own window is gone."""
    monkeypatch.setenv(panel_caps.SPEND_ENV,
                       spend(fleet_window={"runs": 40, "rows": 90,
                                           "measured_rows": 90,
                                           "tokens": 90_000_000}))
    got = panel_caps.check(CFG, {"budget": {"fleet_tokens_per_day": 50_000_000}},
                           77, [], headless=False)
    assert got.stop and "fleet window" in got.refusal


# --------------------------------------------------------------------------
# What it says when the measurement is thin.
# --------------------------------------------------------------------------

def test_a_window_nothing_instrumented_is_unknown_spend_and_not_free_spend(
        monkeypatch):
    """`tokens: null` over runs that HAPPENED means nothing in the window was
    instrumented, not that nothing was spent — an uninstrumented seat and a run
    recorded before v2.14 both report no tokens. Treating that as headroom is how a
    token-only ceiling quietly stops binding, so it goes to the same fork as an
    unreachable board: a note attended, a refusal unattended."""
    body = spend(repo_window={"runs": 9, "rows": 20, "measured_rows": 0,
                              "tokens": None})
    monkeypatch.setenv(panel_caps.SPEND_ENV, body)
    attended = panel_caps.check(CFG, {"budget": {"tokens_per_day": 10}}, 77,
                                notes := [], headless=False)
    assert attended.stop is False
    assert any("UNVERIFIED" in n and "reviewing anyway" in n for n in notes)

    headless = panel_caps.check(CFG, {"budget": {"tokens_per_day": 10}}, 77, [],
                                headless=True)
    assert headless.stop
    assert "none of the 20 reviewer runs" in headless.refusal
    # And it names the ceiling that WOULD bind on a box like this, because
    # "restore the board" is the wrong remedy when the board is fine.
    assert "budget.runs_per_day" in headless.refusal


def test_a_window_with_no_runs_in_it_is_a_real_zero_and_not_a_brick(monkeypatch):
    """The other reading of the same null, and the reason the two are separated by
    `rows`. A quiet repo has spent nothing; calling that unverifiable would refuse
    every unattended run on it for ever, on a board that answered correctly."""
    monkeypatch.setenv(panel_caps.SPEND_ENV, spend())
    got = panel_caps.check(CFG, {"budget": {"tokens_per_day": 10}}, 77, [],
                           headless=True)
    assert got.stop is False


def test_a_board_that_answers_without_the_window_does_not_silently_uncap(
        monkeypatch):
    """Found by the codex second opinion on this diff. A body missing the window a
    ceiling is keyed to used to add a note and PROCEED — so a partial response, a
    version skew or a board bug removed the ceiling on exactly the runs nobody is
    watching. It reaches the same fork as an unreachable board now."""
    partial = json.loads(spend())
    del partial["pr_total"]
    monkeypatch.setenv(panel_caps.SPEND_ENV, json.dumps(partial))
    got = panel_caps.check(CFG, {"budget": {"runs_per_pr": 1}}, 77, [],
                           headless=True)
    assert got.stop and "no `pr_total`" in got.refusal
    assert panel_caps.check(CFG, {"budget": {"runs_per_pr": 1}}, 77, [],
                            headless=False).stop is False


def test_a_ceiling_that_was_reached_outranks_one_that_could_not_be_read(monkeypatch):
    """The answer is KNOWN and it is stop. Reporting the softer refusal would send
    an operator to fix a board that was working."""
    partial = json.loads(spend(repo_window={"runs": 5, "rows": 5,
                                            "measured_rows": 5, "tokens": 9}))
    del partial["pr_total"]
    monkeypatch.setenv(panel_caps.SPEND_ENV, json.dumps(partial))
    got = panel_caps.check(CFG, {"budget": {"runs_per_day": 1, "runs_per_pr": 1}},
                           77, [], headless=True)
    assert got.stop
    assert "spend ceiling is reached" in got.refusal
    assert "UNVERIFIED" not in got.refusal


def test_a_partly_measured_window_says_the_real_spend_is_higher(monkeypatch):
    """A sum over a half-instrumented window is an UNDERCOUNT, and the honest
    thing is to say so in the same breath as the number rather than to leave a
    reader assuming the sum was complete."""
    monkeypatch.setenv(panel_caps.SPEND_ENV,
                       spend(repo_window={"runs": 5, "rows": 20,
                                          "measured_rows": 6, "tokens": 900}))
    got = panel_caps.check(CFG, {"budget": {"tokens_per_day": 100}}, 77, [],
                           headless=False)
    assert "measured over 6 of 20 reviewer runs" in got.refusal
    assert "the real spend is higher" in got.refusal


def test_a_window_nearly_spent_is_warned_about_without_being_refused(monkeypatch):
    """A ceiling that only ever speaks by stopping gives an operator no chance to
    act before it does."""
    monkeypatch.setenv(panel_caps.SPEND_ENV,
                       spend(repo_window={"runs": 9, "rows": 9,
                                          "measured_rows": 9, "tokens": 85}))
    got = panel_caps.check(CFG, {"budget": {"tokens_per_day": 100}}, 77,
                           notes := [], headless=False)
    assert got.stop is False
    assert any("nearly reached" in n for n in notes)


# --------------------------------------------------------------------------
# What it does when it cannot check — #59's question, answered.
# --------------------------------------------------------------------------

def test_an_unreachable_board_does_not_stop_an_attended_run(monkeypatch):
    """#59's property: `/panel` on a laptop with no board, no network and no `qb`
    reviews a PR, and always has. Refusing here would break the case the whole
    constraint exists to protect — and the round cap still binds, because it needs
    no board read."""
    monkeypatch.setenv(panel_caps.SPEND_ENV, "")
    got = panel_caps.check(CFG, {"budget": {"tokens_per_day": 10}}, 77,
                           notes := [], headless=False)
    assert got.stop is False
    assert any("UNVERIFIED" in n and "reviewing anyway" in n for n in notes)


def test_an_unreachable_board_DOES_stop_an_unattended_run(monkeypatch):
    """The other half of the same decision, and the reason it is a decision.
    `qb-start` already reasons this way about `qb-pace` — a spawn proceeds only on
    a definite go — and a governor that cannot read its input must not report
    clear (#244). An unattended run that treats an unreachable board as headroom
    is a ceiling anybody can remove by unplugging a cable."""
    monkeypatch.setenv(panel_caps.SPEND_ENV, "")
    got = panel_caps.check(CFG, {"budget": {"tokens_per_day": 10}}, 77, [],
                           headless=True)
    assert got.stop
    assert "UNVERIFIED" in got.refusal and "unattended" in got.refusal


def test_an_unreachable_board_stops_nothing_when_no_ceiling_is_set(monkeypatch):
    """Unattended, offline, and no dial: still a review. There is no ceiling to
    verify, so there is nothing an unreadable board could be hiding."""
    monkeypatch.setenv(panel_caps.SPEND_ENV, "")
    assert panel_caps.check(CFG, {}, 77, [], headless=True).stop is False


def test_a_board_that_is_too_old_says_so_rather_than_reporting_clear(monkeypatch):
    """A 404 is a capability answer — the board predates this endpoint — and NOT a
    pass. Which of the two it means is the caller's to decide, and it decides
    differently attended and unattended."""
    def four_oh_four(*a, **k):
        return None, "this board has no review/spend endpoint (404)"

    monkeypatch.setattr(panel_caps, "fetch_spend", four_oh_four)
    got = panel_caps.check(CFG, {"budget": {"runs_per_day": 1}}, 77, [],
                           headless=True)
    assert got.stop and "404" in got.refusal


# --------------------------------------------------------------------------
# The refusal is visible, and --force does not move it.
# --------------------------------------------------------------------------

def test_force_does_not_override_a_spend_ceiling(monkeypatch, tmp_path):
    """`--force` overrides this HOST's judgement about what its own seats can
    read. The ceiling is a number a person set on the board for the fleet, and a
    local flag that could switch it off would make it advice again — which is the
    state #55 exists to end."""
    payload, ran, _ = _run(
        monkeypatch, tmp_path, force=True,
        panel_cfg={"budget": {"runs_per_day": 1}},
        spend_body=spend(repo_window={"runs": 3, "rows": 6, "measured_rows": 6,
                                      "tokens": 5}))
    assert ran == []
    assert payload["preflight"]["verdict"] == "refuse"
    assert payload["preflight"]["forced"] is False
    assert any("--force did NOT override" in n for n in payload["config_notes"])


def test_force_still_overrides_a_size_refusal(monkeypatch):
    """The narrowness of the previous test, asserted rather than assumed. A flag
    that stopped working everywhere would pass that test and be a regression."""
    over = "x" * 400
    budgets = {"claude": 10}
    forced = panel_preflight.preflight(over, budgets, {}, [], forced=True,
                                       gate="a precondition failed")
    assert forced.verdict == "run" and forced.forced is True
    hard = panel_preflight.preflight(over, budgets, {}, [], forced=True,
                                     gate="a ceiling was reached",
                                     gate_overridable=False)
    assert hard.verdict == "refuse" and hard.forced is False


def test_a_budget_stop_is_recorded_as_a_stop_that_was_not_convergence(
        monkeypatch, tmp_path):
    """#55's second criterion, and v2.15 is what serves it. A budget stop that
    looked like a clean review is the exact failure v2.15 exists to prevent, and
    `stop_confident: false` is already the field that tells them apart —
    `preland --require-earned-stop` HOLDs on it and the review queue files it
    `unconverged`. It only had to be obeyed."""
    payload, _, recorded = _run(
        monkeypatch, tmp_path, record=True,
        panel_cfg={"budget": {"runs_per_day": 1}},
        spend_body=spend(repo_window={"runs": 2, "rows": 4, "measured_rows": 4,
                                      "tokens": 5}))
    assert payload["round_stop"]["stop"] is True
    assert payload["round_stop"]["confident"] is False
    assert "spend ceiling" in payload["round_stop"]["reason"]
    assert recorded, "a budget stop was not recorded on the board"
    assert recorded[0]["round_stop"]["confident"] is False
    # And per seat, because `_scorecards` builds a row for every selected reviewer
    # and a missing `reviewers` block reads as "ran, found nothing" — a refusal
    # filed as a clean review, per reviewer, in the table that answers which
    # reviewer finds the real issues.
    assert recorded[0]["reviewers"]["claude"]["ran"] is False


def test_a_budget_stop_says_it_did_not_converge_rather_than_leaving_it_null(
        monkeypatch, tmp_path):
    """The refusal's `round_stop` is hand-built, so a key it forgets is NULL (#626).

    `GET /review/convergence` reads NULL as `unmeasured` — "the panel never said"
    — and a caps refusal is the one path that definitely did say: the ceiling
    ended the cycle and no seat read the diff. Left out, the row was dropped from
    the aggregate for having reviewed nothing, the cycle's terminal round became
    the last round that said "go again", and a budget termination was filed
    `open`: maybe still running, outside the denominator, in the flattering
    direction. #637 recalibrates a threshold against exactly that rate.
    """
    payload, _, recorded = _run(
        monkeypatch, tmp_path, record=True,
        panel_cfg={"budget": {"runs_per_day": 1}},
        spend_body=spend(repo_window={"runs": 2, "rows": 4, "measured_rows": 4,
                                      "tokens": 5}))
    assert payload["round_stop"]["converged"] is False
    assert recorded[0]["round_stop"]["converged"] is False
    # Said, not derived: `converged` is absent from a payload that never reached a
    # stopping rule, and False is a different statement from that.
    assert "converged" in payload["round_stop"]


def test_a_size_refusal_is_still_not_a_stop(monkeypatch, tmp_path):
    """The narrowness of the field above. "This round could not usefully read the
    diff" leaves the cycle open; a ceiling ends it. Setting `round_stop` on both
    would tell `preland` that every oversized PR had stopped converging."""
    payload, ran, _ = _run(monkeypatch, tmp_path,
                           panel_cfg={"max_diff_chars": 1})
    assert ran == []
    assert payload["preflight"]["verdict"] in ("refuse", "manifest")
    assert payload["round_stop"] is None


# --------------------------------------------------------------------------
# One setting turns a repo off.
# --------------------------------------------------------------------------

def test_a_repo_switched_off_on_the_board_is_not_panelled(monkeypatch, tmp_path):
    """#55's fourth criterion. A dial takes effect on the next RESOLUTION —
    `resolve_repo` reads the board on every run — so "the next claim rather than
    the next restart" is what a dial already is."""
    off = {**CFG, "enabled": False, "_dials": {"enabled": {
        "value": False, "layer": "board", "source": "https://qb/dials",
        "scope": "repo", "reason": "paused while I am away",
        "set_by": "human/rich", "expires_at": None}}}
    payload, ran, _ = _run(monkeypatch, tmp_path, cfg_extra=off)
    assert ran == []
    assert payload["reviewed"] is False
    assert "switched off on the board" in payload["skip_reason"]
    assert "human/rich" in payload["skip_reason"]
    assert "paused while I am away" in payload["skip_reason"]


def test_a_repo_that_switched_itself_off_is_told_apart_from_a_board_pause():
    """Two causes, two remedies. One is cleared with `POST /dials/clear` and the
    other with a commit, and a reader who cannot tell them apart goes to the wrong
    place. `lander.py` has honoured this key since it existed and the review paths
    never did, so a repo that switched itself off still got reviewed."""
    own = panel_caps.enabled_refusal({**CFG, "enabled": False})
    assert "in its rules" in own and "switched off on the board" not in own
    assert panel_caps.enabled_refusal(CFG) == ""
    assert panel_caps.enabled_refusal({**CFG, "enabled": True}) == ""


def test_the_board_may_switch_a_repo_off_and_may_not_switch_one_on():
    """`reviewers.<seat>.enabled`'s rule with the halves swapped. A repo that
    switched its own reviews off knows something the board does not, so the board
    may turn one off and may not turn one back on over the top of a file that
    said no."""
    assert harness_rules.BOARD_DIALS["enabled"].rule == "narrow"


# --------------------------------------------------------------------------
# Values are checked, not just names.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("junk", ["lots", -1, 2.5, [], {"a": 1}, True])
def test_a_ceiling_this_harness_cannot_read_is_refused_rather_than_ignored(junk):
    """`harness_rules`' standing asymmetry: an unknown NAME is warned about and
    dropped because it may be a setting only a newer harness knows, and a known
    key this harness cannot read is a typo. Taking the default on a typo'd ceiling
    means running UNBOUNDED on a file that asked for a bound."""
    with pytest.raises(SystemExit) as raised:
        panel_caps.resolve_budget({"budget": {"tokens_per_day": junk}}, [])
    assert "budget.tokens_per_day" in str(raised.value)


def test_a_budget_block_of_the_wrong_shape_is_refused():
    with pytest.raises(SystemExit) as raised:
        panel_caps.resolve_budget({"budget": 4_000_000}, [])
    assert "a JSON object of ceilings" in str(raised.value)


@pytest.mark.parametrize("hours", [0, -1, panel_caps.MAX_WINDOW_HOURS + 1, "soon"])
def test_a_window_the_board_would_refuse_is_refused_here_first(hours):
    """The 422 the board would answer with is one the operator never sees — it
    arrives inside a request made on their behalf. Refusing here names the key."""
    with pytest.raises(SystemExit) as raised:
        panel_caps.resolve_budget({"budget_window_hours": hours}, [])
    assert "budget_window_hours" in str(raised.value)


def test_a_mistyped_ceiling_is_named_rather_than_silently_dropped():
    """The nested block's own failure mode, and the loudest one here: `budget:
    {"tokens_per_dy": 4e6}` leaves the ceiling ABSENT on the block whose whole job
    is to stop a spend. `escalate_on` needed the same descent first (#84)."""
    unknown = harness_rules.unknown_keys(
        {"review_panel": {"budget": {"tokens_per_dy": 4_000_000}}})
    assert unknown.get("review_panel.budget") == ["tokens_per_dy"]


def test_every_ceiling_is_settable_from_the_board_and_none_is_settable_twice():
    """A dial the board can hold and this harness does not recognise is stored,
    returned and ignored — loudly, but ignored. A ceiling in that state is a
    ceiling an operator believes is in force and is not."""
    for key in panel_caps.CEILINGS:
        assert f"review_panel.budget.{key}" in harness_rules.BOARD_DIALS
    assert "review_panel.budget_window_hours" in harness_rules.BOARD_DIALS
