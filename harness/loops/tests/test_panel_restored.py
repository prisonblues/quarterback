"""#559 — a line RESTORED is not a line written, and a line diff cannot tell.

`_provenance` attributes an added line by POSITION: it lies between the last
round's commit and this one, so the fix pass wrote it. A revert-of-a-revert
satisfies that and means the opposite — lexray `343d1f15` brought back ~90 lines
that rounds 1 and 2 had already reviewed, and every one of them was attributed to
the pass that restored them. `escalate_on.fix_injection` ends a cycle on that
number, so the pass repairing a bad revert inflates the statistic that produced
the stop it was answering.

What is under test is therefore a pair, and neither half is worth much alone:

- a restored RUN is taken out of the attribution, so the round stops blaming the
  fixer for code the cycle had already seen; and
- everything else stays in it. A four-line run, a scattered `}` or a blank line, a
  copy of a block the branch never lost, and lines the pass genuinely wrote in
  among the restored ones are all still `introduced`. A filter that excluded those
  would turn `fix_injection` into a brake that cannot fire, which is a worse
  failure than the miscount it replaces.

Restoration is a ROUND TRIP and both ends are checked: the content was on the
branch at an earlier round's head, and it is NOT at the commit the fix range
starts from. Half of that test is not a weaker version of it — it is the false
positive.

The repository is real, for `test_panel_reconstruct`'s reason: the claim is about
what a file's content was at an earlier commit, and a double that answers
`git show` with a canned string asserts that claim rather than checking it. The
builder there is imported rather than copied.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import panel  # noqa: E402
import panel_scope  # noqa: E402
import panel_seats  # noqa: E402

from test_panel_reconstruct import _new_repo  # noqa: E402
from test_panel_provenance import CFG, _compare, _panel_round, _round  # noqa: E402


#: The block a fix pass reverts and then puts back. Five lines, which is exactly
#: :data:`panel_scope.RESTORED_RUN_MIN` — so a test that drops one of them is
#: testing the floor rather than the happy path, which is what
#: `test_a_run_one_line_SHORT_of_the_floor_stays_attributed` does deliberately.
BLOCK = ["def mirror(paths):",
         "    out = {}",
         "    for p in paths:",
         "        out[p] = read(p)",
         "    return out"]

#: `app/sync.py` as ROUND 1 reviewed it: the block, with a header and a tail.
V1 = "\n".join(["import os", "", *BLOCK, "", "TAIL = 1"]) + "\n"

#: The same file after a revert took the block out — what round 2 reviewed, and
#: the base of the fix range round 3 attributes against.
V2 = "\n".join(["import os", "", "TAIL = 1"]) + "\n"

#: And after the revert was itself reverted, with one line the fixer genuinely
#: wrote sitting among the ones it brought back. `handle` is in no earlier
#: version, so it is the control: a filter that swallowed it would have excluded
#: the fix pass's own work along with the restoration.
V3 = "\n".join(["import os", "", *BLOCK, "", 'handle = open("x")', "TAIL = 1"]) + "\n"

#: What the compare API answers for `r2..r3`: seven added lines at 3-9 of the new
#: side. Lines 3-8 are the block and the blank after it; line 9 is `handle`, which
#: nothing brought back.
RESTORE_PATCH = ("@@ -2,2 +2,9 @@\n"
                 " \n"
                 + "".join(f"+{ln}\n" for ln in BLOCK)
                 + "+\n"
                 + '+handle = open("x")\n'
                 + " TAIL = 1")

#: What the filter should take out of the attribution on that patch: the block and
#: the blank the second window covers, and never `handle` at 9.
RESTORED = {"app/sync.py": {3, 4, 5, 6, 7, 8}}


def _diff(path: str, patch: str) -> str:
    """One file's compare patch as `_diff_added_text` wants to read it."""
    return f"diff --git a/{path} b/{path}\n{patch}\n"


def _added(patch: str, path: str = "app/sync.py") -> dict:
    return panel_seats._diff_added_text(_diff(path, patch))


def _repo_at(tmp_path, *versions: str):
    """A repo whose `app/sync.py` took each version in turn. Returns the commits."""
    r = _new_repo(tmp_path)
    (r.path / "app").mkdir()
    shas = []
    for i, body in enumerate(versions):
        r.write("app/sync.py", body)
        r.git("add", "app/sync.py")
        r.git("commit", "-q", "-m", f"v{i + 1}")
        shas.append(r.at("HEAD"))
    return r, shas


# --------------------------------------------------------------------------
# The diff walk that now keeps the line's text
# --------------------------------------------------------------------------


def test_the_two_walks_cannot_disagree_about_which_lines_were_added():
    """`_diff_added_lines` is `_diff_added_text` with the content dropped, and it
    has to stay that way. Two parsers over one diff format is two places to get the
    `+++`-is-content ambiguity right, and they would drift — the numbers would then
    say a line was added and the texts would not have it, which is a restoration
    filter reading a line the attribution never saw."""
    diff = _diff("app/sync.py", RESTORE_PATCH)
    text = panel_seats._diff_added_text(diff)
    assert panel_seats._diff_added_lines(diff) == {f: set(v) for f, v in text.items()}
    assert text == {"app/sync.py": {3: BLOCK[0], 4: BLOCK[1], 5: BLOCK[2],
                                    6: BLOCK[3], 7: BLOCK[4], 8: "",
                                    9: 'handle = open("x")'}}


def test_the_plus_sign_is_stripped_and_nothing_else_is():
    """Bytes against bytes. A strip, a case fold or a whitespace normalisation
    would widen the comparison into "looks a bit like", which is the difference
    between excluding restorations and excluding any line that resembles one — and
    indentation is most of what a code line is."""
    diff = _diff("a.py", "@@ -0,0 +1,2 @@\n+    indented\t\n+")
    assert panel_seats._diff_added_text(diff) == {"a.py": {1: "    indented\t", 2: ""}}


def test_a_FORM_FEED_inside_a_line_does_not_shift_every_number_after_it():
    """Found by Codex. `splitlines()` breaks on `\\x0c`, which a diff does not: the
    record separator is the newline and nothing else. Under the old walk a source
    line carrying a form feed counted as two added lines and moved every line
    number after it, which places findings against the wrong lines."""
    diff = _diff("a.py", "@@ -0,0 +1,2 @@\n+head\x0cpage\n+after\n")
    assert panel_seats._diff_added_text(diff) == {"a.py": {1: "head\x0cpage",
                                                          2: "after"}}


def test_runs_are_maximal_and_in_order():
    assert panel_scope._runs([3, 1, 2, 9, 10]) == [[1, 2, 3], [9, 10]]
    assert panel_scope._runs([]) == []


# --------------------------------------------------------------------------
# What counts as a restoration
# --------------------------------------------------------------------------


def test_a_block_the_branch_LOST_and_got_back_is_not_the_fix_passs_own_work(
        tmp_path):
    """The motivating case, at the grain the filter works on. The block is
    byte-identical to what round 1 reviewed and is gone at the anchor, so the lines
    carrying it come back as restored — and `handle`, which no earlier commit ever
    had, does not."""
    r, (v1, v2, _v3) = _repo_at(tmp_path, V1, V2, V3)

    got = panel_scope.restored_lines(str(r.path), _added(RESTORE_PATCH),
                                     {1: v1}, (2, v2))

    assert got["why"] is None and got["lines"] == RESTORED
    assert got["count"] == 6 and got["files"] == 1
    assert got["rounds"] == [1] and got["unread"] == []


def test_a_block_the_branch_NEVER_LOST_is_a_copy_and_stays_the_fixers_own(
        tmp_path):
    """Found by Codex, second pass, and the reason the anchor is READ rather than
    merely left out of the earlier heads.

    Here the block sits in round 1's version AND in the anchor's, and the fixer
    pastes a second copy of it. Matching the older head alone calls that a
    restoration and takes a fresh copy-paste out of the count — and a copy-paste
    defect is one of the commoner things a fix pass introduces. Nothing was
    restored, because nothing had gone."""
    r, (v1, v2) = _repo_at(tmp_path, V1, V1 + "EXTRA = 1\n")
    paste = "@@ -8,0 +9,5 @@\n" + "".join(f"+{ln}\n" for ln in BLOCK)

    got = panel_scope.restored_lines(str(r.path), _added(paste), {1: v1}, (2, v2))

    assert got["why"] is None and got["lines"] == {}


def test_a_run_one_line_SHORT_of_the_floor_stays_attributed(tmp_path):
    """The floor, from below. Four consecutive restored lines are left with the
    fixer, and that is the direction this errs in on purpose: where the filter is
    unsure it leaves the line attributed, so `introduced` stays the FLOOR the
    threshold at 0.5 is argued from."""
    r, (v1, v2) = _repo_at(tmp_path, V1, V2)
    four = ("@@ -2,2 +2,6 @@\n \n"
            + "".join(f"+{ln}\n" for ln in BLOCK[:4]) + " TAIL = 1")
    assert len(_added(four)["app/sync.py"]) == 4

    assert panel_scope.restored_lines(str(r.path), _added(four),
                                      {1: v1}, (2, v2))["lines"] == {}


def test_lines_that_merely_RECUR_in_the_file_are_not_a_restoration(tmp_path):
    """The reason this is a run and not a line, and the whole argument against
    #559's own per-line proposal.

    Every line here is byte-identical to a line the earlier version already had — a
    blank, a `    return None`, a closing bracket — and none of them was restored
    from anywhere. A per-line filter excludes the lot, on a file nobody ever
    reverted, and does that to some share of every fix pass ever written. Five of
    them in a row that the earlier file also had in a row is a different claim, and
    it is false here."""
    earlier = "\n".join(["def a():", "    return None", "", ")", "",
                         "def b():", "    x = 1", "    return None"]) + "\n"
    r, (v1, v2) = _repo_at(tmp_path, earlier, earlier + "TRAILER = 1\n")
    scattered = ("@@ -8,0 +9,5 @@\n"
                 "+\n"
                 "+def c():\n"
                 "+    return None\n"
                 "+\n"
                 "+)\n")
    assert len(_added(scattered)["app/sync.py"]) == 5

    assert panel_scope.restored_lines(str(r.path), _added(scattered),
                                      {1: v1}, (2, v2))["lines"] == {}


def test_a_file_the_earlier_round_did_not_have_restores_nothing(tmp_path):
    """A path that commit never carried is absent, not unreadable: a file the cycle
    created afterwards has no restored lines by construction. It must not be an
    error, must not be banked as a hole, and must not stop the other files being
    read."""
    r, (v1, v2) = _repo_at(tmp_path, V1, V2)

    got = panel_scope.restored_lines(str(r.path), _added(RESTORE_PATCH, "app/new.py"),
                                     {1: v1}, (2, v2))
    assert got["why"] is None and got["lines"] == {} and got["unread"] == []


def test_the_LINE_ENDING_rule_is_stated_rather_than_inherited(tmp_path):
    """Raised by Codex, and settled the other way with the reason written down.

    A trailing `\\r` is stripped from both sides, so a CRLF-to-LF conversion inside
    a fix pass reads as restoration. That is the intended answer: the CONTENT is
    what an earlier round reviewed and the fixer authored none of it, while a
    defect the pass really did introduce changed something other than a terminator
    and matches nothing. What settles it is the alternative — `_git` runs
    `subprocess.run(text=True)`, whose universal-newline translation collapses
    `\\r\\n` on the way out of `git show` while the compare API's patch keeps it, so
    an implicit rule would decide a brake by which side a carriage return happened
    to survive on. All four spellings answer the same here, and that is the
    point."""
    r, (crlf, lf, gone) = _repo_at(tmp_path, V1.replace("\n", "\r\n"), V1, V2)
    lf_added = _added(RESTORE_PATCH)
    crlf_added = _added(RESTORE_PATCH.replace("\n", "\r\n"))

    for version in (crlf, lf):
        for added in (lf_added, crlf_added):
            got = panel_scope.restored_lines(str(r.path), added, {1: version},
                                             (3, gone))
            assert got["lines"] == RESTORED, (version, added)


def test_a_trailing_SPACE_is_content_and_is_not_normalised_away(tmp_path):
    """The carriage return is the only thing taken off. Trailing whitespace inside
    a line is content the earlier round either had or did not, and an `rstrip` here
    would widen a byte comparison into a resemblance. The altered line breaks every
    window that covers it, and here that is all of them."""
    r, (v1, v2) = _repo_at(tmp_path, V1, V2)
    spaced = RESTORE_PATCH.replace(BLOCK[2], BLOCK[2] + " ")

    assert panel_scope.restored_lines(str(r.path), _added(spaced),
                                      {1: v1}, (2, v2))["lines"] == {}


def test_a_window_of_nothing_DISTINCTIVE_is_refused_rather_than_searched(
        tmp_path, monkeypatch):
    """The bound that keeps the matching linear (found by Codex, second pass).

    A five-line window whose rarest line occurs more often than
    `RESTORED_MAX_REPEATS` in the same file is not searched: nothing in it is
    distinctive, which is what generated, tabular or minified content looks like,
    and calling five such lines a restoration is a guess. Refusing leaves them
    attributed — this filter's standing direction — and it is what stops one large
    repetitive file turning a round into a quadratic."""
    r, (v1, v2) = _repo_at(tmp_path, V1, V2)
    monkeypatch.setattr(panel_scope, "RESTORED_MAX_REPEATS", 0)

    assert panel_scope.restored_lines(str(r.path), _added(RESTORE_PATCH),
                                      {1: v1}, (2, v2))["lines"] == {}


def test_a_repeated_block_is_still_found_at_its_SECOND_position(tmp_path):
    """The index anchors on the window's rarest line and then checks the slice, so
    a block that appears twice in the earlier version is matched at either of them
    — the offset arithmetic has to be right in both directions, not only where the
    anchor happens to be the window's first line."""
    twice = "\n".join(["import os", "", *BLOCK, "", *BLOCK, "", "TAIL = 1"]) + "\n"
    r, (v1, v2) = _repo_at(tmp_path, twice, V2)

    got = panel_scope.restored_lines(str(r.path), _added(RESTORE_PATCH),
                                     {1: v1}, (2, v2))
    assert got["lines"] == RESTORED


# --------------------------------------------------------------------------
# Every way the filter declines — and it says which
# --------------------------------------------------------------------------


def test_no_earlier_head_is_VACUOUS_and_takes_no_note(tmp_path):
    """Round 2's only prior round IS the anchor, so there is no older commit a line
    could have been restored from. Nothing was missed — #500's distinction between
    an instrument that is vacuous and one that is blind — and a `why` here would
    fire on every cycle's second round to report that nothing happened."""
    r, (v1,) = _repo_at(tmp_path, V1)
    got = panel_scope.restored_lines(str(r.path), _added(RESTORE_PATCH), {}, (1, v1))
    assert got["why"] is None and got["count"] == 0 and got["rounds"] == []


def test_no_local_checkout_is_a_reason_and_not_a_silent_zero():
    """Reading a file as an earlier round saw it is git, and a panel run from
    outside a checkout has none. That is a measurement that could not be made, and
    it has to say so — a silent `{}` is indistinguishable from a round that looked
    and found no restoration, and the two lean opposite ways."""
    got = panel_scope.restored_lines("", _added(RESTORE_PATCH), {1: "a" * 40},
                                     (2, "b" * 40))
    assert got["lines"] == {} and "no local checkout" in got["why"]


def test_no_ANCHOR_is_a_reason_too(tmp_path):
    """Half a round trip is not a weaker test, it is the false positive. Without
    the commit the range starts from, a block the branch still carries cannot be
    told from one the fix pass brought back, so nothing is filtered and the round
    is told why. `--upload-pack=…` is refused in the same branch: this value
    reaches `git ls-tree` in argv, and a leading `-` reads as an option."""
    r, (v1,) = _repo_at(tmp_path, V1)
    added = _added(RESTORE_PATCH)
    for anchor in (None, (2, "--upload-pack=touch")):
        got = panel_scope.restored_lines(str(r.path), added, {1: v1}, anchor)
        assert got["lines"] == {} and "fix range starts from" in got["why"]


def test_an_earlier_head_this_box_never_HELD_is_named_and_not_passed_over(tmp_path):
    """One of two earlier heads is missing, so the filter ran on half the cycle.

    It does not decline — declining would put the round back on the unfiltered
    count in the case the filter is most needed, since a rewrite orphaning one head
    is exactly when a cycle has been reverting things — and the number it gives is
    still between the unfiltered one and the exact one. What it must not do is
    report that number as if the whole cycle had been read."""
    r, (v1, v2, _v3) = _repo_at(tmp_path, V1, V2, V3)

    got = panel_scope.restored_lines(str(r.path), _added(RESTORE_PATCH),
                                     {1: v1, 2: "c" * 40}, (3, v2))

    assert got["why"] is None and got["count"] == 6
    assert got["rounds"] == [1] and got["unread"] == [2]


def test_NO_earlier_head_in_the_checkout_declines_outright(tmp_path):
    """With none of them readable there is no comparison left to make, and that is
    a `why` rather than a quiet zero."""
    r, (v1, v2) = _repo_at(tmp_path, V1, V2)
    got = panel_scope.restored_lines(str(r.path), _added(RESTORE_PATCH),
                                     {1: "b" * 40}, (2, v2))
    assert got["lines"] == {} and "is in the checkout" in got["why"]
    assert got["unread"] == [1]


def test_a_file_it_could_not_READ_is_told_apart_from_one_that_was_not_THERE(
        tmp_path, monkeypatch):
    """`git show` answers None for both, and they are not the same news. A file
    that commit never carried has no restored lines by construction; a file it did
    carry and this could not hold is a version the comparison never saw, and the
    count is lower than it should be by however much was in it. `git ls-tree`
    against the paths in hand is what separates them."""
    r, (v1, v2) = _repo_at(tmp_path, V1, V2)
    # A ceiling that admits the anchor's copy and refuses round 1's.
    monkeypatch.setattr(panel_scope, "FIX_RANGE_MAX_CHARS", len(V2) + 1)

    got = panel_scope.restored_lines(str(r.path), _added(RESTORE_PATCH),
                                     {1: v1}, (2, v2))
    assert got["why"] is None and got["count"] == 0 and got["unread"] == [1]


def test_an_ANCHOR_it_could_not_read_takes_that_file_out_of_the_filter(
        tmp_path, monkeypatch):
    """The opposite of the case above, and it goes the other way. Without the
    anchor's copy the round trip cannot be established for that file, and matching
    the older head alone is the copy-paste false positive — so the file is dropped
    rather than filtered on half the evidence, and with nothing else in hand the
    round says so."""
    r, (v1, v2) = _repo_at(tmp_path, V1, V2)
    monkeypatch.setattr(panel_scope, "FIX_RANGE_MAX_CHARS", len(V2) - 1)

    got = panel_scope.restored_lines(str(r.path), _added(RESTORE_PATCH),
                                     {1: v1}, (2, v2))
    assert got["lines"] == {} and "still carried there" in got["why"]
    assert got["unread"] == [2]


def test_more_file_versions_than_it_will_read_DECLINES_rather_than_leaning(
        tmp_path, monkeypatch):
    """A filter applied to some of a round's files and not the others is a number
    nobody can correct for. Refused whole, which is `reconstruct_fix_range`'s rule
    for every inexact shape and the same reason: a lean under a brake is worse than
    a decline the round can state."""
    r, (v1, v2) = _repo_at(tmp_path, V1, V2)
    monkeypatch.setattr(panel_scope, "RESTORED_MAX_READS", 0)

    got = panel_scope.restored_lines(str(r.path), _added(RESTORE_PATCH),
                                     {1: v1}, (2, v2))
    assert got["lines"] == {} and "file-versions this will read" in got["why"]


def test_a_pass_with_no_long_enough_run_reads_nothing_at_all(tmp_path, monkeypatch):
    """The bound above is affordable because a file that cannot produce a match is
    never read. Pinned, because the pruning is also what stops a wide fix pass with
    one restorable hunk from tripping the refusal."""
    r, (v1, v2) = _repo_at(tmp_path, V1, V2)
    calls = []
    real = panel_scope._git
    monkeypatch.setattr(panel_scope, "_git",
                        lambda p, *a, **k: (calls.append(a), real(p, *a, **k))[1])

    got = panel_scope.restored_lines(str(r.path), _added("@@ -2,0 +3,1 @@\n+one line\n"),
                                     {1: v1}, (2, v2))
    assert got["lines"] == {}
    assert not any(a and a[0] in ("show", "ls-tree") for a in calls)


# --------------------------------------------------------------------------
# The baseline carries every round's commit, not only the anchor's
# --------------------------------------------------------------------------

THIS_RUN = {"repo": "acme", "github": "acme/board", "pr": 34, "round": 3}


def test_the_baseline_banks_EVERY_rounds_head_and_still_anchors_on_the_latest(
        tmp_path):
    """`head_sha` answers "where does the fix range start", which has one right
    answer. `head_shas` answers a different question — had this cycle already seen
    these lines — and for that the earlier rounds are the whole point."""
    paths = [_round(tmp_path, "r1.json", 1, head_sha="a" * 8),
             _round(tmp_path, "r2.json", 2, head_sha="b" * 8)]
    b = panel.load_baseline(paths, THIS_RUN)
    assert b.head_sha == "b" * 8 and b.head_round == 2
    assert b.head_shas == {1: "a" * 8, 2: "b" * 8}


def test_a_head_that_is_not_a_commit_id_is_banked_by_neither(tmp_path):
    """The validation is the anchor's, and it has to cover this too: an unvalidated
    sha here would reach `git ls-tree` rather than a compare URL, which is a
    different command and the same class of mistake."""
    paths = [_round(tmp_path, "r1.json", 1, head_sha="../../etc"),
             _round(tmp_path, "r2.json", 2, head_sha="b" * 8)]
    b = panel.load_baseline(paths, THIS_RUN)
    assert b.head_shas == {2: "b" * 8}


# --------------------------------------------------------------------------
# The round that consumes it
# --------------------------------------------------------------------------


def _cycle_round_three(monkeypatch, tmp_path, repo, findings, *, patch, head,
                       path=None):
    """Rounds 1 and 2 banked, then round 3 attributing across `patch`."""
    cfg = {**CFG, "path": str(repo.path) if path is None else path}
    r1, _ = _panel_round(monkeypatch, tmp_path, 1,
                         [("app/sync.py", 1, "round one had its own complaint")],
                         head=repo.at("HEAD~2"), cfg=cfg)
    r2, _ = _panel_round(monkeypatch, tmp_path, 2,
                         [("app/sync.py", 3, "round two had another")],
                         head=repo.at("HEAD~1"), baseline=[r1], cfg=cfg)
    _, r3 = _panel_round(monkeypatch, tmp_path, 3, findings, head=head,
                         baseline=[r1, r2], cfg=cfg, max_rounds=6,
                         compare=_compare(files=(("app/sync.py", patch),)))
    return r3


def test_a_revert_of_a_revert_is_not_the_fix_pass_writing_the_code(
        monkeypatch, tmp_path):
    """#559 end to end, and both halves of it in one round.

    Round 2's fix reverted the block; round 3's put it back and wrote one line of
    its own. A finding on a restored line must NOT be `introduced` — those lines
    are what rounds 1 and 2 already reviewed, and calling them the fixer's work is
    what let a correct repair trip `escalate_on.fix_injection`. A finding on the
    line the pass genuinely wrote must still be `introduced`, or the filter has
    disarmed the brake rather than corrected it."""
    repo, (_v1, _v2, v3) = _repo_at(tmp_path, V1, V2, V3)
    r3 = _cycle_round_three(
        monkeypatch, tmp_path, repo,
        [("app/sync.py", 4, "the restored block leaks a handle"),
         ("app/sync.py", 9, "this line is the fix pass's own")],
        patch=RESTORE_PATCH, head=v3)

    got = {f["synthesis"]: f["provenance"] for f in r3["to_fix"]}
    assert got["the restored block leaks a handle"] == "missed"
    assert got["this line is the fix pass's own"] == "introduced"
    assert r3["provenance_counts"]["introduced"] == 1
    assert r3["provenance_restored"] == {"count": 6, "files": 1, "rounds": [1],
                                         "unread": [], "why": None}
    assert any("RESTORED line(s)" in n and "#559" in n for n in r3["config_notes"])


def test_a_copy_of_what_the_branch_STILL_HELD_is_the_fixers_own_work(
        monkeypatch, tmp_path):
    """The boundary from the other side, through a whole round.

    The block is in round 1's version and still at the anchor, and the fixer pastes
    a second copy of it. Nothing was restored, because nothing had gone — and a
    copy-paste defect must stay `introduced`."""
    repo, (_v1, _v2, v3) = _repo_at(tmp_path, V1, V1 + "MID = 1\n",
                                    V1 + "MID = 1\n" + "\n".join(BLOCK) + "\n")
    paste = "@@ -9,0 +10,5 @@\n" + "".join(f"+{ln}\n" for ln in BLOCK)
    r3 = _cycle_round_three(monkeypatch, tmp_path, repo,
                            [("app/sync.py", 11, "the pasted copy is stale")],
                            patch=paste, head=v3)

    assert r3["to_fix"][0]["provenance"] == "introduced"
    assert r3["provenance_restored"]["count"] == 0


def test_a_round_that_could_not_check_says_so_rather_than_leaning_quietly(
        monkeypatch, tmp_path):
    """No checkout, so restored code and written code cannot be told apart. The
    round still attributes — nothing gates on this filter and a round must not
    stall on it — but `introduced` leans HIGH, and a reader comparing rounds has to
    be able to see which of them was filtered. That is #500's rule for the fix range
    arriving one instrument later."""
    repo, (_v1, _v2, v3) = _repo_at(tmp_path, V1, V2, V3)
    r3 = _cycle_round_three(
        monkeypatch, tmp_path, repo,
        [("app/sync.py", 4, "the restored block leaks a handle")],
        patch=RESTORE_PATCH, head=v3, path="/nonexistent/acme-e2e")

    assert r3["to_fix"][0]["provenance"] == "introduced"
    assert r3["provenance_restored"]["why"] and r3["provenance_restored"]["count"] == 0
    assert any("could not tell RESTORED code" in n for n in r3["config_notes"])


def test_round_two_neither_filters_nor_complains(monkeypatch, tmp_path):
    """A cycle's second round has exactly one earlier round and it is the anchor,
    so there is nothing older to compare against. `null` says the question did not
    arise, which a consumer must be able to tell from a round that looked and found
    nothing — and no note, or every cycle would carry one."""
    repo, (_v1, v2) = _repo_at(tmp_path, V2, V1)
    cfg = {**CFG, "path": str(repo.path)}
    r1, _ = _panel_round(monkeypatch, tmp_path, 1,
                         [("app/sync.py", 1, "round one had its own complaint")],
                         head=repo.at("HEAD~1"), cfg=cfg)
    _, r2 = _panel_round(monkeypatch, tmp_path, 2,
                         [("app/sync.py", 3, "a fresh defect")], head=v2,
                         baseline=[r1], cfg=cfg,
                         compare=_compare(files=(("app/sync.py",
                                                  "@@ -2,0 +3,5 @@\n"
                                                  + "".join(f"+{ln}\n"
                                                            for ln in BLOCK)),)))

    assert r2["provenance_restored"] is None
    assert not any("#559" in n for n in r2["config_notes"])


def test_round_one_records_no_filter_at_all(monkeypatch, tmp_path):
    """Nothing to attribute, so nothing to filter out of the attribution — the same
    `null` `fix_range_source` and `provenance_counts` already send."""
    _, r1 = _panel_round(monkeypatch, tmp_path, 1,
                         [("app/sync.py", 11, "a stale mirror")], head="aaa111")
    assert r1["provenance_restored"] is None


def test_recurrence_still_knows_the_fixer_WROTE_there(monkeypatch, tmp_path):
    """The filter is subtracted for provenance only, and `_recurrence` keeps the
    whole added set. The two ask different questions: provenance asks who AUTHORED
    the line, recurrence asks where the fix pass was WORKING — and it says in its
    own name that it reports a position rather than a verdict. The fixer did put
    those lines back, so a finding standing on them is still standing on the last
    pass's work."""
    repo, (_v1, _v2, v3) = _repo_at(tmp_path, V1, V2, V3)
    r3 = _cycle_round_three(
        monkeypatch, tmp_path, repo,
        [("app/sync.py", 4, "the restored block leaks a handle")],
        patch=RESTORE_PATCH, head=v3)

    assert r3["to_fix"][0]["provenance"] == "missed"
    # `revisited` and not `fix-site`: round 2 complained about this file, so the
    # stronger of the two positions applies. Either would do here — what the
    # assertion is for is that recurrence still places the finding ON the pass's
    # lines after provenance has stopped attributing them to it.
    assert r3["to_fix"][0]["recurrence"] == "revisited"
