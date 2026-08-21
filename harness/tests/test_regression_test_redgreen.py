"""The red/green instruction: every brief that writes a fix must ask its tests to fail first.

A regression test written alongside its fix has never run against the broken code.
On PR #90 that gap cost a round: `load_baseline`'s anchor selection was order-
dependent, and a deliberate, docstring'd test for exactly that behaviour passed —
its fixture happened to list the two baselines in the working order. The assertion
was right and the defect was found a round later in code that was already
"covered", because nobody had ever run the test against the bug.

The fix for that is an instruction, not a mechanism (#114): remove the fix, watch the
new test go red on its assertion, restore, watch it go green. Instructions rot in a
way code does not — the paragraph gets rewritten, the file gets reorganised, one of
the three briefs gets updated and the others do not — so what is pinned here is that
every brief which tells a fixer to write a regression test also tells it to prove
that test would have failed.

Three things, each of which failed differently before #114:

1. **Every fix-writing brief carries it.** Not just the panel-driven one: the
   failure mode is not specific to panel fixes, and `/fix-issue` writes the pass
   that, at the round cap, nothing reviews.
2. **The exemption is stated.** A test for a path the fix *created* has no pre-fix
   code to fail against. An instruction with no exemption for the legitimate case
   gets worked around rather than followed, and a worked-around instruction is worse
   than an honest `N-A`.
3. **The panel asks about load-bearing tests, not only absent ones.** `REVIEW_PROMPT`
   asked for "new code paths … that lack a test", a question #90's fixture answered
   correctly. Reading the tests as tests is the cheap lever; mutation testing on a
   diff is the expensive one.

Asserted on the shipped text, because the shipped text is the whole artefact — these
files ARE the behaviour, there is no implementation behind them to test instead.
Patterns rather than literal paragraphs, so a rewrite that keeps the meaning keeps
this suite green and one that drops the instruction does not.

Run: pytest harness/tests
"""

import re
import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1]
# No module-level `COMMANDS`: it was a route straight past `brief()` below, and the last round
# removed exactly this constant from test_commands_wired.py for that reason while leaving it
# here — the next read reaches for the constant that already exists rather than the accessor.

sys.path.insert(0, str(HARNESS / "loops"))

# The briefs that tell a fixer to write a regression test, and therefore owe it a
# red/green step. `panel-review-pr.md` is deliberately absent: it lifts
# `review-pr.md`'s SUB-AGENT BRIEF verbatim by design and single-sources it, so
# requiring its own copy here would be requiring the duplication that file exists
# to avoid. `fix-and-land.md` is absent for the same reason one level up — it runs
# `/fix-issue` and `/review-pr` rather than briefing a fixer itself.
FIX_BRIEFS = ("review-pr.md", "fix-issue.md", "fix-issue-here.md")

#: The one brief that is not a fix loop: `/panel` reviews and never fixes, so it is read to
#: assert an ABSENCE rather than a behaviour.
PANEL = "panel.md"

#: Every repo-root path this suite reads, declared in one place.
#:
#: It is a declaration rather than a summary because `brief` refuses anything absent from it, so
#: a new read cannot be added without adding it here. That is what the sandbox needs: this suite
#: reads files outside its own directory, and `nix build .#checks.<system>.prose-consistency-tests`
#: runs it against a sandbox holding only what that check installs. A read nobody installed does
#: not FAIL there, it ERRORS on a missing file — which is how #163 sat unnoticed, and #246, and
#: #251, and this suite (#257). `_prose_sandbox` compares this set against the check's installs.
READS = frozenset(f"harness/commands/{name}" for name in (*FIX_BRIEFS, PANEL))


def brief(name: str) -> Path:
    """The path to a command brief, refused if this suite has not declared it.

    One accessor is the whole reason `READS` can be trusted: every read here comes through it,
    so this single assertion is what makes that set complete rather than a list somebody has to
    remember to update."""
    rel = f"harness/commands/{name}"
    # Note the shape of this accessor, and its limit: it takes a bare filename and hard-codes
    # the briefs directory onto it, so it can only ever express a read inside
    # `harness/commands/`. That is every filesystem read this suite makes — but it is not every
    # dependency: the module also does `sys.path.insert` on `harness/loops` and imports
    # panel_core, which no path gate can see, and which is why `harness/loops` is declared as a
    # TREE in `_prose_sandbox` rather than as a read here.
    assert rel in READS, (
        f"{rel!r} is read here but is not in READS, so flake.nix's prose-consistency-tests "
        f"check does not know to install it — where this read would error as a FileNotFoundError "
        f"rather than be asserted. Add it to READS and install it in that check.")
    return HARNESS / "commands" / name


@pytest.fixture(scope="module")
def briefs():
    """Each fix-writing brief's text, keyed by filename."""
    out = {}
    for name in FIX_BRIEFS:
        path = brief(name)
        assert path.exists(), f"{name} has moved — this suite is now green about nothing"
        out[name] = path.read_text(encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def review_prompt():
    """The panel's reviewer prompt, from the module that ships it.

    Imported rather than grepped out of the file: the prompt is a module constant
    and asking Python for it is the one read that cannot drift from what the panel
    actually sends."""
    try:
        import panel_core
    except ImportError as exc:  # pragma: no cover - a real breakage, not a skip
        pytest.fail(f"panel_core will not import, so its prompts cannot be checked: {exc}")
    return panel_core.REVIEW_PROMPT


# ---- the briefs ------------------------------------------------------------

def test_every_fix_writing_brief_asks_for_a_regression_test(briefs):
    """The premise the rest of this file rests on.

    If a brief stopped asking for a regression test at all, the red/green
    assertions below would be checking the shape of an instruction that no longer
    has anything to apply to — and they would fail with a message about red/green
    rather than about the much larger thing that went missing.

    `\\s+` rather than a literal space: this is prose wrapped at 70-odd columns and
    `review-pr.md` breaks the phrase across a newline. Matched with a plain space,
    this test went red against the pre-#114 files — which is a premise test failing
    on the premise being true, and would have read as evidence the instruction was
    what it was measuring."""
    for name, text in briefs.items():
        assert re.search(r"regression\s+test", text, re.IGNORECASE), (
            f"{name} no longer asks for a regression test — the red/green step "
            f"below has nothing left to qualify")


def test_every_fix_writing_brief_requires_the_test_to_fail_first(briefs):
    """Red before green, in the order that means something.

    The instruction has to name BOTH halves. "Confirm the test passes" is what every
    test run already does and is not the check; "confirm it fails" without a restore
    leaves the fix stashed. What is matched is the red half plus a restore, in either
    the stash spelling or the pre-fix-blob one.

    Singular and plural both, because the briefs differ legitimately: one runs "the
    new tests" as a set and one walks "each one". The first draft of this test matched
    only "it fails" and failed on `fix-issue-here.md`, which says "confirm they fail" —
    a pattern narrow enough to fail on correct prose is a pattern that gets deleted."""
    for name, text in briefs.items():
        assert re.search(r"\bred/green\b", text, re.IGNORECASE), (
            f"{name} does not mention red/green — the #114 instruction is gone")
        assert re.search(r"(MUST fail|confirm (it|they|each) fails?"
                         r"|would not have failed|(it|they) fails?\b)",
                         text, re.IGNORECASE), (
            f"{name} mentions red/green but never says the test must FAIL first; "
            f"a step that only confirms green is what every test run already does")
        assert re.search(r"stash|pre-fix (blob|code)", text, re.IGNORECASE), (
            f"{name} says the test must fail but not how to get the broken code back "
            f"(a stash of the fix, or the pre-fix blob)")


def test_the_fix_is_what_gets_removed_not_the_test(briefs):
    """Which side of the diff comes out.

    Remove both and the run that follows collects nothing and exits 5 — not a green
    suite, but not the assertion failing either, and it reads as "it failed without the
    fix" to anyone matching on exit status. Every brief has to say that the new test
    stays where it is."""
    for name, text in briefs.items():
        assert re.search(r"test\* file is not in (this |the )?list|not in the list and stays",
                         text, re.IGNORECASE), (
            f"{name} does not say the new TEST file stays out of the removed set — "
            f"remove both and the red run collects nothing, which is not a red test")


def test_no_brief_tells_a_fixer_to_git_stash(briefs):
    """The mechanism, and it is a fleet property rather than a preference.

    `refs/stash` lives in the COMMON git dir, not the per-worktree one, so every
    worktree of a repo shares one stash stack: a stash pushed in one is listed and
    poppable from all the others, and `stash@{0}` resolves to whatever the last pusher
    meant. This harness runs many concurrent worktrees off one `.git` by design, which
    makes `git stash` the wrong primitive here specifically.

    Verified rather than assumed, and the hard way: the PR that added this instruction
    had its own working tree taken by a concurrent agent in a sibling worktree, which
    popped the red/green stash into its own checkout. Two earlier drafts tried to make
    `git stash` safe — a label check, then an entry count — and the count is what caught
    it, but nothing local can stop another worktree popping the entry.

    So the instruction is a patch file, and what is asserted is the ABSENCE of a stash
    push. Matched on the imperative spellings only: the briefs still say `git stash` in
    order to forbid it, and a test that banned the string would ban the warning."""
    for name, text in briefs.items():
        for spelling in (r"git stash push", r"git stash pop", r"git -C \S+ stash"):
            assert not re.search(spelling, text), (
                f"{name} tells a fixer to run `{spelling}`. Every worktree of a repo "
                f"shares one refs/stash, so a red/green stash is poppable from every "
                f"sibling worktree — capture the fix as a patch file instead")
        assert re.search(r"refs/stash", text), (
            f"{name} does not say WHY stash is wrong here (one shared `refs/stash` "
            f"across every worktree), so the next rewrite will reintroduce it")


def test_the_capture_is_verified_non_empty_before_the_red_run(briefs):
    """The state that reads as success and is not.

    A capture that comes out empty — mistyped paths, or a fix already committed — leaves
    the red run executing with the fix still in place. It passes, and a passing red run
    is indistinguishable from the step having been done. This is the one check the whole
    instruction rests on, and it survived three rewrites of the mechanism underneath
    it."""
    for name, text in briefs.items():
        assert re.search(r"test -s", text), (
            f"{name} never checks the captured patch is non-empty; an empty capture "
            f"means the red run executes with the fix in place and comes out GREEN")
        # And it has to HALT. `test -s f || echo "STOP"` prints a warning, exits 0, and
        # runs the red pass anyway — a check that announces the problem and then does
        # the unsafe thing. Codex caught this on the third pass, when the assertion
        # above was the whole test and the guard underneath it was the broken form.
        assert re.search(r"test -s[^\n]*\|\|[^\n]*exit 1", text), (
            f"{name}'s `test -s` guard does not halt — a bare `|| echo` warns, exits 0 "
            f"and proceeds into the red run with the fix still applied, which is the "
            f"exact state the guard exists to prevent")
        assert re.search(r"already committed", text, re.IGNORECASE), (
            f"{name} does not cover the fix that is already committed, which is the "
            f"common way the capture comes out empty")


def test_a_file_the_fix_added_is_captured_and_removed_correctly(briefs):
    """Both halves of the added-file case, which fail in opposite directions.

    `git diff` ignores untracked files, so without `git add -N` a fix spanning an edit
    and a new module is half-captured and the red run imports the new half. And a path
    absent from HEAD cannot be restored by `git checkout HEAD --`, so the removal of an
    added file is an `rm`. Codex flagged the first half; the second follows from it and
    is the one that errors confusingly rather than silently."""
    for name, text in briefs.items():
        assert re.search(r"add -N", text), (
            f"{name} omits `git add -N`, so `git diff` ignores a file the fix ADDED "
            f"and the red run imports it — the fix is only half removed")
        assert re.search(r"\brm\b", text), (
            f"{name} does not say a file the fix ADDED comes out with `rm`; "
            f"`git checkout HEAD --` cannot restore a path absent from HEAD")


def test_the_failure_has_to_be_the_assertion(briefs):
    """An import error is not a red test.

    A stashed fix routinely takes a symbol the new test imports with it, so the test
    errors during collection. That is the step reporting success on a run that never
    reached the assertion naming the defect — so each brief says what the failure has
    to be.

    Two assertions, positive first, because one alternation over both halves is
    satisfiable by the wrong half alone: a brief that kept the words "import error"
    while dropping "on the assertion" would still match, and that brief no longer
    states the requirement at all. Codex caught exactly that in review."""
    for name, text in briefs.items():
        assert re.search(r"on the assertion|assertion that names", text, re.IGNORECASE), (
            f"{name} does not say the failure must BE the assertion that names the "
            f"defect — without that, a collection error counts as red")
        assert re.search(r"import error|on an import", text, re.IGNORECASE), (
            f"{name} names no counter-example for a failure that proves nothing; "
            f"the import error is the one that actually happens, because a stashed "
            f"fix takes the symbol the new test imports with it")


def test_the_new_code_path_exemption_is_stated_in_every_brief(briefs):
    """The exemption, and it is load-bearing itself.

    Some regression tests cover a path the fix CREATED, so there is no pre-fix code
    to fail against; #114 called this out as the thing that decides whether the
    instruction gets followed or worked around. An instruction that reads as
    "always" against a case where it cannot hold teaches the reader to route around
    it, and the honest `N-A` is what preserves the signal about which tests were
    actually proved."""
    for name, text in briefs.items():
        assert re.search(r"exempt", text, re.IGNORECASE), (
            f"{name} states no exemption; a test for a path the fix CREATED has no "
            f"pre-fix code to fail against and will be worked around instead")
        assert "N-A" in text, (
            f"{name} gives the exemption no reportable form — without one, an exempt "
            f"test and a skipped check look identical in the summary")


def test_the_exemption_does_not_cover_prompts_configs_or_docs(briefs):
    """The exemption's width is the whole ballgame, and the first draft got it wrong.

    That draft exempted "a defect that lived in a prompt string, a config default or a
    doc" — which is most of this harness, and which the PR adding the instruction
    disproved in the act of being written: it changed a prompt string and three
    markdown briefs, and nine of this file's eleven tests went red against the
    previous text. Shipped text is an artefact a test can assert on. Codex flagged it
    in review; an exemption wide enough to cover the awkward cases is how a step
    stops happening at all, so each brief has to say that case is NOT exempt."""
    for name, text in briefs.items():
        # The window the exemption is stated in, so "not exempt" somewhere else in the
        # file cannot satisfy this. Each term asserted SEPARATELY: one alternation over
        # the three is satisfied by whichever survives a rewrite, which is how a
        # narrowing gets quietly widened again. Codex flagged exactly that shape here.
        start = next((m.start() for m in re.finditer(r"exempt", text, re.IGNORECASE)), None)
        assert start is not None, (
            f"{name} states no exemption at all, narrow or otherwise")
        window = text[start:start + 1600]
        assert re.search(r"not\b[^.]{0,80}exempt|exempt[^.]{0,80}\bnot\b",
                         window, re.IGNORECASE | re.DOTALL), (
            f"{name} states an exemption and never a limit on it — an exemption with "
            f"no `not exempt` clause is the wide one #114 predicted")
        for term in ("prompt string", "config default", "already exist"):
            assert re.search(term, window, re.IGNORECASE), (
                f"{name}'s exemption does not mention `{term}`, so the case it must "
                f"NOT cover is unstated: shipped text that already existed is an "
                f"artefact a test can assert on, and exempting it excuses most of "
                f"this harness from its own check")


def test_review_pr_reports_the_redgreen_count(briefs):
    """The summary table is the only place the orchestrator sees this happened.

    `/review-pr`'s fixer returns a fixed table and nothing else; a step with no line
    in it is a step whose omission is invisible. It sits beside `DB-backed`, which
    was added for the same reason."""
    text = briefs["review-pr.md"]
    # Located, not indexed. `str.index` raising ValueError is a test that dies without
    # naming an assertion — the exact failure mode this whole file is about.
    marker = "## Review Summary"
    assert marker in text, f"review-pr.md no longer has a `{marker}` block to check"
    table = text[text.index(marker):]
    assert re.search(r"Red/green", table, re.IGNORECASE), (
        "review-pr.md's summary table has no Red/green line, so a fixer that "
        "skipped the step reports identically to one that did it")


def test_review_pr_asks_the_reviewer_to_read_tests_as_tests(briefs):
    """The review half, distinct from the fix half.

    Step 2's Completeness list asked only whether a test was ABSENT. #90's fixture
    was present and passing, so that question had nothing to say about it — the
    reviewer has to be told to ask whether a present test is load-bearing."""
    text = briefs["review-pr.md"]
    assert re.search(r"still pass with the bug (put )?back|as \*\*tests\*\*|load-bearing",
                     text, re.IGNORECASE), (
        "review-pr.md's review step asks only about MISSING tests; a test that would "
        "still pass with the bug restored is not a missing test")


# ---- the panel's prompt ----------------------------------------------------

def test_the_review_prompt_asks_whether_a_present_test_is_load_bearing(review_prompt):
    """The cheaper lever than mutation testing, and the one #114 recommends starting from.

    The prompt's own dimension list is what a reviewer works through, and its test
    line asked for paths "that lack a test". A vacuous regression test is not a
    lacking one, so the panel could read PR #90's diff correctly and say nothing
    about the fixture that hid the defect."""
    assert re.search(r"load-bearing", review_prompt, re.IGNORECASE), (
        "REVIEW_PROMPT has no load-bearing-tests dimension; it asks only whether a "
        "test is absent, which is the question PR #90's fixture answered correctly")
    assert re.search(r"would not have failed|still pass", review_prompt, re.IGNORECASE), (
        "REVIEW_PROMPT names load-bearing tests but never says what makes one absent: "
        "that the test would not have failed against the defect it names")


def test_the_review_prompt_keeps_asking_about_absent_tests_too(review_prompt):
    """Added to, not swapped for.

    The absence question is still the majority of real test findings; a change that
    replaced it with the load-bearing one would trade a common catch for a rare one
    and this suite would have applauded."""
    assert re.search(r"lack a test", review_prompt), (
        "REVIEW_PROMPT no longer asks about code paths that LACK a test — the "
        "load-bearing dimension was meant to be added alongside it, not to replace it")


def test_the_review_prompt_is_still_one_bullet_per_dimension(review_prompt):
    """A prompt read as a checklist, so a dimension has to look like the others.

    The new lines are a `- ` bullet with wrapped continuations, matching every
    dimension around them. A block that broke that shape would still contain the
    words this file greps for while no longer reading as an item to work through."""
    for marker in ("Review for:", "Severity:"):
        assert marker in review_prompt, (
            f"REVIEW_PROMPT no longer contains `{marker}`, so its dimension list "
            f"cannot be delimited — this test cannot report on a shape that moved")
    review_block = review_prompt[review_prompt.index("Review for:"):]
    review_block = review_block[:review_block.index("Severity:")]
    bullets = [ln for ln in review_block.splitlines() if ln.startswith("- ")]
    assert len(bullets) >= 9, f"expected the full dimension list, found {len(bullets)} bullets"
    load_bearing = [ln for ln in bullets if "Load-bearing" in ln]
    assert len(load_bearing) == 1, (
        f"the load-bearing dimension is not a single top-level bullet in the "
        f"dimension list ({len(load_bearing)} found)")


def test_the_panel_command_describes_the_dimensions_it_actually_sends(briefs):
    """`/panel`'s own summary of the bar, which is prose beside a prompt and drifts.

    It enumerates the review dimensions for a reader deciding whether to run the
    panel. A dimension in `REVIEW_PROMPT` and not in that list is a reviewer doing
    work the command says it does not do."""
    text = brief(PANEL).read_text(encoding="utf-8")
    assert re.search(r"load-bearing", text, re.IGNORECASE), (
        "panel.md's dimension list does not mention load-bearing tests, which "
        "REVIEW_PROMPT now asks every reviewer for")


def test_the_reads_are_declared_rather_than_summarised():
    """`brief`'s refusal is the mechanism `_prose_sandbox`'s comparison rests on for this suite,
    and it had no test — an identical mechanism in test_fixer_escalation got one, and a broken
    refusal here would have gone unnoticed while the guards reported the suite as covered."""
    with pytest.raises(AssertionError, match="not in READS"):
        brief("no-such-brief.md")
    with pytest.raises(AssertionError, match="install it"):
        brief("loops.md")          # a real file, and still not one this suite declared
