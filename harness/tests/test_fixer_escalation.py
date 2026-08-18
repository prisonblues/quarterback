"""A fixer may say "the approach is wrong, not the code" — and the halves of that must ship together.

#67's second piece. `review-pr.md`'s brief tells a fixer to fix everything and never
note-and-move-on, which is right and is also why every fixer so far has patched a broken
premise rather than saying so: on PR #61 two rounds and two fixes went on one unexamined
assumption, and the round-2 findings *were* round 1's patch. So the brief now permits one more
outcome — escalate the premise, write no patch — and that permission is only safe while three
things stay true at once. This suite asserts those three, because each of them breaks silently:

1. **The permission and the report ship together.** A fixer allowed to escalate, in a brief
   whose summary has nowhere to say so, has been handed note-and-move-on with a nicer name. The
   permission lives in step 3a and the obligation lives in step 6; nothing but this connects
   them.
2. **The cross-file anchor resolves.** Four commands point at the escalation by the step number
   it has in `review-pr.md`'s brief — `panel-review-pr.md` lifts that brief verbatim into a
   sub-agent that cannot see the file, so a renumbered heading leaves every one of them pointing
   at nothing, and pointing at nothing reads exactly like pointing at something.
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

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMMANDS = REPO_ROOT / "harness" / "commands"

#: The step number the escalation has in `review-pr.md`'s SUB-AGENT BRIEF, and therefore the
#: token every other file refers to it by. It is a string, not an int: `3a` is a step *between*
#: two steps, which is what it is — a way of fixing, sitting under the step that says to fix.
ESCALATION_STEP = "3a"

#: Every command that tells an agent to run a fix pass over review findings, with why it is on
#: the list. A loop that has not heard of the escalation re-briefs an escalated finding to a
#: fresh fixer, who writes the patch the last one declined to write — the round that
#: manufactures the next round's findings, arriving through the rule meant to prevent them.
#: Hardcoded rather than discovered: "which files drive a fix pass" is a judgement, and a
#: pattern loose enough to find them all also finds `/panel` (which reviews and never fixes).
#: A rename fails this suite loudly, which is the intent.
FIX_LOOPS = {
    "review-pr.md": "defines the escalation; the canonical brief every other path lifts",
    "panel-review-pr.md": "cycles panel → fix → panel, and its §4 is what would re-brief it",
    "fix-and-land.md": "repeats review→fix until the To fix list is empty, then MERGES",
    "epic.md": "drives /review-pr headlessly per sub-issue and ff-merges the sub-PRs",
}

#: Read from the source text rather than imported: `app.api.reviews` pulls in FastAPI, the ORM
#: and the app's settings, and this suite's whole reason for living under `harness/` is that it
#: needs none of them. The tuple is a literal one line below its own name, so the read is not a
#: parse of anything that could mean something else.
_OUTCOMES_TUPLE = re.compile(r"^OUTCOMES = \((.*)\)$", re.MULTILINE)

#: `"outcome": "<value>"` as it appears in the `qb record-outcome` payloads the docs show. The
#: docs are where an agent copies the JSON from, so these literals are the vocabulary in
#: practice however carefully the constant is written.
_DOC_OUTCOME = re.compile(r'"outcome":\s*"([^"]*)"')


def command(name: str) -> str:
    # Encoding spelled out: these files are prose full of em dashes, and `read_text()` takes the
    # platform default, which under a C/POSIX locale is ASCII. A nix build and a minimal CI
    # image both commonly run without a UTF-8 locale, and the failure there is a
    # UnicodeDecodeError at collection rather than anything about briefs.
    return (COMMANDS / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def review_pr() -> str:
    return command("review-pr.md")


@pytest.fixture(scope="module")
def brief(review_pr: str) -> str:
    """Just the SUB-AGENT BRIEF: what a sub-agent that cannot see the file is handed.

    The orchestrator's own sections around it are addressed to a different reader, so a
    permission or an obligation that lands outside these markers has landed on the wrong agent.
    """
    start = review_pr.index("### SUB-AGENT BRIEF")
    end = review_pr.index("\n## 2b.", start)
    return review_pr[start:end]


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
    assert "fix everything you find" in brief
    assert "never\nnote a problem and move on" in brief


def test_the_summary_has_somewhere_to_report_an_escalation(brief: str):
    """The other half. Step 6's table is the only thing the orchestrator reads, so a permission
    whose report has no row is a finding that disappears — which is the note-and-move-on this
    brief opens by forbidding, arriving through the exception to it."""
    summary = brief[brief.index("#### 6."):]
    assert "Escalated" in summary, (
        "step 6 has no place to report an escalation, so step 3a's permission is unreported")


def test_the_escalation_write_up_asks_for_the_premise_and_its_cost(brief: str):
    """"Escalated" on its own is the bare `refuted` problem in another column: a confident
    assertion with nothing behind it. What makes it actionable is the premise in one sentence and
    what removing it would cost — the two things only the fixer, holding the patch it declined
    to write, is in a position to say."""
    step = brief[brief.index(f"#### {ESCALATION_STEP}."):brief.index("#### 4.")]
    summary = brief[brief.index("#### 6."):]
    for asked in ("premise", "one sentence"):
        assert asked in step, f"step {ESCALATION_STEP} never asks for the {asked}"
    for field in ("Removing it costs", "Patch not written"):
        assert field in summary, f"the reported write-up has no {field!r} line"
    assert "Write no patch for it" in step


def test_the_escalation_does_not_authorise_a_redesign(brief: str):
    """#67's own honest limit, from n = 2: a heuristic that triggers a redesign cheaply is worse
    than the round cap it improves on. The output is a question for a human, and the moment this
    sentence goes the permission becomes a licence to rewrite the change under review."""
    step = brief[brief.index(f"#### {ESCALATION_STEP}."):brief.index("#### 4.")]
    assert "Never redesign on your own authority" in step
    assert "stop and ask" in step


# ------------------------------------------------------------- 2. the cross-file anchor


@pytest.mark.parametrize("name", sorted(FIX_LOOPS))
def test_every_fix_loop_has_heard_of_the_escalation(name: str):
    """The #169 failure — a mechanism that ships unwired — in its cheapest form. Each of these
    either defines the escalation or decides what happens to a finding carrying one; `epic.md`
    is on the list because it names the gap (nothing there parses one) rather than because it
    acts on it, and naming the gap is the part that must not be dropped."""
    assert f"step {ESCALATION_STEP}" in command(name), (
        f"{name} ({FIX_LOOPS[name]}) never mentions step {ESCALATION_STEP}, so nothing tells it "
        "what to do with a finding the fixer escalated instead of patching")


def test_nothing_points_at_a_step_the_brief_does_not_have(review_pr: str):
    """The anchor resolves in the direction that actually breaks. `panel-review-pr.md` pastes
    the brief into a sub-agent that cannot open this file, so a reference to a step that no
    longer exists is not a broken link a reader notices — it is an instruction that reads as
    complete and names nothing."""
    headings = set(re.findall(r"^#### (\d+[a-z]?)\.", review_pr, re.MULTILINE))
    for name in sorted(FIX_LOOPS):
        for ref in set(re.findall(r"the brief's step (\d+[a-z]?)", command(name))):
            assert ref in headings, (
                f"{name} points at the brief's step {ref}, which review-pr.md does not have")


# --------------------------------------------------- 3. an outcome the database will accept


@pytest.fixture(scope="module")
def outcomes() -> set[str]:
    text = (REPO_ROOT / "app" / "api" / "reviews.py").read_text(encoding="utf-8")
    match = _OUTCOMES_TUPLE.search(text)
    assert match, "app/api/reviews.py no longer declares OUTCOMES on one line — read it another way"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_the_vocabulary_is_the_one_this_suite_thinks_it_is(outcomes: set[str]):
    """A guard on the guard. Every assertion below is "the docs stay inside this set", which
    passes trivially if the set is read wrong — and reading it wrong is silent."""
    assert outcomes == {"fixed", "refuted", "deferred", "superseded"}


@pytest.mark.parametrize("name", sorted(FIX_LOOPS))
def test_no_command_instructs_an_outcome_the_table_would_reject(name: str, outcomes: set[str]):
    """The vocabulary is a SQL CHECK as well as a Python tuple, and the docs are the third place
    it is written — the one an agent copies the JSON out of. An invented `escalated` fails at
    ingest, which is a long way from the paragraph that suggested it."""
    for value in _DOC_OUTCOME.findall(command(name)):
        # `<key of …>`-style placeholders are how these payloads show a field to substitute;
        # only real values are being checked.
        if value.startswith("<"):
            continue
        assert value in outcomes, (
            f"{name} shows `\"outcome\": \"{value}\"`, which is not one of "
            f"{sorted(outcomes)} — the insert would be rejected by "
            "ck_review_finding_outcomes_vocabulary")


@pytest.mark.parametrize("name", ["review-pr.md", "panel-review-pr.md"])
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
    prose is a reviewer's job, and the ruled-out values are legitimately named there too."""
    paragraphs = [
        p for p in command(name).split("\n\n")
        if "escalat" in p.lower() and ("record" in p.lower() or "outcome" in p.lower())
    ]
    assert paragraphs, f"{name} never says what an escalated finding is recorded as"
    assert any("`deferred`" in p for p in paragraphs), (
        f"{name} discusses recording an escalation without naming `deferred`; the four values "
        f"it could be naming instead are {sorted(outcomes)}, and three of them would be wrong")
