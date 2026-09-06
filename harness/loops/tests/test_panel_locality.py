"""Matching a finding across a round boundary by WHERE IT IS (#771, #774).

The identity these tests defend replaces a hash of the reviewing seat's own
wording, which is unstable across exactly the boundary it was being asked about:
#750 measured five of five `--assessed` answers on `lexray#1611` matching nothing.
So the tests are written against the two things that move between rounds and must
not break a match — the MODEL's words and the CODE's line numbers — and against
the three ways a locality matcher goes wrong in silence:

- a hunk parser that counts body lines and so mis-numbers every hunk after a
  `\\ No newline` marker or an added line that looks like a diff header,
- a path normal form that only one side of the comparison agrees with, so a
  finding and the hunk it sits on are filed under two keys,
- a match that is permissive where it cannot be certain — a finding with no line
  range falling back to path equality, or two prior findings both claiming one
  current one — which reads as convergence the cycle did not earn.

`finding_kind` is here too. It is a path classification and nothing else, and the
tests pin the ambiguous case (`harness/commands/*.md`) so a later reader can see
it was chosen rather than fallen into.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel_locality as loc  # noqa: E402


def finding(key: str, file: str, line=None, **extra) -> dict:
    """A finding row in the shape the panel puts on the wire (`Canonical.as_dict`)
    — a key, a file, a line, and words nothing here is allowed to read."""
    row = {"key": key, "file": file, "title": f"something wrong in {file}",
           "severity": "major"}
    if line is not None:
        row["line"] = line
    row.update(extra)
    return row


# ------------------------------------------------------------------ parsing a diff

#: A real `git diff` over two files: one edited in two places, one renamed and
#: edited. Kept as literal text rather than generated, because the parser's whole
#: job is to read bytes a caller hands it and a fixture that came out of `git`
#: would make the test as impure as the module refuses to be.
DIFF = """diff --git a/harness/loops/panel_rounds.py b/harness/loops/panel_rounds.py
index 1111111..2222222 100644
--- a/harness/loops/panel_rounds.py
+++ b/harness/loops/panel_rounds.py
@@ -327,7 +327,7 @@ def _defect_title(reports):
     titles = sorted(f.title for f in reports)
-    return titles[0]
+    return titles[0] if titles else "(untitled)"
@@ -900,3 +900,6 @@ def tail():
     return None
+
+
+# a new paragraph of three lines
diff --git a/harness/loops/old_name.py b/harness/loops/new_name.py
similarity index 94%
rename from harness/loops/old_name.py
rename to harness/loops/new_name.py
index 3333333..4444444 100644
--- a/harness/loops/old_name.py
+++ b/harness/loops/new_name.py
@@ -10,4 +10,4 @@ def thing():
-    return 1
+    return 2
"""


def test_a_hunk_header_becomes_the_new_side_range_it_names():
    scope = loc.parse_diff_scope(DIFF)
    assert scope["harness/loops/panel_rounds.py"][0] == (327, 333)


def test_several_hunks_in_one_file_each_become_their_own_range():
    scope = loc.parse_diff_scope(DIFF)
    # Two ranges and not one span from the first line to the last: the 570 lines
    # between them are untouched, and a matcher told otherwise would attribute a
    # finding at line 600 to a fix pass that never went near it.
    assert scope["harness/loops/panel_rounds.py"] == [(327, 333), (900, 905)]


def test_a_rename_is_keyed_under_the_path_the_file_now_has():
    scope = loc.parse_diff_scope(DIFF)
    assert "harness/loops/new_name.py" in scope
    # The old name carries no ranges at all. Every line number a finding holds is
    # a line number in the file as it stands now, so a scope keyed on the pre-
    # rename path answers nothing for any of them.
    assert "harness/loops/old_name.py" not in scope


def test_a_hunk_header_with_no_count_is_a_single_line():
    scope = loc.parse_diff_scope("--- a/x.py\n+++ b/x.py\n@@ -4 +4 @@\n-old\n+new\n")
    assert scope == {"x.py": [(4, 4)]}


def test_a_no_newline_marker_does_not_move_the_hunk_after_it():
    diff = ("--- a/x.py\n+++ b/x.py\n"
            "@@ -1,2 +1,2 @@\n-a\n+b\n\\ No newline at end of file\n"
            "@@ -80,1 +80,2 @@\n c\n+d\n")
    # The marker is not a line of the file. A parser that counted body lines to
    # find the second hunk would place it one line late, and every finding in the
    # tail of the file would then match the wrong side of the boundary.
    assert loc.parse_diff_scope(diff) == {"x.py": [(1, 2), (80, 81)]}


def test_a_hunk_that_only_deletes_is_located_where_the_lines_were():
    scope = loc.parse_diff_scope("--- a/x.py\n+++ b/x.py\n@@ -40,3 +39,0 @@\n-a\n-b\n-c\n")
    # New-side count zero: the lines are gone, so there is no span to point at.
    # It still contributes the one line they sat above, because "the fix pass
    # deleted the code this finding was about" is precisely what a cross-round
    # question needs to be able to see.
    assert scope == {"x.py": [(39, 39)]}


def test_a_deleted_file_contributes_no_ranges():
    diff = ("diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n"
            "--- a/gone.py\n+++ /dev/null\n@@ -1,3 +0,0 @@\n-a\n-b\n-c\n")
    assert loc.parse_diff_scope(diff) == {}


def test_a_diff_of_a_diff_does_not_re_key_on_its_own_body_lines():
    # The added lines here have `-- a/decoy.py` and `++ b/decoy.py` as their
    # CONTENT, so they render one `-`/`+` wider and read as diff headers to
    # anything matching on the prefix alone. This repo's own fixtures are full of
    # diff text, so it is a live case and not a contrived one.
    diff = ("diff --git a/notes.md b/notes.md\n--- a/notes.md\n+++ b/notes.md\n"
            "@@ -1,2 +1,4 @@\n intro\n+-- a/decoy.py\n+++ b/decoy.py\n outro\n")
    assert loc.parse_diff_scope(diff) == {"notes.md": [(1, 4)]}


def test_an_empty_diff_has_no_scope():
    assert loc.parse_diff_scope("") == {}


# ------------------------------------------------------------ path normalisation

@pytest.mark.parametrize("spelling", [
    "harness/loops/panel.py",
    "./harness/loops/panel.py",
    "a/harness/loops/panel.py",
    "b/harness/loops/panel.py",
    "harness\\loops\\panel.py",
    "  harness/loops/panel.py  ",
])
def test_every_spelling_of_one_path_normalises_to_one_key(spelling):
    assert loc.normalize_path(spelling) == "harness/loops/panel.py"


def test_a_finding_and_a_diff_hunk_meet_under_the_same_key():
    scope = loc.parse_diff_scope("--- a/app/api/reviews.py\n+++ b/app/api/reviews.py\n"
                                 "@@ -10,2 +10,2 @@\n-a\n+b\n")
    # The diff spells it `b/app/api/reviews.py` and the reviewer spelled it
    # `./app/api/reviews.py`. One normal form or the two never meet.
    assert loc.line_intersects_hunks("./app/api/reviews.py", 10, 10, scope)


def test_an_absolute_path_is_left_absolute():
    # The repo root is not knowable from inside a pure function, so guessing at
    # one would fold two checkouts of two repos onto a single key.
    assert loc.normalize_path("/home/rich/src/qb/app/x.py") == "/home/rich/src/qb/app/x.py"


# ------------------------------------------------------------------ finding bounds

def test_a_finding_with_one_line_is_a_one_line_span():
    assert loc.finding_line_bounds(finding("k", "x.py", 42)) == (42, 42)


def test_an_explicit_start_and_end_are_read_as_the_span():
    assert loc.finding_line_bounds({"start_line": 10, "end_line": 20}) == (10, 20)


def test_a_line_range_pair_is_read_as_the_span():
    assert loc.finding_line_bounds({"line_range": [10, 20]}) == (10, 20)


def test_a_span_stated_backwards_is_put_back_in_order():
    # The reviewer named the place; refusing it over the order of two numbers
    # would throw away a finding that is perfectly locatable.
    assert loc.finding_line_bounds({"line_range": (20, 10)}) == (10, 20)


def test_a_finding_that_names_no_line_has_no_bounds():
    assert loc.finding_line_bounds(finding("k", "x.py")) is None
    assert loc.is_locatable(finding("k", "x.py")) is False


def test_a_finding_that_names_no_file_is_not_locatable_either():
    assert loc.is_locatable(finding("k", "", line=10)) is False


def test_a_boolean_is_not_a_line_number():
    # `True` is an `int` to Python and would otherwise become line 1 — the same
    # quiet default the whole module refuses.
    assert loc.finding_line_bounds({"line": True}) is None


# ------------------------------------------------------- slack, at the boundary

SCOPE = {"x.py": [(100, 110)]}


def test_a_finding_exactly_slack_lines_before_a_hunk_intersects_it():
    assert loc.line_intersects_hunks("x.py", 97, 97, SCOPE) is True


def test_a_finding_one_line_past_the_slack_does_not():
    assert loc.line_intersects_hunks("x.py", 96, 96, SCOPE) is False


def test_a_finding_exactly_slack_lines_after_a_hunk_intersects_it():
    assert loc.line_intersects_hunks("x.py", 113, 113, SCOPE) is True


def test_a_finding_one_line_past_the_slack_after_a_hunk_does_not():
    assert loc.line_intersects_hunks("x.py", 114, 114, SCOPE) is False


def test_the_slack_can_be_widened_by_the_caller():
    assert loc.line_intersects_hunks("x.py", 96, 96, SCOPE, slack=4) is True


def test_a_finding_with_no_line_does_not_intersect_a_hunk():
    # Even though the file itself changed. "Somewhere in this file" is not "here",
    # and reading it as "here" attributes every file-level finding on a rewritten
    # file to the fixer.
    assert loc.line_intersects_hunks("x.py", None, None, SCOPE) is False
    # The file-level question is still available to a caller that wants it, and
    # has to be asked out loud:
    assert loc.normalize_path("./x.py") in SCOPE


def test_a_file_the_diff_never_touched_intersects_nothing():
    assert loc.line_intersects_hunks("other.py", 100, 100, SCOPE) is False


# --------------------------------------------------- the same finding, next round

def test_a_finding_whose_lines_shifted_by_two_is_the_same_finding():
    # A fix pass that added a guard clause above it. The defect did not move.
    before = finding("aaa", "app/api/reviews.py", 200)
    after = finding("bbb", "app/api/reviews.py", 202)
    assert loc.same_finding(before, after) is True


def test_a_finding_whose_lines_shifted_by_thirty_is_not():
    # Thirty lines is a restructured function, and calling the new finding a
    # continuation of the old one would be a guess dressed as a match.
    before = finding("aaa", "app/api/reviews.py", 200)
    after = finding("bbb", "app/api/reviews.py", 230)
    assert loc.same_finding(before, after) is False


def test_a_reworded_finding_on_the_same_lines_still_matches():
    before = finding("aaa", "app/api/reviews.py", 200, title="unchecked index")
    after = finding("bbb", "app/api/reviews.py", 200,
                    title="this list access can raise IndexError")
    # Different words, different key, same defect — the case that broke five of
    # five cross-round answers on `lexray#1611`.
    assert before["key"] != after["key"]
    assert loc.same_finding(before, after) is True


def test_two_findings_on_the_same_lines_of_different_files_never_match():
    assert loc.same_finding(finding("a", "app/x.py", 10),
                            finding("b", "harness/x.py", 10)) is False


def test_a_finding_with_no_line_range_matches_nothing():
    unlocated = finding("aaa", "harness/loops/panel_rounds.py")
    located = finding("bbb", "harness/loops/panel_rounds.py", 4000)
    assert loc.same_finding(unlocated, located) is False
    assert loc.same_finding(located, unlocated) is False


def test_two_findings_with_no_line_range_do_not_match_each_other():
    # The tempting fallback is path equality, and on an 8,549-line file it would
    # declare every unlocated finding in it to be every other one.
    a = finding("aaa", "harness/loops/panel_rounds.py")
    b = finding("bbb", "harness/loops/panel_rounds.py")
    assert loc.same_finding(a, b) is False


# ------------------------------------------------------------- matching a round

def test_each_prior_finding_maps_to_the_current_finding_on_its_lines():
    prior = [finding("p1", "app/x.py", 10), finding("p2", "app/y.py", 500)]
    current = [finding("c1", "app/y.py", 502), finding("c2", "app/x.py", 11)]
    assert loc.match_across_rounds(prior, current) == {"p1": "c2", "p2": "c1"}


def test_a_prior_finding_that_matches_nothing_is_absent_from_the_map():
    prior = [finding("p1", "app/x.py", 10), finding("p2", "app/x.py", 900)]
    current = [finding("c1", "app/x.py", 10)]
    got = loc.match_across_rounds(prior, current)
    # Absent, not present-and-empty: `key in matched` has to be the whole
    # question, or a caller reads a miss as a match by forgetting to test it.
    assert got == {"p1": "c1"}
    assert "p2" not in got


def test_two_prior_findings_cannot_both_claim_one_current_finding():
    prior = [finding("p1", "app/x.py", 10), finding("p2", "app/x.py", 12)]
    current = [finding("c1", "app/x.py", 11)]
    got = loc.match_across_rounds(prior, current)
    assert len(got) == 1
    # The lower span is walked first, so the claim is settled and not raced.
    assert got == {"p1": "c1"}


def test_the_current_finding_with_the_most_overlap_wins_the_claim():
    prior = [{"key": "p1", "file": "app/x.py", "line_range": [100, 110]}]
    current = [{"key": "near", "file": "app/x.py", "line_range": [105, 106]},
               {"key": "wide", "file": "app/x.py", "line_range": [108, 120]}]
    # `near` sits inside the prior span but shares two lines with it; `wide`
    # shares three. Closest overlap wins, and it is the same answer whichever
    # order the two arrived in.
    assert loc.match_across_rounds(prior, current) == {"p1": "wide"}
    assert loc.match_across_rounds(prior, list(reversed(current))) == {"p1": "wide"}


def test_the_map_is_the_same_whatever_order_the_rounds_are_held_in():
    prior = [finding("p1", "app/x.py", 10), finding("p2", "app/x.py", 60),
             finding("p3", "app/y.py", 10)]
    current = [finding("c1", "app/x.py", 11), finding("c2", "app/x.py", 61),
               finding("c3", "app/y.py", 12)]
    forward = loc.match_across_rounds(prior, current)
    backward = loc.match_across_rounds(list(reversed(prior)), list(reversed(current)))
    assert forward == backward == {"p1": "c1", "p2": "c2", "p3": "c3"}


def test_a_finding_with_no_key_is_skipped_on_either_side():
    prior = [finding("", "app/x.py", 10), finding("p2", "app/x.py", 60)]
    current = [finding("", "app/x.py", 10), finding("c2", "app/x.py", 60)]
    # The key is the only handle the result has; minting one here would create an
    # identity no other layer could resolve back to a finding.
    assert loc.match_across_rounds(prior, current) == {"p2": "c2"}


def test_a_prior_finding_with_no_line_range_matches_nothing_in_the_round():
    prior = [finding("p1", "app/x.py")]
    current = [finding("c1", "app/x.py", 10), finding("c2", "app/x.py")]
    assert loc.match_across_rounds(prior, current) == {}


def test_matching_two_empty_rounds_is_an_empty_map():
    assert loc.match_across_rounds([], []) == {}


# ------------------------------------------------------- what a finding is about

@pytest.mark.parametrize("path,kind", [
    # Production is the catch-all, and it is the strict population on purpose.
    ("app/api/reviews.py", loc.KIND_PRODUCTION),
    ("harness/loops/panel_rounds.py", loc.KIND_PRODUCTION),
    ("migrations/0012_defect_key.py", loc.KIND_PRODUCTION),
    ("app/static/board.html", loc.KIND_PRODUCTION),
    ("harness/hm-module.nix", loc.KIND_PRODUCTION),
    ("docker-compose.yml", loc.KIND_PRODUCTION),
    ("", loc.KIND_PRODUCTION),
    # …including a module whose NAME contains a prose word. The basename rules
    # match the whole name and never a stem.
    ("app/license.py", loc.KIND_PRODUCTION),

    # Test, by a directory above the file…
    ("tests/test_reviews.py", loc.KIND_TEST),
    ("harness/loops/tests/test_panel_scope.py", loc.KIND_TEST),
    ("mcp/tests/conftest.py", loc.KIND_TEST),
    ("harness/tests/create_worktree_nginx.test.sh", loc.KIND_TEST),
    ("app/fixtures/sample.json", loc.KIND_TEST),
    ("web/__tests__/thing.ts", loc.KIND_TEST),
    # …or by the file's own name, beside the code it exercises.
    ("app/conftest.py", loc.KIND_TEST),
    ("app/api/reviews_test.py", loc.KIND_TEST),
    ("web/panel.spec.ts", loc.KIND_TEST),
    # A fixture with a prose extension is test material: test rules run first.
    ("harness/loops/tests/fixtures/recorded.md", loc.KIND_TEST),

    # Prose, by extension…
    ("README.md", loc.KIND_PROSE),
    ("DEPLOY.md", loc.KIND_PROSE),
    ("harness/loops/README.md", loc.KIND_PROSE),
    ("notes.rst", loc.KIND_PROSE),
    ("notes.txt", loc.KIND_PROSE),
    # …by directory, which is what makes a changelog fragment prose rather than
    # the suffix it happens to carry…
    ("changelog.d/771.fix.md", loc.KIND_PROSE),
    ("docs/architecture/board.png", loc.KIND_PROSE),
    # …and by the conventional extensionless names.
    ("LICENSE", loc.KIND_PROSE),
    ("CONTRIBUTING", loc.KIND_PROSE),
])
def test_a_path_is_classified_by_what_a_finding_in_it_is_about(path, kind):
    assert loc.finding_kind(path) == kind


@pytest.mark.parametrize("path", [
    "harness/commands/panel.md",
    "harness/commands/fix-issue.md",
    "harness/claude/quarterback-workflow.md",
])
def test_a_command_file_is_prose_even_though_it_is_the_harness_contract(path):
    """The ambiguous case, pinned so it reads as a decision.

    These files ARE the loop — `panel.md` is executed, not documented — and the
    case for `production` is real. It is declined because the classification is
    about what a finding is about, not about how load-bearing the file is: a
    defect here is a defect in wording, the fix is an edit to text, and the same
    phrasing is restated across the seventeen files in that directory, so one edit
    produces N findings. That is #751's population exactly. Nothing is hidden by
    it — #774 keeps the pooled rate beside the split, so the finding still counts;
    it just cannot end a cycle on its own.
    """
    assert loc.finding_kind(path) == loc.KIND_PROSE


def test_a_path_is_classified_in_whatever_spelling_it_arrives_in():
    assert loc.finding_kind("./harness/loops/tests/test_x.py") == loc.KIND_TEST
    assert loc.finding_kind("b/changelog.d/771.fix.md") == loc.KIND_PROSE
