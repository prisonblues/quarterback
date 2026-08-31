"""Refuting a finding has to cost what complying with it costs, and one line is the price.

#616. The fix brief permits a false positive — "a genuine false positive you re-examined and
confirmed correct" — and asks nothing whatever in support of a fix. The burden of proof therefore
sits entirely on refusal, and that asymmetry has a direction: when a finding is wrong, complying
is cheaper than disproving it, so the pass complies. The resulting change is unnecessary churn
that reads as diligence, and no guard on the page can see it, because a fix for a non-defect
looks exactly like a fix for a defect.

The measured instance is `prisonblues/lexray#1780`, round 3. A P2 said an ingest-time preview had
permanently lost its Definitions section. A seat verified it and the master judge confirmed it,
and it was wrong: `html_preview` is the free teaser, built inside
`if not viewer_has_full_content_access():`, and an empty definitions panel is what it is for. The
fix merged the paid glossary into it, and round 4 caught the result as a P2 — paid Handbook
meanings rendered to anonymous viewers. Disproving the original took one `grep` for the callers
of `generate_preview_html` and one docstring. Nothing asked for it.

So the brief now owes one line per finding, before that finding's patch, naming who consumes the
code the fix would change. This suite guards the wiring, in the three places it breaks silently:

1. **The requirement and the place to write it ship together.** A fixer told to establish the
   consumers, in a summary with no column for them, has been given homework nobody collects. The
   requirement lives in `review-pr.md`'s step 3 and the record is the **Consumers** column of the
   step-6 table; nothing but this holds the two ends against each other, and a column added to the
   header of that table and not to its example rows is a template an agent copies wrongly.

2. **It survives being lifted.** `panel-review-pr.md` replaces the brief's step 2 with a panel's
   findings and pastes the rest into a sub-agent that cannot open `review-pr.md`. A pre-verified
   finding is precisely where the line earns its keep — the round-3 P2 arrived carrying a seat's
   verification and a judge's confirmation — so the one path most likely to read "replace step 2"
   as "the pre-fix work is done" is the one that must say out loud that it is not.

3. **It is owed on every finding, and the brief says which alternative it rejected.** The narrow
   rule — only findings whose fix touches a response path or a stored artefact — asks the fixer to
   classify its own work before doing the work that would tell it. Round 3's fixer would have
   answered "no response path here", correctly by its own model of the code and wrongly in fact.
   A brief that states the rule without the rejected alternative gets narrowed back by the next
   editor who reads it as verbose, so the rejection is asserted rather than the rule alone.

**What is deliberately NOT asserted: that any consumer line is ever correct, or ever written.**
Whether a fixer really ran the `grep` is not observable from a text file, and a test pretending
to check it would be the loudest possible way of implying something checks it. What is checkable
is that the requirement exists in every brief that runs a fix pass, that the summary can carry
its answer, and that no path drops it on the way to the agent that has to obey it.

**Why it lives under `harness/` rather than in `tests/`.** It reads text files and needs nothing
else, and `tests/conftest.py` resolves `DATABASE_URL` and imports the app at collection. Same
reasoning as `test_fixer_escalation.py`, whose subject is the neighbouring outcome. CI discovers
every `harness/**/tests` directory, so it still runs on every push.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The canonical brief. Every other fix loop either lifts it verbatim or points at it, so this is
#: where the requirement is defined and the only file whose step numbering matters.
REVIEW_PR = "harness/commands/review-pr.md"

#: The loop that replaces the brief's step 2 and pastes the rest into a sub-agent. Its §4 is the
#: one place a reader can conclude the pre-fix work went away with step 2.
PANEL_REVIEW_PR = "harness/commands/panel-review-pr.md"

#: The author's own fix pass over its own findings, carrying the same false-positive permission
#: and therefore the same asymmetry.
FIX_ISSUE = "harness/commands/fix-issue.md"

#: The same pass in the compact in-place variant. A separate file rather than a pointer at the one
#: above, so a requirement added to `/fix-issue` alone reaches none of its users.
FIX_ISSUE_HERE = "harness/commands/fix-issue-here.md"

#: The feature's narrative overview. It briefs no agent, which is why it is not a fix loop, but it
#: is where a reader is told what the column is for and it goes stale by the same edits.
HARNESS_README = "harness/README.md"

#: The column the answer lands in, spelled once. It is the token an agent copies out of the table,
#: so a rename here has to be a rename everywhere.
COLUMN = "Consumers"

#: The step of `review-pr.md`'s SUB-AGENT BRIEF that owes the line. A string, not an int, for the
#: same reason `ESCALATION_STEP` next door is one: it is matched against heading text.
CONSUMER_STEP = "3"

#: The step whose table carries the answer.
SUMMARY_STEP = "6"

#: Every repo-root path this suite reads, declared for the sandbox that runs it.
#:
#: `_prose_sandbox` compares this set against what
#: `nix build .#checks.<system>.prose-consistency-tests` installs. A read nobody installed does
#: not FAIL there, it ERRORS on a missing file, which is #163's mechanism and how four suites
#: before this one sat red in a check no workflow runs.
READS = frozenset({REVIEW_PR, PANEL_REVIEW_PR, FIX_ISSUE, FIX_ISSUE_HERE,
                   HARNESS_README})


class Loop(NamedTuple):
    """Why a brief is on the guarded list, and the behaviour it must still spell out."""

    #: Why it is on the list at all, for the failure message.
    why: str
    #: Substrings that have to survive in it. Naming the issue number is not the property worth
    #: guarding — a file can cite #616 and still say "establish the consumers where it seems
    #: worthwhile". These are what the file DOES.
    behaviour: tuple[str, ...]
    #: What that behaviour is, in a reader's words, for the failure message.
    behaviour_is: str


#: Every brief that tells an agent to run a fix pass over a list of findings, with why it is on
#: the list. A loop that has not heard of the requirement runs a pass in which refuting a finding
#: still costs more than complying with it — which is the whole defect, arriving through the one
#: file that was not updated. Hardcoded rather than discovered, for the reason
#: `test_fixer_escalation.FIX_LOOPS` gives: "which files drive a fix pass" is a judgement, and a
#: pattern loose enough to find them all also finds `/panel`, which reviews and never fixes.
#:
#: `fix-and-land.md` and `epic.md` are deliberately absent, and the split is the same one the
#: escalation suite draws for a different reason. They are on that list because an escalation
#: changes the LOOP — an escalated finding never leaves the To fix list, so "repeat until empty"
#: would spin — and a consumer line changes nothing about control flow. Both drive their fix pass
#: through `/review-pr` or `/panel-review-pr`, so the requirement reaches their fixer through the
#: brief they lift, and restating it in them would be two more copies to keep in step.
FIX_LOOPS = {
    REVIEW_PR: Loop(
        why="defines the requirement; the canonical brief every other path lifts",
        behaviour=("Before you fix a finding, write one line naming who consumes the code",
                   "the entitlement tier it is served to"),
        behaviour_is="the requirement itself — the callers before the patch, and the tier for "
                     "code that reaches a response",
    ),
    PANEL_REVIEW_PR: Loop(
        why="replaces the brief's step 2 and pastes the rest into a sub-agent that cannot read it",
        behaviour=("does NOT remove step 3's consumer line",
                   f"require the **{COLUMN}** column"),
        behaviour_is="that swapping self-discovery for a panel's findings leaves the pre-fix "
                     "line standing, and that the returned table has to carry it",
    ),
    FIX_ISSUE: Loop(
        why="runs its own fix pass over review findings, as the code's AUTHOR",
        behaviour=("Before you fix a finding, write one line naming who consumes the",
                   "it goes in your summary beside the finding"),
        behaviour_is="that the author owes the same line, and writes it where its summary is",
    ),
    FIX_ISSUE_HERE: Loop(
        why="the same author-side fix pass, in place — a separate page, not a pointer at "
            "fix-issue.md, so a requirement added there alone never reaches this one",
        behaviour=("Before you fix a finding, write one line naming who consumes the",
                   "in your summary beside the finding"),
        behaviour_is="that the in-place variant owes the line too. It is the terser page and "
                     "carries no severity ranking and no escalation, but it does say \"fix "
                     "everything you find\" over a list that includes Codex's, which is the "
                     "same asymmetry with fewer words around it",
    ),
}

#: A markdown table row: `| a | b |`. Used to walk the step-6 example table off the brief.
_TABLE_ROW = re.compile(r"^\|.*\|\s*$")

#: A separator row, `|---|---|`. Matched rather than assumed to be second, because a table with
#: no separator is not a table and would otherwise be walked as if it were one.
_SEPARATOR_CELL = re.compile(r"^:?-{2,}:?$")


def doc(rel: str) -> str:
    """The text of a repo-root path this suite has declared, or an error naming what to do.

    Every filesystem access here goes through this. That is what makes `READS` a declaration
    rather than a summary — without it the set is a comment, and `_prose_sandbox`'s stated
    invariant ("each member routes every read through an accessor that refuses anything absent
    from that declaration") would be false for this member while its guards reported it covered.
    """
    assert rel in READS, (
        f"{rel!r} is read here but is not in READS, so flake.nix's prose-consistency-tests check "
        f"does not know to install it — where this read would error as a FileNotFoundError "
        f"rather than be asserted. Add it to READS and install it in that check.")
    return (REPO_ROOT / rel).read_text()


def normalised(text: str) -> str:
    """Whitespace-collapsed, so an assertion survives the line the prose happens to wrap on."""
    return re.sub(r"\s+", " ", text)


def _slice(text: str, start: str, end: str | None) -> str:
    """The span of `text` from `start` up to `end`, both matched as literal substrings.

    Assertions about a brief are almost always about a SECTION of it — "step 3 requires this",
    "the step-6 table has that column" — and a whole-file `in` check passes on a page that says
    the right thing in the wrong place, which for a file pasted into a sub-agent in slices is
    not a nit. Both anchors are asserted rather than defaulted, so a renamed heading fails here
    with the heading in the message instead of silently widening the span to the whole file.
    """
    assert start in text, f"anchor {start!r} is not in this file; the slice cannot be taken"
    body = text.split(start, 1)[1]
    if end is None:
        return body
    assert end in body, f"anchor {end!r} does not follow {start!r}; the slice cannot be closed"
    return body.split(end, 1)[0]


@pytest.fixture(scope="module")
def brief() -> str:
    """`review-pr.md`'s SUB-AGENT BRIEF — the slice `panel-review-pr.md` lifts, and nothing else.

    Bounded on the right by §2b, the first of the orchestrator's own sections: a requirement that
    lives outside these bounds is one the pasted-into sub-agent never receives, and asserting it
    against the whole file is a false pass on exactly that reader.
    """
    return _slice(doc(REVIEW_PR), "### SUB-AGENT BRIEF", "## 2b. Record what happened")


@pytest.fixture(scope="module")
def consumer_block(brief: str) -> str:
    """Step 3 of the brief, up to the escalation that follows it."""
    return _slice(brief, f"#### {CONSUMER_STEP}. Fix everything", "#### 3a.")


@pytest.fixture(scope="module")
def summary(brief: str) -> str:
    """Step 6 of the brief — the table a fixer copies, and the notes under it."""
    return _slice(brief, f"#### {SUMMARY_STEP}. Return a summary", None)


def _table(summary_text: str) -> list[list[str]]:
    """The step-6 example table, as rows of stripped cells."""
    lines = summary_text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("| # | Severity")]
    assert starts, (
        "step 6 has no summary table starting `| # | Severity` — the fixer copies this table "
        "verbatim, so a reworded header is a template nobody can follow")
    rows = []
    for line in lines[starts[0]:]:
        if not _TABLE_ROW.match(line):
            break
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def test_the_brief_requires_the_consumer_line_before_the_fix(consumer_block: str):
    """The requirement is in the step that fixes, and it is owed BEFORE the patch.

    Position is the property, not presence. A line established after the fix is a description of
    what was changed; the point of #616 is that establishing it is what tells you whether to
    change anything at all, which only works while it is owed first."""
    text = normalised(consumer_block)
    assert "Before you fix a finding, write one line naming who consumes the code" in text, (
        f"step {CONSUMER_STEP} of the brief no longer requires the consumer line before the "
        f"patch. Without it the brief permits a false positive and asks nothing in support of a "
        f"fix, so refuting a finding costs more than complying with it (#616)")
    assert "before its patch" in text, (
        "the requirement no longer says WHEN the line is owed. A consumer line written after the "
        "fix documents the change instead of deciding it")


def test_the_line_names_both_the_callers_and_the_entitlement_tier(consumer_block: str):
    """Two halves, and the second is the one the measured failure needed.

    "Who calls this" would have been answered correctly on lexray#1780 round 3 and still missed
    the leak: the caller was the teaser branch, and what mattered was who that branch is served
    to. A requirement that asks only for callers is the version that was already implicit."""
    text = normalised(consumer_block)
    assert "The callers, from a search you actually ran rather than from memory" in text, (
        "the requirement no longer asks for callers from a search that was actually run — a line "
        "answered from memory is the assumption the finding already contained")
    tier = "reaches a response or a stored artefact, the entitlement tier it is served to"
    assert tier in text, (
        "the requirement no longer asks for the entitlement tier of code that reaches a "
        "response. That half is what would have caught lexray#1780's leak; callers alone would "
        "not have (#616)")


def test_the_requirement_is_owed_on_every_finding_and_says_which_rule_it_rejected(
    consumer_block: str,
):
    """Every finding, and the narrower alternative named as rejected rather than left unmentioned.

    The alternative — only findings whose fix touches a response path or a stored artefact — is
    cheaper and reads as more targeted, which is why an editor trimming this page will propose it.
    It fails for a reason that is not obvious from the rule itself: it asks the fixer to classify
    its own work before doing the work that would tell it. Stating the rejection is what stops the
    narrowing being reintroduced as a tidy-up."""
    text = normalised(consumer_block)
    assert "Every finding on the list gets one" in text, (
        "the requirement no longer covers every finding. A line owed only on some findings is a "
        "line the fixer decides it does not owe, on the same judgement #616 is about")
    assert "not only the ones whose fix you judge to touch a response path" in text, (
        "the brief no longer names the narrow rule it rejected, so the next editor reads the "
        "universal one as verbosity and narrows it back")
    assert "classify its own work before doing the work that would tell it" in text, (
        "the brief no longer says WHY the narrow rule fails. Without the reason the rejection is "
        "an assertion of taste and will not survive a reviewer who finds the narrow rule cheaper")


def test_the_brief_says_which_asymmetry_the_line_corrects_and_cites_its_instance(
    consumer_block: str,
):
    """The rule without its evidence is a rule agents talk themselves out of.

    Every other requirement on this page carries the measurement that produced it, and this one
    has an unusually strong instance: a P2 verified by a seat, confirmed by the master judge, and
    wrong, whose fix turned it into an entitlement leak one round later. The block has to carry
    both the asymmetry it corrects and the case that shows the cost."""
    text = normalised(consumer_block)
    assert "genuine false positive you re-examined and confirmed correct" in text, (
        "the block no longer quotes the permission it is correcting, so a reader cannot see what "
        "the line is symmetrical WITH")
    assert "complying is cheaper than disproving it" in text, (
        "the block no longer states the asymmetry. That sentence is the argument; without it the "
        "requirement is one more thing to do")
    assert "html_preview" in text and "generate_preview_html" in text, (
        "the block no longer carries the lexray#1780 instance. A brief that asserts the rule with "
        "no case behind it is the first thing a fix pass under time pressure treats as optional")


def test_unknown_is_permitted_and_bounded(consumer_block: str):
    """An escape hatch with no cost attached is the whole requirement, optional.

    `unknown` has to be available — dynamic dispatch and cross-repo callers are real — and it has
    to bind something, or it is the answer every pass gives. What it binds is the fix: a finding
    whose consumers could not be established is fixed where it was raised and no further, which is
    the narrowing that would have contained the measured leak even with the finding believed."""
    text = normalised(consumer_block)
    assert "`unknown` is a permitted answer and it is not a free one" in text, (
        "the brief no longer permits `unknown`, so a fixer facing a genuinely undeterminable "
        "caller set has to either lie or stall")
    assert "fix that finding at the point it was raised and no further" in text, (
        "`unknown` no longer costs anything. An escape hatch that binds nothing is the answer "
        "every pass gives, and the requirement is then optional in practice")


def test_the_line_is_not_charged_to_the_low_severity_budget(consumer_block: str):
    """The one reading that would switch this off entirely on the rounds that need it.

    `low_severity_fix_lines` is a hard cap a fixer is told to count against, and a fixer that
    reads the consumer line as churn will drop it first — on exactly the low-severity findings
    where a wrong finding is most likely and complying is cheapest."""
    text = normalised(consumer_block)
    assert "It is not charged to `low_severity_fix_lines`" in text, (
        "the brief no longer exempts the consumer line from the churn budget. A fixer counting "
        "its spend will read the line as budgeted and drop it on the findings that need it most")


def test_the_summary_table_carries_the_answer(summary: str):
    """The requirement and the place to write it, held against each other.

    A step-3 line with no step-6 column is homework nobody collects; a column the example rows do
    not fill is a template an agent copies wrongly, which for a table pasted verbatim into a PR
    summary is how the column comes back empty on every run."""
    rows = _table(summary)
    header = rows[0]
    assert COLUMN in header, (
        f"step {SUMMARY_STEP}'s table has no {COLUMN!r} column, so step {CONSUMER_STEP}'s line "
        f"has nowhere to land: {header}")
    assert len(rows) >= 3, "the table has a header and a separator but no example rows to copy"
    separator = rows[1]
    assert all(_SEPARATOR_CELL.match(cell) for cell in separator), (
        f"the second row of the table is not a separator ({separator}); markdown will not render "
        f"this as a table at all")
    index = header.index(COLUMN)
    for row in rows:
        assert len(row) == len(header), (
            f"row {row} has {len(row)} cells against the header's {len(header)}. A column added "
            f"to the header and not to the rows renders as a shifted table and is copied as one")
    for row in rows[2:]:
        assert row[index] and row[index] != "...", (
            f"example row {row} leaves {COLUMN!r} empty or elided. Every other column in this "
            f"table shows the shape of its answer; a blank one is copied blank")


def test_the_table_shows_the_line_doing_the_job_it_was_added_for(summary: str):
    """A refuted finding whose consumer line IS the refutation, and an `unknown` that says which.

    Those two rows are the requirement's argument in the form an agent actually reads. Without the
    refuted row the column looks like paperwork attached to fixes; without the `unknown` row the
    permitted answer has no shape and comes back as a blank cell."""
    rows = _table(summary)
    # Asserted rather than left to `list.index`, which raises ValueError — a test that ERRORS
    # has demonstrated nothing, and the red/green step this repo runs on every fix reads the
    # failure type. The sibling test above owns the column's existence; this one needs it.
    assert COLUMN in rows[0], (
        f"step {SUMMARY_STEP}'s table has no {COLUMN!r} column, so it cannot show the line "
        f"doing anything: {rows[0]}")
    index = rows[0].index(COLUMN)
    refuted = [row for row in rows[2:] if row[-1].startswith("Refuted")]
    assert refuted, "the example table no longer shows a refuted finding"
    assert "generate_preview_html" in refuted[0][index], (
        f"the refuted example row's {COLUMN} cell no longer shows the consumer line and the "
        f"refutation as one sentence, which is the case #616 was filed from")
    unknown = [row for row in rows[2:] if row[index].startswith("`unknown`")]
    assert unknown, (
        f"the example table no longer shows an `unknown` {COLUMN} cell, so the permitted answer "
        f"has no shape and a fixer using it writes a blank")
    assert "not determinable" in unknown[0][index], (
        "the `unknown` example no longer says why it is unknown; `unknown` on its own is the "
        "answer every pass gives")
    assert normalised(summary).count(f"(`{COLUMN}` is step {CONSUMER_STEP}'s line") == 1, (
        f"the note explaining what goes in the {COLUMN} column is missing. Every other column in "
        f"this table is explained under it, and an unexplained one is filled by guess")


@pytest.mark.parametrize("name", sorted(FIX_LOOPS))
def test_every_fix_loop_owes_the_consumer_line(name: str):
    """One file left out is a fix pass where refuting still costs more than complying.

    Substrings rather than a mention of #616: a brief can cite the issue and still tell an agent
    to establish the consumers where it seems worthwhile, which is the requirement with the part
    that binds removed."""
    loop = FIX_LOOPS[name]
    text = normalised(doc(name))
    for token in loop.behaviour:
        assert normalised(token) in text, (
            f"{name} ({loop.why}) no longer says {token!r}. What it has to spell out is "
            f"{loop.behaviour_is} — without it, a fix pass driven from this file runs with the "
            f"asymmetry #616 is about")


def test_the_panel_loop_protects_the_line_from_its_own_step_2_replacement():
    """The instruction that would otherwise delete the requirement, answered where it is given.

    `panel-review-pr.md` §4 tells the fixer to replace the brief's step 2 with the panel's
    findings. "The review is already done" is a short step from there to "the pre-fix work is
    already done", and the finding that broke lexray#1780 arrived carrying a seat's verification
    and the judge's confirmation — so this loop is both the likeliest to drop the line and the one
    whose findings most need it. The protection has to sit in §4, next to the replacement, rather
    than anywhere else in a 1,800-line file."""
    section = _slice(doc(PANEL_REVIEW_PR), "## 4. Launch the fixer sub-agent",
                     "## 4b. Record what actually happened")
    text = normalised(section)
    assert "Replacing step 2 does NOT remove step 3's consumer line" in text, (
        "§4 no longer says that the step-2 replacement leaves the consumer line standing. It is "
        "the section that gives the replacement instruction, and it is the only place a reader "
        "of it will look")
    assert "already verified by a seat and confirmed by the judge" in text, (
        "§4 no longer says why a panel-supplied finding needs the line MORE than a self-derived "
        "one. Without it the requirement reads as boilerplate carried over from a different loop")


def test_the_relay_reads_the_column_rather_than_restating_it():
    """A relay that repeats the table teaches a reader to skim it.

    §6 already shows the fixer's summary verbatim, so the column arrives for free. What a human
    cannot get from a verbatim table is the two readings that matter — an `unknown` line, and a
    refutation that IS its consumer line — so those are what §6 asks for, and asking for the
    column again instead would be the third copy of the same text in one relay."""
    section = _slice(doc(PANEL_REVIEW_PR), "## 6. Relay the result", "## 7. The pre-land verdict")
    text = normalised(section)
    assert f"**{COLUMN} (#616):**" in text, (
        f"§6 no longer relays anything about the {COLUMN} column, so the line is written into a "
        f"summary and read by nobody")
    assert "do not restate it" in text, (
        "the relay item no longer says the column arrives with the verbatim table. Without that "
        "the orchestrator copies it out a second time and the relay grows a section a reader skips")
    assert "came back `unknown`" in text, (
        "the relay no longer surfaces an `unknown` consumer line. That is the one answer a human "
        "needs to see, because it is the one that bounds how far the fix should have gone")


def test_the_readme_explains_the_column_and_names_its_guard():
    """The narrative half. It briefs no agent, which is why it is not a fix loop, and it is where
    somebody arriving at the column asks what it is for — including which suite to break when
    they change it."""
    text = normalised(doc(HARNESS_README))
    assert "refusing was the expensive road" in text, (
        "the README no longer explains the asymmetry the column corrects, so the column reads as "
        "a form to fill in")
    assert "harness/tests/test_fixer_consumers.py" in text, (
        "the README no longer names this suite. The escalation feature next door names its guard "
        "for the same reason: an editor changing the prose has to know what will fail and why")


def test_the_reads_are_declared_rather_than_summarised():
    """`READS` is enforced, not descriptive: `doc` refuses a path absent from it.

    The same guard `_prose_sandbox.GATES` applies to every member of this category from the
    outside. It is asserted here too because a member is added to that mapping by hand, and a
    member that gates nothing passes every comparison there while reading whatever it likes."""
    with pytest.raises(AssertionError):
        doc("docs/DEPLOY.md")
    for rel in sorted(READS):
        assert doc(rel), f"{rel} is declared but empty or unreadable"
