"""#67's third question: is a round's finding standing where the last fix pass was
working, and does it say that pass was built on a wrong assumption?

Two measurements, and the split between them is the thing these tests defend.

The MECHANICAL one (`panel_scope._recurrence`) can see position and nothing else:
the previous round complained about this file, the fixer wrote lines in it, this
finding is on top of what it wrote. Replayed over 36 rounds of this board's own
history that fires on about four new findings in five, at the same rate on the
cycles #67 calls circling as on the ones it does not — because a later round is
READING the fix commit (#41), so the fix's site is where its findings normally
are. The bucket is therefore named for the position (`revisited`) and not for a
verdict (`circling`), and several tests below exist purely to stop the verdict
creeping back in: nothing here may reach a stop rule, a veto, or `confident`.

The ADJUDICATED one is the judge's `premise_verdict`, asked as one extra key on a
verdict it is already writing. It is the half that can see what a finding SAYS
rather than where it sits, and it is stored beside the mechanical answer rather
than folded into it — two witnesses, and the rounds where they disagree are the
rows worth a human's time.

The prompt tests are the awkward, load-bearing ones. A round with no earlier
round must get a judge prompt BYTE-IDENTICAL to the one it has always been given,
or every comparison across this release is also a comparison between two prompts;
and the slot must be swapped on every path, or the literal `<<<RECURRENCE_BRIEF>>>`
token travels to the model as text.
"""

import ast
import json
import sys
from pathlib import Path

import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_rounds  # noqa: E402
import panel_scope  # noqa: E402
import panel_preflight  # noqa: E402  — the seat predicate the e2e rounds pin
from conftest import gh_stub  # noqa: E402


#: A fix pass that wrote lines 100-102 of `app/thing.py` and nothing else.
FIX_ADDED = {"app/thing.py": {100, 101, 102}}

#: What the round before it had asked that fixer to fix.
COMPLAINED = {"app/thing.py": {"aaaa000000000001"}}


# --------------------------------------------------------------------------
# The mechanical bucket: where, not whether
# --------------------------------------------------------------------------

def test_no_fix_range_is_unknown_and_never_a_position():
    """A force-pushed branch, a baseline with no commit, a compare the API
    refused: `_fix_range_diff` has four ways to come back empty and every one of
    them has to leave `unknown`. `elsewhere` would be a positive claim — "we
    looked and this is not where the fixer was" — about a range nobody could
    read."""
    assert panel_scope._recurrence("app/thing.py", 101, {}, COMPLAINED, False) == \
        ("unknown", None)


def test_a_finding_on_top_of_the_fix_in_a_file_that_was_complained_about():
    """All three predicates: the previous round raised a finding in this file, the
    fixer wrote lines in it, and this finding is on one of them. The earlier
    finding's key travels back, because a bucket nobody can check against the
    record it was computed from is not evidence of anything."""
    assert panel_scope._recurrence("app/thing.py", 101, FIX_ADDED, COMPLAINED, True) == \
        ("revisited", "aaaa000000000001")


def test_the_fixer_worked_here_and_nobody_had_complained():
    """Two of the three predicates. Real information and NOT the same news: fresh
    damage where a fix pass happened to be writing is what `provenance`'s
    `introduced` bucket is about, and #67's own caution is that a second finding
    at one site can simply be a second bug."""
    assert panel_scope._recurrence("app/thing.py", 101, FIX_ADDED, {}, True) == \
        ("fix-site", None)


def test_a_finding_away_from_the_fix_is_elsewhere_even_in_a_complained_about_file():
    """The file end alone must never be enough. "Same file" is the reading #67
    explicitly warns is too wide, and this repo's files are long enough that a
    defect 400 lines from the fix is a different neighbourhood."""
    assert panel_scope._recurrence("app/thing.py", 500, FIX_ADDED, COMPLAINED, True) == \
        ("elsewhere", None)


def test_the_radius_is_inclusive_at_its_edge_and_exclusive_past_it():
    """A boundary worth pinning even though the number itself is a guess: what
    must not happen is the rule quietly meaning `radius - 1` or `radius + 1`, so
    that a later calibration is fitting a constant to a different rule than the
    one it thinks it is reading."""
    at_edge = max(FIX_ADDED["app/thing.py"]) + panel_scope.SITE_RADIUS
    assert panel_scope._recurrence("app/thing.py", at_edge, FIX_ADDED, COMPLAINED,
                                   True)[0] == "revisited"
    assert panel_scope._recurrence("app/thing.py", at_edge + 1, FIX_ADDED, COMPLAINED,
                                   True)[0] == "elsewhere"


def test_a_finding_with_no_line_cannot_be_placed():
    """`unknown`, not `elsewhere`. A reviewer that named a file and no line has
    said nothing about position, and recording that as "away from the fix" is the
    invented attribution `_provenance` refuses one function up."""
    assert panel_scope._recurrence("app/thing.py", None, FIX_ADDED, COMPLAINED, True) == \
        ("unknown", None)
    assert panel_scope._recurrence("", 101, FIX_ADDED, COMPLAINED, True) == \
        ("unknown", None)


def test_a_path_that_could_name_two_changed_files_is_unknown():
    """The suffix rule that lets `thing.py` match `app/thing.py` also lets it
    match a second tree's copy. A coin toss between two files is not a
    measurement, and it is the same guard `_provenance` applies to the same
    input."""
    two = {"app/thing.py": {101}, "web/thing.py": {101}}
    assert panel_scope._recurrence("thing.py", 101, two, COMPLAINED, True) == \
        ("unknown", None)


def test_both_ends_match_a_short_path_spelling():
    """Reviewers spell paths differently between rounds, so the fix range and the
    earlier round's complaint may each be recorded under a different spelling of
    one file. Both ends go through `_same_file` or a round's own vocabulary
    decides its measurement."""
    assert panel_scope._recurrence("thing.py", 101, FIX_ADDED,
                                   {"thing.py": {"aaaa000000000001"}}, True) == \
        ("revisited", "aaaa000000000001")


def test_a_file_with_two_earlier_findings_names_the_same_one_every_time():
    """Sorted, so the column is stable across runs. An arbitrary pick that moved
    between runs would make `recurs_of` unauditable — and auditability is the
    whole reason an uncalibrated signal carries a pointer at all."""
    two = {"app/thing.py": {"bbbb000000000002", "aaaa000000000001"}}
    got = {panel_scope._recurrence("app/thing.py", 101, FIX_ADDED, two, True)
           for _ in range(5)}
    assert got == {("revisited", "aaaa000000000001")}


def test_the_vocabulary_is_closed_and_shared_with_the_board():
    """One vocabulary, two processes. The panel spells these buckets and the board
    stores them; #65's class of drift is two sides paraphrasing each other, and it
    has cost this codebase a release already."""
    assert panel_scope.RECURRENCE == ("revisited", "fix-site", "elsewhere", "unknown")
    assert "circling" not in panel_scope.RECURRENCE


# --------------------------------------------------------------------------
# The judge's question
# --------------------------------------------------------------------------

def test_the_premise_vocabulary_keeps_unclear():
    """`unclear` is not a courtesy. Without it a judge with no view either way
    picks whichever of the other two reads as safer, and the measurement fills up
    with confident noise on exactly the findings that most needed a shrug."""
    assert panel_core.PREMISE_VERDICTS == ("invalidates", "separate", "unclear")


def test_a_verdict_outside_the_vocabulary_counts_as_nothing_said():
    """Membership-tested, never pattern-matched. "invalidates the premise" and
    "probably separate" are answers this cannot count, and counting them as the
    word they start with is the drift that hides the signal."""
    assert panel_core._premise_verdict("invalidates") == "invalidates"
    assert panel_core._premise_verdict("  SEPARATE  ") == "separate"
    assert panel_core._premise_verdict("invalidates the premise") == ""
    assert panel_core._premise_verdict("probably separate") == ""
    assert panel_core._premise_verdict(None) == ""
    assert panel_core._premise_verdict(["separate"]) == ""


def test_the_judge_can_answer_the_question_on_a_verdict_it_already_writes():
    """One extra key rather than a second model call — the same trade
    `coverage_note` made. A finding record has to carry it out the other side or
    the answer is paid for and dropped."""
    said = panel_rounds.Finding(reviewer="codex", severity="P2", file="a.py",
                                line=9, title="a defect", detail="")
    [c] = panel_rounds._parse_verdicts(
        [{"id": "F01", "members": [0], "real": True, "severity": "P2",
          "file": "a.py", "line": 9, "synthesis": "merged", "reason": "why",
          "premise": "invalidates"}],
        [said], 34)
    assert c.premise_verdict == "invalidates"
    assert c.as_dict()["premise_verdict"] == "invalidates"


def test_a_judge_that_says_nothing_is_not_the_same_as_one_that_could_not_tell():
    """The brief tells the judge to leave the key off when it has nothing to say,
    and every round with no earlier round never carries the brief at all. `""` is
    "not asked or not answered"; `unclear` is "asked, looked, cannot tell". Two
    states, and the whole vocabulary exists to keep them apart."""
    said = panel_rounds.Finding(reviewer="codex", severity="P2", file="a.py",
                                line=9, title="a defect", detail="")
    [c] = panel_rounds._parse_verdicts(
        [{"id": "F01", "members": [0], "real": True, "severity": "P2",
          "file": "a.py", "line": 9, "synthesis": "merged", "reason": "why"}],
        [said], 34)
    assert c.premise_verdict == ""


# --------------------------------------------------------------------------
# The prompt: what a round with no earlier round is given
# --------------------------------------------------------------------------

def test_a_round_with_nothing_to_compare_against_asks_nothing():
    """Empty brief, so the caller swaps the slot for the empty string — see the
    byte-identity test below, which is what that emptiness is FOR."""
    assert panel_rounds.recurrence_brief([], None) == ""
    assert panel_rounds.recurrence_brief([], 1) == ""


def test_the_brief_names_the_round_and_lists_what_it_asked_for():
    """The judge is being asked about a fix it can see in the diff and complaints
    it cannot. If the complaints do not arrive it answers `separate` about a
    premise it was never shown, which is worse than not asking."""
    got = panel_rounds.recurrence_brief(
        [("aaaa000000000001", "P1", "app/thing.py", 101, "the echo test is wrong")], 2)
    assert "round 2" in got
    assert "app/thing.py:101" in got and "the echo test is wrong" in got
    assert "[P1]" in got


def test_a_long_list_is_cut_and_the_cut_is_declared():
    """Past a couple of dozen complaints the judge is being asked to hold two
    whole reviews at once. What must not happen is a silent cut: a judge shown
    fifteen of forty and told so can answer `unclear`, and one shown fifteen and
    told nothing answers `separate` about a premise it never saw."""
    many = [(f"key{i:012d}", "P3", "a.py", i, f"finding {i}") for i in range(60)]
    got = panel_rounds.recurrence_brief(many, 2)
    assert got.count("\n- ") >= panel_rounds.MAX_RECURRENCE_FINDINGS
    assert "not listed" in got and "unclear" in got
    assert "finding 59" not in got


def test_the_round_one_judge_prompt_is_byte_identical_to_the_one_before_this():
    """The comparison this whole release is for is a comparison BETWEEN ROUNDS,
    and it is worth nothing if a round 1 and a round 2 were also given two
    different prompts. So the slot is a literal token swapped for the empty
    string, exactly as `JUDGE_CODE_SLOT` is, and a round with no earlier round
    gets back the prompt it has always had."""
    filled = panel_core.JUDGE_PROMPT.format(findings="F", coverage="C", ci="", diff="D")
    round_one = (filled.replace(panel_core.JUDGE_CODE_SLOT, "")
                       .replace(panel_core.JUDGE_RECURRENCE_SLOT, ""))
    before = (filled.replace(panel_core.JUDGE_CODE_SLOT, "")
                    .replace(panel_core.JUDGE_RECURRENCE_SLOT + "\n", ""))
    assert round_one.replace("\n\n", "\n") == before.replace("\n\n", "\n")
    assert "RECURRENCE" not in round_one and "#67" not in round_one


def test_the_slot_never_reaches_a_model_as_literal_text():
    """The failure mode of a token-and-replace prompt: a path that forgets the
    swap sends `<<<RECURRENCE_BRIEF>>>` to the model as text. `adjudicate` replaces
    it unconditionally, so this pins that the token appears exactly once in the
    template and that both fills remove it."""
    assert panel_core.JUDGE_PROMPT.count(panel_core.JUDGE_RECURRENCE_SLOT) == 1
    for fill in ("", panel_rounds.recurrence_brief(
            [("k" * 16, "P2", "a.py", 4, "t")], 2)):
        assert panel_core.JUDGE_RECURRENCE_SLOT not in \
            panel_core.JUDGE_PROMPT.replace(panel_core.JUDGE_RECURRENCE_SLOT, fill)


def test_the_brief_tells_the_judge_the_common_answer_is_separate():
    """The one failure that would make this worthless is a judge reading "was the
    last fix wrong?" as an invitation. A second bug in a file somebody just edited
    is a second bug, and the brief has to say so in as many words — #67's own
    limit, enforced in the prompt because nothing downstream can enforce it."""
    got = panel_rounds.recurrence_brief([("k" * 16, "P2", "a.py", 4, "t")], 2)
    assert "THIS IS THE DEFAULT AND THE COMMON CASE" in got
    assert "Nothing is decided by your answer" in got


# --------------------------------------------------------------------------
# What travels between rounds
# --------------------------------------------------------------------------

THIS_RUN = {"repo": "acme", "github": "acme/board", "pr": 34, "round": 3}


def _round(tmp_path, name, round_no, **over):
    p = tmp_path / name
    p.write_text(json.dumps({
        "round": round_no, "cycle": "abc123", "reviewed": True,
        "repo": "acme", "github": "acme/board", "pr": 34,
        "to_fix": [], "dismissed": [], "sonar_findings": [],
        **over,
    }))
    return str(p)


def _finding(key, file="app/thing.py", line=101, sev="P2", title="a defect"):
    return {"key": key, "file": file, "line": line, "severity": sev,
            "synthesis": title, "reported_by": [{"title": title}]}


def test_what_the_anchor_round_asked_for_travels_with_the_baseline(tmp_path):
    """The complaint end of the chain. It has to survive the trip between
    processes exactly as `unread_files` does, or every later round measures
    against an empty set and reports `fix-site` for everything."""
    b = panel.load_baseline(
        [_round(tmp_path, "r2.json", 2, head_sha="b" * 40,
                to_fix=[_finding("aaaa000000000001")])], THIS_RUN)
    assert b.fixed_here == {"app/thing.py": {"aaaa000000000001"}}
    assert b.fixed_findings == [
        ("aaaa000000000001", "P2", "app/thing.py", 101, "a defect")]


def test_a_dismissed_finding_is_nobody_s_premise(tmp_path):
    """The master ruled it not real, so no fixer was ever sent to it and no fix
    pass can have been built on it. Counting it would make every file the panel
    ever mentioned look like a place the fixer was answering a complaint."""
    b = panel.load_baseline(
        [_round(tmp_path, "r2.json", 2, head_sha="b" * 40,
                dismissed=[_finding("dddd000000000004")],
                sonar_findings=[_finding("ssss000000000005", file="s.py")])],
        THIS_RUN)
    assert b.fixed_here == {"s.py": {"ssss000000000005"}}


def test_only_the_round_that_supplied_the_anchor_counts(tmp_path):
    """The fix range under attribution is ONE round wide, so the only complaints
    it can have been answering are that round's. A union over every earlier round
    would read round 1's finding, round 3's finding and round 2's unrelated edit
    as one chain."""
    b = panel.load_baseline(
        [_round(tmp_path, "r1.json", 1, head_sha="a" * 40,
                to_fix=[_finding("1111000000000001", file="old.py")]),
         _round(tmp_path, "r2.json", 2, head_sha="b" * 40,
                to_fix=[_finding("2222000000000002", file="new.py")])],
        THIS_RUN)
    assert b.head_sha == "b" * 40
    assert set(b.fixed_here) == {"new.py"}


def test_a_finding_that_cannot_be_placed_is_not_evidence_the_fixer_was_anywhere(
        tmp_path):
    """A complaint with no file is dropped rather than filed under `""` — which
    `_same_file` would suffix-match against every path there is, turning one
    malformed record into "the fixer was working everywhere"."""
    b = panel.load_baseline(
        [_round(tmp_path, "r2.json", 2, head_sha="b" * 40,
                to_fix=[_finding("aaaa000000000001", file=""), "not a dict"])],
        THIS_RUN)
    assert b.fixed_here == {} and b.fixed_findings == []


def test_a_baseline_from_before_this_existed_measures_nothing(tmp_path):
    """Every payload banked before this release carries findings and no anchor at
    all. It must leave both fields empty rather than raising — a baseline outlives
    the release that wrote it, and `--baseline` is fed old payloads by design."""
    b = panel.load_baseline([_round(tmp_path, "r2.json", 2)], THIS_RUN)
    assert b.fixed_here == {} and b.fixed_findings == []


# --------------------------------------------------------------------------
# The line this must not cross
# --------------------------------------------------------------------------

def test_nothing_in_the_measurement_reaches_a_stop_rule():
    """#67's first limit, enforced rather than promised: *instrument it first,
    watch it over a few dozen cycles, and only then let it gate anything*. The
    replay behind `_recurrence` is why that limit is right — the mechanical rate
    saturates and separates nothing — so a `revisited` count must not reach
    `round_stop`, must not appear in `stop["veto"]` (which is what decides
    `confident`), and must not be able to end a cycle.

    Asserted over the SOURCE of the stop rule, because there is no round shaped
    to catch this: a rule that fires only on a circling cycle would be green on
    every fixture that is not one, which is precisely the population a wrong
    wiring would hide in.
    """
    src = Path(panel_rounds.__file__).read_text()
    # Sliced by the parser rather than by a `\ndef ` search: `round_stop` is the
    # last function in its module, so a text search runs off the end and into
    # `__all__` — which names `recurrence_brief` and made this assertion fail on
    # an export rather than on a stop rule.
    tree = ast.parse(src)
    [fn] = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "round_stop"]
    stop = ast.get_source_segment(src, fn)
    for word in ("recurrence", "revisited", "fix-site", "premise_verdict",
                 "recurs_of"):
        assert word not in stop, f"round_stop reads `{word}` — #67 gates nothing yet"


#: The four modules allowed to know these words: one defines the vocabulary, one
#: carries it between rounds, one asks the judge, one computes and prints it.
MEASURES_IT = {"panel_scope.py", "panel_rounds.py", "panel_core.py", "panel.py"}


def test_no_other_part_of_the_loop_has_learned_the_vocabulary():
    """The blast radius, asserted as a whole rather than one module at a time.

    Every other thing in `harness/loops/` decides something — `panel_caps` holds
    #55's ceilings, `preland` rules on whether a PR may land, `epic` and `lander`
    drive cycles — and a measurement nobody has calibrated must not be reachable
    from any of them yet. Written this way because the modules that would be wrong
    to read it are not all here yet: a named list goes stale the day one lands,
    and this fails on the new file instead.
    """
    loops = Path(panel_rounds.__file__).parent
    for mod in sorted(loops.glob("*.py")):
        if mod.name in MEASURES_IT:
            continue
        body = mod.read_text()
        for word in ("revisited", "recurrence", "premise_verdict", "recurs_of"):
            assert word not in body, (
                f"{mod.name} reads `{word}` — #67 instruments and gates nothing yet")


# --------------------------------------------------------------------------
# The wiring, end to end
#
# `test_panel_provenance` records at length why this section is not decoration:
# every helper in its file was unit-green while `unread_files` came back empty on
# every real run ever made, because `run()` looked its budgets up under a key that
# did not exist. Nothing calling a helper directly could see it. The same shape of
# mistake is available here — a tally computed and never put in the payload, a
# baseline field populated and never read — so what these drive is `run()`.
# --------------------------------------------------------------------------

PR_DIFF = (
    "diff --git a/app/sync.py b/app/sync.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/app/sync.py\n"
    "+++ b/app/sync.py\n"
    "@@ -1,1 +1,2 @@\n"
    "+mirror = {}\n"
    "diff --git a/app/far.py b/app/far.py\n"
    "index 3333333..4444444 100644\n"
    "--- a/app/far.py\n"
    "+++ b/app/far.py\n"
    "@@ -1,1 +1,2 @@\n"
    "+tail = 1\n"
)

#: The fix pass between the two rounds: lines 11 and 12 of `app/sync.py`.
FIX_COMPARE = json.dumps({
    "status": "ahead",
    "files": [{"filename": "app/sync.py",
               "patch": "@@ -10,0 +11,2 @@\n+written_by_the_fix()\n+and_this_one_too()"}]})

E2E_CFG = {
    "github": "acme/e2e",
    "path": "/tmp/acme-e2e",
    "_rules_baseline": ".harness-rules.sample",
    "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
    "review_panel": {"refuse_over_cap_multiple": 0, "manifest_moves": False},
}


@pytest.fixture(autouse=True)
def every_seat_is_on_this_box(monkeypatch):
    """Pin the HOST out of every round here, for the reason
    `test_panel_provenance` states at length: #138's pre-flight skips a seat whose
    CLI is not on PATH, so a file that runs whole rounds is otherwise asserting on
    which vendor CLIs the machine running the suite happens to carry — green
    locally, and quietly not engaging at all on a CI runner that has none."""
    monkeypatch.setattr(panel_preflight, "seat_installed", lambda name: True)


def _panel_round(monkeypatch, tmp_path, round_no, findings, head, baseline=(),
                 premise=""):
    """One round with every subprocess replaced, so what is under test is the
    payload rather than any CLI."""
    fake_sh = gh_stub(
        meta={"title": "feat: mirror", "additions": 20, "deletions": 2,
              "headRefOid": head},
        compare=FIX_COMPARE,
        diff=PR_DIFF)

    def fake_review(name, model, prompt, effort="", **_kw):
        return panel.ReviewerRun(
            [panel.Finding("claude", "P2", f, ln, t, "detail")
             for f, ln, t in findings], None, 800, None)

    seen = {}

    def fake_adjudicate(clusters, diff, model, pr, budget=None, coverage=None,
                        ci="", recurrence="", **_kw):
        seen["recurrence"] = recurrence
        return ([panel.Canonical(id=panel._finding_id(pr, i + 1), severity="P2",
                                 file=f.file, line=f.line, synthesis=f.title,
                                 verdict="confirmed", detail="detail",
                                 reported_by=[f], rationale="real",
                                 premise_verdict=premise)
                 for i, grp in enumerate(clusters) for f in grp], None, "")

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: E2E_CFG)
    monkeypatch.setattr(panel_core, "sh", fake_sh)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", fake_adjudicate)
    out = tmp_path / f"e2e-r{round_no}.json"
    assert panel.run("e2e", 77, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=3) == 0
    return str(out), json.loads(out.read_text()), seen


def test_a_round_with_nothing_before_it_measures_nothing_and_asks_nothing(
        monkeypatch, tmp_path):
    """`{}` and not all-zero, matching `provenance_counts` beside it: the question
    does not arise, which a consumer must be able to tell from "attribution ran and
    found none". And the judge is handed an empty brief, so its prompt is the one
    it has always had."""
    _, r1, seen = _panel_round(monkeypatch, tmp_path, 1,
                               [("app/sync.py", 11, "a stale mirror")], head="a" * 40)
    assert r1["recurrence_counts"] == {} and r1["premise_counts"] == {}
    assert all(f["recurrence"] is None and f["recurs_of"] is None
               for f in r1["to_fix"])
    assert seen["recurrence"] == ""


def test_a_later_round_places_its_findings_against_the_fix_and_says_so(
        monkeypatch, tmp_path):
    """The whole chain in one round: round 1's complaint about `app/sync.py`
    travels through the baseline, the compare API says the fixer wrote lines 11-12
    of that file, and round 2's new finding at line 12 comes back `revisited`
    naming round 1's key. The finding in the untouched file is `elsewhere`."""
    p1, r1, _ = _panel_round(monkeypatch, tmp_path, 1,
                             [("app/sync.py", 11, "a stale mirror")], head="a" * 40)
    _, r2, seen = _panel_round(
        monkeypatch, tmp_path, 2,
        [("app/sync.py", 12, "the mirror is written twice"),
         ("app/far.py", 2, "an unrelated defect")],
        head="b" * 40, baseline=[p1])
    got = {f["file"]: f for f in r2["to_fix"]}
    assert got["app/sync.py"]["recurrence"] == "revisited"
    assert got["app/sync.py"]["recurs_of"] == r1["to_fix"][0]["key"]
    assert got["app/far.py"]["recurrence"] == "elsewhere"
    assert got["app/far.py"]["recurs_of"] is None
    assert r2["recurrence_counts"] == {"revisited": 1, "fix-site": 0,
                                       "elsewhere": 1, "unknown": 0}
    # …and the judge was handed round 1's complaint to rule on.
    assert "a stale mirror" in seen["recurrence"] and "round 1" in seen["recurrence"]


def test_the_judge_s_answer_reaches_the_payload_and_the_tally(monkeypatch, tmp_path):
    """The adjudicated half, which is the one that can see a repeated premise. A
    verdict paid for and dropped on the floor is the failure this pins — and the
    `not-said` bucket has to be counted rather than inferred, or "the judge had
    nothing to say" is only visible as a shortfall against a denominator stored
    somewhere else."""
    p1, _, _ = _panel_round(monkeypatch, tmp_path, 1,
                            [("app/sync.py", 11, "a stale mirror")], head="a" * 40)
    _, r2, _ = _panel_round(
        monkeypatch, tmp_path, 2, [("app/sync.py", 12, "written twice")],
        head="b" * 40, baseline=[p1], premise="invalidates")
    assert r2["to_fix"][0]["premise_verdict"] == "invalidates"
    assert r2["premise_counts"] == {"invalidates": 1, "separate": 0,
                                    "unclear": 0, "not-said": 0}


def test_a_measured_round_still_stops_on_the_round_cap_and_nothing_else(
        monkeypatch, tmp_path):
    """The guarantee the whole release rests on, asserted on a round where the
    measurement is loud: every new finding `revisited`, and the stop is still the
    round cap with the same veto list a round that measured nothing would carry.
    If this ever fails, #67 has started gating on n=2 evidence."""
    p1, _, _ = _panel_round(monkeypatch, tmp_path, 1,
                            [("app/sync.py", 11, "a stale mirror")], head="a" * 40)
    _, r2, _ = _panel_round(
        monkeypatch, tmp_path, 2, [("app/sync.py", 12, "written twice")],
        head="b" * 40, baseline=[p1], premise="invalidates")
    assert r2["recurrence_counts"]["revisited"] == 1
    stop = r2["round_stop"]
    for word in ("revisited", "recurrence", "premise_verdict", "recurs_of",
                 "circling"):
        assert word not in json.dumps(stop), f"the stop rule mentions `{word}`"
    # `premises` in that block is #84's DECLARED-premise brake, which is a
    # different thing and does gate: a fixer states what it is about to fix on and
    # a repeat is braked. Named here so a reader of this assertion does not take
    # its absence from the list above as an oversight — and so the two never get
    # merged on the strength of sharing a word.
    assert stop["premises"]["repeated"] == []


def test_the_report_says_what_it_measured_without_recommending_anything(
        monkeypatch, tmp_path, capsys):
    """It prints, because a number nobody reads accumulates nowhere. It does NOT
    print advice, because the replay says the mechanical rate fires on about four
    findings in five whether a cycle is circling or not — a sentence telling an
    operator to stop would be inventing a threshold out of that."""
    p1, _, _ = _panel_round(monkeypatch, tmp_path, 1,
                            [("app/sync.py", 11, "a stale mirror")], head="a" * 40)
    capsys.readouterr()
    _panel_round(monkeypatch, tmp_path, 2, [("app/sync.py", 12, "written twice")],
                 head="b" * 40, baseline=[p1], premise="invalidates")
    said = capsys.readouterr().out
    assert "on the last fix pass's own lines" in said
    assert "A position, not a verdict" in said and "Nothing stops on it" in said
    assert "contradict the premise of the fix before them" in said


def test_a_skipped_payload_answers_the_recurrence_keys():
    """`_payload_defaults` exists because the skipped PR — the case a payload is
    FOR — was the one raising KeyError. A new key joins it rather than only the
    reviewed path, or `payload['recurrence_counts']` breaks on exactly the payload
    with no findings to count."""
    d = panel._payload_defaults()
    assert d["recurrence_counts"] == {} and d["premise_counts"] == {}
