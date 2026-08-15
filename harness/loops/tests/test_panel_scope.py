"""What a round past the first actually reviews (#41).

Round 2 exists to read the fix commit — nobody else does (#24) — and until
v2.23 it was handed the whole PR instead: the fix plus everything the earlier
rounds had already read and confirmed, paid for again in budget, wall-clock and
attention. PR #34's four rounds grew 140 KB -> 292 KB *because* it was being
reviewed, until both reviewers declared they could not read ~600 lines of one
test file. A review loop that inflates its own input degrades its own later
rounds.

So a later round reviews the INCREMENT, with the rest of the PR as context. The
tests here are about the two things that has to get right:

- the target is never the thing that gets cut, and
- every fall back to whole-PR scope is stated rather than silent, because a
  round that claims it reviewed the increment and in fact re-read the PR is
  wrong about the one measurement this feature exists to produce.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402


def chunk(path: str, body: str) -> str:
    """One file's worth of unified diff, in the shape `gh pr diff` emits."""
    return f"diff --git a/{path} b/{path}\n@@ -1,1 +1,2 @@\n{body}\n"


PR = chunk("fix.py", "+fixed\n+again") + chunk("old.py", "+settled") + chunk("doc.md", "+prose")
INCREMENT = chunk("fix.py", "+the fix commit")


# --------------------------------------------------------------- splitting a diff

def test_a_diff_splits_into_the_files_it_touches():
    got = panel._diff_by_file(PR)
    assert sorted(got) == ["doc.md", "fix.py", "old.py"]
    # Every byte accounted for: the partition is used to build a prompt, so a
    # chunk silently dropped is a file the reviewer is never shown.
    assert "".join(got[f] for f in ["fix.py", "old.py", "doc.md"]) == PR


def test_a_header_that_will_not_split_still_lands_somewhere():
    """`" b/"` is ambiguous for a path containing it. `_diff_added_lines` drops
    such a file (a line nobody can attribute scopes no Sonar issue); here the
    harmless direction is the opposite one — keep the chunk, key it by the whole
    header, and let it fall to the outer context tier. Dropping it would delete
    the file from the prompt."""
    weird = "diff --git nonsense\n@@ -1,1 +1,2 @@\n+x\n"
    got = panel._diff_by_file(weird)
    assert list(got) == ["diff --git nonsense"]
    assert "".join(got.values()) == weird


def test_the_two_splitters_agree_on_the_key():
    """They must, or a file lands in the wrong context tier: `near` is computed
    by matching one function's keys against the other's."""
    assert set(panel._diff_added_lines(PR)) <= set(panel._diff_by_file(PR))


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


def test_fitting_is_monotone_in_the_budget():
    """`fit_argv_budget` binary-searches over this. A non-monotone allocation
    would let that search settle on a size that does not fit."""
    parts = ["aaaa", "bbbb", "cccc"]
    sizes = [sum(len(p) for p in panel._fit_parts(parts, b)) for b in range(0, 15)]
    assert sizes == sorted(sizes)


# --------------------------------------------------------------- what gets composed

def test_whole_pr_scope_is_byte_identical_to_what_it_always_was():
    """The comparison between a scoped round and an unscoped one is only worth
    something if the unscoped one did not also change. `--- DIFF ---` moved from
    the prompt template into here; the rendered result must not have moved."""
    text, target, context = panel.ReviewScope(diff=PR).material(None)
    assert text == f"{panel.PR_SCOPE_HEADER}\n{PR}"
    assert (target, context) == (len(PR), 0)


def test_the_increment_is_the_target_and_the_pr_is_context():
    scope = panel.ReviewScope(scope="increment", diff=PR, increment=INCREMENT,
                              since="abc1234567", round_no=2)
    text, target, context = scope.material(None)
    assert target == len(INCREMENT)
    assert context == len(PR)
    assert "REVIEW TARGET" in text and "abc12345" in text
    # The target appears before any context, because the order it is read in is
    # the order the budget is spent in and both should say the same thing.
    assert text.index("the fix commit") < text.index("settled")


def test_the_files_the_fix_touches_are_the_first_context_it_gets():
    """The seam between the fix and the code it landed in is where a fix pass
    does its damage — #24's motivating defect was a mirror added in one file
    meeting an early `return` in another. So the PR's other changes to the files
    the fix touched outrank everything else."""
    scope = panel.ReviewScope(scope="increment", diff=PR, increment=INCREMENT,
                              since="abc1234", round_no=2)
    assert "fix.py" in scope.near and "old.py" not in scope.near
    assert "old.py" in scope.far and "doc.md" in scope.far
    text = scope.material(None)[0]
    assert text.index("+fixed") < text.index("+settled")


def test_a_tight_budget_drops_context_and_keeps_the_target_whole():
    """The point of the whole exercise. Under the old single-ceiling rule the
    thing lost was whatever sorted last in the diff; here it is always context."""
    scope = panel.ReviewScope(scope="increment", diff=PR, increment=INCREMENT,
                              since="abc1234", round_no=2)
    text, target, context = scope.material(len(INCREMENT))
    assert target == len(INCREMENT)
    assert context == 0
    assert "the fix commit" in text


def test_a_cut_section_says_how_much_is_missing():
    """Truncation of the TARGET is measured and never asked for — a reviewer
    cannot notice its own. Context is the other way round: a reviewer told the
    context is partial can declare it in `could_not_assess`, which turns a silent
    omission into one the judge can rule on."""
    scope = panel.ReviewScope(scope="increment", diff=PR, increment=INCREMENT,
                              since="abc1234", round_no=2)
    text = scope.material(len(INCREMENT) + 10)[0]
    assert "[cut:" in text and "not sent]" in text
    assert panel._cut_note("abc", "abc") == ""
    assert "2 not sent" in panel._cut_note("a", "abc")


def test_the_judge_gets_the_same_material_briefed_differently():
    """It must see what the parties saw — an adjudicator ruling "not in the diff"
    while holding a different diff is the one error it cannot recover from, and
    it would carry the authority of the final call. It must not be told to
    review."""
    scope = panel.ReviewScope(scope="increment", diff=PR, increment=INCREMENT,
                              since="abc1234", round_no=2)
    reviewer, judge = scope.material(None)[0], scope.judge_material(None)[0]
    assert "YOUR REVIEW TARGET" in reviewer and "YOUR REVIEW TARGET" not in judge
    for section in ("--- REVIEW TARGET", "the rest of the PR"):
        assert section in reviewer and section in judge
    # Same evidence, whatever the briefing above it says. Per file rather than as
    # one string: the PR is split across the two context tiers, so it is not
    # contiguous in either prompt — which is the point of the tiers.
    for body in [INCREMENT, *panel._diff_by_file(PR).values()]:
        assert body.strip() in reviewer and body.strip() in judge


def test_an_empty_far_tier_does_not_invent_a_cut_note():
    """A PR whose every file the fix also touched has no outer tier. `""` is not
    a truncated `""`, and saying so would put a spurious veto-shaped line in
    front of a reviewer."""
    scope = panel.ReviewScope(scope="increment", diff=chunk("fix.py", "+a"),
                              increment=chunk("fix.py", "+a"), since="abc1234",
                              round_no=2)
    assert scope.far == ""
    assert "[cut:" not in scope.material(None)[0]


def test_composition_is_stable_across_runs():
    """Two runs of one round must build the same prompt. The near/far split goes
    through a set, whose iteration order is not the diff's."""
    made = [panel.ReviewScope(scope="increment", diff=PR, increment=INCREMENT,
                              since="abc1234", round_no=2).material(None)[0]
            for _ in range(5)]
    assert len(set(made)) == 1


# --------------------------------------------------------------- choosing the scope

def decide(monkeypatch, want="increment", round_no=2, anchor="1111111", head="2222222",
           increment=INCREMENT, problem=""):
    monkeypatch.setattr(panel, "fetch_increment",
                        lambda repo, a, b: (increment, problem))
    return panel.ReviewScope.decide(want, round_no, PR, (anchor, head), "acme/board")


def test_a_later_round_reviews_the_increment(monkeypatch):
    scope, notes = decide(monkeypatch)
    assert scope.scope == "increment"
    assert scope.since == "1111111"
    assert notes == []


def test_round_one_is_always_the_whole_pr(monkeypatch):
    """Nothing to be an increment from. Silent, because `auto` reaches here on
    every round 1 of every cycle and a note on each would be noise."""
    scope, notes = decide(monkeypatch, round_no=1, anchor="")
    assert scope.scope == "pr"
    assert notes == []


def test_since_on_round_one_says_it_was_ignored(monkeypatch):
    """The one round-1 case worth a note: the caller asked for a range by hand,
    so they expected something other than what happened."""
    scope, notes = decide(monkeypatch, round_no=1)
    assert scope.scope == "pr"
    assert "round 1" in notes[0]


def test_asking_for_pr_scope_gets_pr_scope(monkeypatch):
    scope, notes = decide(monkeypatch, want="pr")
    assert scope.scope == "pr"
    assert notes == []


def test_no_anchor_falls_back_and_says_which_flag_would_fix_it(monkeypatch):
    scope, notes = decide(monkeypatch, anchor="")
    assert scope.scope == "pr"
    assert "no baseline said which commit it reviewed" in notes[0]
    assert "--since" in notes[0]


def test_an_unmoved_head_is_reported_as_a_fact_about_the_cycle(monkeypatch):
    """Another round ran without the fixer pushing anything, so there is no fix
    commit to read. Re-reviewing the PR is the useful thing to do with a round
    already paid for — but the caller has to be told, or a round that could not
    possibly find a regression reads as one that looked and found none."""
    scope, notes = decide(monkeypatch, anchor="2222222", head="2222222")
    assert scope.scope == "pr"
    assert "nothing was pushed between the rounds" in notes[0]


def test_a_failed_fetch_falls_back_rather_than_killing_the_review(monkeypatch):
    """A scope optimisation must never cost a review that would otherwise have
    happened."""
    scope, notes = decide(monkeypatch, increment="", problem="404 Not Found")
    assert scope.scope == "pr"
    assert "404 Not Found" in notes[0]


def test_an_empty_increment_falls_back_and_names_the_range(monkeypatch):
    """The head moved without the PR's content moving — an empty commit, a rebase
    onto the same tree, or a merge that only brought in the base branch.
    Reviewing nothing is not a cheaper round, it is no round."""
    scope, notes = decide(monkeypatch, increment="   \n")
    assert scope.scope == "pr"
    assert "changed none of this PR's own files" in notes[0]
    assert "1111111...2222222" in notes[0]


def test_a_base_branch_merge_is_dropped_from_the_target(monkeypatch):
    """The range between two rounds spans whatever the fixer did INCLUDING a merge
    of the base branch, and on this repo that is the normal case — landing six PRs
    took eleven integration merges (#80). Measured on PR #62 the raw range was
    92,415 chars against a 45,370-char PR: the "increment" was twice the size of
    the whole thing, all of it files main had gained in between."""
    raw = INCREMENT + chunk("unrelated.py", "+from main") + chunk("also.py", "+from main")
    scope, notes = decide(monkeypatch, increment=raw)
    assert scope.scope == "increment"
    assert "unrelated.py" not in scope.target and "also.py" not in scope.target
    assert "the fix commit" in scope.target
    assert "2 file(s) this PR does not" in notes[0]


def test_an_increment_bigger_than_the_pr_falls_back(monkeypatch):
    """The floor under the whole feature: a round must never cost MORE than it did
    before scope existed. A file filter cannot remove main's changes to a file the
    PR also touches, so a big enough merge still leaves the increment larger than
    the PR — and at that point it is neither cheaper nor sharper."""
    scope, notes = decide(monkeypatch, increment=chunk("fix.py", "+x" * len(PR)))
    assert scope.scope == "pr"
    assert "neither cheaper nor sharper" in notes[-1]


def test_every_fallback_says_it_reviewed_the_whole_pr(monkeypatch):
    """The wording matters more than it looks. These notes are the only place the
    difference between "reviewed the increment" and "re-read everything" is
    visible — `diff_chars` being large is exactly what it always was."""
    for kw in ({"anchor": ""}, {"anchor": "2222222", "head": "2222222"},
               {"increment": "", "problem": "boom"}, {"increment": " "},
               {"increment": chunk("fix.py", "+x" * len(PR))}):
        _, notes = decide(monkeypatch, **kw)
        assert notes and "reviewed the whole PR, not the increment" in notes[-1]


# --------------------------------------------------------------- the anchor

def payload(round_no: int, **kw) -> dict:
    return {"github": "acme/board", "pr": 7, "round": round_no,
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
    assert base.head_sha == "2222222222"
    assert base.cycle == "cyc"


def test_the_anchor_does_not_depend_on_the_order_the_baselines_were_passed(tmp_path):
    base = load([write(tmp_path, "r2.json", payload(2, head_sha="2222222222")),
                 write(tmp_path, "r1.json", payload(1, head_sha="1111111111"))])
    assert base.head_sha == "2222222222"


def test_an_older_round_still_anchors_when_the_newest_names_no_commit(tmp_path):
    """A payload written before `head_sha` existed must not CLEAR an anchor an
    earlier one supplied: an older commit we can diff against is worth more than
    no increment at all, and whole-PR scope is still the fallback if nothing in
    the set names one."""
    base = load([write(tmp_path, "r1.json", payload(1, head_sha="1111111111")),
                 write(tmp_path, "r2.json", payload(2))])
    assert base.head_sha == "1111111111"


def test_no_baseline_names_a_commit_and_there_is_no_anchor(tmp_path):
    base = load([write(tmp_path, "r1.json", payload(1))])
    assert base.head_sha is None


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


def test_a_payload_with_no_reviewer_block_declares_no_truncation(tmp_path):
    """Pre-v2.15 payloads carry no per-member record. "Nobody said" is not "no
    truncation happened", but inventing a veto out of a missing field would put a
    standing caveat on every cycle whose baseline is old."""
    assert load([write(tmp_path, "r1.json", payload(1))]).truncated_rounds == set()


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
