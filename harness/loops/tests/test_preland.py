"""Tests for preland — the mechanical pre-land verdict.

The three properties worth defending, in the order they get broken:

1. **Absent never reads as clean.** A PR the panel never saw, a board that
   cannot be reached, a check turned off — each must be visible in the payload
   and none may produce READY. Most of this file is that one property, said
   about each check in turn.
2. **The verdict is the round's own statements**, not a proxy for them. The
   clause tests read `stopped`/`confirmed`/`head_sha` off a board row and assert
   the verdict follows, including PR #131's real shape: HOLD on two independent
   counts, because reporting one of them understates it.
3. **HOLD dominates RECONCILE.** Mechanical work on a PR nobody reviewed is work
   spent to reach a wall.
"""

import json
import subprocess
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness_rules  # noqa: E402
import preland  # noqa: E402

HEAD = "a" * 40
BASE = preland.BaseRef("main")
OTHER = "b" * 40


def pr(**over):
    """A PR as `gh pr view --json …` returns it: open, mergeable, CI green."""
    body = {"number": 7, "state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE",
            "headRefOid": HEAD, "headRefName": "feat/x", "baseRefName": "main",
            "title": "feat: a thing", "url": "https://example/7",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
    return {**body, **over}


def review_row(**over):
    """A board row for a round that is clean on every clause."""
    row = {"id": 1, "ts": "2026-08-16T12:45:41+00:00", "round": 1, "cycle": "f5c76fd8",
           "head_sha": HEAD, "stopped": True, "stop_reason": "dry — no findings to fix",
           "stop_confident": True, "stop_veto": [], "confirmed": 0, "unjudged": 0,
           "sonar_gate": "OK", "judge_skip": None}
    return {**row, **over}


@pytest.fixture
def board(monkeypatch):
    """Double the board, keyed by path. Anything unasked for is an outage, which
    is what a missing answer actually means to this module.

    Both entry points are doubled from one table. An answer may be written as
    `(body, err)` — what almost every check reads — or as `(body, err, status)`
    for the one check that reads an HTTP status, so an existing answer does not
    have to grow a third element it has no opinion about.
    """
    answers: dict[str, tuple] = {}

    def request(path, params):
        got = answers.get(path.strip("/"),
                          (None, "board unreachable (test default)"))
        return got if len(got) == 3 else (*got, None)

    monkeypatch.setattr(preland, "board_request", request)
    monkeypatch.setattr(preland, "board_get", lambda p, q: request(p, q)[:2])
    return answers


@pytest.fixture
def repo(tmp_path):
    """A repo root with no guardrail scripts in it — the unenrolled case."""
    (tmp_path / "scripts").mkdir()
    return str(tmp_path)


def proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def check(name, status, **kw):
    return preland.Check(name, status, **kw)


# ------------------------------------------------------------ verdict algebra


def test_all_passing_is_ready():
    assert preland.verdict_of([check("a", "passed"), check("b", "passed")]) == preland.READY


def test_a_status_the_verdict_does_not_recognise_holds():
    """A check added later with a new word for its answer, or a typo, must not
    fall through to READY. A merge gate's default branch is the closed one."""
    assert preland.verdict_of([check("a", "passed"),
                               check("b", "skpped-absent")]) == preland.HOLD


@pytest.mark.parametrize("status", preland.SKIPPED)
def test_skipped_checks_do_not_block(status):
    """A skip is recorded, not counted against the verdict — the record is what
    stops it reading clean, and the caller asked for it. All three kinds of skip
    behave the same way here; they differ only in what they tell the reader."""
    every = [check(name, status) for name in preland.CHECKS]
    assert preland.verdict_of(every) == preland.READY


def test_reconcile_when_only_mechanical_work_is_outstanding():
    assert preland.verdict_of([check("a", "passed"),
                               check("b", "reconcile")]) == preland.RECONCILE


def test_hold_dominates_reconcile():
    assert preland.verdict_of([check("a", "reconcile"),
                               check("b", "failed")]) == preland.HOLD


def test_unreadable_check_holds():
    """`error` is not a third kind of pass. A guardrail that could not be read is
    a guardrail that did not clear."""
    assert preland.verdict_of([check("a", "error")]) == preland.HOLD


def test_exit_codes_match_migration_reconcile():
    assert (preland.EXIT[preland.READY], preland.EXIT[preland.RECONCILE],
            preland.EXIT[preland.HOLD]) == (0, 3, 2)


# ------------------------------------------------------------------- pr_state


def test_open_mergeable_pr_passes():
    c = preland.check_pr_state(pr())
    assert c.status == "passed" and not c.reasons


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_a_pr_that_is_not_open_holds(state):
    """The 2026-08-16 failure: an hour of fix passes on an already-merged PR."""
    c = preland.check_pr_state(pr(state=state))
    assert c.status == "failed"
    assert f"the PR is {state}" in c.reasons[0]


def test_draft_holds():
    assert preland.check_pr_state(pr(isDraft=True)).status == "failed"


def test_conflicting_holds_because_resolving_is_not_mechanical():
    c = preland.check_pr_state(pr(mergeable="CONFLICTING"))
    assert c.status == "failed" and "CONFLICTING" in c.reasons[0]


def test_uncomputed_mergeability_warns_rather_than_holds():
    """GitHub answers UNKNOWN while it thinks. A conflict fails the merge loudly,
    so this is not the gate that has to catch it."""
    c = preland.check_pr_state(pr(mergeable="UNKNOWN"))
    assert c.status == "passed" and c.warnings


# ------------------------------------------------------------------- checkout


def test_checkout_at_the_prs_head_and_clean_passes(monkeypatch):
    monkeypatch.setattr(preland, "_git", lambda root, *a: HEAD if a[0] == "rev-parse" else "")
    c = preland.check_checkout("/x", pr())
    assert c.status == "passed" and not c.warnings


def test_checkout_at_another_commit_holds(monkeypatch):
    """The repo-local guardrails read the tree, so a verdict computed here would
    be about code the PR does not contain."""
    monkeypatch.setattr(preland, "_git", lambda root, *a: OTHER if a[0] == "rev-parse" else "")
    c = preland.check_checkout("/x", pr())
    assert c.status == "failed" and "--skip checkout" in c.reasons[0]


def test_tracked_modifications_hold(monkeypatch):
    monkeypatch.setattr(preland, "_git",
                        lambda root, *a: HEAD if a[0] == "rev-parse" else " M app/x.py")
    c = preland.check_checkout("/x", pr())
    assert c.status == "failed" and "1 tracked file(s)" in c.reasons[0]


def test_a_git_that_cannot_be_read_holds(monkeypatch):
    """`git status --porcelain` says "clean" with an empty string, so a read that
    FAILED must not come back as one — that is this file's own rule broken in the
    check that enforces it."""
    monkeypatch.setattr(preland, "_git", lambda root, *a: None)
    c = preland.check_checkout("/x", pr())
    assert c.status == "error" and "could not read" in c.reasons[0]


def test_untracked_files_only_warn(monkeypatch):
    """This repo's own plan.md is deliberately untracked; a scratch file is not a
    reason to refuse a merge."""
    monkeypatch.setattr(preland, "_git",
                        lambda root, *a: HEAD if a[0] == "rev-parse" else "?? plan.md")
    c = preland.check_checkout("/x", pr())
    assert c.status == "passed" and "1 untracked" in c.warnings[0]


# ------------------------------------------------------------------------- ci


#: `qbdata.py` holds the shared CI vocabulary and lives in `bin/` — see
#: `harness/package.nix` on why that is the one library there. The loops suite does
#: not otherwise import it, so the path goes on here rather than in conftest.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))
import qbdata as qd                                       # noqa: E402


@pytest.fixture
def runs(monkeypatch):
    """Stub the workflow-runs probe, and clear the answer cache around it.

    The probe is one `gh api` call and the cache is module-level, so a test that
    left either alone would either reach the network or read a neighbour's answer.
    Yields a dict of `{"head": [...], "branch": [...]}` for a test to fill in.

    `raising=False` on both, so the fixture still builds against a qbdata that has
    neither — which is what the red half of red/green runs these against.
    """
    reply = {"head": [], "branch": []}
    monkeypatch.setattr(qd, "_ci_cache", {}, raising=False)
    monkeypatch.setattr(qd, "workflow_runs",
                        lambda repo, sha="", branch="": (
                            reply["head"] if sha else reply["branch"], None),
                        raising=False)
    return reply


def run(conclusion="success", status="completed", sha="0123456789"):
    return {"conclusion": conclusion, "status": status, "head_sha": sha}


@pytest.mark.parametrize("rollup,status", [
    ([{"conclusion": "SUCCESS"}], "passed"),
    ([{"conclusion": "FAILURE"}], "failed"),
    ([{"status": "IN_PROGRESS"}], "failed"),
])
def test_ci_gates_on_green_only(rollup, status):
    """Pending fails as hard as red: a reconcile push restarts CI, so an earlier
    green is a statement about a commit that is no longer the head."""
    assert preland.check_ci(pr(statusCheckRollup=rollup, repo="o/r")).status == status


def test_a_pr_whose_head_has_no_run_at_all_holds(runs):
    """No CI signal is the absence of evidence, not evidence of green. A workflow
    that failed to trigger and a repo with no CI look identical from here, and
    only one of them is safe — so the repo that genuinely has none says so in
    .harness-rules, and the message names that switch."""
    c = preland.check_ci(pr(statusCheckRollup=[], repo="o/r"))
    assert c.status == "failed" and c.detail["state"] == "none"
    assert "no run has been created" in c.reasons[0]
    assert "disabled_checks" in c.reasons[0]


def test_a_gated_run_holds_and_names_the_last_run_that_actually_executed(runs):
    """#324, and the whole of it. Two commits pushed to fix a red suite came back
    `action_required` — created, never executed, contributing no check runs — so the
    PR's check list went EMPTY and every reader took that for "CI has not run yet".
    The gate must refuse, must say a human is what it is waiting on, and must carry
    the newest run that DID execute, which is the fact anybody actually wanted."""
    runs["head"] = [run(conclusion="action_required")]
    runs["branch"] = [run(conclusion="action_required"),
                      run(conclusion="failure", sha="843c506aaa")]
    c = preland.check_ci(pr(statusCheckRollup=[], repo="o/r"))
    assert c.status == "failed" and c.detail["state"] == "blocked"
    assert c.detail["last_executed"] == "failure at 843c506"
    assert "human" in c.reasons[0]
    assert "failure at 843c506" in c.reasons[0]


def test_an_unreadable_ci_state_holds_and_does_not_read_as_no_ci(monkeypatch):
    """The #244 rule, one gate along: "I could not tell" used to arrive here as
    `none` and print as "this repo has no CI", which is a sentence about the repo
    standing in for a failed lookup. A gate that merges on an unread signal is the
    defect the check exists to prevent."""
    monkeypatch.setattr(qd, "_ci_cache", {}, raising=False)
    monkeypatch.setattr(qd, "workflow_runs", lambda *a, **k: ([], "HTTP 502"),
                        raising=False)
    c = preland.check_ci(pr(statusCheckRollup=[], repo="o/r"))
    assert c.status == "failed" and c.detail["state"] == "unknown"
    assert "could not be determined" in c.reasons[0]
    assert "disabled_checks" not in c.reasons[0]


def test_a_rollup_carrying_action_required_is_blocked_not_merely_red(runs):
    """A check that says `ACTION_REQUIRED` reached no verdict. Calling it red is not
    dangerous, but it is wrong in the direction that stops a reader looking for the
    approval they have to give — and it hides the last conclusion that was real."""
    runs["branch"] = [run(conclusion="failure", sha="843c506aaa")]
    c = preland.check_ci(
        pr(statusCheckRollup=[{"conclusion": "ACTION_REQUIRED"}], repo="o/r"))
    assert c.status == "failed" and c.detail["state"] == "blocked"
    assert c.detail["last_executed"] == "failure at 843c506"


# --------------------------------------------------------------------- review


def test_board_outage_holds_and_names_the_off_switch(board):
    """The one place absent does not mean skip. An unset board URL means the
    review invariant exists and cannot be seen — and the reader who hits it must
    be told the switch verbatim, not left reading 'down' as 'broken'."""
    board["reviews"] = (None, "no board configured on this host")
    c = preland.check_review("o/r", pr())
    assert c.status == "error"
    assert "disabled_checks" in c.reasons[0] and '["review"]' in c.reasons[0]


def test_never_reviewed_holds(board):
    board["reviews"] = ([], "")
    c = preland.check_review("o/r", pr())
    assert c.status == "failed" and "no panel round is recorded" in c.reasons[0]


def test_a_clean_round_passes(board):
    board["reviews"] = ([review_row()], "")
    c = preland.check_review("o/r", pr())
    assert c.status == "passed" and not c.reasons and not c.warnings


def test_a_round_that_read_an_earlier_commit_holds(board):
    """#98's stamp, first consumed. Without this clause a review of earlier code
    reads as a review of this code."""
    board["reviews"] = ([review_row(head_sha=OTHER)], "")
    c = preland.check_review("o/r", pr())
    assert c.status == "failed" and "it is a review of earlier code" in c.reasons[0]


def test_a_round_with_no_head_sha_holds(board):
    board["reviews"] = ([review_row(head_sha=None)], "")
    c = preland.check_review("o/r", pr())
    assert c.status == "failed" and "recorded no head_sha" in c.reasons[0]


@pytest.mark.parametrize("stopped", [False, None])
def test_a_round_that_did_not_stop_holds(board, stopped):
    """NULL is not a quiet yes: panel.py records a run with no stopping verdict
    when it skips a PR, and that must not read as a stop."""
    board["reviews"] = ([review_row(stopped=stopped, stop_reason="3 P1/P2 outstanding")], "")
    c = preland.check_review("o/r", pr())
    assert c.status == "failed" and "3 P1/P2 outstanding" in c.reasons[0]


def test_confirmed_findings_hold(board):
    board["reviews"] = ([review_row(confirmed=20)], "")
    c = preland.check_review("o/r", pr())
    assert c.status == "failed" and "20 judge-confirmed" in c.reasons[0]


def test_pr_131_holds_on_two_independent_counts(board):
    """The case the issue leads with. Reporting one of the two would understate
    it, which is why reasons is a list."""
    board["reviews"] = ([review_row(stopped=False, stop_reason="41 new findings",
                                    confirmed=41)], "")
    c = preland.check_review("o/r", pr())
    assert len(c.reasons) == 2


def test_a_failing_sonar_gate_holds(board):
    board["reviews"] = ([review_row(sonar_gate="ERROR")], "")
    assert preland.check_review("o/r", pr()).status == "failed"


@pytest.mark.parametrize("gate", ["OK", "skipped", "no-pr-analysis", None])
def test_a_sonar_gate_that_is_not_failing_passes(board, gate):
    board["reviews"] = ([review_row(sonar_gate=gate)], "")
    assert preland.check_review("o/r", pr()).status == "passed"


def test_an_unknown_sonar_status_warns_rather_than_guessing(board):
    board["reviews"] = ([review_row(sonar_gate="WARN")], "")
    c = preland.check_review("o/r", pr())
    assert c.status == "passed" and "'WARN'" in c.warnings[0]


def test_an_unearned_stop_warns_and_lists_the_vetoes(board):
    """Deliberately not a HOLD: two permanently-absent seats on a headless box
    would make a green verdict unreachable."""
    board["reviews"] = ([review_row(stop_confident=False,
                                    stop_veto=["codex read half the diff"])], "")
    c = preland.check_review("o/r", pr())
    assert c.status == "passed"
    assert c.warnings == ["the stop was not earned: codex read half the diff"]


def test_an_unearned_stop_with_no_recorded_vetoes_still_says_so(board):
    board["reviews"] = ([review_row(stop_confident=False, stop_veto=None)], "")
    assert preland.check_review("o/r", pr()).warnings


def test_an_unearned_stop_HOLDS_when_the_caller_asks_for_the_strict_reading(board):
    """#100. `/panel-review-pr` §7 ran the round itself and is about to offer to
    land on the strength of it, so an unearned stop there is not background noise
    about somebody else's box — it is this cycle saying nobody read the whole
    diff. The vetoes still get reported; what changes is the verdict."""
    board["reviews"] = ([review_row(stop_confident=False,
                                    stop_veto=["codex read half the diff"])], "")
    c = preland.check_review("o/r", pr(), earned_stop=True)
    assert c.status == "failed"
    assert c.reasons == ["the stop was not earned: codex read half the diff"]
    assert not c.warnings, "the strict mode moves the veto, it must not print it twice"


def test_the_strict_reading_changes_nothing_about_a_stop_that_WAS_earned(board):
    """The flag is not a second bar on a clean round. A round that stopped
    confidently is READY under both readings, or the flag would be a way of
    refusing every PR rather than the ones whose review did not finish."""
    board["reviews"] = ([review_row()], "")
    c = preland.check_review("o/r", pr(), earned_stop=True)
    assert c.status == "passed" and not c.reasons and not c.warnings


def test_a_stop_the_board_never_recorded_a_verdict_for_is_not_an_unearned_one(board):
    """`stop_confident` is nullable, and null is a question nobody answered rather
    than a stop that failed. It must not become a HOLD here — `/panel-review-pr`
    §7 catches that case against its own round payload, which is the only place
    that can tell the two apart."""
    board["reviews"] = ([review_row(stop_confident=None)], "")
    assert preland.check_review("o/r", pr(), earned_stop=True).status == "passed"


@pytest.mark.parametrize("strict", (False, True))
def test_the_payload_says_which_reading_ran(board, strict):
    """A READY has to say whether the strict clause was even asked. Without it a
    caller cannot tell a stop that was earned from one nobody put the question
    to, and the audit trail is the whole reason `checks` is in the payload."""
    board["reviews"] = ([review_row()], "")
    c = preland.check_review("o/r", pr(), earned_stop=strict)
    assert c.detail["require_earned_stop"] is strict


def test_unadjudicated_findings_warn(board):
    board["reviews"] = ([review_row(unjudged=3, judge_skip="judge crashed")], "")
    c = preland.check_review("o/r", pr())
    assert c.status == "passed" and "judge crashed" in c.warnings[0]


@pytest.mark.parametrize("order", [(2, 1), (1, 2)])
def test_the_newest_round_is_the_one_judged(board, order):
    """A fix cycle's later round is the answer; ruling on an older one resurrects
    findings the fix already cleared. Asserted for BOTH orderings — the endpoint
    sorts newest-first today, and which round this rules on is too much of the
    verdict to rest on another service's ORDER BY staying what it is."""
    rows = {1: review_row(id=1, round=1, ts="2026-08-16T10:00:00+00:00", confirmed=9),
            2: review_row(id=2, round=2, ts="2026-08-16T12:00:00+00:00")}
    board["reviews"] = ([rows[n] for n in order], "")
    c = preland.check_review("o/r", pr())
    assert c.status == "passed" and c.detail["round"] == 2


def test_rounds_sharing_a_timestamp_break_the_tie_on_id(board):
    board["reviews"] = ([review_row(id=1, round=1, confirmed=9),
                         review_row(id=2, round=2)], "")
    assert preland.check_review("o/r", pr()).detail["round"] == 2


# ---------------------------------------------------------------- merge_claim


def test_an_unclaimed_branch_passes(board):
    board["claims"] = ({"claims": []}, "")
    assert preland.check_merge_claim("o/r", pr(), "").status == "passed"


def test_another_agents_merge_claim_holds(board):
    board["claims"] = ({"claims": [{"holder": "zeus/opal-kelp", "acquired": "12:00",
                                    "note": "landing"}]}, "")
    c = preland.check_merge_claim("o/r", pr(), "zeus/thorn-spruce")
    assert c.status == "failed" and "zeus/opal-kelp" in c.reasons[0]


def test_your_own_claim_is_not_a_conflict(board):
    """A lander takes the claim and then asks for the verdict; without this it
    would hold its own merge."""
    board["claims"] = ({"claims": [{"holder": "zeus/me", "acquired": "12:00"}]}, "")
    c = preland.check_merge_claim("o/r", pr(), "zeus/me")
    assert c.status == "passed" and c.summary == "held by you"


def test_unreadable_claims_hold(board):
    board["claims"] = (None, "board unreachable")
    assert preland.check_merge_claim("o/r", pr(), "").status == "error"


# ------------------------------------- #172: an unclaimed repo is not evidence


@pytest.fixture
def claims_by_query(monkeypatch):
    """Double `board_get` on the PARAMS, not only the path.

    `check_merge_claim` reads /claims twice now — once for this branch's key, once
    to find out whether this repo claims anything at all — and the distinction
    between those two answers is the whole of what the capability warning says.

    The third read is `/plan`, because half the claims #172 introduces are keyed on
    a board id and say no repo at all: `plan` is what a test puts this repo's plan
    into, and `plan_asked` records the query, since asking the wrong scope would
    silence this repo's warning with another repo's list.
    """
    keyed: dict[str, object] = {"scoped": [], "fleet": [], "plan": None,
                                "plan_asked": []}

    def get(path, params):
        if path.strip("/") == "plan":
            keyed["plan_asked"].append(dict(params))
            return keyed["plan"], ("" if keyed["plan"] is not None
                                   else "board unreachable")
        assert path.strip("/") == "claims"
        which = "scoped" if params.get("key") else "fleet"
        return {"claims": keyed[which]}, ""

    monkeypatch.setattr(preland, "board_get", get)
    return keyed


def test_an_unclaimed_branch_in_an_unclaimed_REPO_says_it_proves_nothing(claims_by_query):
    """The module's own rule — "a merge gate that fails open wherever it cannot see
    is not a gate" — applied to the claims table. A `passed` that means "the table
    is empty" reads identically to one that means "nobody is landing this", and the
    first is the state #172 was filed about: `claims()` empty fleet-wide while
    thirteen agents worked three shared checkouts."""
    c = preland.check_merge_claim("o/r", pr(), "")
    assert c.status == "passed", "this is a warning, not a hold — nothing is wrong with the PR"
    assert c.warnings, "an empty claims table passed silently"
    assert "no claims at all" in c.warnings[0]
    assert "qb-claim" in c.warnings[0], "the warning has to name the remedy"


def test_a_repo_that_claims_OTHER_things_is_enrolled_and_gets_no_warning(claims_by_query):
    """The point of the check. This repo takes claims — just not on this branch —
    so `unclaimed` here really is evidence that nobody is landing it."""
    claims_by_query["fleet"] = [{"key": "o/r#172", "holder": "zeus/x"}]
    c = preland.check_merge_claim("o/r", pr(), "")
    assert c.status == "passed" and not c.warnings


def test_another_repos_claims_do_not_count_as_this_one_being_enrolled(claims_by_query):
    """The join is on the repo half of the key — derived by the board, prefix-tested
    here — because "somebody somewhere claims things" says nothing about whether
    the agents in THIS tree collide silently."""
    claims_by_query["fleet"] = [{"key": "other/repo#5", "holder": "zeus/x"}]
    c = preland.check_merge_claim("o/r", pr(), "")
    assert c.warnings and "all in other repos" in c.warnings[0]


def test_a_repo_whose_NAME_is_a_prefix_of_this_one_does_not_count(claims_by_query):
    """`o/r` is a prefix of `o/rx`, so a bare `startswith` read a neighbour's claim
    as this repo's — silencing the warning in exactly the case it exists for. The
    separator (`#`, `!` or `:`) is what the board always puts after a repo in a key,
    so requiring one costs nothing and closes the class."""
    claims_by_query["fleet"] = [{"key": "o/rx#5", "holder": "zeus/x"}]
    c = preland.check_merge_claim("o/r", pr(), "")
    assert c.warnings, "a neighbouring repo's claim counted as this repo being enrolled"


def test_a_held_branch_is_not_also_warned_about(claims_by_query):
    """When the key itself has a claim, the check has a real answer and the
    capability question is moot — a warning there would be noise on the one path
    that is working."""
    claims_by_query["scoped"] = [{"holder": "zeus/opal-kelp", "acquired": "12:00"}]
    c = preland.check_merge_claim("o/r", pr(), "zeus/thorn-spruce")
    assert c.status == "failed" and not c.warnings


def test_a_repo_that_claims_its_PLAN_is_enrolled_even_though_no_key_says_so(
        claims_by_query):
    """The keys the warning cannot see. A plan claim is `plan:<uuid>` and a ref-less
    item `item:<uuid>` — no repo in either, deliberately, because a board object may
    span repos — so a repo whose agents use the plan-level claim #172 added was the
    repo most likely to be told it claims nothing at all."""
    claims_by_query["fleet"] = [{"key": "plan:6f2c-…", "holder": "zeus/x"}]
    claims_by_query["plan"] = {
        "counts": {"claimed": 0, "covered": 3},
        "plans": [{"label": "the annex", "claim": {"holder": "zeus/x"}}]}
    c = preland.check_merge_claim("o/r", pr(), "")
    assert c.status == "passed" and not c.warnings, (
        "a repo using plan-level claims was warned as claiming nothing at all")


def test_a_plan_claimed_before_it_has_any_items_still_counts(claims_by_query):
    """The moment the plan claim exists FOR: the surveying agent holds the list
    before there is anything exact in it to hold, so `counts` is all zeroes and the
    plan's own claim is the only evidence there is."""
    claims_by_query["plan"] = {
        "counts": {"open": 0, "claimed": 0, "covered": 0},
        "plans": [{"label": "survey", "claim": {"holder": "zeus/x"}}]}
    c = preland.check_merge_claim("o/r", pr(), "")
    assert not c.warnings


def test_a_repo_whose_plan_is_entirely_free_is_still_warned_about(claims_by_query):
    """A plan with nothing held in it is not enrolment — it is a list nobody has
    picked up, which is the state this whole issue is about."""
    claims_by_query["plan"] = {"counts": {"open": 4, "claimed": 0, "covered": 0},
                               "plans": [{"label": "stage one", "claim": None}]}
    c = preland.check_merge_claim("o/r", pr(), "")
    assert c.warnings and "is claimed by anybody" in c.warnings[0]


def test_the_plan_read_is_scoped_to_this_repo_only(claims_by_query):
    """`exact`, because a repo read widens to the fleet-wide list — and another
    repo's plan silencing this repo's warning is the mistake the separator test
    above exists to prevent, one scope up."""
    claims_by_query["plan"] = {"counts": {}, "plans": []}
    preland.check_merge_claim("o/r", pr(), "")
    assert claims_by_query["plan_asked"] == [{"repo": "o/r", "exact": "true",
                                              "limit": "1"}]


def test_a_plan_read_that_fails_leaves_the_warning_standing(claims_by_query):
    """Best-effort, on the safe side: an outage read as "this repo is fine" would be
    the fail-open the whole check exists to close."""
    claims_by_query["plan"] = None                 # the fixture answers with an error
    c = preland.check_merge_claim("o/r", pr(), "")
    assert c.status == "passed" and c.warnings


def test_a_plan_answer_that_is_not_a_dict_is_not_read_as_enrolment(claims_by_query):
    """Same rule the claims list has: an answer this cannot parse is not evidence
    of anything, and least of all of the thing that silences the warning."""
    claims_by_query["plan"] = ["not", "a", "plan"]
    c = preland.check_merge_claim("o/r", pr(), "")
    assert c.warnings


def test_a_second_board_read_that_fails_says_nothing_extra(board):
    """The capability read is best-effort by design: the caller already has its own
    answer about the key, and a check that turned an outage on the SECOND call into
    a different verdict would be reporting the network rather than the repo."""
    board["claims"] = ({"claims": []}, "")
    # The `board` fixture keys on the path, so both reads get the same empty
    # answer; what matters here is that a well-formed empty answer still produces a
    # warning and never an error.
    c = preland.check_merge_claim("o/r", pr(), "")
    assert c.status == "passed"


# ----------------------------------------------------------------- migrations


def test_a_repo_without_the_reconciler_skips_silently(repo):
    """Capability detection: an absent script means the invariant does not exist
    in that repo, which is what lets one gate serve every repo."""
    c = preland.check_migrations(repo, BASE)
    assert c.status == "skipped-absent"


@pytest.fixture
def reconciler(repo, monkeypatch):
    """A repo that has `scripts/migration_reconcile.py`, with its answer stubbed."""
    (Path(repo) / "scripts" / "migration_reconcile.py").write_text("#\n")

    def answer(plan, code=0):
        monkeypatch.setattr(preland, "run",
                            lambda argv, cwd=None: proc(code, json.dumps(plan)))
    return repo, answer


def test_a_single_head_passes(reconciler):
    root, answer = reconciler
    answer({"action": "noop", "reason": "already linear"})
    assert preland.check_migrations(root, BASE).status == "passed"


@pytest.mark.parametrize("action", ["relink", "renumber"])
def test_a_relink_is_mechanical_work_with_commands(reconciler, action):
    root, answer = reconciler
    answer({"action": action, "reason": "rebase onto main head",
            "base": "0018abc", "base_path": "migrations/versions/0018_x.py",
            "new_down": ["0017abc"]})
    c = preland.check_migrations(root, BASE)
    assert c.status == "reconcile"
    assert "apply --onto origin/main" in c.actions[0].command
    # `base`/`new_down` are revision IDS; only the *_path fields are filenames.
    assert c.actions[0].files == ["migrations/versions/0018_x.py"]
    # apply writes and deliberately does not commit, so the commit is its own step.
    assert "git add" in c.actions[1].command


def test_a_renumber_reports_both_ends_of_every_rename(reconciler):
    """A renumber moves files, and a caller told only the new name cannot see
    what it replaced."""
    root, answer = reconciler
    answer({"action": "renumber", "reason": "two branches minted 0018",
            "renames": [{"old_path": "migrations/versions/0018_x.py",
                         "new_path": "migrations/versions/0019_x.py"}]})
    c = preland.check_migrations(root, BASE)
    assert c.actions[0].files == ["migrations/versions/0018_x.py",
                                  "migrations/versions/0019_x.py"]


def test_the_merge_fallback_brings_base_into_the_branch(reconciler):
    root, answer = reconciler
    answer({"action": "merge", "reason": "the base is itself a merge node"}, code=3)
    c = preland.check_migrations(root, BASE)
    assert c.status == "reconcile"
    assert c.actions[0].command == "git merge origin/main"
    # `alembic`, not `flask db` — the prose this replaced named a Flask-Migrate
    # wrapper that does not exist in an app driving alembic directly, so the
    # command it prescribed could not run here at all.
    assert "alembic merge heads" in c.actions[1].command


def test_a_stop_holds_because_reconciling_base_is_not_this_prs_job(reconciler):
    root, answer = reconciler
    answer({"action": "stop", "reason": "main itself has two heads"}, code=2)
    c = preland.check_migrations(root, BASE)
    assert c.status == "failed" and "main itself has two heads" in c.reasons[0]


def test_an_action_this_check_does_not_know_holds(reconciler):
    """A new action name must never read as noop — the tool chooses, and a
    choice this cannot interpret is a choice it must not overrule."""
    root, answer = reconciler
    answer({"action": "squash", "reason": "something new"})
    c = preland.check_migrations(root, BASE)
    assert c.status == "failed" and "does not know" in c.reasons[0]


def test_a_reconciler_that_says_nothing_readable_holds(reconciler, monkeypatch):
    root, _ = reconciler
    monkeypatch.setattr(preland, "run", lambda argv, cwd=None: proc(1, "", "boom"))
    c = preland.check_migrations(root, BASE)
    assert c.status == "error" and "boom" in c.reasons[0]


def test_a_guardrail_that_cannot_be_launched_holds_rather_than_crashing(monkeypatch):
    """No `uv` on the box is a HOLD with a sentence, not a traceback and no
    verdict at all."""
    monkeypatch.setattr(preland.subprocess, "run",
                        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("uv")))
    result = preland.run(["uv", "run", "python", "x.py"])
    assert result.returncode == 127 and "could not run uv" in result.stderr


def test_a_wedged_guardrail_times_out_into_a_verdict(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="x", timeout=preland.RUN_TIMEOUT)

    monkeypatch.setattr(preland.subprocess, "run", boom)
    result = preland.run(["sleep", "forever"])
    assert result.returncode == 124 and "timed out" in result.stderr


# ----------------------------------------------------------------- sw_version


def test_a_branch_that_deleted_the_guardrail_does_not_get_a_skip(repo, monkeypatch):
    """Capability detection reads the BRANCH's tree, which is the point — and
    also the hole: a diff that removes `scripts/migration_reconcile.py` would
    hand itself `skipped-absent`, switching off the check by the very change the
    check exists to read. An absence only counts when the base lacks it too."""
    monkeypatch.setattr(preland, "run", lambda argv, cwd=None: proc(0))  # it IS on base
    c = preland.check_migrations(repo, BASE)
    assert c.status == "failed" and "cannot switch off the guardrail" in c.reasons[0]


def test_a_base_that_could_not_be_refreshed_holds(reconciler):
    """A verdict against a stale origin/<base> is confidently wrong in the
    direction that lands, so a fetch that FAILED must not be silently proceeded
    past — which is what discarding its result used to do."""
    root, _ = reconciler
    stale = preland.BaseRef("main", fresh=False, why="could not resolve host")
    c = preland.check_migrations(root, stale)
    assert c.status == "error" and "could not be refreshed" in c.reasons[0]


def test_no_fetch_is_the_callers_choice_and_does_not_hold(reconciler):
    root, answer = reconciler
    answer({"action": "noop", "reason": "linear", "exit_code": 0})
    chosen = preland.BaseRef("main", fresh=False, chosen=True)
    assert preland.check_migrations(root, chosen).status == "passed"


def test_the_run_says_when_the_base_was_not_refreshed():
    """One fact about the run, said once — not repeated on each check that
    depends on it."""
    out = preland.payload({"github": "o/r", "path": "/x"}, pr(), [check("a", "passed")],
                          preland.BaseRef("main", fresh=False, chosen=True))
    assert out["base_fresh"] is False and "--no-fetch" in out["warnings"][0]


def test_a_custom_migrations_dir_is_passed_to_the_reconciler(repo, monkeypatch):
    """The reconciler defaults to `migrations/versions`; a repo whose migrations
    live elsewhere would otherwise be analysed as having none — which reports
    NOOP, the cleanest possible answer, about a directory nobody looked in."""
    (Path(repo) / "scripts" / "migration_reconcile.py").write_text("#\n")
    seen = []
    monkeypatch.setattr(preland, "run", lambda argv, cwd=None:
                        (seen.append(argv), proc(0, json.dumps({"action": "noop",
                                                                "exit_code": 0})))[1])
    preland.gather({"github": "o/r", "path": repo, "epic": {"migrations_dir": "db/rev"}},
                   pr(), BASE, dict.fromkeys(
                       ("pr_state", "checkout", "ci", "review", "merge_claim",
                        "queue", "sw_version"), "skipped-flag"))
    assert seen[0][-2:] == ["--versions-path", "db/rev"]


def test_a_plan_that_disagrees_with_its_own_exit_code_holds(reconciler):
    """Two independent statements of one answer, compared against the plan's OWN
    `exit_code` rather than a second copy of its action-to-code table."""
    root, answer = reconciler
    answer({"action": "noop", "exit_code": 0}, code=2)
    c = preland.check_migrations(root, BASE)
    assert c.status == "error" and "disagree" in c.reasons[0]


def test_a_noop_that_came_back_non_zero_holds(reconciler):
    """The fallback when the plan states no exit code: reading only the plan is
    how a failing run carrying a NOOP would have been accepted as clean."""
    root, answer = reconciler
    answer({"action": "noop"}, code=2)
    c = preland.check_migrations(root, BASE)
    assert c.status == "error" and "does not come back non-zero" in c.reasons[0]


def test_a_hostile_branch_name_cannot_escape_an_emitted_command(reconciler):
    """`actions` are strings a loop is told to run verbatim, and a git refname may
    legally contain `;`. An unquoted branch name in one of them is an injection
    into a shell this file does not own."""
    root, answer = reconciler
    answer({"action": "merge", "reason": "two heads", "exit_code": 3}, code=3)
    nasty = preland.BaseRef("main;rm -rf /")
    c = preland.check_migrations(root, nasty)
    assert c.actions[0].command == "git merge 'origin/main;rm -rf /'"


def test_a_repo_without_the_cache_guard_skips_silently(repo):
    assert preland.check_sw_version(repo, BASE).status == "skipped-absent"


@pytest.fixture
def sw_guard(repo, monkeypatch):
    (Path(repo) / "scripts" / "check_sw_version.py").write_text("#\n")

    def answer(code, said=""):
        monkeypatch.setattr(preland, "run", lambda argv, cwd=None: proc(code, said))
    return repo, answer


def test_a_monotonic_counter_passes(sw_guard):
    root, answer = sw_guard
    answer(0, "✓ OK: SERVICE_WORKER_VERSION 1.0.154 > base 1.0.153.")
    assert preland.check_sw_version(root, BASE).status == "passed"


def test_a_regression_is_mechanical_work(sw_guard):
    root, answer = sw_guard
    answer(1, "✗ REGRESSION: SERVICE_WORKER_VERSION 1.0.9 is BELOW base 1.0.153.")
    c = preland.check_sw_version(root, BASE)
    assert c.status == "reconcile" and "--fix" in c.actions[0].command


def test_a_broken_multiline_value_holds(sw_guard):
    """The tool's `--fix` refuses this case, so proposing it as the remedy would
    hand the caller a command that cannot work."""
    root, answer = sw_guard
    answer(1, "HEAD has no plain SERVICE_WORKER_VERSION literal (broken multiline?)")
    assert preland.check_sw_version(root, BASE).status == "failed"


def test_a_case_the_tool_declined_to_fix_holds(sw_guard):
    root, answer = sw_guard
    answer(2, "✗ Cannot --fix: base ref has an unparseable version.")
    assert preland.check_sw_version(root, BASE).status == "failed"


# --------------------------------------------------- which checks run, and why


def test_an_unknown_skip_name_is_a_hard_error():
    """Unlike every other unknown name in .harness-rules, which is warned about
    and dropped: a gate silently running one check short is the failure this
    whole file exists to prevent."""
    with pytest.raises(SystemExit, match="no such check"):
        preland.disabled_checks({}, ["reveiw"])


def test_an_unknown_name_in_the_rules_file_is_a_hard_error():
    with pytest.raises(SystemExit, match="no such check"):
        preland.disabled_checks({"preland": {"disabled_checks": ["migration"]}}, [])


def test_a_non_list_disabled_checks_is_a_hard_error():
    with pytest.raises(SystemExit, match="must be a list"):
        preland.disabled_checks({"preland": {"disabled_checks": "review"}}, [])


def test_the_two_ways_of_turning_a_check_off_stay_apart():
    off = preland.disabled_checks({"preland": {"disabled_checks": ["review"]}}, ["ci"])
    assert off == {"review": "skipped-disabled", "ci": "skipped-flag"}


def test_a_skipped_check_is_still_in_the_payload(monkeypatch, repo):
    """#75's lesson in a new place: a payload must never read clean because the
    guardrail that would have objected is simply not in it."""
    monkeypatch.setattr(preland, "_git", lambda root, *a: HEAD if a[0] == "rev-parse" else "")
    cfg = {"github": "o/r", "path": repo}
    checks = preland.gather(cfg, pr(), BASE, {"review": "skipped-disabled",
                                     "merge_claim": "skipped-flag",
                                     "queue": "skipped-flag"})
    by_name = {c.name: c for c in checks}
    assert [c.name for c in checks] == list(preland.CHECKS)
    assert by_name["review"].status == "skipped-disabled"
    assert by_name["merge_claim"].summary == "turned off (flag)"


def test_a_check_that_crashes_holds_and_the_others_still_run(monkeypatch, repo):
    """A bug in one guardrail must not cost the whole verdict. A loop that gets no
    verdict is a loop deciding for itself."""
    monkeypatch.setattr(preland, "_git", lambda root, *a: HEAD if a[0] == "rev-parse" else "")
    monkeypatch.setattr(preland, "check_ci",
                        lambda *a: (_ for _ in ()).throw(KeyError("statusCheckRollup")))
    checks = preland.gather({"github": "o/r", "path": repo}, pr(), BASE,
                            {"review": "skipped-flag", "merge_claim": "skipped-flag",
                             "queue": "skipped-flag"})
    by_name = {c.name: c for c in checks}
    assert by_name["ci"].status == "error" and "KeyError" in by_name["ci"].reasons[0]
    assert by_name["pr_state"].status == "passed"
    assert preland.verdict_of(checks) == preland.HOLD


def test_the_payload_gathers_every_reason_across_checks():
    checks = [check("pr_state", "failed", reasons=["closed"]),
              check("review", "failed", reasons=["stale", "confirmed"])]
    out = preland.payload({"github": "o/r", "path": "/x"}, pr(), checks, BASE)
    assert out["verdict"] == preland.HOLD and out["exit_code"] == 2
    assert out["reasons"] == ["closed", "stale", "confirmed"]


# ------------------------------------------------------------- the site config
#
# The reader and `board_config` MOVED to `harness_rules` when #305's dial layer
# became their second reader, which is where preland's own comment always said
# they belonged. The behaviour these pin is unchanged; only the module holding it
# is, and `preland.board_config` is still the same function by import.


def test_the_config_reader_takes_assignments_and_ignores_the_rest(tmp_path):
    """It reads a file bash SOURCES, so it must not be able to RUN what is in
    it — a config read that evaluates is a config write that executes."""
    f = tmp_path / "config"
    f.write_text('# a comment\n'
                 'export QUARTERBACK_BASE_URL="https://qb.example"\n'
                 "QUARTERBACK_TOKEN_CMD='cat /run/tok'\n"
                 'if [ -n "$x" ]; then\n'
                 'QUARTERBACK_AGENT=zeus\n')
    assert harness_rules._config_file(f) == {
        "QUARTERBACK_BASE_URL": "https://qb.example",
        "QUARTERBACK_TOKEN_CMD": "cat /run/tok",
        "QUARTERBACK_AGENT": "zeus",
    }


def test_a_missing_config_file_is_empty_not_an_error(tmp_path):
    assert harness_rules._config_file(tmp_path / "nope") == {}


def test_the_environment_beats_the_config_file(monkeypatch, tmp_path):
    f = tmp_path / "config"
    f.write_text("QUARTERBACK_BASE_URL=https://from-file\n")
    monkeypatch.setattr(harness_rules, "QB_CONFIG", f)
    monkeypatch.setenv("QUARTERBACK_BASE_URL", "https://from-env/")
    monkeypatch.setenv("QUARTERBACK_TOKEN", "t")
    assert preland.board_config() == ("https://from-env", "t", "")


def test_an_unset_board_url_is_an_error_and_never_a_guess(monkeypatch, tmp_path):
    """The fleet has more than one board and they are deliberately disjoint, so a
    default would point this agent at another island's."""
    monkeypatch.setattr(harness_rules, "QB_CONFIG", tmp_path / "nope")
    monkeypatch.delenv("QUARTERBACK_BASE_URL", raising=False)
    url, token, why = preland.board_config()
    assert (url, token) == ("", "") and "no default" in why


def test_a_board_with_no_resolvable_token_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(harness_rules, "QB_CONFIG", tmp_path / "nope")
    monkeypatch.setattr(harness_rules, "QB_TOKEN_FILE", tmp_path / "nope-either")
    monkeypatch.setenv("QUARTERBACK_BASE_URL", "https://qb.example")
    monkeypatch.delenv("QUARTERBACK_TOKEN", raising=False)
    assert "no board token" in preland.board_config()[2]


# -------------------------------------------------------------- end to end


@pytest.fixture
def wired(monkeypatch, board, repo):
    """Every outside edge doubled: the repo config, `gh`, git and the board."""
    monkeypatch.setattr(preland, "resolve_repo",
                        lambda spec: {"github": "o/r", "path": repo, "name": "r",
                                      "default_branch": "main", "_rules_from": "test"})
    monkeypatch.setattr(preland, "read_pr", lambda repo_, n, root: pr(number=n or 7))
    monkeypatch.setattr(preland, "_git", lambda root, *a: HEAD if a[0] == "rev-parse" else "")
    # A fetch succeeds; `cat-file -e origin/main:<script>` does not, because the
    # tmp repo has no guardrails on either side. Answering 0 to everything would
    # make every run look like a branch that had DELETED its guardrails.
    monkeypatch.setattr(preland, "run", lambda argv, cwd=None:
                        proc(1 if "cat-file" in argv else 0))
    board["claims"] = ({"claims": []}, "")
    # The line this PR is at the head of. Written here rather than left to the
    # default outage because an end-to-end fixture that could not reach the queue
    # would make every one of these tests a test of the board being down.
    board["merge-queue"] = ({"active_order": [7],
                             "you": {"queued": True, "position": 1, "is_head": True,
                                     "may_integrate": True, "may_merge": True,
                                     "reason": "head and ready", "waiting_on": None}},
                            "", 200)
    return board


def test_a_clean_pr_exits_zero(wired, capsys):
    wired["reviews"] = ([review_row()], "")
    assert preland.main(["--pr", "7", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "READY"


def test_a_held_pr_exits_two(wired, capsys):
    wired["reviews"] = ([review_row(confirmed=8)], "")
    assert preland.main(["--pr", "7", "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "HOLD" and out["reasons"]


def test_an_unearned_stop_is_a_ready_by_default_and_a_hold_under_the_flag(wired, capsys):
    """#100, end to end: the flag has to reach the check, not just exist on the
    parser. The same round, the same board, two verdicts — and the default one is
    unchanged, because `/fix-and-land` on a two-seat headless box still has to be
    able to reach green."""
    wired["reviews"] = ([review_row(stop_confident=False,
                                    stop_veto=["the round cap ran out"])], "")
    assert preland.main(["--pr", "7", "--json"]) == 0
    lax = json.loads(capsys.readouterr().out)
    assert lax["verdict"] == "READY" and lax["warnings"]

    assert preland.main(["--pr", "7", "--json", "--require-earned-stop"]) == 2
    strict = json.loads(capsys.readouterr().out)
    assert strict["verdict"] == "HOLD"
    assert any("the stop was not earned" in r for r in strict["reasons"])


def test_the_report_names_every_check_that_ran(wired, capsys):
    wired["reviews"] = ([review_row()], "")
    preland.main(["--pr", "7"])
    printed = capsys.readouterr().out
    assert all(name in printed for name in preland.CHECKS)


def test_a_ci_job_skips_the_check_it_would_otherwise_be(wired, capsys):
    """A `preland` check running on `pull_request` is itself one of the checks
    `ci` reads, so it would gate on its own pending status."""
    wired["reviews"] = ([review_row()], "")
    assert preland.main(["--pr", "7", "--json", "--skip", "ci"]) == 0
    assert json.loads(capsys.readouterr().out)["checks"]["ci"]["status"] == "skipped-flag"


def test_the_base_ref_is_refreshed_before_the_repo_guardrails_run(wired, monkeypatch):
    """A migration verdict against a stale origin/<base> is confidently wrong in
    the direction that lands."""
    ran = []
    monkeypatch.setattr(preland, "run", lambda argv, cwd=None:
                        (ran.append(argv), proc(1 if "cat-file" in argv else 0))[1])
    wired["reviews"] = ([review_row()], "")
    preland.main(["--pr", "7", "--json"])
    assert ["git", "-C", preland.resolve_repo(None)["path"],
            "fetch", "origin", "main"] in ran


def test_no_fetch_leaves_the_refs_alone(wired, monkeypatch):
    ran = []
    monkeypatch.setattr(preland, "run", lambda argv, cwd=None:
                        (ran.append(argv), proc(1 if "cat-file" in argv else 0))[1])
    wired["reviews"] = ([review_row()], "")
    preland.main(["--pr", "7", "--json", "--no-fetch"])
    assert not any("fetch" in argv for argv in ran)


def test_pr_zero_is_refused():
    with pytest.raises(SystemExit):
        preland.main(["--pr", "0"])


def test_gh_is_asked_from_the_repo_it_resolved(monkeypatch):
    """With `--pr` omitted, `gh` picks the PR from the CURRENT branch — and the
    caller's shell is not necessarily standing in the repo `--repo` named. A
    verdict about the wrong PR would arrive with nothing signalling it."""
    seen = {}

    def fake_sh(argv, **kw):
        seen.update(argv=argv, cwd=kw.get("cwd"))
        return json.dumps(pr())

    monkeypatch.setattr(preland, "sh", fake_sh)
    preland.read_pr("o/r", None, "/srv/checkout")
    assert seen["cwd"] == "/srv/checkout"
    assert "--repo" in seen["argv"] and not any(a.isdigit() for a in seen["argv"])


def test_a_gh_that_says_nothing_usable_is_a_clean_exit_not_a_traceback(monkeypatch):
    monkeypatch.setattr(preland, "sh", lambda argv, **kw: "not json")
    with pytest.raises(SystemExit, match="no usable JSON"):
        preland.read_pr("o/r", 7, "/x")


# ----------------------------------------------------- queue (#227, the stop)


@pytest.fixture
def queue(monkeypatch):
    """Double `board_request` for `/merge-queue`, body and HTTP status both.

    Two things are being doubled and only one of them is the body. The status is
    how `check_queue` tells a board that has no queue (404, a capability answer)
    from one whose queue broke (500, an objection), and a fixture that returned
    only the body could not express the difference the check turns on.

    Anything not `/merge-queue` falls through to an outage, which is what an
    unasked-for path actually means to this module.
    """
    state: dict = {"body": None, "err": "board unreachable (test default)",
                   "status": None, "asked": []}

    def request(path, params):
        if path.strip("/") != preland.QUEUE_PATH:
            return None, "board unreachable (test default)", None
        state["asked"].append(dict(params))
        return state["body"], state["err"], state["status"]

    monkeypatch.setattr(preland, "board_request", request)
    return state


def line(*, queued=True, position=1, is_head=True, may_merge=True, order=(7,),
         reason="fine", waiting_on=None):
    """A `GET /merge-queue?pr=…` answer, in the board's own shape."""
    return {"active_order": list(order),
            "you": {"queued": queued, "position": position, "is_head": is_head,
                    "may_integrate": is_head, "may_merge": may_merge,
                    "reason": reason, "waiting_on": waiting_on}}


def queued(state, body):
    state["body"], state["err"], state["status"] = body, "", 200


def test_the_head_of_the_line_passes(queue):
    queued(queue, line(order=(7, 9)))
    c = preland.check_queue("o/r", pr())
    assert c.status == "passed" and not c.reasons
    assert "head of the line for main" in c.summary


def test_a_pr_behind_another_is_not_ready_and_is_told_by_whom(queue):
    """#317 built the queue and stopped at the contract, saying so: "nothing yet
    forces the stop". This is the stop. A second ready PR must not rebase, push or
    restart CI — that is a whole run spent to learn what the board already says,
    and the push invalidates the head's green checks on the way past."""
    queued(queue, line(
        queued=True, position=3, is_head=False, may_merge=False, order=(9, 4, 7),
        reason="queued behind #9, position 3 of 3 — do not rebase, push or restart CI",
        waiting_on={"pr": 9, "holder": "zeus/opal-kelp", "note": "landing #9"}))
    c = preland.check_queue("o/r", pr())
    assert c.status == "failed", "a non-head PR reached READY"
    assert preland.verdict_of([c]) == preland.HOLD
    assert c.summary == "position 3 of 3"
    said = c.reasons[0]
    assert "queued behind #9" in said and "do not rebase" in said
    assert "zeus/opal-kelp" in said, "the stand-down does not name who to go and ask"
    assert "leaving would re-join at the back" in said


def test_standing_down_never_tells_a_loop_to_leave_the_line(queue):
    """The one stop that keeps its entry. A loop that read "not your turn" as
    "leave" would go to the back of the line every time it was overtaken, which
    starves the PR — worse than the racing the queue replaced."""
    queued(queue, line(queued=True, position=2, is_head=False, may_merge=False,
                       order=(9, 7), reason="queued behind #9",
                       waiting_on={"pr": 9, "holder": "zeus/x", "note": None}))
    said = preland.check_queue("o/r", pr()).reasons[0]
    assert "Stay queued" in said and "your place is kept" in said


def test_a_lone_pr_on_an_empty_line_sees_no_new_friction(queue):
    """The other half of the contract. A gate that made the ordinary case harder
    is a gate people turn off, and a human landing one PR with nobody else waiting
    must not meet a queue at all."""
    queued(queue, line(queued=False, position=None, is_head=False, may_merge=False,
                       order=(), reason="#7 is not in the queue for this base"))
    c = preland.check_queue("o/r", pr())
    assert c.status == "passed" and not c.reasons and not c.warnings
    assert c.summary == "nobody is queued to land on main"


def test_skipping_the_queue_while_others_are_in_it_is_not_a_way_past_this_check(queue):
    """#169's defect wearing the queue's clothes: if never enqueueing passed, the
    way round the gate would be to not use the mechanism."""
    queued(queue, line(queued=False, position=None, is_head=False, may_merge=False,
                       order=(9, 4), reason="#7 is not in the queue for this base"))
    c = preland.check_queue("o/r", pr())
    assert c.status == "failed"
    assert "#9, #4" in c.reasons[0] and "merge_queue_enqueue" in c.reasons[0]


def test_a_head_whose_entry_is_behind_the_branch_warns_and_does_not_hold(queue):
    """This check rules on POSITION and nothing else. Readiness is preland's own
    output, so holding for want of it would be this file refusing to run until it
    had already run — and the board's sentence about it is the caller's next step,
    which is a warning's job."""
    queued(queue, line(may_merge=False,
                       reason="#7 is the head, but it has moved to abc since it "
                              "enqueued at def: re-run preland and re-enqueue"))
    c = preland.check_queue("o/r", pr())
    assert c.status == "passed" and not c.reasons
    assert "re-run preland" in c.warnings[0]
    assert "this run is that re-check" in c.warnings[0]


def test_the_queue_is_asked_about_the_base_and_this_head(queue):
    """Keyed on the base, and pinned to the commit. Asking without `head` would let
    an entry's readiness outlive the commit it was about, which is the permanent
    green light the queue exists to remove."""
    queued(queue, line())
    preland.check_queue("o/r", pr())
    assert queue["asked"] == [{"repo": "o/r", "base": "main", "pr": 7, "head": HEAD}]


def test_a_board_with_no_queue_is_a_capability_answer_not_a_failure(board):
    """The endpoint landed in #317 and a board deployed before it answers 404 —
    the same fact as a repo with no `scripts/migration_reconcile.py`. Every host
    HOLDing until its board is redeployed would be a gate people turn off."""
    board["merge-queue"] = (None, "board answered HTTP 404", 404)
    board["claims"] = ({"claims": []}, "")
    c = preland.check_queue("o/r", pr())
    assert c.status == "skipped-absent"
    assert preland.verdict_of([c]) == preland.READY


def test_a_404_from_a_board_that_answers_nothing_else_is_not_a_capability_answer(board):
    """Codex, round 1. 404 is also what a base URL pointed at the wrong host
    returns, and what a proxy with no upstream returns — and reading either of
    those as "this board has no queue" fails the gate open on exactly the
    misconfiguration it has no other way of noticing. So the absence is
    corroborated against a route that predates the queue by a long way."""
    board["merge-queue"] = (None, "board answered HTTP 404", 404)
    board["claims"] = (None, "board answered HTTP 404 for /claims", 404)
    c = preland.check_queue("o/r", pr())
    assert c.status == "error", (
        "a board that cannot answer /claims either was read as one that merely "
        "predates the queue, so a mis-pointed board URL reaches READY")
    assert preland.verdict_of([c]) == preland.HOLD


def test_a_404_corroborated_against_a_REFUSED_claims_read_is_not_absence(board):
    """A 401 on /claims is a token problem, not a board without a queue. Anything
    that is not a clean answer leaves the 404 uncorroborated."""
    board["merge-queue"] = (None, "board answered HTTP 404", 404)
    board["claims"] = (None, "board answered HTTP 401 — the token was refused", 401)
    assert preland.check_queue("o/r", pr()).status == "error"


def test_a_queue_that_cannot_be_read_holds_and_names_the_off_switch(queue):
    """A line this gate cannot see is a line it cannot rule on — the module's own
    rule about the review check, word for word."""
    queue["body"], queue["err"], queue["status"] = None, "board answered HTTP 500", 500
    c = preland.check_queue("o/r", pr())
    assert c.status == "error"
    assert 'disabled_checks": ["queue"]' in c.reasons[0]


def test_an_answer_with_no_you_verdict_is_unreadable_not_empty(queue):
    """Reading a shape this cannot parse as "you are not queued" would report a
    position about a namespace it never managed to look at."""
    queue["body"], queue["err"], queue["status"] = {"active_order": [9]}, "", 200
    assert preland.check_queue("o/r", pr()).status == "error"


def test_the_queue_check_takes_no_claim_and_makes_no_write(queue):
    """#317's `test_being_at_the_head_takes_no_merge_claim`, from this side. The
    queue is ordering AROUND the `kind=merge` claim, not a second lock: this check
    reads one endpoint with GET and touches nothing else."""
    queued(queue, line())
    preland.check_queue("o/r", pr())
    assert len(queue["asked"]) == 1, "the queue check made more than the one read"


def test_the_queue_check_actually_runs_in_a_gather(repo, monkeypatch, board):
    """#169 in the cheapest place it happens: a check written, tested and never
    listed in `CHECKS` is a guardrail that exists only in its own unit tests. The
    board answer here is the fixture's default outage, so an unwired check shows
    up as a run that reached READY with nothing objecting."""
    monkeypatch.setattr(preland, "_git",
                        lambda root, *a: HEAD if a[0] == "rev-parse" else "")
    assert "queue" in preland.CHECKS
    checks = preland.gather({"github": "o/r", "path": repo}, pr(), BASE,
                            {"review": "skipped-flag", "merge_claim": "skipped-flag"})
    by_name = {c.name: c for c in checks}
    assert by_name["queue"].status == "error", (
        "the queue check is not wired into `gather`, so nothing runs it on a real "
        "verdict and #227's stop is a mechanism that shipped unwired")
    assert preland.verdict_of(checks) == preland.HOLD


# ------------------------------- #318: the merge claim keys on the BASE branch


def test_the_merge_claim_is_read_on_the_base_not_the_head(board, monkeypatch):
    """#318. `check_merge_claim`'s docstring names the incident it exists to
    prevent — "on the same day two agents merged at once" — and a head-branch key
    does not prevent it: two agents landing two DIFFERENT PRs into `main` hold
    `o/r:feat/a` and `o/r:feat/b`, never see each other, and both merge. The base
    is what a simultaneous merge collides on, and it is what the queue keys on."""
    asked: list[dict] = []

    def get(path, params):
        asked.append(dict(params))
        return {"claims": []}, ""

    monkeypatch.setattr(preland, "board_get", get)
    preland.check_merge_claim("o/r", pr(headRefName="feat/a", baseRefName="main"), "")
    assert asked[0] == {"kind": "merge", "key": "o/r:main"}, (
        "the merge claim is still keyed on the head branch, so two agents landing "
        "different PRs into one base hold different keys and neither sees the other")


def test_two_prs_landing_into_one_base_contend_on_one_key(board):
    """The property the key change buys, stated as the incident. Under the old
    reading these two calls produced two different keys and both passed."""
    board["claims"] = ({"claims": [{"holder": "zeus/opal-kelp", "acquired": "12:00",
                                    "note": "landing #9"}]}, "")
    for branch in ("feat/a", "feat/b"):
        c = preland.check_merge_claim("o/r", pr(headRefName=branch), "zeus/me")
        assert c.status == "failed", f"{branch} did not see the land in progress"
        assert "o/r:main" in c.reasons[0] and "landing onto main" in c.reasons[0]


def test_the_ci_vocabulary_is_loaded_from_a_trusted_path_and_not_off_sys_path():
    """The lander's red-CI fixer operates on an upstream-authored dependabot branch,
    and this file refuses to read that branch's `.harness-rules` for exactly that
    reason. A bare `import qbdata` would search the caller's `sys.path` — which in a
    checkout can be the checkout — and hand a PR the chance to execute a `qbdata.py`
    of its own inside a merge gate. Found by Codex on #324."""
    import ast

    import harness_rules
    fn = ast.parse(inspect.getsource(harness_rules._qbdata).lstrip()).body[0]
    # The docstring is where this rule is written down, so it is not evidence
    # either way — the assertion is about the code under it.
    code = "\n".join(ast.unparse(node) for node in fn.body[1:])
    assert "import qbdata" not in code, code
    assert "sys.path.insert" not in code and "sys.path.append" not in code, code
    candidates = harness_rules._qbdata_candidates()
    assert candidates and all(c.name == "bin" for c in candidates), candidates


def test_the_gate_and_a_dashboard_share_one_qbdata_module():
    """Two copies would be two caches and two sets of monkeypatches, which is how a
    stubbed probe in one and a live one in the other look identical until something
    reaches the network in CI."""
    import harness_rules
    assert harness_rules._qbdata() is qd
