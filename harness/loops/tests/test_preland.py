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
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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
    """Double `board_get`, keyed by path. Anything unasked for is an outage,
    which is what a missing answer actually means to this module."""
    answers: dict[str, tuple[object, str]] = {}

    def get(path, params):
        return answers.get(path.strip("/"), (None, "board unreachable (test default)"))

    monkeypatch.setattr(preland, "board_get", get)
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


@pytest.mark.parametrize("rollup,status", [
    ([{"conclusion": "SUCCESS"}], "passed"),
    ([{"conclusion": "FAILURE"}], "failed"),
    ([{"status": "IN_PROGRESS"}], "failed"),
])
def test_ci_gates_on_green_only(rollup, status):
    """Pending fails as hard as red: a reconcile push restarts CI, so an earlier
    green is a statement about a commit that is no longer the head."""
    assert preland.check_ci(pr(statusCheckRollup=rollup)).status == status


def test_a_pr_with_no_checks_at_all_holds():
    """No CI signal is the absence of evidence, not evidence of green. A workflow
    that failed to trigger and a repo with no CI look identical from here, and
    only one of them is safe — so the repo that genuinely has none says so in
    .harness-rules, and the message names that switch."""
    c = preland.check_ci(pr(statusCheckRollup=[]))
    assert c.status == "failed"
    assert "no checks at all" in c.reasons[0] and "disabled_checks" in c.reasons[0]


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
                        "sw_version"), "skipped-flag"))
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
                                     "merge_claim": "skipped-flag"})
    by_name = {c.name: c for c in checks}
    assert [c.name for c in checks] == list(preland.CHECKS)
    assert by_name["review"].status == "skipped-disabled"
    assert by_name["merge_claim"].summary == "turned off (flag)"


def test_a_check_that_crashes_holds_and_the_others_still_run(monkeypatch, repo):
    """A bug in one guardrail must not cost the whole verdict. A loop that gets no
    verdict is a loop deciding for itself."""
    monkeypatch.setattr(preland, "_git", lambda root, *a: HEAD if a[0] == "rev-parse" else "")
    monkeypatch.setattr(preland, "check_ci",
                        lambda p: (_ for _ in ()).throw(KeyError("statusCheckRollup")))
    checks = preland.gather({"github": "o/r", "path": repo}, pr(), BASE,
                            {"review": "skipped-flag", "merge_claim": "skipped-flag"})
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


def test_the_config_reader_takes_assignments_and_ignores_the_rest(tmp_path):
    """It reads a file bash SOURCES, so it must not be able to RUN what is in
    it — a config read that evaluates is a config write that executes."""
    f = tmp_path / "config"
    f.write_text('# a comment\n'
                 'export QUARTERBACK_BASE_URL="https://qb.example"\n'
                 "QUARTERBACK_TOKEN_CMD='cat /run/tok'\n"
                 'if [ -n "$x" ]; then\n'
                 'QUARTERBACK_AGENT=zeus\n')
    assert preland._config_file(f) == {
        "QUARTERBACK_BASE_URL": "https://qb.example",
        "QUARTERBACK_TOKEN_CMD": "cat /run/tok",
        "QUARTERBACK_AGENT": "zeus",
    }


def test_a_missing_config_file_is_empty_not_an_error(tmp_path):
    assert preland._config_file(tmp_path / "nope") == {}


def test_the_environment_beats_the_config_file(monkeypatch, tmp_path):
    f = tmp_path / "config"
    f.write_text("QUARTERBACK_BASE_URL=https://from-file\n")
    monkeypatch.setattr(preland, "QB_CONFIG", f)
    monkeypatch.setenv("QUARTERBACK_BASE_URL", "https://from-env/")
    monkeypatch.setenv("QUARTERBACK_TOKEN", "t")
    assert preland.board_config() == ("https://from-env", "t", "")


def test_an_unset_board_url_is_an_error_and_never_a_guess(monkeypatch, tmp_path):
    """The fleet has more than one board and they are deliberately disjoint, so a
    default would point this agent at another island's."""
    monkeypatch.setattr(preland, "QB_CONFIG", tmp_path / "nope")
    monkeypatch.delenv("QUARTERBACK_BASE_URL", raising=False)
    url, token, why = preland.board_config()
    assert (url, token) == ("", "") and "no default" in why


def test_a_board_with_no_resolvable_token_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(preland, "QB_CONFIG", tmp_path / "nope")
    monkeypatch.setattr(preland, "QB_TOKEN_FILE", tmp_path / "nope-either")
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
