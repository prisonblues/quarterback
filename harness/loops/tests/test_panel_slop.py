"""The deterministic seat, and the contract that lets it stay cheap (#780).

Three things are being held here, and only the first is ordinary testing.

**Every rule ships a positive fixture and a false-positive fixture**, and both
tests are parameterised over `load_rules()` rather than over a written-down list
— so adding a YAML file to `slop_rules/` adds two failing tests until both
fixtures exist. That inversion is the point. A rule set like this decays in one
direction only: someone adds a pattern that seems obviously right, it fires on
honest code, an operator turns the whole seat off, and the nine rules that were
working go with it. A rule with no evidence that it stays quiet is worse than no
rule, and here it cannot land.

**No rule may claim authorship or score slopness.** `test_no_rule_scores_authorship`
fails the build if a rule's id, message or remediation acquires "ai-generated",
"slop score" or "probability". There is no classifier here and there is nothing
to calibrate: this panel reviews code the fleet's agents wrote almost
exclusively, so a rule scoring authorship would score every line and rank
nothing. Each rule names one concrete defect and one remediation.

**The population is the fix pass.** `test_only_the_fix_pass_is_reported` and its
ast twin are what separate this from a linter, and they are the tests to read
first: a defect sitting on a CONTEXT line of the diff is not this seat's
business, however loudly it matches, because the seat exists to measure what the
last pass wrote in order to answer a review.
"""

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel_slop  # noqa: E402
from panel_core import SEVERITIES  # noqa: E402
from panel_slop import SlopRuleError, load_rules, parse_diff, review_fix_pass  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "slop"

RULES = load_rules()
RULE_IDS = [rule.rule_id for rule in RULES]

#: Vocabulary a rule may not use about itself. Not style policing — each of these
#: is a claim the seat cannot support: it does not know who wrote a line, and it
#: has no scale on which one file is slopper than another.
FORBIDDEN = ("ai-generated", "ai generated", "slop score", "probability")


def whole_file_diff(path: str, source: str) -> str:
    """One file's worth of diff in which the fix pass wrote every line.

    The shape `panel_scope._fix_range_diff` emits: a synthesised `diff --git`
    header and the compare API's hunk under it, with no `---`/`+++` pair.
    """
    lines = source.splitlines()
    body = "".join(f"+{line}\n" for line in lines)
    return f"diff --git a/{path} b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


def scan_fixture(kind: str, stem: str):
    """Every finding the seat raises on one fixture, read as a whole added file."""
    path = FIXTURES / kind / f"{stem}.py"
    source = path.read_text(encoding="utf-8")
    rel = f"src/{stem}.py"
    run = review_fix_pass(diff=whole_file_diff(rel, source), sources={rel: source}, rules=RULES)
    return run.findings


def stem_of(rule_id: str) -> str:
    return rule_id.rsplit("/", 1)[-1]


# ------------------------------------------------------------------ the rule set


def test_the_rules_are_data_and_all_of_them_load():
    assert len(RULES) == 10, RULE_IDS
    assert sorted(RULE_IDS) == RULE_IDS, "load_rules must return a stable order"
    for rule in RULES:
        assert rule.source.endswith(".yaml")
        assert rule.severity in SEVERITIES
        assert rule.kind in panel_slop.MATCH_KINDS
        assert rule.languages == frozenset({"python"})


def test_no_rule_scores_authorship():
    """The one test that is about what this seat REFUSES to be.

    A rule that said "likely AI-generated" would be unfalsifiable here and
    useless everywhere: the fleet's agents write nearly every line this panel
    reads. A rule that carried a probability would be inviting exactly the
    calibration argument #67 says an instrument has to win over dozens of cycles
    — and there is nothing to calibrate, because a regex either matched or it did
    not.
    """
    for rule in RULES:
        text = f"{rule.rule_id} {rule.message} {rule.remediation}".casefold()
        for token in FORBIDDEN:
            assert token not in text, f"{rule.rule_id} claims {token!r}"


def test_every_rule_names_a_remediation():
    """A defect with no named remedy is a complaint. The fixer reads `detail` and
    has to be able to act on it without a second round to ask what was meant."""
    for rule in RULES:
        assert len(rule.remediation) > 30, rule.rule_id
        assert rule.message[0].isupper() and not rule.message.endswith("."), rule.rule_id


# --------------------------------------------------- the two-direction contract


@pytest.mark.parametrize("rule_id", RULE_IDS)
def test_every_rule_has_both_fixtures(rule_id):
    stem = stem_of(rule_id)
    for kind in ("positive", "false-positive"):
        assert (FIXTURES / kind / f"{stem}.py").is_file(), f"{rule_id} has no {kind} fixture"


@pytest.mark.parametrize("rule_id", RULE_IDS)
def test_positive_fixture_fires_its_rule(rule_id):
    hit = [f for f in scan_fixture("positive", stem_of(rule_id))
           if f.detail.startswith(rule_id)]
    assert hit, f"{rule_id} did not fire on its own positive fixture"


@pytest.mark.parametrize("rule_id", RULE_IDS)
def test_false_positive_fixture_stays_quiet(rule_id):
    """The half that keeps the seat switched on.

    Each false-positive fixture is honest code of the shape the rule is closest
    to firing on — a Protocol's ellipsis body, a narrow `except` used as control
    flow, a `# noqa: E501` carrying its rule code, a `#483)` citation opening a
    comment — written from what this repo actually contains rather than invented.
    """
    fired = [f for f in scan_fixture("false-positive", stem_of(rule_id))
             if f.detail.startswith(rule_id)]
    assert not fired, f"{rule_id} fired on honest code: {[f.line for f in fired]}"


# ---------------------------------------------------- the population, not a lint


def test_only_the_fix_pass_is_reported():
    """A defect on a CONTEXT line is not a finding, however well it matches.

    This is the whole difference between a seat and a linter, and it is the test
    that fails first if someone widens the input to `gh pr diff` for convenience:
    the narrator comment below was already in the file when the round started, so
    an earlier round has either confirmed it or declined it, and re-reporting it
    every round is exactly the noise that trains an operator to skip this seat.
    """
    diff = ("diff --git a/src/keep.py b/src/keep.py\n"
            "@@ -1,4 +1,5 @@\n"
            " def slug(name):\n"
            "     # This function converts a name to a slug\n"
            "     name = name.strip()\n"
            "+    # Step 1: lower it\n"
            "     return name.lower()\n")
    run = review_fix_pass(diff=diff, rules=RULES)
    raised = {f.detail.split(" —", 1)[0] for f in run.findings}
    assert raised == {"slop/step-comment"}, raised


def test_an_ast_rule_reads_the_whole_file_and_reports_only_the_new_part():
    """The ast matchers need the file; the diff still decides what is reported.

    A placeholder that predates the round is found by the matcher — it has to be,
    the parser cannot see half a module — and then dropped, because line 2 is not
    in the fix pass. The line the pass DID write is reported.
    """
    source = ('def older(value):\n'
              '    pass\n'
              '\n'
              '\n'
              'def newer(value):\n'
              '    pass\n')
    diff = ("diff --git a/src/two.py b/src/two.py\n"
            "@@ -1,2 +1,6 @@\n"
            " def older(value):\n"
            "     pass\n"
            "+\n"
            "+\n"
            "+def newer(value):\n"
            "+    pass\n")
    run = review_fix_pass(diff=diff, sources={"src/two.py": source}, rules=RULES)
    placeholders = [f for f in run.findings if f.detail.startswith("slop/placeholder")]
    assert [f.line for f in placeholders] == [5], [f.line for f in run.findings]


def test_without_the_file_the_ast_rules_declare_themselves_rather_than_vanish():
    """`could_not_assess` is the panel's own vocabulary for "I could not judge
    this", and a seat that ran five fewer rules than it claims to have is wrong
    about the one thing it exists to measure."""
    diff = whole_file_diff("src/x.py", "def f(value):\n    pass\n")
    run = review_fix_pass(diff=diff, rules=RULES)
    assert run.findings == []
    assert len(run.could_not_assess) == 5, run.could_not_assess
    assert all("only the fix pass's hunks were available" in gap for gap in run.could_not_assess)


def test_an_unparseable_file_costs_its_ast_rules_and_not_the_round():
    """CI reports a syntax error far better than this can, and much sooner. What
    matters here is that the line rules still run and the round still ends."""
    source = "def f(:\n    # TODO: implement the wide format\n"
    run = review_fix_pass(diff=whole_file_diff("src/broken.py", source),
                          sources={"src/broken.py": source}, rules=RULES)
    assert [f.detail.split(" —", 1)[0] for f in run.findings] == ["slop/unfinished-marker"]
    assert all("SyntaxError" in gap for gap in run.could_not_assess)


# ------------------------------------------------------- the two guard devices


def test_a_string_literal_is_not_evidence_of_the_thing_it_quotes():
    """Prose examples must not fire a code rule — including this module's own.

    `panel_slop`'s docstring and `slop_rules/type-erasure.yaml` both spell out
    `-> Any` in order to explain the rule; a fix pass that documents a convention
    the same way must not be reported for following it.
    """
    source = ('MESSAGE = "an annotation reading -> Any erases the type"\n'
              'def decode(payload: str) -> str:\n'
              '    return payload\n')
    run = review_fix_pass(diff=whole_file_diff("src/prose.py", source),
                          sources={"src/prose.py": source}, rules=RULES)
    assert run.findings == [], [f.detail for f in run.findings]


def test_a_trailing_comment_is_invisible_to_the_comment_rules():
    """A comment rule looks only at lines whose FIRST token is `#`.

    The defects those rules name — narration, captioning, recorded unfinished
    work — are written
    on a line of their own. An aside at the end of a line of code is a different
    thing, and reading it would put `retry()  # step 2 of the backoff` in front
    of a rule about captioned functions.
    """
    source = ('def retry(n):\n'
              '    return n  # Step 2: this function then gives up\n')
    run = review_fix_pass(diff=whole_file_diff("src/aside.py", source),
                          sources={"src/aside.py": source}, rules=RULES)
    assert [f.detail.split(" —", 1)[0] for f in run.findings] == []


def test_a_trailing_suppression_is_still_read_by_the_line_rules():
    """The other half of that asymmetry, and the reason it is not simply "ignore
    comments": a bare `# noqa` only ever appears trailing, so blanking trailing
    comments outright would delete the rule that matters most here."""
    source = "def widen(value):\n    return value.decode()  # noqa\n"
    run = review_fix_pass(diff=whole_file_diff("src/sup.py", source),
                          sources={"src/sup.py": source}, rules=RULES)
    assert [f.detail.split(" —", 1)[0] for f in run.findings] == ["slop/bare-lint-suppression"]


# ------------------------------------------------------------- the finding shape


def test_a_finding_is_the_shape_every_other_seat_produces():
    """Same :class:`panel_core.Finding` as a vendor seat, so nothing downstream
    needs to know this one is deterministic: the judge, #78's corroboration
    count, the fix budget and the board all read these fields."""
    findings = scan_fixture("positive", "placeholder-implementation")
    assert findings
    for finding in findings:
        assert finding.reviewer == panel_slop.SEAT == "slop"
        assert finding.severity in SEVERITIES
        assert finding.file == "src/placeholder-implementation.py"
        assert isinstance(finding.line, int) and finding.line > 0
        assert finding.title and "\n" not in finding.title
        assert finding.detail.startswith("slop/")
        # Escalation is a declaration about a judgement, and this seat makes
        # none. #279 refuses a flag with no argument behind it; a rule engine
        # has no argument to offer.
        assert not finding.needs_human and not finding.needs_rereview


def test_findings_are_ordered_by_where_a_reader_would_look_for_them():
    findings = scan_fixture("positive", "step-comment")
    assert [f.line for f in findings] == sorted(f.line for f in findings)


# ----------------------------------------------------------------- reading diffs


def test_a_diff_yields_post_image_line_numbers():
    diff = ("diff --git a/a.py b/a.py\n"
            "@@ -10,3 +10,4 @@\n"
            " ctx\n"
            "-gone\n"
            "+one\n"
            "+two\n"
            " tail\n")
    (changed,) = parse_diff(diff)
    assert changed.path == "a.py"
    assert changed.added == frozenset({11, 12})
    assert changed.lines == {10: "ctx", 11: "one", 12: "two", 13: "tail"}


def test_the_plus_plus_plus_header_wins_where_a_real_git_diff_carries_one():
    """`_fix_range_diff` synthesises the `diff --git` line by string formatting,
    so it is the only path in; a real `git diff` also names the post-image, and
    that is the more trustworthy of the two for a path containing " b/"."""
    diff = ("diff --git a/old/b/name.py b/new/name.py\n"
            "--- a/old/b/name.py\n"
            "+++ b/new/name.py\n"
            "@@ -1 +1 @@\n"
            "+x = 1\n")
    (changed,) = parse_diff(diff)
    assert changed.path == "new/name.py"


def test_several_files_stay_apart():
    diff = (whole_file_diff("one.py", "# Step 1: go\n")
            + whole_file_diff("two.py", "# Step 2: stop\n"))
    run = review_fix_pass(diff=diff, rules=RULES)
    assert sorted(f.file for f in run.findings) == ["one.py", "two.py"]


def test_a_file_the_pass_only_deleted_from_raises_nothing():
    diff = ("diff --git a/gone.py b/gone.py\n"
            "@@ -1,2 +1,0 @@\n"
            "-# Step 1: go\n"
            "-# Step 2: stop\n")
    assert review_fix_pass(diff=diff, rules=RULES).findings == []


def test_a_non_python_path_is_left_alone():
    """`languages` is the gate, and it is checked against the path's suffix. The
    ast matchers would raise on a Markdown file and the comment rules would fire
    on every heading in it."""
    diff = whole_file_diff("README.md", "# Step 1: install\n")
    assert review_fix_pass(diff=diff, rules=RULES).findings == []


# ------------------------------------------------------- the loader, refusing


def write_rule(tmp_path, text, name="r.yaml"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


GOOD = ("id: slop/example\n"
        "severity: P3\n"
        "languages: [python]\n"
        "message: Something concrete happened\n"
        "remediation: Do the concrete thing instead of the other one, it is better\n"
        "match:\n"
        "  kind: comment_regex\n"
        "  pattern: '^#+\\s*nope'\n")


def test_the_good_rule_this_suite_mutates_actually_loads(tmp_path):
    (rule,) = load_rules(write_rule(tmp_path, GOOD))
    assert rule.rule_id == "slop/example"
    assert rule.pattern == "^#+\\s*nope"


@pytest.mark.parametrize("broken, because", [
    (GOOD.replace("id: slop/example\n", ""), "no id"),
    (GOOD.replace("id: slop/example", "id: example"), "id outside the slop namespace"),
    (GOOD.replace("languages: [python]\n", ""), "no languages"),
    (GOOD.replace("languages: [python]", "languages: [rust]"), "a language nothing speaks"),
    (GOOD.replace("severity: P3", "severity: BLOCKER"), "a severity band the panel has not got"),
    (GOOD.replace("kind: comment_regex", "kind: vibes"), "an unknown match kind"),
    (GOOD.replace("  kind: comment_regex\n", ""), "no match kind"),
    (GOOD + "confidence: likely\n", "a key the schema does not have"),
    (GOOD + "severity: P4\n", "a key written twice"),
    (GOOD.replace("remediation:", "message:"), "a duplicate key under another name"),
    (GOOD.replace("kind: comment_regex", "kind: python_unused_import"),
     "a pattern on a kind that would never read it"),
    (GOOD.replace("'^#+\\s*nope'", "'^#+([unclosed'"), "a pattern that does not compile"),
    (GOOD.replace("'^#+\\s*nope'", '"^#+\\s*nope"'), "a double-quoted scalar"),
    (GOOD.replace("  kind:", "    kind:"), "an indent the reader does not accept"),
    ("just a sentence\n", "a file that is not a mapping"),
])
def test_a_bad_rule_file_is_fatal_and_never_skipped(tmp_path, broken, because):
    """Strict, and fatal at load. The alternative — dropping the rule and
    carrying on — produces a seat that reports clean because half of it was
    misspelt, and nothing in the report says so. A rules directory is
    checked-in code; a file in it that does not parse is a build failure, the
    same way a test module that does not import is."""
    with pytest.raises(SlopRuleError):
        load_rules(write_rule(tmp_path, broken))


def test_an_empty_rules_directory_is_fatal(tmp_path):
    """A seat with no rules reports clean, and that is indistinguishable from a
    clean fix pass. `flake.nix` makes the same argument about a test suite that
    finds no tests: a thing that disappears has to be as loud as one that fails."""
    with pytest.raises(SlopRuleError):
        load_rules(tmp_path)


def test_two_files_may_not_claim_one_rule_id(tmp_path):
    write_rule(tmp_path, GOOD, "a.yaml")
    write_rule(tmp_path, GOOD, "b.yaml")
    with pytest.raises(SlopRuleError):
        load_rules(tmp_path)


def test_a_hash_inside_a_quoted_pattern_is_not_a_comment(tmp_path):
    """The reader applies the same first-token rule to its own files that the
    comment matcher applies to Python — which it has to, since every comment
    pattern in `slop_rules/` starts with an escaped `#`."""
    (rule,) = load_rules(write_rule(tmp_path, GOOD))
    assert rule.compiled.search("# nope")


# ------------------------------------------------- the fixtures are real Python


@pytest.mark.parametrize("rule_id", RULE_IDS)
def test_both_fixtures_parse(rule_id):
    """A fixture with a syntax error in it would silently stop exercising the ast
    rules, and `test_positive_fixture_fires_its_rule` would keep passing on any
    rule whose evidence is a regex. Checked directly rather than inferred."""
    for kind in ("positive", "false-positive"):
        path = FIXTURES / kind / f"{stem_of(rule_id)}.py"
        ast.parse(path.read_text(encoding="utf-8"))


# --------------------------------------------------- the seat, held to its own rules


@pytest.mark.parametrize("module", ["panel_slop.py", "tests/test_panel_slop.py"])
def test_the_seat_does_not_fire_on_the_prose_that_explains_it(module):
    """The rule set read over its own source, as if a fix pass had written all of it.

    The cheapest way for this seat to lose its credibility is to report the
    comment describing the rule that reported it, and the first version of this
    module did exactly that — four findings, every one of them a docstring here
    quoting ``# noqa`` while arguing about ``# noqa``. That is what the
    backtick-stripping and the multi-line-string handling in :func:`_code_view`
    and :func:`apply_rules` exist for, and this is the test that keeps them.

    Run over every `.py` file in the repo the same way, the ten rules raise 90
    findings across 252 files, and each of the remaining classes is a true
    positive by its own rule's definition — the number is in the report on #780.
    That scan cannot live here: this sandbox holds `harness/loops` and not the
    repo. Two files that ARE here, and are the two most likely to trip it, can.
    """
    source = (Path(__file__).resolve().parent.parent / module).read_text(encoding="utf-8")
    run = review_fix_pass(diff=whole_file_diff(module, source),
                          sources={module: source}, rules=RULES)
    assert run.findings == [], [f"{f.line}: {f.detail.splitlines()[0]}" for f in run.findings]


def test_a_numbered_item_inside_a_block_of_prose_is_not_a_step_comment():
    """The guard the false-positive fixtures cannot express, because it is about
    a comment's NEIGHBOURS rather than its text.

    `harness_rules` enumerates what a per-box overlay may do as `#  1.`, `#  2.`,
    `#  3.` inside one paragraph; `panel_rounds` numbers a rule's clauses the
    same way. Both are the writing this repo asks for. Only the opening line of a
    comment run is read, so a block that really is narration still reports —
    once, on its first line, which is also the better finding.
    """
    source = ("def settle(rows):\n"
              "    # The overlay may do three things, and only three:\n"
              "    #  1. narrow a seat to off\n"
              "    #  2. pin a model\n"
              "    return rows\n")
    run = review_fix_pass(diff=whole_file_diff("src/prose.py", source),
                          sources={"src/prose.py": source}, rules=RULES)
    assert run.findings == [], [f.detail.splitlines()[0] for f in run.findings]


def test_a_backticked_example_in_a_trailing_comment_is_not_evidence():
    """The backtick device, in the one place nothing else covers it.

    A docstring is handled twice over — the parse tree blanks its lines, and
    failing that the quote characters give it away. A TRAILING comment has
    neither: :func:`_code_view` deliberately keeps it, because a bare `# noqa`
    only ever appears there, so a line rule reads it in full. What is left to
    tell a quoted example from real code is the convention this repo actually
    writes with — code quoted inline in backticks.
    """
    source = ('def decode(payload):\n'
              '    return payload  # widening this to `-> Any` would erase the type\n')
    run = review_fix_pass(diff=whole_file_diff("src/doc.py", source), rules=RULES)
    assert [f.detail.splitlines()[0] for f in run.findings
            if f.detail.startswith("slop/type-erasure")] == []
