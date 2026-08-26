"""#165's seven `review_panel` dials, #297's eighth and #492's ninth — thoroughness against convergence, per repo.

The panel had one behaviour and no dials. Every choice it made about what counts as
worth reporting, what a fix round has to clear, and what buys another round was a
constant, and the measurement says those constants do not converge: across seven PRs
panelled on one night, the last round of each raised 201 findings no earlier round
had and **128 of them — 63.7% — were created by the fix pass immediately before it**,
against a ~7% industry baseline for bad-fix injection. Every one of those panels
terminated on the round cap, each saying in its own output "a stop, not convergence".

So there are now nine settings — seven from #165, `low_severity_fix_lines` from #297,
which answers a second measurement taken five days later on the same panel, and
`max_fix_growth_chars` from #492, which answers a field report that the growth ceiling
scales its rope with the starting size — and
this suite is what says they are settings rather than documentation. Each one gets three tests, because there are exactly three ways a
setting fails:

* **its default** — the value a repo that configured nothing gets, which has to be
  the value the rules file documents. A drift between `harness_rules.DEFAULTS` and
  the `panel_core` constant the resolver falls back to is invisible from either side.
* **a non-default value changing behaviour** — the failure #169 is named for, a
  mechanism that ships unwired. A key nothing reads is worse than no key: it reads as
  configured.
* **a bad value being rejected, loudly** — a repo that typed a setting wrong and got
  default behaviour is the failure `warn_unknown_keys` exists to prevent one level
  down, and a `config_notes` line does not stop it, it annotates it: the review still
  runs, under a policy the file did not ask for, in the round the fixer is briefed
  from. So a malformed value of a key this harness KNOWS is a hard exit, the same
  mechanism `harness_rules._check_block_shape` uses. An unknown KEY is the other case
  and keeps its old answer — warned about and dropped — because that one really is
  version skew and failing on it would turn every shared rules file into a version
  pin. Unset is the third and stays silent.

The prose guards are here too rather than in `harness/tests`, because the briefs and
the code are one feature: `fixer_may_defer` is enforced ONLY in prose (there is no
code path that can stop a fixer patching something), so a test suite for it that
reads no markdown is testing nothing at all.
"""

import ast
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402  — `sh` and the dial constants live here
import panel_scope  # noqa: E402  — the two compare calls a scoped round makes
import panel_seats  # noqa: E402  — the resolvers live here
import harness_rules  # noqa: E402  — DEFAULTS, the documented half
from conftest import gh_stub  # noqa: E402

#: The repo root, for the two briefs. Four levels up: tests -> loops -> harness -> repo.
REPO_ROOT = Path(__file__).resolve().parents[3]
REVIEW_PR = REPO_ROOT / "harness/commands/review-pr.md"
PANEL_REVIEW_PR = REPO_ROOT / "harness/commands/panel-review-pr.md"

PANEL_CFG = {"github": "acme/board", "path": "/tmp/acme-board",
             "_rules_baseline": ".harness-rules.sample",
             "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
             "review_panel": {}}

#: The ten, and where each one's default is written twice. `skip_title_patterns` and
#: the rest of the block are not dials and are not listed.
DIALS = {
    "fixer_may_defer": "DEFAULT_FIXER_MAY_DEFER",
    "file_deferral_issues": "DEFAULT_FILE_DEFERRAL_ISSUES",
    "fix_severity_floor": "DEFAULT_FIX_SEVERITY_FLOOR",
    "round_trigger_floor": "DEFAULT_ROUND_TRIGGER_FLOOR",
    "low_severity_fix_lines": "DEFAULT_LOW_SEVERITY_FIX_LINES",
    "max_fix_growth": "DEFAULT_MAX_FIX_GROWTH",
    "max_fix_growth_chars": "DEFAULT_MAX_FIX_GROWTH_CHARS",
    "reviewer_scope": "DEFAULT_REVIEWER_SCOPE",
    "require_failing_test": "DEFAULT_REQUIRE_FAILING_TEST",
    "max_rounds": "DEFAULT_MAX_ROUNDS",
}


def cfg(**dials):
    """`PANEL_CFG` with a `review_panel` block. Deep-copied, because these tests hand
    the dict straight to `run()` as the resolved config and a shared nested mapping
    would let one test's setting leak into the next."""
    return {**PANEL_CFG, "review_panel": dict(dials)}


def _adjudicate(clusters, diff, model, pr, budget=None, coverage=None, cwd=None,
                ci="", **_kw):
    """Every reported finding confirmed — the judge's ruling is not the subject here."""
    flat = [f for grp in clusters for f in grp]
    return ([panel.Canonical(id=panel._finding_id(pr, i + 1), severity=f.severity,
                             file=f.file, line=f.line, synthesis=f.title,
                             verdict="confirmed", detail=f.detail, reported_by=[f],
                             rationale="real")
             for i, f in enumerate(flat)], None, "")


def stub(monkeypatch, findings, *, config=None, diff=None, prompts=None, sonar=(),
         head="abc", increment=None, prior_diff=""):
    """Every process a run would spawn, replaced. `prompts` collects what each seat
    was actually handed, which is the only way to test `reviewer_scope` — its whole
    enforcement is the text of the brief. `sonar` seats SonarCloud with a red gate and
    those issues as its hard ones, which is the only way to reach the floors' one
    exemption through the real `run()`.

    `increment` puts the round under the DEFAULT `increment` round scope: `head` moves
    away from the anchor the baseline recorded, and the two compare round trips
    `ReviewScope.decide` makes are answered here rather than through `gh`. Both are
    needed together — a round whose head has not moved falls back to whole-PR scope,
    which is the case the growth ceiling was already right about (#298)."""
    resolved = config or PANEL_CFG
    if sonar:
        resolved = {**resolved,
                    "reviewers": {**resolved["reviewers"],
                                  "sonarqube": {"enabled": True}}}
        monkeypatch.setattr(panel, "review_sonarqube",
                            lambda *a, **k: ("ERROR", list(sonar), [], None))
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: resolved)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "fix: a real bug", "additions": 3, "deletions": 1,
              "headRefName": "h", "headRefOid": head},
        diff=diff or "diff --git a/a.py b/a.py\n+x\n",
        compare='{"status": "ahead", "files": [{"filename": "a.py", "patch": "@@"}]}'))
    if increment is not None:
        # Two ranges, told apart by their right-hand end: `anchor...head` is the fix
        # commit (the review target), `base...anchor` is the PR as the anchoring round
        # read it (the near-context tier).
        monkeypatch.setattr(panel_scope, "fetch_increment",
                            lambda repo, a, b: ((increment, "") if b == head
                                                else (prior_diff, "")))
        monkeypatch.setattr(panel_scope, "compare_facts",
                            lambda *a: {"status": "ahead", "files": 1, "commits": 1,
                                        "total_commits": 1, "merges": 0})

    def review(cmd_name, model, prompt, *a, **k):
        if prompts is not None:
            prompts.append(prompt)
        return panel.ReviewerRun(list(findings), None, 10, [])

    monkeypatch.setattr(panel, "review_llm", review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", _adjudicate)


def run(monkeypatch, capsys, tmp_path, findings, *, round_no=1, baseline=(),
        max_rounds=2, config=None, diff=None, prompts=None, scope="auto",
        name="r", sonar=(), head="abc", increment=None, prior_diff=""):
    """One whole panel run: the report it prints and the payload it writes."""
    stub(monkeypatch, findings, config=config, diff=diff, prompts=prompts, sonar=sonar,
         head=head, increment=increment, prior_diff=prior_diff)
    out = tmp_path / f"{name}{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline),
                     max_rounds=max_rounds, scope=scope) == 0
    return capsys.readouterr().out, json.loads(out.read_text()), str(out)


def finding(severity, title="unvalidated input", file="a.py"):
    return panel.Finding("claude", severity, file, 3, title, "")


def gate_issue(severity, title="unused import", file="b.py", rule="python:S1128"):
    """One SonarCloud hard-gate issue, at whatever severity Sonar gave it — routinely
    P3 or P4, which is the whole reason the floors need an exemption."""
    return panel.Finding("sonarqube", severity, file, 7, title, rule)


def sonar_canonical(severity, title="unused import", file="b.py"):
    """The same thing as `panel.py` builds it for `outstanding`: a `Canonical` over its
    own single account, carrying **Sonar's** severity and `verdict="sonar"` — the field
    `round_stop` identifies a gate issue by."""
    reports = [gate_issue(severity, title, file)]
    return panel.Canonical(id="34-F02", severity=severity, file=file, line=7,
                           synthesis=title, verdict="sonar", reported_by=reports,
                           rationale="python:S1128")


def judged(severity, title="unvalidated input", file="a.py"):
    """A judge-confirmed finding, for the tests that have to tell a Sonar exemption from
    "P3 goes again"."""
    reports = [panel.Finding("claude", severity, file, 3, title, "")]
    return panel.Canonical(id="34-F01", severity=severity, file=file, line=3,
                           synthesis=title, verdict="confirmed", reported_by=reports,
                           rationale="real")


def notes_about(payload, key):
    return [n for n in payload["config_notes"] if f"`{key}`" in n]


# --------------------------------------------------------------- the two halves agree

@pytest.mark.parametrize("key,const", sorted(DIALS.items()))
def test_the_documented_default_is_the_applied_default(key, const):
    """`harness_rules.DEFAULTS` is what an operator reads; the `panel_core` constant is
    what the resolver falls back to when a rules file (or a test literal) does not
    carry the key. A drift between them is invisible from either side — the file
    documents one number and the panel applies another, silently, in the direction
    nobody checks."""
    documented = harness_rules.DEFAULTS["review_panel"][key]
    applied = getattr(panel_core, const)
    assert documented == applied, (
        f"`review_panel.{key}` is documented as {documented!r} in harness_rules.DEFAULTS "
        f"and applied as {applied!r} from panel_core.{const}")


@pytest.mark.parametrize("key", sorted(DIALS))
def test_every_dial_is_a_key_the_rules_file_accepts(key):
    """DEFAULTS is also the set of names the block ACCEPTS. A dial documented in the
    README and missing from DEFAULTS is warned about as a typo and DROPPED, so a repo
    that set it gets default behaviour and a warning about a setting that exists."""
    assert not harness_rules.unknown_keys({"review_panel": {key: "whatever"}}), (
        f"`review_panel.{key}` is not in DEFAULTS, so resolve_repo drops it as an "
        "unknown key and nothing ever reads what a repo wrote")


def test_the_payload_records_every_dial_as_applied(monkeypatch, capsys, tmp_path):
    """Not as WRITTEN, and not as DEFAULTED: a reader of the payload has to be able to
    see the policy this round actually ran under without also holding the rules file
    and this harness's defaults. (A malformed value cannot reach here at all any more
    — it is a hard exit — so the interesting case is a legal non-default one.)"""
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                        config=cfg(fix_severity_floor="P1"))
    assert set(payload["review_panel"]) == set(DIALS)
    assert payload["review_panel"]["fix_severity_floor"] == "P1"


# ------------------------------------------ a bad VALUE fails; an unknown KEY does not

#: One malformed value per dial, and the fragment of the accepted set the refusal has to
#: carry. Eight rows because the answer is now one answer: a value of a key this harness
#: KNOWS that it cannot read is a typo by the repo's author, and there is no
#: forward-compatibility argument for tolerating it — no newer harness reads `p-4` as a
#: severity either.
BAD_VALUES = [
    ("fixer_may_defer", "maybe", "true or false"),
    ("file_deferral_issues", "P-2", "P1, P2, P3, P4, always or never"),
    ("fix_severity_floor", "p-4", "P1, P2, P3, P4"),
    ("round_trigger_floor", "blocker", "P1, P2, P3, P4"),
    ("low_severity_fix_lines", "a few", "a whole number"),
    ("max_fix_growth", "lots", "is not a number"),
    ("max_fix_growth_chars", "a lot", "a whole number"),
    ("reviewer_scope", "everything", "diff, repo"),
    ("require_failing_test", "sometimes", "true or false"),
    ("max_rounds", 2.5, "whole number of rounds >= 1"),
]


@pytest.mark.parametrize("key,bad,accepted", BAD_VALUES)
def test_a_malformed_value_of_a_known_key_is_a_hard_exit(key, bad, accepted):
    """All ten, in one table, because the failure they share is the one that matters:
    a repo that wrote `fix_severity_floor: p-4` intending the pre-#165 "fix everything"
    silently got the default, stopped fixing P3s and P4s, and the review ran anyway —
    under a policy the file did not ask for, in the round the fixer was briefed from. A
    `config_notes` line does not stop that; it annotates it.

    `_check_block_shape`'s mechanism and sentence shape, so there is ONE way to be wrong
    about a rules file rather than two, and the message names the key, the value and the
    accepted set — an operator's next action is to edit that key."""
    notes: list[str] = []
    with pytest.raises(SystemExit) as refusal:
        panel_seats.resolve_dials({key: bad}, None, notes)
    msg = str(refusal.value)
    assert f"`review_panel.{key}`" in msg, msg
    assert repr(bad) in msg, msg
    assert accepted in msg, msg
    # It names the file the value came out of, the way every other refusal in the rules
    # resolver does — a message about a key with no file to look in is a hunt.
    assert ".harness-rules" in msg
    # And it refuses INSTEAD of noting: a note plus a fallback is the behaviour this
    # replaced, and a run that emits both is a run that still applied the default.
    assert notes == []


@pytest.mark.parametrize("key,unset", [(k, u) for k, _b, _a in BAD_VALUES
                                       for u in (None, "")])
def test_unset_is_still_the_silent_not_configured_reading(key, unset):
    """The line the hard exit is drawn on has three sides, not two, and this is the
    third. Missing, `null` and `""` are "nobody wrote anything" everywhere in this
    harness and none of them is a mistake — `severity_floor`'s docstring says so and is
    right. `max_fix_growth` is the documented exception in the other direction: a
    WRITTEN null — `null` or `""`, as against an absent key — is its off switch, which
    is still not an error. `low_severity_fix_lines` is the second such key and the
    reading is the same: a written null is "no budget at all", which is not the same
    answer as the default and not a mistake either. `max_fix_growth_chars` is the third
    and inherits the reading from the key it sits beside (#492) — the two halves of one
    ceiling are nulled independently, which is most of why it is a second key."""
    dials = panel_seats.resolve_dials({key: unset}, None, [])
    expected = (None if key in ("max_fix_growth", "max_fix_growth_chars",
                                "low_severity_fix_lines")
                else harness_rules.DEFAULTS["review_panel"][key])
    assert getattr(dials, key) == expected


def test_an_unknown_key_is_still_warned_about_and_dropped():
    """The forward-compatibility case, and NOTHING above touches it. An older harness
    meeting a newer repo's setting must not die on it, or a rules file shared across a
    fleet of boxes that upgrade at different times becomes a version pin on every one of
    them — `warn_unknown_keys`'s own docstring, and `_check_block_shape` draws exactly
    this line one level up. A malformed value of a key this harness knows is not that
    case in any direction."""
    unknown = harness_rules.unknown_keys(
        {"review_panel": {"fix_severity_floor_typo": "P1"}})
    assert unknown == {"review_panel": ["fix_severity_floor_typo"]}
    # And the resolver never sees it: an unknown name is dropped before the dials are
    # read, so it cannot reach `resolve_dials` and be refused as a bad value.
    assert panel_seats.resolve_dials({}, None, []).fix_severity_floor == "P3"


# ------------------------------------------------------------------- 1. fixer_may_defer

def test_fixer_may_defer_is_on_by_default_and_the_brief_says_what_that_permits():
    """The fixer's THIRD exit. The brief used to allow exactly two ways to leave a
    finding unfixed and then say so in as many words — "'Not now' is not available to
    you" — so a fixer that correctly judged "this is real, and it is not what this
    change is for" had no legal way to say it, and the only move left was the patch.
    That is the incentive behind the 63.7%."""
    assert harness_rules.DEFAULTS["review_panel"]["fixer_may_defer"] is True
    brief = REVIEW_PR.read_text(encoding="utf-8")
    flat = " ".join(brief.split())
    assert "Three things may leave a finding unfixed" in flat
    assert "fixer_may_defer" in flat
    # The outcome it maps to already exists; a fifth value costs the row and records
    # nothing, so the brief must name the one the database accepts.
    assert "`fixed | refuted | deferred | superseded`" in flat
    # And it is not a way out of work: the justification and the record are both
    # required, and the orchestrator is the one who files.
    assert 'A deferral is not "not now" as a way out of work' in flat
    assert "#223 and #237" in flat


def test_fixer_may_defer_off_is_the_old_two_exit_behaviour():
    """The non-default value, and the only place it can be enforced: this permission
    has no code path — nothing can stop a sub-agent patching something — so the brief
    has to spell out both settings or the `false` half is undocumented behaviour."""
    flat = " ".join(REVIEW_PR.read_text(encoding="utf-8").split())
    assert ('With `fixer_may_defer` off, the first two are the whole list and '
            '"not now" is not available to you.') in flat


def test_the_bar_for_what_is_in_scope_did_not_move():
    """The opposite error, and the one this change could most easily make. What a fix
    round DOES take on is unchanged: fixed properly, with a test, and note-and-move-on
    still forbidden. A brief that gained an outcome and lost a standard has not gained
    anything."""
    flat = " ".join(REVIEW_PR.read_text(encoding="utf-8").split())
    for standard in ("fix everything you find", "never note a problem and move on",
                     "None of that lowers the bar for what IS in scope"):
        assert standard in flat, f"the brief no longer says {standard!r}"


def test_a_deferral_still_has_to_go_somewhere():
    """A `deferred` with nowhere to go is the markdown list this replaced, and #482
    did not weaken that — it named WHERE. The board row is where every deferral goes;
    a GitHub issue is a second copy the gate decides on. Either way the orchestrator
    is the one who writes it, never the fixer, which is the same division step 3a
    already draws."""
    orchestrator = " ".join(REVIEW_PR.read_text(encoding="utf-8").split())
    assert "deferred_to" in orchestrator
    assert ("the orchestrator records it — a board row always, an issue where "
            "`file_deferral_issues` calls for one. You open nothing") in orchestrator
    # And the prose above the template does not promise an issue the gate may refuse.
    assert "the ORCHESTRATOR opens the issue and records the finding against it" \
        not in orchestrator
    panel_md = " ".join(PANEL_REVIEW_PR.read_text(encoding="utf-8").split())
    assert "Three roads arrive here and all three are the same row" in panel_md
    assert "review_panel.fixer_may_defer" in panel_md


@pytest.mark.parametrize("bad", ["maybe", [], 2])
def test_a_bad_fixer_may_defer_is_refused_and_never_read_as_truthy(bad):
    """`bool("maybe")` is True, and so is `bool([1])`: Python truthiness would turn
    every junk value into the permissive half of a policy switch. Refused rather than
    defaulted, and the message names the accepted spellings — a reader has to be able
    to tell what to write instead."""
    notes: list[str] = []
    with pytest.raises(SystemExit) as refusal:
        panel_seats.panel_flag({"fixer_may_defer": bad}, "fixer_may_defer",
                               True, notes)
    assert "is not true or false" in str(refusal.value)
    assert "yes`/`no" in str(refusal.value)
    assert notes == []


# ---------------------------------------------------------------- 2. fix_severity_floor

def test_by_default_a_p4_is_reported_and_is_not_the_fix_rounds_work(monkeypatch, capsys,
                                                                    tmp_path):
    """The default floor is **P3**, so P4 is the tier held back — 31.3% of findings per
    #165, and the tier that actually ballooned PR #236 (a 54-line README rewrite and a
    decode-path rework, both P4). Reported, recorded, marked — and out of the list a fix
    brief is built from, which is the half that matters, since the fix pass is where the
    damage comes from."""
    report, payload, _ = run(monkeypatch, capsys, tmp_path,
                             [finding("P4", "docstring could mention __vNEXT__")])
    assert "### To fix (0)" in report
    assert "### Reported, not this round's work (1) — below the `P3` fix floor" in report
    assert "🔽" in report
    # Still visible. A cut that HID the finding would be a worse artifact than the one
    # it replaced: the point is that the fixer is not briefed with it, not that nobody
    # is told.
    assert "docstring could mention __vNEXT__" in report
    assert "Do not build a fix brief from this list" in report
    assert payload["to_fix"][0]["below_fix_floor"] is True
    assert payload["review_panel"]["fix_severity_floor"] == "P3"


def test_a_p3_is_the_fix_rounds_work(monkeypatch, capsys, tmp_path):
    """The tier the default deliberately KEEPS, and the reason the floor is P3 rather
    than the measured P2: severity is model-authored and wrong sometimes, and what a P2
    floor systematically misses is correctness expressed as craft — a missing regression
    test on a parser or an auth boundary, a missing timeout or cleanup, a migration
    rollback or idempotency gap, any of which a reviewer may label P3. Fixing one inside
    a pass that is already open and already being verified costs a single edit."""
    report, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P3")])
    assert "### To fix (1)" in report
    assert "Reported, not this round's work" not in report
    assert payload["to_fix"][0]["below_fix_floor"] is False


def test_a_p2_is_the_fix_rounds_work(monkeypatch, capsys, tmp_path):
    """The other side of the same line, so the test above is not passing on a bug that
    empties **To fix** whatever the severity."""
    report, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")])
    assert "### To fix (1)" in report
    assert "Reported, not this round's work" not in report
    assert payload["to_fix"][0]["below_fix_floor"] is False


def test_the_floor_at_p4_is_the_pre_165_behaviour(monkeypatch, capsys, tmp_path):
    """The non-default value, and it has to restore the old report exactly: a repo that
    has not adopted the evidence contract can keep fixing everything, and its **To fix**
    list must not gain a heading or lose a finding."""
    report, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P4")],
                             config=cfg(fix_severity_floor="P4"))
    assert "### To fix (1) — master-confirmed, any reviewer count" in report
    assert "Reported, not this round's work" not in report and "🔽" not in report
    assert payload["to_fix"][0]["below_fix_floor"] is False


def test_the_floor_is_read_case_insensitively(monkeypatch, capsys, tmp_path):
    """`_severity` normalises every severity that enters the panel by stripping and
    upper-casing, so a floor that did not would make `p2` in a hand-written rules file
    a different floor from `P2` in a reviewer's reply."""
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                        config=cfg(fix_severity_floor=" p1 "))
    assert payload["review_panel"]["fix_severity_floor"] == "P1"
    assert not notes_about(payload, "fix_severity_floor")


@pytest.mark.parametrize("bad", ["P0", "blocker", "p2 or better", 2, ["P2"]])
def test_a_bad_fix_floor_refuses_the_run_and_names_the_accepted_set(monkeypatch, capsys,
                                                                    tmp_path, bad):
    """Falling back was the failure mode, note or no note: a repo that wrote `p-4`
    meaning the pre-#165 "fix everything" got the default instead, stopped fixing P3s
    and P4s, and the review still RAN — under a policy the file did not ask for, and the
    round the fixer was briefed from is that round. Unset is a different case and is
    deliberately silent (the test below); an unknown KEY is a third, and stays
    warn-and-drop (`test_an_unknown_key_is_still_warned_about_and_dropped`)."""
    stub(monkeypatch, [finding("P3")], config=cfg(fix_severity_floor=bad))
    with pytest.raises(SystemExit) as refusal:
        panel.run("board", 34, post=False, record=False)
    msg = str(refusal.value)
    assert "`review_panel.fix_severity_floor`" in msg and repr(bad) in msg
    assert "P1, P2, P3, P4" in msg and ".harness-rules" in msg


def test_an_unset_floor_is_silent(monkeypatch, capsys, tmp_path):
    """Absent, null and "" are "use the default" everywhere in this harness, and none of
    them is a mistake — the same reading `diff_budget` gives an absent budget."""
    for unset in ({}, {"fix_severity_floor": None}, {"fix_severity_floor": ""}):
        _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                            config=cfg(**unset))
        assert not notes_about(payload, "fix_severity_floor")
        assert payload["review_panel"]["fix_severity_floor"] == "P3"


# --------------------------------------------------------------- 3. round_trigger_floor

def test_a_new_p3_does_not_by_itself_buy_another_round(monkeypatch, capsys, tmp_path):
    """`round_stop`'s rule 1 went again on a new finding at ANY severity, and from round
    2 the thing under review IS the previous round's fix — so the termination test was
    fed by the loop's own output and could only end on the cap. It always did."""
    report, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P3")])
    stop = payload["round_stop"]
    assert stop["stop"] is True
    assert "none at or above the P2 round trigger floor" in stop["reason"]
    # Reported and recorded, just not a round: the count is in the payload because
    # nothing else says those findings were new and bought nothing.
    assert stop["new_below_trigger_floor"] == payload["new_finding_keys"]
    assert stop["new_below_trigger_floor"]
    assert "**go again**" not in report


def test_a_new_p2_still_buys_a_round(monkeypatch, capsys, tmp_path):
    """The floor must not be a way of never going again."""
    report, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")])
    assert payload["round_stop"]["stop"] is False
    assert "1 finding(s) no earlier round raised" in payload["round_stop"]["reason"]
    assert "**go again**" in report


def test_the_trigger_floor_at_p4_is_the_pre_165_behaviour(monkeypatch, capsys, tmp_path):
    """The non-default value: a new P3 buys a round again, exactly as it used to."""
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P3")],
                        config=cfg(round_trigger_floor="P4"))
    assert payload["round_stop"]["stop"] is False
    assert payload["round_stop"]["new_below_trigger_floor"] == []


def test_a_below_floor_finding_the_fixer_never_had_does_not_repeat_the_cycle_to_the_cap(
        monkeypatch, capsys, tmp_path):
    """The interaction that makes the other two dials work at all. A finding below
    `fix_severity_floor` is one no fix round was asked to clear, so it is outstanding
    every round by construction — and rule 3 ("an earlier round already raised it and
    it is still there") would go again on it until the cap, which is the
    non-convergence this whole change exists to remove. P4 findings, because the fix
    floor defaults to P3 now and a P3 IS work the fix round was asked to clear.

    `low_severity_fix_lines: null` here, which is what makes the round's REQUIRED
    floor the fix floor: under the shipped budget a P3 is work the round was asked to
    clear only while the budget lasts, and `Dials.cleared_floor` raises the bound to
    the cut for that (#297, and the test below it). Null is the pre-#297 reading and
    the one this test was written against."""
    conf = cfg(low_severity_fix_lines=None)
    _, _, r1 = run(monkeypatch, capsys, tmp_path, [finding("P4")], round_no=1,
                   max_rounds=3, config=conf)
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P4")], round_no=2,
                        baseline=[r1], max_rounds=3, scope="pr", config=conf)
    stop = payload["round_stop"]
    assert stop["stop"] is True, stop["reason"]
    assert "already raised" not in stop["reason"]
    assert stop["fix_floor"] == "P3" and stop["trigger_floor"] == "P2"


def test_a_bad_trigger_floor_refuses_the_run(monkeypatch, capsys, tmp_path):
    stub(monkeypatch, [finding("P2")], config=cfg(round_trigger_floor="P5"))
    with pytest.raises(SystemExit) as refusal:
        panel.run("board", 34, post=False, record=False)
    assert "`review_panel.round_trigger_floor`='P5'" in str(refusal.value)
    assert "P1, P2, P3, P4" in str(refusal.value)


# -------------------------------------------------------------------- 4. max_fix_growth

#: A first round of about 1,200 chars, and a second of about six times that. The shape
#: #236 measured: 359 insertions to 2,313, none of the 67 findings in the bug fix.
SMALL = "diff --git a/a.py b/a.py\n" + "+one line of fix\n" * 60
HUGE = "diff --git a/a.py b/a.py\n" + "+one line of fix\n" * 400


def test_a_fix_pass_that_multiplies_the_change_stops_the_cycle(monkeypatch, capsys,
                                                               tmp_path):
    """A fix pass that multiplies the diff has written a second change, not a fix. On
    #236 the last one added ~900 lines to a 359-line PR and introduced an unbounded
    FIFO read; the answer is not another round over a bigger change."""
    _, r1_payload, r1 = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                            round_no=1, max_rounds=3, diff=SMALL)
    report, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                             round_no=2, baseline=[r1], max_rounds=3, diff=HUGE,
                             scope="pr")
    stop, growth = payload["round_stop"], payload["round_stop"]["fix_growth"]
    assert growth["over"] is True and growth["limit"] == 3.0
    assert growth["first_chars"] == r1_payload["diff_chars"]
    assert stop["stop"] is True
    assert "max_fix_growth" in stop["reason"] and "splitting" in stop["reason"]
    # A stop, and NOT dressed up as convergence — the same discipline the round cap and
    # a held escalation get.
    assert stop["confident"] is False
    assert any("max_fix_growth" in v for v in stop["veto"])
    assert "a stop, not convergence" in report


#: PR #188's actual round sizes, in churned lines. The PR stood at 185 when round 1
#: read it, 593 after the first fix pass and 721 after the second — 3.21x and then
#: **3.90x**, under a ceiling of 3.0, and the guard fired at neither. Each round after
#: the first reviews the fix commit, so the sizes the old measurement saw are the
#: DIFFERENCES: 408 lines and then 128.
LINE = "+one line of fix\n"
PR_188 = {r: "diff --git a/a.py b/a.py\n" + LINE * n
          for r, n in ((1, 185), (2, 593), (3, 721))}
FIX_188 = {r: "diff --git a/a.py b/a.py\n" + LINE * n for r, n in ((2, 408), (3, 128))}


def test_the_growth_ceiling_measures_the_pr_and_not_the_round(monkeypatch, capsys,
                                                              tmp_path):
    """#298, on PR #188's own three rounds: 185 -> 593 -> 721 churned lines, 3.90x
    under a 3.0x ceiling, and no stop at either round that could have made one.

    `round_scope` decides what the reviewers are asked to LOOK AT; the ceiling asks how
    big the change has BECOME. Taken off the review target, the DEFAULT `increment`
    scope put a fix commit over the cycle's whole-PR starting size — 2.20x at round 2
    and **0.69x at round 3**, both comfortably under any ceiling, while the PR itself
    had nearly quadrupled. The backstop against this repo's measured 63.7% bad-fix
    injection rate was pointed at the wrong number, so it read as configured and
    stopped nothing.

    Run through `run()` rather than against the arithmetic, because the defect was
    never in the arithmetic: it was in which string the numerator came from, and only
    a round that really scopes its target can tell the two apart."""
    # What the old measurement saw, stated as a property of the fixture: both fix
    # commits are UNDER the ceiling against the cycle's starting size, so a test that
    # did not scope its rounds would pass against the unfixed code.
    assert all(len(FIX_188[r]) / len(PR_188[1]) < 3.0 for r in (2, 3))

    _, r1_payload, r1 = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                            round_no=1, max_rounds=4, diff=PR_188[1])
    _, r2_payload, r2 = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=2,
                            baseline=[r1], max_rounds=4, diff=PR_188[2],
                            head="def", increment=FIX_188[2], prior_diff=PR_188[1])
    _, r3_payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=3,
                           baseline=[r1, r2], max_rounds=4, diff=PR_188[3],
                           head="fed", increment=FIX_188[3], prior_diff=PR_188[2])

    for round_no, payload in ((2, r2_payload), (3, r3_payload)):
        # The rounds really did scope, or this asserts nothing at all: with a whole-PR
        # target the numerator was already right and the test passes unfixed.
        assert payload["scope"] == "increment", f"round {round_no}"
        assert payload["diff_chars"] == len(FIX_188[round_no])
        growth = payload["round_stop"]["fix_growth"]
        assert growth["over"] is True, (
            f"round {round_no}: the PR went {len(PR_188[1]):,} -> "
            f"{len(PR_188[round_no]):,} chars and the ceiling read {growth['ratio']}x "
            "— it measured the fix commit, not the PR")
        assert growth["chars"] == len(PR_188[round_no])
        assert growth["first_chars"] == len(PR_188[1]) and growth["first_round"] == 1
        assert payload["round_stop"]["stop"] is True
        assert payload["round_stop"]["confident"] is False
        # The reported ratio still names WHICH measurement it is — two whole-PR sizes,
        # beside the scope the round reviewed under, which is a different fact.
        assert growth["scope"] == "pr" and growth["first_scope"] == "pr"
        assert growth["review_scope"] == "increment"
        assert any("whole PR" in v and "max_fix_growth" in v
                   for v in payload["round_stop"]["veto"])
        # Where the denominator comes from: every round records the PR's own size
        # beside the scope-dependent size of what it reviewed.
        assert payload["pr_chars"] == len(PR_188[round_no])

    assert r1_payload["pr_chars"] == len(PR_188[1])
    # #188's two headline numbers, to one decimal place as the report prints them.
    assert f"{r2_payload['round_stop']['fix_growth']['ratio']:.1f}" == "3.2"
    assert f"{r3_payload['round_stop']['fix_growth']['ratio']:.1f}" == "3.9"


def test_the_growth_denominator_is_a_whole_pr_size_not_an_increment(tmp_path):
    """The other end of #298, and the reason `pr_chars` is recorded at all.

    A cycle whose only baseline is a SCOPED round — which `--baseline` explicitly
    allows, and which every round 3 of a cycle passed only its predecessor gets — has a
    `diff_chars` that is one fix commit. Read as the cycle's starting size it would put
    a whole PR over a fix commit and stop a cycle that has not grown at all, which is
    the same wrong-numerator error pointing the other way."""
    scoped = tmp_path / "scoped.json"
    scoped.write_text(json.dumps({
        "repo": "board", "github": "acme/board", "pr": 34, "round": 2, "cycle": "cyc",
        "reviewed": True, "scope": "increment", "head_sha": "abc",
        "diff_chars": len(FIX_188[3]), "pr_chars": len(PR_188[3]),
        "reviewers_ran": ["claude"], "to_fix": [], "dismissed": [],
        "sonar_findings": []}))
    prior = panel.load_baseline([str(scoped)],
                                {"github": "acme/board", "pr": 34, "round": 3})
    assert prior.first_reviewed == (2, len(PR_188[3]), "pr")

    # And a payload written before `pr_chars` existed: its increment cannot stand in
    # for a PR, so the check does not run rather than inventing a denominator.
    old = tmp_path / "old.json"
    old.write_text(json.dumps({**json.loads(scoped.read_text()), "pr_chars": None}))
    assert panel.load_baseline([str(old)],
                               {"github": "acme/board", "pr": 34,
                                "round": 3}).first_reviewed is None


def test_a_fix_that_did_not_multiply_the_change_is_not_stopped(monkeypatch, capsys,
                                                               tmp_path):
    """3.0 is deliberately loose: a genuine fix that adds the tests the review asked for
    can easily double a small diff, and that is not the shape this catches."""
    _, _, r1 = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=1,
                   max_rounds=3, diff=SMALL)
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=2,
                        baseline=[r1], max_rounds=3, diff=SMALL + SMALL, scope="pr")
    growth = payload["round_stop"]["fix_growth"]
    assert growth["over"] is False and 1 < growth["ratio"] < 3


def test_null_switches_the_multiple_off_and_says_the_other_half_is_still_there(
        monkeypatch, capsys, tmp_path):
    """The non-default value. `null` is the only spelling of "off" — the default IS a
    number, so reading null as "inherit" like every other setting would leave a check
    whose only job is to stop a cycle with no way to opt out.

    Since #492 it switches off the MULTIPLE and not the ceiling: there is an absolute
    half beside it now, and a repo that wrote this null meant "no growth check",
    because at the time that key WAS the whole check. Collapsing the one null onto both
    halves is the obvious alternative and is worse — it makes a written value mean
    something other than what it names — so the round says so instead, and names the
    key that finishes the job. The stop still does not fire here (`HUGE` is 6.5x on a
    growth of ~5,800 chars), which is what the null bought."""
    _, _, r1 = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=1,
                   max_rounds=3, diff=SMALL, config=cfg(max_fix_growth=None))
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=2,
                        baseline=[r1], max_rounds=3, diff=HUGE, scope="pr",
                        config=cfg(max_fix_growth=None))
    assert payload["review_panel"]["max_fix_growth"] is None
    growth = payload["round_stop"]["fix_growth"]
    assert growth["limit"] is None and growth["over_ratio"] is False
    assert growth["over"] is False
    assert payload["round_stop"]["stop"] is False
    note, = notes_about(payload, "max_fix_growth_chars")
    assert "30,000" in note and "in force" in note
    # And it does NOT fire on the shipped defaults, where nobody wrote anything to be
    # surprised about — a note on every run is a note nobody reads.
    _, plain, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], name="plain")
    assert not notes_about(plain, "max_fix_growth_chars")


def test_an_absent_max_fix_growth_is_the_default_not_off():
    """The one setting where absent and `null` differ, so the difference is asserted
    rather than left to a comment: a key nobody wrote is not an opt-out."""
    assert panel_seats.fix_growth_limit({}, []) == 3.0
    assert panel_seats.fix_growth_limit({"max_fix_growth": None}, []) is None


@pytest.mark.parametrize("bad,why", [(False, "is not a number"),
                                     ("lots", "is not a number"),
                                     (0, "is not above zero"),
                                     (-2, "is not above zero"),
                                     (float("inf"), "is not a finite number")])
def test_a_bad_max_fix_growth_is_refused_and_never_read_as_a_threshold(bad, why):
    """`false` is the other way an operator writes "off" and is rejected rather than
    reinterpreted: `isinstance(True, int)` is True, so read as a number it would become
    the threshold 1.0 and stop every cycle whose fix commit is bigger than its first
    round — the switch flipped to "off" turning the feature all the way on. Refused
    rather than defaulted, and the message still names `null` as the off switch, which
    is what the operator writing `false` was reaching for."""
    notes: list[str] = []
    with pytest.raises(SystemExit) as refusal:
        panel_seats.fix_growth_limit({"max_fix_growth": bad}, notes)
    assert why in str(refusal.value)
    assert "null to switch the check off" in str(refusal.value)
    assert notes == []


# ------------------------------------------------------- 4b. max_fix_growth_chars

#: A large first round and a large absolute growth that is nevertheless UNDER 3.0x —
#: the shape #492 is about. 1,200 churned lines to 3,200: +2,000 lines, which at this
#: repo's measured ~66 chars a churned line is the size of a whole second feature, and
#: 2.66x, which the multiple waves straight through. The bigger the PR the wider that
#: gap gets, and the PR most in need of a ceiling is the one handed the loosest.
BIG = "diff --git a/a.py b/a.py\n" + LINE * 1200
BIGGER = "diff --git a/a.py b/a.py\n" + LINE * 3200


def test_the_absolute_ceiling_stops_growth_the_multiple_waves_through(monkeypatch,
                                                                      capsys, tmp_path):
    """A multiple hands its rope out in proportion to the starting size. At 3.0x a
    113-line PR may grow ~226 lines and a 2,000-line one may grow 4,000, so the same
    dial that stops the first at 226 waves four thousand lines of fix-pass output
    through on the second — and "a fix pass that MULTIPLIES the diff has written a
    second change" is a claim about ABSOLUTE second-change-ness that one multiple
    cannot make at both ends of the range (#492)."""
    # Stated as a property of the fixture, or this test asserts nothing: the multiple is
    # NOT crossed here, so a run against the pre-#492 code goes on to another round.
    # The default is spelled out rather than read off `panel_core`: this line is a
    # property of the FIXTURE, and reading it from the code under test would make it
    # the first thing to fail when the dial is absent — a red run that proves the
    # constant is missing rather than that the ceiling does not bind.
    assert len(BIGGER) / len(BIG) < 3.0
    assert len(BIGGER) - len(BIG) > 30000

    _, _, r1 = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=1,
                   max_rounds=3, diff=BIG)
    report, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                             round_no=2, baseline=[r1], max_rounds=3, diff=BIGGER,
                             scope="pr")
    stop = payload["round_stop"]
    growth = stop["fix_growth"]
    assert growth["over"] is True, growth
    # WHICH half fired, because a stop that named the multiple would send an operator
    # to raise a key that was never crossed.
    assert growth["over_ratio"] is False and growth["over_chars"] is True
    assert growth["grown"] == len(BIGGER) - len(BIG)
    assert growth["limit_chars"] == 30000
    assert stop["stop"] is True and stop["confident"] is False
    assert "`max_fix_growth_chars` ceiling" in stop["reason"]
    assert "`max_fix_growth` ceiling" not in stop["reason"], stop["reason"]
    veto, = [v for v in stop["veto"] if "`max_fix_growth_chars`" in v]
    # The veto's CONCLUSION follows the half that fired. `config_notes` never reaches
    # the board, so this list is the record's only copy of why a cycle ended — and "a
    # fix pass that multiplies the change" is simply false at 2.7x, let alone of the
    # 2,000,000-char baseline that grows by 30,001 and sits at 1.02x.
    assert "whatever the ratio says" in veto
    assert "multiplies the change" not in veto
    assert "a stop, not convergence" in report


def test_the_absolute_half_does_not_swallow_the_multiple_that_already_bound(
        monkeypatch, capsys, tmp_path):
    """The pair can only ever TIGHTEN, and a cycle the multiple already stopped must
    still be told that is what stopped it. `SMALL` -> `HUGE` is 6.5x on a growth of
    ~5,800 chars — nowhere near the absolute — so this is the case where the two
    halves disagree in the other direction."""
    _, _, r1 = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=1,
                   max_rounds=3, diff=SMALL)
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=2,
                        baseline=[r1], max_rounds=3, diff=HUGE, scope="pr")
    growth = payload["round_stop"]["fix_growth"]
    assert growth["over_ratio"] is True and growth["over_chars"] is False
    assert growth["grown"] < 30000
    assert "`max_fix_growth` ceiling" in payload["round_stop"]["reason"]
    assert "`max_fix_growth_chars`" not in payload["round_stop"]["reason"]


#: Over BOTH halves at once: 32.6x on a growth of ~33,000 chars.
BOTH_OVER = "diff --git a/a.py b/a.py\n" + LINE * 2000


def test_a_stop_that_crossed_both_halves_names_both_and_keeps_the_multiplied_wording(
        monkeypatch, capsys, tmp_path):
    """"3.4x AND +38,000 chars" is a different argument for splitting than either
    alone, so both are said. And where the multiple DID fire the conclusion goes back
    to "a fix pass that multiplies the change", because there it is true."""
    _, _, r1 = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=1,
                   max_rounds=3, diff=SMALL)
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=2,
                        baseline=[r1], max_rounds=3, diff=BOTH_OVER, scope="pr")
    stop = payload["round_stop"]
    growth = stop["fix_growth"]
    assert growth["over_ratio"] is True and growth["over_chars"] is True
    veto, = [v for v in stop["veto"] if "`max_fix_growth_chars`" in v]
    assert "`max_fix_growth`" in veto and "ceilings" in veto
    assert "multiplies the change" in veto and "whatever the ratio says" not in veto
    assert "`max_fix_growth` ceiling" in stop["reason"]
    assert "`max_fix_growth_chars` ceiling" in stop["reason"]


def test_a_repo_that_nulled_the_multiple_is_told_in_the_VETO_which_half_stopped_it(
        monkeypatch, capsys, tmp_path):
    """The migration hazard at the one moment it costs something, and it rides on the
    veto rather than staying a `config_notes` line because **`config_notes` never
    reaches the board** — the veto list is the record's only copy, which is the same
    reason a baseline problem is deliberately recorded as both.

    `max_fix_growth` can only be None from a WRITTEN null (an absent key inherits
    3.0), so this branch is exactly the repo that switched "the growth check" off
    before #492 and has now been stopped by the half it never wrote. Without this it
    would read the board record, see a cycle terminated by a ceiling, and have nothing
    telling it the key it already set was not the one that fired."""
    conf = cfg(max_fix_growth=None)
    _, _, r1 = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=1,
                   max_rounds=3, diff=BIG, config=conf)
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=2,
                        baseline=[r1], max_rounds=3, diff=BIGGER, scope="pr",
                        config=conf)
    stop = payload["round_stop"]
    assert stop["stop"] is True and stop["fix_growth"]["over_chars"] is True
    veto, = [v for v in stop["veto"] if "`max_fix_growth_chars`" in v]
    assert "switches off the MULTIPLE only" in veto
    assert "nulling it too is the pre-#492 no-growth-check-at-all" in veto
    # And NOT on a repo that wrote neither key: there the multiple is live, nobody's
    # written intent changed meaning, and a pointer about a migration would be noise.
    _, plain, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=2,
                      baseline=[r1], max_rounds=3, diff=BIGGER, scope="pr", name="p")
    assert not any("switches off the MULTIPLE only" in v
                   for v in plain["round_stop"]["veto"])


def test_null_switches_the_absolute_half_off_and_leaves_the_multiple(monkeypatch,
                                                                     capsys, tmp_path):
    """The non-default value, and the reason this is a second key rather than a
    two-part value inside `max_fix_growth`: the two halves are nulled independently, so
    a repo can keep the multiple and decline the absolute without either of them having
    to answer what half a bare `null` switched off."""
    conf = cfg(max_fix_growth_chars=None)
    _, _, r1 = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=1,
                   max_rounds=3, diff=BIG, config=conf)
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=2,
                        baseline=[r1], max_rounds=3, diff=BIGGER, scope="pr",
                        config=conf)
    assert payload["review_panel"]["max_fix_growth_chars"] is None
    assert payload["review_panel"]["max_fix_growth"] == 3.0
    growth = payload["round_stop"]["fix_growth"]
    assert growth["limit_chars"] is None and growth["over"] is False
    assert payload["round_stop"]["stop"] is False


def test_nulling_both_halves_is_no_growth_check_at_all(monkeypatch, capsys, tmp_path):
    """The pre-#165 behaviour, and it takes two nulls now rather than one. Asserted
    rather than left to a comment, because the block that applies the ceiling runs
    whenever EITHER half is set — the shape that most easily leaves an operator who
    switched "the growth check" off still being stopped by it."""
    conf = cfg(max_fix_growth=None, max_fix_growth_chars=None)
    _, _, r1 = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=1,
                   max_rounds=3, diff=BIG, config=conf)
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=2,
                        baseline=[r1], max_rounds=3, diff=BIGGER, scope="pr",
                        config=conf)
    assert payload["round_stop"]["fix_growth"] is None
    assert payload["round_stop"]["stop"] is False


def test_an_absent_max_fix_growth_chars_is_the_default_not_off():
    """The same distinction `max_fix_growth` draws one function up, and it has to be
    drawn again here: the default is a number, so reading an absent key as `null` would
    leave half a check whose only job is to stop a cycle with no way back on."""
    assert panel_seats.fix_growth_chars_limit({}, []) == 30000
    assert panel_seats.fix_growth_chars_limit({"max_fix_growth_chars": None}, []) is None


@pytest.mark.parametrize("bad,why", [
    (False, "a whole number"),
    ("a lot", "a whole number"),
    (2.5, "a whole number"),
    (0, "above zero"),
    (-30000, "above zero"),
])
def test_a_bad_max_fix_growth_chars_is_refused_and_never_read_as_a_threshold(bad, why):
    """`0` is refused rather than read as "stop the moment it grows at all": a growth
    ceiling of zero is one no cycle that ran a fix pass can be under, so it would stop
    every one of them — the switch turned all the way on, which is the same failure
    `max_fix_growth: false` produces beside it, and `null` is the spelling already
    available for what the operator meant. `false` is refused for that key's reason
    (`isinstance(True, int)`), and a fractional value because half a char is not a size
    any diff has."""
    notes: list[str] = []
    with pytest.raises(SystemExit) as refusal:
        panel_seats.fix_growth_chars_limit({"max_fix_growth_chars": bad}, notes)
    assert why in str(refusal.value)
    assert "null to switch this half of the check off" in str(refusal.value)
    assert notes == []


def test_the_dials_line_names_both_halves_of_the_growth_ceiling(monkeypatch, capsys,
                                                                tmp_path):
    """The report's **Panel dials** line is the only place a round's policy is written
    down where an operator can see it, and `/panel-review-pr.md` briefs the fixer off
    it. A line that named only the multiple would make the absolute stop, when it
    fires, read as an arithmetic bug."""
    report, _, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")])
    assert "fix growth cap 3x or +30,000 chars" in report
    # And "off" only where BOTH are null — a line that vanishes at some settings is one
    # a reader cannot tell from a dial that was never applied.
    off, _, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], name="off",
                    config=cfg(max_fix_growth=None, max_fix_growth_chars=None))
    assert "fix growth cap off" in off


def test_the_orchestrator_is_told_that_naming_findings_does_not_lift_the_budget():
    """#492's third part, and it is prose because the mistake is an orchestrator's.

    `low_severity_fix_lines` caps ACCUMULATION and its docstring is emphatic that the
    question is mechanical rather than discretionary — the spend is counted with `git
    diff --numstat` after each fix and the fixer is never asked whether this risks
    ballooning. On the cycle #492 was filed from the orchestrator LIFTED it for round
    2, because the human had named which findings to fix: it read a narrowed finding
    list as the budget having been spent by decision. The pass came out at 422 lines
    and produced 13 new findings, which is the exact shape the budget exists to
    prevent, and the one brake still capable of firing was the one removed.

    Which findings a pass may touch and how much churn it may add are two controls.
    Nothing in the brief said so, and collapsing them is a natural mistake for an
    orchestrator that has just been handed a shorter list."""
    flat = " ".join(PANEL_REVIEW_PR.read_text(encoding="utf-8").split())
    assert ("Selecting findings and capping churn are INDEPENDENT controls, and naming "
            "findings NEVER lifts the budget (#492)") in flat
    assert "relay the budget with a narrowed list exactly as you would with the full one" in flat
    # And the unpaid remainder goes where a below-floor finding goes, rather than
    # vanishing because a narrower list implied it was already handled.
    assert ("a pass that runs out of it reports the unpaid findings exactly as it "
            "reports below-floor ones") in flat


def test_the_orchestrator_is_told_the_growth_ceiling_now_has_two_halves():
    """The **Panel dials** line is what §4 briefs the fixer from, and an orchestrator
    that believed the ceiling was one number would read the absolute stop as an
    arithmetic bug when it fired."""
    flat = " ".join(PANEL_REVIEW_PR.read_text(encoding="utf-8").split())
    assert "TWO halves, a multiple and an absolute char count" in flat
    assert "stops the cycle on whichever is crossed first" in flat


def test_the_orchestrator_is_told_to_carry_guard_to_guarded_and_not_to_gate_on_it():
    """Report-only has to be said to the actor that would otherwise invent a threshold.
    An orchestrator handed a 6:1 ratio and no rule will supply one, and a rule supplied
    that way is a ceiling with its argument written afterwards (#67)."""
    flat = " ".join(PANEL_REVIEW_PR.read_text(encoding="utf-8").split())
    assert "**Guard-to-guarded**" in flat and "`guard_ratio` in the JSON" in flat
    assert "no threshold here for you to apply and none for you to invent" in flat
    assert "available from round 1's diffstat" in flat


# ----------------------------------------------------- 4c. guard-to-guarded (#492)

def guard_diff(*files):
    """A unified diff that adds `n` lines to each named path — the shape
    `_diff_added_lines` reads, headers and hunk marker included, because the
    classifier's whole input is the `b/` path off those headers."""
    out = []
    for path, added in files:
        out.append(f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
                   f"@@ -0,0 +1,{added} @@\n")
        out.extend("+a line\n" for _ in range(added))
    return "".join(out)


#: The reported cycle's own shape: 406 lines of test for a 66-line config change.
GUARDED = guard_diff(("harness/loops/harness_rules.py", 66),
                     ("harness/loops/tests/test_dials.py", 406))


def test_guard_to_guarded_is_measured_and_reported_from_round_one(monkeypatch, capsys,
                                                                  tmp_path):
    """406 lines of test for a 66-line config change, and nothing in the panel noticed
    that the apparatus built to protect a change had outgrown the change (#492).

    Round ONE, which is the point: `max_fix_growth` needs a second round before it has
    a ratio at all, and this is answerable from the first round's diffstat — which is
    the round where an operator can still act on it cheaply. It is also a DIFFERENT
    failure from raw growth: a fix pass can sit well under 3.0x overall while the
    test-to-source ratio inside it goes to 6:1."""
    report, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                             round_no=1, diff=GUARDED)
    assert "Guard-to-guarded" in report
    assert "406 test + 0 doc line(s) added against 66 source — **6.15:1**" in report
    assert payload["guard_ratio"] == {"test": 406, "doc": 0, "source": 66,
                                      "guard": 406, "ratio": 6.15}
    # REPORT-ONLY (#67's instrument-before-gate, the rule `provenance` and `recurrence`
    # already live under). A threshold invented today would be a ceiling with its
    # argument written afterwards; this one earns a gate over a few dozen cycles or
    # never gets one.
    assert "nothing stops on this" in report
    assert payload["round_stop"]["stop"] is False
    assert not any("uard" in v for v in payload["round_stop"]["veto"])


def test_a_change_that_adds_no_source_has_no_ratio_rather_than_an_infinite_one(
        monkeypatch, capsys, tmp_path):
    """A pure test or docs PR is the commonest benign shape there is, and a large
    number reported for it would be an accusation. The quantity is undefined, not
    enormous, and the line says so instead of dividing."""
    report, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                             round_no=1, diff=guard_diff(("docs/how-it-works.md", 40)))
    assert "no source lines at all — no ratio to take" in report
    assert payload["guard_ratio"] == {"test": 0, "doc": 40, "source": 0,
                                      "guard": 40, "ratio": None}


def test_a_diff_that_adds_nothing_is_not_measured_at_all(monkeypatch, capsys, tmp_path):
    """Null, not a mapping of zeros. "Nobody measured one" and "measured, and it was
    none" are different facts and the payload's other counters are all shaped to keep
    them apart — a consumer handed `{}` here would index it and get a zero for a change
    nothing ever read."""
    deletion = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
                "@@ -1,2 +0,0 @@\n-gone\n-also gone\n")
    report, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                             round_no=1, diff=deletion)
    assert payload["guard_ratio"] is None
    assert "Guard-to-guarded" not in report


def test_the_ratio_counts_lines_ADDED_and_not_churn(monkeypatch, capsys, tmp_path):
    """A deletion is not apparatus being built. Counting churn would make a fix pass
    that rewrites one test file in place read the same as one that writes a second test
    suite, and only the second is the thing this measures."""
    rewrite = ("diff --git a/tests/test_a.py b/tests/test_a.py\n"
               "--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
               "@@ -1,3 +1,2 @@\n-old\n-old\n-old\n+new\n+new\n"
               "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
               "@@ -1,0 +1,4 @@\n+x\n+x\n+x\n+x\n")
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=1,
                        diff=rewrite)
    assert payload["guard_ratio"] == {"test": 2, "doc": 0, "source": 4,
                                      "guard": 2, "ratio": 0.5}


@pytest.mark.parametrize("path,kind", [
    ("harness/loops/panel.py", "source"),
    ("app/api/dials.py", "source"),
    # A whole SEGMENT, never a substring: this is the one way the measurement goes
    # quietly wrong, since a ratio over the wrong files reads exactly like one over
    # the right files.
    ("src/protest/client.py", "source"),
    ("src/contests/rules.py", "source"),
    ("harness/loops/tests/test_panel_dials.py", "test"),
    ("tests/fixtures/big.json", "test"),
    ("conftest.py", "test"),
    ("web/ui/Button.spec.tsx", "test"),
    ("internal/store/store_test.go", "test"),
    # Directory beats basename: a README inside the test tree grew the test tree, and
    # calling it documentation would be true and useless.
    ("tests/README.md", "test"),
    ("docs/architecture.rst", "doc"),
    ("changelog.d/492.feat.md", "doc"),
    ("README.md", "doc"),
    ("harness/commands/panel-review-pr.md", "doc"),
])
def test_a_path_is_classified_by_segment_not_by_substring(path, kind):
    """The classifier is deliberately coarse — it feeds a number that is reported and
    gates nothing, so a misfiled path costs a slightly wrong line rather than a stopped
    cycle. Coarse is not the same as loose about segments, though: `protest` and
    `contests` both contain `test`, and a substring match would count a client library
    as apparatus."""
    assert panel_seats._guard_kind(path) == kind


# --------------------------------------------------------------------- 5. reviewer_scope

def test_by_default_a_reviewer_is_asked_for_defects_in_the_change(monkeypatch, capsys,
                                                                  tmp_path):
    """`review-pr.md` told the panel's own fixer to search the codebase rather than
    review the diff, and the reviewer prompt said the marginal cost of completeness was
    near zero. On #236 that is how a bug fix became 2,313 insertions with none of its
    67 findings in the fix."""
    prompts: list[str] = []
    report, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                             prompts=prompts)
    assert prompts
    sent = prompts[0]
    assert "file findings only" in sent
    assert "search the codebase" not in sent
    assert "this change BREAKS or leaves" in sent
    assert payload["review_panel"]["reviewer_scope"] == "diff"
    assert "reviewer scope diff" in report


def test_repo_scope_restores_the_licence_to_expand_the_change(monkeypatch, capsys,
                                                              tmp_path):
    """The non-default value, and the pre-#165 wording verbatim — a repo whose review
    round is the only pass that ever looks at the neighbours can still have it."""
    prompts: list[str] = []
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                        config=cfg(reviewer_scope="repo"), prompts=prompts)
    assert "search the codebase, don't just review the diff" in prompts[0]
    assert "should change to stay consistent" in prompts[0]
    assert payload["review_panel"]["reviewer_scope"] == "repo"


@pytest.mark.parametrize("scope", ["diff", "repo"])
def test_no_scope_slot_survives_into_a_reviewers_prompt(scope):
    """The slots are literal tokens swapped with `str.replace`, which fails silently: a
    renamed token leaves `<<<REVIEWER_SCOPE>>>` in a prompt that is otherwise complete,
    and a reviewer reads it as part of the instructions."""
    brief = panel_core.reviewer_brief(scope)
    for slot in (panel_core.REVIEWER_SCOPE_SLOT, panel_core.RELATED_CODE_SLOT):
        assert slot not in brief, f"{slot} was never substituted for scope {scope!r}"
    assert "<<<" not in brief.replace("<<<CODE_ACCESS_BRIEF>>>", "")


def test_the_two_briefs_still_take_the_same_format_keys():
    """One closure renders both templates, which is what keeps `SCHEMA_ECHOES` able to
    recognise either prompt's own example rather than filing it as a finding. A slot
    added to the review brief must not have made it a different shape."""
    keys = {"n": 1, "repo": "acme/board", "base": "main", "ci": "", "diff": "d",
            "code": ""}
    assert panel_core.reviewer_brief().format(**keys)
    assert panel_core.MOVE_MANIFEST_PROMPT.format(**keys)


def test_a_bad_reviewer_scope_refuses_the_run(monkeypatch, capsys, tmp_path):
    """`resolve_round_scope`'s own lesson, one setting over — except that a config value
    nothing checks is not merely read as the fallback: the seats are then briefed with a
    question the repo did not ask, and `whole-repo` was plainly reaching for `repo`."""
    stub(monkeypatch, [finding("P2")], config=cfg(reviewer_scope="whole-repo"))
    with pytest.raises(SystemExit) as refusal:
        panel.run("board", 34, post=False, record=False)
    assert "`review_panel.reviewer_scope`='whole-repo'" in str(refusal.value)
    assert "diff, repo" in str(refusal.value)


# ---------------------------------------------------------------- 6. require_failing_test

def test_require_failing_test_is_off_by_default_and_changes_nothing(monkeypatch, capsys,
                                                                    tmp_path):
    """Default False, and the reason is the whole of this setting: the artefact it needs
    does not exist. #92's standing decision is that a reviewer never gains an execution
    capability — it EMITS a test and CI or the fixer runs it — and #114 requires the test
    be shown RED against the unfixed code. Defaulting it on would silently stop findings
    from blocking on the strength of an artefact nobody produces."""
    assert harness_rules.DEFAULTS["review_panel"]["require_failing_test"] is False
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")])
    assert payload["review_panel"]["require_failing_test"] is False
    assert not notes_about(payload, "require_failing_test")


def test_turning_it_on_is_recorded_and_says_it_is_not_enforced(monkeypatch, capsys,
                                                               tmp_path):
    """The non-default value, and what it changes is the REPORT and nothing else. A repo
    that switched it on and saw nothing would reasonably conclude findings were being
    filtered on evidence. They are not, and the round has to say so — otherwise this
    key is the "mechanism that ships unwired" in its purest form."""
    report, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                             config=cfg(require_failing_test=True))
    assert payload["review_panel"]["require_failing_test"] is True
    said = [n for n in payload["config_notes"] if "require_failing_test" in n]
    assert len(said) == 1
    assert "NOT enforced" in said[0] and "#92, #114" in said[0]
    assert "failing test required yes" in report
    # Nothing about the verdict moved: the P2 still buys a round.
    assert payload["round_stop"]["stop"] is False


def test_a_bad_require_failing_test_refuses_the_run(monkeypatch, capsys, tmp_path):
    stub(monkeypatch, [finding("P2")], config=cfg(require_failing_test="sometimes"))
    with pytest.raises(SystemExit) as refusal:
        panel.run("board", 34, post=False, record=False)
    assert "`review_panel.require_failing_test`='sometimes'" in str(refusal.value)
    assert "is not true or false" in str(refusal.value)


# ------------------------------------------------------------------------- 7. max_rounds

def test_the_cap_defaults_to_two(monkeypatch, capsys, tmp_path):
    """#165 proposes 1 and this deliberately keeps 2: round 2 is what caught a serious
    defect CREATED by round 1's fix on #236, so the problem is not that round 2 exists
    — it is that round 1's fix was allowed to be 900 lines. The three settings above
    attack the growth instead, which makes round 2 cheap."""
    # `--max-rounds` is left off throughout: what is under test is the cap `run()`
    # applies when nobody named one. Round 2 with a baseline, because naming a cap is
    # one of the three things that says a run is part of a cycle and the Rounds block
    # only exists for a cycle — a repo SETTING deliberately is not one of the three,
    # since a standing policy is not a caller's declaration that a loop is running.
    _, first, r1 = run(monkeypatch, capsys, tmp_path, [finding("P2")], max_rounds=None)
    assert first["review_panel"]["max_rounds"] == 2
    report, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                             round_no=2, baseline=[r1], max_rounds=None, scope="pr")
    assert "round 2 of at most 2" in report
    assert payload["round_stop"]["max_rounds"] == 2


def test_the_setting_raises_the_cap_over_the_constant(monkeypatch, capsys, tmp_path):
    _, _, r1 = run(monkeypatch, capsys, tmp_path, [finding("P2")], max_rounds=None,
                   config=cfg(max_rounds=4))
    report, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                             round_no=2, baseline=[r1], max_rounds=None, scope="pr",
                             config=cfg(max_rounds=4))
    assert "round 2 of at most 4" in report
    assert payload["round_stop"]["max_rounds"] == 4


def test_the_cli_still_wins_over_the_setting(monkeypatch, capsys, tmp_path):
    """`resolve_round_scope`'s order: `--max-rounds` is the CALLER's cap and only
    `/panel-review-pr` drives a loop, so a caller that named one has said something more
    specific than the repo's standing policy."""
    report, _, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], max_rounds=2,
                       config=cfg(max_rounds=9))
    assert "round 1 of at most 2" in report


def test_a_round_the_setting_allows_is_accepted(monkeypatch, capsys, tmp_path):
    """The guard that refuses `--round N` past the cap moved into `run()` for exactly
    this: checked against the flag and the constant alone, a repo that RAISED its cap
    had the round it asked for refused."""
    report, _, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=3,
                       max_rounds=None, config=cfg(max_rounds=3))
    assert "round 3 of at most 3" in report


def test_a_round_past_the_settings_cap_is_refused(monkeypatch, capsys, tmp_path):
    """And the other half — the cap still binds, and the message names which of the
    three answers supplied it, because "raise the cap" is unactionable without that."""
    stub(monkeypatch, [finding("P2")], config=cfg(max_rounds=2))
    with pytest.raises(SystemExit, match=r"past the cap of 2, from `review_panel"):
        panel.run("board", 34, post=False, record=False, round_no=3)


@pytest.mark.parametrize("bad", [0, -1, 2.5, "two", True])
def test_a_bad_max_rounds_is_refused(bad):
    """A bool is rejected before the integer read: `max_rounds: true` is 1 to Python,
    which would cap every cycle at one round on a value that says nothing about rounds.
    A non-integer is refused rather than rounded — a cap silently lowered runs a round
    the file did not ask for — and now it is refused rather than defaulted, because a
    cycle that ran to a cap nobody wrote cannot be told from one that converged."""
    notes: list[str] = []
    with pytest.raises(SystemExit) as refusal:
        panel_seats.resolve_max_rounds(None, {"max_rounds": bad}, notes)
    assert "whole number of rounds >= 1" in str(refusal.value)
    assert notes == []


def test_a_whole_float_is_accepted_as_a_cap():
    """`2.0` out of a generator is the same cap as `2`; `2.5` is not a cap at all."""
    assert panel_seats.resolve_max_rounds(None, {"max_rounds": 3.0}, []) == 3


# ------------------------------------------------- 8. low_severity_fix_lines (#297)
#
# The eighth dial, and the one measured five days after the other seven landed. PR
# #188's feature was 185 churned lines; two fix passes turned it into 721, so 74% of
# that PR was review-response code, and round 2's fix list was 89% below P2. The fix
# floor cannot see that shape: #188's round 1 was 408 lines of individually reasonable
# small fixes, every one of them correctly admitted by a per-FINDING rule. So the band
# between the two floors gets a combined line budget for the round.

#: The shipped DEFAULTS, written out: this repo's own `.harness-rules.sample` closes the
#: gap between the floors (both P2), which leaves the budget inert — see the test at the
#: end of this section. Every test here that needs a band says so rather than inheriting
#: it, so a later change to either default cannot quietly empty these tests out.
BAND = {"fix_severity_floor": "P3", "round_trigger_floor": "P2"}


def band_run(monkeypatch, capsys, tmp_path, findings, **kw):
    """`run` with the two floors a tier apart, so there IS a band to budget."""
    config = cfg(**{**BAND, **kw.pop("dials", {})})
    return run(monkeypatch, capsys, tmp_path, findings, config=config, **kw)


def test_the_budget_marks_the_band_between_the_floors_and_nothing_else(
        monkeypatch, capsys, tmp_path):
    """WHICH findings are on the budget, which is the whole of the mechanism's reach.

    At or above `round_trigger_floor` a finding is unconditional work and is not marked
    — the measured cut is at P2, zero P1s lost, and #297 is explicit that it is a line
    budget and NOT a severity change. Below `fix_severity_floor` a finding is not this
    round's work at all and keeps its 🔽. The band between them is what the fix floor
    admits and the measurement does not, and it is the only thing 💸 may appear on."""
    report, _, _ = band_run(monkeypatch, capsys, tmp_path,
                            [finding("P1", "a bad cast"), finding("P2", "a race"),
                             finding("P3", "a stale docstring", file="b.py"),
                             finding("P4", "a typo", file="c.py")])
    assert "💸 **P3**" in report
    assert "💸 **P1**" not in report and "💸 **P2**" not in report
    # And the below-floor finding is untouched by any of it: still 🔽, still under its
    # own heading, still never 💸.
    assert "🔽 **P4**" in report and "💸 **P4**" not in report


def test_the_budget_note_states_the_number_the_order_and_the_measurement(
        monkeypatch, capsys, tmp_path):
    """The note under **To fix**, which is the only place the rule reaches a fixer.

    It rides with the list rather than in a section of its own, which is the opposite
    of the choice the below-floor findings get and for the same reason read the other
    way: those must not be swept into a brief, and these must — with their budget. An
    orchestrator pastes the To fix list, so the budget has to be in the heading or the
    fixer is briefed the pre-#297 behaviour: every low finding unconditional.

    Cheapest-first and COUNTED are both load-bearing. Cheapest-first is what makes a
    budget buy the most fixes; counting is what keeps the rule mechanical, and #297 is
    explicit that "does this risk ballooning?" asked of the fixer is a judgement by the
    actor whose judgement the 85% impugns."""
    report, _, _ = band_run(monkeypatch, capsys, tmp_path, [finding("P3")])
    assert "share a 40-line budget for the WHOLE round" in report
    assert "spend cheapest first" in report
    assert "git diff --numstat" in report
    assert "stop when the budget is spent" in report
    # Counted, not estimated, and the fixer is told not to answer the question at all.
    assert "Count, do not estimate" in report
    assert "whether a fix risks ballooning" in report
    # And what the budget does not reach is NOT dropped — the half a reader has to be
    # able to see, or a budget reads as a licence to ignore findings.
    assert "reported and recorded exactly like a below-floor finding" in report


def test_no_budget_is_the_unconditional_pre_297_fix_list(monkeypatch, capsys, tmp_path):
    """A written `null` is the off switch, and off means exactly what the round did
    before this key existed: every finding at or above the fix floor is unconditional
    work, nothing is marked, and no budget note appears at all."""
    report, payload, _ = band_run(monkeypatch, capsys, tmp_path, [finding("P3")],
                                  dials={"low_severity_fix_lines": None})
    assert payload["review_panel"]["low_severity_fix_lines"] is None
    assert "💸" not in report
    assert "budget for the WHOLE round" not in report
    # Said on the dials line anyway. A dial that vanishes from the report when it is
    # off cannot be told from one that was never applied.
    assert "below-P2 fix budget off" in report
    assert payload["to_fix"][0]["budgeted_fix"] is False


def test_a_zero_budget_takes_the_band_out_of_the_fixers_list_entirely(
        monkeypatch, capsys, tmp_path):
    """`0` is a budget that buys nothing, and that is a third answer rather than a
    spelling of "off": the band is then not this round's work in any sense, so it
    leaves the fixer's list and renders where a below-floor finding renders.

    The applied floor rises to the cut with it, and the report says the cut — naming
    `fix_severity_floor` there would name a floor these findings are ABOVE while
    listing them as not this round's work. The blurb names the key that actually
    decided it, because the operator's next action is to edit that key."""
    report, payload, _ = band_run(monkeypatch, capsys, tmp_path, [finding("P3")],
                                  dials={"low_severity_fix_lines": 0})
    assert "### To fix (0)" in report
    assert "🔽 **P3**" in report and "💸" not in report
    assert "Reported, not this round's work (1) — below the `P2` fix floor" in report
    assert "`review_panel.low_severity_fix_lines` is 0" in report
    assert "applied floor is the `P2` cut rather than the `P3` fix floor" in report
    # Recorded as below the floor, not as budgeted: nothing is on a budget of zero.
    row = payload["to_fix"][0]
    assert row["below_fix_floor"] is True and row["budgeted_fix"] is False


def test_the_payload_tells_budgeted_work_from_work_that_is_not_this_rounds(
        monkeypatch, capsys, tmp_path):
    """Three answers, three flags, and the third cannot be spelled by the other two.
    `below_fix_floor` is "not this round's work"; `budgeted_fix` is "this round's work
    while the budget lasts". A programmatic consumer building a fixer's brief has to be
    able to see which without re-deriving either floor."""
    _, payload, _ = band_run(monkeypatch, capsys, tmp_path,
                             [finding("P2", "a race"),
                              finding("P3", "a stale docstring", file="b.py"),
                              finding("P4", "a typo", file="c.py")])
    got = {r["severity"]: (r["below_fix_floor"], r["budgeted_fix"])
           for r in payload["to_fix"]}
    assert got == {"P2": (False, False), "P3": (False, True), "P4": (True, False)}


def test_a_budgeted_finding_the_budget_may_not_have_reached_does_not_run_to_the_cap(
        monkeypatch, capsys, tmp_path):
    """The interaction that makes the budget safe, and it is the one the fix floor
    already needed. `round_stop`'s rule 3 goes again on a finding an earlier round
    raised that is still outstanding, justified on "the fixer was told about them and
    they are still there" — which is exactly as false of a budgeted finding the budget
    ran out before as it is of a below-floor one. The panel sees a fix commit, not a
    ledger, so it cannot tell a repeated budgeted finding the fixer paid for from one
    it never reached. Left unbounded that runs every budgeted cycle to the cap.

    So while a budget is in force the REQUIRED floor is the cut."""
    _, _, r1 = band_run(monkeypatch, capsys, tmp_path, [finding("P3")], round_no=1,
                        max_rounds=3)
    _, payload, _ = band_run(monkeypatch, capsys, tmp_path, [finding("P3")],
                             round_no=2, baseline=[r1], max_rounds=3, scope="pr")
    stop = payload["round_stop"]
    assert stop["stop"] is True, stop["reason"]
    assert "already raised" not in stop["reason"]
    assert stop["fix_floor"] == "P2"


def test_and_with_no_budget_that_same_repeat_still_buys_a_round(
        monkeypatch, capsys, tmp_path):
    """The other half of the test above, and what says it is the BUDGET doing it rather
    than the trigger floor. With no budget the round WAS asked to clear that P3
    unconditionally, so a repeat of it is a fixer that did not do what it was told, and
    rule 3's justification holds word for word."""
    off = {"low_severity_fix_lines": None}
    _, _, r1 = band_run(monkeypatch, capsys, tmp_path, [finding("P3")], round_no=1,
                        max_rounds=3, dials=off)
    _, payload, _ = band_run(monkeypatch, capsys, tmp_path, [finding("P3")],
                             round_no=2, baseline=[r1], max_rounds=3, scope="pr",
                             dials=off)
    stop = payload["round_stop"]
    assert stop["stop"] is False
    assert "already raised" in stop["reason"]
    assert stop["fix_floor"] == "P3"


def test_the_budget_is_inert_where_the_two_floors_meet(monkeypatch, capsys, tmp_path):
    """This repo's own configuration, and it must be a no-op rather than a mis-applied
    one. `.harness-rules.sample` has set both floors to P2 since 2026-08-20, so there
    is no band: every finding is either unconditional or below the floor, and a budget
    with nothing to spend it on must not mark, must not move a floor, and must not
    change which findings a round is asked to clear."""
    report, payload, _ = run(monkeypatch, capsys, tmp_path,
                             [finding("P2", "a race"), finding("P3", "a nit")],
                             config=cfg(fix_severity_floor="P2",
                                        round_trigger_floor="P2"))
    assert "💸" not in report
    assert "budget for the WHOLE round" not in report
    assert payload["to_fix"][1]["below_fix_floor"] is True
    assert payload["to_fix"][1]["budgeted_fix"] is False
    # Printed all the same, at the value the file wrote: a reader of the artifact has
    # to be able to tell "inert here" from "never configured".
    assert "below-P2 fix budget 40 lines" in report


def test_the_dials_answer_the_three_floor_questions_separately():
    """The unit view of the three properties, because the reports above cannot show
    that they are three different questions rather than one asked three times.

    `fix_severity_floor` is what the FILE says. `fix_floor` is what may be fixed at all
    this round. `cleared_floor` is what the round was REQUIRED to clear, which a
    positive budget separates from both."""
    band = dict(fix_severity_floor="P3", round_trigger_floor="P2")
    on = panel_seats.Dials(**band, low_severity_fix_lines=40)
    assert (on.fix_floor, on.cleared_floor) == ("P3", "P2")
    assert on.budgeted("P3") and not on.budgeted("P2") and not on.budgeted("P4")

    zero = panel_seats.Dials(**band, low_severity_fix_lines=0)
    assert (zero.fix_floor, zero.cleared_floor) == ("P2", "P2")
    assert not zero.budgeted("P3"), "nothing is on a budget of zero"

    off = panel_seats.Dials(**band, low_severity_fix_lines=None)
    assert (off.fix_floor, off.cleared_floor) == ("P3", "P3")
    assert not off.budgeted("P3")

    # And with the floors met there is no band, so every answer collapses back.
    met = panel_seats.Dials(fix_severity_floor="P2", round_trigger_floor="P2",
                            low_severity_fix_lines=0)
    assert (met.fix_floor, met.cleared_floor) == ("P2", "P2")
    assert not met.budgeted("P2") and not met.budgeted("P3")

    # The shape that catches a floor derived from the trigger floor without asking
    # whether there is a band at all: a fix floor ABOVE the cut, where "raise the
    # applied floor to the trigger floor" would LOWER it and a repo that asked to fix
    # P1s only would start fixing P2s on the strength of a budget of zero.
    strict = panel_seats.Dials(fix_severity_floor="P1", round_trigger_floor="P2",
                               low_severity_fix_lines=0)
    assert (strict.fix_floor, strict.cleared_floor) == ("P1", "P1")
    assert not strict.budgeted("P1") and not strict.budgeted("P2")


@pytest.mark.parametrize("written,applied", [
    ({}, 40),                       # absent inherits the default
    ({"low_severity_fix_lines": 0}, 0),
    ({"low_severity_fix_lines": 12}, 12),
    ({"low_severity_fix_lines": 12.0}, 12),       # a generator's integral float
    ({"low_severity_fix_lines": " 12 "}, 12),     # a hand's string
    ({"low_severity_fix_lines": None}, None),     # a written null is "no budget"
    ({"low_severity_fix_lines": ""}, None),
])
def test_the_budget_reads_what_a_rules_file_can_legitimately_hold(written, applied):
    """An ABSENT key inherits the default and a WRITTEN null does not, the distinction
    `fix_growth_limit` draws and for a sharper reason: here `0` is a perfectly good
    budget that means the OPPOSITE of off, so collapsing the two would leave one of the
    two readings unwritable and a repo spelling "fix none of them" could get "fix all
    of them"."""
    assert panel_seats.low_severity_budget(dict(written), []) == applied


@pytest.mark.parametrize("bad,accepted", [
    ("a few", "a whole number"),
    (12.5, "a whole number"),        # half a line is not a quantity numstat reports
    ([], "a whole number"),
    (True, "a whole number"),        # `isinstance(True, int)` — a 1-line budget, silently
    (-1, "zero or more"),
])
def test_a_bad_budget_is_refused_rather_than_defaulted(bad, accepted):
    """A bool is rejected before the integer read: `low_severity_fix_lines: false` is
    the other way a hand writes "off", and Python would read it as a 1-line budget —
    which is neither off nor anything else. A negative is refused rather than clamped:
    a repo that wrote one meant something and nothing here can tell which."""
    notes: list[str] = []
    with pytest.raises(SystemExit) as refusal:
        panel_seats.low_severity_budget({"low_severity_fix_lines": bad}, notes)
    msg = str(refusal.value)
    assert accepted in msg, msg
    assert "0 to spend none, or null for no budget at all" in msg
    assert notes == []


def test_the_fixers_brief_carries_the_spend_rule_and_not_a_judgement_call():
    """The prose half, and for this dial it is the half that does the work: nothing in
    the panel can measure a fix that has not been made, so the panel budgets and the
    fixer counts. A brief that named the budget without the procedure would be asking
    the fixer to invent one, which is the discretion #297 exists to remove."""
    flat = " ".join(REVIEW_PR.read_text(encoding="utf-8").split())
    assert "low_severity_fix_lines" in flat
    # Measure, then spend: the order is the whole of what makes cheapest-first
    # possible, since a fix's cost is not knowable until it has been made.
    assert "**Measure before you spend.**" in flat
    assert "run `git diff --numstat` for it" in flat
    assert "**Spend cheapest first, and stop when it runs out.**" in flat
    # Exhaustion mid-list, and the list that fits entirely — both said, because a rule
    # that only describes running out reads as "expect to lose some".
    assert "Stop at the first one that does not fit" in flat
    assert "If the whole list fits, the whole list gets fixed" in flat
    assert 'never ask yourself whether a fix "risks ballooning"' in flat
    # And the unpaid remainder has somewhere to go, or the budget reads as a licence
    # to drop findings.
    assert "recorded `deferred` against the issue you open for the batch" in flat


def test_the_orchestrator_relays_the_marks_with_the_list():
    """`/panel-review-pr` pastes the **To fix** list into a sub-agent's brief, and the
    💸 marks are the only thing separating a budgeted finding from an unconditional
    one. A list pasted without them briefs the pre-#297 behaviour."""
    panel_md = " ".join(PANEL_REVIEW_PR.read_text(encoding="utf-8").split())
    assert "Paste the 💸 marks with the findings that carry them" in panel_md
    assert "low_severity_fix_lines" in panel_md
    # And the unpaid remainder joins the row the below-floor findings already use.
    assert "budget ran out before it, which is the same row for the same reason" \
        in panel_md


# ------------------------------------------- 9. file_deferral_issues (#482)
#
# The tail arriving one step downstream of the fix floor. The floor keeps a P4 out of
# the fix pass and §4b's bookkeeping then filed it as a GitHub issue anyway — twenty
# open issues on this repo that are panel exhaust and nothing else (#66 #69 #72 #74
# #95 #104 #111 #119 #120 #126 #132 #133 #140 #223 #237 #285 #286 #288 #300), one of
# which (#283 rescued three live defects from it) had become a place findings went to
# not be found. The board row is the durable record; the GitHub issue is a work item
# on a human's tracker; for the P3/P4 tail they are not the same thing.


def gate(value=None):
    """A `Dials` with just this dial set, since nothing else here interacts with it."""
    return panel_seats.resolve_dials(
        {} if value is None else {"file_deferral_issues": value}, None, [])


def test_the_default_keeps_the_p3_p4_tail_off_the_tracker():
    """P2: at or above it a deferral is a work item somebody will pick up, and the row
    and the issue coincide. Below it they do not, and below it is where the volume is —
    P3 and P4 are 67.4% of findings by #165's own severity split."""
    d = gate()
    assert d.file_deferral_issues == "P2"
    assert d.files_issue("P1") and d.files_issue("P2")
    assert not d.files_issue("P3") and not d.files_issue("P4")


def test_the_row_is_never_in_question_only_the_issue():
    """The whole claim of this dial, asserted where it can be: nothing here turns a
    finding into no record at all. `files_issue` answers ONE question — is a second
    copy opened on a tracker — and the `deferred` row is written at every setting, which
    is what the briefs say and what `deferral_gist` tells a reader on every round."""
    assert "board row" in gate("never").deferral_gist()
    for value in ("always", "never", "P1", "P4"):
        line = gate(value).deferral_gist()
        assert "row" in line or "issue" in line


def test_always_is_the_pre_482_behaviour():
    """The non-default value that restores what §4b did before this key existed: an
    issue for every deferral, whatever its severity. A repo that has not adopted the
    argument must be able to keep its old bookkeeping in one word."""
    d = gate("always")
    assert all(d.files_issue(s) for s in ("P1", "P2", "P3", "P4"))
    assert d.deferral_gist() == "every deferral gets a GitHub issue"


def test_never_files_nothing_and_says_the_escalation_still_does():
    """The other end, for a repo whose work is not queued on its tracker (`mode:
    jungle`). It is not "discard": the rows are still written and still relayed. And
    even here the line has to name the exemption, or a reader takes `never` literally
    and drops the one issue that is a question rather than a task."""
    d = gate("never")
    assert not any(d.files_issue(s) for s in ("P1", "P2", "P3", "P4"))
    assert "an escalation still does" in d.deferral_gist()


@pytest.mark.parametrize("value", ["never", "always", "P1", "P2", "P3", "P4"])
def test_an_escalation_is_exempt_at_every_setting(value):
    """§4b has three roads to `deferred` and only two of them are work items. An
    escalation's issue *asks* a question about the change's premise — it is what carries
    that question past the end of the session, and the cycle is not finished until a
    human answers it — so suppressing it would drop the question rather than save a
    ticket. Same exemption a Sonar hard-gate issue gets from both severity floors."""
    assert gate(value).files_issue("P4", escalated=True)


@pytest.mark.parametrize("severity", ["", None, "blocker", "critical"])
def test_an_unreadable_severity_files_the_issue(severity):
    """The safe direction, and there is only one. An issue nobody needed costs a line on
    a tracker; withholding one because the severity could not be read leaves the finding
    in a row whose severity nothing can sort by — which is the dumping ground this dial
    exists to prevent, arriving through the back door."""
    assert gate().files_issue(severity)
    # `never` is a decision somebody made about every deferral, so it still holds: the
    # fallback is for an unreadable BAND, not an override of the dial.
    assert not gate("never").files_issue(severity)


@pytest.mark.parametrize("written,applied", [
    (" p2 ", "P2"), ("P4", "P4"), (" ALWAYS ", "always"), ("Never", "never")])
def test_the_gate_is_read_case_insensitively(written, applied):
    """Both halves, each normalised to the spelling its own vocabulary uses everywhere
    else — a band upper-cased like every severity that enters the panel, a word
    lower-cased like every other word in a rules file. One written value must not mean
    two things depending on which layer carried it."""
    assert gate(written).file_deferral_issues == applied


def test_the_report_says_a_below_floor_finding_is_a_board_row_at_the_default(
        monkeypatch, capsys, tmp_path):
    """This list IS §4b's road 2, so the orchestrator reading it is about to decide
    issue-or-row for exactly these findings. The answer belongs on the artifact, not in
    whoever remembers the repo's config — the same argument the dial line already makes
    for the fix floor."""
    report, _, _ = run(monkeypatch, capsys, tmp_path, [finding("P4")])
    assert "`review_panel.file_deferral_issues` is `P2`" in report
    assert ("the board row is the whole record — no GitHub issue, so the `deferred` "
            "row carries a one-line `note` instead") in report


def test_the_report_says_each_gets_an_issue_when_the_gate_is_always(
        monkeypatch, capsys, tmp_path):
    """The non-default half of the same line, so the test above is not passing on a
    sentence that is printed whatever the dial says."""
    report, _, _ = run(monkeypatch, capsys, tmp_path, [finding("P4")],
                       config=cfg(file_deferral_issues="always"))
    assert "`review_panel.file_deferral_issues` is `always`" in report
    assert "each also gets a GitHub issue" in report


def test_a_split_below_floor_tier_is_counted_rather_than_summarised(
        monkeypatch, capsys, tmp_path):
    """The case answering from the floor would get wrong. With the fix floor at P2 the
    below-floor tier holds two bands, and a gate BETWEEN them files for some of it and
    not the rest — so the line counts the findings it is actually true of instead of
    stating one tier's answer for both."""
    report, _, _ = run(monkeypatch, capsys, tmp_path,
                       [finding("P3"), finding("P4")],
                       config=cfg(fix_severity_floor="P2",
                                  file_deferral_issues="P3"))
    assert "### Reported, not this round's work (2)" in report
    assert ("1 of them also get a GitHub issue and the rest are a board row with a "
            "one-line `note` and no issue") in report


def test_the_board_may_set_the_gate_and_may_not_set_a_word_it_does_not_know():
    """A policy knob a repo can only change by a commit reviewed by the panel that knob
    configures is the shape #305 exists to fix, so this one is board-settable like the
    floors beside it — and settable BOTH ways, since neither direction is the safe one.
    Its two ends are words, which is why it is a `deferral_gate` and not a `severity`:
    `never` is unwritable as a band and `always` would have to be spelled `P4`."""
    dial = harness_rules.BOARD_DIALS["review_panel.file_deferral_issues"]
    assert (dial.kind, dial.nullable, dial.rule) == ("deferral_gate", False, "either")
    for good in ("P1", "p3", "always", " never "):
        assert harness_rules._dial_problem("d", dial, good) == "", good
    for bad in ("P0", "sometimes", "P-2", 2, True, None):
        assert harness_rules._dial_problem("d", dial, bad), bad


@pytest.mark.parametrize("dial_name", ["review_panel.file_deferral_issues",
                                       "review_panel.fix_severity_floor"])
@pytest.mark.parametrize("written", [" P2 ", "\tp2\n"])
def test_a_board_set_severity_band_is_stripped_before_it_is_judged(dial_name, written):
    """The layer must not decide what a written value means. `panel_seats._severity`
    strips and upper-cases every severity that enters the panel, so `severity_floor`
    accepts `" p2 "` out of a rules file — and a board dial that refused the same
    value would make one written value mean two things depending on which layer
    carried it, which is precisely the layer a person typing into a settings endpoint
    cannot see. It refused: the regex ran against the raw string while the word
    endpoints beside it were being trimmed, so `" always "` was accepted and `" P2 "`
    was not."""
    dial = harness_rules.BOARD_DIALS[dial_name]
    assert harness_rules._dial_problem(dial_name, dial, written) == ""


@pytest.mark.parametrize("written,applied", [("p3", "P3"), (" Always ", "always")])
def test_a_board_set_gate_is_normalised_before_it_is_applied(written, applied,
                                                             monkeypatch):
    """Normalised where the dial is read rather than by each consumer, so the provenance
    table shows the value the round actually applied. A table reading `p3` beside a
    round that ran `P3` is one a reader has to second-guess."""
    monkeypatch.setattr(harness_rules, "_dial_body", lambda github: (
        {"dials": [{"dial": "review_panel.file_deferral_issues", "value": written}]},
        "board", ""))
    dials, _, problems, _ = harness_rules.board_dials("acme/board")
    assert problems == []
    assert dials["review_panel.file_deferral_issues"]["value"] == applied


# ---------------------------------------------------- and the briefs say the same thing

def test_the_orchestrator_brief_splits_the_row_from_the_issue():
    """The enforcement point for this dial is prose — §4b is what opens (or does not
    open) the issue — so a suite that reads no markdown is testing half a feature, the
    same reason `fixer_may_defer`'s guards are here."""
    flat = " ".join(PANEL_REVIEW_PR.read_text(encoding="utf-8").split())
    assert "review_panel.file_deferral_issues" in flat
    assert "**Every deferral gets a board row. Only some of them get a GitHub issue**" \
        in flat
    # Below the gate: no target, and a note that makes the row worth reading later.
    assert "Record the row with **no `deferred_to`**" in flat
    assert "The note is not optional here and it is the whole difference between a " \
           "record and a dumping ground." in flat
    # The read the write exists for — named now, so the row is a memory rather than a
    # dumping ground even before anything queries it across PRs.
    assert "GET /review/findings?repo=<owner/name>&pr=<n>" in flat
    # The exemption, and the fallback that stops the two records being lost together.
    assert "An escalation is exempt at every setting, `never` included." in flat
    assert "If `qb record-outcome` fails, file the issue whatever the gate says" in flat


def test_the_review_brief_no_longer_says_every_deferral_gets_an_issue():
    """Asserted partly as an ABSENCE, which is how a contradiction elsewhere in the same
    brief survives a green suite. `review-pr.md` used to instruct the orchestrator to
    open an issue on all three roads unconditionally, in the sentence a fixer's deferral
    lands on."""
    flat = " ".join(REVIEW_PR.read_text(encoding="utf-8").split())
    assert "Your job is the same on all three and it is the half the fixer is " \
           "forbidden to do: open the issue," not in flat
    assert "open an issue for it only if `review_panel.file_deferral_issues` says so" \
        in flat
    assert ("The row is the record; the issue is a work item, and they are not the "
            "same thing (#482).") in flat
    # And the deferral still has somewhere to go — the point was never that the record
    # is optional.
    assert "a one-line `note`" in flat
    assert "An escalation is exempt at every setting" in flat


# --------------------------------------------------------------- the report says which

def test_the_report_states_the_dials_on_every_round(monkeypatch, capsys, tmp_path):
    """The orchestrator builds the fixer's brief out of this report, so "which findings
    is the fixer being asked to clear" has to be readable from the artifact rather than
    from whoever remembers the repo's config. Printed at the defaults too: a reader
    weighing a quiet round needs to know whether the quiet was measured or configured."""
    report, _, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")])
    assert ("**Panel dials** (`review_panel`): fix at/above P3 · below-P2 fix budget "
            "40 lines · another round at/above P2 · reviewer scope diff · fix growth "
            "cap 3x or +30,000 chars · fixer may defer yes · failing test "
            "required no · deferrals at/above P2 get a GitHub issue, below it a "
            "board row only (an escalation always gets one)") in report


def _release_pr(monkeypatch, config):
    """A PR whose title the repo's `skip_title_patterns` refuses, with `config` in
    force. The round reviews nothing, which is the case the two tests below split."""
    stub(monkeypatch, [finding("P2")], config=config)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "release: v9", "additions": 3, "deletions": 1,
              "headRefOid": "abc"},
        diff="diff --git a/a.py b/a.py\n+x\n"))


def test_a_skipped_round_records_no_dials(monkeypatch, capsys, tmp_path):
    """Null on the paths that reviewed nothing, for the reason `code_access` is: a round
    that never dispatched a seat and never briefed a fixer did not apply a review
    policy."""
    _release_pr(monkeypatch, {**PANEL_CFG,
                              "review_panel": {"fix_severity_floor": "P1",
                                               "skip_title_patterns": ["^release:"]}})
    out = tmp_path / "skip.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False) == 0
    payload = json.loads(out.read_text())
    assert payload["review_panel"] is None
    assert not notes_about(payload, "fix_severity_floor")


def test_a_bad_value_refuses_even_a_round_that_would_have_been_skipped(monkeypatch,
                                                                      capsys, tmp_path):
    """The dials are resolved before the PR is fetched, deliberately: a rules file this
    harness cannot read is a fact about the REPO rather than about the round, so it is
    answered before anything about this particular PR is known. It used to travel as a
    `config_notes` line on the skip payload; a hard exit is louder and cannot be read
    past."""
    _release_pr(monkeypatch, {**PANEL_CFG,
                              "review_panel": {"fix_severity_floor": "nope",
                                               "skip_title_patterns": ["^release:"]}})
    with pytest.raises(SystemExit) as refusal:
        panel.run("board", 34, post=False, json_file=str(tmp_path / "skip.json"),
                  record=False)
    assert "`review_panel.fix_severity_floor`='nope'" in str(refusal.value)


# ---------------------------------------- the floors do NOT apply to the hard gate

def test_a_new_p3_sonar_gate_issue_still_buys_a_round_under_the_default_floors():
    """The regression the floors reintroduced. `panel.py` builds each Sonar issue as a
    `Canonical` carrying **Sonar's own** severity — routinely P3/P4 — and puts it in
    `outstanding` precisely so the stop rule counts it. Filtered by
    `round_trigger_floor` it dropped out of `triggering`, landed in `quiet_new`, and the
    cycle stopped `confident: True` saying "reported, not fixed here" — a failing
    quality gate reported as convergence, on a PR that cannot merge."""
    gate = sonar_canonical("P3")
    d = panel.round_stop(2, 5, [gate.key], [gate], [],
                         trigger_floor="P2", fix_floor="P3")
    assert d["stop"] is False, d["reason"]
    assert d["new_below_trigger_floor"] == []


def test_a_still_open_p3_sonar_gate_issue_keeps_the_cycle_going():
    """The other half, and the one rule 1 cannot reach: an issue an earlier round
    already raised is not new, so it has to be caught by rule 2 or rule 3. Rule 2's bar
    is a hardcoded `("P1", "P2")` tuple, which a P3 gate issue could not clear at all —
    however red the gate — so the exemption has to be there too."""
    gate = sonar_canonical("P3")
    d = panel.round_stop(2, 5, [], [gate], [], repeated={gate.key},
                         trigger_floor="P2", fix_floor="P3")
    assert d["stop"] is False, d["reason"]
    # The reason names what it counted. "1 P1/P2 still outstanding" would be a false
    # sentence about a P3 `python:S1128`, and this stop is the one a reader is most
    # likely to be reconciling against a red gate on the PR.
    assert "SonarCloud gate issue" in d["reason"]
    # Rule 3 carries the exemption too, through the shared `above` helper rather than a
    # second copy of the test — and rule 2 firing first makes that redundant TODAY,
    # deliberately: the exemption is a property of the key, so a floor added to rule 3
    # later cannot quietly re-filter the gate.
    assert "1 finding(s) an earlier round already raised" not in d["reason"]


def test_a_new_p3_JUDGED_finding_under_the_same_floors_still_stops_the_cycle():
    """The discriminator: this suite must be testing "Sonar is exempt" and not "P3 goes
    again". Same severity, same floors, same round — the only difference is
    `verdict`."""
    c = judged("P3")
    d = panel.round_stop(2, 5, [c.key], [c], [], trigger_floor="P2", fix_floor="P3")
    assert d["stop"] is True
    assert "none at or above the P2 round trigger floor" in d["reason"]
    assert d["new_below_trigger_floor"] == [c.key]


def test_a_p4_sonar_issue_is_exempt_too_because_the_gate_does_not_grade_on_severity():
    """One tier further down, because the argument is not "P3 is close enough to the
    floor" — it is that a red gate is not a severity judgement at all."""
    gate = sonar_canonical("P4")
    assert panel.round_stop(2, 5, [gate.key], [gate], [], trigger_floor="P2",
                            fix_floor="P3")["stop"] is False


def test_a_whole_run_with_only_a_p3_gate_issue_does_not_report_convergence(
        monkeypatch, capsys, tmp_path):
    """End to end through the real `run()`, because the exemption is only worth
    anything if `outstanding` actually carries the verdict this far: the judged half is
    a below-floor P4 nobody is asked to fix, so the gate issue is the only thing
    standing between this round and a clean, confident stop."""
    report, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P4")],
                             sonar=[gate_issue("P3")], max_rounds=3)
    assert payload["sonar_findings"][0]["verdict"] == "sonar"
    assert payload["round_stop"]["stop"] is False, payload["round_stop"]["reason"]
    assert payload["round_stop"]["confident"] is False
    assert "**go again**" in report


# ------------------------------------------------- the briefs do not contradict them

def test_the_fixer_has_no_licence_to_fix_whatever_else_it_notices():
    """FIX 4's contradiction, asserted as an ABSENCE. The escape hatch sat three lines
    from the instruction that establishes the floor and it bit hardest for exactly the
    P3/P4 items the floor holds back — and every prose guard in this suite up to now
    asserts a substring is PRESENT, which is how contradictory text elsewhere in the
    same brief survives a green suite."""
    flat = " ".join(PANEL_REVIEW_PR.read_text(encoding="utf-8").split())
    assert "obvious defects it trips over" not in flat
    assert "must fix those too" not in flat
    # The genuine half is not weakened: a defect at or above the floor and inside the
    # change is still fixed, and a P1 the panel missed is still a P1.
    assert "subject to the same floor and the same scope as a panel finding" in flat
    assert "a P1 the panel missed and the fixer walks straight into is still a P1" in flat
    assert "Below the floor, or outside the change under review, it is **reported in " \
           "the summary and not fixed**" in flat


def test_the_fixer_is_asked_for_the_id_it_was_given_and_never_for_a_key():
    """FIX 5, also as an absence. The rendered report shows local finding IDs
    (`[236-F01]`) and never the 16-character digest — deliberately, since a literal key
    on a PR comment reads as an API key to every secret scanner — so a fixer physically
    cannot supply one, and a template demanding it produces either a fabricated key or
    a blank row."""
    flat = " ".join(REVIEW_PR.read_text(encoding="utf-8").split())
    assert "Key: <the finding's key, verbatim" not in flat
    assert "a deferral nobody can key is a deferral nothing tracks" not in flat
    assert "a premise nobody can key stays in the loop" not in flat
    assert "ID: <the panel's finding ID for it, verbatim" in flat
    assert "The fixer reports finding IDs; you supply the keys." in flat
    # And the orchestrator's own brief states the mapping rather than implying it.
    panel_md = " ".join(PANEL_REVIEW_PR.read_text(encoding="utf-8").split())
    assert "Map the fixer's finding IDs to keys first" in panel_md
    assert '"\\(.id)\\t\\(.key)' in panel_md


# ------------------------------------------------- the sandbox holds what this file reads
#
# #246: this file reads the briefs at `REPO_ROOT / "harness/commands/…"` while living
# three directories below that root, and `nix build .#checks.<system>.loops-tests` runs
# it in a sandbox containing only what that check copies in. The check used to copy the
# suite in flat, so `parents[3]` resolved to `/` and every brief read errored as a
# FileNotFoundError — not a failure, an ERROR line, in a build no workflow runs. That is
# #163's mechanism exactly, and it is the reason the enumeration is asserted here rather
# than left to whoever adds the next read.

#: A `cp`/`install` of a repo-root path into the sandbox, anchored on the command so that
#: a `${./x}` in a comment or passed as an argument is not mistaken for a copy.
_FLAKE_COPY = re.compile(r'^\s*(?:cp|install)(?:\s+-\S+)*\s+\$\{\s*\./([^}\s]+?)\s*\}',
                         re.MULTILINE)


def _loops_check_region(flake_text: str) -> str:
    """The `loops-tests` block of `flake.nix`, and only it.

    Anchored on the attribute at line start, because `flake.nix` names this check in the
    prose beside it: a first-occurrence search for the bare name would slice from a comment
    and compare this suite against whatever block happened to follow — looking, from the
    outside, exactly as healthy as a correct one."""
    starts = [m.start() for m in re.finditer(r"^\s*loops-tests = ", flake_text, re.MULTILINE)]
    assert len(starts) == 1, (
        f"expected exactly one line defining loops-tests in flake.nix, found {len(starts)}")
    end = flake_text.find("\n        '';", starts[0])
    assert end != -1, "the loops-tests block is not terminated by a closing ''; at its level"
    return flake_text[starts[0]:end]


def _repo_root_reads() -> set[str]:
    """The repo-root paths this file joins onto `REPO_ROOT`, out of its own syntax tree.

    Parsed rather than grepped: a pattern over the raw source cannot tell an expression
    from a sentence, and this file's prose mentions the brief paths repeatedly.

    Refuses what it cannot resolve rather than passing over it. A reader that silently
    skipped `REPO_ROOT.joinpath(...)`, a variable segment or a second name bound to
    `REPO_ROOT` would leave the guard below with nothing to report and the sandbox
    erroring on a file nobody copied in — the failure this guard exists to prevent,
    wearing the guard's own clothes. The refusal costs whoever writes a dynamic join one
    message on their line; the alternative costs the next person an afternoon."""
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents = {id(c): n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    found, unreadable = set(), []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and node.id == "REPO_ROOT"
                and isinstance(node.ctx, ast.Load)):
            continue
        segments, cur, parent = [], node, parents.get(id(node))
        while isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div) \
                and parent.left is cur:
            if not (isinstance(parent.right, ast.Constant)
                    and isinstance(parent.right.value, str)):
                segments = None
                break
            segments.append(parent.right.value)
            cur, parent = parent, parents.get(id(parent))
        if segments:
            found.add("/".join(segments))
        else:
            unreadable.append(node.lineno)
    assert not unreadable, (
        "REPO_ROOT is used at line(s) " + ", ".join(str(n) for n in unreadable) + " in a "
        "shape this reader cannot follow, so the file read there would not reach the list "
        "checked against flake.nix. Write it as `REPO_ROOT / \"literal\"`, or teach the "
        "reader the new shape and add the file to the loops-tests check")
    return found


#: The one repo-root read the sandbox is NOT required to supply: the guard below reads
#: `flake.nix` to compare against, behind an `is_file()` that skips when it is absent. It is
#: named here rather than special-cased inside the comparison so that it cannot quietly grow
#: — anything else added to this set is a read somebody has decided to stop guarding, which
#: is a decision that should be visible in a diff.
_READ_BUT_NOT_COPIED = {"flake.nix"}


def test_the_reader_finds_the_briefs_it_is_meant_to_find():
    """The guard below is only worth having if its reader works, and a reader that silently
    found NOTHING would make it pass against any flake at all."""
    assert _repo_root_reads() == {"harness/commands/review-pr.md",
                                 "harness/commands/panel-review-pr.md",
                                 "flake.nix"}


def test_the_loops_check_supplies_every_repo_root_file_this_file_reads():
    """The enumeration in `flake.nix` is the thing that goes stale, so nothing relies on
    somebody remembering it. Add a third brief read here and this fails in the ordinary
    `pytest harness/loops/tests` before a push, rather than erroring in a nix build that
    no workflow runs.

    Skipped rather than failed when `flake.nix` is absent: this file is itself collected
    from a sandbox that does not hold the flake, and a check that cannot see the
    expression cannot judge it."""
    flake = REPO_ROOT / "flake.nix"
    if not flake.is_file():
        pytest.skip("no flake.nix beside this checkout, so there is no check to compare against")
    copied = set(_FLAKE_COPY.findall(_loops_check_region(flake.read_text(encoding="utf-8"))))
    # A copy of `harness/commands` supplies every file beneath it.
    missing = sorted(p for p in _repo_root_reads() - _READ_BUT_NOT_COPIED
                     if p not in copied
                     and not any(p.startswith(c + "/") for c in copied))
    assert not missing, (
        "this file reads repo-root paths that flake.nix's loops-tests check does not copy "
        "into its sandbox, so they will error there as FileNotFoundError rather than be "
        "asserted: " + ", ".join(missing) + ". Add a `cp ${./<path>}` for each")


def test_the_region_reader_stops_at_the_end_of_its_own_check():
    """`'';` is two characters a shell script may legitimately contain, and a slice that ran
    past the block's end would credit this check with a neighbour's copies — reporting a file
    as supplied that this sandbox never receives."""
    text = ("        loops-tests = pkgs.runCommand \"a\" { } ''\n"
            "          cp -r ${./harness/loops} repo/harness/loops\n"
            "        '';\n"
            "        worktree-tests = pkgs.runCommand \"b\" { } ''\n"
            "          cp -r ${./harness/bin} harness/bin\n"
            "        '';\n")
    assert set(_FLAKE_COPY.findall(_loops_check_region(text))) == {"harness/loops"}


def test_the_region_reader_refuses_an_ambiguous_or_absent_check():
    """A renamed check, which is the whole reason the name is written out here. `nix flake
    check` on a flake whose check has been renamed says nothing about this suite, so a
    region reader that quietly returned "" would report every read as uncopied — or, worse,
    every read as satisfied."""
    with pytest.raises(AssertionError, match="found 0"):
        _loops_check_region("        mcp-tests = pkgs.runCommand \"a\" { } ''\n        '';\n")
    with pytest.raises(AssertionError, match="found 2"):
        _loops_check_region("        loops-tests = pkgs.runCommand \"a\" { } ''\n"
                            "        '';\n"
                            "        loops-tests = pkgs.runCommand \"b\" { } ''\n"
                            "        '';\n")


def test_only_a_copy_counts_as_supplying_a_file():
    """`${./x}` is Nix putting a path in the store, which is not the same as the sandbox
    having that file where this suite reads it. Running a script or naming one in a comment
    both interpolate a path, and neither is a copy."""
    block = ("          # cp ${./harness/README.md} repo/harness/README.md\n"
             "          bash ${./scripts/release.py} preview\n"
             "          cp -r ${./harness/commands} repo/harness/commands\n"
             "          install -Dm644 ${./.harness-rules.sample} repo/.harness-rules.sample\n")
    assert set(_FLAKE_COPY.findall(block)) == {"harness/commands", ".harness-rules.sample"}


# ------------------------------------- which LAYER supplied the dials — #305


#: A cfg as `resolve_repo` now returns one: the dials, and beside each of them the
#: layer that answered. `_dials` is the whole of #305's reporting half, and the
#: panel's job is to carry it onto the round rather than to compute it.
def _layered(**board):
    dials = {
        "review_panel.fix_severity_floor": {
            "value": "P3", "layer": "sample",
            "source": "origin/main:.harness-rules.sample"},
        "review_panel.max_rounds": {"value": 2, "layer": "defaults",
                                    "source": "harness_rules.DEFAULTS"},
        "reviewers.claude.enabled": {"value": True, "layer": "sample",
                                     "source": "origin/main:.harness-rules.sample"},
        # Deliberately outside the two review blocks: `resolve_repo` reports every
        # dial in the config, and a round's artifact wants the ones that governed
        # the round.
        "loops.issue_executor": {"value": False, "layer": "defaults",
                                 "source": "harness_rules.DEFAULTS"},
    }
    dials.update(board)
    return {**PANEL_CFG, "_rules_from": "origin/main:.harness-rules.sample",
            "_dials": dials, "_dials_from": "https://qb.example/dials",
            "_dials_unreadable": False,
            "review_panel": {"fix_severity_floor": "P3"}}


def test_the_round_records_which_layer_supplied_each_dial(monkeypatch, capsys, tmp_path):
    """`review_panel` said WHAT ran and never WHERE IT CAME FROM, and that is the
    half the #299 incident turned on: the sample stated both floors at P2 while five
    rounds put P4s in `to_fix`, and no artifact could settle which described the run.
    """
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                        config=_layered())
    said = payload["rules"]["dials"]
    assert said["review_panel.fix_severity_floor"]["layer"] == "sample"
    assert said["review_panel.max_rounds"]["layer"] == "defaults"
    # Scoped to what governs a REVIEW. The loop schedule is a dial and did not.
    assert "loops.issue_executor" not in said
    assert payload["rules"]["baseline"] == ".harness-rules.sample"


def test_a_round_that_ran_under_a_board_dial_says_so_in_the_public_notes(
        monkeypatch, capsys, tmp_path):
    """#52's "never silent" applied to the one layer that can move a floor without a
    pull request. `config_notes` is where it goes because `--post` puts that list in
    a public PR comment, so the reader of the review sees the floor it was run
    against, who moved it and why."""
    cfg = _layered(**{"review_panel.fix_severity_floor": {
        "value": "P3", "layer": "board", "source": "https://qb.example/dials",
        "scope": "repo", "reason": "trying P3 for a fortnight", "set_by": "rich",
        "expires_at": "2999-01-01T00:00:00+00:00"}})
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], config=cfg)
    said = [n for n in payload["config_notes"] if "from the BOARD" in n]
    assert len(said) == 1
    assert "review_panel.fix_severity_floor" in said[0]
    assert "trying P3 for a fortnight" in said[0] and "rich" in said[0]
    assert "2999-01-01" in said[0]
    assert payload["rules"]["dials"][
        "review_panel.fix_severity_floor"]["layer"] == "board"


def test_a_round_whose_board_would_not_answer_says_that_too(monkeypatch, capsys,
                                                            tmp_path):
    """A configured board that did not answer is NOT the same fact as no dial being
    set, and a round that quietly ran on the repo's own rules while a dial was in
    force on the board is the disagreement this whole feature exists to end."""
    cfg = {**_layered(), "_dials_unreadable": True}
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], config=cfg)
    assert any("would not answer" in n for n in payload["config_notes"])
    assert payload["rules"]["dials_unreadable"] is True


def test_a_repo_with_no_board_dial_adds_no_note_at_all(monkeypatch, capsys, tmp_path):
    """The quiet case has to stay quiet: a round on a repo nobody has set a dial for
    reads exactly as it did before this landed."""
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                        config=_layered())
    assert not [n for n in payload["config_notes"] if "BOARD" in n]
