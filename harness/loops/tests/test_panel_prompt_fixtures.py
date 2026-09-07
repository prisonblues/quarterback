"""#781: golden fixtures of the fully rendered seat prompt, and the vocabulary drift guards beside them.

Nothing in this suite pinned what a seat is actually TOLD. The prompt a reviewer
receives is assembled from `panel_core`'s templates, the `reviewer_scope` dial, the
code-access brief, the CI brief, the PR's own claim and the review material — six
inputs, in three modules, none of which had a test that reads the finished text. So a
change to any one of them could alter what every seat is asked while the whole suite
stayed green, and one already had: the CI brief told seats that a missing run was "a
fact about the commit rather than about the repo", which is false where the base branch
is in no workflow's trigger list, and it shipped because nothing read the rendered text.

**The fixture is the rendered prompt, not the source string.** That is the half worth
saying out loud, because the obvious cheaper test — assert some phrase is in the
template — is the one that has never caught anything. A checked-in copy of the finished
text turns a prompt change into a visible diff in review, and it is far easier to read
than the escaped, slotted, `.format`-ed source it comes from. A reviewer of a PR that
edits `REVIEW_PROMPT` can see, in this directory, exactly what the seats will now read.
Prior art: `alexhawat/mergeCraft`, `tests/prompts/fixtures/` +
`tests/test_prompt_artifact_contracts.py`.

**What is a placeholder and what is literal.** Everything the HARNESS says is literal —
that is the text under test. Everything that comes from OUTSIDE (the PR's number, repo,
base, title, body, and the review material itself) is normalised back to a `${NAME}`
placeholder, so the fixture reads as a template and does not churn on the fixture PR.
The CI brief is the one harness-authored block held out as `${CI_BRIEF}`, because it has
nine states and inlining one of them in six fixtures would pin the state nobody gets
wrong; `ci_brief_every_state.txt` pins all nine on its own, which is the shape that
would have caught the #628 wording.

**Rendered through `run()`, never through a copy of it.** `prompt_for` is a closure
inside `panel.run`, and the composition around it — the claim's budget deduction, the
brief selection, the `NEXT_DOOR_SLOT` swap — is what a re-implementation here would
quietly stop testing. `test_panel_next_door` learned this the hard way: deleting the
slot swap from `run()` outright left the whole harness suite green.

**The drift guards are the half that catches a change rather than recording one.**
mergeCraft's prompt test asserts the rendered text still names every value of its
finding taxonomy, so the taxonomy cannot drift away from what the reviewer is told to
emit. Ours does the same for the two vocabularies this panel relays — the severity bands
and the `needs_human` classes — and for the dials, where the failure is #622's: a dial
whose value stops reaching the line the fixer is briefed from is a policy that reads as
configured and binds nothing, and it should be a red test rather than a quiet round.

REGENERATING A FIXTURE. When a prompt change is deliberate:

    QB_REWRITE_PROMPT_FIXTURES=1 uv run --extra dev pytest \\
        harness/loops/tests/test_panel_prompt_fixtures.py

That rewrites the files and then FAILS the run, deliberately: a run that rewrote its own
expectation has verified nothing, and the point of the fixture is the diff. Read
`git diff harness/loops/tests/fixtures/prompts/` — that diff is the change to what every
seat is told — and re-run without the variable to go green.
"""

import difflib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import needs_human  # noqa: E402  — the `needs_human_class` vocabulary
import panel  # noqa: E402
import panel_core  # noqa: E402  — `sh`, the templates, SEVERITIES
import panel_preflight as pf  # noqa: E402  — the manifest verdict and its header
import panel_scope  # noqa: E402  — `ci_brief`, held out of the reviewer fixtures
import panel_seats  # noqa: E402  — `Dials`, whose gist the fixer is briefed from
from conftest import gh_stub, pr_tarball  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "prompts"
REWRITE = os.environ.get("QB_REWRITE_PROMPT_FIXTURES") == "1"

# ----------------------------------------------------------------- the sentinel round
#
# Every value here is normalised back to a placeholder before the fixture is compared,
# so these are chosen to be UNMISTAKEABLE in the rendered text rather than realistic:
# a substitution that silently matched nothing would bake a value into the fixture, and
# `_as_template` refuses rather than allowing it.

PR_NUMBER = 34
REPO = "acme/board"
BASE = "main"
TITLE = "fix: stop re-baking the error cards"
BODY = "This drops the enactment from 117.9 MB to 8.16 MB, verified end to end."
DIFF = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1 +1 @@\n-value = compute(1)\n+value = compute(2)\n")

#: The CI state every reviewer fixture renders under. Held out as `${CI_BRIEF}` there
#: and pinned in full, all nine states, by `ci_brief_every_state.txt`.
CI_STATUS = "PASS"

#: 200 distinct lines split across two files, character for character — a move at ratio
#: 1, which is what buys the manifest verdict. Lifted from `test_panel_preflight`, and
#: 200 rather than a dozen for its reason: a manifest's fixed overhead is ~1.3 KB, so a
#: smaller move produces a manifest no smaller than its diff and is not substituted.
MOVED = [f"    value_{i} = compute({i}, flag=True, retries=3)" for i in range(200)]


def _file(path: str, *, removed=(), added=()) -> str:
    head = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}",
            f"@@ -1,{len(removed)} +1,{len(added)} @@"]
    return "\n".join(head + [f"-{r}" for r in removed] + [f"+{a}" for a in added]) + "\n"


SPLIT = (_file("big.py", removed=MOVED)
         + _file("part_a.py", added=MOVED[:100])
         + _file("part_b.py", added=MOVED[100:]))

def _cfg(**review_panel) -> dict:
    return {"github": REPO, "path": "/nonexistent/acme-board", "name": "board",
            "_rules_baseline": ".harness-rules.sample",
            "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
            "review_panel": review_panel}


#: One hint as #508's board serves them, and one refutation as #773's does. Both blocks
#: are empty on the ordinary round — which is what keeps two rounds of the same PR
#: comparable — so the filled state needs a fixture of its own or it is never read.
HINT = {"pr": 712, "file": "app/cards.py", "line": 88, "severity": "P2",
        "title": "the cache key omits the locale, so two locales share one entry",
        "detail": "confirmed and fixed by adding the locale to the key",
        "age_hours": 6, "outcome": "fixed"}
REFUTATION = {"file": "app/cards.py", "line": 41, "severity": "P2", "round": 1,
              "title": "`render_card` is called before the registry is populated",
              "reason": "the registry is populated at import time by `_bootstrap`, "
                        "which runs before any request handler is bound"}


def _render(monkeypatch, tmp_path, *, review_panel=None, diff=DIFF, title=TITLE,
            body=BODY, reads_code=False, no_pr_claim=False, hints=(),
            refutations=()) -> str:
    """The prompt `run()` hands the claude seat, with every outside edge pinned.

    One seat, because the axes below are properties of the prompt and not of the panel,
    and a second seat would only render one of the same six strings again.

    The board is stubbed rather than reached, in both directions. `next_door` and
    `refutations` default EMPTY because that is the ordinary round — a round with
    neither sends a prompt byte-identical to the pre-#508 one, which is what #508 leans
    on so that comparing two rounds is not also comparing two prompts — and a fixture
    that reached the real board would change when somebody else's PR got reviewed.
    `history_brief` is pinned the same way and for the same reason; it reads the
    operator's own clone and has its own suite.

    The one edge NOT pinned here is `seat_installed`: `conftest`'s autouse
    `every_seat_installed` already answers it True, which is what stops these fixtures
    comparing against the empty-string prompt an absent seat is dispatched with (#222)
    on a CI runner that carries no vendor CLI.
    """
    cfg = _cfg(**(review_panel or {}))
    seen: dict[str, str] = {}
    monkeypatch.setattr(panel, "load_repo_cfg", lambda _n: cfg)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": title, "body": body, "additions": 3, "deletions": 1,
              "headRefName": "h", "headRefOid": "abcdef1234"},
        diff=diff))
    monkeypatch.setattr(panel_core, "sh_bytes",
                        lambda *a, **k: pr_tarball(files={"a.py": "value = compute(2)\n"}))
    monkeypatch.setattr(panel, "board_next_door", lambda *a, **k: (list(hints), ""))
    monkeypatch.setattr(panel, "board_refutations",
                        lambda *a, **k: (list(refutations), ""))
    monkeypatch.setattr(panel, "history_brief", lambda *a, **k: ("", ""))
    monkeypatch.setattr(panel, "review_ci", lambda *a: (CI_STATUS, [], None))
    monkeypatch.setattr(panel, "adjudicate",
                        lambda *a, **k: ([], None, panel.CoverageRuling()))

    def reviewer(name, model, prompt, effort="", code_tree=None, budget_usd=None):
        seen[name] = prompt
        return panel.ReviewerRun([], None, 10, [], code_blind=code_tree is None)

    monkeypatch.setattr(panel, "review_llm", reviewer)
    out = tmp_path / "round.json"
    assert panel.run("board", PR_NUMBER, post=False, json_file=str(out), record=False,
                     no_code_access=not reads_code, no_pr_claim=no_pr_claim) == 0
    assert json.loads(out.read_text())["reviewed"] is True
    assert seen.get("claude"), "the seat was dispatched with no prompt at all"
    return seen["claude"]


def _as_template(prompt: str, *, claim: bool = True, manifest: bool = False) -> str:
    """The rendered prompt with everything that came from OUTSIDE swapped for a
    placeholder — what varies per PR, plus the CI brief, which has nine states and a
    fixture of its own.

    Every substitution must land exactly once. A silent miss is the failure this whole
    module exists to prevent, one level up: a normaliser that stopped matching would
    bake the sentinel PR's values into the golden file and the fixture would go on
    passing while pinning the wrong thing.
    """
    subs = [(f"PR #{PR_NUMBER} ({REPO}), base={BASE}:",
             "PR #${PR_NUMBER} (${REPO}), base=${BASE}:"),
            (panel_scope.ci_brief(CI_STATUS, []), "${CI_BRIEF}")]
    if claim:
        subs += [(f"TITLE: {TITLE}", "TITLE: ${PR_TITLE}"), (BODY, "${PR_BODY}")]
    if not manifest:
        subs.append((DIFF, "${DIFF}"))
    for old, new in subs:
        assert prompt.count(old) == 1, (
            f"the normaliser expected exactly one {new} in the rendered prompt and "
            f"found {prompt.count(old)}. Either the prompt stopped rendering it — which "
            "is the finding — or this module's sentinels have gone stale.")
        prompt = prompt.replace(old, new)
    if manifest:
        # The manifest body is the review MATERIAL under a move verdict: generated by
        # `panel_preflight` from the diff, pinned by its own suite, and hundreds of
        # lines of file table here. What this fixture is for is the BRIEF above it.
        head = prompt.index(pf.MOVE_MANIFEST_HEADER)
        prompt = prompt[:head] + pf.MOVE_MANIFEST_HEADER + "\n${MOVE_MANIFEST}\n"
    return prompt


def _matches_fixture(name: str, rendered: str) -> None:
    """Compare against the checked-in golden, or rewrite it and fail. See the module
    docstring for why a rewrite still fails."""
    path = FIXTURES / name
    want = path.read_text(encoding="utf-8") if path.exists() else ""
    if rendered == want:
        return
    where = path.relative_to(REPO_ROOT)
    if REWRITE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        pytest.fail(
            f"{name} was rewritten from the prompt this checkout renders. Nothing is "
            f"verified by a run that rewrote its own expectation, so this is still a "
            f"failure: read `git diff {where}` — that diff is the change to what every "
            f"seat is told — and re-run without QB_REWRITE_PROMPT_FIXTURES.")
    diff = "".join(difflib.unified_diff(
        want.splitlines(keepends=True), rendered.splitlines(keepends=True),
        fromfile=f"{where} (checked in)", tofile=f"{where} (rendered now)"))
    pytest.fail(
        f"the rendered prompt no longer matches {where}. Every line below is a change "
        f"to what a seat is asked. If it is deliberate, regenerate with "
        f"QB_REWRITE_PROMPT_FIXTURES=1 and read the resulting git diff.\n\n{diff}")


# ------------------------------------------------------- the prompt, in each state
#
# The axes are the ones that change the rendered text, and each is a separate whole-file
# golden rather than a shared body plus a delta: the duplication IS the readability, and
# a reviewer comparing two of these files can see the whole of what differs.


def test_the_default_seat_prompt_is_the_one_checked_in(monkeypatch, tmp_path):
    """`reviewer_scope: diff`, no tree, the PR's claim shown — what a seat on this
    repo's shipped dials is actually handed."""
    got = _render(monkeypatch, tmp_path)
    _matches_fixture("reviewer_diff_scope_code_blind.txt", _as_template(got))


def test_a_seat_holding_the_tree_is_told_so_in_the_prompt_checked_in(monkeypatch,
                                                                    tmp_path):
    """`reviewer_code_access` on. The only difference from the default is the `{code}`
    slot, and it is a difference worth a file: a seat told it has a checkout it does not
    have spends the round reporting that the diff matches nothing (#458)."""
    got = _render(monkeypatch, tmp_path, reads_code=True,
                  review_panel={"reviewer_code_access": True})
    _matches_fixture("reviewer_diff_scope_reads_code.txt", _as_template(got))


def test_repo_scope_widens_the_prompt_to_the_text_checked_in(monkeypatch, tmp_path):
    """`reviewer_scope: repo` — the pre-#165 wording, kept verbatim so that switching
    the dial back really does restore the prompt this panel was measured on. Two slots
    move, not one: the scope paragraph and the Related-code bullet's tail."""
    got = _render(monkeypatch, tmp_path, reads_code=True,
                  review_panel={"reviewer_scope": "repo", "reviewer_code_access": True})
    _matches_fixture("reviewer_repo_scope_reads_code.txt", _as_template(got))


def test_repo_scope_without_tools_drops_the_instruction_it_cannot_follow(monkeypatch,
                                                                        tmp_path):
    """The fourth state, and the one that exists because the other three made it
    necessary: `repo` scope says "search the codebase, don't just review the diff",
    which a seat with no tools cannot do and — since #458 put NO_TOOLS_BRIEF in the same
    prompt — was being told twice, in opposite directions. On antigravity a model
    resolving that contradiction the wrong way ends its own session."""
    got = _render(monkeypatch, tmp_path, review_panel={"reviewer_scope": "repo"})
    _matches_fixture("reviewer_repo_scope_code_blind.txt", _as_template(got))


def test_a_round_without_the_pr_claim_sends_the_prompt_checked_in(monkeypatch, tmp_path):
    """`--no-pr-claim`, which is #550's control arm rather than a convenience: the block
    is a framing that primes, a primed seat reports fewer findings, and fewer findings
    look like a clean PR. This fixture is the pre-#550 posture, and the diff between it
    and the default is exactly the priming being measured."""
    got = _render(monkeypatch, tmp_path, no_pr_claim=True)
    _matches_fixture("reviewer_diff_scope_no_pr_claim.txt",
                     _as_template(got, claim=False))


def test_a_round_carrying_both_memories_sends_the_prompt_checked_in(monkeypatch,
                                                                   tmp_path):
    """The two optional blocks, filled and in one prompt, which is the state no other
    test reads end to end. Their ORDER is the reason this fixture exists rather than two
    smaller ones: a next-door hint is a defect confirmed on ANOTHER pull request and a
    refutation is one disproved on THIS one, they can name the same shape, and
    `panel_core` says in as many words that read in the wrong order the seat resolves
    the contradiction whichever way it likes. Nothing but the rendered prompt shows
    which order it actually got."""
    got = _render(monkeypatch, tmp_path, hints=[HINT], refutations=[REFUTATION])
    _matches_fixture("reviewer_diff_scope_hint_and_refutation.txt", _as_template(got))


def test_a_move_round_sends_the_manifest_prompt_checked_in(monkeypatch, tmp_path):
    """A move over the seats' cap asks a different question — four structural ones —
    and a seat handed the review brief over manifest material reviews file names for
    correctness. The two templates share `_FINDINGS_ENVELOPE`, so the reply contract
    below the brief must stay identical to the others; this file is where that is
    visible."""
    got = _render(monkeypatch, tmp_path, diff=SPLIT,
                  review_panel={"max_diff_chars": len(SPLIT) // 3})
    assert pf.MOVE_MANIFEST_HEADER in got, "the round did not reach a manifest verdict"
    _matches_fixture("move_manifest_code_blind.txt",
                     _as_template(got, manifest=True))


def test_every_ci_state_says_which_one_it_is_in_the_text_checked_in():
    """The nine states, in the one file, because this block is where the class of defect
    #781 is named for actually shipped: `none`'s body asserted the absence was "a fact
    about the commit rather than about the repo", which is precisely false on a PR whose
    base is in no workflow's trigger list. A reviewer told that reasons that a run is
    coming when nothing the author can do will produce one. Nothing read the rendered
    text, so nothing objected."""
    states = [("PASS", [], None, None),
              ("FAIL", ["pytest", "lint"], None, None),
              ("PENDING", [], None, None),
              ("blocked", [], None, None),
              ("none", [], None, None),
              ("none", [], None, {"base": "release/2.1"}),
              (panel_scope.LOCAL_PASS, [], None, None),
              (panel_scope.LOCAL_FAIL, ["make test"], None, None),
              (panel_scope.LOCAL_UNREAD, [], "the suite timed out after 600s", None),
              ("unknown", [], "gh exited 1", None)]
    blocks = []
    for status, failing, skip, unrunnable in states:
        label = status + ("" if unrunnable is None else " (base in no trigger list)")
        blocks.append(f"===== {label} =====\n"
                      + panel_scope.ci_brief(status, failing, skip, unrunnable) + "\n")
    _matches_fixture("ci_brief_every_state.txt", "\n".join(blocks))


# ------------------------------------------------------------- the vocabulary guards
#
# The half that catches drift rather than recording it. A fixture says "the text changed";
# these say "the text no longer names something the panel still scores, judges or
# budgets on", which is a defect however the text was changed.


def test_no_unfilled_slot_token_survives_into_a_rendered_prompt(monkeypatch, tmp_path):
    """A `<<<…>>>` token in the text a seat reads is a feature half-wired: the template
    carries the slot and nothing swaps it, so every reviewer on every round is handed a
    marker for a block that was never rendered — and on the common path, where the block
    is empty, the seat is shown a token instead of nothing at all.

    Enumerated from `panel_core`'s own `*_SLOT` constants rather than from a list here,
    because the failure this catches is a slot ADDED to a template and not to
    `panel.prompt_for`, and a hand-written list is written by the same person who forgot
    the swap. `JUDGE_CODE_SLOT` is excluded: it belongs to the judge's prompt, which
    `panel_rounds` renders, and it is not in either reviewer template."""
    slots = {name: getattr(panel_core, name) for name in dir(panel_core)
             if name.endswith("_SLOT") and name != "JUDGE_CODE_SLOT"}
    assert slots, "no `*_SLOT` constants found — this guard has stopped guarding"
    rendered = {
        "default": _render(monkeypatch, tmp_path),
        "repo scope": _render(monkeypatch, tmp_path,
                              review_panel={"reviewer_scope": "repo"}),
        "manifest": _render(monkeypatch, tmp_path, diff=SPLIT,
                            review_panel={"max_diff_chars": len(SPLIT) // 3}),
    }
    leaked = {f"{where}: {name}" for where, prompt in rendered.items()
              for name, token in slots.items() if token in prompt}
    assert not leaked, (
        f"the rendered prompt still carries {sorted(leaked)}. A slot that reaches a seat "
        "is a block nothing filled — swap it in `panel.prompt_for` (after `.format`, "
        "count 1, the way `NEXT_DOOR_SLOT` is swapped) or take it out of the template.")


@pytest.mark.parametrize("band", panel_core.SEVERITIES)
def test_every_severity_band_the_panel_scores_is_named_in_both_briefs(band):
    """A seat can only emit a band it was told about, and every downstream rung — the
    fix floor, the round trigger, the corroboration thresholds, the low-severity budget
    — is expressed in these four. A band that fell out of the prompt would be a band no
    reviewer ever returns and every dial still names."""
    for label, text in (("reviewer_brief(diff)", panel_core.reviewer_brief("diff")),
                        ("reviewer_brief(repo)", panel_core.reviewer_brief("repo")),
                        ("MOVE_MANIFEST_PROMPT", panel_core.MOVE_MANIFEST_PROMPT)):
        assert band in text, (
            f"{label} never names {band}, but `panel_core.SEVERITIES` still scores it — "
            "a seat cannot report a severity it was not shown")


@pytest.mark.parametrize("cls", needs_human.NEEDS_HUMAN_CLASSES)
def test_every_needs_human_class_is_offered_to_the_seat(cls):
    """The panel's own finding taxonomy, and the direct analogue of mergeCraft's
    taxonomy assertion. `class_or_none` DROPS a class it does not recognise, so a
    vocabulary that grows in `app/needs_human.py` without the envelope growing with it
    gives seats a class they are never told exists — and `announce` then files every
    real instance of it under `other`. The prompt is where the vocabulary is published;
    if it is not there, it is not offered.

    Read off the rendered briefs rather than off `_FINDINGS_ENVELOPE`, on this module's
    own argument: the envelope is a template and the seat reads a prompt."""
    for label, text in (("reviewer_brief(diff)", panel_core.reviewer_brief("diff")),
                        ("MOVE_MANIFEST_PROMPT", panel_core.MOVE_MANIFEST_PROMPT)):
        assert cls in text, (
            f"`needs_human_class` accepts {cls!r} and {label} never names it — the seats "
            "are never told they may return it. Add it to `panel_core."
            "_FINDINGS_ENVELOPE`'s `needs_human` bullet beside the others.")


def test_the_briefs_offer_no_class_the_panel_would_discard():
    """The other direction, and the one that costs a round rather than a class: a
    spelling offered in the prompt and refused by `class_or_none` is a seat doing what
    it was asked and having the answer thrown away."""
    brief = panel_core.reviewer_brief("diff")
    marker, end = "`needs_human_class`, one of ", "and `needs_human_reason`"
    assert marker in brief and end in brief, (
        "the `needs_human` bullet no longer reads "
        f"{marker!r} … {end!r}, so this guard can no longer find the class list")
    listed = brief.split(marker, 1)[1].split(end, 1)[0]
    # Each entry is `name (gloss)`, separated by `·`. The name is the first word.
    offered = {entry.split()[0] for entry in listed.split("·") if entry.split()}
    unknown = {c for c in offered if needs_human.class_or_none(c) is None}
    assert not unknown, (
        f"the prompt offers {sorted(unknown)} and `needs_human.class_or_none` refuses "
        "them, so a seat that returns one has its escalation discarded")
    assert offered == set(needs_human.NEEDS_HUMAN_CLASSES), (
        f"the prompt lists {sorted(offered)} and the vocabulary is "
        f"{sorted(needs_human.NEEDS_HUMAN_CLASSES)}")


#: What `Dials.gist()` — the `**Panel dials**` line the orchestrator builds the fixer's
#: brief out of — must still say when the dial is moved off its default, and the value
#: that moves it. #622 is the shape: every brake on the fix pass is a number relayed
#: through a brief, so a number that stops arriving is a policy that reads as configured
#: and bounds nothing.
RELAYED = {
    "fix_severity_floor": ("P1", "P1"),
    "round_trigger_floor": ("P3", "P3"),
    "low_severity_fix_lines": (37, "37 lines"),
    "low_severity_fix_full_chars": (9_100, "9,100+ chars"),
    "unrefereed_line_weight": (5, "x5"),
    "max_fix_growth": (7.0, "7x"),
    "max_fix_growth_chars": (8_800, "+8,800 chars"),
    "min_fix_growth_chars": (640, "over +640"),
    "max_fix_guard_lines": (13, "guard 13 lines/pass"),
    "reviewer_scope": ("repo", "reviewer scope repo"),
    "fixer_may_defer": (False, "fixer may defer no"),
    "require_failing_test": (True, "failing test required yes"),
    "file_deferral_issues": ("never", "no deferral gets a GitHub issue"),
    "threshold_by_severity": ({"P3": 4}, "P3 4 seats"),
}

#: The dials the gist deliberately does NOT carry, each with the reason. Listed rather
#: than left out, so that `test_every_dial_is_either_relayed_or_deliberately_not`
#: forces a decision when a seventeenth dial lands instead of letting it go quiet.
NOT_RELAYED = {
    # A ceiling on the CYCLE, not on the pass: the fixer is briefed per round and the
    # round it is on is already in the report's header.
    "max_rounds",
    # #508's lookback bounds what the REVIEWERS were shown, before any fixer exists.
    "next_door_days",
}


@pytest.mark.parametrize("key", sorted(RELAYED))
def test_every_relayed_dial_still_reaches_the_line_the_fixer_is_briefed_from(key):
    """#622's failure, made mechanical. `panel-review-pr.md` tells the orchestrator to
    state the dials in the sub-agent's brief because the sub-agent cannot read
    `.harness-rules` for itself in worktree mode, and the only place it can read them
    off is this line. A dial that stops printing is not a smaller report — it is a fix
    pass briefed under a policy nobody stated, spending against a budget it was never
    told, which is exactly how #1780's four passes came out at 850, 322, 356 and 142
    added lines against a 40-line budget."""
    value, expected = RELAYED[key]
    dials = panel_seats.resolve_dials({key: value}, None, [])
    assert getattr(dials, key) == value, "the resolver refused the test's own value"
    assert expected in dials.gist(), (
        f"`review_panel.{key}` resolved to {value!r} and the **Panel dials** line does "
        f"not say {expected!r}:\n\n  {dials.gist()}\n\nThe fixer is briefed from that "
        "line, so a dial missing from it is a policy that reads as configured and "
        "bounds nothing (#169, #622).")


def test_the_dials_line_still_carries_every_clause_at_the_shipped_defaults():
    """The parametrized test above moves each dial off its default, which is the only way
    to see a VALUE arrive. This is the other half: a dial that vanishes from the line at
    the defaults is one a reader cannot tell from a dial that was never applied — and the
    reader is the agent writing the fixer's brief, on a repo that configured nothing.

    Asserted on the LABELS rather than on the values, because a line that had lost its
    labels would still contain "P2" somewhere and pass. `max_fix_guard_lines` and
    `threshold_by_severity` have no clause here and are the documented exceptions: both
    ship as null, where a clause would report an absence rather than a policy."""
    gist = panel_seats.resolve_dials({}, None, []).gist()
    for clause in ("fix at/above", "fix budget", "another round at/above",
                   "reviewer scope", "fix growth cap", "fixer may defer",
                   "failing test required", "GitHub issue"):
        assert clause in gist, (
            f"the **Panel dials** line no longer carries the {clause!r} clause:\n\n"
            f"  {gist}")


def test_every_dial_is_either_relayed_or_deliberately_not():
    """The guard on the guard. `RELAYED` and `NOT_RELAYED` have to cover every dial
    `Dials` carries, or a new one lands, reaches no brief, and this file says nothing —
    which is the same silence #781 is about, one layer along."""
    known = set(RELAYED) | NOT_RELAYED
    carried = set(panel_seats.resolve_dials({}, None, []).as_dict())
    assert carried == known, (
        f"unaccounted dials: {sorted(carried - known)}; stale entries: "
        f"{sorted(known - carried)}. Every dial is either relayed to the fixer (add it "
        "to RELAYED with the text the gist must print) or deliberately not (add it to "
        "NOT_RELAYED with the reason).")


def test_the_fixtures_on_disk_are_the_ones_this_module_pins():
    """A golden nobody renders is a file that rots. Named here so that deleting a test
    without deleting its fixture — or the reverse — is red rather than invisible."""
    expected = {"reviewer_diff_scope_code_blind.txt",
                "reviewer_diff_scope_reads_code.txt",
                "reviewer_repo_scope_reads_code.txt",
                "reviewer_repo_scope_code_blind.txt",
                "reviewer_diff_scope_no_pr_claim.txt",
                "reviewer_diff_scope_hint_and_refutation.txt",
                "move_manifest_code_blind.txt",
                "ci_brief_every_state.txt"}
    found = {p.name for p in FIXTURES.glob("*.txt")}
    assert found == expected, (
        f"unrendered fixtures: {sorted(found - expected)}; missing: "
        f"{sorted(expected - found)}")
