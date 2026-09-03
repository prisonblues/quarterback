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

A fourth subject arrived later and is the same lever pointed at the other artefact (#724).
`REVIEW_PROMPT` grew a dimension for load-bearing *prose*: a comment or docstring the diff
WRITES that states a checkable property of the code beside it is a claim, and a claim in a
diff is reviewable. It sits in this file rather than one of its own because the two
dimensions are one idea — a test that cannot fail and a comment that is not true are both an
assertion nobody checked — and because the `/panel` and `/review-pr` drift guards those
dimensions need were already here. Measured on the three PRs landed 2026-09-03: of the three
defects a final adversarial pass found across #720, #719 and #715, two were in the prose and
the third was a prose claim that happened to be true. A wrong comment is worse than a wrong
test in one specific way, and it is what the bullet has to say: a wrong test can at least go
red, whereas nothing ever EXECUTES a comment, so a wrong one survives every round, every CI
run and every rebase.

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


def _completeness(text: str) -> str:
    """`/review-pr`'s **Completeness** paragraph — the operative section, not the brief.

    Delimited for `_dimensions`' reason one artefact over, and this change is what made it
    concrete rather than hypothetical. The `Unverified claims` block it adds to the report
    format matches TWO of the three patterns asserted below ("comments … claims" and
    "laborious"), so a whole-file search passes with the Completeness instruction deleted and
    only the reporting line left standing — an assertion that cannot fail on the thing it names,
    which is the shape this file exists to object to."""
    start = text.index("**Completeness:**")
    rest = text[start + len("**Completeness:**"):]
    nxt = re.search(r"^\*\*[A-Z]", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def _summary_block(text: str) -> str:
    """The report format `/review-pr` tells a reviewer to return, delimited by its fence.

    A separate accessor from `_completeness` because the two answer different halves: what the
    reviewer is told to LOOK for, and what the reviewer is made to SAY. A dimension present in
    the first and absent from the second is a check with no channel to report through."""
    start = text.index("## Review Summary")
    return text[start:text.index("\n```", start)]


def _summary_field(summary: str, name: str) -> str:
    """One declaration line of the report format, bounded to its OWN block.

    A field is a heading line plus its indented continuation, ended by a blank line. Bounding
    matters and the first draft of this file proved why: the `none` assertion below scanned from
    the field's heading to the end of the summary, where `Docs updated: ... (or "none needed")`
    would have answered it — so the assertion passed whether or not the field it names spells its
    empty case. That is the shape this file objects to, found by an independent pass on the very
    change that adds the dimension for it."""
    start = summary.lower().index(name.lower())
    rest = summary[start:]
    end = rest.find("\n\n")
    return rest if end < 0 else rest[:end]


def _panel_dimensions(text: str) -> str:
    """`/panel`'s own enumeration of the dimensions it sends — the parenthesis after "Craft
    review", and not the hundred-odd lines of argument-parsing and workflow below it."""
    start = text.index("Craft review (")
    return text[start:text.index(")", start)]


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


def test_review_pr_asks_the_reviewer_to_read_comments_as_claims(briefs):
    """The review half again, and the sibling of the tests question above it.

    `/review-pr` and the panel apply the same bar by design — `panel.md`'s opening paragraph
    says "the same exhaustive bar as `/review-pr`" — so a dimension in one and not the other
    means which reviewer you spent decides whether the comments in the diff were read at all.

    Three assertions because the dimension is three things, and a brief carrying only the first
    is the one that quietly changes behaviour: the check, the severity rule that decides what a
    fix pass does about it, and the instruction to LOOK before declaring a claim unverifiable."""
    section = _completeness(briefs["review-pr.md"])
    assert re.search(r"comments?\b[^.]*\bclaims?\b", section, re.IGNORECASE), (
        "review-pr.md's Completeness section asks for stale docs but never asks whether a "
        "comment the diff WROTE is true of the code beside it, which REVIEW_PROMPT has asked "
        "since #724")
    assert re.search(r"rests? on|leans on|depends on", section, re.IGNORECASE), (
        "review-pr.md carries the check without the rule that prices it, so every false "
        "comment claim reads as the same severity — which is the blanket floor #724's first "
        "cut shipped and this one replaced")
    assert re.search(r"laborious|only then|before you decide", section, re.IGNORECASE), (
        "review-pr.md never says that checking a structural claim is laborious rather than "
        "impossible, so a reviewer declares one unverifiable without opening a file")


def test_review_pr_reports_the_claims_it_could_not_verify(briefs):
    """The other half of the parity, and the half that was missing when it was first claimed.

    `REVIEW_PROMPT` routes a claim the material cannot settle into `could_not_assess`, which is
    a channel the envelope already has. `/review-pr` has no envelope — it returns a table — so
    carrying the dimension across without a line to put the residue on left the workflow with
    the check and nowhere to report the uncertainty, while the PR body claimed parity. That
    residue is also what #724 defers its expensive half on, so a workflow that cannot record it
    cannot contribute to the count."""
    summary = _summary_block(briefs["review-pr.md"])
    assert re.search(r"unverified claims", summary, re.IGNORECASE), (
        "review-pr.md's Completeness section asks the reviewer to check the diff's comment "
        "claims, and its report format has no line for the ones it could not settle — so the "
        "uncertainty half of the dimension is asked for and cannot be reported")
    assert re.search(r"\bnone\b", _summary_field(summary, "Unverified claims"),
                     re.IGNORECASE), (
        "review-pr.md's `Unverified claims` line does not spell its empty case, so a pass with "
        "nothing to declare reads identically to one that forgot the line — `Surface` above it "
        "writes `none` out for exactly this reason")


# ---- the panel's prompt ----------------------------------------------------

def _dimensions(prompt: str) -> str:
    """The `Review for:` checklist, delimited from the severity paragraph that follows it.

    A helper rather than a fourth inline slice: several tests here ask about the list and one
    asks about the severity paragraph after it, and bounding the search is what keeps those
    questions distinct from the rest of the brief. Two of them would otherwise be answered by
    text no reviewer works through as a dimension — `_FINDINGS_ENVELOPE` names
    `could_not_assess` and spells severities `"P1|P2|P3|P4"`, so a whole-prompt grep for either
    passes with the dimension that was supposed to carry it deleted."""
    for marker in ("Review for:", "Severity:"):
        assert marker in prompt, (
            f"REVIEW_PROMPT no longer contains `{marker}`, so its dimension list "
            f"cannot be delimited — this test cannot report on a shape that moved")
    block = prompt[prompt.index("Review for:"):]
    return block[:block.index("Severity:")]


def _bullets(block: str) -> list[str]:
    """The dimension list as whole bullets, continuation lines folded onto their opener.

    `- ` opens a dimension and an indented line continues it, which is the shape
    `test_the_review_prompt_is_still_one_bullet_per_dimension` pins. Folding is what makes the
    assertions below about a NAMED dimension rather than about the list: every argument a
    dimension makes is on its continuation lines — the comments bullet's "claim" is on its
    third and its severity floor on its last — so an unfolded, line-wise search finds neither,
    and the obvious repair, searching the whole block, is answered by any bullet in it."""
    out: list[str] = []
    for line in block.splitlines():
        if line.startswith("- "):
            out.append(line[2:])
        elif out and line.startswith("  ") and line.strip():
            out[-1] += " " + line.strip()
    return out


def _dimension(prompt: str, name: str) -> str:
    """The one dimension whose NAME — the text before its first colon — matches `name`.

    Matched on the NAME rather than anywhere in the bullet, because dimension bodies contain
    each other's subject words — `Test coverage`'s body says "a test" and `Load-bearing tests`'s
    says "no test" — so a body match for a word this file greps on returns whichever came first
    and then reports confidently about the wrong bullet."""
    hits = [b for b in _bullets(_dimensions(prompt))
            if re.search(name, b.split(":", 1)[0], re.IGNORECASE)]
    assert len(hits) == 1, (
        f"expected exactly one dimension whose name matches /{name}/, found {len(hits)}: "
        f"{[b.split(':', 1)[0] for b in hits]}")
    return hits[0]


def _severity_tier(prompt: str, tier: str) -> str:
    """What the severity paragraph offers as examples of one tier, and nothing from the next.

    Sliced at the `·` separators rather than searched whole, so "is a comment claim listed
    under P2?" cannot be answered by it being listed under P3 — which is the question, since
    the default reading of a comment defect is that it is polish."""
    para = prompt[prompt.index("Severity:"):]
    para = para[:para.index("\n\n")]
    rest = para[para.index(f"{tier} "):]
    return rest.split("\u00b7", 1)[0]


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
    review_block = _dimensions(review_prompt)
    bullets = [f"- {b}" for b in _bullets(review_block)]
    assert len(bullets) >= 10, f"expected the full dimension list, found {len(bullets)} bullets"
    # Counted per NAMED dimension rather than per occurrence of "Load-bearing". There are two of
    # them now — tests (#114) and comments (#724) — so a bare substring count asserts they have not
    # both arrived, which is the opposite of what this test is for.
    for name in ("Load-bearing tests", "Load-bearing comments"):
        opened = [ln for ln in bullets if ln.startswith(f"- {name}")]
        assert len(opened) == 1, (
            f"`{name}` is not a single top-level bullet in the dimension list "
            f"({len(opened)} found)")


def test_the_review_prompt_asks_whether_a_comment_the_diff_writes_is_true(review_prompt):
    """#724: the dimension the tests one has had since #114, pointed at the other artefact.

    The Documentation bullet beside it catches a comment the diff LEFT BEHIND — behaviour
    changed, the docstring did not. It has nothing to say about one the diff WROTE, and that is
    where the defects were: of the three a final adversarial pass found across #720, #719 and
    #715, two were comments asserting a property the code did not have (`qb-reconcile`'s
    `APPLY_STATE` describing a fresh read that never happens, `panel_seats.PR_HOLD_TTL`
    comparing against the wrong default) and the third was a load-bearing claim that happened
    to be true.

    Asserted on the concrete shapes as well as the word "claim", for the reason the tests
    dimension names its own: a bullet that says "check the comments" without saying what a
    checkable claim looks like is read as the staleness bullet restated."""
    bullet = _dimension(review_prompt, r"comment")
    assert re.search(r"\bclaim", bullet, re.IGNORECASE), (
        "REVIEW_PROMPT's comment dimension never says a comment is a CLAIM, which is the "
        "whole move — a claim in the diff is reviewable and a description of it is not")
    shapes = [pat for pat in (r"only caller", r"return", r"re-read", r"reach")
              if re.search(pat, bullet, re.IGNORECASE)]
    assert len(shapes) >= 3, (
        f"REVIEW_PROMPT's comment dimension names {len(shapes)} of the concrete claim shapes "
        f"(only caller / nothing returns / re-reads / cannot be reached); without them it "
        f"reads as the Documentation bullet asking for staleness a second time")


def test_the_review_prompt_says_why_a_wrong_comment_outlives_a_wrong_test(review_prompt):
    """The argument, not just the instruction — and it is the reason this is not a nit.

    A reviewer that reads "check the comments" prices it as polish, because in most codebases
    it is. What makes it P2 here is that a wrong test can at least go red, and nothing ever
    executes a comment: it survives every round, every CI run and every rebase, and the next
    change is made on the strength of it. Dropping that sentence leaves a dimension whose
    findings the master judge has no reason to keep."""
    bullet = _dimension(review_prompt, r"comment")
    assert re.search(r"execut", bullet, re.IGNORECASE), (
        "REVIEW_PROMPT's comment dimension asks for the check but never says why a wrong "
        "comment outlives a wrong test — that nothing ever executes it")
    assert re.search(r"surviv|round|rebase", bullet, re.IGNORECASE), (
        "REVIEW_PROMPT's comment dimension does not say what that costs: a wrong comment "
        "survives every round, CI run and rebase, and is then built on")


def test_an_unverifiable_comment_claim_has_somewhere_to_go(review_prompt):
    """The #715 case — with the channel gated on having actually looked.

    `panel.run`'s "there is no early return between the claim and here" is TRUE, and an AST walk
    over 2,600 lines is what settled it here. That is convenience, not necessity: grep `return`
    over the span and read the enclosing scopes and a seat with no shell reaches the same
    answer. The first cut of this said such a claim was unsettleable, which is false — and false
    in the direction that costs something, because `CODE_ACCESS_BRIEF`'s own rule is that a
    question you can answer by opening a file is not a coverage gap, and a declared gap costs
    the round its confident stop.

    So both assertions, and the second is the one that keeps the first honest: a channel with no
    obligation to look first manufactures declarations, and the residue is what #724 defers its
    expensive half on. A count inflated by claims nobody opened a file about is not a
    measurement of anything."""
    bullet = _dimension(review_prompt, r"comment")
    assert "could_not_assess" in bullet, (
        "REVIEW_PROMPT's comment dimension asks a seat to check claims the material may not "
        "settle and gives it nowhere to say so — an unreported uncertainty is indistinguishable "
        "from a claim that was checked and held")
    assert re.search(r"laborious|after you have looked|grep the callers", bullet, re.IGNORECASE), (
        "REVIEW_PROMPT's comment dimension offers `could_not_assess` without saying that "
        "checking is usually laborious rather than impossible, so a seat declares a coverage "
        "gap — which costs the round its confident stop — instead of opening a file")


def test_the_comment_severity_is_conditioned_on_what_rests_on_the_claim(review_prompt):
    """A discriminator, not a floor — and the first cut of #724 shipped the floor.

    "At least P2 for any false checkable claim" is assigned by ARTEFACT TYPE, and severity is
    what the fix pass acts on: a blanket floor sweeps in a stale count, a wrong complexity
    description and a local detail nothing rests on, turns each into a mandatory repair and
    lengthens the round. The consequence of relying on the claim is the thing that varies, so it
    is what the rule keys on.

    Asserted in both places and asserted to AGREE. The dimension list says what to look for and
    the severity paragraph says what it is worth; a seat reaching for a P-number reads the
    second, so a rule stated only in the bullet is a rule the scale overrides. P1 and P4 are
    asserted silent because a two-way split with a third tier offering the same example is not a
    split — the seat picks whichever it reads first."""
    bullet = _dimension(review_prompt, r"comment")
    assert re.search(r"rests? on|leans on|depends on", bullet, re.IGNORECASE), (
        "REVIEW_PROMPT's comment dimension names no discriminator, so every false claim in a "
        "comment is priced the same — which is the blanket floor this replaced")
    assert "P2" in bullet and "P3" in bullet, (
        "REVIEW_PROMPT's comment dimension does not price BOTH cases, so the case it leaves "
        "unpriced is decided by whatever the seat reads next")
    for tier, expect in (("P2", r"rests on|leans on"), ("P3", r"nothing rests on|nothing")):
        text = _severity_tier(review_prompt, tier)
        assert re.search(r"comment", text, re.IGNORECASE), (
            f"REVIEW_PROMPT's severity paragraph does not name a comment claim under {tier}; "
            f"the bullet's rule and the scale a seat picks from disagree, and the scale wins")
        assert re.search(expect, text, re.IGNORECASE), (
            f"REVIEW_PROMPT lists a comment claim under {tier} unconditionally, so the two "
            f"tiers offer the same example and the discriminator decides nothing")
    for tier in ("P1", "P4"):
        assert not re.search(r"comment", _severity_tier(review_prompt, tier), re.IGNORECASE), (
            f"REVIEW_PROMPT offers a comment claim as an example of {tier} as well, so the "
            f"P2/P3 split is not a split — a seat picks whichever tier it reads first")


def test_the_review_prompt_keeps_asking_about_stale_docs_too(review_prompt):
    """Added to, not swapped for — the same guard the absent-tests question has.

    Staleness is the common case and the claim is the rare one; a change that folded the two
    into a single "documentation" bullet would trade the frequent catch for the sharp one and
    every other assertion in this file would still pass."""
    stale = _dimension(review_prompt, r"documentation")
    assert re.search(r"stale", stale, re.IGNORECASE), (
        "REVIEW_PROMPT no longer asks whether a behaviour change left CLAUDE.md, docs, README "
        "or docstrings stale — the comment-claims dimension was added beside that question, "
        "not in place of it")


def test_the_panel_command_describes_the_dimensions_it_actually_sends(briefs):
    """`/panel`'s own summary of the bar, which is prose beside a prompt and drifts.

    It enumerates the review dimensions for a reader deciding whether to run the
    panel. A dimension in `REVIEW_PROMPT` and not in that list is a reviewer doing
    work the command says it does not do."""
    listed = _panel_dimensions(brief(PANEL).read_text(encoding="utf-8"))
    assert re.search(r"load-bearing", listed, re.IGNORECASE), (
        "panel.md's dimension list does not mention load-bearing tests, which "
        "REVIEW_PROMPT now asks every reviewer for")
    assert re.search(r"comment[^)]*\b(true|claim)", listed, re.IGNORECASE), (
        "panel.md's dimension list does not mention the comments the diff writes, which "
        "REVIEW_PROMPT has asked every reviewer about since #724 — a reader deciding whether "
        "to spend a panel is told it reviews less than it does")


def test_the_reads_are_declared_rather_than_summarised():
    """`brief`'s refusal is the mechanism `_prose_sandbox`'s comparison rests on for this suite,
    and it had no test — an identical mechanism in test_fixer_escalation got one, and a broken
    refusal here would have gone unnoticed while the guards reported the suite as covered."""
    with pytest.raises(AssertionError, match="not in READS"):
        brief("no-such-brief.md")
    with pytest.raises(AssertionError, match="install it"):
        brief("loops.md")          # a real file, and still not one this suite declared
