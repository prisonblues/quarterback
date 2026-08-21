"""#165's seven `review_panel` dials, and #297's eighth — thoroughness against convergence, per repo.

The panel had one behaviour and no dials. Every choice it made about what counts as
worth reporting, what a fix round has to clear, and what buys another round was a
constant, and the measurement says those constants do not converge: across seven PRs
panelled on one night, the last round of each raised 201 findings no earlier round
had and **128 of them — 63.7% — were created by the fix pass immediately before it**,
against a ~7% industry baseline for bad-fix injection. Every one of those panels
terminated on the round cap, each saying in its own output "a stop, not convergence".

So there are now eight settings — seven from #165 and `low_severity_fix_lines` from
#297, which answers a second measurement taken five days later on the same panel — and
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

#: The eight, and where each one's default is written twice. `skip_title_patterns` and
#: the rest of the block are not dials and are not listed.
DIALS = {
    "fixer_may_defer": "DEFAULT_FIXER_MAY_DEFER",
    "fix_severity_floor": "DEFAULT_FIX_SEVERITY_FLOOR",
    "round_trigger_floor": "DEFAULT_ROUND_TRIGGER_FLOOR",
    "low_severity_fix_lines": "DEFAULT_LOW_SEVERITY_FIX_LINES",
    "max_fix_growth": "DEFAULT_MAX_FIX_GROWTH",
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


def stub(monkeypatch, findings, *, config=None, diff=None, prompts=None, sonar=()):
    """Every process a run would spawn, replaced. `prompts` collects what each seat
    was actually handed, which is the only way to test `reviewer_scope` — its whole
    enforcement is the text of the brief. `sonar` seats SonarCloud with a red gate and
    those issues as its hard ones, which is the only way to reach the floors' one
    exemption through the real `run()`."""
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
              "headRefName": "h", "headRefOid": "abc"},
        diff=diff or "diff --git a/a.py b/a.py\n+x\n"))

    def review(cmd_name, model, prompt, *a, **k):
        if prompts is not None:
            prompts.append(prompt)
        return panel.ReviewerRun(list(findings), None, 10, [])

    monkeypatch.setattr(panel, "review_llm", review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", _adjudicate)


def run(monkeypatch, capsys, tmp_path, findings, *, round_no=1, baseline=(),
        max_rounds=2, config=None, diff=None, prompts=None, scope="auto",
        name="r", sonar=()):
    """One whole panel run: the report it prints and the payload it writes."""
    stub(monkeypatch, findings, config=config, diff=diff, prompts=prompts, sonar=sonar)
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
    ("fix_severity_floor", "p-4", "P1, P2, P3, P4"),
    ("round_trigger_floor", "blocker", "P1, P2, P3, P4"),
    ("low_severity_fix_lines", "a few", "a whole number"),
    ("max_fix_growth", "lots", "is not a number"),
    ("reviewer_scope", "everything", "diff, repo"),
    ("require_failing_test", "sometimes", "true or false"),
    ("max_rounds", 2.5, "whole number of rounds >= 1"),
]


@pytest.mark.parametrize("key,bad,accepted", BAD_VALUES)
def test_a_malformed_value_of_a_known_key_is_a_hard_exit(key, bad, accepted):
    """All eight, in one table, because the failure they share is the one that matters:
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
    answer as the default and not a mistake either."""
    dials = panel_seats.resolve_dials({key: unset}, None, [])
    expected = (None if key in ("max_fix_growth", "low_severity_fix_lines")
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
    """`deferred_to` names an issue ref, so the row wants one — a `deferred` with
    nowhere to go is the markdown list this replaced. The orchestrator opens it, never
    the fixer, which is the same division step 3a already draws."""
    orchestrator = " ".join(REVIEW_PR.read_text(encoding="utf-8").split())
    assert "deferred_to" in orchestrator
    assert "the orchestrator files it — you open nothing" in orchestrator
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


def test_null_switches_the_growth_check_off(monkeypatch, capsys, tmp_path):
    """The non-default value. `null` is the only spelling of "off" — the default IS a
    number, so reading null as "inherit" like every other setting would leave a check
    whose only job is to stop a cycle with no way to opt out."""
    _, _, r1 = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=1,
                   max_rounds=3, diff=SMALL, config=cfg(max_fix_growth=None))
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")], round_no=2,
                        baseline=[r1], max_rounds=3, diff=HUGE, scope="pr",
                        config=cfg(max_fix_growth=None))
    assert payload["review_panel"]["max_fix_growth"] is None
    assert payload["round_stop"]["fix_growth"] is None
    assert payload["round_stop"]["stop"] is False


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


# --------------------------------------------------------------- the report says which

def test_the_report_states_the_dials_on_every_round(monkeypatch, capsys, tmp_path):
    """The orchestrator builds the fixer's brief out of this report, so "which findings
    is the fixer being asked to clear" has to be readable from the artifact rather than
    from whoever remembers the repo's config. Printed at the defaults too: a reader
    weighing a quiet round needs to know whether the quiet was measured or configured."""
    report, _, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")])
    assert ("**Panel dials** (`review_panel`): fix at/above P3 · below-P2 fix budget "
            "40 lines · another round at/above P2 · reviewer scope diff · fix growth "
            "cap 3x · fixer may defer yes · failing test required no") in report


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
             "          bash ${./scripts/release_stamp.py} check\n"
             "          cp -r ${./harness/commands} repo/harness/commands\n"
             "          install -Dm644 ${./.harness-rules.sample} repo/.harness-rules.sample\n")
    assert set(_FLAKE_COPY.findall(block)) == {"harness/commands", ".harness-rules.sample"}
