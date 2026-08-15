"""What a round past the first actually reviews (#41).

Round 2 exists to read the fix commit — nobody else does (#24) — and until
v2.25 it was handed the whole PR instead: the fix plus everything the earlier
rounds had already read and confirmed, paid for again in budget, wall-clock and
attention. PR #34's four rounds grew 140 KB -> 292 KB *because* it was being
reviewed, until both reviewers declared they could not read ~600 lines of one
test file. A review loop that inflates its own input degrades its own later
rounds.

So a later round reviews the INCREMENT, with the PR as it stood at the anchor
behind it as context. The tests here are about the things that has to get right:

- the target is never the thing that gets cut,
- the context is what an earlier round REALLY saw, so the header saying so is
  true and the fix commit is not handed over twice, and
- every fallback to whole-PR scope is stated rather than silent, because a
  round that claims it reviewed the increment and in fact re-read the PR is
  wrong about the one measurement this feature exists to produce.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402


def chunk(path: str, body: str) -> str:
    """One file's worth of unified diff, in the shape `gh pr diff` emits."""
    return f"diff --git a/{path} b/{path}\n@@ -1,1 +1,2 @@\n{body}\n"


#: doc.md is deliberately the bulk of it, so a budget can run out inside the far
#: tier without having to be tuned to the byte.
DOC = "+prose " * 200
PR = chunk("fix.py", "+fixed\n+again") + chunk("old.py", "+settled") + chunk("doc.md", DOC)
#: The same PR one round earlier — before the fix commit landed in fix.py.
PRIOR = chunk("fix.py", "+fixed") + chunk("old.py", "+settled") + chunk("doc.md", DOC)
INCREMENT = chunk("fix.py", "+the fix commit")


# --------------------------------------------------------------- splitting a diff

def test_a_diff_splits_into_the_files_it_touches():
    got = panel._diff_by_file(PR)
    assert sorted(got) == ["doc.md", "fix.py", "old.py"]
    # Every byte accounted for: the partition is used to build a prompt, so a
    # chunk silently dropped is a file the reviewer is never shown.
    assert "".join(got[f] for f in ["fix.py", "old.py", "doc.md"]) == PR


def test_a_header_that_will_not_split_still_lands_somewhere():
    """The harmless direction here is to keep the chunk, key it by the whole
    header, and let it fall to the outer context tier. Dropping it would delete
    the file from the prompt."""
    weird = "diff --git nonsense\n@@ -1,1 +1,2 @@\n+x\n"
    got = panel._diff_by_file(weird)
    assert list(got) == ["diff --git nonsense"]
    assert "".join(got.values()) == weird


def test_a_preamble_before_the_first_header_is_kept():
    """`cur` started as None and the append was guarded on it, so anything before
    the first `diff --git` — a commit header, a `From …` line, a warning that
    arrived on the same stream — was silently dropped. Under increment scope the
    whole prompt is rebuilt out of this mapping, so the loss would show up as a
    reviewer's copy of the PR differing from what every earlier release sent."""
    got = panel._diff_by_file("warning: on the same stream\n" + PR)
    assert got[panel.DIFF_PREAMBLE] == "warning: on the same stream\n"
    assert "".join(got.values()) == "warning: on the same stream\n" + PR


def test_a_path_containing_the_separator_is_keyed_by_the_whole_path():
    """Splitting on the FIRST `" b/"` matches inside the a-side filename, so this
    file was keyed by a suffix of itself. It then matched no increment file,
    dropped out of the near tier into the far one, and became the first thing a
    tight budget cut — the PR's own changes to a file the fix touched, demoted."""
    got = panel._diff_by_file(chunk("x b/y.txt", "+a"))
    assert list(got) == ["x b/y.txt"]


def test_a_rename_header_still_gives_the_b_side():
    """The symmetry that pins an ambiguous path does not hold for a rename, which
    falls back to the first `" b/"` — what this always did."""
    assert panel._diff_file_path("diff --git a/old.py b/new.py") == "new.py"
    assert panel._diff_file_path("diff --git nonsense") is None


def test_the_two_splitters_agree_on_the_key_for_ordinary_paths():
    """They no longer HAVE to — the near/far tiering matches `_diff_by_file`'s
    keys against its own, and `_diff_added_lines` is not involved in it — but they
    read the same header through the same parser, so they do."""
    assert set(panel._diff_added_lines(PR)) <= set(panel._diff_by_file(PR))
    assert set(panel._diff_added_lines(chunk("x b/y.txt", "+a"))) == {"x b/y.txt"}


# --------------------------------------------------------------- spending a budget

def test_priority_order_spends_on_the_target_first():
    parts = panel._fit_parts(["aaaa", "bbbb", "cccc"], 6)
    assert parts == ["aaaa", "bb", ""]


def test_an_uncapped_budget_keeps_everything():
    assert panel._fit_parts(["aaaa", "bbbb"], None) == ["aaaa", "bbbb"]


def test_a_budget_smaller_than_the_target_still_cuts_the_target():
    """The target is first, not exempt. A reviewer handed a prefix of the thing
    it is reviewing is what `truncated` exists to report — the priority order
    makes that rare, and does not pretend it cannot happen."""
    assert panel._fit_parts(["aaaaaaaa", "bbbb"], 3) == ["aaa", ""]


def test_a_negative_budget_is_no_capacity_rather_than_a_backwards_slice():
    """`part[:left]` with a negative `left` returns everything BUT the last
    `|left|` chars, so a budget of -4 handed a reviewer the target with its tail
    quietly removed — the opposite of "no room", and silent."""
    assert panel._fit_parts(["aaaaaaaa", "bbbb"], -4) == ["", ""]
    assert panel._fit_parts(["aaaaaaaa"], 0) == [""]


def test_fitting_is_monotone_in_the_budget():
    """A non-monotone allocation would let `fit_argv_budget`, which shrinks a
    budget until the rendered prompt fits, settle on a size that does not."""
    parts = ["aaaa", "bbbb", "cccc"]
    sizes = [sum(len(p) for p in panel._fit_parts(parts, b)) for b in range(0, 15)]
    assert sizes == sorted(sizes)


# --------------------------------------------------------------- what gets composed

def scope(**kw) -> panel.ReviewScope:
    got = {"scope": "increment", "diff": PR, "increment": INCREMENT, "prior_diff": PRIOR,
           "since": "abc1234567", "round_no": 2, **kw}
    return panel.ReviewScope(**got)


def test_whole_pr_scope_is_byte_identical_to_what_it_always_was():
    """The comparison between a scoped round and an unscoped one is only worth
    something if the unscoped one did not also change. `--- DIFF ---` moved from
    the prompt template into here; the rendered result must not have moved."""
    text, target, context = panel.ReviewScope(diff=PR).material(None)
    assert text == f"{panel.PR_SCOPE_HEADER}\n{PR}"
    assert (target, context) == (len(PR), 0)


def test_a_budget_under_whole_pr_scope_still_means_chars_of_diff():
    """No frame overhead is taken out of it there: `max_diff_chars` has meant
    "this many chars of diff" since before scope existed, and a "pr" round is
    supposed to be unchanged."""
    text, target, _ = panel.ReviewScope(diff=PR).material(20)
    assert text == f"{panel.PR_SCOPE_HEADER}\n{PR[:20]}" and target == 20


def test_the_increment_is_the_target_and_the_anchors_pr_is_context():
    text, target, context = scope().material(None)
    assert target == len(INCREMENT)
    assert context == len(scope().near) + len(scope().far)
    assert "REVIEW TARGET" in text and "abc12345" in text
    # The target appears before any context, because the order it is read in is
    # the order the budget is spent in and both should say the same thing.
    assert text.index("the fix commit") < text.index("settled")


def test_the_context_does_not_contain_the_target():
    """The defect this design is built around. `near` sliced out of the CURRENT
    PR diff contains the fix commit, because the fix is part of the PR — so the
    reviewer got the target twice, the second time under a header telling it an
    earlier round had already dealt with that code. Both briefs say re-reporting
    such a finding is out of scope, so the duplicate argued the target out of
    scope."""
    got = scope()
    assert "the fix commit" in got.target
    assert "the fix commit" not in got.near and "the fix commit" not in got.far
    assert got.material(None)[0].count("the fix commit") == 1


def test_the_files_the_fix_touches_are_the_first_context_it_gets():
    """The seam between the fix and the code it landed in is where a fix pass
    does its damage — #24's motivating defect was a mirror added in one file
    meeting an early `return` in another. So those files, as they stood before
    the fix, outrank everything else."""
    got = scope()
    assert "fix.py" in got.near and "old.py" not in got.near
    assert "old.py" in got.far and "doc.md" in got.far
    assert got.near == chunk("fix.py", "+fixed")
    text = got.material(None)[0]
    assert text.index("+fixed") < text.index("+settled")


def overhead(got: panel.ReviewScope) -> int:
    """How much of a scoped prompt is not diff text — the brief and the section
    headers. Derived rather than hard-coded, so editing the brief does not silently
    turn these budgets into a different test."""
    whole = got.material(None)[0]
    return len(whole) - len(got.increment) - len(got.near) - len(got.far)


def test_a_tight_budget_drops_context_and_keeps_the_target_whole():
    """The point of the whole exercise. Under the old single-ceiling rule the
    thing lost was whatever sorted last in the diff; here it is always context."""
    got = scope()
    # Room for the frame, the cut markers, the target and the near tier — but not
    # for the far one.
    text, target, context = got.material(
        overhead(got) + 300 + len(INCREMENT) + len(got.near))
    assert target == len(INCREMENT)
    assert "the fix commit" in text
    assert 0 < context < len(got.near) + len(got.far)


def test_the_budget_covers_the_whole_prompt_and_not_just_the_diff_text():
    """The brief and the section headers are over a kilobyte and used to be
    prepended AFTER the budget had been spent, so `max_diff_chars` under-counted
    the prompt in the direction that matters for a model whose context window is
    the reason the budget exists."""
    for budget in (2_000, 3_000, 5_000):
        assert len(scope().material(budget)[0]) <= budget


def test_a_cut_section_says_how_much_is_missing():
    """Truncation of the TARGET is measured and never asked for — a reviewer
    cannot notice its own. Context is the other way round: a reviewer told the
    context is partial can declare it in `could_not_assess`, which turns a silent
    omission into one the judge can rule on."""
    got = scope()
    text = got.material(overhead(got) + 300 + len(INCREMENT))[0]
    assert "[cut:" in text and "not sent]" in text
    assert panel._cut_note("abc", "abc") == ""
    assert "2 not sent" in panel._cut_note("a", "abc")


def test_the_judge_gets_the_same_material_briefed_differently():
    """It must see what the parties saw — an adjudicator ruling "not in the diff"
    while holding a different diff is the one error it cannot recover from, and
    it would carry the authority of the final call. It must not be told to
    review."""
    got = scope()
    reviewer, judge = got.material(None)[0], got.judge_material(None)[0]
    assert "YOUR REVIEW TARGET" in reviewer and "YOUR REVIEW TARGET" not in judge
    for section in ("--- REVIEW TARGET", "the rest of the PR"):
        assert section in reviewer and section in judge
    # Same evidence, whatever the briefing above it says. Per tier rather than as
    # one string: the material is split across three sections, so it is not
    # contiguous in either prompt — which is the point of the tiers.
    for body in (got.increment, got.near, got.far):
        assert body.strip() in reviewer and body.strip() in judge


def test_the_context_header_does_not_claim_the_code_is_already_fixed():
    """It said "already reviewed, already fixed", which is the highest-salience
    line in the prompt and argues against the paragraph under it: a defect nobody
    raised is in scope wherever it sits, and a section header calling it settled
    biases the reviewer and the judge away from exactly the misses this round
    exists to find. What HAS been fixed is in the target."""
    text = scope().material(None)[0]
    assert "already fixed" not in text
    assert "not the target" in text and "an earlier round read" in text


def test_a_defect_nobody_raised_stays_in_scope_wherever_it_is():
    """The correction `zeus/marten-tidal` argued for on the board (post 2459), and
    it is right: if a round can only report on the increment, a pre-existing defect
    becomes structurally unfindable, and #48's `missed` bucket goes to zero by
    construction rather than by measurement. On PR #75's real r1->r2 that bucket
    was 12 of 26 — twelve defects that sat in round 1's diff and round 1 did not
    see. Suppressing those would not re-attribute them, it would make them
    invisible, and the loop would look converged because it stopped looking.

    So what is out of scope is a defect an earlier round ALREADY RAISED — which is
    fixed, and whose fix is in the target. Not "anything outside the target"."""
    for brief in (panel.INCREMENT_BRIEF, panel.JUDGE_INCREMENT_BRIEF):
        assert "already raised" in brief.lower()
    assert "in scope wherever you find it" in panel.INCREMENT_BRIEF
    # The reviewer is told earlier rounds can be WRONG about what they read —
    # otherwise "already reviewed" reads as "already settled". Compared with the
    # line wrapping collapsed: both briefs are prose and wrap where they wrap.
    for brief in (panel.INCREMENT_BRIEF, panel.JUDGE_INCREMENT_BRIEF):
        assert "not the same as being right about it" in " ".join(brief.split())


def test_an_empty_tier_gets_neither_a_header_nor_a_cut_note():
    """A PR whose every file the fix also touched has no outer tier. `""` is not
    a truncated `""`, and a labelled section with nothing under it reads as
    material that went missing."""
    got = scope(diff=chunk("fix.py", "+a"), increment=chunk("fix.py", "+a"),
                prior_diff=chunk("fix.py", "+a"))
    assert got.far == ""
    text = got.material(None)[0]
    assert "[cut:" not in text and "the rest of the PR" not in text


def test_the_brief_names_the_round_that_supplied_the_anchor():
    """`load_baseline` deliberately keeps an older anchor when the newest baseline
    names no commit, so a round 3 can be anchored on round 1's head — and telling
    its reviewers "Round 2 reviewed this PR at <round 1's sha>" states a falsehood
    in the sentence that defines what they must treat as already read."""
    assert "Round 1 reviewed" in scope(round_no=3, since_round=1).material(None)[0]
    assert "An earlier round reviewed" in scope(round_no=3).material(None)[0]


def test_an_anchorless_scope_reads_the_same_in_both_lines():
    """`decide` guarantees a non-empty anchor under increment scope, so this is
    only reachable by hand — which is when the brief and the target header would
    be read side by side, and they used to disagree."""
    text = scope(since="").material(None)[0]
    assert "at the previous round" in text
    assert "what changed since the previous round" in text


def test_composition_is_stable_across_runs():
    """Two runs of one round must build the same prompt."""
    assert len({scope().material(None)[0] for _ in range(5)}) == 1


def test_a_preamble_on_either_diff_still_reaches_the_reviewer():
    """Both mappings key a preamble by "", so leaving that key in the touched set
    would match the PR diff's own preamble and drop it out of the far tier — text
    deleted from the reviewer's copy by a coincidence of keys."""
    got = scope(diff="warning: from gh\n" + PR, increment="warning: from gh\n" + INCREMENT)
    assert "warning: from gh" in got.far


def test_the_tiers_follow_the_diffs_own_order():
    """`_diff_subset` promises the target in its original order and builds it that
    way; presenting the context alphabetically instead is a difference with no
    reason. The dicts are insertion-ordered, so nothing had to be sorted to make
    the prompt stable — the set is only ever an `in` test."""
    got = scope(diff=chunk("z.py", "+z") + chunk("a.py", "+a"),
                increment=chunk("q.py", "+q"), prior_diff="")
    assert got.far == chunk("z.py", "+z") + chunk("a.py", "+a")


# --------------------------------------------------------------- choosing the scope

FACTS = {"status": "ahead", "commits": 1, "total_commits": 1, "merges": 0}


def decide(monkeypatch, want="increment", round_no=2, anchor="1111111", head="2222222",
           increment=INCREMENT, prior=PRIOR, problem="", facts=None, base="main",
           since_round=1):
    """One scope decision with both compare fetches stubbed: the increment
    (`anchor...head`) and the PR as of the anchor (`base...anchor`)."""
    def fetch(repo, a, b):
        return (prior, "") if a == base else (increment, problem)
    said = dict(FACTS, files=len([f for f in panel._diff_by_file(increment) if f]))
    said.update(facts or {})
    monkeypatch.setattr(panel, "fetch_increment", fetch)
    monkeypatch.setattr(panel, "compare_facts", lambda *a: said)
    return panel.ReviewScope.decide(want, round_no, PR, (anchor, head), "acme/board",
                                    base, since_round)


def test_a_later_round_reviews_the_increment(monkeypatch):
    got, notes = decide(monkeypatch)
    assert got.scope == "increment"
    assert got.since == "1111111" and got.since_round == 1
    assert got.prior_diff == PRIOR
    assert notes == []


def test_round_one_is_always_the_whole_pr(monkeypatch):
    """Nothing to be an increment from. Silent, because `auto` reaches here on
    every round 1 of every cycle and a note on each would be noise."""
    got, notes = decide(monkeypatch, round_no=1, anchor="")
    assert got.scope == "pr"
    assert notes == []


def test_since_on_round_one_says_it_was_ignored(monkeypatch):
    """The one round-1 case worth a note: the caller asked for a range by hand,
    so they expected something other than what happened."""
    _, notes = decide(monkeypatch, round_no=1, since_round=None)
    assert "--since was passed on round 1" in notes[0]


def test_a_round_one_anchor_from_a_baseline_does_not_blame_a_flag(monkeypatch):
    """The branch tested the MERGED anchor and the message named only one of its
    two sources, so a baseline's `head_sha` on a round-1 run sent the reader
    hunting for a flag they never passed."""
    _, notes = decide(monkeypatch, round_no=1, since_round=2)
    assert "a baseline for round 2 named a head" in notes[0]
    assert "--since" not in notes[0]


def test_asking_for_pr_scope_gets_pr_scope(monkeypatch):
    got, notes = decide(monkeypatch, want="pr")
    assert got.scope == "pr"
    assert notes == []


def test_no_anchor_falls_back_and_says_which_flag_would_fix_it(monkeypatch):
    _, notes = decide(monkeypatch, anchor="")
    assert "no baseline said which commit it reviewed" in notes[0]
    assert "--since" in notes[0]


def test_an_unmoved_head_is_reported_as_a_fact_about_the_cycle(monkeypatch):
    """Another round ran without the fixer pushing anything, so there is no fix
    commit to read. Re-reviewing the PR is the useful thing to do with a round
    already paid for — but the caller has to be told, or a round that could not
    possibly find a regression reads as one that looked and found none."""
    got, notes = decide(monkeypatch, anchor="2222222", head="2222222")
    assert got.scope == "pr"
    assert "nothing was pushed between the rounds" in notes[0]
    assert "round 1 reviewed" in notes[0]


def test_an_abbreviated_anchor_still_matches_the_head(monkeypatch):
    """`--since` is documented as taking a SHA and SHAs are routinely written
    short. Compared raw, a seven-character anchor equal to the head missed this
    branch, fetched an empty range and reported "the head moved without the PR's
    content moving" — a description of something that did not happen."""
    _, notes = decide(monkeypatch, anchor="abc1234", head="abc1234def567")
    assert "nothing was pushed between the rounds" in notes[0]


def test_a_failed_fetch_falls_back_rather_than_killing_the_review(monkeypatch):
    """A scope optimisation must never cost a review that would otherwise have
    happened."""
    got, notes = decide(monkeypatch, increment="", problem="404 Not Found")
    assert got.scope == "pr"
    assert "404 Not Found" in notes[0]


def test_a_failed_context_fetch_falls_back_rather_than_mislabelling_the_context(
        monkeypatch):
    """The near tier costs a second compare call, and it can fail on its own. The
    fallback is the whole PR, not "use the current diff and call it the anchor's":
    re-reading the PR is only dearer, whereas a context header that is false
    suppresses findings."""
    def fetch(repo, a, b):
        return ("", "gh api failed") if a == "main" else (INCREMENT, "")
    monkeypatch.setattr(panel, "fetch_increment", fetch)
    monkeypatch.setattr(panel, "compare_facts", lambda *a: dict(FACTS, files=1))
    got, notes = panel.ReviewScope.decide("increment", 2, PR, ("1111111", "2222222"),
                                          "acme/board", "main", 1)
    assert got.scope == "pr"
    assert "the PR as of 1111111 was not" in notes[-1]


def test_an_empty_increment_falls_back_and_names_the_range(monkeypatch):
    """The head moved without the PR's content moving — an empty commit, a rebase
    onto the same tree, or a merge that only brought in the base branch.
    Reviewing nothing is not a cheaper round, it is no round."""
    got, notes = decide(monkeypatch, increment="   \n", facts={"files": 0})
    assert got.scope == "pr"
    assert "changed none of this PR's own files" in notes[-1]
    assert "1111111...2222222" in notes[-1]


def test_a_base_branch_merge_is_dropped_from_the_target(monkeypatch):
    """The range between two rounds spans whatever the fixer did INCLUDING a merge
    of the base branch, and on this repo that is the normal case — landing six PRs
    took eleven integration merges (#80). Measured on PR #62 the raw range was
    92,415 chars against a 45,370-char PR: the "increment" was twice the size of
    the whole thing, all of it files main had gained in between."""
    raw = INCREMENT + chunk("unrelated.py", "+from main") + chunk("also.py", "+from main")
    got, notes = decide(monkeypatch, increment=raw)
    assert got.scope == "increment"
    assert "unrelated.py" not in got.target and "also.py" not in got.target
    assert "the fix commit" in got.target
    assert "2 file(s) this PR does not" in notes[0]


def test_a_merge_in_the_range_is_reported_because_no_filter_can_remove_it(monkeypatch):
    """Files the PR does not touch are dropped from the target; main's changes to
    files it DOES touch cannot be, and a reviewer reads them as the fixer's work.
    The code already knew that and said nothing — this is the saying."""
    _, notes = decide(monkeypatch, facts={"merges": 2})
    assert "2 merge commit(s)" in notes[0]
    assert "cannot be told apart from the fixer's" in notes[0]


def test_a_non_ancestor_range_is_reported_rather_than_reviewed_as_a_delta(monkeypatch):
    """`a...b` is measured from the merge base, so after a force-push it is not
    the delta from `a`: anything the fixer REVERTED between the two heads is in
    neither tier, and the round cannot see that a finding it raised was addressed
    by deletion."""
    _, notes = decide(monkeypatch, facts={"status": "diverged"})
    assert "is `diverged`, not `ahead`" in notes[0]
    assert "REVERTED" in notes[0]


def test_a_truncated_compare_is_refused_rather_than_reviewed(monkeypatch):
    """GitHub truncates a large comparison with a 200 and no error, and the diff
    media type cannot be paginated. A short response is smaller than the PR, so it
    clears every guard and becomes the REVIEW TARGET: half a fix commit reviewed
    as though it were the whole of it — the exact failure `truncated` exists to
    catch, in the one place it cannot see."""
    got, notes = decide(monkeypatch, facts={"files": 9})
    assert got.scope == "pr"
    assert "returned 1 file(s) against the 9" in notes[-1]


def test_an_unreadable_compare_response_says_the_checks_did_not_run(monkeypatch):
    """No caveat would otherwise read as "checked, nothing wrong"."""
    monkeypatch.setattr(panel, "fetch_increment",
                        lambda repo, a, b: ((PRIOR, "") if a == "main" else (INCREMENT, "")))
    monkeypatch.setattr(panel, "compare_facts", lambda *a: {})
    got, notes = panel.ReviewScope.decide("increment", 2, PR, ("1111111", "2222222"),
                                          "acme/board", "main", 1)
    assert got.scope == "increment"
    assert "was not checked against GitHub's own account" in notes[0]


def test_an_increment_bigger_than_the_pr_falls_back(monkeypatch):
    """The floor under the whole feature: a round must never cost MORE than it did
    before scope existed. A file filter cannot remove main's changes to a file the
    PR also touches, so a big enough merge still leaves the increment larger than
    the PR — and at that point it is neither cheaper nor sharper."""
    got, notes = decide(monkeypatch, increment=chunk("fix.py", "+x" * len(PR)))
    assert got.scope == "pr"
    assert "neither cheaper nor sharper" in notes[-1]


def test_every_fallback_says_it_reviewed_the_whole_pr(monkeypatch):
    """The wording matters more than it looks. These notes are the only place the
    difference between "reviewed the increment" and "re-read everything" is
    visible — `diff_chars` being large is exactly what it always was."""
    for kw in ({"anchor": ""}, {"anchor": "2222222", "head": "2222222"},
               {"increment": "", "problem": "boom"},
               {"increment": " ", "facts": {"files": 0}},
               {"facts": {"files": 9}},
               {"increment": chunk("fix.py", "+x" * len(PR))}):
        _, notes = decide(monkeypatch, **kw)
        assert notes and "reviewed the whole PR, not the increment" in notes[-1], kw


# --------------------------------------------------------------- fetching a range

def _sh(monkeypatch, fn):
    monkeypatch.setattr(panel, "sh", fn)


def test_fetch_increment_asks_for_the_three_dot_diff(monkeypatch):
    """Two dots 404s, and would answer a different question if it did not: a diff
    against a commit no longer in the history, which the round would report on as
    though it were new."""
    seen = {}

    def fake(args, **kw):
        seen["args"] = args
        return "the diff"
    _sh(monkeypatch, fake)
    assert panel.fetch_increment("acme/board", "aaa1111", "bbb2222") == ("the diff", "")
    assert seen["args"][:2] == ["gh", "api"]
    assert seen["args"][2] == "repos/acme/board/compare/aaa1111...bbb2222"
    assert "Accept: application/vnd.github.diff" in seen["args"]


def test_fetch_increment_quotes_the_stderr_tail_of_a_failed_call(monkeypatch):
    def fake(args, **kw):
        raise subprocess.CalledProcessError(1, args, stderr="line one\ngh: Not Found (404)")
    _sh(monkeypatch, fake)
    diff, problem = panel.fetch_increment("acme/board", "aaa1111", "bbb2222")
    assert diff == ""
    assert "aaa1111...bbb2222" in problem and "Not Found (404)" in problem


@pytest.mark.parametrize("boom", [
    OSError("no gh"),
    subprocess.TimeoutExpired("gh", 5),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ValueError("something else entirely"),
])
def test_fetch_increment_never_raises(monkeypatch, boom):
    """The docstring's contract is load-bearing — `decide` has no `try` around the
    call — and naming two exception families did not keep it. `sh` runs with
    `text=True`, so a diff that is not valid UTF-8 raises `UnicodeDecodeError`, a
    `ValueError`; a `timeout=` through `sh` would raise `TimeoutExpired`, a
    `SubprocessError`. Either killed the whole panel run over a scope
    optimisation."""
    def fake(args, **kw):
        raise boom
    _sh(monkeypatch, fake)
    diff, problem = panel.fetch_increment("acme/board", "aaa1111", "bbb2222")
    assert diff == "" and problem.startswith("could not fetch")
    assert boom.__class__.__name__ in problem


def test_compare_facts_reads_the_ranges_own_account(monkeypatch):
    seen = {}

    def fake(args, **kw):
        seen["args"] = args
        return '{"status": "ahead", "files": 3, "commits": 2, ' \
               '"total_commits": 2, "merges": 1}'
    _sh(monkeypatch, fake)
    got = panel.compare_facts("acme/board", "aaa1111", "bbb2222")
    assert got["status"] == "ahead" and got["files"] == 3 and got["merges"] == 1
    assert "--jq" in seen["args"]


@pytest.mark.parametrize("reply", ["not json", "[1, 2]", '"a string"'])
def test_compare_facts_returns_nothing_it_cannot_read(monkeypatch, reply):
    _sh(monkeypatch, lambda args, **kw: reply)
    assert panel.compare_facts("acme/board", "a", "b") == {}


def test_compare_facts_never_raises(monkeypatch):
    def fake(args, **kw):
        raise subprocess.CalledProcessError(1, args, stderr="boom")
    _sh(monkeypatch, fake)
    assert panel.compare_facts("acme/board", "a", "b") == {}


# --------------------------------------------------------------- the configured scope

def test_the_config_value_is_validated_and_a_typo_says_so():
    """`decide` treats every string that is not exactly `increment` as `pr`, and
    says nothing about it — so `round_scope: incremental` produced a round 2 that
    re-read the whole PR and reported no reason, in a feature whose whole contract
    is that every fallback is written down. `--scope` goes through argparse's
    `choices`; a repo config goes through nothing."""
    for bad in ("incremental", "Increment", "inc", 3, True, []):
        notes = []
        assert panel.resolve_round_scope("auto", {"round_scope": bad}, notes) == \
            panel.DEFAULT_ROUND_SCOPE
        assert notes and "is not one of" in notes[0], bad


def test_an_unset_or_auto_config_value_is_the_default_and_is_silent():
    for cfg in ({}, {"round_scope": None}, {"round_scope": ""}, {"round_scope": "auto"}):
        notes = []
        assert panel.resolve_round_scope("auto", cfg, notes) == panel.DEFAULT_ROUND_SCOPE
        assert notes == []


def test_the_flag_wins_over_the_config():
    notes = []
    assert panel.resolve_round_scope("pr", {"round_scope": "increment"}, notes) == "pr"
    assert notes == []


# --------------------------------------------------------------- the anchor

def payload(round_no: int, **kw) -> dict:
    return {"github": "acme/board", "pr": 7, "round": round_no, "reviewers_ran": ["claude"],
            "cycle": "cyc", "to_fix": [], "dismissed": [], "sonar_findings": [], **kw}


def write(tmp_path, name: str, body: dict) -> str:
    path = tmp_path / name
    path.write_text(panel.json.dumps(body))
    return str(path)


def load(paths, round_no=3):
    return panel.load_baseline(paths, {"github": "acme/board", "pr": 7, "round": round_no})


def test_the_anchor_comes_from_the_latest_round_not_the_earliest(tmp_path):
    """`cycle` is inherited from the EARLIEST baseline and the anchor from the
    LATEST — two rules over one set, and both are right. A cycle is named once;
    an increment is "what changed since anyone last looked". Anchoring on the
    earliest would hand round 3 rounds 1 AND 2's work and re-review a fix commit
    round 2 already read."""
    base = load([write(tmp_path, "r1.json", payload(1, head_sha="1111111111")),
                 write(tmp_path, "r2.json", payload(2, head_sha="2222222222"))])
    assert base.head_sha == "2222222222" and base.head_round == 2
    assert base.cycle == "cyc"


def test_the_anchor_does_not_depend_on_the_order_the_baselines_were_passed(tmp_path):
    base = load([write(tmp_path, "r2.json", payload(2, head_sha="2222222222")),
                 write(tmp_path, "r1.json", payload(1, head_sha="1111111111"))])
    assert base.head_sha == "2222222222"


def test_an_older_round_still_anchors_when_the_newest_names_no_commit(tmp_path):
    """A payload written before `head_sha` existed must not CLEAR an anchor an
    earlier one supplied: an older commit we can diff against is worth more than
    no increment at all, and whole-PR scope is still the fallback if nothing in
    the set names one.

    Both orderings, because it only used to work in one of them: `b.rounds` holds
    every round accepted so far, so `was >= max(b.rounds)` let the anchorless
    round 2 block round 1's sha whenever it was read first. The same set of
    baselines anchored or did not on --baseline argument order alone."""
    files = {"r1.json": payload(1, head_sha="1111111111"), "r2.json": payload(2)}
    paths = {name: write(tmp_path, name, body) for name, body in files.items()}
    for order in (["r1.json", "r2.json"], ["r2.json", "r1.json"]):
        base = load([paths[n] for n in order])
        assert base.head_sha == "1111111111", order
        assert base.head_round == 1


def test_no_baseline_names_a_commit_and_there_is_no_anchor(tmp_path):
    base = load([write(tmp_path, "r1.json", payload(1))])
    assert base.head_sha is None and base.head_round is None


def test_an_anchor_that_could_address_another_endpoint_is_refused(tmp_path):
    """It is interpolated into a REST path, and a baseline is a file the caller
    points at. No shell is involved, so this is not injection — it is a value that
    quietly reviews the wrong thing or 404s without saying why."""
    base = load([write(tmp_path, "r1.json", payload(1, head_sha="../../../etc"))])
    assert base.head_sha is None
    assert base.problems and "cannot be a commit or a ref" in base.problems[0]


def test_a_rejected_baseline_does_not_lend_its_anchor(tmp_path):
    """A payload from another PR is not a thinner baseline, it is a wrong one —
    and anchoring an increment on another PR's head would review a range with
    nothing to do with this cycle."""
    other = {"github": "acme/board", "pr": 99, "round": 2, "head_sha": "9999999999",
             "to_fix": [], "dismissed": [], "sonar_findings": []}
    base = load([write(tmp_path, "other.json", other)])
    assert base.head_sha is None
    assert base.problems


def test_an_earlier_rounds_truncation_is_carried_forward(tmp_path):
    """Increment scope makes an old truncation PERMANENT. Under whole-PR scope a
    region round 1 was cut off from is read again by round 2; under increment
    scope round 2 reads only the fix commit and never returns to it, so the cycle
    can converge over code no round in it ever read. That has to reach the veto
    list or the cheaper round buys its saving out of coverage nobody is told it
    lost."""
    cut = payload(1, head_sha="1111111111",
                  reviewers={"claude": {"ran": True, "truncated": True},
                             "codex": {"ran": True, "truncated": False}})
    assert load([write(tmp_path, "r1.json", cut)]).truncated_rounds == {1}

    clean = payload(1, head_sha="1111111111",
                    reviewers={"claude": {"ran": True, "truncated": False}})
    assert load([write(tmp_path, "clean.json", clean)]).truncated_rounds == set()


def test_a_later_whole_pr_round_closes_an_earlier_rounds_gap(tmp_path):
    """The set was accumulate-only, so a round 3 still vetoed on round 1's
    truncation even when round 2 had re-read the whole PR untruncated in between —
    asserting that nothing had read that region, which the baselines themselves
    disprove."""
    cut = payload(1, head_sha="1111111111",
                  reviewers={"claude": {"ran": True, "truncated": True}})
    whole = payload(2, head_sha="2222222222", scope="pr",
                    reviewers={"claude": {"ran": True, "truncated": False}})
    scoped = payload(2, head_sha="2222222222", scope="increment",
                     reviewers={"claude": {"ran": True, "truncated": False}})
    r1 = write(tmp_path, "r1.json", cut)
    assert load([r1, write(tmp_path, "r2.json", whole)]).truncated_rounds == set()
    # ...but a scoped round 2 never returned to it, so the gap is still open.
    assert load([r1, write(tmp_path, "r2b.json", scoped)]).truncated_rounds == {1}


def test_a_round_that_read_nothing_is_recorded_as_unread(tmp_path):
    """A title-skipped round records a head, and so does one whose every seat
    failed. The anchor advances over it either way, so the next scoped round's
    increment starts after code the cycle has no read of."""
    skipped = payload(2, head_sha="2222222222", reviewers_ran=[], reviewed=False)
    base = load([write(tmp_path, "r1.json", payload(1, head_sha="1111111111")),
                 write(tmp_path, "r2.json", skipped)])
    assert base.unread_rounds == {2} and base.head_sha == "2222222222"


def test_a_payload_that_never_said_who_ran_claims_nothing(tmp_path):
    """"No `reviewers_ran` key" is not "no reviewer ran" — inventing a veto out of
    a missing field would put a standing caveat on every cycle with an old
    baseline."""
    old = {k: v for k, v in payload(1, head_sha="1111111111").items()
           if k != "reviewers_ran"}
    assert load([write(tmp_path, "r1.json", old)]).unread_rounds == set()


def test_a_payload_with_no_reviewer_block_declares_no_truncation(tmp_path):
    """Pre-v2.15 payloads carry no per-member record. "Nobody said" is not "no
    truncation happened", but inventing a veto out of a missing field would put a
    standing caveat on every cycle whose baseline is old."""
    assert load([write(tmp_path, "r1.json", payload(1))]).truncated_rounds == set()


def test_a_malformed_reviewers_block_costs_a_note_not_the_run(tmp_path):
    """`or {}` substitutes only for a FALSY value, so a hand-edited baseline whose
    `reviewers` is a list went straight into `.values()` and raised — in the one
    function whose rule is that a bad payload costs a `problems` entry rather than
    a review every reviewer CLI has already been paid for."""
    for junk in ([{"truncated": True}], "claude", 7):
        base = load([write(tmp_path, "r1.json", payload(1, reviewers=junk))])
        assert base.truncated_rounds == set() and base.rounds == {1}


# --------------------------------------------------------------- the payload's promises

def test_every_scope_key_has_a_default():
    """The skip-pattern exit emits a payload too, and a consumer reading
    `payload['scope']` should not have to know which exit produced it."""
    defaults = panel._payload_defaults()
    assert defaults["scope"] == "pr"
    assert defaults["since_sha"] is None
    assert defaults["head_sha"] is None
    assert defaults["context_chars"] == 0


def test_pr_scope_is_the_default_shape():
    """`scope` is recorded rather than inferred from the round number, because
    scope falls back to "pr" whenever the anchor is missing — so "round 2" does
    not imply "increment"."""
    assert panel.ReviewScope().scope == "pr"
    assert panel.DEFAULT_ROUND_SCOPE == "increment"
    assert panel.ReviewScope(diff=PR).target == PR


def test_rounds_are_named_in_the_plural_when_there_are_several():
    """These land in the veto list, which the operator is told to read as the
    reason a quiet round is not convergence — "round 1, 2 had a truncated
    reviewer" reads as a typo in one of the more closely-read lines the tool
    emits."""
    assert panel._rounds_phrase([1]) == "round 1"
    assert panel._rounds_phrase([1, 2]) == "rounds 1, 2"


# --------------------------------------------------------------- the whole round

CFG = {"github": "acme/board", "path": "/tmp/acme-board", "name": "board",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
       "review_panel": {}}
HEAD = "b" * 40
ANCHOR = "a" * 40
#: What `decide` builds for the runs below, so a test can work out a budget that
#: lands between "the whole target fits" and "all of the context fits".
SCOPED = panel.ReviewScope(scope="increment", diff=PR, increment=INCREMENT,
                           prior_diff=PRIOR, since=ANCHOR, round_no=2, since_round=1)


def budget_for_partial_context() -> int:
    """A budget that buys the frame, the markers and the whole target, and then
    runs out part way through the context — the case the whole priority order
    exists for, and the one the short-context note and veto are about."""
    want = overhead(SCOPED) + 250 + len(INCREMENT)
    target, context = SCOPED.material(want)[1:]
    assert target == len(INCREMENT), "the budget must not cut the target"
    assert context < len(SCOPED.near) + len(SCOPED.far), "it must cut the context"
    return want


def _judge(seen):
    def fake(clusters, diff, model, pr, budget=None, coverage=None):
        seen["diff"], seen["budget"] = diff, budget
        return [], None, ""
    return fake


def _stub_run(monkeypatch, seen, *, cfg=None, findings=(), increment=INCREMENT,
              problem="", facts=None, title="feat: x"):
    """Every process a run would spawn, replaced — so what is under test is the
    wiring inside `run()` rather than any CLI."""
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: dict(cfg or CFG))

    def fake_sh(args, **kw):
        if args[:3] == ["gh", "pr", "view"]:
            return json.dumps({"title": title, "additions": 3, "deletions": 1,
                               "baseRefName": "main", "headRefName": "feat/x",
                               "headRefOid": HEAD})
        # The compare call PROVENANCE makes (v2.24), answered in the shape its
        # `--jq` projects. Scope's own compare calls are stubbed at
        # `fetch_increment`/`compare_facts` below, so this one is the only reader
        # left — and left unanswered it degrades to a `config_notes` entry, which
        # these tests read as a scope note that never happened.
        if args[:2] == ["gh", "api"] and "/compare/" in args[2]:
            return json.dumps({"status": "ahead", "files": [
                {"filename": "fix.py", "patch": "@@ -1,1 +1,2 @@\n+the fix commit"}]})
        return PR

    monkeypatch.setattr(panel, "sh", fake_sh)
    monkeypatch.setattr(panel, "fetch_increment",
                        lambda repo, a, b: (PRIOR, "") if a == "main"
                        else (increment, problem))
    said = dict(FACTS, files=len([f for f in panel._diff_by_file(increment) if f]))
    said.update(facts or {})
    monkeypatch.setattr(panel, "compare_facts", lambda *a: said)
    def reviewer(name, model, prompt, effort=""):
        seen.setdefault("prompts", {})[name] = prompt
        return panel.ReviewerRun(list(findings), None, 10, [])
    monkeypatch.setattr(panel, "review_llm", reviewer)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", _judge(seen))


def _round(monkeypatch, tmp_path, seen, *, round_no=2, baselines=(), **kw):
    _stub_run(monkeypatch, seen, **kw)
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baselines),
                     max_rounds=3) == 0
    return json.loads(out.read_text())


def _baseline(tmp_path, name="r1.json", **kw):
    body = {"repo": "board", "github": "acme/board", "pr": 34, "round": 1,
            "cycle": "cyc", "head_sha": ANCHOR, "reviewers_ran": ["claude"],
            "to_fix": [], "dismissed": [], "sonar_findings": [], **kw}
    path = tmp_path / name
    path.write_text(json.dumps(body))
    return str(path)


def test_a_scoped_round_records_what_it_reviewed_and_against_what(monkeypatch, tmp_path):
    """The payload IS the measurement — `scope`, `since_sha` and `head_sha` are
    what a consumer reads to know whether a round 2's small `diff_chars` is a
    scoped round or a shrinking PR, and what the round after it can anchor on."""
    seen = {}
    got = _round(monkeypatch, tmp_path, seen, baselines=[_baseline(tmp_path)])
    assert got["scope"] == "increment"
    assert got["since_sha"] == ANCHOR and got["head_sha"] == HEAD
    assert got["diff_chars"] == len(INCREMENT) < len(PR)
    assert got["context_chars"] == len(SCOPED.near) + len(SCOPED.far)
    assert got["config_notes"] == []
    assert "the fix commit" in seen["prompts"]["claude"]


def test_a_scope_note_reaches_config_notes(monkeypatch, tmp_path):
    """The contract the docs state to the operator: if a round was not scoped, the
    reason is in `config_notes`. A note `decide` produced that `run()` dropped
    would leave `scope: "pr"` on a round 2 with nothing to explain it."""
    seen = {}
    got = _round(monkeypatch, tmp_path, seen, baselines=[_baseline(tmp_path)],
                 problem="404 Not Found", increment="")
    assert got["scope"] == "pr" and got["since_sha"] is None
    assert any("404 Not Found" in n for n in got["config_notes"])


def test_a_bad_configured_scope_says_so_rather_than_acting_on_it(monkeypatch, tmp_path):
    """`decide` reads every string that is not exactly `increment` as `pr`, so
    `round_scope: incremental` used to turn scoping OFF silently — the config said
    one thing, the round did another, and `config_notes` said nothing at all."""
    seen = {}
    cfg = dict(CFG, review_panel={"round_scope": "incremental"})
    got = _round(monkeypatch, tmp_path, seen, cfg=cfg, baselines=[_baseline(tmp_path)])
    assert got["scope"] == panel.DEFAULT_ROUND_SCOPE
    assert any("`round_scope`='incremental'" in n for n in got["config_notes"])


def test_a_configured_pr_scope_is_honoured(monkeypatch, tmp_path):
    """The escape hatch for a repo whose PRs are small enough that re-reading them
    costs nothing — and the thing a typo must not be mistaken for."""
    seen = {}
    cfg = dict(CFG, review_panel={"round_scope": "pr"})
    got = _round(monkeypatch, tmp_path, seen, cfg=cfg, baselines=[_baseline(tmp_path)])
    assert got["scope"] == "pr" and got["config_notes"] == []


def test_truncation_is_measured_against_the_target_not_the_material(monkeypatch,
                                                                    tmp_path):
    """A budget between the target and the whole material is not truncation — it
    is the priority order working, and counting it here would make `truncated`
    fire on almost every increment round and stop meaning anything on the round
    where it matters. It is still a coverage gap, so it is both a note and a
    VETO: the context is the only part of the PR a scoped round can find a
    pre-existing defect in."""
    seen = {}
    cfg = dict(CFG, review_panel={"max_diff_chars": budget_for_partial_context()})
    got = _round(monkeypatch, tmp_path, seen, cfg=cfg, baselines=[_baseline(tmp_path)])
    assert got["diff_truncated"] is False
    assert got["reviewers"]["claude"]["truncated"] is False
    assert any("only part of the PR context" in n for n in got["config_notes"])
    assert any("saw only part of the PR behind the increment" in v
               for v in got["round_stop"]["veto"])
    assert got["round_stop"]["confident"] is False


def test_a_budget_under_the_target_is_truncation(monkeypatch, tmp_path):
    """The one thing that must never pass silently: a reviewer handed a prefix of
    the thing it is reviewing cannot see what it was not given."""
    seen = {}
    cfg = dict(CFG, review_panel={"max_diff_chars": 30})
    got = _round(monkeypatch, tmp_path, seen, cfg=cfg, baselines=[_baseline(tmp_path)])
    assert got["diff_truncated"] is True
    assert got["reviewers"]["claude"]["truncated"] is True
    # And the veto quotes the TARGET's size, not the PR's — the reviewer was cut
    # off from the increment, which is what it was asked to read.
    assert any(f"of {len(INCREMENT):,} diff chars" in v
               for v in got["round_stop"]["veto"])


def test_an_inherited_truncation_vetoes_a_scoped_round(monkeypatch, tmp_path):
    """The cost this feature has that its own numbers cannot show: round 2 reads
    only the fix commit and never returns to what round 1 was cut off from, so the
    cycle can converge over code no round in it ever read."""
    seen = {}
    cut = _baseline(tmp_path, reviewers={"claude": {"ran": True, "truncated": True}})
    got = _round(monkeypatch, tmp_path, seen, baselines=[cut])
    assert got["scope"] == "increment"
    assert any("round 1 had a truncated reviewer" in v
               for v in got["round_stop"]["veto"])


def test_a_round_that_read_nothing_vetoes_the_round_after_it(monkeypatch, tmp_path):
    """A skipped round still records a head, so the anchor steps over it — and
    `scope`/`since_sha` say what this round reviewed, never what it stepped over."""
    seen = {}
    skipped = _baseline(tmp_path, reviewers_ran=[], reviewed=False)
    got = _round(monkeypatch, tmp_path, seen, baselines=[skipped])
    assert any("no reviewer read it" in v for v in got["round_stop"]["veto"])


def test_the_judge_is_handed_fitted_material_and_no_second_budget(monkeypatch,
                                                                  tmp_path):
    """The judge must hold what the panel held — an adjudicator ruling "not in the
    diff" against a different diff cannot be recovered from, and it does it with
    the authority of the final call. The material arrives already fitted, so
    passing the budget on would cut it a second time, through the marker that says
    how much is missing."""
    seen = {}
    _round(monkeypatch, tmp_path, seen, baselines=[_baseline(tmp_path)])
    assert seen["budget"] is None
    assert "REVIEW TARGET" in seen["diff"] and "the fix commit" in seen["diff"]
    assert "YOUR REVIEW TARGET" not in seen["diff"]


def test_a_judge_short_of_the_material_is_reported_and_vetoes(monkeypatch, tmp_path):
    """`judge_max_diff_chars` can cut the judge's copy where no reviewer's was
    cut, and nothing looked at it: the judge would dismiss a finding whose
    evidence sat in the part it did not get, and the round would record that as
    convergence."""
    seen = {}
    cfg = dict(CFG, review_panel={"judge_max_diff_chars": budget_for_partial_context()})
    got = _round(monkeypatch, tmp_path, seen, cfg=cfg, baselines=[_baseline(tmp_path)])
    assert any("the judge saw" in n for n in got["config_notes"])
    assert any("the judge saw" in v for v in got["round_stop"]["veto"])
    assert got["round_stop"]["confident"] is False


def test_a_round_one_run_reviews_the_whole_pr_and_says_nothing_about_it(monkeypatch,
                                                                        tmp_path):
    """`auto` reaches the round-1 branch on every cycle, so a note there would be
    noise on every first round ever run."""
    seen = {}
    got = _round(monkeypatch, tmp_path, seen, round_no=1)
    assert got["scope"] == "pr" and got["diff_chars"] == len(PR)
    assert got["context_chars"] == 0 and got["config_notes"] == []
    assert seen["prompts"]["claude"].count(panel.PR_SCOPE_HEADER) == 1
