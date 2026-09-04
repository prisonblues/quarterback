"""#504 — reconstructing the fix range across a branch REWRITE.

Every test here runs against a REAL git repository built in `tmp_path`, and that
is the point rather than an accident. What is under test is a claim about git's
own behaviour — that `patch-id` is stable across a rebase that did not resolve a
conflict, and moves when one did — and a double that answers `diff-tree` and
`patch-id` with canned strings would be asserting that claim rather than checking
it. The rebases here are real rebases, and the conflict in `_rewritten` is a real
conflict resolved with real content.

The wiring lives at the bottom of the same file rather than beside #500's and
#512's tests in `test_panel_provenance.py`, because those rounds want a real
repository too and the builder above is the only thing that makes one. What it
borrows from there is the round harness itself, imported rather than copied.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import panel_scope  # noqa: E402

#: The repo the fork point is nominally on. Every test here stubs
#: :func:`panel_scope._merge_base_now`, so nothing reaches GitHub and the value
#: only has to be a plausible `owner/name`.
GH_REPO = "acme/e2e"


def _git_env(tmp_path):
    """A git environment that cannot read the developer's own config.

    The fleet installs managed hooks and a signing key globally, and a test that
    inherits them is a test that runs gitleaks on every fixture commit and fails on
    the machine of anyone whose `commit.gpgsign` is on. Same isolation
    `test_epic_model_ceiling._repo` uses and for the same reason."""
    (tmp_path / ".gitconfig").write_text("")
    (tmp_path / ".gitconfig-system").write_text("")
    return {# The ambient PATH, not a written-out one: `git` is in the Nix store on
            # this fleet and in /usr/bin elsewhere, and a hard-coded list makes the
            # suite pass on whichever of those the author happened to be on.
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "GIT_CONFIG_GLOBAL": str(tmp_path / ".gitconfig"),
            "GIT_CONFIG_SYSTEM": str(tmp_path / ".gitconfig-system"),
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com"}


class _Repo:
    """A git repo under test, with just enough sugar to write a history in a line."""

    def __init__(self, path: Path, env: dict):
        self.path, self.env = path, env

    def git(self, *args, check=True) -> str:
        out = subprocess.run(["git", "-C", str(self.path), *args], env=self.env,
                             capture_output=True, text=True)
        if check and out.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {out.stderr}")
        return out.stdout

    def write(self, name: str, body: str):
        (self.path / name).write_text(body)

    def commit(self, name: str, body: str, message: str) -> str:
        self.write(name, body)
        self.git("add", name)
        self.git("commit", "-q", "-m", message)
        return self.at("HEAD")

    def at(self, rev: str) -> str:
        return self.git("rev-parse", rev).strip()

    def subjects(self, *shas: str) -> list[str]:
        return [self.git("show", "-s", "--format=%s", s).strip() for s in shas]


def _new_repo(tmp_path) -> _Repo:
    env = _git_env(tmp_path)
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], env=env, check=True)
    return _Repo(path, env)


def _cycle(tmp_path, *, conflict: bool, fix: bool = True, base_commits: int = 1):
    """A review cycle with a rebase in the middle — the shape #500 measured.

    Round 1 reviews the branch at `anchor` (two commits: one editing `app.py`, one
    adding `lib.py`). A fix pass adds `fix.py`. `main` then moves — by
    `base_commits` commits, the last of which touches `app.py`'s own line when
    `conflict` is set — and the branch is rebased onto it, which is the ordinary
    and correct thing to do and the thing that blinds the round.

    Returns `(repo, anchor, head, expected_fix_subjects)`."""
    r = _new_repo(tmp_path)
    r.commit("app.py", "a\nb\nc\n", "m1")
    r.git("checkout", "-q", "-b", "pr")
    r.commit("app.py", "a\nPR ONE\nc\n", "p1")
    r.commit("lib.py", "x\n", "p2")
    anchor = r.at("HEAD")
    if fix:
        r.commit("fix.py", "y\n", "f1")
    r.git("checkout", "-q", "main")
    for i in range(base_commits):
        last = i == base_commits - 1
        if last and conflict:
            r.commit("app.py", "a\nMAIN TWO\nc\n", f"m{i + 2}")
        else:
            r.commit(f"main{i}.py", f"m{i}\n", f"m{i + 2}")
    r.git("checkout", "-q", "pr")
    rebase = subprocess.run(["git", "-C", str(r.path), "rebase", "main"],
                            env=r.env, capture_output=True, text=True)
    if rebase.returncode != 0:
        assert conflict, f"unexpected rebase conflict: {rebase.stderr}"
        r.write("app.py", "a\nPR ONE over MAIN TWO\nc\n")
        r.git("add", "app.py")
        r.git("-c", "core.editor=true", "rebase", "--continue")
    # p1 is only ever part of the fix pass by accident — it is the commit the
    # rebase had to rewrite — so a conflict run expects it and a clean run does not.
    expected = (["p1", "f1"] if conflict else ["f1"]) if fix else ([] if not conflict
                                                                   else ["p1"])
    return r, anchor, r.at("HEAD"), expected


@pytest.fixture
def fork_point(monkeypatch):
    """Point :func:`panel_scope._merge_base_now` at the repo's real fork point.

    The function under test asks the FORGE where the branch forks rather than the
    clone, and its docstring says why: a stale `origin/main` is an ancestor of the
    rebased head, so `git merge-base` answers with the stale tip and every base
    commit the rebase moved onto lands inside the reconstructed "fix pass". Here
    the forge is a stub that tells the truth; `test_a_fork_point_that_cannot_be_read`
    is the other half."""

    def install(repo: _Repo, answer=...):
        # `...` and not None as the "use the real one" default: None is a value the
        # forge genuinely returns — it is `_merge_base_now`'s own failure — and a
        # sentinel that collides with it makes the test for that case silently
        # exercise the happy path.
        real = repo.git("merge-base", "main", "pr").strip()
        monkeypatch.setattr(panel_scope, "_merge_base_now",
                            lambda *a, **k: real if answer is ... else answer)
        return real

    return install


# --------------------------------------------------------------- it reconstructs


def test_a_clean_rebase_gives_back_exactly_the_fix_pass(tmp_path, fork_point):
    """The case the whole issue is for, and the common one: `main` moved, somebody
    rebased between rounds, nothing conflicted.

    Before this, the round attributed NOTHING — `compare/a...b` is three-dot, the
    merge base moved back, and `_fix_range_diff` correctly refuses a span that has
    widened toward the whole PR. The commits are still there; only the range was
    wrong. So the answer is the fix pass and nothing else: `fix.py`, not `app.py`
    and not `lib.py`, which the last round had already reviewed and which came
    through the rebase with their patches intact."""
    r, anchor, head, expected = _cycle(tmp_path, conflict=False, base_commits=3)
    fork_point(r)

    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main", anchor, head)

    assert got["why"] is None
    assert r.subjects(*got["commits"]) == expected
    assert got["prior"] == 2 and got["carried"] == 2 and got["unmatched"] == 0
    assert "fix.py" in got["diff"]
    # The load-bearing negative. `main` moved three commits and the branch was
    # replayed onto them; if any of that is inside the range, the fixer is blamed
    # for commits nobody on this PR wrote — the 21-commit over-attribution #500
    # measured, reintroduced by the repair meant to end it.
    assert "main0.py" not in got["diff"]
    assert "lib.py" not in got["diff"]


def test_the_reconstructed_lines_are_the_ones_provenance_places_findings_against(
        tmp_path, fork_point):
    """End to end through the consumer, because a diff that reconstructs perfectly
    and does not parse has repaired nothing.

    `_diff_added_lines` is what `_provenance` and `_recurrence` both read, and it
    wants `diff --git` headers and `@@` hunks. `git diff-tree -p` emits those; the
    assertion is that it emits them for a set of commits handed in on stdin, which
    is the form this uses and not the form anything else in the panel does."""
    r, anchor, head, _ = _cycle(tmp_path, conflict=False)
    fork_point(r)
    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main", anchor, head)

    from panel_seats import _diff_added_lines
    assert _diff_added_lines(got["diff"]) == {"fix.py": {1}}


def test_a_conflict_RESOLVED_in_the_rebase_is_a_DECLINE_that_names_the_cost(
        tmp_path, fork_point):
    """The honest failure #504 names, and the answer it gets — which changed under
    review, so the reasoning is worth having here rather than in a commit message.

    A rebase that resolved a conflict CHANGED that commit's content, so its patch-id
    moved and it is somewhere among the commits with no counterpart. Here `p1` is
    that commit: the last round reviewed it, the rebase rewrote it, and nothing can
    say which of the leftovers it is.

    This first attributed anyway and reported the lean in `config_notes`, on the
    reading that per-commit degradation was what the issue asked for. The argument
    against it is not that a lean is dishonest but what the number DOES:
    `escalate_on.fix_injection` ends a cycle, and the README's case for its 0.5
    threshold is that `introduced` is a documented FLOOR — "a measured 0.64 is at
    least 0.64". A source that over-counts breaks that, and the price is a cycle
    stopped with real findings unfixed. Nothing reads a note before firing a brake.
    And `unmatched` bounds the damage at one COMMIT, which can be a thousand lines
    and cover every finding in the round.

    #504's own wording is what settles it: *that commit's lines cannot be
    attributed.* So this declines, and the reason names the rebase — which is the
    "report what did not" half of the issue's own instruction."""
    r, anchor, head, _ = _cycle(tmp_path, conflict=True)
    fork_point(r)

    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main", anchor, head)

    assert got["diff"] is None
    assert "changed content in the rewrite" in got["why"]
    # The correspondence it DID find still travels, because that is what tells an
    # operator a conflicted rebase is the thing to go and look at.
    assert (got["prior"], got["carried"], got["unmatched"]) == (2, 1, 1)


def test_an_AMENDED_TIP_corresponds_the_commits_BELOW_it(tmp_path, fork_point):
    """The defect that decides how the prior side is bounded.

    Not every rewrite is a rebase. A fixer who amends the tip leaves everything below
    it byte-identical — SHAs included — so `head..anchor` (the obvious spelling of
    "what the last round had and this branch no longer does") contains ONLY the
    amended commit, and every commit below the amend is absent from the prior set.

    Both bounds decline here, because an amended commit is content that moved and
    that is the unmatched refusal. What they differ on is the ANSWER: bounded by the
    fork point this says "1 of the 2 commits the last round reviewed changed content",
    which is what happened; bounded by the other head it would say none of 1 came
    through and the histories cannot be corresponded — the squash refusal, about a
    branch that was not squashed. A `why` that names the wrong rewrite sends an
    operator looking for the wrong thing, and on a long branch the same mis-bounding
    is what would put the commits below the amend inside a fix pass."""
    r = _new_repo(tmp_path)
    r.commit("app.py", "a\n", "m1")
    r.git("checkout", "-q", "-b", "pr")
    r.commit("lib.py", "reviewed in round 1\n", "p1")
    r.commit("tip.py", "also reviewed\n", "p2")
    anchor = r.at("HEAD")
    r.write("tip.py", "reworked by the fix pass\n")
    r.git("add", "tip.py")
    r.git("commit", "-q", "--amend", "--no-edit")
    fork_point(r)

    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main",
                                            anchor, r.at("HEAD"))

    assert (got["prior"], got["carried"], got["unmatched"]) == (2, 1, 1)
    assert "changed content in the rewrite" in got["why"]
    assert "cannot be corresponded" not in got["why"]


def test_a_pass_at_the_TIP_is_read_as_its_net_change_not_as_its_working(
        tmp_path, fork_point):
    """Found by Codex on review, and it is the difference between measuring the fix
    pass and measuring the fixer's keystrokes.

    A pass of two commits where the second UNDOES part of the first: concatenating
    the two patches leaves the undone line in the added-line set, so a later finding
    that happens to sit at that number reads `introduced` — a defect blamed on a line
    the pass did not leave behind. Line numbers drift the same way, since each
    commit's patch is numbered in its own tree rather than the head's.

    Where the pass is the branch tip — which a rebase makes it, by replaying the
    reviewed commits first — one two-dot diff answers exactly and neither problem
    arises. That is now the ONLY shape that attributes; the fallback declines, which
    `test_a_pass_that_is_NOT_the_tail_of_the_branch_is_a_DECLINE` covers."""
    r = _new_repo(tmp_path)
    r.commit("app.py", "a\n", "m1")
    r.git("checkout", "-q", "-b", "pr")
    r.commit("lib.py", "reviewed\n", "p1")
    anchor = r.at("HEAD")
    r.commit("fix.py", "kept\nSCRATCHED LATER\n", "f1")
    r.commit("fix.py", "kept\n", "f2")
    r.git("checkout", "-q", "main")
    r.commit("main0.py", "moved on\n", "m2")
    r.git("checkout", "-q", "pr")
    r.git("rebase", "main")
    fork_point(r)

    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main",
                                            anchor, r.at("HEAD"))

    from panel_seats import _diff_added_lines
    # One line, not two. The concatenated shape reports `{1, 2}` here, and a finding
    # at fix.py:2 would then be attributed to a pass that deleted that line.
    assert _diff_added_lines(got["diff"]) == {"fix.py": {1}}


def test_a_pass_that_is_NOT_the_tail_of_the_branch_is_a_DECLINE(tmp_path,
                                                                fork_point):
    """The other refusal that keeps `introduced` a floor.

    Reading each leftover commit's own patch was the fallback here, and it is a
    superset twice over: a line the pass added and a later commit removed is still in
    the set, and each line's number comes from its own commit's tree rather than the
    head's. Both push `introduced` UP, into the brake — so where no single diff is
    the pass, there is no attribution.

    The history is built with `cherry-pick` rather than driven through `rebase -i`,
    because what is under test is the SHAPE — the fixer's commit sitting BELOW two
    the last round reviewed, which a reorder, an `--autosquash` or a hand-rebuilt
    branch all produce — and not git's rebase driver."""
    r = _new_repo(tmp_path)
    r.commit("app.py", "a\n", "m1")
    r.git("checkout", "-q", "-b", "pr")
    p1 = r.commit("early.py", "reviewed\n", "p1")
    anchor = r.commit("late.py", "reviewed too\n", "p2")
    f1 = r.commit("fix.py", "the fix\n", "f1")
    r.git("checkout", "-q", "main")
    r.commit("main0.py", "moved on\n", "m2")
    # The branch rebuilt onto the new base with the fix pass at the BOTTOM.
    r.git("checkout", "-q", "-B", "pr", "main")
    r.git("cherry-pick", f1, p1, anchor)
    fork_point(r)

    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main",
                                            anchor, r.at("HEAD"))

    # Every reviewed commit came through, so this is NOT the unmatched refusal — it
    # is the shape alone, which is what makes the test worth having separately.
    assert (got["prior"], got["carried"], got["unmatched"]) == (2, 2, 0)
    assert got["diff"] is None
    assert "not the tail of the rewritten branch" in got["why"]


def test_a_pass_that_RE_APPLIES_a_reviewed_patch_is_AMBIGUOUS_and_refused(
        tmp_path, fork_point):
    """Found by Codex, and the reason it is a refusal rather than a tie-break.

    A patch-id is not unique. Here the fix pass deletes a file the last round
    reviewed and puts it back byte for byte, so `f2` carries the SAME patch-id as the
    reviewed commit that created it, and the branch now carries two copies of a patch
    the last round had one of. One is the replayed commit and one is the fixer's own.

    Deciding it by position was the first shape of this — "a rebase replays the
    reviewed commits first, so the earlier claimant is the copy" — which holds for a
    plain rebase and not for an interactive one, a reorder, a cherry-pick or an
    arbitrary force-push. Getting it backwards puts an already-reviewed commit inside
    the pass and leaves the fixer's own out, on a signal that ends cycles. A coin
    toss is not available, so this declines."""
    r = _new_repo(tmp_path)
    r.commit("app.py", "a\n", "m1")
    r.git("checkout", "-q", "-b", "pr")
    r.commit("dup.py", "D\n", "p1")
    anchor = r.at("HEAD")
    r.git("rm", "-q", "dup.py")
    r.git("commit", "-q", "-m", "f1")
    r.commit("dup.py", "D\n", "f2")               # the same patch p1 carried
    r.commit("real.py", "the fix\n", "f3")
    r.git("checkout", "-q", "main")
    r.commit("main0.py", "moved on\n", "m2")
    r.git("checkout", "-q", "pr")
    r.git("rebase", "main")
    fork_point(r)

    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main",
                                            anchor, r.at("HEAD"))

    assert got["diff"] is None
    assert "appear more often on the rewritten branch" in got["why"]


# ------------------------------------------------------------------ it declines


def test_no_local_checkout_is_a_decline_and_not_a_guess(tmp_path):
    """`patch-id` is git rather than the compare API, so a repo with no `path` in
    its rules has nothing to reconstruct from. That is exactly today's behaviour —
    #509's veto, nothing attributed — and the `why` is what tells an operator the
    remedy is a config line rather than a rebase they should not have done."""
    got = panel_scope.reconstruct_fix_range("", GH_REPO, "main", "aaa111", "bbb222")
    assert got["diff"] is None
    assert "no local checkout" in got["why"]


def test_a_commit_this_box_never_HELD_is_a_decline(tmp_path, fork_point):
    """The reconstruction rests on #500's observation that the old SHAs still
    resolve — and they resolve where somebody still holds them. A panel running
    from a clone that never fetched the pre-rebase head has no object to
    patch-id, and inventing one is not available.

    Asserted on the ANCHOR specifically: it is the end a rewrite orphans, so it is
    the end that goes missing, and a check that only ever verified the head would
    pass every time and catch nothing."""
    r, _anchor, head, _ = _cycle(tmp_path, conflict=False)
    fork_point(r)
    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main",
                                            "0" * 40, head)
    assert got["diff"] is None
    assert "not in the checkout" in got["why"]


def test_a_fork_point_that_cannot_be_read_is_a_decline(tmp_path, fork_point):
    """The bound is not optional. Without a fork point there is nothing to stop the
    range running back to the root commit, and every commit the base branch ever
    made would read as the fix pass — a worse answer than the blindness this
    replaces, because it is a confident one."""
    r, anchor, head, _ = _cycle(tmp_path, conflict=False)
    fork_point(r, answer=None)
    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main", anchor, head)
    assert got["diff"] is None
    assert "fork point" in got["why"]


def test_a_fork_point_this_clone_does_not_have_is_a_decline(tmp_path, fork_point):
    """The forge answered and the clone cannot follow. Same refusal as an unreadable
    fork point and deliberately not a fallback to the local `merge-base`: the local
    answer is the stale one this asks the forge to avoid, so reaching for it here
    would make the failure path do the thing the success path refuses to."""
    r, anchor, head, _ = _cycle(tmp_path, conflict=False)
    fork_point(r, answer="0" * 40)
    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main", anchor, head)
    assert got["diff"] is None
    assert "fork point" in got["why"]


def test_a_rebase_with_NO_fix_pass_says_so_rather_than_inventing_an_empty_range(
        tmp_path, fork_point):
    """Somebody rebased and nobody fixed anything. Every commit on the branch is
    patch-equivalent to one the last round reviewed, so there is no pass here.

    It must not come back as a readable range with no added lines: that reading
    labels every new finding `missed` — "the earlier round looked at this and did
    not see it" — confidently, about a round whose range was never established.
    #500's own `no-fix`/`blind` split is the same argument one layer up."""
    r, anchor, head, _ = _cycle(tmp_path, conflict=False, fix=False)
    fork_point(r)
    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main", anchor, head)
    assert got["diff"] is None
    assert "no fix pass here" in got["why"]
    assert got["unmatched"] == 0


def test_a_branch_RESET_BACKWARDS_reconstructs_nothing(tmp_path, fork_point):
    """`behind` — the other rewrite, and the one that reaches the compare API
    looking like nothing at all. The head is an ANCESTOR of what the last round
    reviewed, so the fix pass has been removed from the branch rather than rewritten
    on it, and there is nothing to reconstruct.

    The failure to avoid is the opposite of a miss: every commit still on the branch
    matches one the last round saw, so a reconstruction that reported "no fix pass"
    as a clean answer would clear #509's veto on a round that is exactly as blind as
    a diverged one."""
    r, _, head, _ = _cycle(tmp_path, conflict=False)
    r.git("checkout", "-q", "pr")
    r.git("reset", "-q", "--hard", "HEAD~1")           # the fix pass, dropped
    fork_point(r)
    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main",
                                            head, r.at("HEAD"))
    assert got["diff"] is None
    # Not the same sentence as a rebase that carried no fix pass, and the difference
    # is the whole reason `unmatched` is counted: a force-push dropped work, and an
    # operator reading "there is no fix pass here" would take that for a quiet cycle.
    assert "REMOVED" in got["why"]


def test_a_rewrite_with_no_correspondence_at_all_REFUSES(tmp_path, fork_point):
    """A squash, a re-created branch, a rewrite this cannot see through: the prior
    round had commits and not one of them came out the other side.

    Then the two histories have not been corresponded, and everything on the branch
    is "unmatched" — which under the per-commit rule would attribute the entire PR
    to the fix pass. That is the exact catastrophe the `rewritten` verdict exists to
    prevent, so this refuses outright. Per-commit degradation is for a pass that is
    mostly recognisable; it is not a licence to attribute a branch nothing vouches
    for."""
    r, anchor, _head, _ = _cycle(tmp_path, conflict=False)
    r.git("checkout", "-q", "-B", "pr", "main")
    r.commit("everything.py", "squashed\n", "one squashed commit")
    fork_point(r)
    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main",
                                            anchor, r.at("HEAD"))
    assert got["diff"] is None
    assert "cannot be corresponded" in got["why"]
    assert (got["prior"], got["carried"]) == (2, 0)


def test_a_history_too_long_to_patch_is_a_decline(tmp_path, fork_point, monkeypatch):
    """The bound `_fix_range_diff` has on chars, this has on commits — and for its
    reason, which is that nothing gates on provenance so it may not cost a round
    an unbounded amount of local git. A branch carrying more commits than this
    either side of a rewrite is past the size any fix pass is."""
    monkeypatch.setattr(panel_scope, "RECONSTRUCT_MAX_COMMITS", 1)
    r, anchor, head, _ = _cycle(tmp_path, conflict=False)
    fork_point(r)
    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main", anchor, head)
    assert got["diff"] is None
    assert "either side of the rewrite" in got["why"]


def test_a_reconstruction_too_LARGE_to_hold_is_a_decline(tmp_path, fork_point,
                                                         monkeypatch):
    """The char bound, on the same terms as `_fix_range_diff`'s: not attributed,
    rather than held whole in memory.

    It bites in `_patch_ids`, which is the ONE place the whole branch's patches are
    held at once — and that is why the diff assembled afterwards carries no second
    bound: it is a subset of what has already been measured."""
    monkeypatch.setattr(panel_scope, "FIX_RANGE_MAX_CHARS", 10)
    r, anchor, head, _ = _cycle(tmp_path, conflict=False)
    fork_point(r)
    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main", anchor, head)
    assert got["diff"] is None
    assert "patch-ids could not be computed" in got["why"]


def test_an_anchor_that_is_not_a_REF_is_a_decline_that_says_so(tmp_path, fork_point):
    """A baseline can be any file the caller points at, so `head_sha` is not
    guaranteed to be a commit id — and a value starting `-` reads as an OPTION in
    argv. git declines it either way; the reason has to name the malformed value
    rather than send an operator looking for a commit that was never missing."""
    r, _anchor, head, _ = _cycle(tmp_path, conflict=False)
    fork_point(r)
    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main",
                                            "--upload-pack=evil", head)
    assert got["diff"] is None
    assert "is not a ref" in got["why"]


def test_a_range_with_only_one_end_is_a_decline(tmp_path):
    """Round 1, or a baseline written before `head_sha` existed. `_fix_range_diff`
    already answers `blind` for both and never reaches `rewritten`, so this is
    belt-and-braces on a helper that is public in `__all__` and callable from
    anywhere."""
    for anchor, head in (("aaa111", None), (None, "bbb222"), (None, None)):
        got = panel_scope.reconstruct_fix_range("/nonexistent", GH_REPO, "main",
                                                anchor, head)
        assert got["diff"] is None and "two ends" in got["why"]


# ----------------------------------------------------------------- the plumbing


def test_git_returns_None_for_every_way_a_call_can_fail(tmp_path):
    """One None for all of them, which is the contract the rest of the module leans
    on: a repair that cannot run leaves the round exactly as blind as it was, and a
    helper that raised on a missing checkout would take the round down instead."""
    assert panel_scope._git("", "rev-parse", "HEAD") is None
    assert panel_scope._git(str(tmp_path / "nope"), "rev-parse", "HEAD") is None
    r = _new_repo(tmp_path)
    assert panel_scope._git(str(r.path), "rev-parse", "--verify", "--quiet",
                            "refs/heads/nothing") is None
    r.commit("a.py", "1\n", "only")
    assert panel_scope._git(str(r.path), "rev-parse", "HEAD") is not None


def test_git_output_that_is_not_UTF_8_does_not_take_the_round_down(tmp_path,
                                                                   fork_point):
    """Found by Codex, and it is the difference between "never raises" being the
    contract and being the intention.

    `subprocess.run(text=True)` DECODES, and git output is not guaranteed UTF-8 — a
    source file in another encoding is ordinary in a long-lived repository and its
    bytes go straight into the diff. That decode raises `UnicodeDecodeError` out of
    `subprocess.run` itself, past an `except` naming only `OSError` and
    `SubprocessError`, and takes down a whole review round over an attribution
    nothing gates on.

    Asserted as a SUCCESS rather than as a clean decline, because `errors="replace"`
    is what the fix is: the added-line scan wants `diff --git`, `@@` and a leading
    `+`, none of which a replacement character disturbs, so the round keeps its
    attribution instead of losing it to an encoding."""
    r = _new_repo(tmp_path)
    r.commit("app.py", "a\n", "m1")
    r.git("checkout", "-q", "-b", "pr")
    r.commit("lib.py", "reviewed\n", "p1")
    anchor = r.at("HEAD")
    (r.path / "latin.py").write_bytes(b"# caf\xe9 latin-1, not utf-8\nvalue = 1\n")
    r.git("add", "latin.py")
    r.git("commit", "-q", "-m", "f1")
    r.git("checkout", "-q", "main")
    r.commit("main0.py", "moved on\n", "m2")
    r.git("checkout", "-q", "pr")
    r.git("rebase", "main")
    fork_point(r)

    got = panel_scope.reconstruct_fix_range(str(r.path), GH_REPO, "main",
                                            anchor, r.at("HEAD"))

    assert got["why"] is None
    assert r.subjects(*got["commits"]) == ["f1"]
    from panel_seats import _diff_added_lines
    assert _diff_added_lines(got["diff"]) == {"latin.py": {1, 2}}


def test_patch_id_output_this_cannot_READ_is_a_failure_not_a_partial_map(tmp_path,
                                                                        monkeypatch):
    """Found by Codex. A line `_patch_ids` cannot parse is a HOLE, and a hole is not
    the same as a commit that had no patch.

    A commit missing from the PRIOR map reserves nothing, so its counterpart on the
    rewritten branch reads as the fix pass — over-attribution bought by a parse that
    was silently skipped. `patch-id` does not emit such a line today; what this pins
    is that if it ever did, the caller declines rather than corresponding two
    histories from a map it knows is incomplete."""
    r = _new_repo(tmp_path)
    first = r.commit("a.py", "1\n", "only")
    real = panel_scope._git

    def one_bad_line(repo_path, *args, **kw):
        if args and args[0] == "patch-id":
            return "deadbeef " + first + "\nthis line has three fields\n"
        return real(repo_path, *args, **kw)

    monkeypatch.setattr(panel_scope, "_git", one_bad_line)
    assert panel_scope._patch_ids(str(r.path), [first]) is None


def test_patch_ids_tells_an_empty_answer_apart_from_a_failed_one(tmp_path):
    """`{}` and None are opposite instructions to the caller — "these commits
    changed nothing, carry on" against "this cannot be corresponded, stop" — and a
    helper that returned `{}` for a failure would hand the caller a fix set of every
    commit on the branch, computed against patch-ids that were never read."""
    r = _new_repo(tmp_path)
    first = r.commit("a.py", "1\n", "first")
    assert panel_scope._patch_ids(str(r.path), []) == {}
    assert panel_scope._patch_ids(str(tmp_path / "nope"), [first]) is None
    assert list(panel_scope._patch_ids(str(r.path), [first])) == [first]


def test_an_EMPTY_commit_has_no_patch_id_and_is_not_matched(tmp_path):
    """git emits no patch for a commit that changed nothing, so `patch-id` has
    nothing to hash and the commit simply does not appear. That is correct — a
    commit that added no line cannot have introduced a defect on one — and the
    caller reads a missing id as "not attributable" rather than as "matched", which
    is why it is asserted here rather than left to be discovered."""
    r = _new_repo(tmp_path)
    real = r.commit("a.py", "1\n", "real")
    r.git("commit", "-q", "--allow-empty", "-m", "empty")
    empty = r.at("HEAD")
    got = panel_scope._patch_ids(str(r.path), [real, empty])
    assert list(got) == [real]


# ------------------------------------------------- the round that consumes it

# `_panel_round` is the whole-round harness #500's and #512's tests are written
# against, and these belong beside them in what they assert: a payload, a veto and
# a note. It is imported rather than copied — a second copy is a second thing to
# teach about every `gh` call panel.py grows, which is the failure `conftest.gh_stub`
# exists to have stopped.
from test_panel_provenance import CFG, _compare, _panel_round  # noqa: E402


def _rebased_round(tmp_path, monkeypatch, path=None, *, conflict=False,
                   finding=("fix.py", 1, "the fix pass left a dangling handle")):
    """Round 1 at the pre-rebase head, round 2 at the post-rebase one, with the
    compare API saying `diverged` — which is what it says, and what #500 measured."""
    r, anchor, head, _ = _cycle(tmp_path, conflict=conflict)
    real = r.git("merge-base", "main", "pr").strip()
    monkeypatch.setattr(panel_scope, "_merge_base_now", lambda *a, **k: real)
    cfg = {**CFG, "path": str(r.path) if path is None else path}
    r1_path, _ = _panel_round(monkeypatch, tmp_path, 1,
                              [("app.py", 2, "a stale mirror")], head=anchor, cfg=cfg)
    _, r2 = _panel_round(monkeypatch, tmp_path, 2, [finding], head=head,
                         baseline=[r1_path], cfg=cfg,
                         compare=_compare(status="diverged"))
    return r, r2


def test_a_REBASED_round_ATTRIBUTES_from_the_reconstructed_pass(tmp_path,
                                                                monkeypatch):
    """#500's measured case, with the instruments back on.

    `main` moves, somebody rebases between rounds — ordinary and correct — and the
    compare API answers `diverged`. Before this the round attributed nothing, took
    #509's veto, and `escalate_on.fix_injection` could not fire whatever the fix
    pass had done. The pass is still on the branch; the finding sits on a line it
    wrote; and `introduced` is now what says so."""
    _, r2 = _rebased_round(tmp_path, monkeypatch)

    assert r2["provenance_counts"]["introduced"] == 1
    assert r2["provenance_counts"]["unknown"] == 0
    assert r2["fix_range_source"] == "reconstructed"
    # And no veto, on #512's rule: what it is about is whether the round
    # ATTRIBUTED, not which reader answered. Vetoing a round that had just repaired
    # itself is the alert fatigue the veto was written to avoid.
    assert not any("#500" in v for v in r2["round_stop"]["veto"])
    assert any("RECONSTRUCTED" in n for n in r2["config_notes"])


def test_the_reconstruction_publishes_its_own_working(tmp_path, monkeypatch):
    """`fix_range_source` says a reconstruction happened; this says what it was.

    A reader comparing `introduced` across a cycle is comparing a reconstructed
    round's rate against linear rounds', and the denominator's provenance changed
    under them — so the commits it named and the prior round's it corresponded have
    to be in the payload rather than only in a sentence."""
    r, r2 = _rebased_round(tmp_path, monkeypatch)
    # The BEHAVIOUR first and the new key second, on this suite's standing rule:
    # against the pre-#504 code this must go red on the attribution, not on a payload
    # key that does not exist yet. A test that fails with a `KeyError` has
    # demonstrated nothing about the defect it is named for.
    assert r2["fix_range_source"] == "reconstructed"
    got = r2["fix_range_rebuilt"]
    assert r.subjects(*got["commits"]) == ["f1"]
    assert (got["prior"], got["carried"], got["unmatched"]) == (2, 2, 0)
    assert got["why"] is None
    # And NOT the patch itself. This payload is written to a file, handed to the next
    # round as `--baseline` and recorded on the board; the reconstruction's diff can
    # run to `FIX_RANGE_MAX_CHARS` and nothing downstream reads it (found by Codex).
    assert "diff" not in got


def test_a_CONFLICTED_rebase_is_reported_and_still_vetoes(tmp_path, monkeypatch):
    """The refusal, carried all the way to what a human reads off the PR comment.

    A conflict resolved during the rebase moved a reviewed commit's content, so the
    reconstruction declines and the round is exactly as blind as #500 found it —
    #509's veto fires, every finding is `unknown`. What it must NOT do is attribute
    from a set it knows is a superset and leave the correction to a note:
    `escalate_on.fix_injection` ends cycles and reads no notes.

    The note is still worth asserting, because "the branch was rewritten" on its own
    now reads as "so it was rebuilt", and an operator needs to know a conflicted
    rebase is the thing that stopped it."""
    _, r2 = _rebased_round(tmp_path, monkeypatch, conflict=True)

    assert r2["fix_range_source"] is None
    assert r2["provenance_counts"]["introduced"] == 0
    assert r2["provenance_counts"]["unknown"] == 1
    assert any("#500" in v for v in r2["round_stop"]["veto"])
    assert r2["fix_range_rebuilt"]["unmatched"] == 1
    assert any("could not be reconstructed" in n and "changed content in the rewrite" in n
               for n in r2["config_notes"])


def test_a_rewrite_the_reconstruction_CANNOT_repair_still_vetoes(tmp_path,
                                                                 monkeypatch):
    """The other half, so arming the instruments cannot quietly switch #509 off.

    No local checkout, so there is nothing to patch-id and the round is exactly as
    blind as it was before #504. It must still take the veto — and it must also say
    which of the repair's refusals it hit, because `no_range_why` alone now reads as
    "the branch was rewritten", which a reader will take for "so it was rebuilt"."""
    _, r2 = _rebased_round(tmp_path, monkeypatch, path="")

    # This one GUARDS behaviour rather than regressing a defect — every assertion but
    # the last two held before #504 and has to go on holding — so it is the one test
    # here with no red to demonstrate. Preserved behaviour first, the new reporting
    # after it, so a failure says which of the two moved.
    assert r2["fix_range_source"] is None
    assert r2["provenance_counts"]["unknown"] == 1
    assert any("#500" in v for v in r2["round_stop"]["veto"])
    assert r2["fix_range_rebuilt"]["why"]
    assert any("could not be reconstructed" in n and "no local checkout" in n
               for n in r2["config_notes"])
