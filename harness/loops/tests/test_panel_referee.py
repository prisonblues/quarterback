'''#554: price a fix pass by whether anything can check it, not by how long it is.

Every dial the panel owned measured COST — lines (`low_severity_fix_lines`), chars and
multiples (`max_fix_growth`, `max_fix_growth_chars`), rounds (`max_rounds`), claimed
importance (the two severity floors). `escalate_on.fix_injection` (#489) is the one
that touches the real quantity, and it can only do so retrospectively: the findings
have to exist before the rate can be computed. Nothing priced work by whether any
mechanism in the loop could catch it being wrong.

The measurement, on lexray#1697 round 1, since reverted. A 93-line fix pass across
three files changed **no production logic at all** — the production file's entire
share of it was a docstring and a comment — and introduced ten findings, nine of them
in the test files it wrote and the tenth in the docstring it corrected. Red/green ran
and went red 4 of 4. It could not have caught any of the ten, because it asks whether
a new test detects the thing it was written for and never whether that test also opens
a socket, whether its assertion is sufficient, or whether it is as strong as the test
beside it.

That is structural rather than unlucky, and the sentence is the whole issue: **a
production fix has an external referee and a test fix has none, because nothing tests
a test.** A docstring fix has none either.

What is pinned here is the four things that make this a mechanism rather than an
opinion, in the shape `test_panel_injection.py` and `test_panel_volume.py` pin their
own rungs:

* the CLASSIFIER — churn split into production / test / prose by path AND by line, so
  that a pass which touched a production file only in its docstring is not read as
  partly refereed, which is the case the whole issue turns on;
* the DIAL — a FLAG and not a fraction, which is what makes it shippable as a gate on
  one cycle's evidence: #67 forbids a threshold nobody has calibrated, and a predicate
  on a fact has nothing to calibrate. A fraction would also be wrong, since the
  commonest healthy pass there is — a small production fix carrying a large regression
  test — is overwhelmingly unrefereed by proportion;
* the STOP — never dressed up as convergence, bounded to rule 1, behind
  `fix_injection` in the `reason` and ahead of the cap, and able to make exactly ONE
  transition: `go again` -> stop;
* the BUDGET — `unrefereed_line_weight`, the ex-ante half, which reprices the same
  work before it is written rather than stopping the cycle after it was.
'''

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_rounds  # noqa: E402
import panel_seats  # noqa: E402
import panel_propose  # noqa: E402
import harness_rules  # noqa: E402

DEFAULT_BLOCK = harness_rules.DEFAULTS["review_panel"]

#: The two triple-quote fences, built rather than written, so a fixture can carry a
#: real one without terminating the test file's own docstrings.
DQ, SQ = '"' * 3, "'" * 3

#: One Python file's diff header, so the fence fixtures below are only their hunks.
DIFF_HEADER = ("diff --git a/app/x.py b/app/x.py\n"
               "--- a/app/x.py\n+++ b/app/x.py\n")

#: The shape #554 measured, in miniature: a production file touched ONLY in its
#: docstring and its comments, beside the tests and prose that were the rest of it.
#: The production file is the load-bearing part — a path-only classifier calls this
#: pass partly refereed and the rule does not fire.
MEASURED = '''diff --git a/app/publish.py b/app/publish.py
--- a/app/publish.py
+++ b/app/publish.py
@@ -1,6 +1,8 @@
 def publish(x):
-    """Send it.
+    """Send it to the broker.
+
+    No database or network access.
     """
-    # old note
+    # the broker swallows exceptions
     return x
diff --git a/tests/test_publish.py b/tests/test_publish.py
--- a/tests/test_publish.py
+++ b/tests/test_publish.py
@@ -1,3 +1,6 @@
 def test_publish():
+    # patch every collaborator
+    with mock.patch("app.publish.redis"):
+        assert publish(1) == 1
     pass
diff --git a/docs/publish.md b/docs/publish.md
--- a/docs/publish.md
+++ b/docs/publish.md
@@ -1 +1,2 @@
 # Publish
+It sends to the broker.
'''

#: The commonest HEALTHY shape, and the one a proportion would have condemned: a
#: two-line production fix carrying a regression test three times its size.
HEALTHY = '''diff --git a/app/calc.py b/app/calc.py
--- a/app/calc.py
+++ b/app/calc.py
@@ -1,3 +1,3 @@
 def add(a, b):
-    return a - b
+    return a + b
diff --git a/tests/test_calc.py b/tests/test_calc.py
--- a/tests/test_calc.py
+++ b/tests/test_calc.py
@@ -1 +1,4 @@
 x
+def test_add():
+    assert add(1, 2) == 3
+    assert add(0, 0) == 0
'''


def _finding(severity="P2", key_from="boom", file="a.py", line=1):
    reported = [panel.Finding("claude", severity, file, line, key_from, "")]
    return panel.Canonical(id="34-F01", severity=severity, file=file, line=line,
                           synthesis=key_from, verdict="confirmed",
                           reported_by=reported)


def _unrefereed(production=0, test=7, prose=3, armed=True):
    return panel_rounds.referee_state(
        {"production": production, "test": test, "prose": prose}, armed)


# --------------------------------------------------------------------- the classifier

def test_a_pass_that_touched_production_ONLY_IN_ITS_PROSE_reads_as_unrefereed():
    """The case the whole issue turns on, and the reason this classifier is not
    `_guard_kind` alone. #554's pass DID touch a production file — so a path-only
    reading calls it partly refereed and the rule never fires — and its entire share
    of that file was a docstring and a comment, neither of which any mechanism in the
    loop can check."""
    got = panel_seats.referee_split(MEASURED)
    assert got["production"] == 0
    assert got["unrefereed"] == got["churn"] == 10
    assert got["share"] == 1.0


def test_a_small_production_fix_with_a_large_test_is_NOT_unrefereed():
    """The other half, and the reason the rule is a predicate rather than a
    proportion. This pass is 60% unrefereed by line and is exactly the work the panel
    wants: the production line has a referee, and red/green on the test beside it is
    what demonstrates the referee works."""
    got = panel_seats.referee_split(HEALTHY)
    assert got["production"] == 2
    assert got["share"] == 0.6
    assert panel_rounds.referee_state(got, True)["over"] is False


@pytest.mark.parametrize("path,text,kind", [
    ("app/x.py", "    return 1", "production"),
    ("app/x.py", "    # why", "prose"),
    ("app/x.py", "", "prose"),
    ("app/x.py", "       ", "prose"),
    ("app/x.py", "    x = 1  # trailing comments are not comments", "production"),
    ("app/x.py", '    marker = "# not a comment"', "production"),
    ("app/x.go", "\t// why", "prose"),
    ("app/x.go", "\tif err != nil {", "production"),
    # The `*` family, in BOTH directions. A Codex second opinion pointed out that the
    # parametrization used to carry only the docblock case, which verifies a broad
    # heuristic in the one direction that cannot hurt — the collision it left
    # untested read production as prose, which is the direction that FIRES the brake.
    # The `*` family, and every one of them is PRODUCTION here — because none of
    # these lines is inside a `/* … */`, and outside one a leading star is a pointer
    # store, a generator method or a universal selector. A docblock continuation is
    # prose by BLOCK STATE, which a per-line table cannot see; see
    # `test_a_star_line_is_prose_only_INSIDE_a_block_comment`.
    ("app/x.go", "\t * not inside a block, so not a continuation", "production"),
    ("app/x.c", "    *dst = value;", "production"),
    ("app/x.c", "    * dst = value;", "production"),
    ("app/x.c", "    *cursor++;", "production"),
    ("app/x.rs", "    *slot = transform(in);", "production"),
    ("web/x.js", "  *generator() {", "production"),
    ("web/x.js", "  * generator() {", "production"),
    ("web/x.css", "* { margin: 0 }", "production"),
    ("web/x.css", "/* a real comment */", "prose"),
    ("app/x.c", "    // a line comment", "prose"),
    ("app/x.sql", "-- why", "prose"),
    ("web/x.ts", " * jsdoc continuation", "production"),
    ("README.md", "# Heading", "prose"),
    ("docs/x.rst", "anything at all", "prose"),
    ("changelog.d/1.feature", "anything at all", "prose"),
    ("tests/test_x.py", "    assert x", "test"),
    ("tests/test_x.py", "    # even a comment in a test file", "test"),
    ("app/test_x.py", "    assert x", "test"),
    ("app/conftest.py", "    yield", "test"),
    ("app/x_test.go", "\tt.Fatal()", "test"),
    ("web/x.spec.ts", "  expect(1)", "test"),
    ("Makefile", "\tgo build", "production"),
])
def test_each_line_is_classified_by_its_path_and_then_by_itself(path, text, kind):
    """Path first, then the line. A test path claims every line in it including its
    comments — the split below `unrefereed` is for a READER deciding which kind of
    unrefereed work a pass did, and a comment in a test file is unrefereed under
    either heading.

    A trailing comment on a line of code is production, correctly: the line changes
    behaviour and red/green can see it. And a `#` inside a Python string is not a
    comment, which is why the marker table is keyed by suffix rather than sniffed out
    of the line. `Makefile` has no suffix at all and falls through to production
    rather than to a marker read out of a directory name."""
    diff = (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1 +1,2 @@\n x\n+{text}\n")
    got = panel_seats.referee_split(diff)
    assert got[kind] == 1, got
    assert got["churn"] == 1


def test_a_star_line_is_prose_only_INSIDE_a_block_comment():
    """The replacement for two rounds of failed `*` guessing, and the reason it is
    state rather than a marker.

    A bare `*` marker ate `*dst = value;`. Narrowing it to `* ` moved the collision to
    `* dst = value;` — the same pointer store with a space — and to
    `* generator() {`. Both were caught by successive Codex second opinions, and the
    second one is the proof that no prefix can answer this: the question is not what
    the line starts with, it is whether the line is inside a `/* … */`.

    So `*` is not guessed about at all. A continuation is prose because a block is
    open, and the identical line outside one is code."""
    inside = ("diff --git a/app/x.c b/app/x.c\n--- a/app/x.c\n+++ b/app/x.c\n"
              "@@ -1,1 +1,5 @@\n x\n"
              "+    /* what this does\n+     * and why\n+     */\n"
              "+    * dst = value;\n")
    got = panel_seats.referee_split(inside)
    # Three lines of block comment, and the store after it is code.
    assert (got["prose"], got["production"]) == (3, 1)
    # The same star line with no block open is code, four times over, and cannot
    # fire the rung.
    outside = ("diff --git a/app/x.c b/app/x.c\n--- a/app/x.c\n+++ b/app/x.c\n"
               "@@ -1,1 +1,5 @@\n x\n"
               "+    * dst = value;\n+    * a = b;\n+    * c = d;\n+    * e = f;\n")
    got = panel_seats.referee_split(outside)
    assert got["production"] == 4
    assert panel_rounds.referee_state(got, True)["over"] is False


def test_a_block_comment_that_ENDS_and_leaves_code_on_the_line_is_not_prose():
    """`_next_block` scans rather than testing a prefix, so a block opened and closed
    inside one line neither opens a block nor makes that line a comment."""
    diff = ("diff --git a/app/x.c b/app/x.c\n--- a/app/x.c\n+++ b/app/x.c\n"
            "@@ -1,1 +1,3 @@\n x\n"
            "+    int x = 1; /* note */ int y = 2;\n+    int z = 3;\n")
    assert panel_seats.referee_split(diff)["production"] == 2


def test_a_MULTILINE_VALUE_is_not_a_docstring_because_of_the_line_before_it():
    """Codex's third finding on the second pass, and the deepest of the three: a
    triple-quote at the head of a line is *syntactically identical* whether it opens a
    docstring or a multiline argument. Nothing on the line separates them.

    The line BEFORE it does. A docstring is the first statement of a suite, so its
    predecessor ends with a colon; a value continues a call or an assignment, so its
    predecessor ends with `(`, `,` or `=`. An unknown predecessor — the first line of
    a hunk — is not a host, which is the safe direction."""
    value = (DIFF_HEADER + "@@ -1,1 +1,5 @@\n x\n"
             f"+    cur.execute(\n+        {DQ}SELECT 1\n+        FROM t\n"
             f"+        WHERE id = 2{DQ})\n")
    got = panel_seats.referee_split(value)
    assert got["production"] == 4 and got["prose"] == 0
    assert panel_rounds.referee_state(got, True)["over"] is False
    # ...and the same fence directly under a `def` still reads as the docstring it is.
    doc = (DIFF_HEADER + "@@ -1,1 +1,4 @@\n x\n"
           f"+def f():\n+    {DQ}Send it.\n+    {DQ}\n")
    got = panel_seats.referee_split(doc)
    assert (got["production"], got["prose"]) == (1, 2)


def test_a_one_line_docstring_FOLLOWED_BY_CODE_is_counted_as_code():
    """Codex's second finding on that pass. `<fence>doc<fence>; authorize()` is a
    valid line whose second half is a statement, and reading the whole line as prose
    hides it. Rare, and rarity is not the standard — the property is that no
    misreading may move a line OUT of production."""
    with_code = (DIFF_HEADER + "@@ -1,1 +1,3 @@\n x\n+def f():\n"
                 f"+    {DQ}doc{DQ}; authorize()\n")
    got = panel_seats.referee_split(with_code)
    assert got["production"] == 2 and got["prose"] == 0
    # The control, one character apart: the same one-liner with nothing after it is
    # the docstring it looks like.
    alone = (DIFF_HEADER + "@@ -1,1 +1,3 @@\n x\n+def g():\n"
             f"+    {DQ}doc{DQ}\n")
    got = panel_seats.referee_split(alone)
    assert got["production"] == 1 and got["prose"] == 1


def test_a_python_docstring_is_prose_even_though_no_line_starts_with_a_marker():
    """The half no line-comment rule can see, and where #554's tenth finding sat. The
    fence is tracked through the hunk, so the body of a docstring counts as prose
    whether or not the line carrying it looks like one."""
    diff = ('diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n'
            '@@ -1,2 +1,6 @@\n'
            ' def f():\n'
            '+    """One.\n'
            '+\n'
            '+    Two, which starts with no marker of any kind.\n'
            '+    """\n'
            '     return 1\n')
    got = panel_seats.referee_split(diff)
    assert (got["prose"], got["production"]) == (4, 0)


def test_the_two_diff_SIDES_keep_separate_docstring_parity():
    """A unified diff is two files interleaved. One parity counter over both reads a
    REPLACED docstring line — `-` then `+`, each carrying the same fence — as opening
    and then closing, so every prose line after it in the hunk comes back
    `production`. That is the commonest docstring edit there is, and getting it wrong
    makes a pass look refereed by exactly the lines that are not.

    Caught on the first cut of this classifier by running it against `MEASURED`,
    which came back `production: 1`."""
    diff = ('diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n'
            '@@ -1,4 +1,4 @@\n'
            ' def f():\n'
            '-    """Old summary.\n'
            '+    """New summary.\n'
            '     Body text nobody edited.\n'
            '     """\n')
    got = panel_seats.referee_split(diff)
    assert (got["prose"], got["production"]) == (2, 0)


def test_a_docstring_that_QUOTES_THE_OTHER_FENCE_STYLE_does_not_invert_the_tracker():
    """Codex's second finding on this PR, and the first cut of this reader had it.

    A docstring delimited one way that quotes the OTHER style in its text carries an
    odd number of fence occurrences, so a single parity bit toggled by either style
    comes out of that line in the wrong state. Every production line left in the hunk
    then reads as prose, and the brake can fire on a pass that was refereed all along.
    Such a docstring is ordinary — this module has several.

    Only the fence that OPENED a string may close it, which is what a tokeniser does
    and what `_next_fence` now does."""
    line = f"{DQ}it uses {SQ} inside{DQ}"
    diff = (DIFF_HEADER + "@@ -1,3 +1,5 @@\n def f():\n"
            f"+    {line}\n+    return compute()\n x\n")
    got = panel_seats.referee_split(diff)
    assert (got["prose"], got["production"]) == (1, 1)
    # ...so the pass still has something a referee can be wrong about.
    assert panel_rounds.referee_state(got, True)["over"] is False


def test_an_ASSIGNED_multiline_string_is_production_and_not_a_docstring():
    """Codex's third finding. Every triple-quoted literal was being read as a
    docstring, so an SQL statement or an HTML template — executable data a test can
    absolutely be wrong about — counted as prose. That is the direction that FIRES the
    brake, which is the one direction this reader may not lean in.

    A docstring opener starts its line; a template is assigned to something first.
    That is the only discriminator available from inside a hunk, and it is enough for
    the shapes that actually occur."""
    diff = (DIFF_HEADER + "@@ -1,2 +1,6 @@\n def f():\n"
            f"+    sql = {DQ}SELECT 1\n+    FROM t\n+    WHERE id = 2{DQ}\n"
            "+    return sql\n x\n")
    got = panel_seats.referee_split(diff)
    assert got["prose"] == 0 and got["production"] == 4
    assert panel_rounds.referee_state(got, True)["over"] is False


def test_a_bare_fence_on_its_own_line_does_not_OPEN_one():
    """The other half of that fix, and the trade it makes, stated rather than hidden.

    From inside a hunk a bare triple-quote on its own line is indistinguishable from a
    template's CLOSING delimiter, and reading a closer as an opener would flip every
    line after a template to prose — the unsafe direction again. So it does not open
    one, and the cost is that a docstring written with a bare opener has its BODY
    counted as production. That makes a pass look MORE refereed than it is, which is
    the only way this reader is allowed to be wrong."""
    diff = (DIFF_HEADER + "@@ -1,2 +1,6 @@\n def f():\n"
            f"+    tpl = render()\n+    {DQ}\n+    still_code()\n"
            "+    more_code()\n x\n")
    got = panel_seats.referee_split(diff)
    # All four lines are production. The two after the bare fence obviously, and the
    # fence line itself because `tpl = render()` cannot HOST a docstring — the two
    # rules compose, and they compose in the safe direction.
    assert got["production"] == 4
    assert panel_rounds.referee_state(got, True)["over"] is False


def test_a_docstring_QUOTING_ITS_OWN_DELIMITER_does_not_end_a_line_early():
    """Codex's second-pass P1, and the more dangerous of the two it found.

    A docstring body that quotes its own delimiter escaped does not end the string,
    but a raw substring test says it does. The reader then meets the REAL closer —
    very often written with a trailing comment — while its state says closed, reads
    that as an opener, and opens a string that never closes. Every production line
    left in the hunk becomes prose.

    Measured on Codex's own example: four added calls came back
    `production=0, prose=7` and the brake fired on a pass that was four-sevenths
    production logic."""
    body = f"    Example token: \\{DQ}"
    diff = (DIFF_HEADER + "@@ -1,1 +1,9 @@\n def f():\n"
            f"+    {DQ}Summary.\n+{body}\n+    {DQ}  # end docs\n"
            "+    authorize()\n+    charge()\n+    commit()\n+    notify()\n")
    got = panel_seats.referee_split(diff)
    assert got["production"] == 4 and got["prose"] == 3
    assert panel_rounds.referee_state(got, True)["over"] is False


def test_a_CLOSING_fence_with_a_trailing_comment_is_not_read_as_an_OPENER():
    """The half of that fix which stands on its own, and it is reachable without any
    escaping at all: a hunk that BEGINS inside a docstring starts closed by design,
    and the first thing it meets is the closing delimiter — which, written
    `<fence>  # end docs`, is indistinguishable from an opener with content after it.

    Reading it as an opener turns the documented safe lean (a hunk beginning inside a
    docstring counts as production) into its opposite for the rest of the hunk. An
    opener's text is prose; nobody writes a docstring whose first characters are a
    comment marker."""
    assert panel_seats._next_fence("", f"    {DQ}  # end docs", (DQ, SQ)) == ""
    # ...while a real opener still opens, and a one-liner still opens nothing.
    assert panel_seats._next_fence("", f"    {DQ}Summary.", (DQ, SQ)) == DQ
    assert panel_seats._next_fence("", f"    {DQ}One.{DQ}", (DQ, SQ)) == ""
    diff = (DIFF_HEADER + "@@ -40,2 +40,5 @@\n     trailing docstring prose\n"
            f"+    {DQ}  # end docs\n+    authorize()\n+    charge()\n")
    # Three, not two: the closer line is production as well, because the line before
    # it cannot host a docstring either. Both rules refuse to open, independently.
    assert panel_seats.referee_split(diff)["production"] == 3


@pytest.mark.parametrize("name,diff,expect", [
    # A deleted file: `+++ /dev/null` names no path, so the `diff --git` header's name
    # has to survive it or three deletions are attributed to nothing.
    ("deleted file",
     "diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ /dev/null\n"
     "@@ -1,3 +0,0 @@\n-def f():\n-    return 1\n-x = 2\n",
     {"production": 3, "churn": 3}),
    # A rename or mode change carries no hunk at all.
    ("rename only, no hunk",
     "diff --git a/app/x.py b/app/y.py\nsimilarity index 100%\n"
     "rename from app/x.py\nrename to app/y.py\n",
     {"churn": 0}),
    # `@@ -1,3 +1,4 @@ def outer():` — git puts the enclosing section after the ranges.
    ("hunk header with section text",
     "diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n"
     "@@ -1,3 +1,4 @@ def outer():\n x\n+    return compute()\n",
     {"production": 1, "churn": 1}),
    ("no newline at end of file",
     "diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n"
     "@@ -1,1 +1,2 @@\n x\n+    return compute()\n\\ No newline at end of file\n",
     {"production": 1, "churn": 1}),
    ("CRLF line endings",
     "diff --git a/app/x.py b/app/x.py\r\n--- a/app/x.py\r\n+++ b/app/x.py\r\n"
     "@@ -1,1 +1,2 @@\r\n x\r\n+    return compute()\r\n",
     {"production": 1, "churn": 1}),
])
def test_the_reader_survives_the_diff_shapes_that_are_not_plain_hunks(name, diff, expect):
    """The state-machine shapes a real fix range throws at this, named by a Codex
    second opinion as untested. None of them was broken; all of them were unasserted,
    which is the same thing one refactor later — a diff parser is exactly where a
    silent misread lives, and every misread here is a wrong verdict about a cycle."""
    got = panel_seats.referee_split(diff)
    for key, want in expect.items():
        assert got[key] == want, (name, key, got)


def test_fence_state_resets_between_HUNKS_and_between_FILES():
    """Two hunks of a file are two windows into it, and a string opened in the first
    says nothing about where the second begins; two files share nothing at all.
    Carrying the state across either would turn one unclosed docstring into prose for
    the remainder of a whole fix range."""
    across_hunks = (DIFF_HEADER + f"@@ -1,2 +1,3 @@\n def f():\n+    {DQ}Docs.\n"
                    "@@ -40,1 +41,2 @@\n y\n+    return compute()\n")
    assert panel_seats.referee_split(across_hunks)["production"] == 1
    across_files = ("diff --git a/docs/a.md b/docs/a.md\n--- a/docs/a.md\n"
                    "+++ b/docs/a.md\n@@ -1 +1,2 @@\n x\n+prose\n"
                    + DIFF_HEADER + "@@ -1 +1,2 @@\n x\n+    return compute()\n")
    got = panel_seats.referee_split(across_files)
    assert (got["prose"], got["production"]) == (1, 1)


def test_an_added_line_that_LOOKS_LIKE_a_file_header_is_content():
    """Codex's first finding, and `_diff_added_lines` beside this one already guarded
    it: an added line whose own text begins `++ ` is spelled `+++ ` in a diff.

    Ungated, such a line escapes the churn count AND re-points the tracked path at
    whatever it names, misclassifying every line after it — which here would
    manufacture a test-file reading out of a pass that never touched one."""
    # `+` marker plus content reading `++ b/…`, which is four `+` characters short
    # of nothing and exactly what a `+++ ` header looks like.
    diff = (DIFF_HEADER + "@@ -1,1 +1,3 @@\n x\n"
            "+++ b/tests/test_x.py\n+    return compute()\n")
    got = panel_seats.referee_split(diff)
    assert got["churn"] == 2 and got["test"] == 0
    assert got["production"] == 2



def test_a_hunk_that_BEGINS_inside_a_docstring_leans_toward_NOT_firing():
    """The one approximation worth naming, and it is written down rather than fixed.
    Parity starts fresh at each `@@`, so an edit deep in a long docstring whose
    context lines carry no fence is read as beginning OUTSIDE one and counts as
    production.

    That is the safe direction and the only one this rule may lean in: it makes the
    pass look MORE refereed than it was, so the brake declines to fire on a pass it
    misread rather than firing on one it did. Closing it needs the file's whole text,
    which this reader does not have and the round has no reason to fetch."""
    diff = ('diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n'
            '@@ -40,3 +40,3 @@\n'
            '     paragraph before\n'
            '-    the sentence as it was\n'
            '+    the sentence as it now is\n'
            '     paragraph after\n')
    got = panel_seats.referee_split(diff)
    assert got["production"] == 2
    assert panel_rounds.referee_state(got, True)["over"] is False


def test_it_counts_CHURN_and_not_added_lines_alone():
    """Where this parts company with `guard_ratio` beside it, and it is not an
    inconsistency. That one asks how much apparatus a change BUILT, so a deletion is
    not apparatus. This prices what a fix pass DID, in the unit the budget is already
    spent in — insertions plus deletions, which is what `git diff --numstat` reports.
    A pass that DELETES an assertion has done unrefereed work exactly as one that
    adds a weak assertion has."""
    diff = ('diff --git a/tests/test_x.py b/tests/test_x.py\n'
            '--- a/tests/test_x.py\n+++ b/tests/test_x.py\n'
            '@@ -1,5 +1,2 @@\n'
            ' def test_x():\n'
            '-    assert a\n-    assert b\n-    assert c\n-    assert d\n'
            '     pass\n')
    got = panel_seats.referee_split(diff)
    assert got["test"] == got["churn"] == 4
    # ...and a pass that only deleted tests is still a pass nothing can check.
    assert panel_rounds.referee_state(got, True)["over"] is True


def test_a_diff_with_no_churn_reports_no_share_rather_than_zero():
    """`None` and not `0.0`, on `injection_state`'s rule: zero is a claim about a fix
    pass and this is the absence of one."""
    assert panel_seats.referee_split("")["share"] is None
    assert panel_rounds.referee_state(None, True)["share"] is None


def test_the_classifier_shares_ONE_path_reader_with_the_apparatus_ratio():
    """`_guard_kind` and not a second table. #492 already classifies these paths for
    `guard_ratio`, and two path classifiers are two things that can disagree about one
    file — the failure `_diff_file_path`'s docstring names one layer down. What #554
    adds is the LINE half on top."""
    for path, guard in (("tests/x.py", "test"), ("docs/x.md", "doc"),
                        ("app/x.py", "source")):
        assert panel_seats._guard_kind(path) == guard
    diff = ('diff --git a/tests/x.py b/tests/x.py\n--- a/tests/x.py\n'
            '+++ b/tests/x.py\n@@ -1 +1,2 @@\n x\n+    assert 1\n')
    assert panel_seats.referee_split(diff)["test"] == 1


def test_EVERY_misreading_this_classifier_can_make_leans_the_same_way():
    """The property the whole gate rests on, asserted as a property rather than left
    to the docstrings that claim it.

    "Zero production lines" is not ground truth — it is this reader's output, and the
    reader is heuristics. What makes it safe to END A CYCLE on is that every way it
    can be wrong counts a line as PRODUCTION, which makes the pass look refereed and
    the brake decline to fire. Two violations of that were real (a bare `*` marker
    eating a pointer store; a fence tracker ending a docstring a line early) and both
    are covered above.

    Collected here so the next person to add a marker, a suffix or a fence style has
    one test to run and one sentence to satisfy: your case must not be able to move a
    line OUT of `production` unless it is genuinely not code."""
    ambiguous = [
        # (path, line) — every shape where a reasonable reader might go either way.
        ("app/x.c", "    *dst = value;"),
        ("app/x.c", "    * dst = value;"),          # the space that moved the bug
        ("app/x.c", "    */ trailing = code;"),
        ("web/x.css", "* { margin: 0 }"),
        ("web/x.js", "  *generator() {"),
        ("web/x.js", "  * generator() {"),
        ("app/x.ts", " * not inside any block"),
        ("app/x.py", '    marker = "# not a comment"'),
        ("app/x.py", "    x = 1  # trailing comments are not comments"),
        ("app/x.py", f"    sql = {DQ}SELECT 1"),
        ("Makefile", "\tgo build"),
        ("app/x", "    a file with no suffix at all"),
    ]
    for path, line in ambiguous:
        diff = (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
                f"@@ -1 +1,2 @@\n x\n+{line}\n")
        got = panel_seats.referee_split(diff)
        assert got["production"] == 1, (path, line, got)
        assert panel_rounds.referee_state(got, True)["over"] is False


# --------------------------------------------------------------------------- the dial

def test_the_default_is_on_and_is_the_one_the_rules_file_documents():
    """ON, like `premise_repeated`, `fix_injection` and #505's rung, and earned the
    same way: it can only turn a `go again` into a stop, so no value of it can make a
    review look cleaner than it is."""
    assert DEFAULT_BLOCK["escalate_on"] == {"premise_repeated": 2,
                                            "premise_undecidable": True,
                                            "fix_injection": 0.5,
                                            "new_findings_not_falling": 1,
                                            "unrefereed_fix": True,
                                            "guard_lines": False}
    assert panel_rounds.unrefereed_fix_brake(DEFAULT_BLOCK, []) is True


def test_a_repo_that_never_heard_of_the_key_gets_the_default():
    assert panel_rounds.unrefereed_fix_brake({}, []) is True


def test_a_repo_that_wrote_a_DIFFERENT_rung_still_gets_this_one():
    """Read per KEY, not per block. `review_panel` merges one level deep, so a written
    `escalate_on` REPLACES the default object wholesale — without the per-key fallback
    `{"premise_repeated": 2}` would silently switch this brake off, which is the exact
    failure #84 hit and is worth not shipping a third time."""
    assert panel_rounds.unrefereed_fix_brake(
        {"escalate_on": {"premise_repeated": 2}}, []) is True


@pytest.mark.parametrize("value", [False, None, ""])
def test_false_and_null_are_both_the_off_switch(value):
    """`false` is what an operator reaches for to turn a brake off and refusing it
    would be this harness telling somebody their "off" was a typo — the same reading
    its two siblings give it."""
    assert panel_rounds.unrefereed_fix_brake(
        {"escalate_on": {"unrefereed_fix": value}}, []) is False


@pytest.mark.parametrize("value", [0.5, 1, "sometimes", 2, []])
def test_there_is_no_NUMBER_this_dial_could_take(value):
    """A threshold is not a switch. The rule is a predicate on a fact — the pass
    contains no refereed line — so a number over it would have to mean "produce
    unrefereed passes N times first", which is the behaviour the rule refuses. Guessing
    one would be inventing the policy, which is the line every dial in this file draws
    between an unknown KEY (warned about and dropped) and a malformed value of a known
    one."""
    with pytest.raises(SystemExit):
        panel_rounds.unrefereed_fix_brake(
            {"escalate_on": {"unrefereed_fix": value}}, [])


def test_a_malformed_escalate_on_block_is_refused_here_too():
    """This reader is public and is called directly by tests, so it does not rely on a
    sibling having validated the block first."""
    with pytest.raises(SystemExit):
        panel_rounds.unrefereed_fix_brake({"escalate_on": "premise_repeated"}, [])


# -------------------------------------------------------------------- the measurement

def test_the_predicate_is_the_ABSENCE_of_a_refereed_line_and_not_a_proportion():
    """One production line is enough to disarm it, at any share. That is the claim
    #554 makes, and it is deliberately not "mostly unrefereed": a pass with a
    production line in it has something a referee can be wrong about."""
    assert _unrefereed(production=0, test=99, prose=0)["over"] is True
    assert _unrefereed(production=1, test=99, prose=0)["over"] is False
    # 99% unrefereed and not a step-back signal, which is the reading a fraction
    # could not give and the reason there is no fraction here.
    assert _unrefereed(production=1, test=99, prose=0)["share"] == 0.99


def test_a_pass_too_small_to_be_a_pass_does_not_end_a_cycle():
    """`UNREFEREED_MIN_CHURN` is the noise floor, and it is a constant rather than a
    third uncalibrated dial for `FIX_INJECTION_MIN_NEW`'s reason — sharpened by this
    rule having no threshold of its own, so a knob under it would put the guess back
    one level down. Under four lines a pass is a typo correction, and ending a cycle
    on the observation that a typo had no test would be the brake firing on the
    cheapest round there is."""
    assert panel_seats.UNREFEREED_MIN_CHURN == panel_rounds.FIX_INJECTION_MIN_NEW == 4
    assert _unrefereed(test=3, prose=0)["over"] is False
    assert _unrefereed(test=3, prose=1)["over"] is True


def test_a_repo_that_switched_the_brake_off_still_SEES_the_measurement():
    """`armed` apart from the counts, the split `premise_state` keeps between its
    `undecidable` list and its `undecidable_brake` flag and for its reason: the payload
    records what the cycle MEASURED, and a repo that declined the policy must not have
    it applied — but must still be able to see that its fix pass wrote nothing
    checkable."""
    off = _unrefereed(armed=False)
    assert off["armed"] is False and off["over"] is False
    assert (off["churn"], off["unrefereed"], off["share"]) == (10, 10, 1.0)


def test_a_round_with_no_readable_fix_range_measures_nothing_and_stops_nothing():
    """#500's blindness arriving at this rung as it arrives at #489's. `over: False` by
    construction — a round that could not see the pass does not end a cycle on what the
    pass contained."""
    blind = panel_rounds.referee_state(None, True)
    assert blind["churn"] == 0 and blind["over"] is False


# ----------------------------------------------------------------------- the stop rule

def test_a_cycle_whose_last_fix_pass_wrote_nothing_checkable_ends():
    """The whole point. Four new findings would buy another round under rule 1, and
    that round would be a review of artefacts no mechanism in the loop refereed."""
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  unrefereed=_unrefereed())
    assert got["stop"] is True
    assert "not one of them was production code" in got["reason"]
    assert "7 test, 3 prose" in got["reason"]
    assert "a human answers this" in got["reason"]


def test_that_stop_is_never_reported_as_convergence():
    """The same discipline `max_fix_growth`, the round cap, a held escalation and the
    two rungs beside it all get: a veto line naming what happened, and `confident`
    false."""
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  unrefereed=_unrefereed())
    assert got["confident"] is False
    assert any("nothing tests a test" in v and "not convergence (#554)" in v
               for v in got["veto"])


def test_the_veto_names_RED_GREEN_because_that_is_what_a_reader_assumes_covered_it():
    """Red/green ran on the pass #554 measured and went red 4 of 4. A reader who is
    told a fix pass was all test will reasonably think the red/green step covered it,
    and the veto is the only place they are told what that step actually asks."""
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  unrefereed=_unrefereed())
    line = next(v for v in got["veto"] if "#554" in v)
    assert "Red/green" in line and "not that it is sound" in line


def test_it_is_not_the_cap_and_does_not_say_it_is():
    """Both are true of this round and only one is actionable. A reader told "the
    counter ran out" goes looking for a bigger cap, which here buys another review of
    unrefereed work."""
    got = panel_rounds.round_stop(2, 2, ["k1", "k2", "k3", "k4"], [], [],
                                  unrefereed=_unrefereed())
    assert "round cap" not in got["reason"]
    assert "production code" in got["reason"]


def test_the_attribution_rung_is_the_more_specific_truth_and_wins_the_reason():
    """`circling`'s ordering rule, one level down. #489's rate NAMES the fix pass as
    the author of this round's findings; this one says what the pass was made of — so
    the rate owns the `reason`, and both veto lines are on the record."""
    counts = {"introduced": 3, "missed": 1, "missed-unread": 0, "unknown": 0}
    got = panel_rounds.round_stop(
        2, 5, ["k1", "k2", "k3", "k4"], [], [],
        injection=panel_rounds.injection_state(counts, 0.5),
        unrefereed=_unrefereed())
    assert "introduced by the fix pass" in got["reason"]
    assert got["fix_injection"]["fired"] is True
    assert got["unrefereed_fix"]["fired"] is True
    assert any("#554" in v for v in got["veto"])
    assert any("escalate_on.fix_injection" in v for v in got["veto"])


def test_it_is_the_more_specific_truth_than_the_count_not_falling():
    """The other side of the same ordering. #505's rung says only that the work is not
    shrinking; this says what kind of work it was."""
    flat = panel_rounds.not_falling_state([(1, 9), (2, 9)], 1)
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  not_falling=flat, unrefereed=_unrefereed())
    assert "production code" in got["reason"]
    assert got["new_findings_not_falling"]["fired"] is True
    assert any("new_findings_not_falling" in v for v in got["veto"])


def test_a_DRY_round_keeps_its_own_reason_and_its_confidence():
    """The guarantee that makes a default-on defensible, CHECKED rather than merely
    obeyed: the only transition this can make is `go again` -> stop. A round with
    nothing outstanding has no next round to prevent."""
    got = panel_rounds.round_stop(2, 5, [], [], [], unrefereed=_unrefereed())
    assert got["stop"] is True and got["confident"] is True
    assert got["reason"].startswith("dry")
    assert got["unrefereed_fix"]["over"] is True
    assert got["unrefereed_fix"]["fired"] is False


def test_a_below_floor_policy_stop_keeps_its_own_reason_and_its_confidence():
    """#165's floor stops are POLICY stops and are deliberately not vetoed. Vetoing one
    through this door would make every configured convergence non-confident and hand
    the cap back its monopoly on ending the loop."""
    quiet = [_finding("P4", key_from=f"nit {i}") for i in range(4)]
    got = panel_rounds.round_stop(2, 5, [c.key for c in quiet], quiet, [],
                                  trigger_floor="P2", cleared_floor="P2",
                                  unrefereed=_unrefereed())
    assert got["stop"] is True and got["confident"] is True
    assert "round trigger floor" in got["reason"]


def test_a_round_going_again_for_an_EARLIER_ROUNDS_P1_is_not_cancelled():
    """The bound the two rungs beside it take, in the corrected form #505 landed after
    Codex found the old one: "it may only take away the round rule 1 was buying" is not
    "rule 1 won the `reason`". Rules 1-3 are an if/elif chain, so a round with four
    triggering news AND a P1 an earlier round raised reports rule 1 while going again
    for both — and that P1 is work the fix pass FAILED to do, not work it generated.

    A statistic may end the loop it is a statistic about; it may not overrule a named
    P1, and neither may a fact about a diff."""
    news = [_finding("P2", key_from=f"new {i}") for i in range(4)]
    blocker = _finding("P1", key_from="a P1 an earlier round raised")
    got = panel_rounds.round_stop(2, 9, [c.key for c in news], [*news, blocker], [],
                                  unrefereed=_unrefereed())
    assert got["stop"] is False and "no earlier round raised" in got["reason"]
    assert got["unrefereed_fix"]["over"] is True
    assert got["unrefereed_fix"]["fired"] is False


def test_this_rounds_OWN_new_blockers_do_not_disarm_it():
    """The other side of that bound, and the one that keeps it from gutting the rule.
    `blockers` is every outstanding P1/P2, and on the ordinary round those ARE the
    news. Bounded on `blockers` outright this could not fire on the cycle it was
    measured from, where every new finding was a P2."""
    news = [_finding("P1", key_from=f"new blocker {i}") for i in range(4)]
    got = panel_rounds.round_stop(2, 9, [c.key for c in news], news, [],
                                  unrefereed=_unrefereed())
    assert got["stop"] is True and got["unrefereed_fix"]["fired"] is True


def test_it_may_not_cancel_the_repair_round_for_a_REPEATED_finding():
    """Rule 3's half of the same bound."""
    news = [_finding("P3", key_from=f"new {i}") for i in range(4)]
    stale = _finding("P3", key_from="the one the last fix missed")
    got = panel_rounds.round_stop(2, 9, [c.key for c in news], [*news, stale], [],
                                  repeated={stale.key}, unrefereed=_unrefereed())
    assert got["stop"] is False and got["unrefereed_fix"]["fired"] is False


def test_the_measurement_rides_in_the_payload_whether_it_fired_or_not():
    """Always present, its three siblings' rule and for their reason: a payload with no
    key and a round with no fix pass to read are different claims, and a consumer
    forced to tell them apart would be reading the payload's age rather than the
    cycle's state."""
    off = panel_rounds.round_stop(2, 5, [], [], [])
    assert off["unrefereed_fix"] == {
        "armed": False, "production": 0, "test": 0, "prose": 0, "churn": 0,
        "unrefereed": 0, "share": None, "min_churn": 4, "over": False,
        "fired": False}
    on = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                 unrefereed=_unrefereed())
    assert on["unrefereed_fix"] == {
        "armed": True, "production": 0, "test": 7, "prose": 3, "churn": 10,
        "unrefereed": 10, "share": 1.0, "min_churn": 4, "over": True,
        "fired": True}


def test_the_constructive_pass_follows_this_rung_like_the_others():
    """#507's rule is every BUILT rung, not a subset a reader has to memorise the
    membership of — and this one has a claim of its own on top of that. It fires
    exactly when the last pass answered its findings by writing more test, which is
    the strongest evidence this harness has that the fixer never found the change the
    findings were asking for. The seats are the ones asked, not the fixer, which is
    #297's discipline."""
    assert "unrefereed_fix" in panel_propose.PROPOSE_ESCALATIONS
    stop = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                   unrefereed=_unrefereed())
    assert panel_propose.escalations_fired(stop) == ["unrefereed_fix"]
    # A round that measured it and did not fire on it buys no fan-out — `fired`, never
    # `over`, for the reason the two are kept apart at all.
    dry = panel_rounds.round_stop(2, 5, [], [], [], unrefereed=_unrefereed())
    assert dry["unrefereed_fix"]["over"] is True
    assert panel_propose.escalations_fired(dry) == []


# --------------------------------------------------------------------- the budget half

def test_the_weight_default_is_the_one_the_rules_file_documents():
    """#554's other proposal: the budget's unit becomes exposure rather than length.
    Two, and written down in both places `test_panel_dials` pins together."""
    assert DEFAULT_BLOCK["unrefereed_line_weight"] == 2
    assert panel_core.DEFAULT_UNREFEREED_LINE_WEIGHT == 2
    assert panel_seats.unrefereed_line_weight({}, []) == 2


def test_one_prices_every_line_alike_which_is_how_the_weighting_is_switched_off():
    """`1` IS "off" — the pre-#554 behaviour — so there is deliberately no `null`
    spelling for the same thing. That asymmetry with `low_severity_fix_lines` beside it
    is the point: there, `null` had a meaning nothing else could spell."""
    assert panel_seats.unrefereed_line_weight({"unrefereed_line_weight": 1}, []) == 1
    assert panel_seats.unrefereed_line_weight(
        {"unrefereed_line_weight": None}, []) == 2


@pytest.mark.parametrize("value", [0, -1, 0.5, True, False, "lots", []])
def test_a_weight_under_one_or_unreadable_is_refused_rather_than_clamped(value):
    """Below 1 would make an unrefereed line CHEAPER than a production one, which is
    not a looser version of this policy but its inverse — a repo that wrote 0.5 meant
    something and nothing here can tell which of two opposite things it was. `0` goes
    with them: an unrefereed line that costs nothing is an unbounded budget for the
    one category the budget exists to bound.

    Bools are settled before the integer read, for `low_severity_budget`'s reason:
    `isinstance(True, int)` is True, so `false` — the other way a hand writes "off" —
    would otherwise become a weight of 1 and be right by accident."""
    with pytest.raises(SystemExit):
        panel_seats.unrefereed_line_weight({"unrefereed_line_weight": value}, [])


def test_an_integral_float_or_a_string_of_digits_counts():
    """A generator that emits `2.0`, or a settings channel that stringifies. Half a
    line is not a quantity `git diff --numstat` can report, which is why `2.5` is not
    in this list and is in the one above."""
    for raw in (2.0, "2", " 3 "):
        assert panel_seats.unrefereed_line_weight(
            {"unrefereed_line_weight": raw}, []) >= 2


def test_the_weight_is_on_the_dials_line_the_fixers_brief_is_built_from():
    """The orchestrator builds the fixer's brief out of this report, so the unit the
    budget is counted in has to be readable from the artifact rather than from whoever
    remembers the repo's config. It rides WITH the budget rather than in a field of its
    own, because it is not a second setting a reader weighs separately — a line saying
    "40 lines" beside one saying "x2" leaves the reader to work out what 40 buys."""
    gist = panel_seats.resolve_dials({}, None, []).gist()
    assert "below-P2 fix budget 40 lines, unrefereed x2" in gist
    # Printed at `1` too: a dial that vanishes from the line at some settings is one a
    # reader cannot tell from a dial that was never applied.
    off = panel_seats.resolve_dials({"unrefereed_line_weight": 1}, None, []).gist()
    assert "40 lines, unrefereed x1" in off
    # ...and suppressed where the budget is off, where it is a unit of nothing.
    none = panel_seats.resolve_dials(
        {"low_severity_fix_lines": None}, None, []).gist()
    assert "fix budget off ·" in none


def test_the_budget_NOTE_carries_the_weight_into_the_fixers_brief(
        monkeypatch, tmp_path, capsys, every_seat_is_on_this_box):
    """Codex's fourth finding on this PR, and it was the one that mattered most: the
    dial was resolved, validated, put on the dials line and applied by nobody.

    The 💸 note under **To fix** is what an orchestrator sweeps into the fixer's brief
    along with the findings it bounds — `panel-review-pr.md` says so in as many words —
    so a weight stated only on the dials line is a weight the fixer is never told
    about, and the budget goes on charging every churned line alike. The note is where
    the arithmetic has to be."""
    _, r1 = _panel_round(monkeypatch, tmp_path, 1,
                         [("app/mirror.py", 2, "a nit worth one line")],
                         head="a" * 40, compare=UNREFEREED_COMPARE,
                         config={"fix_severity_floor": "P4",
                                 "round_trigger_floor": "P1"})
    note = next(line for line in capsys.readouterr().out.splitlines()
                if "budget for the WHOLE round" in line)
    assert "2x a line of production code" in note
    assert "nothing tests a test (#554)" in note
    # The dial the round APPLIED is the one the note quotes, not the shipped default.
    assert r1["review_panel"]["unrefereed_line_weight"] == 2


def test_the_brief_tells_the_fixer_to_multiply_rather_than_to_forecast():
    """#297's discipline, carried onto the second axis. The fixer is never asked
    whether a fix is important or whether the work risks ballooning — it classifies
    each line by the file it is in and whether it is a comment, and multiplies. The
    brief is the artifact that actually governs the fix pass, so the rule has to be IN
    it and not only in the dial's docstring."""
    brief = (Path(__file__).resolve().parents[2]
             / "commands" / "review-pr.md").read_text()
    assert "cost = production_lines + weight x unrefereed_lines" in brief
    assert "nothing tests a test" in brief
    assert "not a judgement about worth" in brief
    # And the stop, so a fixer is not surprised by a cycle ending on the shape of its
    # own pass — with the reassurance that a real fix plus its test cannot trip it.
    assert "escalate_on.unrefereed_fix" in brief
    assert "It is\nnot asking you to skip tests" in brief


# ------------------------------------------------------------------------- the reach

import json  # noqa: E402
import panel_preflight  # noqa: E402
from conftest import gh_stub  # noqa: E402

PR_DIFF = '''diff --git a/app/sync.py b/app/sync.py
--- a/app/sync.py
+++ b/app/sync.py
@@ -1,3 +1,5 @@
 def sync():
+    mirror()
+    mirror()
     return 1
diff --git a/app/mirror.py b/app/mirror.py
--- a/app/mirror.py
+++ b/app/mirror.py
@@ -1,3 +1,7 @@
 def mirror():
+    retry()
+    retry()
+    lock()
+    unlock()
     return 1
'''

#: Round 2's findings, deliberately in the file the FIX PASS never touched. That
#: keeps `escalate_on.fix_injection` out of these tests: every one of them is
#: attributed `missed` rather than `introduced`, so the rate is 0 and the rung under
#: test is the one that fires. Both rungs firing is covered by a unit test above,
#: where the ordering is the thing being pinned; here the ordering would only mask
#: whether this rung reached the verdict at all.
ROUND_2_FINDINGS = [("app/mirror.py", 2, "the second retry is unguarded"),
                    ("app/mirror.py", 3, "no timeout on the retry"),
                    ("app/mirror.py", 4, "the retry loop never exits"),
                    ("app/mirror.py", 5, "the lock is never released")]

#: The fix pass between the two rounds, as the compare API hands it over: four
#: churned lines across a test file and a docstring, and not one line of production
#: code. This is #554's measured shape, arriving through the real reader.
UNREFEREED_COMPARE = json.dumps({
    "status": "ahead",
    "files": [
        {"filename": "tests/test_sync.py",
         "patch": "@@ -1,0 +2,2 @@\n+    with mock.patch('app.sync.redis'):\n"
                  "+        assert sync() == 1"},
        {"filename": "app/sync.py",
         "patch": '@@ -1,2 +1,4 @@\n def sync():\n+    """Mirror it.\n'
                  '+\n+    No network access.\n+    """'},
    ]})

#: The same pass with ONE production line in it, and nothing else changed.
REFEREED_COMPARE = json.dumps({
    "status": "ahead",
    "files": [
        {"filename": "tests/test_sync.py",
         "patch": "@@ -1,0 +2,2 @@\n+    with mock.patch('app.sync.redis'):\n"
                  "+        assert sync() == 1"},
        {"filename": "app/sync.py",
         # The docstring sits where one goes — directly under the `def` — so it is
         # read as prose, and the one production line is `mirror_once()`. Written this
         # way deliberately: a triple-quote that does NOT follow a suite header is a
         # multiline value and counts as production, which would make this control
         # pass for the wrong reason.
         "patch": '@@ -1,2 +1,4 @@\n def sync():\n+    """Mirror it.\n+    """\n'
                  "+    mirror_once()"},
    ]})

E2E_CFG = {
    "github": "acme/e2e",
    "path": "/nonexistent/acme-e2e",
    "_rules_baseline": ".harness-rules.sample",
    "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
    "review_panel": {"refuse_over_cap_multiple": 0, "manifest_moves": False,
                     "max_rounds": 3},
}


@pytest.fixture
def every_seat_is_on_this_box(monkeypatch):
    """#138's pre-flight skips a seat whose CLI is not on PATH, so a file that runs
    whole rounds is otherwise asserting on which vendor CLIs the machine running the
    suite happens to carry — green locally and quietly not engaging on a CI runner
    that has none."""
    monkeypatch.setattr(panel_preflight, "seat_installed", lambda name: True)


def _panel_round(monkeypatch, tmp_path, round_no, findings, head, compare,
                 baseline=(), config=None):
    """One whole round with every subprocess replaced, so what is under test is the
    wiring rather than any CLI."""
    fake_sh = gh_stub(
        meta={"title": "feat: mirror", "additions": 20, "deletions": 2,
              "headRefOid": head},
        compare=compare, diff=PR_DIFF)

    def fake_review(name, model, prompt, effort="", **_kw):
        return panel.ReviewerRun(
            [panel.Finding("claude", "P2", f, ln, t, "detail")
             for f, ln, t in findings], None, 800, None)

    def fake_adjudicate(clusters, diff, model, pr, budget=None, coverage=None,
                        ci="", recurrence="", **_kw):
        return ([panel.Canonical(id=panel._finding_id(pr, i + 1), severity="P2",
                                 file=f.file, line=f.line, synthesis=f.title,
                                 verdict="confirmed", detail="detail",
                                 reported_by=[f], rationale="real")
                 for i, grp in enumerate(clusters) for f in grp], None,
                panel.CoverageRuling())

    monkeypatch.setattr(panel, "load_repo_cfg",
                        lambda name: {**E2E_CFG,
                                      "review_panel": {**E2E_CFG["review_panel"],
                                                       **(config or {})}})
    monkeypatch.setattr(panel_core, "sh", fake_sh)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", fake_adjudicate)
    out = tmp_path / f"e2e-r{round_no}.json"
    assert panel.run("e2e", 77, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=3) == 0
    return str(out), json.loads(out.read_text())


def test_a_round_with_no_fix_pass_before_it_measures_nothing(
        monkeypatch, tmp_path, capsys, every_seat_is_on_this_box):
    """Round 1 has no earlier round to attribute against, so there is no pass whose
    refereeing could be read. The key is still present — a consumer must never have to
    tell "absent" from "measured and found none" — and the report says nothing, because
    a line of zeroes would claim a pass wrote nothing when in fact none was looked
    at."""
    _, r1 = _panel_round(monkeypatch, tmp_path, 1,
                         [("app/sync.py", 2, "the mirror is written twice")],
                         head="a" * 40, compare=UNREFEREED_COMPARE)
    assert r1["round_stop"]["unrefereed_fix"]["churn"] == 0
    assert r1["round_stop"]["unrefereed_fix"]["fired"] is False
    assert "Refereed-ness" not in capsys.readouterr().out


def test_a_second_round_reads_the_real_fix_range_and_ends_the_cycle(
        monkeypatch, tmp_path, capsys, every_seat_is_on_this_box):
    """The reach half, end to end and through the real reader: round 1 complains,
    the compare API hands over a fix pass that is a test file and a docstring, and
    round 2 classifies it, fires, and says so in the payload, the reason and the
    report.

    This is what a unit test on `round_stop` alone cannot show — that the diff the
    gate reads is the one `panel.py` actually assembled for the fix range, rather
    than a hand-built dict that agrees with the rule by construction."""
    p1, _ = _panel_round(monkeypatch, tmp_path, 1,
                         [("app/sync.py", 2, "the mirror is written twice")],
                         head="a" * 40, compare=UNREFEREED_COMPARE)
    _, r2 = _panel_round(monkeypatch, tmp_path, 2,
                         ROUND_2_FINDINGS,
                         head="b" * 40, compare=UNREFEREED_COMPARE, baseline=[p1])
    got = r2["round_stop"]["unrefereed_fix"]
    assert got["production"] == 0 and got["churn"] >= 4
    assert got["over"] is True and got["fired"] is True
    assert r2["round_stop"]["stop"] is True
    assert r2["round_stop"]["confident"] is False
    assert "not one of them was production code" in r2["round_stop"]["reason"]
    # `fix_injection` measured this round and did NOT fire — every finding is in a
    # file the fix pass never touched — which is what leaves the `reason` to this
    # rung and is the case #554 calls the ex-ante half: a pass with nothing
    # refereeable in it is caught on its own shape, before its consequences exist.
    assert r2["round_stop"]["fix_injection"]["fired"] is False
    assert "Refereed-ness of the last fix pass:" in capsys.readouterr().out


def test_ONE_production_line_in_the_same_pass_lets_the_cycle_go_again(
        monkeypatch, tmp_path, capsys, every_seat_is_on_this_box):
    """The control, and the one that shows the gate is reading the pass rather than
    the round. Same PR, same findings, same round — the only difference is that the
    fix pass contains a line red/green could be wrong about."""
    p1, _ = _panel_round(monkeypatch, tmp_path, 1,
                         [("app/sync.py", 2, "the mirror is written twice")],
                         head="a" * 40, compare=REFEREED_COMPARE)
    _, r2 = _panel_round(monkeypatch, tmp_path, 2,
                         ROUND_2_FINDINGS,
                         head="b" * 40, compare=REFEREED_COMPARE, baseline=[p1])
    got = r2["round_stop"]["unrefereed_fix"]
    assert got["production"] == 1 and got["over"] is False and got["fired"] is False
    assert r2["round_stop"]["stop"] is False
    report = capsys.readouterr().out
    assert "1 production line(s)" in report


def test_a_repo_that_switched_it_off_still_gets_the_line_and_keeps_its_round(
        monkeypatch, tmp_path, capsys, every_seat_is_on_this_box):
    """`escalate_on.unrefereed_fix: false` is one line and it is the whole off switch.
    The measurement still rides in the payload and still prints — a repo that declined
    the policy must not have it applied, and must still be able to see that its fix
    pass wrote nothing checkable."""
    off = {"escalate_on": {"unrefereed_fix": False}}
    p1, _ = _panel_round(monkeypatch, tmp_path, 1,
                         [("app/sync.py", 2, "the mirror is written twice")],
                         head="a" * 40, compare=UNREFEREED_COMPARE, config=off)
    _, r2 = _panel_round(monkeypatch, tmp_path, 2,
                         ROUND_2_FINDINGS,
                         head="b" * 40, compare=UNREFEREED_COMPARE, baseline=[p1],
                         config=off)
    got = r2["round_stop"]["unrefereed_fix"]
    assert got["armed"] is False and got["production"] == 0
    assert got["over"] is False and got["fired"] is False
    assert r2["round_stop"]["stop"] is False
    assert "is off for this repo" in capsys.readouterr().out
