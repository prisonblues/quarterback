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
from collections import Counter
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
            "_rules_baseline": ".harness-rules.sample",
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


def test_the_one_sided_file_lists_are_CAPPED_in_the_payload():
    """`preflight.shape` rides in `--json`, in `--json-file` and in the payload piped
    to `qb record-review` on EVERY run, and these two lists were the only part of it
    that grew with the PR: a 700-file refactor wrote 700 paths into each board row.
    The manifest's own file table has been capped since it was written; this is the
    same rule one layer out, with the elided count emitted even when it is 0 so a
    consumer can tell "all of them" from "the first forty"."""
    lines = ["    a_line_of_content()"]
    diff = "".join(_file(f"gained_{i}.py", added=lines)
                   for i in range(pf.PAYLOAD_FILE_ROWS + 7))
    d = pf.diff_shape(diff).as_dict()
    assert len(d["files_added_only"]) == pf.PAYLOAD_FILE_ROWS
    assert d["files_added_only_elided"] == 7
    assert d["files_removed_only"] == [] and d["files_removed_only_elided"] == 0


def test_the_diff_is_parsed_ONCE_on_a_manifest_verdict(monkeypatch):
    """`diff_shape` parsed it and then `move_manifest` parsed it again to rebuild the
    same three structures — a second full pass and a second pair of Counters over
    ~10,000 lines on the 763 KB case, for data that was in hand. It is also two
    answers where there should be one: the manifest has to describe the diff the
    verdict weighed."""
    calls = []
    real = pf._hunk_bodies
    monkeypatch.setattr(pf, "_hunk_bodies",
                        lambda diff: calls.append(len(diff)) or real(diff))
    got = _pre(SPLIT, {"claude": len(SPLIT) // 4}, {}, [])
    assert got.verdict == "manifest", "the path that used to parse twice"
    assert len(calls) == 1, calls


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
    assert pf.seat_ceilings(UNCAPPED, ALL_HERE) == ()
    assert pf.tightest_ceiling(UNCAPPED, pf.diff_shape(SPLIT), ALL_HERE) is None


def test_antigravity_is_capped_by_the_kernel_whether_or_not_a_repo_says_so():
    """That seat's prompt travels in argv, so `MAX_ARG_STRLEN` applies to it
    without anybody configuring anything. It is the only cap PR #137's repo had,
    and therefore the only reason the case is catchable at all."""
    got = pf.tightest_ceiling({"claude": None, "antigravity": None},
                              pf.diff_shape(SPLIT), ALL_HERE)
    assert got == pf.Ceiling(panel_core.ARGV_PROMPT_MAX_BYTES, "bytes", "antigravity")


def test_a_configured_antigravity_cap_does_not_HIDE_the_kernels():
    """The mixed-unit collapse, and the reason this is two ceilings rather than a
    `min()` of them.

    `min(cap, ARGV_PROMPT_MAX_BYTES)` compared a CHARACTER budget against a BYTE
    limit and kept whichever was the smaller integer, so a repo setting
    `antigravity.max_diff_chars: 100_000` hid the 120,000-byte argv ceiling behind
    the smaller number. On a diff averaging two bytes per character that seat's
    real ceiling is ~60,000 characters — genuinely tighter than the one that won —
    and the round was measured against the looser one and let through.
    """
    argv = panel_core.ARGV_PROMPT_MAX_BYTES
    small = argv // 2
    both = pf.seat_ceilings({"antigravity": small}, ALL_HERE)
    assert both == (pf.Ceiling(small, "chars", "antigravity"),
                    pf.Ceiling(argv, "bytes", "antigravity")), \
        "both are declared, both are real"

    # ASCII: the two readings of the size are the same number, so the smaller
    # ceiling binds and it is the configured one — the old answer, still right.
    ascii_shape = pf.diff_shape(FRESH * 40)
    assert ascii_shape.chars == ascii_shape.nbytes, "fixture drifted: not ASCII"
    assert pf.tightest_ceiling({"antigravity": small}, ascii_shape, ALL_HERE).unit \
        == "chars"

    # A diff the CHARACTER budget fits whole and the kernel's BYTE limit does not.
    # Under `min()` the configured ceiling won on being the smaller integer, the
    # round was measured in characters, `size <= cap` held and the verdict was
    # `run` — handing `execve` a prompt it cannot carry.
    text = _file("w.py", added=[f"    # 全角の行 {i} — 相当な長さの説明があります"
                                for i in range(2600)])
    wide = pf.diff_shape(text)
    assert wide.nbytes > argv > wide.chars, f"fixture drifted: {wide}"
    cap = wide.chars + 1
    assert cap < argv, "the numeric min() would have chosen the configured cap"
    assert pf.tightest_ceiling({"antigravity": cap}, wide, ALL_HERE) \
        == pf.Ceiling(argv, "bytes", "antigravity")
    # End to end, which is the half a unit test of the selection cannot show. Read
    # in characters this diff FITS (`size <= cap`, so `run` with an empty reason and
    # nothing said anywhere); read in the bytes the kernel actually counts it is
    # half again over the argv ceiling. The threshold is written down rather than
    # left at 3 because CJK is three bytes per character at most, so no fixture can
    # be 3x over in bytes while still fitting a character ceiling.
    assert wide.chars <= cap, "read in characters, this diff fitted"
    got = _pre(text, {"antigravity": cap}, {"refuse_over_cap_multiple": 1}, [])
    assert got.refused, got.reason
    assert got.cap_unit == "bytes" and got.measured == wide.nbytes
    assert got.over == pytest.approx(wide.nbytes / argv)
    assert f"{argv:,}-byte ceiling" in got.reason


def test_a_bigger_configured_budget_leaves_the_kernel_holding_the_floor():
    """The other side of the pair above: a cap looser than the kernel's changes
    nothing, because the kernel's applies to that seat regardless."""
    big = panel_core.ARGV_PROMPT_MAX_BYTES * 4
    got = pf.tightest_ceiling({"antigravity": big}, pf.diff_shape(SPLIT), ALL_HERE)
    assert got.limit == panel_core.ARGV_PROMPT_MAX_BYTES and got.unit == "bytes"


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
    assert pf.seat_ceilings(budgets, no_agy) == ()
    # The same round, on a box that HAS it: the ceiling is real and applies.
    assert pf.seat_ceilings(budgets, ALL_HERE)[0].seat == "antigravity"
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

    assert pf.seat_ceilings({"claude": 10, "antigravity": 10}, none_here) == ()
    assert _pre(FRESH * 40, {"claude": 10}, {}, [], installed=none_here).verdict == "run"


def test_the_host_predicate_is_resolved_in_the_BODY_so_it_can_be_replaced(monkeypatch):
    """A default argument binds the function object at `def` time, so
    `installed=seat_installed` in the signature made
    `monkeypatch.setattr(panel_preflight, "seat_installed", ...)` a no-op — and
    every end-to-end test here went on reading the real PATH while appearing to
    pin it. That is the shape of failure a CI runner finds and a workstation does
    not: ten tests here passed locally and failed with the vendor CLIs hidden."""
    monkeypatch.setattr(pf, "seat_installed", lambda name: False)
    assert pf.seat_ceilings({"antigravity": None}) == ()
    assert pf.preflight(FRESH * 40, {"antigravity": None}, {}, []).verdict == "run"


def test_seat_installed_asks_about_the_COMMAND_not_the_seat_name(monkeypatch):
    """The reviewer is `antigravity`; the command is `agy`. Asking PATH for
    "antigravity" would report the one seat that IS argv-bound as absent on every
    box, which is the direction that quietly switches the ceiling off."""
    asked = []
    monkeypatch.setattr(panel_core.shutil, "which", lambda c: asked.append(c) or "/x/bin/agy")
    assert pf.seat_installed("antigravity") is True
    assert asked == ["agy"]
    assert pf.seat_installed("claude") is True
    assert asked[-1] == "claude"


def test_the_tightest_seat_holds_the_floor_and_ties_break_by_name():
    """Deterministically, because the seat's name goes in the refusal's reason and
    a reason that names a different seat on two runs of the same round is a reason
    nobody can check."""
    shape = pf.diff_shape(FRESH)
    assert pf.tightest_ceiling({"claude": 50, "codex": 10, "pi": 90}, shape,
                               ALL_HERE) == pf.Ceiling(10, "chars", "codex")
    for _ in range(5):
        assert pf.tightest_ceiling({"pi": 10, "codex": 10}, shape,
                                   ALL_HERE).seat == "codex"
    # And the tie an EMPTY diff makes ordinary: every ratio is 0.0, so the
    # secondary key decides and it is the smaller number, exactly as the
    # numeric `min()` this replaced would have said.
    nothing = pf.diff_shape("")
    assert pf.tightest_ceiling({"claude": 50, "codex": 10, "pi": 90}, nothing,
                               ALL_HERE) == pf.Ceiling(10, "chars", "codex")


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


def test_a_manifest_disabled_move_is_not_reported_as_NOT_move_shaped():
    """The refusal's reason contradicted the measurement it was made from.
    `tried_manifest` was only ever set inside the `manifest_on` branch, so switching
    the manifest off sent the reason down the "it is not move-shaped … under the N
    move ratio" path on a diff whose ratio is 1.0."""
    got = _pre(SPLIT, {"claude": len(SPLIT) // 10},
                       {"manifest_moves": False}, [])
    assert got.refused
    assert got.shape.is_move()
    assert "it IS move-shaped" in got.reason
    assert "`manifest_moves` is off" in got.reason
    assert "not move-shaped" not in got.reason


def test_a_move_with_no_manifest_UNDER_the_multiple_runs_and_SAYS_why():
    """The case that fell through to `verdict("run", "")` with an empty reason. A
    move-shaped diff over the ceiling, the manifest unavailable, and the refusal
    threshold not crossed: reviewing it as truncated content is the only answer left,
    and it is the one case where a reader of `preflight.reason` most needs to know
    the manifest path was reached and did not help."""
    cap = int(len(SPLIT) / 1.5)          # over the ceiling, well under 3x
    got = _pre(SPLIT, {"claude": cap}, {"manifest_moves": False}, [])
    assert got.verdict == "run"
    assert 1 < got.over < 3
    assert "it IS move-shaped" in got.reason
    assert "`manifest_moves` is off" in got.reason
    assert "Under the 3x refusal threshold" in got.reason
    assert got.as_dict()["reason"]        # and it reaches the payload


def test_a_move_whose_manifest_does_not_help_says_so_with_the_refusal_SWITCHED_OFF():
    """`refuse_over_cap_multiple: 0` is the documented way to keep the manifest and
    drop the refusal. It must not become the way to lose the explanation as well."""
    got = _pre(SPLIT, {"claude": len(SPLIT) // 10},
                       {"manifest_moves": False, "refuse_over_cap_multiple": 0}, [])
    assert got.verdict == "run"
    assert "With the refusal switched off" in got.reason


def test_a_manifest_SMALLER_than_the_diff_but_over_the_CEILING_is_not_substituted():
    """`len(text) < shape.chars` only rejected a manifest bigger than the diff it
    replaces. A manifest smaller than a 763 KB diff and still over a tight ceiling was
    substituted and then truncated by the ordinary budget path — a seat reading a
    PREFIX of a manifest, reported as a clean `manifest` verdict beside
    `diff_truncated: true`, which is the confusing pair the substitution exists to
    avoid."""
    size = len(pf.move_manifest(SPLIT))
    cap = size - 100                      # smaller than the diff, over the ceiling
    assert size < len(SPLIT), "fixture drifted: the manifest is not smaller"
    got = _pre(SPLIT, {"claude": cap}, {}, [])
    assert got.verdict != "manifest"
    assert got.manifest == ""
    assert "smaller than the diff, but still over" in got.reason
    assert "prefix of a manifest" in got.reason
    # And with the refusal off it runs rather than silently substituting.
    ran = _pre(SPLIT, {"claude": cap}, {"refuse_over_cap_multiple": 0}, [])
    assert (ran.verdict, ran.manifest) == ("run", "")


def test_a_manifest_EXACTLY_the_size_of_the_ceiling_still_fits():
    """The two comparisons have to agree about the boundary, and they did not. A
    diff is admitted by `size <= cap` — inclusive — while the manifest was
    substituted only on `fitted < min(cap, size)`, so a manifest whose length was
    exactly the ceiling fell through to the branch that says a seat "would read a
    prefix of a manifest". At `fitted == cap` nothing is truncated and no prefix is
    read: the sentence was simply false, and the substitution this whole path exists
    to make was declined at the one size where it is exactly affordable."""
    cap = len(pf.move_manifest(SPLIT))
    assert cap < len(SPLIT), "fixture drifted: the manifest is not smaller"
    got = _pre(SPLIT, {"claude": cap}, {}, [])
    assert got.verdict == "manifest"
    assert len(got.manifest) == cap, "exactly at the ceiling, and it fits"
    # One character tighter is over, and that IS the prefix case.
    over = _pre(SPLIT, {"claude": cap - 1}, {}, [])
    assert over.verdict != "manifest"
    assert "prefix of a manifest" in over.reason


def test_a_manifest_EXACTLY_the_size_of_the_diff_is_still_no_saving():
    """The other end of the same comparison, which the fix must not loosen:
    `fitted < size` stays strict, because a manifest the same length as the diff
    replaces the problem with a copy of it rather than solving anything."""
    tiny = [f"    x{i} = {i}" for i in range(6)]
    diff = _file("from.py", removed=tiny) + _file("to.py", added=tiny)
    assert len(pf.move_manifest(diff)) > len(diff), "fixture drifted"
    got = _pre(diff, {"claude": len(diff) // 10}, {}, [])
    assert got.verdict != "manifest"
    assert "replace the problem with a copy of it" in got.reason


def test_a_manifest_that_fits_BOTH_the_diff_and_the_ceiling_is_still_substituted():
    """The other side of the guard above, so it cannot become a switch that turns the
    feature off: a ceiling the manifest comfortably fits under still gets one."""
    got = _pre(SPLIT, {"claude": len(SPLIT) // 4}, {}, [])
    assert got.verdict == "manifest"
    assert len(got.manifest) < len(SPLIT) // 4


def test_a_forced_manifest_does_not_claim_the_round_read_a_manifest():
    """--force does not re-run the verdict: it turns `manifest` into `run`, and
    `panel.run` then reviews the full diff as content because `pre.verdict ==
    "manifest"` is what triggers the substitution and is no longer true. The reason
    was carried through verbatim, so the payload recorded "Reviewed as a MANIFEST
    instead — what moved where …" on a round that reviewed the diff. `preflight.reason`
    is what a reader has six weeks later instead of the round."""
    got = _pre(SPLIT, {"claude": len(SPLIT) // 4}, {}, [], forced=True)
    assert (got.verdict, got.would_have, got.forced) == ("run", "manifest", True)
    assert got.reason.startswith("--force: ")
    assert "Reviewed as a MANIFEST instead" not in got.reason
    assert "was overruled" in got.reason
    assert "reviewed as content" in got.reason
    # The diagnosis survives: an override that erases the measurement leaves nothing
    # to argue with.
    assert "move-shaped" in got.reason


def test_a_forced_refusal_does_not_still_advise_passing_force():
    """The refusal's remedies end "or pass --force". Prefixed rather than replaced,
    a forced round's audit reason told the reader to do the thing they had just
    done."""
    got = _pre(FRESH, {"claude": len(FRESH) // 10}, {}, [], forced=True)
    assert got.would_have == "refuse"
    assert "or pass --force" not in got.reason
    assert "The refusal was overruled" in got.reason


def test_the_argv_ceiling_is_measured_in_BYTES_not_characters():
    """`ARGV_PROMPT_MAX_BYTES` is the kernel's `MAX_ARG_STRLEN`, in bytes;
    `DiffShape.chars` is `len(diff)`, in characters. Compared against each other, a
    diff full of em-dashes and arrows — which every diff in this repo is — is
    understated by exactly its non-ASCII density, in the direction that lets an
    over-cap round through."""
    # Three bytes per character over most of the line, so the two readings differ by
    # more than 2x — and a pure move, so the verdict has a reason to state the unit in.
    wide = [f"    # 全角の行 {i} — 相当な長さの説明がここにあります" for i in range(900)]
    diff = (_file("wide.py", removed=wide)
            + _file("part_a.py", added=wide[:450])
            + _file("part_b.py", added=wide[450:]))
    shape = pf.diff_shape(diff)
    assert shape.nbytes > shape.chars * 2, "fixture drifted: not enough non-ASCII"
    # A diff whose CHARACTER count fits the kernel ceiling and whose BYTE count does
    # not. Read in characters this round is not over the cap at all.
    cap = panel_core.ARGV_PROMPT_MAX_BYTES
    assert shape.chars < cap < shape.nbytes, f"fixture drifted: {shape}"
    got = _pre(diff, {"antigravity": None}, {}, [])
    assert got.over > 1, "the byte reading is what the argv ceiling is in"
    assert got.over == pytest.approx(shape.nbytes / cap)
    assert got.verdict == "manifest"
    assert "bytes of diff" in got.reason
    assert f"{cap:,}-byte ceiling" in got.reason
    # And a CONFIGURED ceiling is still characters, on the same diff.
    chars = _pre(diff, {"claude": shape.chars // 2}, {}, [])
    assert chars.over == pytest.approx(shape.chars / (shape.chars // 2))
    assert "chars of diff" in chars.reason
    assert f"{shape.chars // 2:,}-char ceiling" in chars.reason


def test_the_one_sided_file_LISTS_cannot_be_passed_positionally():
    """`nbytes` was inserted ahead of them, which changed what the sixth positional
    argument means without changing the arity: an existing
    `DiffShape(chars, added, removed, moved, files, added_only, removed_only)` bound
    a tuple of paths to an `int` field and serialised `"bytes": ["a.py"]` into every
    board record for that PR, with no TypeError anywhere to notice it. Keyword-only
    turns a silent rebinding into an error at the call site."""
    with pytest.raises(TypeError):
        pf.DiffShape(100, 5, 5, 5, 2, 100, ("a.py",), ("b.py",))
    # And the short hand-built literal the class's docstring invites still works.
    s = pf.DiffShape(100, 5, 5, 5, 2)
    assert (s.chars, s.nbytes, s.files_added_only) == (100, 0, ())


def test_both_readings_of_the_size_are_serialised():
    """So which one a verdict used is checkable rather than taken on trust."""
    d = pf.diff_shape("diff --git a/x b/x\n@@ -1 +1 @@\n+é\n").as_dict()
    assert d["chars"] == 34 and d["bytes"] == 35


def test_the_judge_budget_does_not_hold_the_CEILING(monkeypatch, tmp_path):
    """A deliberate boundary, pinned because the refusal payload records
    `diff_budgets` WITH a `judge` entry and so invites the opposite reading. This
    verdict decides whether to dispatch the SEATS; `judge_max_diff_chars` says what
    adjudication is worth, and counting it here would let that knob refuse a round
    every reviewer could read whole."""
    _, got, seen = _run(monkeypatch, tmp_path, FRESH,
                        _panel(judge_max_diff_chars=len(FRESH) // 50))
    assert got["preflight"]["verdict"] == "run"
    assert got["preflight"]["cap"] is None
    assert seen["prompts"]["claude"], "the seats were dispatched"
    # The budget is still RECORDED, which is the half that invites the misreading.
    assert got["diff_budgets"]["judge"] == len(FRESH) // 50


def test_the_move_ratio_threshold_is_configurable():
    """The same diff, read as content at 0.9 and as a move at 0.5.

    The ceiling has to be one the MANIFEST fits under as well as one the diff is
    far past, and the fixture asserts both rather than assuming them: the
    substitution is measured against the ceiling now, not only against the diff, so
    a cap chosen only to be "far over" can no longer reach a manifest verdict at
    all and the test would be pinning the refusal twice.
    """
    new = [f"    fresh_{i} = brand_new({i}, retries=3)" for i in range(50)]
    diff = _file("from.py", removed=BODY) + _file("to.py", added=BODY + new)
    ratio = pf.diff_shape(diff).move_ratio
    assert 0.5 <= ratio < 0.9, f"fixture drifted: {ratio}"
    cap = len(diff) // 4
    assert len(pf.move_manifest(diff)) < cap, "fixture drifted: manifest over the cap"
    budgets = {"claude": cap}
    assert _pre(diff, budgets, {}, []).refused            # under the 0.9 default
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
    # The ceiling has to be one the MANIFEST fits under, or the verdict being
    # overridden is the refusal and this test pins nothing it means to. Asserted
    # rather than assumed: the manifest's fixed overhead is prose, and prose grows.
    cap = len(SPLIT) // 4
    assert len(pf.move_manifest(SPLIT)) < cap, "fixture drifted: manifest over the cap"
    got = _pre(SPLIT, {"claude": cap}, {}, [], forced=True)
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


@pytest.mark.parametrize("junk", [float("nan"), float("inf"), float("-inf"),
                                  "nan", "inf"])
def test_a_NON_FINITE_threshold_falls_back_and_says_so(junk):
    """`nan` compares false against everything and `inf` is never exceeded, so both
    switch a check off while reading like a number: `move_shape_ratio: nan` silently
    means "nothing is ever a move", `refuse_over_cap_multiple: inf` silently means
    "never refuse". The negative check already established that a value which cannot
    be the thing at all is reported rather than honoured; these are the same class."""
    notes = []
    got = _pre(FRESH, {"claude": len(FRESH) // 10},
                       {"refuse_over_cap_multiple": junk}, notes)
    assert got.refused, "it fell back to the default, which refuses"
    assert any("not a finite number" in n for n in notes), notes


def test_a_move_ratio_ABOVE_ONE_falls_back_rather_than_becoming_unsatisfiable():
    """The ratio is relocated lines as a fraction of the LARGER side, so 1.0 is a
    move with no residue at all and there is nothing above it to express.
    `move_shape_ratio: 90` — meant as 90% — otherwise passes validation, makes
    `is_move` unsatisfiable, and turns every over-cap round into a refusal whose
    reason reads "under the 90 move ratio": a plausible sentence about a threshold
    that cannot be met."""
    notes = []
    got = _pre(SPLIT, {"claude": len(SPLIT) // 4}, {"move_shape_ratio": 90}, notes)
    assert got.verdict == "manifest", "the default 0.9 applied instead"
    assert any("cannot be above 1" in n for n in notes), notes
    # 1.0 itself is legitimate and stays silent — "a move with no residue".
    quiet = []
    _pre(SPLIT, {"claude": len(SPLIT) // 4}, {"move_shape_ratio": 1.0}, quiet)
    assert quiet == []


def test_FALSE_on_a_numeric_threshold_is_refused_and_told_which_number_to_write():
    """`false` is the other way an operator writes "off", and it must not be read as
    the number 0: the same rule covers `move_shape_ratio`, where a threshold of 0
    makes every diff with one relocated line a move — the switch flipped to "off"
    turning the feature all the way on. So it falls back and the note names `0`."""
    notes = []
    got = _pre(FRESH, {"claude": len(FRESH) // 10},
                       {"refuse_over_cap_multiple": False}, notes)
    assert got.refused
    assert any("write `0` to switch it off" in n for n in notes), notes


def test_FALSE_on_the_MOVE_RATIO_is_not_told_to_write_the_value_that_is_the_trap():
    """The `0` hint was emitted for every key, `move_shape_ratio` included — and
    `_rule`'s own docstring argues at length that reading `false` as 0 would be
    wrong there because "every diff with one relocated line is a move: the switch
    flipped to off turning the feature all the way on". So the note refused to
    INTERPRET `false` as 0 and then told the operator to type it by hand. An
    operator who complied got the exact behaviour the paragraph warns about.

    `move_shape_ratio` has no off. The switch somebody reaching for one wants is
    `manifest_moves`, and that is what the note now names."""
    notes = []
    got = _pre(SPLIT, {"claude": len(SPLIT) // 4}, {"move_shape_ratio": False}, notes)
    assert got.thresholds["move_shape_ratio"] == pf.DEFAULT_MOVE_SHAPE_RATIO
    assert len(notes) == 1, notes
    assert "is not a number" in notes[0]
    assert "write `0`" not in notes[0], "that is the trap, not the remedy"
    assert "manifest_moves" in notes[0]
    # And 0 really is the trap: at a threshold of 0 a diff with one relocated line
    # is a move, which is what the advice would have produced.
    barely = _file("a.py", removed=["shared = 1"] + [f"x{i} = {i}" for i in range(80)],
                   added=["shared = 1"])
    assert not pf.diff_shape(barely).is_move(), "not a move at the default"
    assert pf.diff_shape(barely).is_move(0), "and every diff is one at 0"


def test_NULL_on_the_refusal_multiple_means_the_DEFAULT_and_not_off():
    """Two docstrings said null switched the refusal off while `_rule` read it as
    "use the default", so an operator who wrote `refuse_over_cap_multiple: null` to
    opt out got refusals with nothing in `config_notes` to explain them. The docs
    were the wrong half — null cannot mean "off" while it also means "inherit", and
    every other setting in this harness reads it as "inherit" — so this pins the
    behaviour the docs now describe."""
    notes = []
    got = _pre(FRESH, {"claude": len(FRESH) // 10},
                       {"refuse_over_cap_multiple": None}, notes)
    assert got.refused, "null is inherit, and the inherited default refuses"
    assert notes == [], "and inherit is the silent case"
    # `0` is the one spelling of off.
    assert _pre(FRESH, {"claude": len(FRESH) // 10},
                        {"refuse_over_cap_multiple": 0}, []).verdict == "run"


@pytest.mark.parametrize("raw,off", [(False, True), ("false", True), ("off", True),
                                     ("no", True), ("0", True), (True, False),
                                     ("true", False), ("YES", False), ("", False),
                                     (None, False),
                                     # The bare numbers, which `_FALSE_WORDS`
                                     # accepted as STRINGS while rejecting as
                                     # integers — see the test below.
                                     (0, True), (1, False), (0.0, True), (1.0, False)])
def test_manifest_moves_is_VALIDATED_as_a_boolean(raw, off):
    """It was `panel.get("manifest_moves", True)` — raw truthiness — while both
    numeric settings introduced beside it went through `_rule` on purpose, so that a
    junk threshold is reported the way every other bad config value is. The gap
    mattered in the one direction nobody notices: `manifest_moves: "false"` is a
    non-empty string, so the feature an operator had just written "false" against
    stayed ON, and `thresholds` then reported `bool(raw)` as though the value had
    been checked."""
    notes = []
    got = _pre(SPLIT, {"claude": len(SPLIT) // 4}, {"manifest_moves": raw}, notes)
    assert got.thresholds["manifest_moves"] is not off
    assert got.verdict == ("refuse" if off else "manifest")
    assert notes == []


def test_a_manifest_moves_value_that_is_NOT_a_boolean_falls_back_and_says_so():
    notes = []
    got = _pre(SPLIT, {"claude": len(SPLIT) // 4},
                       {"manifest_moves": "maybe"}, notes)
    assert got.thresholds["manifest_moves"] is True
    assert got.verdict == "manifest"
    assert any("is not true or false" in n for n in notes), notes
    # A number that is not a boolean is still junk, so 0/1 widening the accepted
    # set has not turned the key into "any truthy value".
    more = []
    _pre(SPLIT, {"claude": len(SPLIT) // 4}, {"manifest_moves": 2}, more)
    assert any("is not true or false" in n for n in more), more


def test_manifest_moves_reads_the_SAME_off_switch_its_neighbour_documents():
    """`_FALSE_WORDS` contains the string `"0"`, so `manifest_moves: "0"` switched
    the manifest off while the bare `manifest_moves: 0` — the natural spelling in a
    JSON `.harness-rules`, where a number needs no quoting decision — fell through
    to "is not true or false" and left it ON with a note. And `0` is the documented,
    only spelling of off for `refuse_over_cap_multiple`, which sits in the same
    block: an operator writing both expected both off and got one."""
    notes = []
    got = _pre(SPLIT, {"claude": len(SPLIT) // 4},
               {"manifest_moves": 0, "refuse_over_cap_multiple": 0}, notes)
    assert got.thresholds["manifest_moves"] is False
    assert got.thresholds["refuse_over_cap_multiple"] == 0
    assert got.verdict == "run", "both switched off, so the round is an ordinary one"
    assert notes == [], "and neither needed explaining"


def test_a_measured_ratio_of_zero_is_not_serialised_as_NO_CEILING():
    """`round(self.over, 2) or None` emitted null both for "no cap" and for a real
    cap the diff is tiny against (200 chars against 120,000 rounds to 0.0). The
    `preflight` block's own docs make exactly that null-vs-measured distinction
    load-bearing one level up, so reusing null inside it undercuts the same
    argument."""
    small = _file("x.py", added=["one line"])
    got = _pre(small, {"antigravity": None}, {}, [])
    assert got.verdict == "run"
    assert got.cap == panel_core.ARGV_PROMPT_MAX_BYTES
    assert round(got.over, 2) == 0.0
    assert got.as_dict()["over_cap"] == 0.0, "measured, and small"
    # And no cap at all still serialises as null, which is the other statement.
    assert _pre(small, UNCAPPED, {}, []).as_dict()["over_cap"] is None


def test_the_verdict_serialises_the_measurement_it_was_made_from():
    """A verdict a consumer cannot check is a verdict nobody argues with, and the
    board is the consumer that has to hold it for six weeks."""
    cap = len(SPLIT) // 4
    got = _pre(SPLIT, {"antigravity": cap}, {}, [])
    d = got.as_dict()
    assert d["verdict"] == "manifest"
    assert d["cap"] == cap and d["cap_seat"] == "antigravity"
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
    with pytest.raises(ValueError, match="only a refusal has a reason"):
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


def test_the_refusal_notice_ADDS_UP_when_the_verdict_was_measured_in_BYTES():
    """The notice is the human-facing artefact: it goes on the PR, and a reader who
    divides the two numbers it prints must get the multiple it prints.

    It did not. `over` came from UTF-8 bytes whenever antigravity's argv ceiling
    bound, and every line of the report said "chars" and quoted `shape.chars` — so a
    100,000-character / 260,000-byte diff against a 120,000-BYTE ceiling posted
    "100,000 chars … tightest seat ceiling: 120,000 chars, exceeded 2.2x". Divide
    and you get 0.83x, and the only honest conclusion is that the tool is broken.
    Nothing covered it: the byte work was pinned on `reason` alone.
    """
    argv = panel_core.ARGV_PROMPT_MAX_BYTES
    wide = [f"    # 全角の行 {i} — 相当な長さの説明があります" for i in range(6000)]
    diff = _file("w.py", added=wide)
    s = pf.diff_shape(diff)
    assert s.nbytes > argv * 3 and s.nbytes > s.chars * 2, f"fixture drifted: {s}"
    got = _pre(diff, {"antigravity": None}, {}, [])
    assert got.refused and got.cap_unit == "bytes"

    text = pf.refusal_report("board", 137, "a title", "main", got)
    # The ceiling, the size it was compared against and the multiple are all in the
    # SAME unit, and the multiple is what dividing them gives.
    assert f"{argv:,} bytes (antigravity)" in text
    assert f"{s.nbytes:,} bytes" in text
    assert f"exceeded {s.nbytes / argv:.1f}x" in text
    assert "in BYTES rather than characters" in text
    # Both readings of the diff, because the ratio was computed from one of them.
    assert f"{s.chars:,} chars / {s.nbytes:,} bytes" in text
    # And the remedy that cannot work is not offered: no setting raises the
    # kernel's argv limit, so "raise `max_diff_chars`" would send an operator round
    # the loop for the same refusal. That advice only reads as plausible while the
    # notice is silent about which ceiling it means.
    assert "Raise `review_panel.max_diff_chars`" not in text
    assert "Drop the `antigravity` seat" in text
    for line in text.splitlines():
        assert "  " not in line.strip(), line


def test_an_ASCII_refusal_does_not_print_the_same_number_twice():
    """The other half of the rule above: on a diff whose two readings agree, a
    second copy of the same figure is noise between the reader and the measurement."""
    got = _pre(FRESH, {"claude": len(FRESH) // 10}, {}, [])
    assert got.shape.chars == got.shape.nbytes, "fixture drifted: not ASCII"
    text = pf.refusal_report("board", 137, "a title", "main", got)
    assert f"{got.shape.chars:,} chars," in text
    assert "bytes" not in text
    assert f"{got.cap:,} chars (claude)" in text
    # …and the configured-ceiling remedy is the one that applies here.
    assert "Raise `review_panel.max_diff_chars`" in text


def test_the_refusal_notice_MARKS_a_truncated_title():
    """`title[:60]` cut mid-word with no marker, while `_quote` in the same module
    appends " …" for exactly this reason and `fit_comment` marks its own cut. A reader
    of a posted refusal could not tell a 60-character title from a truncated one."""
    got = _pre(FRESH, {"claude": len(FRESH) // 10}, {}, [])
    long = "refactor: " + "the world and everything that is in it " * 3
    assert len(long) > 60
    text = pf.refusal_report("board", 137, long, "main", got)
    assert f"{long[:60]} …" in text
    # A title that fits is quoted whole, with no marker to make a reader doubt it.
    short = "fix: one thing"
    assert f"{short}\n" in pf.refusal_report("board", 137, short, "main", got)


def test_the_refusal_notice_reports_the_CI_gate_and_names_the_one_it_did_NOT_read():
    """CI is size-independent, costs one API call and consumes no seat's budget — it is
    the one part of a round a 763 KB diff cannot make useless, and a refusal that lost
    it left `/panel-review-pr` told to stop the cycle with nothing said about a red
    build. Sonar is a panel MEMBER with a `ran: false` row, so it is not read; the
    notice says that gate was not evaluated rather than letting its default read as a
    pass."""
    got = _pre(FRESH, {"claude": len(FRESH) // 10}, {}, [])
    text = pf.refusal_report("board", 137, "a title", "main", got,
                             "FAIL", ("build", "lint"))
    assert "CI: FAILED" in text
    assert "build, lint" in text
    assert "SonarCloud: NOT evaluated" in text
    assert "never as a pass" in text
    for line in text.splitlines():
        assert "  " not in line.strip(), line


@pytest.mark.parametrize("status,says", [
    ("PASS", "PASSED on this commit"),
    ("PENDING", "STILL RUNNING"),
    ("none", "no checks are configured"),
    ("unknown", "could NOT be read"),
    ("", "NOT read for this refusal"),
])
def test_no_CI_state_is_allowed_to_read_as_a_PASS(status, says):
    """The same discipline `ci_brief` applies for a reviewer, applied for a human: a
    refusal notice that lets a missing gate read as a green one is the same failure as
    a refusal that reads as a clean review."""
    line = pf._ci_line(status)
    assert says in line
    if status != "PASS":
        assert "not a pass" in line


def test_the_refusal_guard_survives_python_dash_O():
    """It was a bare `assert`, and `python -O` strips assertions — so the one thing
    standing between a caller with the wrong verdict and a reasonless "**Why:** ."
    notice on somebody's PR was removed by a flag on the interpreter. The docstring
    says that failure is not hypothetical, which is the argument for an explicit
    raise."""
    import subprocess
    loops = str(Path(__file__).resolve().parent.parent)
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import panel_preflight as pf\n"
        "ok = pf.Preflight('run', '', pf.diff_shape(''))\n"
        "try:\n"
        "    pf.refusal_report('board', 1, 't', 'main', ok)\n"
        "except ValueError as e:\n"
        "    print('RAISED' if 'only a refusal' in str(e) else 'WRONG')\n"
        "else:\n"
        "    print('SLIPPED THROUGH')\n" % loops)
    for flags in ([], ["-O"], ["-OO"]):
        out = subprocess.run([sys.executable, *flags, "-c", code],
                             capture_output=True, text=True, timeout=60)
        assert out.stdout.strip() == "RAISED", (flags, out.stdout, out.stderr)


def test_the_private_helpers_are_not_re_exported_under_a_star_import():
    """`panel.py` does `from panel_preflight import *` LAST of five sibling modules, so
    anything in `__all__` wins a name collision against `panel_core`, `panel_seats`,
    `panel_scope` and `panel_rounds` — silently, with no error, and with no test able
    to notice, because the tests reach each module's helpers through the module object.
    `_rule`, `_listing` and `_quote` are generic enough names that a sibling could grow
    one any day."""
    assert not [n for n in pf.__all__ if n.startswith("_")]
    # Still reachable where they are actually used, which is here.
    assert callable(pf._rule) and callable(pf._listing) and callable(pf._quote)


def test_a_zero_ceiling_is_INFINITELY_over_not_comfortably_under():
    """`over()` guarded its ZeroDivisionError with 0.0, which is the one answer that
    is wrong in the most consequential direction (217-R3-F02): a diff of any nonzero
    size against a ceiling of zero is infinitely over, and 0.0 reads as comfortably
    under — inside the function that decides whether to refuse the round.

    `seat_ceilings` also filters a non-positive budget now, so config cannot reach
    this today: `diff_budget` already refuses `<= 0` and falls back with a note. Both
    halves are pinned because the class is public and its next caller need not go
    through `diff_budget`."""
    shape = pf.diff_shape(FRESH)
    assert pf.Ceiling(0, "chars", "claude").over(shape) == float("inf")
    # 0/0 is not over anything: nothing to send and nothing to cut.
    assert pf.Ceiling(0, "chars", "claude").over(pf.diff_shape("")) == 0.0
    # And a zero budget never becomes a ceiling in the first place.
    assert pf.seat_ceilings({"claude": 0}, ALL_HERE) == ()
    assert [c.limit for c in pf.seat_ceilings({"claude": 10}, ALL_HERE)] == [10]


#: A move whose DIFF is byte-heavy — em dashes are one char and three bytes — while
#: a manifest OF it is nearly all ASCII prose. That density gap is what makes the
#: two texts rank a set of ceilings differently, and it is not contrived: this
#: repo's own diffs are full of em dashes, which is why `DiffShape` carries `nbytes`
#: at all.
DENSE_BODY = [f"    value_{i} = compute({i})  # — — — — — — — — — —" for i in range(200)]
DENSE_SPLIT = (_file("big.py", removed=DENSE_BODY)
               + _file("a.py", added=DENSE_BODY[:100])
               + _file("b.py", added=DENSE_BODY[100:]))


def test_the_manifest_ranks_ceilings_by_its_OWN_size_not_the_diffs(monkeypatch):
    """`tightest_ceiling` ranks by ratio against the DIFF's density, and a manifest
    has neither the same density nor the same size — so the seat that binds for the
    diff need not bind for the manifest (217-R3-F01). The substitution therefore
    measures against every ceiling rather than the diff's winner alone.

    **This is LATENT, not reachable from config today, and the test says so rather
    than pretending otherwise.** For a byte ceiling to out-rank the char ones on the
    diff, the char budgets have to exceed ~87,000 (`ARGV_PROMPT_MAX_BYTES` over this
    repo's ~1.38 bytes/char) — and a manifest is ~2.3 KB, so it then fits them
    trivially and the two rankings agree. `seat_ceilings` also gives a configured
    `antigravity: N` a **chars** ceiling plus the kernel's bytes one, so the mixed
    pair that flips has to be built by hand. What is pinned here is the ranking
    property itself, which is what the code relies on and what a future ceiling unit
    would break.
    """
    shape = pf.diff_shape(DENSE_SPLIT)
    text = pf.move_manifest(DENSE_SPLIT, shape)
    # The density gap is real and is why `DiffShape` carries `nbytes` at all.
    assert len(DENSE_SPLIT.encode()) / len(DENSE_SPLIT) > 1.3
    assert len(text.encode()) / len(text) < 1.05

    mixed = (pf.Ceiling(2_000, "chars", "claude"),
             pf.Ceiling(2_400, "bytes", "antigravity"))
    by_diff = max(mixed, key=lambda c: c.over(shape))
    by_manifest = max(mixed, key=lambda c: c.of_text(text) / c.limit)
    assert by_diff.seat == "antigravity", "the diff is byte-bound"
    assert by_manifest.seat == "claude", "the manifest is char-bound"
    # And the consequence the fix exists for: the manifest fits the diff's winner
    # while overflowing its own, so ranking on the diff alone would substitute a
    # manifest that one seat can only read a prefix of.
    assert by_diff.of_text(text) <= by_diff.limit
    assert by_manifest.of_text(text) > by_manifest.limit


def test_a_move_that_cannot_be_manifested_small_enough_is_refused_not_truncated():
    """The reachable half, end to end: when the manifest does not fit, the round is
    refused and the reason names the manifest's own measurement — never substituted
    and then cut, which would leave a seat reading a prefix of a manifest."""
    got = _pre(DENSE_SPLIT, {"claude": 2_000}, {}, [], installed=ALL_HERE)
    assert got.refused
    assert got.manifest == ""
    assert "a manifest of it came to" in got.reason

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
    assert "NOT covered" in text


def test_the_coverage_DISCLAIMER_survives_a_section_that_found_something():
    """It used to print only in the empty branch, which inverted the rule it was
    written for. "A pattern that matches nothing is worse than an absent section:
    it reads as 'checked, and clean'" — and a section that found ONE duplicate
    reads as having found THE duplicates. A TS move that duplicates a `function`
    and a class METHOD lists the function, and the method is never mentioned."""
    diff = (_file("from.ts", removed=["export function pick(x) {"])
            + _file("a.ts", added=["export function pick(x) {"])
            + _file("b.ts", added=["export function pick(x) {"]))
    text = pf.move_manifest(diff)
    assert "! pick" in text, "the section fired"
    assert "NOT covered" in text, "and still said what it cannot see"
    assert "METHODS" in text
    assert "@typing.overload" in text, "and which of its hits are ordinary"


@pytest.mark.parametrize("line,name", [
    ("def handle(self, x):", "handle"),
    ("    async def fetch(url):", "fetch"),
    ("class Widget(Base):", "Widget"),
    ("class Bare:", "Bare"),
    ("export function render(node) {", "render"),
    ("async function poll() {", "poll"),
    # The JS/TS spellings the check claimed and did not have. `function name(` is
    # the one form modern JS/TS uses least, and while only it was matched a
    # TypeScript move written in any of these got a false all-clear inside a
    # language the manifest said it had checked.
    ("class Foo {", "Foo"),
    ("class Foo extends Bar {", "Foo"),
    ("export class Foo implements Baz {", "Foo"),
    ("export default class Foo {", "Foo"),
    ("export default function render(n) {", "render"),
    ("function* walk(node) {", "walk"),
    ("const handle = (req, res) => {", "handle"),
    ("  let fetchAll = async () => {", "fetchAll"),
    ("export const send: Sender = payload => post(payload)", "send"),
    ("var legacy = function (a, b) {", "legacy"),
    ("export interface Widget {", "Widget"),
    ("interface Widget extends Base {", "Widget"),
    ("export enum Colour {", "Colour"),
    ("export type Handler<T> = (x: T) => void", "Handler"),
    # The spellings the JS/TS pass claimed and still did not have. Each was a
    # silent miss INSIDE a spelling `_DEF_SPELLINGS` told the reader was covered,
    # which is the false all-clear the whole section is written against.
    ("const identity = <T>(x: T) => x", "identity"),                  # generic arrow
    ("const wrap = async <T, U>(x: T): U => cast(x)", "wrap"),        # …and async
    ("const pick = <T extends Foo<Bar>>(x: T) => x", "pick"),         # …and nested
    ("const send: (x: T) => U = x => transform(x)", "send"),          # function-TYPED
    ("const enum Colour {", "Colour"),
    ("export declare class Remote extends Base {", "Remote"),
    ("declare class Ambient {", "Ambient"),
])
def test_the_definition_shapes_it_recognises(line, name):
    assert pf._def_name(line) == name


@pytest.mark.parametrize("line", [
    # A method is spelled identically to a call, an `if`, a `for` and a `catch`, so
    # a pattern loose enough to catch one flags all of them — and a section that
    # fires on `if (ok) {` appearing twice is worse than one that misses a method,
    # because the reader stops believing it. Named in the manifest's disclaimer
    # instead, where a reader can act on it.
    "    render(props) {",
    "if (ok) {",
    "for (const x of xs) {",
    "} catch (e) {",
    # Not a definition either way: the right-hand side has to BE a function, or
    # `const total = (a + b);` is filed as one.
    "    const total = (a + b);",
    "    const label = words.map(w => w).join(' ');",
    "    return compute(x)",
    "    for (const item of items) {",
    "    const same = a === b ? f : g;",
    # A multi-declarator line: the arrow belongs to `c`, not to `a`. The
    # function-typed annotation admits `=>` and nothing else containing `=`,
    # precisely so this cannot be filed under the first name on the line — a
    # wrong name in the duplicate section is worse than a missing one.
    "    const a = b, c = (x) => y;",
])
def test_the_shapes_it_deliberately_does_not_recognise(line):
    assert pf._def_name(line) == ""


def test_a_definition_added_once_is_not_flagged():
    assert pf.duplicate_definitions({"only_here": Counter({"a.py": 1})}) == {}
    # Twice in ONE file is still twice, and still a duplicate.
    assert pf.duplicate_definitions({"twice": Counter({"a.py": 2})}) == {
        "twice": Counter({"a.py": 2})}


def test_a_duplicate_names_the_files_each_copy_LANDED_in():
    """The values were `[]` for every key under a `dict[str, list[str]]` annotation
    promising locations, and the only caller iterated the keys and ignored them.
    Where a copy went is the one thing a reader can act on without the checkout the
    panel does not have."""
    diff = (_file("from.py", removed=["def review_llm(name):"])
            + _file("a.py", added=["def review_llm(name):"])
            + _file("b.py", added=["def review_llm(name):",
                                   "def review_llm(name):"]))
    dupes = pf.duplicate_definitions(pf._hunk_bodies(diff).def_sites)
    # Data, not rendered strings: a list of "b.py x2" would be the same misleading
    # annotation one step along — values a reader would take for paths.
    assert dupes == {"review_llm": Counter({"a.py": 1, "b.py": 2})}
    assert "! review_llm — added in a.py, b.py x2" in pf.move_manifest(diff)


def test_the_manifest_says_the_OTHER_half_of_the_trap_is_unseeable():
    """The canonical duplicate-copy accident leaves the original exactly where it
    was, in a file the merge never touched — so it appears in the diff as neither an
    added nor a deleted line, and no amount of parsing recovers it from `gh pr diff`.
    The section used to render "(none found …)" over that case, which reads as
    checked-and-clean for precisely the failure it is named after. It now says which
    half it can see, and the unseeable half is listed with the two other facts that
    need the branch."""
    text = pf.move_manifest(SPLIT)
    assert "DEFINITIONS THIS CHANGE ADDS IN MORE THAN ONE PLACE" in text
    assert "half of the duplicate-copy trap" in text
    assert "in a file this change never touches" in text
    assert "checking it needs the branch" in text


def test_the_duplicate_disclaimer_names_the_spellings_it_did_NOT_look_at():
    """"A pattern that matches nothing is worse than an absent section" is this
    module's own rule, and it applies one level finer than a language name: a reader
    told "JavaScript/TypeScript" cannot know that a class method was not looked
    at."""
    text = pf.move_manifest(SPLIT)
    assert "Class and object METHODS" in text
    assert "wraps onto a second line" in text


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


def test_a_repeated_residue_line_does_not_consume_the_whole_listing():
    """`sorted(bodies.elements(), …)` expanded the Counter, so one long boilerplate
    line added five hundred times was quoted up to `MANIFEST_RESIDUE_LINES` times —
    and since the sort is longest-first it crowded out every unique line behind it,
    with the elision count then reporting "… and N more, not listed" for exactly the
    lines a reviewer needed to see. The repetition is information and is kept as a
    multiplier; what it must not be is the whole budget."""
    boiler = "        raise NotImplementedError('this subclass owes an implementation')"
    unique = [f"        if guard_{i} is None: return None   # dropped on the way across"
              for i in range(5)]
    diff = (_file("from.py", removed=BODY + [boiler] * 500 + unique)
            + _file("to.py", added=BODY))
    text = pf.move_manifest(diff)
    assert f"{boiler.strip()}   (x500)" in text
    for line in unique:
        assert line.strip() in text, "a unique residue line was crowded out"
    assert "not listed" not in text.split("WHAT DID NOT SURVIVE")[1].split("WHAT CHANGED")[0]


def test_the_residue_HEADER_says_which_of_its_two_numbers_the_listing_counts():
    """The header counted OCCURRENCES and everything under it counts DISTINCT
    lines: `_listing` iterates distinct bodies, carries repetition as `xN` and
    elides against the distinct total. So 500 copies of one line plus 5 unique ones
    rendered as "505 line(s)" over six quoted entries and no "and N more" note — two
    numbers in different units in adjacent lines, with nothing saying which was
    which."""
    boiler = "        raise NotImplementedError('this subclass owes an implementation')"
    unique = [f"        if guard_{i} is None: return None" for i in range(5)]
    diff = (_file("from.py", removed=BODY + [boiler] * 500 + unique)
            + _file("to.py", added=BODY))
    text = pf.move_manifest(diff)
    head = next(ln for ln in text.splitlines()
                if ln.startswith("WHAT DID NOT SURVIVE"))
    assert "505 line(s), 6 distinct" in head, head


def test_an_UNREPEATED_residue_does_not_state_the_same_number_twice():
    """Both figures only when they differ. On the ordinary residue — every line
    unique — "12 line(s), 12 distinct" is a second number that says nothing and one
    more thing between the reader and the listing."""
    orphans = [f"unique orphan {i}" for i in range(12)]
    diff = _file("from.py", removed=BODY + orphans) + _file("to.py", added=BODY)
    head = next(ln for ln in pf.move_manifest(diff).splitlines()
                if ln.startswith("WHAT DID NOT SURVIVE"))
    assert head.endswith("(12 line(s))"), head


def test_a_file_with_no_counted_lines_is_not_labelled_BOTH():
    """A mode change, a binary, a rename git recorded without content: `a == r == 0`,
    which `diff_shape` reasons about explicitly as being NEITHER one-sided nor the
    other. The row rendered `path: +0 / -0  [both]`, asserting the file had gained and
    lost text when it did neither."""
    diff = (_file("moved_from.py", removed=BODY) + _file("moved_to.py", added=BODY)
            + "diff --git a/chmod.sh b/chmod.sh\nold mode 100644\nnew mode 100755\n")
    text = pf.move_manifest(diff)
    assert "chmod.sh: +0 / -0  [no counted lines]" in text
    assert "[both]" not in text


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

    def fake_review(name, model, prompt, effort="", **kw):
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


def test_a_refused_round_records_the_CI_gate_and_not_the_SONAR_one(monkeypatch,
                                                                   tmp_path,
                                                                   capsys):
    """CI is size-independent, costs one API read and consumes no seat's budget — the
    one part of a round a 763 KB diff cannot make useless. A refusal that discarded it
    left `/panel-review-pr` told to stop the cycle with nothing said about a red build.
    Sonar is a panel MEMBER with a `ran: false` row below, so it is not dispatched, and
    the notice says that gate was not evaluated rather than leaving its default to read
    as a pass."""
    cfg = {**_panel(max_diff_chars=len(FRESH) // 10),
           "reviewers": {"claude": {"enabled": True, "model": "sonnet"}}}
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(pf, "seat_installed", ALL_HERE)
    monkeypatch.setattr(panel_core, "sh", gh_stub(diff=FRESH))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: pytest.fail("a seat was dispatched"))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("FAIL", ["build"], None))
    out = tmp_path / "refused.json"
    assert panel.run("e2e", 137, post=False, json_file=str(out), record=False) == 0
    got = json.loads(out.read_text())
    assert got["preflight"]["verdict"] == "refuse"
    assert got["ci_status"] == "FAIL"
    assert got["ci_failing"] == ["build"]
    assert got["sonar_gate"] == "skipped", "not read, and not claimed either way"
    printed = capsys.readouterr().out
    assert "CI: FAILED" in printed
    assert "SonarCloud: NOT evaluated" in printed


def test_an_UNREADABLE_CI_gate_on_a_refusal_is_labelled_ONCE(monkeypatch, tmp_path,
                                                             capsys):
    """`review_ci` returns its skip reason already labelled — `ci: TimeoutExpired` —
    because the ordinary path puts that string straight into `PanelResult.skipped`,
    which is parsed board-side as "<reviewer>: <reason>". Neither consumer on the
    refusal path parses it that way, and both added a second label to the first:
    `config_notes` rendered "⚠️ config: ci: TimeoutExpired", filing a CI outage as a
    config key called `ci`, and `_ci_line` rendered "could NOT be read (ci: timed
    out)". Both read as though something had been mislabelled, which is the
    impression a refusal notice can least afford."""
    cfg = {**_panel(max_diff_chars=len(FRESH) // 10),
           "reviewers": {"claude": {"enabled": True, "model": "sonnet"}}}
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(pf, "seat_installed", ALL_HERE)
    monkeypatch.setattr(panel_core, "sh", gh_stub(diff=FRESH))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: pytest.fail("a seat was dispatched"))
    monkeypatch.setattr(panel, "review_ci",
                        lambda *a: ("unknown", [], "ci: TimeoutExpired"))
    out = tmp_path / "refused.json"
    assert panel.run("e2e", 137, post=False, json_file=str(out), record=False) == 0
    got = json.loads(out.read_text())
    note = next(n for n in got["config_notes"] if "TimeoutExpired" in n)
    assert note == "CI could not be read — TimeoutExpired"
    printed = capsys.readouterr().out
    assert "could NOT be read (TimeoutExpired)" in printed
    assert "ci: TimeoutExpired" not in printed
    # And it is still not filed as a reviewer that failed to run.
    assert not any("ci" in r for r in got["reviewers"])


def _wide_move(lines):
    """A move-shaped diff whose BYTE count runs well ahead of its character count,
    for the renderers that have to name which of the two they measured."""
    body = [f"    # 全角の行 {i} — 相当な長さの説明があります" for i in range(lines)]
    return (_file("big.py", removed=body)
            + _file("part_a.py", added=body[:lines // 2])
            + _file("part_b.py", added=body[lines // 2:]))


def test_every_BANNER_on_a_byte_measured_round_names_the_unit(monkeypatch, tmp_path,
                                                              capsys):
    """The verdict knew, and none of its renderers did. `preflight` computed `over`
    from UTF-8 bytes whenever antigravity's argv ceiling bound, while the manifest
    banner, the `--force` banner and every per-seat skip reason printed
    `shape.chars` beside the word "chars" — so the report stated a character count
    against a byte ceiling and a multiple that matched neither."""
    argv = panel_core.ARGV_PROMPT_MAX_BYTES
    diff = _wide_move(1400)
    s = pf.diff_shape(diff)
    assert s.nbytes > argv > s.chars, f"fixture drifted: {s}"

    # A manifest round: the banner above the findings.
    _, got, _ = _run(monkeypatch, tmp_path, diff, _panel(), seats=("antigravity",))
    assert got["preflight"]["verdict"] == "manifest"
    assert got["preflight"]["cap_unit"] == "bytes"
    printed = capsys.readouterr().out
    assert f"diff is {s.nbytes:,} bytes against antigravity's {argv:,}" in printed

    # The same round forced: the `--force` banner.
    _, got, _ = _run(monkeypatch, tmp_path, diff, _panel(), seats=("antigravity",),
                     force=True)
    assert got["preflight"]["would_have"] == "manifest"
    assert f"{s.nbytes:,}-byte diff against a {argv:,}-byte ceiling" \
        in capsys.readouterr().out


def test_a_byte_measured_REFUSAL_says_bytes_in_every_seats_skip_reason(monkeypatch,
                                                                      tmp_path):
    """`skipped` is parsed board-side and `reviewers[n]["skip"]` is the structured
    twin, so a per-seat reason stating characters against a byte ceiling disagrees
    with `skip_reason` in the same payload — and does it in the table that answers
    which reviewer finds the real issues."""
    argv = panel_core.ARGV_PROMPT_MAX_BYTES
    diff = _wide_move(3000) + FRESH * 30      # over the multiple, not move-shaped
    s = pf.diff_shape(diff)
    assert not s.is_move() and s.nbytes > argv * 3, f"fixture drifted: {s}"
    _, got, _ = _run(monkeypatch, tmp_path, diff, _panel(),
                     seats=("antigravity", "claude"))
    assert got["preflight"]["verdict"] == "refuse"
    want = f"({s.nbytes:,} bytes against {argv:,})"
    assert all(want in row for row in got["skipped"]), got["skipped"]
    assert all(want in r["skip"] for r in got["reviewers"].values())


def test_a_refused_SCOPED_round_records_what_it_was_going_to_review(monkeypatch,
                                                                   tmp_path):
    """The refuse payload is careful to carry `head_sha`/`merge_base`/`base_sha` so
    round r+1 can anchor, and recorded no `scope` and no `since_sha` — so a refusal
    under `--scope increment --since <sha>` published the field defaults and nothing
    told it apart from a refused whole-PR round. `load_baseline` reads
    `payload.get("scope") or "pr"`, which is harmless today only because
    `reviewers_ran == []` routes the round to `unread_rounds` before scope matters: a
    coupling nothing states and nothing enforces."""
    increment = _file("fix.py", added=BODY)
    cfg = {"github": "acme/board", "path": "/tmp/b", "name": "board",
           "_rules_baseline": ".harness-rules.sample",
           "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
           # Tight enough that the INCREMENT itself is far over — the verdict is
           # weighed on the target, so refusing takes a target that does not fit.
           "review_panel": {"max_diff_chars": len(increment) // 10}}
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(pf, "seat_installed", ALL_HERE)
    monkeypatch.setattr(panel_core, "sh", gh_stub(diff=FRESH + increment))
    monkeypatch.setattr(panel_scope, "fetch_increment",
                        lambda repo, a, b: (increment, ""))
    monkeypatch.setattr(panel_scope, "compare_facts",
                        lambda *a: {"commits": 1, "files": 1, "additions": 200,
                                    "deletions": 0})
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: pytest.fail("a seat was dispatched"))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    out = tmp_path / "r2.json"
    assert panel.run("e2e", 137, post=False, json_file=str(out), record=False,
                     round_no=2, baseline=[_payload(tmp_path, "r1.json")],
                     max_rounds=3) == 0
    got = json.loads(out.read_text())
    assert got["preflight"]["verdict"] == "refuse"
    assert got["scope"] == "increment", "what the round was going to review"
    assert got["since_sha"] == "a" * 40, "and the commit it was measured from"


def test_a_scoped_round_is_NOT_refused_for_the_size_of_its_CONTEXT(monkeypatch,
                                                                  tmp_path):
    """The verdict weighs the review TARGET and deliberately not the context tiers
    that travel with it, and this pins the contract rather than leaving it to the
    comment. The target is the tier never cut while anything else is present, so
    "would a seat read a useless fraction of it" is a question about the target;
    losing context under a tight budget is the DESIGN of increment scope, is labelled
    in the prompt, and already vetoes a confident stop on its own. Refusing here would
    refuse the case scoping exists to make cheap."""
    increment = _file("fix.py", added=["    the_fix()"])
    pr = FRESH * 4 + increment
    cfg = {"github": "acme/board", "path": "/tmp/b", "name": "board",
           "_rules_baseline": ".harness-rules.sample",
           "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
           # Enough to pay for the increment AND the scoped frame — which is over a
           # kilobyte and cannot be cut — so the target is not truncated and the
           # squeeze lands on the context, which is the regime this pins. Far under
           # the context itself: weighed on target + near + far this round would be
           # ~20x over and refused.
           "review_panel": {"max_diff_chars": 4_000}}
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(pf, "seat_installed", ALL_HERE)
    monkeypatch.setattr(panel_core, "sh", gh_stub(diff=pr))
    monkeypatch.setattr(panel_scope, "fetch_increment",
                        lambda repo, a, b: (increment, ""))
    monkeypatch.setattr(panel_scope, "compare_facts",
                        lambda *a: {"commits": 1, "files": 1, "additions": 1,
                                    "deletions": 0})
    monkeypatch.setattr(panel, "review_llm",
                        lambda n, m, prompt, effort="", **kw:
                        panel.ReviewerRun([], None, 10, None))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, ""))
    out = tmp_path / "r2.json"
    assert panel.run("e2e", 137, post=False, json_file=str(out), record=False,
                     round_no=2, baseline=[_payload(tmp_path, "r1.json")],
                     max_rounds=3) == 0
    got = json.loads(out.read_text())
    assert got["reviewed"] is True
    assert got["preflight"]["verdict"] == "run"
    assert got["preflight"]["shape"]["chars"] == len(increment)
    # The context the budget could not pay for is NOT silent: it is a veto in its own
    # right, which is the half that makes the exemption above honest.
    assert got["context_chars"] > got["preflight"]["shape"]["chars"] * 100
    assert (got["context_chars"] + got["diff_chars"]) / 4_000 > 3, \
        "the whole prompt IS past the refusal multiple — the target is not"
    assert got["reviewers"]["claude"]["truncated"] is False, "the target fitted whole"
    assert any("saw only part of the PR behind the increment" in v
               for v in got["round_stop"]["veto"])


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
                        lambda n, m, p, effort="", **kw: panel.ReviewerRun([], None, 10, None))
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
                        lambda n, m, p, effort="", **kw: panel.ReviewerRun([], None, 10, None))
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
    before the diff is even fetched.

    Driven rather than asserted on the default dict. This test used to be one
    assertion on `_payload_defaults()`, which pins the DEFAULT and not the path: if
    the skip branch were moved below the `preflight` call, or began stamping a
    verdict of its own, that assertion would have gone on passing unchanged.
    """
    assert panel._payload_defaults()["preflight"] is None, "the default it relies on"
    calls: list = []
    seen: dict = {"prompts": {}}
    cfg = {"github": "acme/board", "path": "/tmp/acme-board", "name": "board",
           "_rules_baseline": ".harness-rules.sample",
           "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
           # A ceiling the fixture diff is far past, so a round that DID reach the
           # verdict would refuse and stamp one — the skip has to beat it there.
           "review_panel": {"max_diff_chars": 10,
                            "skip_title_patterns": ["^chore: promote"]}}
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(pf, "seat_installed", ALL_HERE)
    monkeypatch.setattr(panel_core, "sh",
                        gh_stub(meta={"title": "chore: promote main to prod"},
                                diff=FRESH, calls=calls))
    monkeypatch.setattr(panel, "review_llm",
                        lambda n, m, prompt, effort="":
                        seen["prompts"].setdefault(n, prompt)
                        or panel.ReviewerRun([], None, 10, None))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    out = tmp_path / "skipped.json"
    assert panel.run("e2e", 137, post=False, json_file=str(out), record=False) == 0
    got = json.loads(out.read_text())
    assert got["preflight"] is None, "never weighed, which is not the same as `run`"
    assert got["skip_reason"] == "title matches skip pattern /^chore: promote/"
    assert seen["prompts"] == {}, "and no seat was prompted"
    # It returns before the diff is even fetched, which is what makes it the cheap
    # path — and the reason a verdict there would have nothing to weigh.
    assert not any("diff" in c for c in calls), calls


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
           "_rules_baseline": ".harness-rules.sample",
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
    def reviewer(name, model, prompt, effort="", **kw):
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
           "_rules_baseline": ".harness-rules.sample",
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
                        lambda n, m, p, effort="", **kw: panel.ReviewerRun([], None, 10, None))
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
           "_rules_baseline": ".harness-rules.sample",
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
                        lambda n, m, p, effort="", **kw: panel.ReviewerRun([], None, 10, None))
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
    # The round's OWN manifest gap is a separate sentence, because "no reviewer read
    # it" is false of a manifest round: every seat ran.
    assert any("read a MANIFEST of a move" in v for v in got["round_stop"]["veto"])
    # And the ONE veto in that block still gated on `review.scope` rather than on the
    # captured target scope cannot fire here, whichever flag guards it. It keys off
    # `short_context`, which compares what each seat was sent against the CONTEXT
    # TIERS of the material it was sent — and a manifest's material is a whole-target
    # composition whose `near` and `far` are both "", so the comparison is `sent < 0`
    # for every seat. Converting its guard would change nothing and would imply this
    # veto can fire on a manifest round, which it cannot.
    assert not any("saw only part of the PR behind the increment" in v
                   for v in got["round_stop"]["veto"])


def test_the_shared_reply_contract_does_not_tell_a_manifest_reviewer_to_judge_a_diff():
    """`_FINDINGS_ENVELOPE` is appended VERBATIM to both prompts, which is what lets
    `SCHEMA_ECHOES` recognise either one's own example rather than filing it as a
    finding — so forking it would be the wrong fix. But it said "only if the diff is
    genuinely flawless" and "a file the diff does not include" under a prompt whose
    first sentence is "you are deliberately NOT being given its diff", contradicting it
    on the one point a manifest round hinges on."""
    assert "the material below is genuinely flawless" in panel_core.MOVE_MANIFEST_PROMPT
    assert "a file\n  the material below does not include" in panel_core.REVIEW_PROMPT
    for prompt in (panel_core.REVIEW_PROMPT, panel_core.MOVE_MANIFEST_PROMPT):
        assert "if the diff is genuinely flawless" not in prompt
    # Still one string, shared: two hand-kept copies are one edit away from a manifest
    # run in which the example parses as a finding nobody made.
    assert panel_core._FINDINGS_ENVELOPE in panel_core.REVIEW_PROMPT
    assert panel_core._FINDINGS_ENVELOPE in panel_core.MOVE_MANIFEST_PROMPT


def test_the_manifest_prompt_asks_only_for_the_HALF_of_the_trap_it_can_see():
    """The prompt told the seat that "a move that keeps BOTH copies" is what section 3
    lists, and the section cannot see that case at all: the surviving original is in a
    file the diff never touches. A brief that promises evidence the manifest does not
    carry spends the round's most valuable instruction on a false premise."""
    assert "Names this change ADDS in more" in panel_core.MOVE_MANIFEST_PROMPT
    assert "seen from a diff at all" in panel_core.MOVE_MANIFEST_PROMPT


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
    # Bound by NAME, not by position — and read BEFORE `run` is replaced, because
    # `inspect` on the double reports the double's own `*a, **k`. This asserted on
    # `args[-1]`, which was `force` until v2.51 added a parameter after it, and then
    # the test turned on somebody else's argument order rather than on whether
    # `--force` arrives at all.
    import inspect
    at = list(inspect.signature(panel.run).parameters).index("force")
    monkeypatch.setattr(panel, "run", lambda *a, **k: got.update(args=a) or 0)
    monkeypatch.setattr(sys, "argv", ["panel.py", "--pr", "137", "--force"])
    assert panel.main() == 0
    assert got["args"][at] is True
