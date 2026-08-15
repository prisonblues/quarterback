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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402


def _reports(title, file="a.py", reviewer="codex", line=1, flagged=False):
    """One reviewer's report of a defect — what a Canonical carries in
    `reported_by`, and what its key is derived from."""
    return [panel.Finding(reviewer, "P2", file, line, title, "",
                          needs_rereview=flagged)]


def _canonical(file, title, severity="P2", reviewer="codex"):
    """A judged finding as the panel serialises it, for the round diff to read."""
    reports = _reports(title, file=file, reviewer=reviewer)
    return panel.Canonical(id="34-F01", severity=severity, file=file, line=1,
                           synthesis=title, verdict="confirmed",
                           reported_by=reports)


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


def test_a_schema_echoed_before_the_answer_is_not_the_answer():
    """Quoting the requested shape and then answering is ordinary model behaviour,
    and the echo is a well-formed envelope carrying an empty `findings`. Taking the
    FIRST one replaced a real review with a clean "found nothing" — silently, since
    a quoted example parses perfectly. Whatever the model wrote last is its answer."""
    raw = ('I will reply as {"findings": [], "could_not_assess": []}.\n\n'
           'Here it is:\n'
           '{"findings": [{"severity": "P1", "file": "a.py", "title": "boom"}], '
           '"could_not_assess": ["the migration"]}')
    findings, gaps = panel.parse_reply("codex", raw)
    assert [f.title for f in findings] == ["boom"]
    assert gaps == ["the migration"]


def test_a_sentence_about_the_schema_is_not_an_envelope():
    """`{"findings": "an array of objects"}` carries the key and no answer. The
    real reply — a bare array here — must win over it."""
    raw = ('{"findings": "an array of objects, severity P1..P4"}\n'
           '[{"severity": "P2", "file": "a.py", "title": "the real one"}]')
    findings, _ = panel.parse_reply("codex", raw)
    assert [f.title for f in findings] == ["the real one"]


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


def test_a_string_no_is_not_a_yes():
    """This parser deliberately tolerates imperfect LLM JSON, and `bool("false")`
    is True — so a model spelling the answer out made a declaration it explicitly
    declined to make. That flag both vetoes a stop and feeds the per-member honesty
    count, where a manufactured yes cannot be spotted later."""
    findings, _ = panel.parse_reply("codex", json.dumps({"findings": [
        {"title": "a", "needs_rereview": "false"},
        {"title": "b", "needs_rereview": "no"},
        {"title": "c", "needs_rereview": "0"},
        {"title": "d", "needs_rereview": None},
        {"title": "e", "needs_rereview": {"why": "structural"}},
        {"title": "f", "needs_rereview": "true"},
        {"title": "g", "needs_rereview": "Yes"},
        {"title": "h", "needs_rereview": 1},
        {"title": "i", "needs_rereview": True},
    ]}))
    assert [f.title for f in findings if f.needs_rereview] == ["f", "g", "h", "i"]


def test_the_flag_survives_the_merge_from_any_reporter():
    """One reviewer seeing that the fix will be structural is the observation;
    the others not saying so is not a contradiction of it."""
    a = panel.Finding("claude", "P2", "a.py", 10, "same bug", needs_rereview=False)
    b = panel.Finding("codex", "P2", "a.py", 12, "same bug", needs_rereview=True)
    [c] = panel._parse_verdicts([{"id": "F1", "members": [0, 1], "real": True}], [a, b], 34)
    assert c.reviewers == ["claude", "codex"] and c.needs_rereview is True
    # ...but attribution is not flattened with it: honesty is per reviewer. It is
    # READ off each reporter's own report now rather than reconstructed onto a
    # representative, so there is no longer an order in which the merge could
    # credit the wrong member.
    assert c.rereview_by == ["codex"]
    flags = {r["reviewer"]: r["needs_rereview"] for r in c.as_dict()["reported_by"]}
    assert flags == {"claude": False, "codex": True}


# ---- what the merge treats as one defect -----------------------------------

def test_two_files_with_one_basename_are_two_defects():
    """Reviewers spell paths differently, so a merge is tempted by the basename —
    but the file is half of the defect key, so merging these would hand one file's
    defect the other's identity."""
    a = panel.Finding("claude", "P2", "app/api/reviews.py", 10, "unused import")
    b = panel.Finding("codex", "P2", "harness/loops/reviews.py", 12, "unused import")
    assert panel.cluster_findings([a, b]) == [[a], [b]]
    assert panel._defect_key(a.file, [a]) != panel._defect_key(b.file, [b])


def test_a_short_path_and_the_full_one_are_the_same_defect():
    """One reviewer quotes `reviews.py`, another `app/api/reviews.py`. That is one
    defect — the judge is what says so now, and the record it writes keeps both
    accounts. What survives the ruling is the spelling the judge chose, so the
    round diff is where the two must still be recognised as one file."""
    a = panel.Finding("claude", "P2", "reviews.py", 10, "unused import")
    b = panel.Finding("codex", "P1", "app/api/reviews.py", 12, "unused import")
    [c] = panel._parse_verdicts(
        [{"id": "F1", "members": [0, 1], "file": "app/api/reviews.py"}], [a, b], 34)
    assert c.file == "app/api/reviews.py" and c.reviewers == ["claude", "codex"]
    was = panel.Baseline(keys={c.key}, titles={"unused import": {c.file}})
    assert was.raised_before(_canonical("reviews.py", "unused import"))


def test_the_representative_does_not_depend_on_who_reported_it():
    """The identity is the first of the reporters' titles alphabetically. Taking
    whichever member happened to win a merge let the wording flip between rounds
    as members dropped out or re-rated, which reads on the PR as a fix that broke
    something."""
    same = [panel.Finding("codex", "P2", "a.py", 10, "zebra wording"),
            panel.Finding("claude", "P2", "a.py", 12, "alpha wording")]
    round1 = panel.Canonical(id="34-F01", severity="P2", file="a.py", line=10,
                             synthesis="the judge's words", verdict="confirmed",
                             reported_by=same)
    round2 = panel.Canonical(id="34-F01", severity="P2", file="a.py", line=12,
                             synthesis="the judge's other words", verdict="confirmed",
                             reported_by=list(reversed(same)))
    assert round1.key == round2.key
    assert panel.Baseline(keys={round1.key}).raised_before(round2)


# ---- defect identity -------------------------------------------------------

def test_the_key_ignores_the_line_and_normalises_the_title():
    """A line number moves when the fix above it lands. An identity that moves
    links nothing, so the same defect described the same way in two rounds is one
    key, whatever line each reviewer put it on."""
    said = "Unicode dash survives the strip!"
    again = "unicode  dash survives the strip"
    assert (panel._defect_key("app/x.py", _reports(said, file="app/x.py", line=4))
            == panel._defect_key("app/x.py", _reports(again, file="app/x.py", line=91)))
    assert (panel._defect_key("app/x.py", _reports("a", file="app/x.py"))
            != panel._defect_key("app/y.py", _reports("a", file="app/y.py")))


# ---- baselines -------------------------------------------------------------

def _serialised(file, title):
    """A finding as an earlier round's --json-file holds it: the key it sent, and
    the reporters' own titles the key was made from."""
    return {"file": file, "synthesis": title,
            "key": panel._defect_key(file, _reports(title, file=file)),
            "reported_by": [{"reviewer": "codex", "title": title}]}


def _payload(tmp_path, name, round_no, titles, dismissed=(), **over):
    p = tmp_path / name
    p.write_text(json.dumps({
        "round": round_no,
        "repo": "acme", "github": "acme/board", "pr": 34,
        "to_fix": [_serialised("a.py", t) for t in titles],
        "dismissed": [_serialised("a.py", t) for t in dismissed],
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
    assert b.raised_before(_canonical("a.py", "false alarm"))
    assert b.raised_before(_canonical("a.py", "real bug"))


def test_a_baseline_reads_the_key_the_payload_carries(tmp_path):
    """The panel sends a key with every finding, and it is the same identity the
    board chains runs on. Re-deriving one here — from the judge's freshly-worded
    synthesis, the only title an older payload had — is how the local round diff
    and the board's chains came to disagree about which findings were new."""
    p = tmp_path / "r1.json"
    p.write_text(json.dumps({
        "round": 1, "repo": "acme", "github": "acme/board", "pr": 34,
        "to_fix": [{"file": "a.py", "key": "deadbeefdeadbeef",
                    "synthesis": "the judge reworded this one completely",
                    "reported_by": [{"reviewer": "codex", "title": "leaky handle"}]}],
    }))
    b = panel.load_baseline([str(p)], THIS_RUN)
    assert "deadbeefdeadbeef" in b.keys
    assert b.raised_before(_canonical("a.py", "leaky handle"))


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
    assert b.raised_before(_canonical("a.py", "real bug"))


def test_another_prs_baseline_is_reported_and_not_counted(tmp_path):
    """A stale or cross-wired payload is not a thinner baseline, it is a wrong
    one: its keys make this PR's real findings read as repeated and can stop the
    loop a round early."""
    path = _payload(tmp_path, "other.json", 1, ["someone else's bug"], pr=99)
    b = panel.load_baseline([path], THIS_RUN)
    assert b.keys == set() and b.rounds == set()
    assert any("another review's" in p and "pr=99" in p for p in b.problems)


def test_a_baseline_that_is_not_earlier_is_reported_and_not_counted(tmp_path):
    """Reported AND excluded, like the cross-PR case. A current or future payload
    that still loaded made genuinely new findings read as repeated, which ends the
    loop a round early on findings nobody has fixed."""
    path = _payload(tmp_path, "r2.json", 2, ["x"])
    b = panel.load_baseline([path], THIS_RUN)
    assert any("not earlier than" in p for p in b.problems)
    assert b.keys == set() and b.rounds == set()
    assert not b.raised_before(_canonical("a.py", "x"))


def test_a_baseline_that_does_not_say_whose_it_is_is_not_believed(tmp_path):
    """The identity check only rejected fields that were PRESENT and unequal, so a
    hand-edited or truncated payload omitting them suppressed this run's findings
    on the word of a file that never said which review it came from."""
    path = tmp_path / "anon.json"
    path.write_text(json.dumps({"round": 1, "to_fix": [_serialised("a.py", "x")]}))
    b = panel.load_baseline([str(path)], THIS_RUN)
    assert b.keys == set() and b.rounds == set()
    assert any("does not say which review" in p for p in b.problems)


def test_a_baseline_written_from_another_checkout_is_the_same_review(tmp_path):
    """`repo` is the local directory's name, not the review's. /panel-review-pr's
    parallel mode gives every PR a throwaway worktree, so round 1 writing
    "quarterback-feat-issue-24" and round 2 asking as "quarterback" is the normal
    case — and rejecting the baseline for it would blame the review for where it
    was run. `github` + `pr` are what name a review."""
    path = tmp_path / "elsewhere.json"
    path.write_text(json.dumps({
        "round": 1, "repo": "board-worktree-r1", "github": "acme/board", "pr": 34,
        "to_fix": [_serialised("a.py", "real bug")],
    }))
    b = panel.load_baseline([str(path)], THIS_RUN)
    assert b.problems == [] and b.rounds == {1}
    assert b.raised_before(_canonical("a.py", "real bug"))
    # ...but a baseline from a different PR is still a wrong baseline, not a
    # thinner one.
    other = tmp_path / "other-pr.json"
    other.write_text(json.dumps({
        "round": 1, "repo": "acme", "github": "acme/board", "pr": 99,
        "to_fix": [_serialised("a.py", "real bug")],
    }))
    assert panel.load_baseline([str(other)], THIS_RUN).keys == set()


def test_a_round_two_with_no_baseline_at_all_is_a_problem(tmp_path):
    """Without one, every finding reads as new, the report prints "N of N raised by
    no earlier round (0 known from 0 earlier rounds)", and the round would be free
    to record a CONFIDENT verdict on a comparison it never made."""
    b = panel.load_baseline([], THIS_RUN)
    assert any("no --baseline" in p for p in b.problems)
    # ...and a first round is not missing anything.
    assert panel.load_baseline([], {**THIS_RUN, "round": 1}).problems == []


def test_a_path_spelled_short_is_the_same_defect_as_the_full_one(tmp_path):
    """Reviewers spell paths differently — `reviews.py` and `app/api/reviews.py`
    are one file. A round where only the short-path reviewer raised the defect
    hashes to a different key AND used to fail the exact-file title check, so a
    persistent defect counted as new and bought a fix cycle nobody needed."""
    p = tmp_path / "r1.json"
    p.write_text(json.dumps({
        "round": 1, "repo": "acme", "github": "acme/board", "pr": 34,
        "to_fix": [_serialised("app/api/reviews.py", "stop is parsed and discarded")],
    }))
    b = panel.load_baseline([str(p)], THIS_RUN)
    assert b.raised_before(_canonical("reviews.py", "stop is parsed and discarded"))
    # Not a licence to merge two distinct files that merely end in the same name.
    assert not b.raised_before(
        _canonical("harness/loops/reviews.py", "stop is parsed and discarded"))


def test_a_cycle_is_inherited_from_the_earliest_baseline(tmp_path):
    """Every round of one cycle carries the same id, so the board can tell "the
    re-review of THIS declaration" from "whatever ran next on this PR". Taken from
    the earliest ROUND, not from whichever --baseline was listed first — a round 1
    that predates the field carries only its run_key, which is the same thing."""
    r1 = _payload(tmp_path, "r1.json", 1, ["a"], run_key="cyc-1")
    r2 = _payload(tmp_path, "r2.json", 2, ["b"], cycle="cyc-1", run_key="run-2")
    b = panel.load_baseline([r2, r1], {**THIS_RUN, "round": 3})
    assert b.cycle == "cyc-1" and b.rounds == {1, 2} and b.problems == []
    # No usable baseline, no inherited cycle — the run mints its own.
    assert panel.load_baseline([], {**THIS_RUN, "round": 1}).cycle is None


def test_a_baseline_from_another_cycle_is_reported_and_not_counted(tmp_path):
    """Two agents looping one PR at once produce two cycles, and their keys are
    unrelated. Merged into one history, a finding only the OTHER cycle raised reads
    as a repeat here — which can suppress the fix round it needed. The stored cycle
    id exists precisely to make that checkable, and this was the one place the
    stored fact went unchecked."""
    mine = _payload(tmp_path, "r1.json", 1, ["a"], cycle="cyc-A")
    theirs = _payload(tmp_path, "other.json", 2, ["b"], cycle="cyc-B")
    b = panel.load_baseline([mine, theirs], {**THIS_RUN, "round": 3})
    assert b.cycle == "cyc-A" and b.rounds == {1}
    assert any("cycle cyc-B" in p for p in b.problems)
    assert b.raised_before(_canonical("a.py", "a"))
    assert not b.raised_before(_canonical("a.py", "b"))


def test_two_baselines_for_one_round_say_so_rather_than_picking_by_hex(tmp_path):
    """`min((round, cycle))` fell through to comparing opaque hex ids when two
    payloads shared a round, so the winner was lexicographic — neither the earliest
    nor the first listed, and contradicting the rule written beside it. The tie is
    now taken in order, and the ambiguity is reported rather than resolved in
    silence."""
    first = _payload(tmp_path, "a.json", 1, ["a"], cycle="zzz")
    second = _payload(tmp_path, "b.json", 1, ["b"], cycle="zzz")
    b = panel.load_baseline([first, second], THIS_RUN)
    assert b.cycle == "zzz"
    assert any("two payloads for one round" in p for p in b.problems)


def test_a_baseline_is_not_rejected_for_an_identity_this_run_cannot_supply(tmp_path):
    """The check tested key PRESENCE, so `{"repo": None}` — what the documented
    `panel.py --pr N` invocation produced before --repo resolved a default —
    rejected EVERY baseline for "not saying which review it is from". Round 2 then
    loaded nothing, called every finding new and could never record a confident
    stop: the round diff silently no-opped for the workflow it documents."""
    path = _payload(tmp_path, "r1.json", 1, ["real bug"])
    b = panel.load_baseline([path], {**THIS_RUN, "repo": None})
    assert b.problems == [] and b.rounds == {1}
    assert b.raised_before(_canonical("a.py", "real bug"))
    # ...and a field the caller DOES know is still required to be present.
    anon = tmp_path / "anon.json"
    anon.write_text(json.dumps({"round": 1, "to_fix": [_serialised("a.py", "x")]}))
    assert any("does not say which review" in p
               for p in panel.load_baseline([str(anon)], THIS_RUN).problems)


def test_earlier_rounds_are_counted_not_guessed_from_the_highest_label(tmp_path):
    """Baselines for rounds 1 and 3 are two earlier rounds. Reporting max(round)
    as the count invents a round nobody ran, and it prints on the PR comment."""
    b = panel.load_baseline(
        [_payload(tmp_path, "r1.json", 1, ["a"]), _payload(tmp_path, "r3.json", 3, ["b"])],
        {**THIS_RUN, "round": 4})
    assert len(b.rounds) == 2


def test_a_reworded_finding_is_the_same_defect_as_far_as_the_round_diff_goes(tmp_path):
    """The key is file + normalised title, and the title is the reporters' own — so
    a reviewer that re-words its report between rounds would otherwise land a
    persistent defect in `new_findings` and report the fix as having broken
    something."""
    path = _payload(tmp_path, "r1.json", 1, ["unused import in the header"])
    b = panel.load_baseline([path], THIS_RUN)
    assert b.raised_before(_canonical("a.py", "Unused import in the header!"))
    assert b.raised_before(_canonical("a.py", "unused imports in the header"))
    # Not a licence to swallow a different defect in the same file.
    assert not b.raised_before(_canonical("a.py", "session is never closed"))
    # ...nor the same words about another file.
    assert not b.raised_before(_canonical("b.py", "unused import in the header"))


def test_a_plural_ending_in_es_still_matches_its_own_singular(tmp_path):
    """The commonest noun class in a review title ends in a plain `s` after an
    `e` — files, lines, nodes, values, handles. An `es` rule ahead of the `s` one
    took two characters off every one of them ("files" -> "fil") while the
    singular stemmed to itself, so singular and plural never matched and the
    reword fallback never fired for the words it exists for: a persistent defect
    reworded from "handle" to "handles" between rounds read as one no earlier
    round raised, which prints as the fix having broken something."""
    path = _payload(tmp_path, "r1.json", 1, ["the stale node handle"])
    b = panel.load_baseline([path], THIS_RUN)
    assert b.raised_before(_canonical("a.py", "the stale node handles"))
    assert panel._stem("files") == panel._stem("file")
    assert panel._stem("values") == panel._stem("value")
    # ...and two different words are still two different words.
    assert not b.raised_before(_canonical("a.py", "the stale node cache"))


def test_one_material_word_apart_is_two_defects_however_alike_the_titles_read(tmp_path):
    """Findings share long boilerplate and differ in one noun. A character ratio
    puts these at 0.93, and calling the second a repeat drops it from
    `new_findings` and out of the fixer's brief entirely — a lost defect, which is
    strictly worse than the wasted round a false "new" costs."""
    path = _payload(tmp_path, "r1.json", 1, ["the N+1 query in the user loop"])
    b = panel.load_baseline([path], THIS_RUN)
    assert b.raised_before(_canonical("a.py", "the N+1 query in the user loop"))
    assert not b.raised_before(_canonical("a.py", "the N+1 query in the order loop"))
    # ...and a genuinely reworded title is still absorbed, which is the whole
    # reason the fallback exists.
    assert b.raised_before(_canonical("a.py", "The N+1 queries in the user loop!"))


# ---- the stopping rule -----------------------------------------------------

def _confirmed(sev):
    return _canonical("a.py", "t", severity=sev)


def test_new_findings_earn_another_round():
    d = panel.round_stop(1, 3, ["k1"], [_confirmed("P3")], [])
    assert d["stop"] is False and "1 finding" in d["reason"]


def test_a_blocker_still_confirmed_earns_another_round_even_with_nothing_new():
    """A P2 raised again after the fix is a P2 that was not fixed. Severity does
    this regardless of what any reviewer declared."""
    d = panel.round_stop(2, 3, [], [_confirmed("P2")], [])
    assert d["stop"] is False and "P1/P2" in d["reason"]


def test_a_dry_round_of_polish_is_finished():
    d = panel.round_stop(2, 3, [], [_confirmed("P4")], [])
    assert d["stop"] is True and d["confident"] is True and "dry" in d["reason"]


def test_the_cap_stops_the_loop_but_is_not_recorded_as_convergence():
    """"We ran out of rounds" and "there was nothing left" are different facts,
    and only one of them is a clean bill of health."""
    d = panel.round_stop(2, 2, ["k1", "k2"], [_confirmed("P1")], [])
    assert d["stop"] is True and d["confident"] is False
    assert "round cap (2)" in d["reason"] and "unreviewed" in d["reason"]


def test_a_declaration_vetoes_the_verdict_but_never_extends_the_loop():
    """A truncated reviewer is truncated again next round at the same budget, so
    treating that as a reason to go again is a loop with no exit. It is a reason
    to stop CALLING the PR clean."""
    d = panel.round_stop(2, 3, [], [], ["codex saw 60,000 of 118,402 diff chars"])
    assert d["stop"] is True and d["confident"] is False and d["veto"]


def test_a_finding_still_there_after_the_fix_earns_another_round():
    """A judge-confirmed P3 the last round already raised is a finding the fixer was
    told about and did not fix. Recording a veto and stopping anyway ended the cycle
    with a confirmed defect present — /panel-review-pr's bar is every confirmed
    finding, not every P1/P2."""
    d = panel.round_stop(2, 3, [], [_confirmed("P3")], [], repeated=1)
    assert d["stop"] is False and "still outstanding" in d["reason"]
    # No veto, though: the veto list answers "why this round's QUIET is not
    # evidence of a quiet PR", and this round was not quiet — its repeat is
    # already the stated reason for going again.
    assert d["veto"] == []


def test_the_cap_is_what_ends_an_argument_about_a_repeated_p4():
    """Two reviewers can disagree about a P4 forever, so rule 3 needs a floor. The
    cap is it — and a cap reached with work outstanding is not convergence."""
    d = panel.round_stop(2, 2, [], [_confirmed("P4")], [], repeated=1)
    assert d["stop"] is True and d["confident"] is False
    assert "round cap (2)" in d["reason"] and "unreviewed" in d["reason"]


def test_a_baseline_that_could_not_be_read_also_costs_the_verdict_its_confidence():
    d = panel.round_stop(2, 3, [], [], [], baseline_ok=False)
    assert d["stop"] is True and d["confident"] is False


# ---- what makes a quiet round suspect --------------------------------------

def test_the_veto_names_every_way_a_round_can_look_quiet_without_being_quiet():
    meta = {
        "claude": {"ran": True, "truncated": True, "max_diff_chars": 60_000},
        "codex": {"ran": False, "skip": "codex (gpt): exited 1 (429 rate limited)"},
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


def test_a_seat_this_box_does_not_carry_is_reported_without_vetoing():
    """A reviewer whose CLI is not installed is a fact about the HOST, not about
    the round: it is absent every round, so a veto on it makes `confident`
    permanently unreachable on the headless machines — which is exactly where
    the unattended loops run and where the signal has to mean something. A repo
    that lists a workstation-only vendor (this one lists two) would otherwise
    buy every unattended run a standing veto and teach the reader to discount
    all of them. The skip is still reported; it is just not evidence."""
    absent = {"antigravity": {"ran": False, "absent": True,
                              "skip": "antigravity (m): CLI absent"},
              "pi": {"ran": False, "absent": True, "skip": "pi (kimi): CLI absent"},
              "claude": {"ran": True}}
    assert panel.coverage_veto(absent, None, 0, 1_000) == []
    assert panel.round_stop(1, 2, [], [], [])["confident"] is True
    # Every other way of not running still says something about this round.
    crashed = {"antigravity": {"ran": False, "skip": "antigravity (m): exited 1 (boom)"},
               "claude": {"ran": True}}
    assert panel.coverage_veto(crashed, None, 0, 1_000) == [
        "antigravity did not run (antigravity (m): exited 1 (boom))"]


def test_a_box_carrying_none_of_the_reviewer_clis_cannot_stop_confidently():
    """The floor under the exemption above. Absent seats are exempted one at a
    time, so a host that carries NONE of them produces an empty veto list — and
    `confident` is `not veto`, so the round reports a confident stop on a diff
    nobody read. That is the strongest wrong signal the panel can emit, and it
    lands on exactly the unattended hosts the exemption was added for."""
    none_ran = {"antigravity": {"ran": False, "absent": True, "skip": "a: CLI absent"},
                "pi": {"ran": False, "absent": True, "skip": "p: CLI absent"}}
    veto = panel.coverage_veto(none_ran, None, 0, 1_000)
    assert veto == ["no reviewer ran — nothing read this diff"]
    assert panel.round_stop(1, 2, [], [], veto)["confident"] is False


def test_absence_is_recorded_state_rather_than_a_message_tail():
    """The exemption reads `meta["absent"]`, not the end of the skip line.

    Both directions of the message-matching version are wrong. A genuine failure
    whose composed reason happens to end in those words would escape the veto;
    and the day the absent branch's message gains a suffix — a hint, an install
    pointer — the standing veto the exemption exists to remove comes back, with
    nothing failing to say so."""
    lookalike = {"codex": {"ran": False, "skip": "codex (gpt): exited 1 — no CLI absent"},
                 "claude": {"ran": True}}
    assert panel.coverage_veto(lookalike, None, 0, 1_000) == [
        "codex did not run (codex (gpt): exited 1 — no CLI absent)"]
    decorated = {"codex": {"ran": False, "absent": True,
                           "skip": "codex (gpt): CLI absent — install it first"},
                 "claude": {"ran": True}}
    assert panel.coverage_veto(decorated, None, 0, 1_000) == []


def test_a_reviewer_whose_cli_is_missing_records_that_as_state(monkeypatch):
    """Where the flag comes from — the one branch that may set it."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: None)
    got = panel.review_llm("antigravity", "m", "p")
    assert got.absent is True and got.skip.endswith(panel.CLI_ABSENT)


def test_a_panel_with_nothing_to_declare_vetoes_nothing():
    meta = {"claude": {"ran": True, "truncated": False, "could_not_assess": []}}
    assert panel.coverage_veto(meta, None, 0, 1_000) == []


# ---- the master rules on coverage, findings or not -------------------------

def _judge_returning(monkeypatch, reply):
    seen = {}

    def fake_run_cli(args, label, timeout=panel.CLI_TIMEOUT, attempts=3, stdin_text=None):
        seen["prompt"] = stdin_text
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
    findings, skip, note = panel.adjudicate(
        [], "diff", "opus", 34, coverage={"codex": ["the migration"], "claude": []})
    assert findings == [] and skip is None
    assert note.startswith("the migration is unread")
    assert "could not assess the migration" in seen["prompt"]


def test_a_coverage_only_reply_is_not_a_judge_that_failed_to_rule(monkeypatch):
    """With nothing to adjudicate, an envelope carrying only the note is a
    complete answer — reporting it as unparseable would veto the round."""
    _judge_returning(monkeypatch, json.dumps({"coverage_note": "nothing unread"}))
    _, skip, note = panel.adjudicate([], "diff", "", 34,
                                     coverage={"codex": ["the schema"]})
    assert skip is None and note == "nothing unread"


def test_a_coverage_only_round_whose_judge_said_nothing_is_not_adjudicated(monkeypatch):
    """The findings path calls an unparseable reply a judge that failed to rule;
    the coverage-only path returned skip_reason=None for ANY unusable reply — prose,
    a crash-truncated answer, an object carrying neither key. `coverage_veto` then
    added no "the round was not adjudicated" entry and, with no other veto,
    `round_stop` recorded `stop: true, confident: true`: a confident clean verdict
    on a judge that produced nothing, in the one round where the split most needed
    adjudicating."""
    for reply in ("I had a look and it all seems fine, honestly.",
                  json.dumps({"summary": "fine"}),
                  '{"verdicts": [{"id": 1'):
        _judge_returning(monkeypatch, reply)
        findings, skip, note = panel.adjudicate([], "diff", "", 34,
                                                coverage={"codex": ["the migration"]})
        assert findings == [] and skip and "unparseable" in skip, reply


def test_nothing_found_and_nothing_declared_needs_no_judge(monkeypatch):
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the judge must not run with nothing to rule on")))
    assert panel.adjudicate([], "diff", "", 34, coverage={"claude": []}) == ([], None, "")


# ---- what the PR comment promises ------------------------------------------

PANEL_CFG = {"github": "acme/board", "path": "/tmp/acme-board",
             "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
             "review_panel": {}}


def _fake_adjudicate(clusters, diff, model, pr, budget=None, coverage=None):
    """Every reported finding confirmed, one canonical record each — the judge's
    ruling is not what these tests are about."""
    flat = [f for grp in clusters for f in grp]
    return ([panel.Canonical(id=panel._finding_id(pr, i + 1), severity=f.severity,
                             file=f.file, line=f.line, synthesis=f.title,
                             verdict="confirmed", detail=f.detail, reported_by=[f],
                             rationale="real")
             for i, f in enumerate(flat)], None, "")


def _stub_panel(monkeypatch, findings=None, title="feat: x", cfg=PANEL_CFG):
    """Every process a run would spawn, replaced — so what is under test is what
    the panel itself builds, not the CLIs."""
    if findings is None:
        findings = [panel.Finding("claude", "P3", "a.py", 3, "unused import")]
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(panel, "sh", lambda args, **kw: (
        json.dumps({"title": title, "additions": 3, "deletions": 1,
                    "baseRefName": "main", "headRefName": "feat/x", "headRefOid": "abc"})
        if args[:3] == ["gh", "pr", "view"] else "diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun(list(findings), None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", _fake_adjudicate)


def _report(monkeypatch, capsys, tmp_path, round_no, baseline=(), max_rounds=None,
            findings=None, title="feat: x", cfg=PANEL_CFG):
    """One whole panel run, so what is under test is the report it writes on the
    PR and the payload it writes beside it."""
    _stub_panel(monkeypatch, findings, title, cfg)
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
    report, out = _report(monkeypatch, capsys, tmp_path, 1)
    assert "**Rounds:**" not in report and "go again" not in report
    assert "· round 1" not in report
    assert "unused import" in report
    # ...and the PAYLOAD says the same thing, which is the copy the board keeps.
    # It used to carry `round_stop` regardless, so `record_review` stored a plain
    # `/panel` read as a cycle mid-flight ("not stopped: 1 finding(s) no earlier
    # round raised") that nothing would ever advance.
    payload = json.loads(Path(out).read_text())
    assert payload["round_stop"] is None and payload["stop_reason"] is None
    # None, not 0: "the panel did not say" is the column's own state, and the
    # count would be the vacuous "every finding, against no earlier round".
    assert payload["new_findings"] is None and payload["new_finding_keys"] == []


def test_a_review_only_run_that_found_nothing_records_no_convergence(monkeypatch,
                                                                     capsys, tmp_path):
    """The other half of it: with no findings the payload said `stopped: true,
    stop_confident: true, "dry — no findings to fix"` — a confident-convergence
    record on the board for a PR that had no cycle at all."""
    _, out = _report(monkeypatch, capsys, tmp_path, 1, findings=[])
    payload = json.loads(Path(out).read_text())
    assert payload["round_stop"] is None and payload["stop_reason"] is None


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
    report, out = _report(monkeypatch, capsys, tmp_path, 2, baseline=[r1], max_rounds=2)
    assert "**Rounds:** round 2 of at most 2" in report
    # The same finding again is not fresh damage, and the ↻/🆕 marker stays off.
    assert "🆕" not in report
    assert "1 finding(s) an earlier round already raised" in report
    # ...and the payload says the same thing the report does.
    payload = json.loads(Path(out).read_text())
    assert payload["new_findings"] == 0
    assert payload["to_fix"][0]["new_this_round"] is False


def test_a_round_two_with_no_baseline_says_so_on_the_pr(monkeypatch, capsys, tmp_path):
    """The operator is told to list the vetoes. A round that never had a baseline
    cannot claim convergence, and "not convergence" with nothing listed leaves the
    reader to guess which of the reasons applied."""
    report, out = _report(monkeypatch, capsys, tmp_path, 2, max_rounds=3)
    assert "no --baseline" in report
    payload = json.loads(Path(out).read_text())
    assert payload["round_stop"]["confident"] is False
    assert any("no --baseline" in v for v in payload["round_stop"]["veto"])


def test_a_cycles_rounds_all_carry_the_same_id(monkeypatch, capsys, tmp_path):
    """Round 1 mints it, every later round inherits it from its earliest baseline.
    Without it "the round that answered this declaration" is the guess "whatever
    ran next on this PR", which credits one cycle's round 2 to another's round 1
    the moment two agents loop the same PR at once."""
    _, r1 = _report(monkeypatch, capsys, tmp_path, 1, max_rounds=3)
    r1_payload = json.loads(Path(r1).read_text())
    _, r2 = _report(monkeypatch, capsys, tmp_path, 2, baseline=[r1], max_rounds=3)
    assert json.loads(Path(r2).read_text())["cycle"] == r1_payload["cycle"]
    # A round 1 of a DIFFERENT cycle over the same PR is not the same cycle.
    _, other = _report(monkeypatch, capsys, tmp_path, 1, max_rounds=3)
    assert json.loads(Path(other).read_text())["cycle"] != r1_payload["cycle"]


def test_the_round_verdict_survives_a_report_cut_to_fit_a_comment():
    """`fit_comment` trims from the end and the verdict is the last block, so the
    one line the caller of a cycle acts on was the first thing to go — leaving a
    comment that lists findings and never says whether to go again."""
    verdict = "\n\n**Rounds:** round 2 of at most 2 — **stop**: dry, unreviewed"
    report = ("## Reviewer panel — PR #1\n\n### To fix\n"
              + "\n".join(f"- **P2** `a.py:{i}` — issue {i}" for i in range(5_000))
              + verdict)
    fitted = panel.fit_comment(report)
    assert len(fitted) <= panel.COMMENT_CHARS
    assert fitted.endswith(verdict) and "truncated" in fitted


def test_a_verdict_block_too_long_to_reserve_is_cut_rather_than_overflowing():
    """The tail was reserved whole and only the head was sliced, so `max(0, …)`
    clamped the SLICE and not the RESULT: a run with many vetoes returned a comment
    LONGER than the limit, and `--post` loses the whole thing to a hard API
    rejection. The verdict line survives; the veto list is what gets cut."""
    verdict = ("\n\n**Rounds:** round 2 of at most 2 — **stop**: dry, unreviewed\n"
               + "\n".join(f"  - ⚠️ codex could not assess area {i}" for i in range(4_000)))
    report = ("## Reviewer panel — PR #1\n\n### To fix\n"
              + "\n".join(f"- **P2** `a.py:{i}` — issue {i}" for i in range(5_000))
              + verdict)
    assert len(verdict) > panel.COMMENT_CHARS       # the tail alone does not fit
    fitted = panel.fit_comment(report)
    assert len(fitted) <= panel.COMMENT_CHARS
    assert "**Rounds:** round 2 of at most 2 — **stop**" in fitted


def test_a_baseline_problem_is_not_printed_twice_on_the_pr(monkeypatch, capsys, tmp_path):
    """It is deliberately both a config note and a veto — the payload needs both,
    since `config_notes` never reaches the board and the veto list is its only
    copy — but a reader of the comment saw the same sentence in two places and read
    it as two problems. The second appearance points at the first."""
    report, out = _report(monkeypatch, capsys, tmp_path, 2, max_rounds=3)
    assert report.count("nothing to compare against") == 1
    assert "the config note above" in report
    # ...and the payload still carries it in both roles, in full.
    payload = json.loads(Path(out).read_text())
    assert any("no --baseline" in n for n in payload["config_notes"])
    assert any("nothing to compare against" in v for v in payload["round_stop"]["veto"])


# ---- the hard gate is part of the loop -------------------------------------

SONAR_CFG = {**PANEL_CFG,
             "reviewers": {**PANEL_CFG["reviewers"], "sonarqube": {"enabled": True}}}


def _sonar_round(monkeypatch, tmp_path, round_no, baseline=(), max_rounds=3):
    """A round whose ONLY outstanding item is a SonarCloud hard-gate issue."""
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: SONAR_CFG)
    monkeypatch.setattr(panel, "sh", lambda args, **kw: (
        json.dumps({"title": "feat: x", "additions": 3, "deletions": 1,
                    "baseRefName": "main", "headRefName": "feat/x", "headRefOid": "abc"})
        if args[:3] == ["gh", "pr", "view"] else "diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "review_sonarqube", lambda *a, **k: (
        "ERROR", [panel.Finding("sonarqube", "P2", "a.py", 9, "cognitive complexity 21")],
        [], None))
    out = tmp_path / f"s{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=max_rounds) == 0
    return str(out), json.loads(out.read_text())


def test_a_new_hard_gate_issue_is_not_a_dry_round(monkeypatch, tmp_path):
    """The gate's issues MUST end up resolved, and they were left out of the round
    diff entirely — so a round whose only outstanding item was one of them recorded
    itself as dry and the caller stopped without running another fixer."""
    _, r1 = _sonar_round(monkeypatch, tmp_path, 1)
    assert r1["new_findings"] == 1
    assert r1["round_stop"]["stop"] is False


def test_a_hard_gate_issue_still_open_after_the_fix_earns_another_round(monkeypatch,
                                                                        tmp_path):
    path, _ = _sonar_round(monkeypatch, tmp_path, 1)
    _, r2 = _sonar_round(monkeypatch, tmp_path, 2, baseline=[path])
    assert r2["new_findings"] == 0            # not new — but not resolved either
    assert r2["round_stop"]["stop"] is False
    # "outstanding", not "confirmed": a hard-gate issue is never adjudicated, and
    # the reason string ends up on the PR comment.
    assert "still outstanding" in r2["round_stop"]["reason"]


# ---- the baseline the next round needs -------------------------------------

def test_a_json_file_that_could_not_be_written_fails_the_run(monkeypatch, capsys,
                                                             tmp_path):
    """That file IS the next round's baseline. Warning and exiting 0 let the caller
    advance the cycle onto a baseline that does not exist, where every repeated
    finding reads as new."""
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: PANEL_CFG)
    monkeypatch.setattr(panel, "sh", lambda args, **kw: (
        json.dumps({"title": "feat: x", "additions": 3, "deletions": 1,
                    "baseRefName": "main", "headRefName": "feat/x", "headRefOid": "abc"})
        if args[:3] == ["gh", "pr", "view"] else "diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    unwritable = tmp_path / "no-such-dir" / "r1.json"
    assert panel.run("board", 34, post=False, json_file=str(unwritable),
                     record=False, round_no=1, max_rounds=2) == panel.UNWRITTEN_PAYLOAD_EXIT
    err = capsys.readouterr().err
    # ...and the review it already paid for is still reported, not thrown away.
    assert "could not write" in err and "re-run the CYCLE" in err


SKIP_CFG = {**PANEL_CFG, "review_panel": {"skip_title_patterns": [r"^chore\("]}}


def test_a_skipped_pr_still_writes_the_baseline_the_caller_was_promised(monkeypatch,
                                                                       capsys, tmp_path):
    """`--json-file` was honoured on every exit but this one, which returned 0 with
    no file. The caller is told "if the panel could not write that file the round
    did not happen", and then feeds the file to the next round as `--baseline`: a
    skipped PR left it no signal and no baseline at all."""
    _stub_panel(monkeypatch, title="chore(deps): bump x", cfg=SKIP_CFG)
    out = tmp_path / "skip.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False) == 0
    payload = json.loads(out.read_text())
    assert payload["reviewed"] is False and "skip pattern" in payload["skip_reason"]
    assert payload["round_stop"] is None


def test_a_skipped_round_says_which_round_it_was_and_whose_cycle(monkeypatch, capsys,
                                                                 tmp_path):
    """The skip payload was built from the defaults alone, so a skipped round 2
    serialised itself as round 1 with a fresh id. Fed forward as the next round's
    `--baseline` — which is what the caller is told to do with every round's file —
    it collided with the real round 1 over the round number and renamed the cycle
    out from under every later round."""
    r1 = _payload(tmp_path, "r1.json", 1, ["unused import"], cycle="cyc-1")
    _stub_panel(monkeypatch, title="chore(deps): bump x", cfg=SKIP_CFG)
    out = tmp_path / "skip.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=2, baseline=[r1]) == 0
    payload = json.loads(out.read_text())
    assert payload["round"] == 2 and payload["cycle"] == "cyc-1"
    assert payload["prior_rounds"] == 1 and payload["prior_findings"] == 1
    # ...and a round 3 reading both files sees two rounds of one cycle, not an
    # ambiguity and not a conflict.
    b = panel.load_baseline([r1, str(out)],
                            {"github": "acme/board", "pr": 34, "round": 3})
    assert b.problems == [] and b.rounds == {1, 2} and b.cycle == "cyc-1"


def test_a_round_that_lost_its_baseline_records_no_cycle_rather_than_a_new_one(
        monkeypatch, capsys, tmp_path):
    """`followed_by` requires the cycles to match, so minting a fresh id for a
    round 2 whose `--baseline` was mistyped makes round 1 and round 2 of one PR two
    unrelated cycles forever — every re-review declaration round 1 made answers
    `null` permanently, and nothing on the board says why. Null records
    "unattributable", which is the truth and is recoverable."""
    _, out = _report(monkeypatch, capsys, tmp_path, 2,
                     baseline=[str(tmp_path / "nope.json")])
    payload = json.loads(Path(out).read_text())
    assert payload["cycle"] is None
    assert any("unreadable" in n for n in payload["config_notes"])


def test_a_review_only_run_records_no_cycle_either(monkeypatch, capsys, tmp_path):
    """`ReviewIn.cycle` documents "absent ... for a standalone review that is
    nobody's round 2", and the producer minted one on every path anyway."""
    _, out = _report(monkeypatch, capsys, tmp_path, 1)
    assert json.loads(Path(out).read_text())["cycle"] is None


def test_a_json_file_that_is_a_symlink_is_not_followed(tmp_path):
    """`Path.write_text` follows symlinks, so a pre-planted
    `/tmp/panel-34-r1.json` -> `~/.ssh/authorized_keys` is a write under the
    caller's own identity. The shipped defence was a paragraph telling an LLM
    orchestrator to `mktemp -d`; this is the one every caller gets."""
    target = tmp_path / "authorized_keys"
    target.write_text("original")
    link = tmp_path / "r1.json"
    link.symlink_to(target)
    assert panel.write_payload(str(link), {"round": 1})
    assert target.read_text() == "original"


def test_an_integral_float_is_the_same_declaration_on_both_paths():
    """`app/api/reviews.py::_count_or_none` accepts `1.0` as a count; `_flag` fell
    through to False for it. One model output, two answers about whether a
    declaration was made."""
    assert panel._flag(1.0) is True
    assert panel._flag(0.0) is False
    assert panel._flag(1.5) is False


def test_a_skipped_pr_whose_baseline_could_not_be_written_fails_too(monkeypatch,
                                                                    capsys, tmp_path):
    """Same contract as a reviewed run: exit non-zero, so the orchestrator does not
    advance the cycle onto a baseline that does not exist."""
    _stub_panel(monkeypatch, title="chore(deps): bump x", cfg=SKIP_CFG)
    unwritable = tmp_path / "no-such-dir" / "skip.json"
    assert panel.run("board", 34, post=False, json_file=str(unwritable),
                     record=False) == panel.UNWRITTEN_PAYLOAD_EXIT
    assert "could not write" in capsys.readouterr().err


def test_the_repo_a_payload_names_is_the_one_it_resolved(monkeypatch, capsys, tmp_path):
    """`--repo` has no argparse default, so the documented `panel.py --pr N` wrote
    `"repo": null` — a payload nobody can attribute, which round 2 then discarded
    as "does not say which review it is from". The whole round diff no-opped for
    the invocation the workflow prescribes."""
    _, out = _report(monkeypatch, capsys, tmp_path, 1,
                     cfg={**PANEL_CFG, "name": "board"}, max_rounds=2)
    assert json.loads(Path(out).read_text())["repo"] == "board"


# ---- the CLI's own arguments -----------------------------------------------

def test_a_round_past_the_cap_is_rejected_rather_than_recorded(monkeypatch):
    """`--round 5 --max-rounds 2` records an impossible position and hits the cap
    branch on the spot, writing "round cap (2) reached" into a round 5."""
    monkeypatch.setattr(sys, "argv",
                        ["panel.py", "--pr", "1", "--round", "5", "--max-rounds", "2"])
    with pytest.raises(SystemExit, match="past --max-rounds"):
        panel.main()


def test_a_round_past_the_DEFAULT_cap_is_rejected_too(monkeypatch):
    """The guard used to fire only when --max-rounds was spelled out, and the cap
    `run()` applies is the default when it is not. So `--round 3` alone passed
    validation and took the cap branch on the spot, writing "round cap (2)
    reached — …, unreviewed" into a round 3 and printing "round 3 of at most 2" —
    the exact corrupted metadata the guard exists to prevent."""
    monkeypatch.setattr(sys, "argv", ["panel.py", "--pr", "1", "--round", "3"])
    with pytest.raises(SystemExit, match="past --max-rounds 2"):
        panel.main()


def test_the_round_the_default_cap_allows_is_still_accepted(monkeypatch):
    """Round 2 with no --max-rounds is the ordinary re-review, and the tighter
    guard must not refuse it. It gets past validation and dies on the repo instead,
    which is as far as this test can go without a checkout."""
    monkeypatch.setattr(sys, "argv", ["panel.py", "--pr", "1", "--round", "2"])
    monkeypatch.setattr(panel, "run", lambda *a, **k: 0)
    assert panel.main() == 0
