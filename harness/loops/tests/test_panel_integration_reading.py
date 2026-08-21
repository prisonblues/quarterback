"""After an integration: is the earlier round still a review of this PR? (#278)

Two mechanisms used to invalidate each other's work. *Review, then integrate*
threw the round away, because any integration moves the head and `preland` read a
moved head as a review of earlier code. *Integrate, then review* paid for a whole
fresh cycle whatever the merge contained. Both spend the same amount however
trivial the merge was, and neither looks at what actually changed — which at
#80's measured integration cost (quadratic in open PRs: five concurrent PRs is
about ten integration merges) and #275's measured 283,795 tokens per `claude`
seat per round is the ceiling on running more than one thing at a time.

The decision is that neither order is applied blindly: after an integration the
question is how much of the merge is genuinely new material TO THIS PR, and the
measurement is `git diff` between the pre-merge head and the merge result,
restricted to the files this PR touches, counted in changed lines.

Everything here defends four properties:

1. **The two readings are different verdicts**, on fixtures built from real git —
   a base that moved in files this PR does not touch, and a hand-resolved
   conflict in a file it does.
2. **The boundary is a dial**, `review_panel.distant_merge_lines`, validated like
   every other one — `null` restores the pre-#278 behaviour and `0` admits only an
   empty resolution.
3. **A push is never distant.** Size does not excuse unreviewed work of this PR's
   own kind; only a range carrying a merge can take the cheap reading.
4. **Which reading was taken is SAID**, in the round's `config_notes` and in the
   gate's own reasons, because a round that stood on a distant merge and one that
   re-reviewed a resolution are different claims about coverage.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness_rules  # noqa: E402
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_scope  # noqa: E402
import panel_seats  # noqa: E402
import preland  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
PANEL_REVIEW_PR = REPO_ROOT / "harness/commands/panel-review-pr.md"


# ------------------------------------------------------------ the two halves agree

def test_the_documented_default_is_the_applied_default():
    """`harness_rules.DEFAULTS` is what an operator reads; the `panel_core` constant
    is what the resolvers fall back to. A drift between them is invisible from
    either side — the file documents one allowance and the gate applies another."""
    assert (harness_rules.DEFAULTS["review_panel"]["distant_merge_lines"]
            == panel_core.DEFAULT_DISTANT_MERGE_LINES == 20)


def test_the_key_is_a_known_setting_and_not_a_typo():
    assert harness_rules.unknown_keys(
        {"review_panel": {"distant_merge_lines": 0}}) == {}


# ------------------------------------------------------------------- the resolver

@pytest.mark.parametrize("block,want", [
    ({}, 20),                                  # absent inherits the default
    ({"distant_merge_lines": 0}, 0),           # only an empty resolution is distant
    ({"distant_merge_lines": 5}, 5),
    ({"distant_merge_lines": 5.0}, 5),         # a generator's integral float
    ({"distant_merge_lines": " 5 "}, 5),       # a hand's string
    ({"distant_merge_lines": None}, None),     # a written null switches it off
    ({"distant_merge_lines": ""}, None),
])
def test_the_dial_reads_every_spelling_that_means_something(block, want):
    notes = []
    assert panel_seats.distant_merge_lines(block, notes) == want
    assert notes == []


@pytest.mark.parametrize("bad,accepted", [
    ("a few", "a whole number"),
    (5.5, "a whole number"),
    (-1, "zero or more"),
    # `false` is the obvious way a hand writes "off" and `isinstance(True, int)` is
    # True, so an unguarded integer read turns it into a ONE-LINE allowance — not
    # off, and not anything else either.
    (False, "a whole number"),
    (True, "a whole number"),
])
def test_a_bad_dial_is_refused_and_never_read_as_an_allowance(bad, accepted):
    notes = []
    with pytest.raises(SystemExit) as refusal:
        panel_seats.distant_merge_lines({"distant_merge_lines": bad}, notes)
    msg = str(refusal.value)
    assert "`review_panel.distant_merge_lines`" in msg and accepted in msg
    # The message has to say what to write, because the reader's next action is to
    # edit that key: all three readings are named.
    assert "0 to require an empty resolution" in msg
    assert "null to read every head move as a review of earlier code" in msg
    assert notes == []


# -------------------------------------------------------------------- the reading

SPAN = ("a" * 40, "b" * 40)


def reading(churn, merges=1, limit=20):
    return panel_scope.merge_involvement(
        panel_scope.Integration(churn=churn, merges=merges), limit, SPAN)


def test_an_empty_resolution_over_this_prs_files_is_the_distant_case():
    """"A merge whose resolution is empty over this PR's files is the distant case,
    mechanically" — the decision's own words, and the one reading that needs no
    judgement at all."""
    got = reading({})
    assert got.distant and got.lines == 0 and got.files == ()
    assert "DISTANT merge" in got.why and "still a review of this PR's change" in got.why


def test_a_resolution_past_the_allowance_is_involved_and_names_the_numbers():
    got = reading({"shared.py": 24, "other.py": 2})
    assert not got.distant and got.verdict == panel_scope.MERGE_INVOLVED
    assert got.lines == 26 and got.files == ("other.py", "shared.py")
    assert "26 line(s) across 2" in got.why and "20-line `distant_merge_lines`" in got.why
    assert "that resolution is unreviewed work" in got.why


def test_the_allowance_is_inclusive_at_its_own_edge():
    """At the limit is distant, one past it is not. Stated because an off-by-one
    here is a whole panel cycle in one direction and an unread merge in the other,
    and the key's documentation says "at or under"."""
    assert reading({"a.py": 20}).distant
    assert not reading({"a.py": 21}).distant


def test_a_push_is_never_distant_however_small():
    """The one way this measurement could WEAKEN a gate rather than sharpen it. A
    range with no merge commit in it is a push, and a push into this PR's own files
    is unreviewed work of exactly the kind a round exists to read — so size is not
    consulted at all."""
    got = reading({"a.py": 1}, merges=0)
    assert not got.distant
    assert "no merge commit" in got.why and "size does not excuse a push" in got.why


def test_a_zero_allowance_keeps_the_reading_and_admits_only_an_empty_resolution():
    assert reading({}, limit=0).distant
    assert not reading({"a.py": 1}, limit=0).distant


def test_a_null_allowance_reads_every_move_as_involved_and_says_which_it_is():
    """Off is not a failure and must not be reported as one: nothing could not be
    read, the repo asked for the pre-#278 behaviour."""
    got = reading({}, limit=None)
    assert got.verdict == panel_scope.MERGE_INVOLVED
    assert "`distant_merge_lines` is null for this repo" in got.why


def test_an_unreadable_range_is_unread_rather_than_distant():
    """`unread` is a real answer. It must never be `distant`, or a range nobody
    could measure would let a merge through on the strength of not having looked."""
    got = panel_scope.merge_involvement(
        panel_scope.Integration(problem="the branch was rewritten"), 20, SPAN)
    assert got.verdict == panel_scope.MERGE_UNREAD and not got.distant
    assert "could not be measured (the branch was rewritten)" in got.why


def test_deletions_count_as_much_as_additions():
    """The #80 incident is a merge that DELETED a landed fix — `stderr_gist` defined
    twice when a branch that had moved the function met a main that already had it,
    and the second definition won. An added-lines measure scores that at zero and
    calls the merge distant."""
    deleted = panel_scope._changed_lines(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,1 @@\n-x\n-y\n-z\n")
    assert deleted == 3


def test_the_file_headers_are_not_counted_as_churn():
    """`+++`/`---` are not content. Counted, every file carries two lines it does not
    have — which at a low allowance is the difference between distant and involved
    on a merge that changed nothing."""
    assert panel_scope._changed_lines(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n context\n") == 0


# ------------------------------------------------ real git: the two fixtures

def git(root, *args, **kw):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          text=True, capture_output=True, **kw).stdout


def write(root, name, body):
    (Path(root) / name).write_text(body, encoding="utf-8")


def build(tmp_path, main_touches, resolution, name="repo"):
    """A repo with a PR branch and an integration merge on it, from real git.

    ``main_touches`` is what `main` did after the PR branched; ``resolution`` is
    what the merge commit leaves in `shared.py` — the file the PR itself edits, and
    therefore the only one the measurement looks at.

    Returns ``(root, pre_merge_head, merge_head)``. The merge is made with
    ``--no-commit`` and then committed by hand, which is what an agent resolving a
    conflict does and what makes the resolution a real tree rather than a replay.
    """
    root = tmp_path / name
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example")
    git(root, "config", "user.name", "t")
    git(root, "config", "commit.gpgsign", "false")
    write(root, "shared.py", "".join(f"line {i}\n" for i in range(20)))
    write(root, "other.py", "unrelated\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-qb", "feat/x")
    write(root, "shared.py",
          "".join(f"FEAT {i}\n" for i in range(12))
          + "".join(f"line {i}\n" for i in range(12, 20)))
    git(root, "commit", "-qam", "the PR's own change")
    pre = git(root, "rev-parse", "HEAD").strip()
    git(root, "checkout", "-q", "main")
    for name, body in main_touches.items():
        write(root, name, body)
    git(root, "commit", "-qam", "main moved")
    git(root, "update-ref", "refs/remotes/origin/main", "main")
    git(root, "checkout", "-q", "feat/x")
    # `--no-commit` even where the merge would not conflict: the point is that the
    # tree the merge leaves is written HERE, so both fixtures are built the same way
    # and only what the resolution contains differs.
    subprocess.run(["git", "-C", str(root), "merge", "--no-commit", "--no-ff", "main"],
                   text=True, capture_output=True)
    write(root, "shared.py", resolution)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Merge branch 'main' into feat/x")
    return str(root), pre, git(root, "rev-parse", "HEAD").strip()


DISTANT_MAIN = {"other.py": "unrelated, and rewritten\n" * 40}
INVOLVED_MAIN = {"shared.py": "".join(f"MAIN {i}\n" for i in range(12))
                 + "".join(f"line {i}\n" for i in range(12, 20))}
#: The PR's own change, untouched — a merge that landed nothing in `shared.py`.
KEPT = ("".join(f"FEAT {i}\n" for i in range(12))
        + "".join(f"line {i}\n" for i in range(12, 20)))
#: A hand resolution taking both sides, in the twelve lines both branches edited.
HAND = ("".join(f"MAIN-AND-FEAT {i}\n" for i in range(12))
        + "".join(f"line {i}\n" for i in range(12, 20)))


@pytest.fixture
def distant(tmp_path):
    """`main` moved in a file this PR does not touch; `shared.py` is untouched by
    the merge. The resolution over this PR's files is empty."""
    return build(tmp_path, DISTANT_MAIN, KEPT, "distant")


@pytest.fixture
def involved(tmp_path):
    """A real conflict — both branches rewrote the same twelve lines of
    `shared.py` — resolved by hand into a third thing. 24 changed lines against the
    pre-merge head, past the 20-line allowance."""
    return build(tmp_path, INVOLVED_MAIN, HAND, "involved")


def pr_at(head, **over):
    return {"number": 7, "state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE",
            "headRefOid": head, "headRefName": "feat/x", "baseRefName": "main",
            "title": "feat: a thing", "url": "https://example/7",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}], **over}


def test_a_distant_merge_measures_as_nothing_and_an_involved_one_as_its_resolution(
        distant, involved):
    """The measurement itself, straight off real git and before any policy."""
    root, pre, head = distant
    step = preland._integration_since(root, pre, pr_at(head))
    assert step.problem == "" and step.merges == 1 and step.churn == {}

    root, pre, head = involved
    step = preland._integration_since(root, pre, pr_at(head))
    assert step.problem == "" and step.merges == 1
    assert step.churn == {"shared.py": 24}


def test_this_prs_own_files_are_measured_against_the_base_AFTER_the_integration(
        involved):
    """Which files are "this PR's" is `merge-base(origin/<base>, head)` to the head,
    and after an integration that merge base IS the base tip the merge brought in.
    That is the point: what the PR still contributes over the code it just absorbed
    is what an earlier round's findings were about, and a fork point computed from
    before the merge would count `main`'s own work as this PR's."""
    root, pre, head = involved
    fork = git(root, "merge-base", "origin/main", head).strip()
    assert fork == git(root, "rev-parse", "main").strip()
    assert git(root, "diff", "--name-only", fork, head).split() == ["shared.py"]


def test_the_range_is_restricted_to_this_prs_own_files(distant):
    """`other.py` is 40 rewritten lines the range genuinely contains, and it is not
    this PR's change — dragging it into the count would make every base merge
    involved, which is the "re-review everything" this replaces."""
    root, pre, head = distant
    assert "other.py" in git(root, "diff", "--name-only", pre, head)
    assert preland._integration_since(root, pre, pr_at(head)).churn == {}


# -------------------------------------------------- the gate, on those fixtures

def board_row(head_sha, **over):
    row = {"id": 1, "ts": "2026-08-21T12:00:00+00:00", "round": 1, "cycle": "f5c76fd8",
           "head_sha": head_sha, "stopped": True, "stop_reason": "dry",
           "stop_confident": True, "stop_veto": [], "confirmed": 0, "unjudged": 0,
           "sonar_gate": "OK", "judge_skip": None}
    return {**row, **over}


@pytest.fixture
def board(monkeypatch):
    answers = {}
    monkeypatch.setattr(preland, "board_get",
                        lambda path, params: answers.get(path.strip("/"),
                                                         (None, "board unreachable")))
    return answers


def review(board, fixture, limit=20):
    root, pre, head = fixture
    board["reviews"] = ([board_row(pre)], "")
    return preland.check_review("o/r", pr_at(head), False,
                                preland.MergeGate(root, limit))


def test_a_round_before_a_distant_merge_still_stands(board, distant):
    """The saving, and the whole point. The head moved, the round is older than it,
    and the round is STILL a review of this PR's change — because the merged code
    is not this PR's change and is not what the findings were about. Nothing is
    claimed as reviewed that was not."""
    c = review(board, distant)
    assert c.status == "passed" and not c.reasons
    # Not silence, either. A payload has to show that the head moved and why it was
    # let through.
    assert len(c.warnings) == 1
    assert "the PR's head is now" in c.warnings[0]
    assert "DISTANT merge" in c.warnings[0]
    assert c.detail["merge_reading"] == {"verdict": "distant", "lines": 0,
                                         "files": [], "limit": 20}


def test_a_round_before_an_involved_merge_is_a_review_of_earlier_code(board, involved):
    """The other half, and the one that must not soften. A hand resolution in code
    this PR also touches is unreviewed work, and #80's `stderr_gist` incident is
    what an unread one costs — a landed fix silently reverted because a function
    that had MOVED met a main that already had it."""
    c = review(board, involved)
    assert c.status == "failed"
    assert "it is a review of earlier code" in c.reasons[0]
    assert "24 line(s) across 1 of this PR's own file(s)" in c.reasons[0]
    assert "INVOLVED merge" in c.reasons[0]
    assert c.detail["merge_reading"]["files"] == ["shared.py"]


def test_the_boundary_is_the_dial_and_not_a_constant(board, distant, involved):
    """Both fixtures, both ways round. `null` is the pre-#278 behaviour — every head
    move is a review of earlier code, whatever the merge contained — and a large
    enough allowance takes even a hand-resolved conflict as distant. A repo's
    judgement about cost, per #305, and not this file's."""
    assert review(board, distant, limit=None).status == "failed"
    assert "`distant_merge_lines` is null" in review(board, distant,
                                                     limit=None).reasons[0]
    assert review(board, involved, limit=100).status == "passed"
    assert review(board, involved, limit=23).status == "failed"
    assert review(board, involved, limit=24).status == "passed"


def test_a_zero_allowance_holds_on_anything_the_merge_left_in_this_prs_files(
        board, tmp_path):
    """`0` is the strictest setting that still saves a round: an empty resolution
    passes, one changed line does not."""
    empty = build(tmp_path, DISTANT_MAIN, KEPT, "empty")
    # `main` revised one line of `shared.py` that the PR does not edit, so the merge
    # needed no hand at all and still left two changed lines in a file this PR
    # touches — the smallest merge that is not empty over this PR's files.
    trivial = build(tmp_path,
                    {"shared.py": "".join(f"line {i}\n" for i in range(19))
                                  + "line 19 revised\n"},
                    "".join(f"FEAT {i}\n" for i in range(12))
                    + "".join(f"line {i}\n" for i in range(12, 19))
                    + "line 19 revised\n", "trivial")
    assert preland._integration_since(trivial[0], trivial[1],
                                      pr_at(trivial[2])).churn == {"shared.py": 2}
    assert review(board, empty, limit=0).status == "passed"
    assert review(board, trivial, limit=0).status == "failed"
    assert review(board, trivial, limit=20).status == "passed"


def test_a_push_after_the_round_holds_however_small_it_is(board, tmp_path):
    """No merge in the range, so no cheap reading is available. This is the case the
    flat clause was right about and it must keep holding — a one-line fix pushed
    after a round is unreviewed work of this PR's own kind."""
    root, pre, head = build(tmp_path, DISTANT_MAIN, KEPT, "pushed")
    write(root, "shared.py", KEPT + "one more line\n")
    git(root, "commit", "-qam", "a small fix nobody reviewed")
    pushed = git(root, "rev-parse", "HEAD").strip()
    board["reviews"] = ([board_row(head)], "")
    c = preland.check_review("o/r", pr_at(pushed), False, preland.MergeGate(root, 20))
    assert c.status == "failed" and "no merge commit" in c.reasons[0]


def test_an_unmeasurable_move_holds_exactly_as_it_did_before(board, distant):
    """The fail-safe. No checkout, a commit that is not here, a rewritten branch —
    every one of them is an unread precondition, and an unread precondition is not
    a satisfied one."""
    root, pre, head = distant
    board["reviews"] = ([board_row(pre)], "")
    c = preland.check_review("o/r", pr_at(head), False, preland.MergeGate("", 20))
    assert c.status == "failed"
    assert "it is a review of earlier code" in c.reasons[0]
    assert "could not be measured" in c.reasons[0]
    assert c.detail["merge_reading"]["verdict"] == "unread"

    board["reviews"] = ([board_row("f" * 40)], "")
    gone = preland.check_review("o/r", pr_at(head), False, preland.MergeGate(root, 20))
    assert gone.status == "failed" and "is not in this checkout" in gone.reasons[0]


def test_an_unmoved_head_is_never_measured_at_all(board, distant):
    """The cheap path stays cheap: no `git` runs when the round read this very
    commit, which is every ordinary PR."""
    root, _pre, head = distant
    board["reviews"] = ([board_row(head)], "")
    c = preland.check_review("o/r", pr_at(head), False, preland.MergeGate(root, 20))
    assert c.status == "passed" and not c.warnings
    assert "merge_reading" not in c.detail


def test_a_merge_that_REVERTS_this_prs_change_is_never_distant(board, tmp_path):
    """The defect this whole reading exists to catch, and the one the file set can
    hide. The merge resolved `shared.py` all the way back to base — the PR's own
    change, which an earlier round read and confirmed, silently discarded. Measured
    from the head alone, `shared.py` is no longer one of this PR's files at all: it
    would score zero and the merge would read DISTANT on the strength of the change
    having been dropped. #80's `stderr_gist` incident is this shape."""
    root, pre, head = build(tmp_path, DISTANT_MAIN,
                            "".join(f"line {i}\n" for i in range(20)), "reverted")
    fork = git(root, "merge-base", "origin/main", head).strip()
    assert "shared.py" not in git(root, "diff", "--name-only", fork, head)
    step = preland._integration_since(root, pre, pr_at(head))
    assert step.churn == {"shared.py": 24}
    c = review(board, (root, pre, head))
    assert c.status == "failed" and "INVOLVED merge" in c.reasons[0]


def test_a_rewritten_branch_has_no_range_and_is_never_distant(board, tmp_path):
    """`git diff A B` compares two commits with no ancestry between them quite
    happily, and the answer is not a delta from `A`: anything dropped in the rewrite
    is in neither side. A small diff plus any merge commit in the replacement history
    would otherwise preserve a review of code that no longer exists."""
    root, pre, head = build(tmp_path, DISTANT_MAIN, KEPT, "rewritten")
    git(root, "checkout", "-qb", "feat/x2", "main")
    write(root, "shared.py", KEPT)
    git(root, "commit", "-qam", "the PR, rewritten onto main")
    rewritten = git(root, "rev-parse", "HEAD").strip()
    step = preland._integration_since(root, head, pr_at(rewritten))
    assert step.churn is None and "not an ancestor of the head" in step.problem
    board["reviews"] = ([board_row(head)], "")
    c = preland.check_review("o/r", pr_at(rewritten), False, preland.MergeGate(root, 20))
    assert c.status == "failed" and "the branch was rewritten" in c.reasons[0]


def test_a_binary_file_this_pr_also_touches_refuses_the_reading(board, tmp_path):
    """It cannot be counted in lines, and it must not be allowed to look like an
    empty resolution — a merge that replaced an asset this PR also edits is real
    material."""
    root = tmp_path / "binary"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example")
    git(root, "config", "user.name", "t")
    (root / "asset.bin").write_bytes(b"\x00\x01\x02")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-qb", "feat/x")
    (root / "asset.bin").write_bytes(b"\x00\x01\x03")
    git(root, "commit", "-qam", "the PR touches the asset")
    pre = git(root, "rev-parse", "HEAD").strip()
    git(root, "checkout", "-q", "main")
    (root / "asset.bin").write_bytes(b"\x00\x09\x09")
    git(root, "commit", "-qam", "main touches it too")
    git(root, "update-ref", "refs/remotes/origin/main", "main")
    git(root, "checkout", "-q", "feat/x")
    subprocess.run(["git", "-C", str(root), "merge", "--no-commit", "--no-ff", "main"],
                   text=True, capture_output=True)
    (root / "asset.bin").write_bytes(b"\x00\x01\x09")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Merge branch 'main' into feat/x")
    head = git(root, "rev-parse", "HEAD").strip()
    step = preland._integration_since(str(root), pre, pr_at(head))
    assert step.churn is None and "binary" in step.problem


# ----------------------------------------------- the round says which reading

CHUNK = ("diff --git a/shared.py b/shared.py\n@@ -1,1 +1,2 @@\n+one line\n")
#: doc.md is deliberately the bulk of it, so an increment carrying a real
#: resolution stays comfortably under the size guard that sends a round back to
#: whole-PR scope — the guard is not what these tests are about.
PR_DIFF = CHUNK + ("diff --git a/doc.md b/doc.md\n@@ -1,1 +1,2 @@\n"
                   + "+prose\n" * 400)


def decide(monkeypatch, increment, merges=1, limit=20):
    """One scope decision, with both compare fetches stubbed."""
    monkeypatch.setattr(panel_scope, "fetch_increment",
                        lambda repo, a, b: ((PR_DIFF, "") if a == "main"
                                            else (increment, "")))
    monkeypatch.setattr(panel_scope, "compare_facts",
                        lambda *a: {"status": "ahead", "commits": 2,
                                    "total_commits": 2, "merges": merges,
                                    "files": len([f for f in
                                                  panel._diff_by_file(increment) if f])})
    return panel.ReviewScope.decide("increment", 2, PR_DIFF, ("1111111", "2222222"),
                                    "acme/board", "main", 1, limit)


def test_a_round_after_a_distant_merge_says_the_earlier_round_stands(monkeypatch):
    """The merge brought in a file this PR does not touch and nothing else. The
    round must SAY it took the distant reading — a reader comparing two payloads
    must never have to infer which of the two happened."""
    brought_in = ("diff --git a/elsewhere.py b/elsewhere.py\n"
                  "@@ -1,1 +1,2 @@\n+main's own work\n")
    _, notes = decide(monkeypatch, brought_in)
    said = [n for n in notes if "takes the DISTANT reading" in n]
    assert len(said) == 1
    assert "changed 0 line(s) of this PR's own files" in said[0]
    assert "No round was required on this merge's account" in said[0]


def test_a_round_after_an_involved_merge_says_it_is_reading_the_resolution(monkeypatch):
    """The other claim about coverage, in the same field."""
    resolution = ("diff --git a/shared.py b/shared.py\n@@ -1,24 +1,24 @@\n"
                  + "".join(f"-old {i}\n+new {i}\n" for i in range(12)))
    got, notes = decide(monkeypatch, resolution)
    assert got.scope == "increment"
    said = [n for n in notes if "takes the INVOLVED reading" in n]
    assert len(said) == 1
    assert "24 line(s) across 1 of this PR's own file(s)" in said[0]
    assert "that resolution is unreviewed work" in said[0]


def test_a_range_with_no_merge_in_it_says_nothing_about_integrations(monkeypatch):
    """An ordinary fix pass is not an integration, and a note on every round 2
    saying so would be noise in the one field an operator reads for the caveats
    that matter."""
    _, notes = decide(monkeypatch, CHUNK.replace("one line", "the fix"), merges=0)
    assert not any("follows an integration" in n for n in notes)


def test_the_reading_survives_a_fallback_to_whole_pr_scope(monkeypatch):
    """The distant case usually leaves NO increment at all, which sends the round
    back to the whole PR — so a note held until the increment is used would be
    dropped exactly where it is most worth having."""
    brought_in = ("diff --git a/elsewhere.py b/elsewhere.py\n"
                  "@@ -1,1 +1,2 @@\n+main's own work\n")
    got, notes = decide(monkeypatch, brought_in)
    assert got.scope == "pr"
    assert any("changed none of this PR's own files" in n for n in notes)
    assert any("takes the DISTANT reading" in n for n in notes)


def test_a_null_dial_leaves_the_round_saying_the_reading_was_not_taken(monkeypatch):
    _, notes = decide(monkeypatch, CHUNK.replace("one line", "the fix"), limit=None)
    said = [n for n in notes if "follows an integration" in n]
    assert len(said) == 1 and "`distant_merge_lines` is null for this repo" in said[0]


# ------------------------------------------------------------------ the prose half

def test_the_orchestrator_is_told_what_the_two_readings_mean():
    """The skill is what an agent reads when a round comes back after an
    integration, and a `config_notes` line nobody is told to look for is a line
    nobody reads."""
    flat = " ".join(PANEL_REVIEW_PR.read_text(encoding="utf-8").split())
    assert "distant_merge_lines" in flat
    assert "takes the DISTANT reading" in flat and "takes the INVOLVED reading" in flat
    assert "the earlier round STANDS" in flat
