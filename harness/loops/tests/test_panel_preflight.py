"""The verdict before the round: refuse it, read a manifest, or read the diff.

A panel was launched on PR #137 — 763,375 chars, 6.4x antigravity's argv cap, on a
change where `panel.py` was split into six modules with nothing retyped — and was
killed five minutes in by a human asking "is this a crazy token count?". Every
piece needed to catch that already existed and none of them was wired to the
decision: #75 reports truncation *after* the round, the argv cap is documented as
a permanent property of the harness and gates nothing, and #41 makes only *later*
rounds cheaper.

So these tests pin three things, in the order they matter:

1. **The shape is measured, not guessed.** A move is mechanically identifiable —
   the added lines are a near-permutation of the deleted ones — and the
   measurement has to survive the things that look like it: file headers whose
   first characters are `+++`/`---`, blank lines that match every other blank
   line, and a diff of a diff.
2. **The verdict is the tool's.** Fits the cap -> read it. Over the cap and
   move-shaped -> read a manifest, because no budget makes relocated text
   reviewable. Far over the cap with no smaller honest question -> refuse. No cap
   configured anywhere -> run, unchanged, because this must never become the
   default diff budget #49 refused on evidence.
3. **A refusal is louder than a review.** `reviewed: False`, a `skip_reason`, a
   board record and a PR comment. "0 findings" and "nobody looked" render
   identically everywhere else in this harness, and every guard in it exists
   because that once cost somebody a merge.

The end-to-end half at the bottom is not decoration, for the same reason
`test_panel_provenance` says its own is not: the unit tests here would all be
green with the verdict computed and then ignored. What pins it is a run that
refuses and dispatches nobody, and a run whose seats receive a manifest and no
diff at all.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_scope  # noqa: E402
import panel_preflight as pf  # noqa: E402
from conftest import gh_stub  # noqa: E402


# ----------------------------------------------------------------------------- fixtures

def _file(path, added=(), removed=(), context=()):
    """One file's worth of unified diff, headers included.

    The headers matter as much as the bodies: `--- a/x` and `+++ b/x` begin with
    the same characters a counted line does, and a shape measured off `startswith`
    alone counts every file in the diff as one added and one removed line.
    """
    out = [f"diff --git a/{path} b/{path}",
           "index 1111111..2222222 100644",
           f"--- a/{path}", f"+++ b/{path}",
           f"@@ -1,{len(removed) + len(context)} +1,{len(added) + len(context)} @@"]
    out += [f" {c}" for c in context]
    out += [f"-{r}" for r in removed]
    out += [f"+{a}" for a in added]
    return "\n".join(out) + "\n"


#: 200 distinct lines, so a diff built from two disjoint slices of it has a move
#: ratio of exactly 0.
#:
#: 200 and not 12, and the reason is a bug these tests found rather than an
#: arbitrary size. A manifest's BODY scales with the change's shape but its brief
#: and section headers are a fixed ~1.3 KB, so on a diff barely over a small
#: ceiling the substitution hands a seat MORE text than the diff did. The verdict
#: measures for that now (see `test_a_move_whose_manifest_is_no_smaller`), and a
#: fixture under the fixed overhead would exercise only that branch — every
#: manifest assertion below would have been testing the refusal.
BODY = [f"    value_{i} = compute({i}, flag=True, retries=3)" for i in range(200)]

#: A split: every line leaves one file and arrives in two others, character for
#: character. This is PR #137's shape in miniature.
SPLIT = (_file("big.py", removed=BODY)
         + _file("part_a.py", added=BODY[:100])
         + _file("part_b.py", added=BODY[100:]))

#: The same size, none of it relocated.
FRESH = _file("new.py", added=BODY) + _file("other.py", added=BODY)

#: Budgets as `run()` builds them — bare seat name to cap, `None` for uncapped.
UNCAPPED = {"claude": None, "codex": None}

#: "every seat's CLI is on this box", injected everywhere below.
#:
#: Not optional politeness. `smallest_cap` skips a seat whose CLI is absent — an
#: uninstalled `agy` must not hold a ceiling on a round it cannot read — so a test
#: that leaves the default in place is asserting on which vendor CLIs the machine
#: happens to carry. Locally that quietly passes; on a CI runner, which has none of
#: them, every verdict here would collapse to `cap is None` and every assertion
#: about a refusal would fail for a reason having nothing to do with the code.
ALL_HERE = dict.fromkeys(("claude", "codex", "antigravity", "pi"), True).get


def _pre(diff, budgets, panel_cfg, notes, **kw):
    """`preflight` on a box that carries every seat. See :data:`ALL_HERE`."""
    kw.setdefault("installed", ALL_HERE)
    return pf.preflight(diff, budgets, panel_cfg, notes, **kw)


def _panel(**over):
    return {"github": "acme/board", "path": "/tmp/acme-board", "name": "board",
            "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
            "review_panel": over}


# ----------------------------------------------------------------------------- shape

def test_a_pure_split_is_a_move_at_ratio_one():
    """Nothing was written: every line is deleted once and added once."""
    s = pf.diff_shape(SPLIT)
    assert s.added == s.removed == s.moved == len(BODY)
    assert s.move_ratio == 1.0
    assert s.is_move()


def test_fresh_code_is_not_a_move_at_any_threshold():
    s = pf.diff_shape(FRESH)
    assert s.removed == 0
    assert s.moved == 0
    assert s.move_ratio == 0.0
    assert not s.is_move(0.0)   # not even at a threshold nothing can fail


def test_an_empty_diff_relocates_nothing():
    """`moved == 0` and `max(added, removed) == 0` is 0/0, and the guard against
    it has to answer "not a move" rather than raise inside a verdict."""
    s = pf.diff_shape("")
    assert s.move_ratio == 0.0
    assert not s.is_move(0.0)


def test_file_headers_are_not_counted_as_changed_lines():
    """`--- a/x` and `+++ b/x` start with the marker characters. Counted, they add
    one add and one remove per file — which on a many-file pure move drags the
    ratio down by exactly the file count and can push it under the threshold."""
    s = pf.diff_shape(SPLIT)
    assert s.added == len(BODY)      # not len(BODY) + 3 headers
    assert s.removed == len(BODY)


def test_a_content_line_that_looks_like_a_header_IS_counted():
    """The other half of the same rule: a diff of a diff, a Markdown rule, a
    docstring underline. Anchoring on the hunk header rather than on the leading
    characters is what tells the two apart, and getting it wrong the other way
    silently drops real lines out of the measurement."""
    diff = _file("doc.md", added=["--- a/inner.py", "+++ b/inner.py", "normal"])
    s = pf.diff_shape(diff)
    assert s.added == 3
    assert s.removed == 0


def test_blank_lines_are_excluded_from_both_sides():
    """A blank line matches every other blank line, so counting them inflates the
    ratio in exact proportion to how airy the code is — a diff that moves nothing
    but reflows whitespace would read as a move."""
    diff = _file("a.py", removed=["", "   ", "kept"], added=["", "   ", "kept"])
    s = pf.diff_shape(diff)
    assert s.added == s.removed == s.moved == 1
    assert s.move_ratio == 1.0


def test_the_ratio_is_measured_against_the_LARGER_side():
    """100 relocated lines plus 50 newly written ones is 0.67 here and 0.8 by
    `2*moved/(added+removed)`. The 50 new lines are exactly what a content review
    is for, so the measure slower to call it a move is the right one."""
    moved = [f"line {i}" for i in range(100)]
    new = [f"brand new {i}" for i in range(50)]
    diff = _file("from.py", removed=moved) + _file("to.py", added=moved + new)
    s = pf.diff_shape(diff)
    assert (s.added, s.removed, s.moved) == (150, 100, 100)
    assert s.move_ratio == pytest.approx(100 / 150)
    assert not s.is_move(0.9)


def test_a_repeated_line_contributes_only_as_often_as_it_appears_on_BOTH_sides():
    """Multiset intersection, not set intersection. `pass` deleted twice and added
    five times relocated twice; a set would call all five relocated and a large
    refactor of repetitive code would read as a pure move."""
    diff = _file("a.py", removed=["pass", "pass"], added=["pass"] * 5)
    s = pf.diff_shape(diff)
    assert (s.added, s.removed, s.moved) == (5, 2, 2)


def test_the_one_sided_files_name_a_split_source_and_its_destinations():
    s = pf.diff_shape(SPLIT)
    assert s.files == 3
    assert s.files_removed_only == ("big.py",)
    assert set(s.files_added_only) == {"part_a.py", "part_b.py"}


def test_an_unparseable_header_keeps_its_lines_rather_than_dropping_them():
    """A header this cannot key is keyed by its own text, so its hunk bodies still
    reach the counters. Dropping them would understate a diff's size, and size is
    what the refusal is measured on."""
    diff = "diff --git a/x b/y\n@@ -1 +1 @@\n-gone\n+here\n"
    s = pf.diff_shape(diff)
    assert (s.added, s.removed) == (1, 1)
    assert s.files == 1


# ----------------------------------------------------------------------------- the cap

def test_no_configured_cap_means_there_is_nothing_to_refuse_against():
    """The answer that keeps this from becoming the default diff budget. A repo
    running claude and codex off stdin with no `max_diff_chars` has declared no
    ceiling, so no size this file invented is ever applied to its diffs."""
    assert pf.smallest_cap(UNCAPPED, ALL_HERE) == (None, "")


def test_antigravity_is_capped_by_the_kernel_whether_or_not_a_repo_says_so():
    """That seat's prompt travels in argv, so `MAX_ARG_STRLEN` applies to it
    without anybody configuring anything. It is the only cap PR #137's repo had,
    and therefore the only reason the case is catchable at all."""
    cap, seat = pf.smallest_cap({"claude": None, "antigravity": None}, ALL_HERE)
    assert (cap, seat) == (panel_core.ARGV_PROMPT_MAX_BYTES, "antigravity")


def test_a_smaller_configured_budget_beats_the_kernel_and_a_bigger_one_does_not():
    small = panel_core.ARGV_PROMPT_MAX_BYTES // 2
    assert pf.smallest_cap({"antigravity": small}, ALL_HERE) == (small, "antigravity")
    big = panel_core.ARGV_PROMPT_MAX_BYTES * 4
    assert pf.smallest_cap({"antigravity": big}, ALL_HERE)[0] == panel_core.ARGV_PROMPT_MAX_BYTES


def test_a_seat_whose_CLI_is_ABSENT_declares_no_ceiling_here():
    """The headless-host regression, and the one this whole feature could most
    easily have caused. `budgets` holds every CONFIGURED seat, not every runnable
    one, and `agy` is a workstation package — this repo's own rules enable a seat
    that records "antigravity: CLI absent" and never runs on a headless box.
    Counting its argv ceiling would refuse a round on behalf of a reviewer that was
    never going to read anything, on exactly the unattended hosts where nobody is
    watching to pass `--force`, while the seats that DID run read off stdin with no
    cap at all."""
    budgets = {"claude": None, "antigravity": None}
    no_agy = {"claude": True, "antigravity": False}.get
    assert pf.smallest_cap(budgets, no_agy) == (None, "")
    # The same round, on a box that HAS it: the ceiling is real and applies.
    assert pf.smallest_cap(budgets, ALL_HERE)[1] == "antigravity"
    # End to end: the verdict follows.
    big = FRESH * 40
    assert _pre(big, budgets, {}, [], installed=no_agy).verdict == "run"
    assert _pre(big, budgets, {}, [], installed=ALL_HERE).refused


def test_a_box_carrying_no_seat_at_all_refuses_nothing():
    """The floor under the exemption above. Every seat absent means no ceiling,
    which means `run` — and the round then produces "no reviewer ran — nothing read
    this diff" through `coverage_veto`, which is the existing and correct answer.
    A refusal there would be the panel declining a round on behalf of seats that do
    not exist."""
    def none_here(name):
        return False

    assert pf.smallest_cap({"claude": 10, "antigravity": 10}, none_here) == (None, "")
    assert _pre(FRESH * 40, {"claude": 10}, {}, [], installed=none_here).verdict == "run"


def test_the_host_predicate_is_resolved_in_the_BODY_so_it_can_be_replaced(monkeypatch):
    """A default argument binds the function object at `def` time, so
    `installed=seat_installed` in the signature made
    `monkeypatch.setattr(panel_preflight, "seat_installed", ...)` a no-op — and
    every end-to-end test here went on reading the real PATH while appearing to
    pin it. That is the shape of failure a CI runner finds and a workstation does
    not: ten tests here passed locally and failed with the vendor CLIs hidden."""
    monkeypatch.setattr(pf, "seat_installed", lambda name: False)
    assert pf.smallest_cap({"antigravity": None}) == (None, "")
    assert pf.preflight(FRESH * 40, {"antigravity": None}, {}, []).verdict == "run"


def test_seat_installed_asks_about_the_COMMAND_not_the_seat_name(monkeypatch):
    """The reviewer is `antigravity`; the command is `agy`. Asking PATH for
    "antigravity" would report the one seat that IS argv-bound as absent on every
    box, which is the direction that quietly switches the ceiling off."""
    asked = []
    monkeypatch.setattr(pf.shutil, "which", lambda c: asked.append(c) or "/x/bin/agy")
    assert pf.seat_installed("antigravity") is True
    assert asked == ["agy"]
    assert pf.seat_installed("claude") is True
    assert asked[-1] == "claude"


def test_the_tightest_seat_holds_the_floor_and_ties_break_by_name():
    """Deterministically, because the seat's name goes in the refusal's reason and
    a reason that names a different seat on two runs of the same round is a reason
    nobody can check."""
    assert pf.smallest_cap({"claude": 50, "codex": 10, "pi": 90}, ALL_HERE) == (10, "codex")
    for _ in range(5):
        assert pf.smallest_cap({"pi": 10, "codex": 10}, ALL_HERE)[1] == "codex"


# ----------------------------------------------------------------------------- verdict

def test_an_uncapped_panel_runs_however_large_the_diff():
    got = _pre(SPLIT * 200, UNCAPPED, {}, [])
    assert got.verdict == "run"
    assert got.reason == ""
    assert got.cap is None


def test_a_diff_that_fits_every_cap_is_read_as_a_diff_even_when_it_is_a_move():
    """A small move costs nothing to read as content and reading it tells you
    strictly more than a manifest of it does. The manifest is what you do when the
    diff will not fit, not what you do to moves."""
    got = _pre(SPLIT, {"claude": len(SPLIT) + 1}, {}, [])
    assert got.verdict == "run"
    assert got.shape.is_move()      # it IS a move; it just fits


def test_a_move_over_the_cap_is_read_as_a_manifest():
    """PR #137's case. No budget makes relocated text reviewable, so the answer is
    a different question rather than a smaller slice of the same one."""
    got = _pre(SPLIT, {"claude": len(SPLIT) // 4}, {}, [])
    assert got.verdict == "manifest"
    assert "move-shaped" in got.reason
    assert "MANIFEST" in got.reason


def test_a_move_gets_the_manifest_at_ANY_multiple_over_the_cap():
    """Deliberately not gated on the refusal threshold. A seat spending its budget
    re-reading relocated code is producing findings about the base branch whether
    it was cut at 90% or at 16%."""
    got = _pre(SPLIT, {"claude": len(SPLIT) - 1}, {}, [])
    assert got.over < 1.01
    assert got.verdict == "manifest"


def test_a_move_whose_manifest_is_no_smaller_is_not_substituted():
    """A manifest that is not SMALLER than the diff is not a saving, it is a second
    copy of the problem — and then a truncated one, so the seat reads a prefix of a
    manifest instead of a prefix of a diff. The manifest's body scales with the
    change's shape while its brief is a fixed kilobyte, so this is reachable on any
    small move over a small ceiling. Measured, because "the manifest is always
    smaller" is the kind of claim that is true of every case anyone tested."""
    tiny = [f"    x{i} = {i}" for i in range(6)]
    diff = _file("from.py", removed=tiny) + _file("to.py", added=tiny)
    assert pf.diff_shape(diff).is_move()
    assert len(pf.move_manifest(diff)) > len(diff)
    got = _pre(diff, {"claude": len(diff) // 10}, {}, [])
    assert got.refused
    assert "it IS move-shaped" in got.reason
    assert "replace the problem with a copy of it" in got.reason


def test_the_manifest_a_verdict_chose_is_the_one_it_measured():
    """Carried on the verdict rather than rebuilt by the caller. Two builds are two
    texts that have to agree about which one was weighed, and the weighing is the
    only thing standing between a manifest round and the case above."""
    got = _pre(SPLIT, {"claude": len(SPLIT) // 4}, {}, [])
    assert got.verdict == "manifest"
    assert got.manifest == pf.move_manifest(SPLIT)
    assert len(got.manifest) < got.shape.chars


def test_a_run_verdict_carries_no_manifest():
    """`manifest` is set on a manifest verdict and nowhere else, so a caller cannot
    substitute one on a round that was never ruled move-shaped."""
    assert _pre(FRESH, UNCAPPED, {}, []).manifest == ""
    assert _pre(FRESH, {"claude": len(FRESH) // 10}, {}, []).manifest == ""


def test_a_large_content_diff_is_refused_and_says_why():
    got = _pre(FRESH, {"claude": len(FRESH) // 10}, {}, [])
    assert got.refused
    assert "refusal threshold" in got.reason
    assert "not move-shaped" in got.reason
    # The remedies, in the reason itself: a refusal a reader cannot act on is an
    # advisory, and an advisory a human has to notice is what failed on #137.
    assert "Split the PR" in got.reason
    assert "--force" in got.reason


def test_over_the_cap_but_under_the_multiple_is_still_an_ordinary_truncated_round():
    """The behaviour every release before this one had, kept on purpose. Over the
    cap is truncation and has been reported as such since #75; this feature is for
    the case where truncation has stopped being a caveat and become the review."""
    got = _pre(FRESH, {"claude": int(len(FRESH) / 2)}, {}, [])
    assert 1 < got.over <= 3
    assert got.verdict == "run"


def test_the_refusal_threshold_is_configurable_and_zero_switches_it_off():
    tiny = {"claude": len(FRESH) // 10}
    assert _pre(FRESH, tiny, {"refuse_over_cap_multiple": 0}, []).verdict == "run"
    assert _pre(FRESH, tiny, {"refuse_over_cap_multiple": 50}, []).verdict == "run"
    assert _pre(FRESH, tiny, {"refuse_over_cap_multiple": 2}, []).refused


def test_turning_the_manifest_off_falls_back_to_the_refusal():
    """Strictly less useful, and available because it is a switch a repo may want
    before it trusts the shape detection. It must not fall back to reviewing the
    move as content — that is the outcome the whole issue is about."""
    got = _pre(SPLIT, {"claude": len(SPLIT) // 10},
                       {"manifest_moves": False}, [])
    assert got.refused


def test_the_move_ratio_threshold_is_configurable():
    moved = [f"line {i}" for i in range(100)]
    new = [f"brand new {i}" for i in range(50)]
    diff = _file("from.py", removed=moved) + _file("to.py", added=moved + new)
    budgets = {"claude": len(diff) // 10}
    assert _pre(diff, budgets, {}, []).refused            # 0.67 < 0.9
    assert _pre(diff, budgets,
                        {"move_shape_ratio": 0.5}, []).verdict == "manifest"


def test_force_overrides_a_refusal_and_records_what_it_overrode():
    """This repo's standing rule is that the tool chooses the action and a caller
    that overrides the choice silently is the bug. So the flag cannot erase the
    verdict — `would_have` is what makes the override auditable after the fact."""
    got = _pre(FRESH, {"claude": len(FRESH) // 10}, {}, [], forced=True)
    assert got.verdict == "run"
    assert got.forced is True
    assert got.would_have == "refuse"
    assert got.reason.startswith("--force: ")


def test_force_overrides_a_manifest_too():
    got = _pre(SPLIT, {"claude": len(SPLIT) // 10}, {}, [], forced=True)
    assert (got.verdict, got.would_have, got.forced) == ("run", "manifest", True)


def test_force_leaves_a_run_verdict_unmarked():
    """`forced` must mean "an override happened", not "the flag was passed". A run
    that would have run anyway was not overridden, and recording it as one would
    put a caveat on the report of every forced round that needed no forcing."""
    got = _pre(SPLIT, UNCAPPED, {}, [], forced=True)
    assert (got.verdict, got.forced, got.would_have) == ("run", False, "")


@pytest.mark.parametrize("junk", ["lots", None, True, [3], {"n": 3}, "3.x"])
def test_a_setting_that_cannot_be_a_number_falls_back_and_SAYS_so(junk):
    """The manners `diff_budget` established: silently honouring junk reviews on a
    threshold nobody set, and silently dropping it leaves you believing a
    threshold you never got. `None` and "" are the documented way to say "use the
    default" and are silent — see the next test."""
    notes = []
    got = _pre(FRESH, {"claude": len(FRESH) // 10},
                       {"refuse_over_cap_multiple": junk}, notes)
    assert got.refused                     # fell back to the default, which refuses
    if junk is None:
        assert notes == []
    else:
        assert any("refuse_over_cap_multiple" in n for n in notes)


def test_an_unset_setting_is_silent():
    """Absent and null mean "use the default" and are not mistakes. A note for
    every round on every repo that configured nothing is a note nobody reads."""
    notes = []
    _pre(FRESH, UNCAPPED, {"move_shape_ratio": None,
                                   "refuse_over_cap_multiple": ""}, notes)
    assert notes == []


def test_a_FRACTIONAL_threshold_is_honoured_as_written():
    """It was coerced through the DEFAULT's type — an int — so `2.9` became `2` and
    refused a third earlier than the number in the file. A threshold is a real
    number, and both of these are only ever compared and formatted."""
    notes = []
    budgets = {"claude": len(FRESH) // 3}          # about 3x over
    over = _pre(FRESH, budgets, {}, notes).over
    assert 2.9 < over < 3.1, f"fixture drifted: {over}"
    assert _pre(FRESH, budgets, {"refuse_over_cap_multiple": 2.5},
                        notes).refused
    assert not _pre(FRESH, budgets, {"refuse_over_cap_multiple": 3.5},
                            notes).refused
    assert notes == []
    # And it survives the round trip a `.harness-rules` value takes, where JSON
    # gives a float and a hand-written value can arrive as a string.
    assert _pre(FRESH, budgets, {"refuse_over_cap_multiple": "2.5"},
                        notes).refused


def test_a_negative_threshold_falls_back_rather_than_inverting_the_test():
    notes = []
    got = _pre(FRESH, {"claude": len(FRESH) // 10},
                       {"refuse_over_cap_multiple": -2}, notes)
    assert got.refused
    assert any("cannot be negative" in n for n in notes)


def test_the_verdict_serialises_the_measurement_it_was_made_from():
    """A verdict a consumer cannot check is a verdict nobody argues with, and the
    board is the consumer that has to hold it for six weeks."""
    got = _pre(SPLIT, {"antigravity": 100}, {}, [])
    d = got.as_dict()
    assert d["verdict"] == "manifest"
    assert d["cap"] == 100 and d["cap_seat"] == "antigravity"
    assert d["shape"]["moved"] == len(BODY)
    assert d["shape"]["move_ratio"] == 1.0
    assert d["thresholds"]["move_shape_ratio"] == pf.DEFAULT_MOVE_SHAPE_RATIO
    assert json.dumps(d)          # it has to survive the payload it rides in


def test_the_refusal_notice_will_not_render_a_verdict_that_is_not_a_refusal():
    """Handed a `run` verdict it would print "**Why:** ." over a measurement table
    and a list of remedies — a document that reads exactly like a refusal, names no
    reason, and would be posted to the PR. Not hypothetical: it is what the first
    hand-run of `refusal_report` produced. A caller with the wrong verdict has a
    bug, and the bug has to surface here rather than on somebody's PR."""
    ran = _pre(FRESH, UNCAPPED, {}, [])
    assert ran.verdict == "run"
    with pytest.raises(AssertionError, match="only a refusal has a reason"):
        pf.refusal_report("board", 137, "a title", "main", ran)


def test_the_refusal_notice_names_the_measurement_and_the_remedies():
    got = _pre(FRESH, {"claude": len(FRESH) // 10}, {}, [])
    text = pf.refusal_report("board", 137, "refactor: the world", "main", got)
    assert "REFUSED" in text
    assert "not a clean review" in text
    assert "board#137" in text and "refactor: the world" in text
    assert f"{got.shape.chars:,} chars" in text
    assert "tightest seat ceiling" in text
    # Every remedy on one line each: the source wraps and the rendered text must
    # not, or a reader gets a run of spaces mid-sentence.
    for line in text.splitlines():
        assert "  " not in line.strip(), line


# ----------------------------------------------------------------------------- manifest

def test_the_manifest_says_what_moved_where():
    text = pf.move_manifest(SPLIT)
    assert "WHAT MOVED WHERE" in text
    assert f"big.py: +0 / -{len(BODY):,}  [lost only]" in text
    assert "part_a.py: +100 / -0  [gained only]" in text


def test_the_manifest_names_a_line_that_did_not_survive():
    """The failure a move review exists to catch: a guard clause, an `except` arm,
    a decorator dropped on the way across. It is invisible in a content review of
    the destination and it is one line of the manifest."""
    lost = "        if handle is None: return"
    diff = (_file("from.py", removed=BODY + [lost])
            + _file("to.py", added=BODY))
    text = pf.move_manifest(diff)
    assert "WHAT DID NOT SURVIVE" in text
    assert lost.strip() in text


def test_the_manifest_names_what_changed_besides_moving():
    """The only genuinely new code in the change, and therefore the only place a
    content review belongs. A move that quietly rewrites logic while nobody is
    reading is what a manifest is most likely to miss, so it gets its own
    section."""
    smuggled = "    if user.is_admin: bypass_checks()"
    diff = (_file("from.py", removed=BODY)
            + _file("to.py", added=BODY + [smuggled]))
    text = pf.move_manifest(diff)
    assert "WHAT CHANGED BESIDES MOVING" in text
    assert smuggled.strip() in text


def test_a_pure_move_says_so_in_both_residue_sections():
    text = pf.move_manifest(SPLIT)
    assert "every deleted line reappears somewhere" in text
    assert "this is a pure move" in text


def test_a_definition_added_in_two_places_is_flagged():
    """#62's trap. A merge that keeps both copies of a moved function is a clean
    merge, a green test run and a silent bug: the later binding wins and the dead
    one is the one anybody reading the old file will find."""
    diff = (_file("from.py", removed=["def review_llm(name):", "    pass"])
            + _file("a.py", added=["def review_llm(name):", "    pass"])
            + _file("b.py", added=["def review_llm(name):", "    pass"]))
    text = pf.move_manifest(diff)
    assert "! review_llm" in text
    assert "the later binding wins" in text


def test_no_duplicate_says_which_languages_it_actually_looked_at():
    """An empty section reads as "checked, and clean". The check covers Python and
    JS/TS spellings and nothing else, so a Go move must be told the section did
    not apply rather than left with a false all-clear."""
    text = pf.move_manifest(SPLIT)
    assert "Python" in text and "JavaScript/TypeScript" in text
    assert "NOT covered by this check" in text


@pytest.mark.parametrize("line,name", [
    ("def handle(self, x):", "handle"),
    ("    async def fetch(url):", "fetch"),
    ("class Widget(Base):", "Widget"),
    ("class Bare:", "Bare"),
    ("export function render(node) {", "render"),
    ("async function poll() {", "poll"),
])
def test_the_definition_shapes_it_recognises(line, name):
    from collections import Counter
    assert list(pf.duplicate_definitions(Counter({line: 2}))) == [name]


def test_a_definition_added_once_is_not_flagged():
    from collections import Counter
    assert pf.duplicate_definitions(Counter({"def only_here(x):": 1})) == {}


def test_the_manifest_states_the_two_facts_it_cannot_measure():
    """Test counts before and after, and whether a module now reaches backward
    into another, are the other evidence that bears on a move — and both need the
    branch checked out, which the panel never has. Claiming them from a diff would
    be inventing the two facts a reader would most want to rely on."""
    text = pf.move_manifest(SPLIT)
    assert "WHAT IS NOT HERE" in text
    assert "Test counts" in text
    assert "Do not assume either is fine" in text


def test_the_residue_listing_is_capped_and_says_how_much_it_dropped():
    """A move at the 0.9 threshold can still leave 10% of a 763KB diff as residue,
    which is 76KB and back where we started. An elided tail has to be a number a
    reader can act on rather than an absence."""
    many = [f"unique orphan line number {i}" for i in range(pf.MANIFEST_RESIDUE_LINES + 40)]
    diff = _file("from.py", removed=BODY + many) + _file("to.py", added=BODY)
    text = pf.move_manifest(diff)
    assert "and 40 more, not listed" in text


def test_a_long_residue_line_is_quoted_not_reproduced():
    long = "x = " + "y" * 400
    diff = _file("from.py", removed=BODY + [long]) + _file("to.py", added=BODY)
    text = pf.move_manifest(diff)
    assert long not in text
    assert long[:pf.MANIFEST_LINE_CHARS] in text


def test_the_manifest_is_the_same_text_twice():
    """A listing that reorders between two runs of the same round is a manifest
    nobody can compare — and the panel/fix cycle compares rounds for a living."""
    orphans = [f"orphan {i}" for i in range(30)]
    diff = _file("from.py", removed=BODY + orphans) + _file("to.py", added=BODY)
    assert pf.move_manifest(diff) == pf.move_manifest(diff)


def test_the_manifest_is_orders_of_magnitude_smaller_than_the_diff_it_replaces():
    """The claim the whole feature rests on: the manifest's size is a function of
    the change's SHAPE, not of the diff's length. A split ten times as long
    produces a manifest of about the same size."""
    big = SPLIT * 200
    assert len(pf.move_manifest(big)) < len(big) / 20


# ----------------------------------------------------------------------------- run()

def _run(monkeypatch, tmp_path, diff, panel_cfg, *, force=False, post=False,
         seats=("claude",), record=False):
    """One `run()` with every subprocess replaced, returning
    ``(exit_code, payload, seen)``.

    `seen` collects what each seat was actually handed, which is the only thing
    that can tell a manifest round from a diff round: the verdict in the payload
    is the panel's own account of itself, and this feature exists because such an
    account was previously written after the money was spent.
    """
    cfg = {**panel_cfg,
           "reviewers": {n: {"enabled": True, "model": "sonnet"} for n in seats}}
    seen: dict = {"prompts": {}, "posted": [], "recorded": []}

    def fake_review(name, model, prompt, effort=""):
        seen["prompts"][name] = prompt
        return panel.ReviewerRun([], None, 10, None)

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    # See ALL_HERE: without this the verdict depends on whether the machine
    # running the suite happens to carry the vendor CLIs, and a CI runner carries
    # none of them.
    monkeypatch.setattr(pf, "seat_installed", ALL_HERE)
    monkeypatch.setattr(panel_core, "sh", gh_stub(diff=diff))
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, ""))
    monkeypatch.setattr(panel, "record_run",
                        lambda p: seen["recorded"].append(p))
    monkeypatch.setattr(panel, "post_summary",
                        lambda repo, n, report: seen["posted"].append(report) or True)
    out = tmp_path / "run.json"
    code = panel.run("e2e", 137, post=post, json_file=str(out), record=record,
                     force=force)
    return code, json.loads(out.read_text()), seen


def test_a_refused_round_dispatches_nobody(monkeypatch, tmp_path, capsys):
    """The whole point. Four seats at full effort against a diff nothing could
    usefully read is what this was filed about, and the assertion that matters is
    that no seat was asked."""
    code, got, seen = _run(monkeypatch, tmp_path, FRESH,
                           _panel(max_diff_chars=len(FRESH) // 10))
    assert code == 0
    assert seen["prompts"] == {}
    assert got["reviewed"] is False
    assert got["preflight"]["verdict"] == "refuse"
    assert "refusal threshold" in got["skip_reason"]
    assert "REFUSED" in capsys.readouterr().out


def test_a_refused_round_cannot_be_read_as_a_clean_one(monkeypatch, tmp_path):
    """A panel that quietly declines is #62's disease in a new place — a merge gate
    trusting a proxy. `reviewed`, `skip_reason` and an empty finding list have to
    disagree with each other loudly enough that no consumer can take the third for
    a verdict."""
    _, got, _ = _run(monkeypatch, tmp_path, FRESH,
                     _panel(max_diff_chars=len(FRESH) // 10))
    assert got["reviewed"] is False
    assert got["skip_reason"]
    assert got["to_fix"] == [] and got["dismissed"] == []
    assert got["reviewers_ran"] == []
    # And the size that caused it, so the refusal can be checked rather than
    # believed. `diff_chars` stays 0 because nothing was reviewed.
    assert got["diff_chars"] == 0
    assert got["preflight"]["shape"]["chars"] == len(FRESH)


def test_a_refusal_IS_recorded_on_the_board(monkeypatch, tmp_path):
    """Unlike the title-pattern skip, and that difference is the design rather
    than an inconsistency: a title skip says this PR was never worth a panel, a
    refusal says a panel was wanted and this diff defeated it. The second is the
    observation the board exists to accumulate."""
    _, _, seen = _run(monkeypatch, tmp_path, FRESH,
                      _panel(max_diff_chars=len(FRESH) // 10), record=True)
    assert len(seen["recorded"]) == 1
    assert seen["recorded"][0]["preflight"]["verdict"] == "refuse"


def test_a_refusal_is_posted_to_the_PR(monkeypatch, tmp_path):
    """Posting is most of what makes it loud. The terminal copy is read by whoever
    is watching, and under the epic (#52) nobody is."""
    _, _, seen = _run(monkeypatch, tmp_path, FRESH,
                      _panel(max_diff_chars=len(FRESH) // 10), post=True)
    assert len(seen["posted"]) == 1
    assert "REFUSED" in seen["posted"][0]
    assert "not a clean review" in seen["posted"][0]


def test_a_refused_round_still_records_the_commit_it_did_not_review(monkeypatch,
                                                                   tmp_path):
    """A refused round still moved the head, and round r+1 has to anchor its
    increment and its fix range somewhere. Left null, a refusal anywhere in a
    cycle loses the anchor for every round after it."""
    _, got, _ = _run(monkeypatch, tmp_path, FRESH,
                     _panel(max_diff_chars=len(FRESH) // 10))
    assert got["head_sha"]
    assert got["merge_base"]


def test_a_manifest_round_hands_the_seats_a_manifest_and_no_diff(monkeypatch,
                                                                tmp_path):
    """The substitution, end to end. A seat that receives the diff here is a seat
    spending its budget re-reading code that is already in the base branch."""
    _, got, seen = _run(monkeypatch, tmp_path, SPLIT,
                        _panel(max_diff_chars=len(SPLIT) // 4))
    assert got["reviewed"] is True
    assert got["preflight"]["verdict"] == "manifest"
    prompt = seen["prompts"]["claude"]
    assert pf.MOVE_MANIFEST_HEADER in prompt
    assert "WHAT DID NOT SURVIVE" in prompt
    assert "You are reviewing a MOVE" in prompt
    # The moved code itself is not in there. `BODY[0]` is one of the relocated
    # lines and appears twice in the diff and nowhere in the manifest.
    assert BODY[0] not in prompt


def test_a_manifest_round_does_not_send_the_review_brief(monkeypatch, tmp_path):
    """Two prompts, and a seat given the diff brief over manifest material would
    review file names for correctness — the exact fabrication the manifest
    replaces."""
    _, _, seen = _run(monkeypatch, tmp_path, SPLIT,
                      _panel(max_diff_chars=len(SPLIT) // 4))
    prompt = seen["prompts"]["claude"]
    assert "Do NOT report: relocated code" in prompt
    assert "--- DIFF ---" not in prompt


def test_a_manifest_round_says_so_above_its_findings(monkeypatch, tmp_path, capsys):
    """A reader who takes a manifest round's findings for a content review reads
    "no correctness findings" as "the moved code is correct". Nobody read the moved
    code."""
    _run(monkeypatch, tmp_path, SPLIT, _panel(max_diff_chars=len(SPLIT) // 4))
    out = capsys.readouterr().out
    assert "reviewed as a MOVE MANIFEST" in out
    assert "was not read by anybody" in out


def test_a_manifest_round_measures_the_manifest_it_actually_sent(monkeypatch,
                                                                tmp_path):
    """`diff_chars` is "how big was the thing we reviewed", and under a manifest
    round the thing we reviewed is the manifest. The PR's own size is in the
    pre-flight block, where it is a measurement rather than a claim about
    coverage."""
    _, got, _ = _run(monkeypatch, tmp_path, SPLIT,
                     _panel(max_diff_chars=len(SPLIT) // 4))
    assert got["diff_chars"] < len(SPLIT)
    assert got["preflight"]["shape"]["chars"] == len(SPLIT)
    assert got["diff_truncated"] is False      # the manifest fitted


def test_force_reviews_the_diff_and_says_it_was_overruled(monkeypatch, tmp_path,
                                                          capsys):
    """An override is a decision. It cannot look like the tool having decided to
    run, on the report or in the record."""
    _, got, seen = _run(monkeypatch, tmp_path, FRESH,
                        _panel(max_diff_chars=len(FRESH) // 10), force=True)
    assert got["reviewed"] is True
    assert seen["prompts"]["claude"]
    assert got["preflight"]["forced"] is True
    assert got["preflight"]["would_have"] == "refuse"
    assert "`--force` overrode a pre-flight `refuse` verdict" in capsys.readouterr().out


def test_the_verdict_is_not_written_into_config_notes_as_well(monkeypatch, tmp_path):
    """It rides in `preflight`, which reaches the board, and in a warning above the
    findings, which is a better place for it than a "⚠️ config:" line. Written into
    both — as it first was — one report carried the same three sentences twice.
    `config_notes` still gets a bad THRESHOLD, like any other bad config value."""
    _, forced, _ = _run(monkeypatch, tmp_path, FRESH,
                        _panel(max_diff_chars=len(FRESH) // 10), force=True)
    assert not any("--force overrode" in n for n in forced["config_notes"])
    _, mani, _ = _run(monkeypatch, tmp_path, SPLIT,
                      _panel(max_diff_chars=len(SPLIT) // 4))
    assert not any("move-shaped" in n for n in mani["config_notes"])
    _, junk, _ = _run(monkeypatch, tmp_path, FRESH,
                      _panel(move_shape_ratio="nope"))
    assert any("move_shape_ratio" in n for n in junk["config_notes"])


def test_a_manifest_round_vetoes_a_confident_stop(monkeypatch, tmp_path):
    """The least trustworthy quiet the panel produces, and the one nothing else
    catches: every other coverage veto keys off a seat being short of what it was
    SENT, and a manifest round's seats got the whole manifest. Without this the
    cycle can stop `confident: True` having had nobody read a line of the moved
    code."""
    cfg = _panel(max_diff_chars=len(SPLIT) // 4)
    monkeypatch.setattr(panel, "load_repo_cfg",
                        lambda name: {**cfg, "reviewers": {
                            "claude": {"enabled": True, "model": "sonnet"}}})
    monkeypatch.setattr(pf, "seat_installed", ALL_HERE)
    monkeypatch.setattr(panel_core, "sh", gh_stub(diff=SPLIT))
    monkeypatch.setattr(panel, "review_llm",
                        lambda n, m, p, effort="": panel.ReviewerRun([], None, 10, None))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, ""))
    out = tmp_path / "veto.json"
    # `--max-rounds` is what says this run is part of a cycle, which is what makes
    # `round_stop` a verdict rather than a field nobody reads.
    assert panel.run("e2e", 137, post=False, json_file=str(out), record=False,
                     max_rounds=2) == 0
    got = json.loads(out.read_text())
    assert got["preflight"]["verdict"] == "manifest"
    assert got["reviewers"]["claude"]["truncated"] is False   # the manifest FITTED
    assert any("read a MANIFEST of a move" in v for v in got["round_stop"]["veto"])
    assert got["round_stop"]["confident"] is False


def test_a_forced_round_vetoes_through_the_ORDINARY_truncation_path(monkeypatch,
                                                                   tmp_path):
    """No second mechanism for it, deliberately. A forced round reviews a diff far
    over the ceiling, so its seats are cut and `coverage_veto` already says so with
    the numbers — a bespoke "you forced this" veto would be the same fact twice."""
    cfg = _panel(max_diff_chars=len(FRESH) // 10)
    monkeypatch.setattr(panel, "load_repo_cfg",
                        lambda name: {**cfg, "reviewers": {
                            "claude": {"enabled": True, "model": "sonnet"}}})
    monkeypatch.setattr(pf, "seat_installed", ALL_HERE)
    monkeypatch.setattr(panel_core, "sh", gh_stub(diff=FRESH))
    monkeypatch.setattr(panel, "review_llm",
                        lambda n, m, p, effort="": panel.ReviewerRun([], None, 10, None))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, ""))
    out = tmp_path / "forced.json"
    assert panel.run("e2e", 137, post=False, json_file=str(out), record=False,
                     max_rounds=2, force=True) == 0
    got = json.loads(out.read_text())
    assert got["reviewers"]["claude"]["truncated"] is True
    assert any("diff chars" in v for v in got["round_stop"]["veto"])
    assert got["round_stop"]["confident"] is False


def test_a_reviewed_round_records_the_verdict_it_passed(monkeypatch, tmp_path):
    """On every run, not only on a refused one. "The panel weighed this and
    proceeded" and "the panel never weighed it" are otherwise the same silence,
    and counting the refusals needs the denominator."""
    _, got, _ = _run(monkeypatch, tmp_path, FRESH, _panel())
    assert got["reviewed"] is True
    assert got["preflight"]["verdict"] == "run"
    assert got["preflight"]["cap"] is None
    assert got["preflight"]["forced"] is False


def test_an_uncapped_repo_behaves_exactly_as_it_did_before(monkeypatch, tmp_path):
    """The claim that this is not the default diff budget #49 refused. A repo that
    configured no ceiling and enables no argv-bound seat gets its whole diff, at
    any size."""
    big = FRESH * 50
    _, got, seen = _run(monkeypatch, tmp_path, big, _panel())
    assert got["preflight"]["verdict"] == "run"
    assert big in seen["prompts"]["claude"]


def test_a_title_skip_never_reaches_the_verdict(monkeypatch, tmp_path):
    """`preflight: None` and a `run` verdict are different statements, and the
    difference is what answers "was this PR ever weighed?". The skip path returns
    before the diff is even fetched."""
    assert panel._payload_defaults()["preflight"] is None


# ------------------------------------------------- what the verdict is measured ON

def _payload(tmp_path, name, **kw):
    """An earlier round's `--json-file`, as `load_baseline` reads it."""
    body = {"repo": "board", "github": "acme/board", "pr": 137, "round": 1,
            "cycle": "cyc", "head_sha": "a" * 40, "reviewers_ran": ["claude"],
            "scope": "pr", "to_fix": [], "dismissed": [], "sonar_findings": [],
            "reviewers": {"claude": {"ran": True, "truncated": False}}, **kw}
    path = tmp_path / name
    path.write_text(json.dumps(body))
    return str(path)


def test_a_scoped_round_is_weighed_on_its_INCREMENT_not_on_the_PR(monkeypatch,
                                                                 tmp_path):
    """Both questions the verdict asks are about the thing being REVIEWED: would a
    seat read a useless fraction of it, and is IT a move. Measured on the PR
    instead, a round 2 whose material is a small fix commit gets refused — or handed
    a manifest — because of a size that round was never going to send. A round 2 fix
    commit is neither large nor move-shaped just because the PR it lands in is."""
    increment = _file("fix.py", added=["    the_fix()"])
    pr = FRESH + increment
    cfg = {"github": "acme/board", "path": "/tmp/b", "name": "board",
           "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
           # A ceiling the PR is far past and the increment is nowhere near.
           "review_panel": {"max_diff_chars": len(FRESH) // 10}}
    seen: dict = {}
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(pf, "seat_installed", ALL_HERE)
    monkeypatch.setattr(panel_core, "sh", gh_stub(diff=pr))
    monkeypatch.setattr(panel_scope, "fetch_increment",
                        lambda repo, a, b: (increment, ""))
    monkeypatch.setattr(panel_scope, "compare_facts",
                        lambda *a: {"commits": 1, "files": 1, "additions": 1,
                                    "deletions": 0})
    def reviewer(name, model, prompt, effort=""):
        seen["prompt"] = prompt
        return panel.ReviewerRun([], None, 10, None)

    monkeypatch.setattr(panel, "review_llm", reviewer)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, ""))
    out = tmp_path / "r2.json"
    assert panel.run("e2e", 137, post=False, json_file=str(out), record=False,
                     round_no=2, baseline=[_payload(tmp_path, "r1.json")],
                     max_rounds=3) == 0
    got = json.loads(out.read_text())
    assert got["scope"] == "increment"
    assert got["reviewed"] is True
    assert got["preflight"]["verdict"] == "run"
    # The measurement is the increment's, not the PR's — the same scope-dependence
    # `diff_chars` has, and the reason `scope` has to be read beside both.
    assert got["preflight"]["shape"]["chars"] == len(increment)


# ------------------------------------------------- a manifest round as a baseline

def test_a_manifest_round_does_not_count_as_having_RE_READ_the_PR(tmp_path):
    """The strongest wrong signal `load_baseline` can emit. A manifest round records
    `scope: "pr"` — the manifest travels as the round's material — with nothing
    truncated, because the manifest fitted. It therefore satisfied every term of the
    re-read test while having read not one line of the diff, and ONE entry there
    erases every earlier round's truncation and unread record."""
    cut = _payload(tmp_path, "r1.json", round=1,
                   reviewers={"claude": {"ran": True, "truncated": True}})
    mani = _payload(tmp_path, "r2.json", round=2,
                    preflight={"verdict": "manifest", "reason": "move-shaped"})
    b = panel.load_baseline([cut, mani], {"repo": "board", "github": "acme/board",
                                          "pr": 137, "round": 3})
    assert b.manifest_rounds == {2}
    assert b.truncated_rounds == {1}, "round 1's gap must survive a manifest round"


def test_a_real_whole_PR_reread_still_clears_a_manifest_rounds_gap(tmp_path):
    """The other direction, so the veto cannot become permanent: a later round that
    genuinely read the whole PR read the code the manifest only described, and a veto
    saying otherwise states something the baselines themselves disprove."""
    mani = _payload(tmp_path, "r1.json", round=1,
                    preflight={"verdict": "manifest", "reason": "move-shaped"})
    full = _payload(tmp_path, "r2.json", round=2,
                    preflight={"verdict": "run", "reason": None})
    b = panel.load_baseline([mani, full], {"repo": "board", "github": "acme/board",
                                           "pr": 137, "round": 3})
    assert b.manifest_rounds == set()


def test_a_payload_written_before_preflight_existed_is_not_a_manifest_round(tmp_path):
    """`preflight` absent means the round predates the field, not that it read a
    manifest — and reading it the other way would put a standing veto on every
    cycle whose round 1 was written by an older harness. The conservative
    direction here is the permissive one, because the field's absence is evidence
    about the WRITER, not about the round."""
    old = _payload(tmp_path, "r1.json", round=1)
    assert "preflight" not in json.loads(Path(old).read_text())
    b = panel.load_baseline([old], {"repo": "board", "github": "acme/board",
                                    "pr": 137, "round": 2})
    assert b.manifest_rounds == set()


@pytest.mark.parametrize("junk", [None, "manifest", 5, [], {"verdict": "run"},
                                  {"verdict": None}, {}])
def test_a_malformed_preflight_block_does_not_crash_the_loader(tmp_path, junk):
    """`load_baseline`'s standing rule is that a bad payload costs a `problems`
    entry and never a run — a hand-edited baseline must not raise from inside the
    one function written to survive them."""
    bad = _payload(tmp_path, "r1.json", round=1, preflight=junk)
    b = panel.load_baseline([bad], {"repo": "board", "github": "acme/board",
                                    "pr": 137, "round": 2})
    assert b.manifest_rounds == set()


def test_an_inherited_manifest_round_vetoes_a_later_SCOPED_round(monkeypatch,
                                                                tmp_path):
    """The gap a manifest round leaves does not close by itself. Under increment
    scope round 3 anchors after the manifest round's head and never returns to the
    relocated code, so without this the cycle converges over code no round in it
    read. It needs its own sentence because "no reviewer read it" — what the unread
    veto says — is false of a manifest round: every seat ran."""
    increment = _file("fix.py", added=["    the_fix()"])
    cfg = {"github": "acme/board", "path": "/tmp/b", "name": "board",
           "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
           "review_panel": {}}
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(pf, "seat_installed", ALL_HERE)
    monkeypatch.setattr(panel_core, "sh", gh_stub(diff=FRESH + increment))
    monkeypatch.setattr(panel_scope, "fetch_increment",
                        lambda repo, a, b: (increment, ""))
    monkeypatch.setattr(panel_scope, "compare_facts",
                        lambda *a: {"commits": 1, "files": 1, "additions": 1,
                                    "deletions": 0})
    monkeypatch.setattr(panel, "review_llm",
                        lambda n, m, p, effort="": panel.ReviewerRun([], None, 10, None))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, ""))
    mani = _payload(tmp_path, "r1.json", round=1,
                    preflight={"verdict": "manifest", "reason": "move-shaped"})
    out = tmp_path / "r2.json"
    assert panel.run("e2e", 137, post=False, json_file=str(out), record=False,
                     round_no=2, baseline=[mani], max_rounds=3) == 0
    got = json.loads(out.read_text())
    assert got["scope"] == "increment"
    assert any("read a MANIFEST of a move rather than its code" in v
               for v in got["round_stop"]["veto"])
    assert got["round_stop"]["confident"] is False


def test_a_refused_round_tells_the_board_that_NO_seat_ran(monkeypatch, tmp_path):
    """This feature's own failure mode, arriving in the board's statistics.
    `_scorecards` builds a row for every name in `reviewers_selected` and, with no
    `reviewers` block, "a member is assumed to have run unless it appears in
    `skipped`" — deliberately, so a quiet reviewer is not filed as broken. So a
    refusal that sent `reviewers_selected` and nothing else would record every
    configured seat as having run and found nothing: a refusal read as a clean
    review, per reviewer, in the table that answers which reviewer finds the real
    issues. The title-pattern skip dodges this by never being recorded; this path is
    recorded on purpose."""
    _, got, _ = _run(monkeypatch, tmp_path, FRESH,
                     _panel(max_diff_chars=len(FRESH) // 10),
                     seats=("claude", "codex"))
    assert got["reviewers_selected"] == ["claude", "codex"]
    # Both shapes, because the board reads two different keys.
    assert set(got["reviewers"]) == {"claude", "codex"}
    assert all(m["ran"] is False for m in got["reviewers"].values())
    assert all("refused this round" in m["skip"] for m in got["reviewers"].values())
    # `skipped` is parsed board-side as "<name>: <reason>", so the prefix matters.
    assert sorted(x.split(":", 1)[0] for x in got["skipped"]) == ["claude", "codex"]


# ------------------------------------------- a manifest of an INCREMENT keeps its scope

def _scoped_manifest_run(monkeypatch, tmp_path, baselines, cap_divisor=4):
    """A round 2 whose INCREMENT is itself move-shaped and over the ceiling."""
    increment = SPLIT
    cfg = {"github": "acme/board", "path": "/tmp/b", "name": "board",
           "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
           "review_panel": {"max_diff_chars": len(SPLIT) // cap_divisor}}
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(pf, "seat_installed", ALL_HERE)
    monkeypatch.setattr(panel_core, "sh", gh_stub(diff=FRESH + increment))
    monkeypatch.setattr(panel_scope, "fetch_increment",
                        lambda repo, a, b: (increment, ""))
    monkeypatch.setattr(panel_scope, "compare_facts",
                        lambda *a: {"commits": 1, "files": 3, "additions": 200,
                                    "deletions": 200})
    monkeypatch.setattr(panel, "review_llm",
                        lambda n, m, p, effort="": panel.ReviewerRun([], None, 10, None))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, ""))
    out = tmp_path / "r2.json"
    assert panel.run("e2e", 137, post=False, json_file=str(out), record=False,
                     round_no=2, baseline=list(baselines), max_rounds=3) == 0
    return json.loads(out.read_text())


def test_a_manifest_of_an_increment_still_records_the_scope_it_TARGETED(monkeypatch,
                                                                       tmp_path):
    """The substitution puts the manifest in a whole-target ("pr") scope — there are
    no tiers to compose — and that must not erase what the round set out to review.
    `scope` means what was reviewed, and `since_sha` is the commit its target was
    measured from; dropped, a scoped round that read a manifest publishes
    `since_sha: null` and loses the anchor record entirely."""
    got = _scoped_manifest_run(monkeypatch, tmp_path,
                               [_payload(tmp_path, "r1.json", round=1)])
    assert got["preflight"]["verdict"] == "manifest"
    assert got["scope"] == "increment", "what the round targeted"
    assert got["since_sha"] == "a" * 40, "the commit its target was measured from"


def test_a_manifest_round_does_not_lose_the_INHERITED_vetoes_it_should_carry(
        monkeypatch, tmp_path):
    """Three inherited coverage vetoes are gated on `scope == "increment"`, and the
    manifest substitution flips that flag. A move-shaped round 2 would have skipped
    every one of them and been free to stop `confident: True` over gaps earlier
    rounds left — because its MATERIAL stopped looking scoped, which is a different
    fact from the round's scope."""
    cut = _payload(tmp_path, "r1.json", round=1,
                   reviewers={"claude": {"ran": True, "truncated": True}})
    got = _scoped_manifest_run(monkeypatch, tmp_path, [cut])
    assert got["scope"] == "increment"
    assert any("had a truncated reviewer" in v for v in got["round_stop"]["veto"])
    assert got["round_stop"]["confident"] is False


# ----------------------------------------------------------------------------- the CLI

def test_the_ask_path_refuses_force(monkeypatch):
    """An ask has no diff, so there is no pre-flight verdict for a flag to
    override. Accepted silently it would be a caller believing it asked for
    something this run does not do — the reason every other round flag is refused
    there."""
    monkeypatch.setattr(sys, "argv", ["panel.py", "--ask", "a premise", "--force"])
    with pytest.raises(SystemExit) as e:
        panel.main()
    assert "--force" in str(e.value)
    assert "no pre-flight verdict to override" in str(e.value)


def test_force_reaches_run_from_the_command_line(monkeypatch):
    """The flag is parsed, and it is threaded all the way through. A `--force` that
    argparse accepts and `main` drops is a refusal nobody can get past."""
    got = {}
    monkeypatch.setattr(panel, "run", lambda *a, **k: got.update(args=a) or 0)
    monkeypatch.setattr(sys, "argv", ["panel.py", "--pr", "137", "--force"])
    assert panel.main() == 0
    assert got["args"][-1] is True
