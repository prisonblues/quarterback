"""A fixer may say "the approach is wrong, not the code" — and the halves of that must ship together.

#67's second piece. `review-pr.md`'s brief tells a fixer to fix everything and never
note-and-move-on, which is right and is also why every fixer so far has patched a broken
premise rather than saying so: on PR #61 two rounds and two fixes went on one unexamined
assumption, and the round-2 findings *were* round 1's patch. So the brief now permits one more
outcome — escalate the premise, write no patch — and that permission is only safe while three
things stay true at once. This suite asserts those three, because each of them breaks silently:

1. **The permission and the report ship together.** A fixer allowed to escalate, in a brief
   whose summary has nowhere to say so, has been handed note-and-move-on with a nicer name. The
   permission lives in step 3a, the durable record is the step-5 commit body and the relay is
   step 6's table; nothing but this connects them.
2. **The cross-file anchor resolves.** Every command in `FIX_LOOPS`, and the harness README,
   point at the escalation by the step number it has in `review-pr.md`'s brief — and
   `panel-review-pr.md` lifts that brief verbatim into a sub-agent that cannot see the file, so a
   renumbered heading leaves every one of them pointing at nothing, and pointing at nothing reads
   exactly like pointing at something. The same anchor has two further directions: outward,
   because step 3a tells the fixer to run `panel.py --ask` at a path whose flags have to exist;
   and inward, because the brief cannot cite a section of the file it was lifted out of.
3. **The recorded outcome is a value the database will accept.** An escalation is recorded as
   `deferred`; the vocabulary is a SQL CHECK as well as a Python tuple, so a doc that invents
   `"outcome": "escalated"` costs the row and records nothing. The tuple-vs-constraint agreement
   is already guarded at import in `app/api/reviews.py`; what nothing guarded is the third
   place the vocabulary is written, which is prose an agent copies from.

**What is deliberately NOT asserted: that the escalation is ever used correctly, or at all.**
That is a judgement about one finding at a time, and #67's own limit applies — the evidence is
two PRs, the output is "stop and ask", and nothing here gates anything. Measuring recurrence
mechanically (is this round circling the last round's fix?) is the issue's other piece and is
not built; a test that pretended to check it would be the loudest possible way of implying it is.

**Why it lives under `harness/` rather than in `tests/`.** It reads text files and needs nothing
else, and `tests/conftest.py` resolves `DATABASE_URL` and imports the app at collection. Same
reasoning as `test_release_numbers.py`, which has the longer version. CI discovers every
`harness/**/tests` directory, so it still runs on every push.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
#: Every read is repo-root-relative — the keys of `FIX_LOOPS` and `ANCHOR_DOCS` included, since
#: those are what a failure message names — so one accessor covers the commands, the harness
#: README and the app, and every path printed on the way out is a path a reader can type.
REVIEW_PR = "harness/commands/review-pr.md"

#: The author's own fix pass, and the one loop that runs before a PR exists — so the only
#: place the premise check is shown without `--pr`. Named, like `REVIEW_PR`, because two
#: things read it: the `FIX_LOOPS` entry below and the guard on that snippet.
FIX_ISSUE = "harness/commands/fix-issue.md"

#: The script step 3a tells the fixer to run, and the repo file this suite reads its flags out
#: of. One file at two spellings: `~/.claude/loops` is the installed copy — here a nix store
#: symlink, so it can be a generation behind the checkout and is not byte-identical to it. What
#: a text guard can prove is that the two paths have not drifted apart, which is the one part of
#: the snippet the flag check does not read.
PANEL_PY = "harness/loops/panel.py"
PANEL_PY_INSTALLED = "~/.claude/loops/panel.py"

#: The step number the escalation has in `review-pr.md`'s SUB-AGENT BRIEF, and therefore the
#: token every other file refers to it by. It is a string, not an int: `3a` is a step *between*
#: two steps, which is what it is — a way of fixing, sitting under the step that says to fix.
ESCALATION_STEP = "3a"


class Loop(NamedTuple):
    """Why a command is on the guarded list, and the behaviour it must still spell out."""

    #: Why it is on the list at all, for the failure message.
    why: str
    #: Substrings that have to survive in it. Mentioning `step 3a` is not the property worth
    #: guarding — a file that names the step and still says "repeat until the To fix list is
    #: empty, then merge" mentions it and does the opposite. These are what the file DOES.
    behaviour: tuple[str, ...]
    #: What that behaviour is, in a reader's words, for the failure message.
    behaviour_is: str
    #: True for the file that defines the step rather than pointing at it. Only the message
    #: differs: for the definer a missing token means the definition moved or was reworded, not
    #: that it never heard of its own step.
    defines: bool = False
    #: True where the file also tells an agent to record a finding's outcome on the board — the
    #: subset whose escalation paragraph has to name `deferred`.
    records_outcomes: bool = False


#: Every command that tells an agent to run a fix pass over review findings, with why it is on
#: the list. A loop that has not heard of the escalation re-briefs an escalated finding to a
#: fresh fixer, who writes the patch the last one declined to write — the round that
#: manufactures the next round's findings, arriving through the rule meant to prevent them.
#: Hardcoded rather than discovered: "which files drive a fix pass" is a judgement, and a
#: pattern loose enough to find them all also finds `/panel` (which reviews and never fixes).
#: A rename fails this suite loudly, which is the intent.
FIX_LOOPS = {
    REVIEW_PR: Loop(
        why="defines the escalation; the canonical brief every other path lifts",
        behaviour=("Write no patch for it", "Never redesign on your own authority"),
        behaviour_is="the permission itself — no patch, and no redesign on a fixer's authority",
        defines=True,
        records_outcomes=True,
    ),
    "harness/commands/panel-review-pr.md": Loop(
        why="cycles panel → fix → panel, and its §4 is what would re-brief it",
        behaviour=("Never re-brief an escalated finding to a fixer",
                   "Match it by premise, not by key"),
        behaviour_is="that an escalated finding never goes back to a fixer — including when a "
                     "later round re-reports the same premise under a new finding key",
        records_outcomes=True,
    ),
    "harness/commands/fix-and-land.md": Loop(
        why="repeats review→fix until the To fix list is empty, then MERGES",
        behaviour=("do **not** merge", "stop for a human"),
        behaviour_is="that the loop stops and does not merge, since an escalated finding never "
                     "leaves the To fix list and 'repeat until empty' would spin or re-brief it",
    ),
    "harness/commands/epic.md": Loop(
        why="drives /review-pr headlessly per sub-issue and ff-merges the sub-PRs",
        behaviour=("before opening the `epic→base` PR", "into the `epic→base` PR body"),
        behaviour_is="that the driver reads each relay before the epic→base PR and carries the "
                     "escalation into that PR's body, the merge already having happened",
    ),
    FIX_ISSUE: Loop(
        why="runs its own fix pass over review findings, as the code's AUTHOR",
        behaviour=("change the approach", "the patch you did not write"),
        behaviour_is="that the author may take the redesign — the one path where that is its "
                     "call — and writes it up in step 3a's fields if it declines",
    ),
}

#: Files that reference the brief's step number without driving a fix pass, so they belong to
#: the anchor check and not to the behaviour one. `harness/README.md` is the feature's narrative
#: overview: it briefs no agent, which is why it is not in `FIX_LOOPS`, but it names the step and
#: goes stale by the same renumbering.
ANCHOR_DOCS = ("harness/README.md",)

#: The subset of `FIX_LOOPS` that tells an agent to record a finding's outcome on the board.
#: Derived from the entries themselves rather than relisted by name: a rename then fails in one
#: place with one message, which is what the `FIX_LOOPS` comment promises.
RECORDS_OUTCOMES = tuple(
    sorted(name for name, loop in FIX_LOOPS.items() if loop.records_outcomes))

#: Read from the source text rather than imported: `app.api.reviews` pulls in FastAPI, the ORM
#: and the app's settings, and this suite's whole reason for living under `harness/` is that it
#: needs none of them. Tolerant of the edits that do not change the value — an annotation
#: (`OUTCOMES: tuple[str, ...] = (…)`) or a trailing comment — because every assertion below
#: reads the set through it, so a pattern that misses turns the lot into a fixture error.
_OUTCOMES_TUPLE = re.compile(r"^OUTCOMES(?:\s*:[^=]+)?\s*=\s*\(([^)]*)\)", re.MULTILINE)

#: The CHECK constraint the vocabulary is spelled in for a third time. `app/api/reviews.py`
#: guards itself against it at import; this is the name the docs and this suite's own failure
#: message quote, and a rename would leave both citing a constraint that does not exist.
_VOCAB_CONSTRAINT = "ck_review_finding_outcomes_vocabulary"

#: `"outcome": "<value>"` as it appears in the `qb record-outcome` payloads the docs show. The
#: docs are where an agent copies the JSON from, so these literals are the vocabulary in
#: practice however carefully the constant is written.
_DOC_OUTCOME = re.compile(r'"outcome":\s*"([^"]*)"')

#: A reference to a step *of the brief*, in the phrasings the family actually uses: "the brief's
#: step 3a", "`/review-pr`'s step 3a", "`review-pr.md`'s brief (step 3a)". Owner-qualified on
#: purpose — `fix-issue.md` and `fix-and-land.md` have their own numbered steps, and matching a
#: bare "step 4" would check those against the wrong file's headings. Two things the owner half
#: has to get right, and neither is free:
#:
#: * **the left lookbehind.** `findall` anchors nothing, so `review-pr.md` matches inside
#:   `panel-review-pr.md` — reading "`panel-review-pr.md`'s step 5" as a reference to the brief
#:   and checking it against the wrong file's headings. That is a false failure, or a false pass
#:   when the number happens to exist in both, which is the misattribution this pattern exists to
#:   avoid arriving through the pattern itself.
#: * **no bare `its`.** It matched "(its step 3a)" in files whose owner was a sentence away and
#:   would equally have matched "run `/panel` (its step 2)". The owner is spelled out in every
#:   guarded file instead, which is cheaper than teaching a regex what a sentence is.
#:
#: Case-insensitive because a sentence or a heading can start with the reference, and applied to
#: whitespace-normalised text because prose wraps between the owner and the step.
_STEP_REFERENCE = re.compile(
    r"(?<![-\w])(?:the\s+brief|`?/?review-pr(?:\.md)?`?)['’]?s?\s+(?:brief\s+)?"
    r"\(?step\s+(\d+[a-z]?)",
    re.IGNORECASE,
)

#: The README section step 3a sends the fixer to for the `--ask` contract.
_PREMISE_CHECK_SECTION = "The premise check (`--ask`)"


#: Every repo-root path this suite reads, declared in one place.
#:
#: It is a *declaration* rather than a summary because `doc` refuses anything absent from it
#: (below), so a new read cannot be added without adding it here. That is what the sandbox
#: needs: this suite lives two directories below the files it reads, and
#: `nix build .#checks.<system>.prose-consistency-tests` runs it against a sandbox holding only
#: what that check copies in. A read nobody copied in does not FAIL there, it ERRORS on a
#: missing file — which is how #163 sat unnoticed for a day, and #246 after it, and how this
#: suite's own ten reads sat erroring inside `worktree-tests` (#251).
#:
#: Derived from `FIX_LOOPS` and `ANCHOR_DOCS` rather than relisting their entries, so a rename
#: there still fails in one place with one message.
READS = frozenset({
    *FIX_LOOPS,
    *ANCHOR_DOCS,
    PANEL_PY,
    "harness/loops/README.md",
    "app/api/reviews.py",
    "app/models/review.py",
})


@functools.lru_cache(maxsize=None)
def doc(relpath: str) -> str:
    # Encoding spelled out: these files are prose full of em dashes, and `read_text()` takes the
    # platform default, which under a C/POSIX locale is ASCII. A nix build and a minimal CI
    # image both commonly run without a UTF-8 locale, and the failure there is a
    # UnicodeDecodeError at collection rather than anything about briefs.
    #
    # Cached because the parametrised tests read the same handful of files several times each.
    # A plain speed-up: the module-scoped fixtures below still exist, for the slices they name.
    # Refused rather than read, if it is not in `READS`. One accessor is the whole reason this
    # works: every read in this suite comes through here, so this single assertion is what makes
    # `READS` a complete enumeration rather than a list somebody has to remember to update. The
    # alternative — parsing this file for its reads, as #182 and #246 must, since theirs are
    # built inline — cannot chase every way of naming a path and fails silently at the first
    # idiom it misses. Here there is nothing to chase.
    assert relpath in READS, (
        f"{relpath!r} is read here but is not in READS, so flake.nix's prose-consistency-tests "
        f"check does not know to copy it into its sandbox — where this read would error as a "
        f"FileNotFoundError rather than be asserted. Add it to READS and add an `install` line "
        f"for it to that check.")
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def _located(haystack: str, marker: str, what: str, start: int = 0,
             where: str = REVIEW_PR) -> int:
    """`str.index` with a message. A bare `.index()` miss aborts the whole module with
    `ValueError: substring not found`, naming neither the marker nor the file it was looked for
    in — in a suite whose every other failure says what moved."""
    at = haystack.find(marker, start)
    assert at != -1, (
        f"{what} can no longer be located: {marker!r} is not in {where}"
        f"{' after the preceding marker' if start else ''}")
    return at


@pytest.fixture(scope="module")
def review_pr() -> str:
    return doc(REVIEW_PR)


@pytest.fixture(scope="module")
def brief(review_pr: str) -> str:
    """Just the SUB-AGENT BRIEF: what a sub-agent that cannot see the file is handed.

    The orchestrator's own sections around it are addressed to a different reader, so a
    permission or an obligation that lands outside these markers has landed on the wrong agent.
    """
    start = _located(review_pr, "### SUB-AGENT BRIEF", "the start of the brief")
    end = _located(review_pr, "\n## 2b.", "the end of the brief", start)
    return review_pr[start:end]


@pytest.fixture(scope="module")
def escalation(brief: str) -> str:
    """Step 3a alone. Sliced in one place because two tests want it and the boundary moves."""
    start = _located(brief, f"#### {ESCALATION_STEP}.", f"step {ESCALATION_STEP}")
    # Searched from the START of 3a, not from 0: `#### 4.` also appears before it in a brief
    # where the steps were reordered, and slicing backwards yields the empty string — which
    # fails every assertion below with "step 3a never asks for the premise", about a step that
    # asks for it perfectly well.
    end = _located(brief, "#### 4.", f"the step after {ESCALATION_STEP}", start)
    return brief[start:end]


@pytest.fixture(scope="module")
def summary(brief: str) -> str:
    return brief[_located(brief, "#### 6.", "step 6, the summary"):]


@pytest.fixture(scope="module")
def commit_step(brief: str) -> str:
    start = _located(brief, "#### 5.", "step 5, commit and push")
    return brief[start:_located(brief, "#### 6.", "step 6, the summary", start)]


def normalised(text: str) -> str:
    """Prose with its line breaks flattened, for assertions about wording rather than wrapping."""
    return " ".join(text.split())


# --------------------------------------------------------- 1. permission and report together


def test_the_brief_permits_the_escalation_in_a_step_of_its_own(brief: str):
    """A heading, not a sentence in passing. It is the one exception to "fix everything", and an
    exception a fixer has to infer from prose is one that gets inferred in both directions."""
    assert re.search(rf"^#### {ESCALATION_STEP}\. ", brief, re.MULTILINE), (
        f"the brief has no `#### {ESCALATION_STEP}.` heading — if the escalation moved, every "
        f"reference in {sorted(FIX_LOOPS)} now points at nothing")


def test_the_brief_still_forbids_note_and_move_on(brief: str):
    """The escalation is a carve-out from this rule and is worthless without it. A brief that
    permits escalating and no longer insists on fixing everything else has not gained an
    outcome, it has lost a standard."""
    flat = normalised(brief)
    for standard in ("fix everything you find", "never note a problem and move on"):
        assert standard in flat, (
            f"the brief no longer says {standard!r}; step {ESCALATION_STEP} is a carve-out from "
            "that standard, and without it the carve-out is the whole rule")


def test_the_summary_has_somewhere_to_report_an_escalation(summary: str):
    """The other half. Step 6's table is the only thing the orchestrator reads, so a permission
    whose report has no row is a finding that disappears — which is the note-and-move-on this
    brief opens by forbidding, arriving through the exception to it."""
    assert "Escalated" in summary, (
        "step 6 has no place to report an escalation, so step 3a's permission is unreported")


def test_the_commit_body_records_the_escalation_too(commit_step: str):
    """The summary is relay text and the commit is the artifact: step 5 calls it "the only record
    that reaches a reader who never saw this run", which makes it the more durable half of claim
    1. Nothing else asserts step 5 mentions the escalation at all."""
    flat = normalised(commit_step)
    assert "escalat" in flat.lower(), (
        "step 5 never mentions an escalation, so the commit body — the only record that outlives "
        "the relay — can omit the one finding nobody fixed")
    assert "premise" in flat.lower(), (
        "step 5 asks for the escalated finding without its premise; a commit body saying only "
        "'escalated' is the bare `refuted` problem in another artifact")


def test_the_escalation_write_up_asks_for_the_premise_and_its_cost(escalation: str, summary: str):
    """"Escalated" on its own is the bare `refuted` problem in another column: a confident
    assertion with nothing behind it. What makes it actionable is the premise in one sentence and
    what removing it would cost — the two things only the fixer, holding the patch it declined
    to write, is in a position to say."""
    for asked in ("premise", "one sentence"):
        assert asked in escalation, f"step {ESCALATION_STEP} never asks for the {asked}"
    for field in ("Removing it costs", "Patch not written"):
        assert field in summary, f"the reported write-up has no {field!r} line"
    assert "Write no patch for it" in escalation


def test_the_escalation_does_not_authorise_a_redesign(escalation: str):
    """#67's own honest limit, from n = 2: a heuristic that triggers a redesign cheaply is worse
    than the round cap it improves on. The output is a question for a human, and the moment this
    sentence goes the permission becomes a licence to rewrite the change under review."""
    assert "Never redesign on your own authority" in escalation
    assert "stop and ask" in escalation


def test_the_summary_counts_use_the_recorded_vocabulary_and_state_their_invariant(summary: str):
    """The counts replaced a boolean that could only say yes, and they are what `epic.md`'s relay
    scan and a human both read. Two ways for them to lie quietly: a third name for `refuted` (so
    the summary and the board disagree about what happened to a finding), and no stated sum (so
    a dropped finding leaves the numbers merely unremarkable)."""
    flat = normalised(summary)
    # Scoped to the counts line, and case-insensitively. The retired phrase is what the counts
    # line must not say; three lines above it the table legitimately reads "the evidence it was
    # not a defect", so a case-sensitive check over the whole section was passing on the capital
    # N alone — and would have failed on a rewording that merely started a sentence with it.
    counts = next((line for line in summary.splitlines()
                   if "Findings:" in line and "Fixed:" in line), None)
    assert counts, "step 6's summary has no counts line naming both Findings and Fixed"
    assert "Refuted: N" in counts, (
        f"the counts line ({counts.strip()!r}) no longer says `Refuted: N` — the summary's label "
        "for 'not a defect' is deliberately the same token the board records")
    assert not re.search(r"\bnot a defect\b", counts, re.IGNORECASE), (
        f"the counts line ({counts.strip()!r}) says 'not a defect', a third name for what this "
        "file already calls a false positive and records as `refuted`; one outcome, one word")
    assert "Deferred: N" in counts, (
        f"the counts line ({counts.strip()!r}) no longer says `Deferred: N` — `deferred` is a "
        "permitted fixer outcome under `review_panel.fixer_may_defer` (#165), and a permitted "
        "outcome missing from the counts is a finding that can leave the table without the "
        "arithmetic below noticing")
    assert "Fixed + Deferred + Escalated + Refuted = Findings" in flat, (
        "the counts state no invariant, so nothing tells a reader that the four must sum to "
        "Findings — the one cheap check that catches a finding that fell off the table. Every "
        "permitted outcome has to be a term in it: one left out is exactly the note-and-move-on "
        "the permission was granted to replace")
    for empty in ("Escalated: none", "Deferred: none"):
        assert empty in flat, (
            f"step 6 no longer spells the empty case {empty!r}; a missing block reads as "
            "forgotten, and the run where that matters is the run where a reader has to be sure")


# ------------------------------------------------------------- 2. the cross-file anchor


@pytest.mark.parametrize("name", sorted(FIX_LOOPS))
def test_every_fix_loop_says_what_it_does_with_an_escalation(name: str):
    """The #169 failure — a mechanism that ships unwired — in its cheapest form. Each of these
    either defines the escalation or decides what happens to a finding carrying one, and what is
    asserted is the deciding: naming `step 3a` proves nothing, since a file can name it in one
    paragraph and still say "repeat until the To fix list is empty, then merge" in the next."""
    loop = FIX_LOOPS[name]
    text = normalised(doc(name))
    assert f"step {ESCALATION_STEP}" in text, (
        f"{name} ({loop.why}) no longer mentions step {ESCALATION_STEP} — "
        + (f"the definition moved or was renumbered without {ESCALATION_STEP!r} following it"
           if loop.defines else
           "so nothing tells it what to do with a finding the fixer escalated instead of "
           "patching"))
    for token in loop.behaviour:
        assert token in text, (
            f"{name} ({loop.why}) no longer says {token!r}. What it has to spell out is "
            f"{loop.behaviour_is} — and mentioning step {ESCALATION_STEP} while dropping that "
            "is the unwired form of the same failure")


def test_nothing_points_at_a_step_the_brief_does_not_have(brief: str):
    """The anchor resolves in the direction that actually breaks. `panel-review-pr.md` pastes
    the brief into a sub-agent that cannot open this file, so a reference to a step that no
    longer exists is not a broken link a reader notices — it is an instruction that reads as
    complete and names nothing.

    Headings come from the brief, not from the whole file: every reference checked here is
    worded "the brief's step N", and a step number that exists only in the orchestrator's own
    sections satisfies a whole-file check while the sub-agent's copy contains no such step —
    a false pass on precisely the scenario this test is named for."""
    headings = set(re.findall(r"^#### (\d+[a-z]?)\.", brief, re.MULTILINE))
    assert ESCALATION_STEP in headings, (
        f"the brief has no step {ESCALATION_STEP} heading; the references below cannot resolve")
    sources = {name: doc(name) for name in (*FIX_LOOPS, *ANCHOR_DOCS)}
    found: dict[str, set[str]] = {}
    for name, text in sources.items():
        found[name] = set(_STEP_REFERENCE.findall(normalised(text)))
        for ref in sorted(found[name]):
            assert ref in headings, (
                f"{name} points at the brief's step {ref}, which review-pr.md's brief does not "
                f"have — it has {sorted(headings)}")
    # Non-vacuous, in both senses: a regex that matches nothing passes every assertion above
    # over an empty set, and that is exactly how a guard like this dies. Every guarded file has
    # at least one owner-qualified reference today, and a rewording that hides one from the
    # pattern must fail here rather than silently stop being checked.
    silent = sorted(name for name, refs in found.items() if not refs)
    assert not silent, (
        f"{silent} contain no reference this test can read, so the anchor is no longer checked "
        f"for them. Accepted phrasings are \"the brief's step {ESCALATION_STEP}\", "
        f"\"(its step {ESCALATION_STEP})\", \"`/review-pr`'s step {ESCALATION_STEP}\" and "
        f"\"`review-pr.md`'s brief (step {ESCALATION_STEP})\"")
    assert any(ESCALATION_STEP in refs for refs in found.values()), (
        f"no guarded file references step {ESCALATION_STEP} in a form this test reads")


def test_the_brief_cites_no_section_of_the_file_it_was_lifted_out_of(brief: str):
    """The same anchor, inward. `panel-review-pr.md` pastes this slice into a sub-agent that
    cannot open `review-pr.md`, so a `§2b` inside it resolves to nothing for the one reader it
    was written for — an instruction that reads as complete and names nothing, which is what the
    test above guards in the other direction. Step numbers are fine: the brief carries its own
    headings. Section marks are not, because the sections they name are the orchestrator's, and
    the fixture's own boundary at `## 2b.` is what proves the reader cannot see them."""
    cited = sorted(set(re.findall(r"§\s*\S+", brief)))
    assert not cited, (
        f"the brief cites {cited} — sections of {REVIEW_PR} that the sub-agent it is handed to "
        "cannot see. Say what the orchestrator does with the finding, not where that is written")


def test_the_premise_check_invocation_resolves(escalation: str):
    """The anchor pointing outwards. Step 3a tells the fixer to put the premise to the seats with
    `panel.py --ask`, and adds "skip silently if the script isn't there" — which turns a wrong
    flag name into a silent no-op, so the one signal step 3a says a fixer cannot self-report
    would simply never be collected. Neither `panel.py` nor the loops README is in the change
    that added this paragraph, so nothing else here says the invocation is real."""
    fence = re.search(r"```bash\n(.*?)```", escalation, re.DOTALL)
    assert fence, f"step {ESCALATION_STEP} no longer shows the `--ask` invocation as a bash block"
    snippet = fence.group(1)
    # The path first, because it is what a fixer pastes and the flag check below cannot see it:
    # a wrong path is the same silent no-op as a wrong flag name, by the paragraph's own "skip
    # silently if the script isn't there". Two assertions, because two things can drift — the
    # snippet away from the installed path, and the installed path away from the repo file this
    # test actually reads.
    assert PANEL_PY_INSTALLED.endswith(PANEL_PY.removeprefix("harness/")), (
        f"{PANEL_PY_INSTALLED} and {PANEL_PY} no longer name the same file under the harness "
        "root, so reading the second proves nothing about the first")
    invoked = re.search(r"python3\s+(\S*panel\.py)", snippet)
    assert invoked, (
        f"step {ESCALATION_STEP}'s bash block no longer runs a `panel.py`, so the flags below are "
        "checked against a script it does not call")
    assert invoked.group(1) == PANEL_PY_INSTALLED, (
        f"step {ESCALATION_STEP} runs {invoked.group(1)!r}, not {PANEL_PY_INSTALLED!r} — the path "
        f"whose repo copy ({PANEL_PY}) is where the flags below are read from")
    panel_py = doc(PANEL_PY)
    flags = sorted(set(re.findall(r"--[a-z][a-z-]+", snippet)))
    assert "--ask" in flags, f"step {ESCALATION_STEP}'s bash block does not run `--ask`"
    for flag in flags:
        assert f'"{flag}"' in panel_py, (
            f"step {ESCALATION_STEP} passes {flag}, which {PANEL_PY} does not "
            "declare — and the paragraph's own 'skip silently' makes that a no-op, not an error")
    loops_readme = doc("harness/loops/README.md")
    assert _PREMISE_CHECK_SECTION in normalised(escalation), (
        f"step {ESCALATION_STEP} no longer cites the README section that carries the `--ask` "
        "contract (exit 0 on every verdict, no judge, no round)")
    assert f"### {_PREMISE_CHECK_SECTION}" in loops_readme, (
        f"harness/loops/README.md has no `### {_PREMISE_CHECK_SECTION}` section, so step "
        f"{ESCALATION_STEP} cites a heading that does not exist")


def test_the_premise_is_not_interpolated_into_a_shell_string(escalation: str):
    """A premise about code carries backticks and `$(…)` — the ones in this very file do — and
    inside a double-quoted argument bash executes them, while a `$VAR` in it expands to empty and
    sends the seats a premise nobody wrote. It happened: a `--body "..."` built this way ran a
    slash command out of a finding's prose and silently emptied a bullet. So the shown invocation
    has to keep the premise out of the command line, and has to bound the call — a hung ask
    inside a fix pass otherwise outlives the foreground Bash cap and takes the pass with it."""
    fence = re.search(r"```bash\n(.*?)```", escalation, re.DOTALL)
    assert fence, f"step {ESCALATION_STEP} no longer shows the `--ask` invocation as a bash block"
    snippet = fence.group(1)
    assert re.search(r"<<\s*'[A-Z]+'", snippet), (
        "the invocation no longer builds the premise in a QUOTED heredoc, so whatever it "
        "substitutes is expanded by the shell first")
    # Asserted positively. Excluding one leading character (`--ask "[^$"]`) rejected the least
    # of the bad forms and passed the worst: `--ask $premise`, unquoted, word-splits the premise
    # into a dozen arguments, and `--ask "$(cat premise.txt)"` runs a command substitution. The
    # property is "the premise is not on the command line", and only one shape has it.
    ask = re.search(r"--ask\s+(\S+)", snippet)
    assert ask, f"step {ESCALATION_STEP}'s bash block passes `--ask` no value at all"
    assert re.fullmatch(r'"\$[A-Za-z_]\w*"', ask.group(1)), (
        f"`--ask` is given {ask.group(1)!r} rather than a quoted variable holding the heredoc's "
        "value. Anything else puts the premise on the command line: backticks and `$(…)` in it "
        "execute, an unquoted `$var` word-splits it, and an unset `$VAR` in the prose empties it")
    assert re.search(r'--context\s+"', snippet), (
        "`--context` is unquoted, so a path or line range with a space or a glob breaks argument "
        "splitting")
    assert re.search(r"^timeout \d+ ", snippet, re.MULTILINE), (
        "the ask is unbounded: it spawns the real reviewer CLIs, and a slow seat outliving the "
        "10-minute foreground Bash cap kills the fix pass around it")
    assert "not run" in normalised(escalation), (
        f"step {ESCALATION_STEP} bounds the ask without saying how a killed run is reported; "
        "'not run' is the verdict slot step 6 already has for it")


def test_the_pr_less_premise_check_is_shown_and_is_still_a_valid_invocation():
    """The same anchor outwards, from the one loop that runs before a PR exists.

    `fix-issue.md` is the author's own fix pass: nothing is merged, no PR has been opened, and so
    step 3a's invocation cannot be run as written — its `--pr` has nothing to name. That file used
    to say so in prose and show no command, which asks a reader following it alone to mentally
    edit a snippet they were never shown, and left the claim underneath ("an `--ask` with no
    `--pr` is accepted") resting on nothing: step 3a's own guard reads the flags of the form WITH
    `--pr`, so if `panel.py` ever required a PR number — outright, or whenever `--context` is
    given — the paragraph would keep saying otherwise and the ask would exit non-zero into a
    "skip silently" instruction.

    Two halves, both checkable: the command is in the file and uses only flags `panel.py`
    declares, without `--pr`; and `panel.py` still reaches the ask without one. The second is
    asserted as an ORDERING — the `--ask` early return sits above the `--pr`-required guard — and
    keyed to the source text rather than to a line number, since a line number is exactly the
    fact that rots. `--context` is checked into the same window for the compound form, which is
    the shape this snippet actually uses.
    """
    fences = re.findall(r"```bash\n(.*?)```", doc(FIX_ISSUE), re.DOTALL)
    asks = [f for f in fences if "--ask" in f]
    assert len(asks) == 1, (
        f"{FIX_ISSUE} shows {len(asks)} `--ask` invocations, not 1 — the paragraph on step "
        f"{ESCALATION_STEP}'s fields has to show the PR-less command itself, and exactly one of "
        "them, or this guard is reading something else")
    snippet = asks[0]
    invoked = re.search(r"python3\s+(\S*panel\.py)", snippet)
    assert invoked and invoked.group(1) == PANEL_PY_INSTALLED, (
        f"{FIX_ISSUE}'s block runs {invoked.group(1) if invoked else None!r}, not "
        f"{PANEL_PY_INSTALLED!r} — the path whose repo copy ({PANEL_PY}) the flags are read from")
    panel_py = doc(PANEL_PY)
    flags = sorted(set(re.findall(r"--[a-z][a-z-]+", snippet)))
    assert "--ask" in flags, f"{FIX_ISSUE}'s block does not run `--ask`"
    assert "--pr" not in flags, (
        f"{FIX_ISSUE} shows `--pr` in the invocation an author runs before any PR exists; the "
        "flag has no number to carry there, which is the whole reason this file shows its own "
        f"copy of step {ESCALATION_STEP}'s command")
    for flag in flags:
        assert f'"{flag}"' in panel_py, (
            f"{FIX_ISSUE} passes {flag}, which {PANEL_PY} does not declare — and step "
            f"{ESCALATION_STEP}'s 'skip silently' makes that a no-op, not an error")
    # The safe-quoting property step 3a established, carried over rather than restated: this is
    # the same premise text, going to the same flag, and a copy that lost the heredoc would run
    # the backticks in it.
    assert re.search(r"<<\s*'[A-Z]+'", snippet), (
        f"{FIX_ISSUE}'s copy no longer builds the premise in a QUOTED heredoc, so the shell "
        "expands whatever it substitutes")
    ask = re.search(r"--ask\s+(\S+)", snippet)
    assert ask and re.fullmatch(r'"\$[A-Za-z_]\w*"', ask.group(1)), (
        f"`--ask` is given {ask.group(1) if ask else None!r} rather than a quoted variable "
        "holding the heredoc's value, which puts the premise on the command line")
    assert re.search(r'--context\s+"', snippet), (
        "`--context` is unquoted, so a path or line range with a space or a glob breaks argument "
        "splitting")
    # Now the claim the prose makes about panel.py. `--pr` is optional at the parser (no
    # `required=True`) and the ask returns before the guard that demands it, so neither `--ask`
    # alone nor `--ask --context` needs a PR number.
    declared = _located(panel_py, 'ap.add_argument("--pr"', "the --pr declaration",
                        where=PANEL_PY)
    decl_end = _located(panel_py, "ap.add_argument(", "the flag declared after --pr",
                        declared + 1, where=PANEL_PY)
    assert "required=True" not in panel_py[declared:decl_end], (
        f"{PANEL_PY} now declares --pr as required=True, so argparse rejects the PR-less ask "
        f"{FIX_ISSUE} tells an author to run before argv is ever inspected")
    returns_the_ask = _located(panel_py, "return ask(", "the --ask early return", where=PANEL_PY)
    for guard, why in (
        ("--pr is required",
         "a bare `--ask` would be rejected for having no PR number"),
        ("belongs to --ask",
         "`--ask --context` — the form this snippet uses — would be rejected as a PR review"),
    ):
        at = _located(panel_py, guard, f"the {guard!r} guard", where=PANEL_PY)
        assert returns_the_ask < at, (
            f"{PANEL_PY} now reaches its {guard!r} guard before returning from `--ask`, so "
            f"{why} — and {FIX_ISSUE} tells an author to run exactly that")


# --------------------------------------------------- 3. an outcome the database will accept


@pytest.fixture(scope="module")
def outcomes() -> set[str]:
    text = doc("app/api/reviews.py")
    match = _OUTCOMES_TUPLE.search(text)
    assert match, "app/api/reviews.py no longer declares OUTCOMES as a tuple — read it another way"
    literal = match.group(1)
    assert "(" not in literal, (
        f"the OUTCOMES literal read as {literal!r}, which contains a nested tuple — the pattern "
        "matched more than the one declaration")
    values = set(re.findall(r'"([^"]+)"', literal))
    assert values, f"no string values in the OUTCOMES literal {literal!r}"
    return values


def test_the_vocabulary_is_the_one_this_suite_thinks_it_is(outcomes: set[str]):
    """A guard on the guard. Every assertion below is "the docs stay inside this set", which
    passes trivially if the set is read wrong — and reading it wrong is silent."""
    assert outcomes == {"fixed", "refuted", "deferred", "superseded"}


def test_the_constraint_the_docs_name_exists(outcomes: set[str]):
    """§2b cites `app/models/review.py` and this suite's own failure message quotes the CHECK by
    name, so both go stale on a rename with nothing saying so — the drift the anchor tests exist
    to catch, one directory over. `app/api/reviews.py` already fails at import if its tuple and
    the CHECK disagree; what is unguarded is the NAME the prose points at."""
    models = doc("app/models/review.py")
    assert _VOCAB_CONSTRAINT in doc("app/api/reviews.py"), (
        f"app/api/reviews.py no longer names {_VOCAB_CONSTRAINT}, so its import-time guard is no "
        "longer comparing OUTCOMES against the CHECK the prose sends a reader to")
    # Scoped to that CHECK's own call rather than searched for over the module. `'fixed'` occurs
    # in this file in prose, in a sibling constraint and in a docstring, so a whole-file substring
    # search is satisfied by a vocabulary CHECK that has lost a value — which is the drift.
    named = _located(models, f'name="{_VOCAB_CONSTRAINT}"', "the vocabulary CHECK",
                     where="app/models/review.py")
    opened = models.rfind("CheckConstraint(", 0, named)
    assert opened != -1, (
        f"{_VOCAB_CONSTRAINT} in app/models/review.py is not inside a `CheckConstraint(` call, so "
        "there is no expression to read the four values out of")
    check = models[opened:named]
    for value in sorted(outcomes):
        assert f"'{value}'" in check, (
            f"the {_VOCAB_CONSTRAINT} CHECK in app/models/review.py does not list {value!r}, "
            "which app/api/reviews.py declares — the two spellings of the vocabulary have "
            f"drifted. It reads: {' '.join(check.split())!r}")
    assert _VOCAB_CONSTRAINT in doc(REVIEW_PR), (
        f"{REVIEW_PR} §2b no longer names the constraint that makes 'there is no fifth outcome' "
        "a fact rather than a convention")


@pytest.mark.parametrize("name", sorted(FIX_LOOPS))
def test_no_command_instructs_an_outcome_the_table_would_reject(name: str, outcomes: set[str]):
    """The vocabulary is a SQL CHECK as well as a Python tuple, and the docs are the third place
    it is written — the one an agent copies the JSON out of. An invented `escalated` fails at
    ingest, which is a long way from the paragraph that suggested it."""
    for value in _DOC_OUTCOME.findall(doc(name)):
        # How these payloads show a field to substitute: `<key of …>`, a `{{template}}`, a
        # `$VAR`, or the vocabulary itself as an alternation. Only bare literals are checked,
        # because only a bare literal is what an agent would send unaltered.
        if value.startswith(("<", "{", "$")) or "|" in value:
            continue
        assert value in outcomes, (
            f"{name} shows `\"outcome\": \"{value}\"`, which is not one of "
            f"{sorted(outcomes)} — the insert would be rejected by "
            f"{_VOCAB_CONSTRAINT}")


@pytest.mark.parametrize("name", RECORDS_OUTCOMES)
def test_an_escalation_is_recorded_as_deferred(name: str, outcomes: set[str]):
    """`refuted` is a claim that the finding was not a defect, and an escalation claims the
    opposite: the defect is real and the *fix* is what is in dispute. Recorded as `refuted` it
    would put the fixer's disagreement about the approach into the number that measures whether
    reviewers are RIGHT — the one figure this whole feature exists to produce. So the paragraph
    that tells an agent what to record must name `deferred`, in both files that tell it to
    record anything.

    Checked by paragraph rather than by sentence: which of `fixed`/`refuted`/`deferred` a
    sentence is *ruling out* is not something a pattern can read, so what is asserted is that
    the escalation's recording paragraph exists and names the right value. The direction of its
    prose is a reviewer's job, and the ruled-out values are legitimately named there too. The
    window is the paragraph OR the bullet: `panel-review-pr.md` writes the vocabulary as one
    unbroken bullet list, so splitting on blank lines alone lets `escalat`, `record` and
    `deferred` come from three different bullets — and the check would then pass on a file whose
    escalation-recording sentence had been deleted."""
    blocks = [
        chunk
        for paragraph in doc(name).split("\n\n")
        for chunk in ([paragraph] if "\n- " not in paragraph else paragraph.split("\n- "))
    ]
    relevant = [
        b for b in blocks
        if "escalat" in b.lower() and ("record" in b.lower() or "outcome" in b.lower())
    ]
    assert relevant, f"{name} never says what an escalated finding is recorded as"
    assert any("`deferred`" in b for b in relevant), (
        f"{name} discusses recording an escalation without naming `deferred` in the same "
        f"paragraph or bullet; the four values it could be naming instead are {sorted(outcomes)}, "
        "and three of them would be wrong")

# ---- the reads this suite declares (#251, #257) ------------------------------
#
# The comparison against flake.nix lives in `_prose_sandbox`, which knows every member of the
# `prose-consistency-tests` check: the converse direction — does the check install anything
# nothing reads — cannot be asked from inside one member, since a check serving two suites
# installs files either one alone does not read.
#
# What stays here is the half that belongs beside the reads: the declaration, and `doc`'s
# refusal of anything absent from it.


def test_the_reads_are_declared_rather_than_summarised():
    """`READS` is only worth comparing if it is what the suite actually reads, and `doc`'s
    refusal is what makes that true. Asserted rather than trusted: it is the mechanism the
    sandbox comparison rests on, and one `assert` away from being decoration."""
    with pytest.raises(AssertionError, match="not in READS"):
        doc("docs/DEPLOY.md")
    # And the refusal names what to do about it, in both places that need changing.
    with pytest.raises(AssertionError, match="install"):
        doc("app/api/nothing_here.py")
