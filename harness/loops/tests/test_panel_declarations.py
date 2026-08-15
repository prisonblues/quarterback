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


def test_a_bare_array_declares_nothing_which_is_not_declaring_clean():
    """Four CLIs get the new contract at four different speeds, and a model that
    ignores the envelope has still done the review — but it was never heard on
    coverage, and None is how the board stores that. `[]` would say it was asked
    and had nothing to declare, which is the collapse this release exists to
    remove."""
    findings, gaps = panel.parse_reply("claude", '[{"severity":"P1","title":"boom"}]')
    assert [f.severity for f in findings] == ["P1"]
    assert gaps is None


def test_an_envelope_that_omits_the_key_declares_nothing_either():
    _, gaps = panel.parse_reply("codex", json.dumps({"findings": []}))
    assert gaps is None
    # ...but present-and-empty IS an answer: asked, and no gap to report.
    _, gaps = panel.parse_reply("codex", json.dumps({"findings": [], "could_not_assess": []}))
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


def test_an_earlier_brace_in_the_prose_does_not_cost_the_declarations():
    """The nastier half of the same bug: the first `{...}` span is prose and fails
    to parse, so a scan that picks by bracket position falls through to the next
    span by offset — which is the envelope's own INNER findings array. It parses
    cleanly, the findings survive, and the declarations vanish with no symptom."""
    raw = ('Note: severities are {P1..P4}, dashes are literal.\n'
           '{"findings": [{"title": "x", "file": "a.py"}], "could_not_assess": ["the schema"]}')
    findings, gaps = panel.parse_reply("codex", raw)
    assert [f.title for f in findings] == ["x"]
    assert gaps == ["the schema"]


def test_a_bare_array_behind_a_prose_object_is_still_found():
    """The mirror case: an old-shape reviewer whose reply opens with something
    brace-shaped must not be read as an envelope with no findings."""
    findings, gaps = panel.parse_reply("claude", 'Legend: {} means nothing.\n'
                                                 '[{"severity": "P2", "title": "boom"}]')
    assert [f.title for f in findings] == ["boom"] and gaps is None


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
    assert panel.parse_reply("codex", "[]") == ([], None)


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


# ---- what the merge treats as one defect -----------------------------------

def test_two_files_with_one_basename_are_two_defects():
    """The dedup bucket is the basename, because reviewers spell paths
    differently — but the representative's full path becomes the round's defect
    key, so merging these would hand one file's defect the other's identity."""
    a = panel.Finding("claude", "P2", "app/api/reviews.py", 10, "unused import")
    b = panel.Finding("codex", "P2", "harness/loops/reviews.py", 12, "unused import")
    got = panel.group_findings([a, b])
    assert sorted(f.file for f, _ in got) == ["app/api/reviews.py", "harness/loops/reviews.py"]


def test_a_short_path_and_the_full_one_are_the_same_defect():
    """One reviewer quotes `reviews.py`, another `app/api/reviews.py`. That is one
    defect, and it keeps the more specific spelling so the key does not depend on
    who wrote the shortest path."""
    a = panel.Finding("claude", "P2", "reviews.py", 10, "unused import")
    b = panel.Finding("codex", "P1", "app/api/reviews.py", 12, "unused import")
    (rep, revs), = panel.group_findings([a, b])
    assert rep.file == "app/api/reviews.py" and revs == ["claude", "codex"]


def test_the_representative_does_not_depend_on_who_reported_it():
    """Its title becomes the round's defect key. Picking by severity alone let the
    wording flip between rounds as members dropped out or re-rated, which reads on
    the PR as a fix that broke something."""
    same = [panel.Finding("codex", "P2", "a.py", 10, "zebra wording"),
            panel.Finding("claude", "P2", "a.py", 12, "alpha wording")]
    (rep, _), = panel.group_findings(same)
    (rep2, _), = panel.group_findings(list(reversed(same)))
    assert rep.title == rep2.title == "alpha wording"


# ---- defect identity -------------------------------------------------------

def test_the_key_ignores_the_line_and_normalises_the_title():
    """A line number moves when the fix above it lands. An identity that moves
    links nothing, so the same defect described the same way in two rounds is one
    key, whatever line each reviewer put it on."""
    assert (panel.finding_key("app/x.py", "Unicode dash survives the strip!")
            == panel.finding_key("app/x.py", "unicode  dash survives the strip"))
    assert panel.finding_key("app/x.py", "a") != panel.finding_key("app/y.py", "a")


# ---- baselines -------------------------------------------------------------

def _payload(tmp_path, name, round_no, titles, dismissed=(), **over):
    p = tmp_path / name
    p.write_text(json.dumps({
        "round": round_no,
        "repo": "acme", "github": "acme/board", "pr": 34,
        "to_fix": [{"file": "a.py", "title": t} for t in titles],
        "dismissed": [{"file": "a.py", "title": t} for t in dismissed],
        **over,
    }))
    return str(p)


THIS_RUN = {"repo": "acme", "github": "acme/board", "pr": 34, "round": 2}


def test_a_baseline_counts_what_was_raised_not_what_was_confirmed(tmp_path):
    """A finding the judge dismissed in round 1 and a reviewer raises again in
    round 2 is not new information. Diffing against confirmed findings only is how
    a loop re-discovers its own rejects forever and never converges."""
    path = _payload(tmp_path, "r1.json", 1, ["real bug"], dismissed=["false alarm"])
    b = panel.load_baseline([path], THIS_RUN)
    assert b.problems == [] and b.rounds == {1}
    assert panel.finding_key("a.py", "false alarm") in b.keys
    assert panel.finding_key("a.py", "real bug") in b.keys


def test_an_unreadable_baseline_is_reported_not_swallowed(tmp_path):
    """Its absence makes every finding look new — which reads as "the fix broke
    things", the exact opposite of the truth. Silence here is worse than an error."""
    b = panel.load_baseline([str(tmp_path / "nope.json")], THIS_RUN)
    assert b.keys == set() and b.problems and "unreadable" in b.problems[0]


def test_a_malformed_round_costs_the_verdict_its_confidence_not_the_review(tmp_path):
    """Every other defect in a baseline is downgraded to a problems entry. An
    unguarded int() made this one raise out of run() — after the diff was fetched
    and every reviewer CLI had been paid for."""
    path = _payload(tmp_path, "r1.json", "two", ["real bug"])
    b = panel.load_baseline([path], THIS_RUN)
    assert any("malformed round" in p for p in b.problems)
    # ...and its findings are still counted, which is the point of not raising.
    assert panel.finding_key("a.py", "real bug") in b.keys


def test_another_prs_baseline_is_reported_and_not_counted(tmp_path):
    """A stale or cross-wired payload is not a thinner baseline, it is a wrong
    one: its keys make this PR's real findings read as repeated and can stop the
    loop a round early."""
    path = _payload(tmp_path, "other.json", 1, ["someone else's bug"], pr=99)
    b = panel.load_baseline([path], THIS_RUN)
    assert b.keys == set() and b.rounds == set()
    assert any("another review's" in p and "pr=99" in p for p in b.problems)


def test_a_baseline_that_is_not_earlier_is_reported(tmp_path):
    path = _payload(tmp_path, "r2.json", 2, ["x"])
    b = panel.load_baseline([path], THIS_RUN)
    assert any("not earlier than" in p for p in b.problems)


def test_earlier_rounds_are_counted_not_guessed_from_the_highest_label(tmp_path):
    """Baselines for rounds 1 and 3 are two earlier rounds. Reporting max(round)
    as the count invents a round nobody ran, and it prints on the PR comment."""
    b = panel.load_baseline(
        [_payload(tmp_path, "r1.json", 1, ["a"]), _payload(tmp_path, "r3.json", 3, ["b"])],
        {**THIS_RUN, "round": 4})
    assert len(b.rounds) == 2


def test_a_reworded_finding_is_the_same_defect_as_far_as_the_round_diff_goes(tmp_path):
    """The key is file + normalised title, and the title is whichever group member
    won the merge — so any rewording between rounds would otherwise land a
    persistent defect in `new_findings` and report the fix as having broken
    something."""
    path = _payload(tmp_path, "r1.json", 1, ["unused import in the header"])
    b = panel.load_baseline([path], THIS_RUN)
    assert b.raised_before("a.py", "Unused import in the header!")
    assert b.raised_before("a.py", "unused imports in the header")
    # Not a licence to swallow a different defect in the same file.
    assert not b.raised_before("a.py", "session is never closed")
    # ...nor the same words about another file.
    assert not b.raised_before("b.py", "unused import in the header")


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


# ---- the master rules on coverage, findings or not -------------------------

def _judge_returning(monkeypatch, reply):
    seen = {}

    def fake_run_cli(args, label, attempts=2):
        seen["prompt"] = args[2]
        return reply, None

    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(panel, "run_cli", fake_run_cli)
    return seen


def test_a_round_with_no_findings_still_gets_its_coverage_adjudicated(monkeypatch):
    """The round where "clean versus could-not-tell" matters MOST is the one with
    no findings: two members that could not read the migration and a third
    reporting clean is a split, and a finding count of zero says nothing about
    it. Returning early on an empty group list skipped exactly that."""
    seen = _judge_returning(monkeypatch, json.dumps(
        {"verdicts": [], "coverage_note": "the migration is unread; claude's clean is partial"}))
    verdicts, skip, note = panel.judge(
        [], "diff", "opus", coverage={"codex": ["the migration"], "claude": []})
    assert verdicts == {} and skip is None
    assert note.startswith("the migration is unread")
    assert "could not assess the migration" in seen["prompt"]


def test_a_coverage_only_reply_is_not_a_judge_that_failed_to_rule(monkeypatch):
    """With nothing to adjudicate, an envelope carrying only the note is a
    complete answer — reporting it as unparseable would veto the round."""
    _judge_returning(monkeypatch, json.dumps({"coverage_note": "nothing unread"}))
    _, skip, note = panel.judge([], "diff", "", coverage={"codex": ["the schema"]})
    assert skip is None and note == "nothing unread"


def test_nothing_found_and_nothing_declared_needs_no_judge(monkeypatch):
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the judge must not run with nothing to rule on")))
    assert panel.judge([], "diff", "", coverage={"claude": []}) == ({}, None, "")


# ---- what the PR comment promises ------------------------------------------

PANEL_CFG = {"github": "acme/board", "path": "/tmp/acme-board",
             "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
             "review_panel": {}}


def _report(monkeypatch, capsys, tmp_path, round_no, baseline=(), max_rounds=None):
    """One whole panel run with every process it would spawn replaced, so what is
    under test is the report it writes on the PR."""
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: PANEL_CFG)
    monkeypatch.setattr(panel, "sh", lambda args, **kw: (
        json.dumps({"title": "feat: x", "additions": 3, "deletions": 1,
                    "baseRefName": "main", "headRefName": "feat/x", "headRefOid": "abc"})
        if args[:3] == ["gh", "pr", "view"] else "diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "review_llm", lambda *a, **k: panel.ReviewerRun(
        [panel.Finding("claude", "P3", "a.py", 3, "unused import")], None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "judge", lambda *a, **k: (
        {0: {"id": 0, "real": True, "severity": "P3", "reason": "real"}}, None, ""))
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline),
                     max_rounds=max_rounds) == 0
    return capsys.readouterr().out, str(out)


def test_a_review_only_run_does_not_promise_a_round_nobody_will_run(monkeypatch, capsys,
                                                                    tmp_path):
    """`--max-rounds` is the CALLER's cap and only /panel-review-pr drives the
    loop. A plain `/panel` read used to comment "round 1 of at most 2 — go
    again", telling the reader a re-review was coming when nothing would run it —
    with a "no earlier round raised" count that is vacuously every finding."""
    report, _ = _report(monkeypatch, capsys, tmp_path, 1)
    assert "**Rounds:**" not in report and "go again" not in report
    assert "· round 1" not in report
    assert "unused import" in report


def test_a_first_round_inside_a_cycle_still_reports_where_the_loop_stands(monkeypatch,
                                                                          capsys, tmp_path):
    """Naming the cap is what says this run belongs to a cycle — /panel-review-pr
    passes it on round 1, and that round's `go again` is a promise something will
    keep."""
    report, _ = _report(monkeypatch, capsys, tmp_path, 1, max_rounds=2)
    assert "**Rounds:** round 1 of at most 2 — **go again**" in report


def test_a_re_review_says_which_round_it_is_and_where_the_loop_stands(monkeypatch, capsys,
                                                                      tmp_path):
    _, r1 = _report(monkeypatch, capsys, tmp_path, 1, max_rounds=2)
    report, _ = _report(monkeypatch, capsys, tmp_path, 2, baseline=[r1], max_rounds=2)
    assert "**Rounds:** round 2 of at most 2" in report
    # The same finding again is not fresh damage, and the ↻/🆕 marker stays off.
    assert "🆕" not in report
    assert "1 finding(s) an earlier round already raised" in report


# ---- the CLI's own arguments -----------------------------------------------

def test_a_round_past_the_cap_is_rejected_rather_than_recorded(monkeypatch):
    """`--round 5 --max-rounds 2` records an impossible position and hits the cap
    branch on the spot, writing "round cap (2) reached" into a round 5."""
    monkeypatch.setattr(sys, "argv",
                        ["panel.py", "--pr", "1", "--round", "5", "--max-rounds", "2"])
    try:
        panel.main()
    except SystemExit as e:
        assert "past --max-rounds" in str(e)
    else:
        raise AssertionError("--round past --max-rounds should not be accepted")
