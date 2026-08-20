"""#165's seven `review_panel` dials — thoroughness against convergence, per repo.

The panel had one behaviour and no dials. Every choice it made about what counts as
worth reporting, what a fix round has to clear, and what buys another round was a
constant, and the measurement says those constants do not converge: across seven PRs
panelled on one night, the last round of each raised 201 findings no earlier round
had and **128 of them — 63.7% — were created by the fix pass immediately before it**,
against a ~7% industry baseline for bad-fix injection. Every one of those panels
terminated on the round cap, each saying in its own output "a stop, not convergence".

So there are now seven settings, and this suite is what says they are settings rather
than documentation. Each one gets three tests, because there are exactly three ways a
setting fails:

* **its default** — the value a repo that configured nothing gets, which has to be
  the value the rules file documents. A drift between `harness_rules.DEFAULTS` and
  the `panel_core` constant the resolver falls back to is invisible from either side.
* **a non-default value changing behaviour** — the failure #169 is named for, a
  mechanism that ships unwired. A key nothing reads is worse than no key: it reads as
  configured.
* **a bad value being rejected, loudly** — a repo that typed a setting wrong and got
  default behaviour with nothing said is the failure `warn_unknown_keys` exists to
  prevent, one level down. Every rejection here lands in `config_notes`, which prints
  above the findings and travels in the payload and onto the PR.

The prose guards are here too rather than in `harness/tests`, because the briefs and
the code are one feature: `fixer_may_defer` is enforced ONLY in prose (there is no
code path that can stop a fixer patching something), so a test suite for it that
reads no markdown is testing nothing at all.
"""

import json
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

#: The seven, and where each one's default is written twice. `skip_title_patterns` and
#: the rest of the block are not dials and are not listed.
DIALS = {
    "fixer_may_defer": "DEFAULT_FIXER_MAY_DEFER",
    "fix_severity_floor": "DEFAULT_FIX_SEVERITY_FLOOR",
    "round_trigger_floor": "DEFAULT_ROUND_TRIGGER_FLOOR",
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


def stub(monkeypatch, findings, *, config=None, diff=None, prompts=None):
    """Every process a run would spawn, replaced. `prompts` collects what each seat
    was actually handed, which is the only way to test `reviewer_scope` — its whole
    enforcement is the text of the brief."""
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: config or PANEL_CFG)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "fix: a real bug", "additions": 3, "deletions": 1,
              "headRefOid": "abc"},
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
        name="r"):
    """One whole panel run: the report it prints and the payload it writes."""
    stub(monkeypatch, findings, config=config, diff=diff, prompts=prompts)
    out = tmp_path / f"{name}{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline),
                     max_rounds=max_rounds, scope=scope) == 0
    return capsys.readouterr().out, json.loads(out.read_text()), str(out)


def finding(severity, title="unvalidated input", file="a.py"):
    return panel.Finding("claude", severity, file, 3, title, "")


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
    """Not as WRITTEN: a repo whose value was rejected has to be able to see which
    policy actually ran, beside the note saying why its own was not used."""
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                        config=cfg(fix_severity_floor="nonsense"))
    assert set(payload["review_panel"]) == set(DIALS)
    assert payload["review_panel"]["fix_severity_floor"] == "P2"


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
def test_a_bad_fixer_may_defer_is_reported_and_not_read_as_truthy(bad):
    """`bool("maybe")` is True, and so is `bool([1])`: Python truthiness would turn
    every junk value into the permissive half of a policy switch. The note names the
    accepted spellings, because a reader has to be able to tell a rejected value from
    an honoured one."""
    notes: list[str] = []
    got = panel_seats.panel_flag({"fixer_may_defer": bad}, "fixer_may_defer",
                                 True, notes)
    assert got is True and len(notes) == 1
    assert "is not true or false" in notes[0] and "yes`/`no" in notes[0]


# ---------------------------------------------------------------- 2. fix_severity_floor

def test_by_default_a_p3_is_reported_and_is_not_the_fix_rounds_work(monkeypatch, capsys,
                                                                    tmp_path):
    """The measured cut: applied to the seven PRs it discards 99 of 147 findings (67.3%)
    and loses ZERO P1s. Reported, recorded, marked — and out of the list a fix brief is
    built from, which is the half that matters, since the fix pass is where the damage
    comes from."""
    report, payload, _ = run(monkeypatch, capsys, tmp_path,
                             [finding("P3", "docstring could mention __vNEXT__")])
    assert "### To fix (0)" in report
    assert "### Reported, not this round's work (1) — below the `P2` fix floor" in report
    assert "🔽" in report
    # Still visible. A cut that HID the finding would be a worse artifact than the one
    # it replaced: the point is that the fixer is not briefed with it, not that nobody
    # is told.
    assert "docstring could mention __vNEXT__" in report
    assert "Do not build a fix brief from this list" in report
    assert payload["to_fix"][0]["below_fix_floor"] is True
    assert payload["review_panel"]["fix_severity_floor"] == "P2"


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
                        config=cfg(fix_severity_floor=" p3 "))
    assert payload["review_panel"]["fix_severity_floor"] == "P3"
    assert not notes_about(payload, "fix_severity_floor")


@pytest.mark.parametrize("bad", ["P0", "blocker", "p2 or better", 2, ["P2"]])
def test_a_bad_fix_floor_is_reported_with_the_set_that_is_accepted(monkeypatch, capsys,
                                                                   tmp_path, bad):
    """Silently falling back is the failure mode: a repo that meant "only fix blockers"
    and typed `blocker` would have every P4 fixed and nothing anywhere saying why.
    Unset is a different case and is deliberately silent — see the test below."""
    report, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P3")],
                             config=cfg(fix_severity_floor=bad))
    note = notes_about(payload, "fix_severity_floor")
    assert len(note) == 1 and "P1, P2, P3, P4" in note[0]
    assert "is not a severity" in note[0]
    # Loud: on the report, above the findings, and on the PR comment under `--post`.
    assert f"⚠️ config: {note[0]}" in report
    assert payload["review_panel"]["fix_severity_floor"] == "P2"


def test_an_unset_floor_is_silent(monkeypatch, capsys, tmp_path):
    """Absent, null and "" are "use the default" everywhere in this harness, and none of
    them is a mistake — the same reading `diff_budget` gives an absent budget."""
    for unset in ({}, {"fix_severity_floor": None}, {"fix_severity_floor": ""}):
        _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                            config=cfg(**unset))
        assert not notes_about(payload, "fix_severity_floor")
        assert payload["review_panel"]["fix_severity_floor"] == "P2"


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
    non-convergence this whole change exists to remove."""
    _, _, r1 = run(monkeypatch, capsys, tmp_path, [finding("P3")], round_no=1,
                   max_rounds=3)
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P3")], round_no=2,
                        baseline=[r1], max_rounds=3, scope="pr")
    stop = payload["round_stop"]
    assert stop["stop"] is True, stop["reason"]
    assert "already raised" not in stop["reason"]
    assert stop["fix_floor"] == "P2" and stop["trigger_floor"] == "P2"


def test_a_bad_trigger_floor_is_reported(monkeypatch, capsys, tmp_path):
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                        config=cfg(round_trigger_floor="P5"))
    note = notes_about(payload, "round_trigger_floor")
    assert len(note) == 1 and "P1, P2, P3, P4" in note[0]
    assert payload["review_panel"]["round_trigger_floor"] == "P2"


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
def test_a_bad_max_fix_growth_is_reported_and_never_read_as_a_threshold(bad, why):
    """`false` is the other way an operator writes "off" and is rejected rather than
    reinterpreted: `isinstance(True, int)` is True, so read as a number it would become
    the threshold 1.0 and stop every cycle whose fix commit is bigger than its first
    round — the switch flipped to "off" turning the feature all the way on."""
    notes: list[str] = []
    assert panel_seats.fix_growth_limit({"max_fix_growth": bad}, notes) == 3.0
    assert len(notes) == 1 and why in notes[0]
    assert "null to switch the check off" in notes[0]


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


def test_a_bad_reviewer_scope_is_reported(monkeypatch, capsys, tmp_path):
    """`resolve_round_scope`'s own lesson, one setting over: a config value nothing
    checks is read as the fallback, silently, and the round then reports a scope it did
    not use."""
    prompts: list[str] = []
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                        config=cfg(reviewer_scope="whole-repo"), prompts=prompts)
    note = notes_about(payload, "reviewer_scope")
    assert len(note) == 1 and "diff, repo" in note[0]
    assert payload["review_panel"]["reviewer_scope"] == "diff"
    assert "search the codebase" not in prompts[0]


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


def test_a_bad_require_failing_test_is_reported(monkeypatch, capsys, tmp_path):
    _, payload, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")],
                        config=cfg(require_failing_test="sometimes"))
    note = notes_about(payload, "require_failing_test")
    assert len(note) == 1 and "is not true or false" in note[0]
    assert payload["review_panel"]["require_failing_test"] is False


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
def test_a_bad_max_rounds_is_reported(bad):
    """A bool is rejected before the integer read: `max_rounds: true` is 1 to Python,
    which would cap every cycle at one round on a value that says nothing about rounds.
    A non-integer is refused rather than rounded — a cap silently lowered runs a round
    the file did not ask for."""
    notes: list[str] = []
    assert panel_seats.resolve_max_rounds(None, {"max_rounds": bad}, notes) == 2
    assert len(notes) == 1 and "whole number of rounds >= 1" in notes[0]


def test_a_whole_float_is_accepted_as_a_cap():
    """`2.0` out of a generator is the same cap as `2`; `2.5` is not a cap at all."""
    assert panel_seats.resolve_max_rounds(None, {"max_rounds": 3.0}, []) == 3


# --------------------------------------------------------------- the report says which

def test_the_report_states_the_dials_on_every_round(monkeypatch, capsys, tmp_path):
    """The orchestrator builds the fixer's brief out of this report, so "which findings
    is the fixer being asked to clear" has to be readable from the artifact rather than
    from whoever remembers the repo's config. Printed at the defaults too: a reader
    weighing a quiet round needs to know whether the quiet was measured or configured."""
    report, _, _ = run(monkeypatch, capsys, tmp_path, [finding("P2")])
    assert ("**Panel dials** (`review_panel`): fix at/above P2 · another round at/above "
            "P2 · reviewer scope diff · fix growth cap 3x · fixer may defer yes · "
            "failing test required no") in report


def test_a_skipped_round_records_no_dials_but_still_reports_a_bad_value(monkeypatch,
                                                                       capsys, tmp_path):
    """Null on the paths that reviewed nothing, for the reason `code_access` is: a round
    that never dispatched a seat and never briefed a fixer did not apply a review
    policy. The VALIDATION still travels, because a broken rules file is a fact about
    the repo rather than about the round."""
    config = {**cfg(fix_severity_floor="nope"),
              "review_panel": {"fix_severity_floor": "nope",
                               "skip_title_patterns": ["^release:"]}}
    stub(monkeypatch, [finding("P2")], config=config)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "release: v9", "additions": 3, "deletions": 1,
              "headRefOid": "abc"},
        diff="diff --git a/a.py b/a.py\n+x\n"))
    out = tmp_path / "skip.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False) == 0
    payload = json.loads(out.read_text())
    assert payload["review_panel"] is None
    assert notes_about(payload, "fix_severity_floor")
