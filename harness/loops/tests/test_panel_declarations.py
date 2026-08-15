"""What a reviewer declares about its own pass, and how rounds are decided.

A panel used to answer one question — what did you find — and a run that found
nothing was indistinguishable from a run that could not look: a reviewer handed
half the diff, one whose CLI never started, and one with genuinely nothing to say
all reported the same zero. And the fix that followed was reviewed by nobody,
because the panel read the diff as it was BEFORE the fix and then stopped.

So reviewers now report two things they can actually observe (what they could not
assess, and which fixes need re-reading), the panel measures the one they cannot
(truncation), and the loop itself turns on a mechanical count of findings no
earlier round raised. These tests pin all three, and in particular that the
declarations never drive the loop — they only stop a broken round being read as a
converged one.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402


# ---- the reply envelope ----------------------------------------------------

def test_the_envelope_carries_findings_and_declarations():
    raw = json.dumps({
        "findings": [{"severity": "P2", "file": "a.py", "line": 4, "title": "leak",
                      "detail": "closes nothing"}],
        "could_not_assess": ["the migration, which is not in the diff"],
    })
    findings, gaps = panel.parse_reply("codex", raw)
    assert [f.title for f in findings] == ["leak"]
    assert gaps == ["the migration, which is not in the diff"]


def test_a_bare_array_still_parses_and_declares_nothing():
    """Four CLIs get the new contract at four different speeds, and a model that
    ignores the envelope has still done the review. Declaring nothing is the
    honest record of a reviewer that was asked and answered in the old shape."""
    findings, gaps = panel.parse_reply("claude", '[{"severity":"P1","title":"boom"}]')
    assert [f.severity for f in findings] == ["P1"]
    assert gaps == []


def test_prose_around_the_envelope_does_not_hide_the_declarations():
    """The failure this guards: preferring `[` unconditionally finds the envelope's
    INNER findings array, parses fine, and silently drops everything alongside it —
    a bug with no symptom except declarations that are never there."""
    raw = ('Here is my review:\n```json\n'
           '{"findings": [{"title": "x", "file": "a.py"}], "could_not_assess": ["runtime"]}\n'
           '```\nHope that helps.')
    findings, gaps = panel.parse_reply("pi", raw)
    assert len(findings) == 1 and gaps == ["runtime"]


def test_a_string_where_a_list_was_asked_for_is_taken_as_one_item():
    _, gaps = panel.parse_reply("pi", json.dumps(
        {"findings": [], "could_not_assess": "the schema"}))
    assert gaps == ["the schema"]


def test_unparseable_is_still_none_so_the_caller_can_retry():
    """Distinct from ([], []) — "found nothing" and "said nothing usable" are not
    the same event, and collapsing them is how a reviewer's work gets dropped."""
    assert panel.parse_reply("codex", "sorry, I can't do that") is None
    assert panel.parse_reply("codex", "") is None
    # An object that isn't the envelope has no findings to take.
    assert panel.parse_reply("codex", '{"verdict": "looks fine"}') is None
    assert panel.parse_reply("codex", "[]") == ([], [])


# ---- the re-review declaration ---------------------------------------------

def test_fix_needs_rereview_flags_by_index():
    """Indexes into the array just returned, so a reviewer needs no id scheme —
    and cannot flag a finding it did not report."""
    raw = json.dumps({"findings": [{"title": "one"}, {"title": "two"}],
                      "fix_needs_rereview": [1, 7, True]})
    findings, _ = panel.parse_reply("codex", raw)
    assert [f.needs_rereview for f in findings] == [False, True]


def test_an_index_counts_the_findings_the_model_sent_not_the_ones_we_kept():
    """A junk entry among the findings is dropped, and every index after it would
    otherwise point one finding too far — flagging the neighbour of the one the
    reviewer meant."""
    raw = json.dumps({"findings": [None, {"title": "one"}, {"title": "two"}],
                      "fix_needs_rereview": [2]})
    findings, _ = panel.parse_reply("codex", raw)
    assert [f.title for f in findings] == ["one", "two"]
    assert [f.needs_rereview for f in findings] == [False, True]


def test_a_per_finding_flag_means_the_same_thing():
    findings, _ = panel.parse_reply("codex", json.dumps(
        {"findings": [{"title": "one", "needs_rereview": True}]}))
    assert findings[0].needs_rereview is True


def test_the_flag_survives_the_merge_from_any_reporter():
    """One reviewer seeing that the fix will be structural is the observation;
    the others not saying so is not a contradiction of it."""
    a = panel.Finding("claude", "P2", "a.py", 10, "same bug", needs_rereview=False)
    b = panel.Finding("codex", "P2", "a.py", 12, "same bug", needs_rereview=True)
    (rep, revs), = panel.group_findings([a, b])
    assert revs == ["claude", "codex"] and rep.needs_rereview is True
    # ...but attribution is not flattened with it: honesty is per reviewer.
    assert rep.rereview_by == ["codex"]


# ---- defect identity -------------------------------------------------------

def test_the_key_ignores_the_line_and_normalises_the_title():
    """A line number moves when the fix above it lands. An identity that moves
    links nothing, so the same defect described the same way in two rounds is one
    key, whatever line each reviewer put it on."""
    assert (panel.finding_key("app/x.py", "Unicode dash survives the strip!")
            == panel.finding_key("app/x.py", "unicode  dash survives the strip"))
    assert panel.finding_key("app/x.py", "a") != panel.finding_key("app/y.py", "a")


# ---- baselines -------------------------------------------------------------

def _payload(tmp_path, name, round_no, titles, dismissed=()):
    p = tmp_path / name
    p.write_text(json.dumps({
        "round": round_no,
        "to_fix": [{"file": "a.py", "title": t} for t in titles],
        "dismissed": [{"file": "a.py", "title": t} for t in dismissed],
    }))
    return str(p)


def test_a_baseline_counts_what_was_raised_not_what_was_confirmed(tmp_path):
    """A finding the judge dismissed in round 1 and a reviewer raises again in
    round 2 is not new information. Diffing against confirmed findings only is how
    a loop re-discovers its own rejects forever and never converges."""
    path = _payload(tmp_path, "r1.json", 1, ["real bug"], dismissed=["false alarm"])
    keys, rounds, problems = panel.load_baseline([path])
    assert problems == [] and rounds == 1
    assert panel.finding_key("a.py", "false alarm") in keys
    assert panel.finding_key("a.py", "real bug") in keys


def test_an_unreadable_baseline_is_reported_not_swallowed(tmp_path):
    """Its absence makes every finding look new — which reads as "the fix broke
    things", the exact opposite of the truth. Silence here is worse than an error."""
    keys, _, problems = panel.load_baseline([str(tmp_path / "nope.json")])
    assert keys == set() and problems and "unreadable" in problems[0]


# ---- the stopping rule -----------------------------------------------------

def _finding(sev):
    return panel.Finding("claude", sev, "a.py", 1, "t")


def test_new_findings_earn_another_round():
    d = panel.round_stop(1, 3, ["k1"], [_finding("P3")], [])
    assert d["stop"] is False and "1 finding" in d["reason"]


def test_a_blocker_still_confirmed_earns_another_round_even_with_nothing_new():
    """A P2 raised again after the fix is a P2 that was not fixed. Severity does
    this regardless of what any reviewer declared."""
    d = panel.round_stop(2, 3, [], [_finding("P2")], [])
    assert d["stop"] is False and "P1/P2" in d["reason"]


def test_a_dry_round_of_polish_is_finished():
    d = panel.round_stop(2, 3, [], [_finding("P4")], [])
    assert d["stop"] is True and d["confident"] is True and "dry" in d["reason"]


def test_the_cap_stops_the_loop_but_is_not_recorded_as_convergence():
    """"We ran out of rounds" and "there was nothing left" are different facts,
    and only one of them is a clean bill of health."""
    d = panel.round_stop(2, 2, ["k1", "k2"], [_finding("P1")], [])
    assert d["stop"] is True and d["confident"] is False
    assert "round cap (2)" in d["reason"] and "unreviewed" in d["reason"]


def test_a_declaration_vetoes_the_verdict_but_never_extends_the_loop():
    """A truncated reviewer is truncated again next round at the same budget, so
    treating that as a reason to go again is a loop with no exit. It is a reason
    to stop CALLING the PR clean."""
    d = panel.round_stop(2, 3, [], [], ["codex saw 60,000 of 118,402 diff chars"])
    assert d["stop"] is True and d["confident"] is False and d["veto"]


def test_a_finding_still_there_after_the_fix_costs_the_stop_its_confidence():
    """The loop does not go again for a repeated P3 — two reviewers can disagree
    about one of those forever — but "the fixer was told and it is still there"
    is not the same event as "nothing was found", and only one of them is a
    clean bill of health."""
    d = panel.round_stop(2, 2, [], [_finding("P3")], [], repeated=1)
    assert d["stop"] is True and d["confident"] is False
    assert any("did not land" in v for v in d["veto"])


def test_a_baseline_that_could_not_be_read_also_costs_the_verdict_its_confidence():
    d = panel.round_stop(2, 3, [], [], [], baseline_ok=False)
    assert d["stop"] is True and d["confident"] is False


# ---- what makes a quiet round suspect --------------------------------------

def test_the_veto_names_every_way_a_round_can_look_quiet_without_being_quiet():
    meta = {
        "claude": {"ran": True, "truncated": True, "max_diff_chars": 60_000},
        "codex": {"ran": False, "skip": "codex (gpt): CLI absent"},
        "pi": {"ran": True, "could_not_assess": ["the amendment path"]},
        "antigravity": {"ran": True, "unstructured": True},
    }
    why = panel.coverage_veto(meta, judge_skip="judge: claude CLI absent",
                              flagged=2, diff_chars=118_402)
    joined = " | ".join(why)
    assert "claude saw 60,000 of 118,402" in joined
    assert "codex did not run" in joined
    assert "pi could not assess: the amendment path" in joined
    assert "antigravity returned no structured reply" in joined
    assert "not adjudicated" in joined
    assert "2 finding(s) whose reporter said the FIX needs re-reading" in joined


def test_a_panel_with_nothing_to_declare_vetoes_nothing():
    meta = {"claude": {"ran": True, "truncated": False, "could_not_assess": []}}
    assert panel.coverage_veto(meta, None, 0, 1_000) == []
