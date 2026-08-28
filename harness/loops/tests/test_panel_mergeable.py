"""The panel refuses a branch that cannot merge, BEFORE it spends a round (#271).

The check already existed and ran at the wrong end of the cycle.
`preland.check_pr_state` refuses a CONFLICTING branch at the merge gate — after a
full multi-vendor round and a judge have been spent on it. Measured live on PR
#270: 28 files, 5,572 lines, a branch four commits behind its base, and the
cheapest refusal in the system arriving after the most expensive step. Every
finding in that round was about a diff that had to be rebased before it could
land, and at `review_panel.max_rounds: 1` (PR #247) nothing re-reads what the
rebase changes.

Three things are pinned here, and the third is the one that makes this a fix
rather than a second copy of a check:

* the refusal happens before any seat is dispatched, not after;
* it is LOUD in the payload — `preflight.verdict`, `skip_reason` and
  `config_notes` all say it, because a wrong review target that leaves
  `config_notes: []` is the whole of the sibling complaint in #241;
* the sentence is `preland`'s own. The three pre-land checks in #96 drifted
  because one question was asked in two places in two wordings, and a reviewer
  refusing with one sentence while the merge gate refuses with another is that
  same failure one loop earlier.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402
import preland  # noqa: E402
from conftest import UNSET, gh_stub, pr_meta  # noqa: E402


PR_DIFF = ("diff --git a/a.py b/a.py\n"
           "--- a/a.py\n"
           "+++ b/a.py\n"
           "@@ -1,0 +1,1 @@\n"
           "+first\n")

CFG = {"github": "acme/board", "path": "/tmp/repo",
       "_rules_baseline": ".harness-rules.sample",
       "review_panel": {},
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}}}

#: The sentence the merge gate says about a conflicted branch, asked of the
#: module that owns it rather than transcribed. A copy here would pass while the
#: two implementations drifted, which is the failure this test exists over.
CONFLICT_SAID = preland.mergeability({"mergeable": "CONFLICTING"})[1]


def _run(monkeypatch, tmp_path, *, mergeable="CONFLICTING", cfg_extra=None,
         force=False, title="feat: a thing", mergeable_now=UNSET, calls=None):
    """One panel run against a PR in the given mergeable state, recording which
    seats were dispatched — a seat that runs at all is this issue's defect.

    `mergeable_now` is what the RE-READ answers; it defaults to agreeing with the
    opening one, and is only reached when that said UNKNOWN."""
    cfg = {**CFG, "review_panel": {**CFG["review_panel"], **(cfg_extra or {})}}
    ran = []
    fake_sh = gh_stub(meta=pr_meta(title=title, head="aaa111", mergeable=mergeable),
                      mergeable_now=mergeable_now, calls=calls, diff=PR_DIFF)

    def fake_review(name, model, prompt, effort="", **_kw):
        ran.append(name)
        return panel.ReviewerRun([], None, 800, None)

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(panel_core, "sh", fake_sh)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, panel.CoverageRuling()))
    out = tmp_path / "r.json"
    assert panel.run("e2e", 77, post=False, json_file=str(out), record=False,
                     round_no=1, max_rounds=2, force=force) == 0
    return json.loads(out.read_text()), ran


def test_a_conflicting_branch_is_refused_before_any_seat_runs(monkeypatch, tmp_path):
    """The whole issue in one assertion. The seats are the minutes and the money;
    a refusal that arrives after them has cost everything it exists to save."""
    payload, ran = _run(monkeypatch, tmp_path)
    assert ran == [], "a seat was dispatched against a branch that cannot merge"
    assert payload["reviewed"] is False
    assert payload["preflight"]["verdict"] == "refuse"


def test_the_refusal_is_loud_in_the_payload(monkeypatch, tmp_path):
    """A reader of the JSON must be able to tell, and from more than one field:
    `/panel-review-pr` reads `skip_reason`, the board reads `preflight`, and a
    human reads `config_notes`. #241's round left `config_notes: []` under a
    target that was wrong, and that silence is what made it cost a day."""
    payload, _ = _run(monkeypatch, tmp_path)
    assert "CONFLICTING" in payload["skip_reason"]
    assert "CONFLICTING" in payload["preflight"]["reason"]
    assert any("CONFLICTING" in n for n in payload["config_notes"])
    # And per seat, because `_scorecards` builds a row for every selected
    # reviewer and a missing `reviewers` block reads as "ran, found nothing".
    assert payload["reviewers"]["claude"]["ran"] is False


def test_the_refusal_says_what_the_merge_gate_says(monkeypatch, tmp_path):
    """One implementation, two callers. If `preland` rewords its sentence this
    goes red rather than the two quietly diverging (#96)."""
    payload, _ = _run(monkeypatch, tmp_path)
    assert CONFLICT_SAID in payload["skip_reason"]
    assert any(n.startswith(CONFLICT_SAID) for n in payload["config_notes"])


def test_the_refusal_names_no_ceiling_because_size_was_not_the_question(
        monkeypatch, tmp_path):
    """A gate refusal and a size refusal are the same `refuse` verdict, and the
    size refusal's prose is all about a diff that is too big. Printed over a
    five-line diff it tells an operator to split a PR whose size was never the
    problem — and `cap` is None here, so the same confusion is a `TypeError`
    away from taking down the payload build instead."""
    payload, _ = _run(monkeypatch, tmp_path)
    assert payload["preflight"]["cap"] is None
    assert "Split the PR" not in payload["reviewers"]["claude"]["skip"]
    assert "precondition" in payload["reviewers"]["claude"]["skip"]


def test_a_title_skipped_round_does_not_claim_it_was_refused_for_merging(
        monkeypatch, tmp_path):
    """The title skip returns before this gate is consumed, so a conflicted PR
    called "Merge main into x" reviews nothing for a reason that has nothing to do
    with merging. A note claiming a precondition refusal would be a second,
    contradictory answer to "why is this payload empty"."""
    payload, ran = _run(monkeypatch, tmp_path, title="Merge main into feat/x",
                        cfg_extra={"skip_title_patterns": ["^Merge "]})
    assert ran == []
    assert "title matches skip pattern" in payload["skip_reason"]
    assert not any("CONFLICTING" in n for n in payload["config_notes"])


def test_a_mergeable_branch_is_reviewed_and_says_nothing(monkeypatch, tmp_path):
    """The gate must be quiet in the ordinary case. A note on every run is a note
    that gets trained away, and this one has to be readable when it fires."""
    payload, ran = _run(monkeypatch, tmp_path, mergeable="MERGEABLE")
    assert ran == ["claude"]
    assert payload["preflight"]["verdict"] == "run"
    assert payload["config_notes"] == []


def test_an_uncomputed_mergeability_warns_and_does_not_refuse(monkeypatch, tmp_path):
    """GitHub computes mergeability lazily and answers UNKNOWN until it has. The
    merge gate warns rather than refusing on that, and so does this: refusing on
    "we could not tell" would stop rounds on GitHub's own scheduling. The fact is
    still recorded — an unread precondition is not a satisfied one — and the note
    says the question was put twice, so a reader does not take it for the cold
    first answer it usually is."""
    payload, ran = _run(monkeypatch, tmp_path, mergeable="UNKNOWN")
    assert ran == ["claude"]
    assert payload["preflight"]["verdict"] == "run"
    said = " ".join(payload["config_notes"])
    assert "not computed mergeability yet" in said and "asked for twice" in said


def test_a_cold_unknown_is_asked_again_and_the_second_answer_is_the_one_used(
        monkeypatch, tmp_path):
    """The measurement that makes this gate real rather than decorative. GitHub
    computes mergeability lazily: the first query schedules the merge test and
    answers UNKNOWN while it runs. Three consecutive reads of an open PR on this
    repo gave UNKNOWN, CONFLICTING, CONFLICTING. Asked once, this gate would
    refuse only the PRs somebody happened to have looked at recently."""
    payload, ran = _run(monkeypatch, tmp_path, mergeable="UNKNOWN",
                        mergeable_now="CONFLICTING")
    assert ran == [], "the cold UNKNOWN was taken for an answer"
    assert payload["preflight"]["verdict"] == "refuse"
    assert "CONFLICTING" in payload["skip_reason"]


def test_a_branch_that_can_merge_is_not_asked_twice(monkeypatch, tmp_path):
    """The re-read is for the cold answer only. Spending it on every round would
    be an API call per panel for a question already answered."""
    calls = []
    _run(monkeypatch, tmp_path, mergeable="MERGEABLE", calls=calls)
    asked = [a for a in calls if a[:3] == ["gh", "pr", "view"] and a[-1] == "mergeable"]
    assert asked == []


def test_the_dial_turns_it_off_and_the_round_says_so(monkeypatch, tmp_path):
    """A dial rather than a rule: an architectural read where the conflict is
    incidental, or a PR whose conflict IS the subject, are real cases. What must
    not happen is a conflicted branch being reviewed SILENTLY."""
    payload, ran = _run(monkeypatch, tmp_path,
                        cfg_extra={"require_mergeable": False})
    assert ran == ["claude"]
    assert payload["preflight"]["verdict"] == "run"
    assert any("require_mergeable" in n and "Reviewed anyway" in n
               for n in payload["config_notes"])


def test_force_overrides_it_and_leaves_the_overruled_verdict_behind(
        monkeypatch, tmp_path):
    """`--force` is the per-run override, and this repo's standing rule is that
    "the tool chose to run" and "a caller overrode the tool" must never look
    alike. So the round runs, and `would_have` still records what the panel
    decided."""
    payload, ran = _run(monkeypatch, tmp_path, force=True)
    assert ran == ["claude"]
    assert payload["preflight"]["verdict"] == "run"
    assert payload["preflight"]["forced"] is True
    assert payload["preflight"]["would_have"] == "refuse"
    assert any("--force" in n and "CONFLICTING" in n
               for n in payload["config_notes"])


def test_a_forced_round_reports_the_override_without_a_ceiling(monkeypatch, tmp_path,
                                                               capsys):
    """The report's `--force` notice formats `pre.cap`, which a gate refusal does
    not have. Left on the size wording it raises `TypeError` from the middle of
    the report — on the one path whose entire purpose is to warn a human."""
    _run(monkeypatch, tmp_path, force=True)
    out = capsys.readouterr().out
    assert "`--force` overrode a pre-flight `refuse` verdict" in out
    assert "provisional on the rebase" in out


def test_the_refusal_notice_tells_a_human_to_rebase(monkeypatch, tmp_path, capsys):
    """The notice is what a person finds — in a terminal, or on the PR under
    `--post`. It must not read as a clean review, and its remedies must be about
    the problem it actually found."""
    _run(monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert "🛑 Panel REFUSED — no review happened" in out
    assert "Rebase the branch onto its base" in out
    assert "review_panel.require_mergeable: false" in out
    assert "Split the PR" not in out, "the size remedies are about another problem"
