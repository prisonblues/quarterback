"""What a round was judged AGAINST — the other end of `head_sha`'s range (#98).

v2.24 recorded which commit a round read. The base end stayed a branch *name*,
so an empty To-fix list — a round's most consequential output, and the one an
unattended land acts on — could not be checked against anything later. The
claim is only true relative to a base.

**Two fields, because the obvious one field cannot do the job, and that is what
most of this file exists to pin.** #98 proposed stamping GitHub's `baseRefOid`
and comparing it later against the PR's current `baseRefOid`. That field is the
base branch's TIP as of the PR being opened or of the last push to the HEAD
branch: nothing recomputes it when the base branch advances. Measured on this
repo — PR #87 held `88643c14` across ten commits of `main`, and `git merge-base`
against the moved `main` agreed with it afterwards, the branch having been cut
from the then-tip so that the two coincided. So a check resting on it alone
answers "unmoved, the review still stands" in precisely the case it exists to
catch.

**And it is not the fork point either**, which is #241 and the second half of
this file: measured both older and newer than the true merge base on this repo.
So the two ends below are now BOTH asked for as what they are.

The two ends therefore mean different things and are never derived from each
other:

* `merge_base` — where the branch FORKED, computed by asking for a merge base
  (#241). It was `baseRefOid` off the metadata read until that field was measured
  wrong in both directions, which is the second half of this file. Moves only
  when the branch acts. It is the PR's anchor and not always the round's — under
  v2.28's increment scope the target is `since_sha...head_sha` — which is why
  nothing here asserts it IS what the seats read, only that it is recorded and
  distinct.
* `base_sha` — the base branch's tip at review time. Costs its own lookup, and
  is the only end a staleness check can rest on.

Bounded, swallowed and best-effort throughout: nothing gates on either, so a
`gh` that hangs or lies must cost the round a signal and never the round.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402  — `sh` is defined here since #129
from conftest import UNSET, gh_stub, pr_meta  # noqa: E402

REF_BODY = json.dumps({"ref": "refs/heads/main",
                       "object": {"sha": "beef0001", "type": "commit"}})


def _sh_returning(text: str):
    return lambda args, **kw: text


def _sh_raising(exc: BaseException):
    def fake(args, **kw):
        raise exc
    return fake


# --------------------------------------------------------------------------
# _base_tip_now — the end that actually moves
# --------------------------------------------------------------------------

def test_the_base_tip_is_read_from_the_ref(monkeypatch):
    monkeypatch.setattr(panel_core, "sh", _sh_returning(REF_BODY))
    assert panel._base_tip_now("acme/board", "main") == "beef0001"


def test_the_ref_endpoint_is_asked_for_by_name(monkeypatch):
    """Matched on the API path so a change of spelling fails here rather than
    quietly falling through to whatever else the caller's `sh` double returns.

    `git/ref/heads/…` and not `commits/…`: the commits endpoint ships the whole
    commit including its file list to deliver one sha, and this call runs on the
    critical path of every reviewed round."""
    seen = []
    monkeypatch.setattr(panel_core, "sh",
                        lambda args, **kw: (seen.append(args), REF_BODY)[1])
    panel._base_tip_now("acme/board", "release/2.x")
    assert seen[0][:2] == ["gh", "api"]
    assert seen[0][2] == "repos/acme/board/git/ref/heads/release/2.x"


def test_the_call_is_bounded_by_a_timeout(monkeypatch):
    """It runs before any reviewer is dispatched, so a hung `gh` would stall the
    whole panel for a stamp nothing gates on — the same reason `_head_sha_now`
    and `_fix_range_diff` are bounded."""
    seen = {}

    def fake(args, **kw):
        seen.update(kw)
        return REF_BODY

    monkeypatch.setattr(panel_core, "sh", fake)
    panel._base_tip_now("acme/board", "main")
    assert seen.get("timeout") == panel.FIX_RANGE_TIMEOUT_S


def test_no_base_branch_asks_nothing(monkeypatch):
    """An empty ref would build `…/git/ref/heads/` and ask GitHub for every
    branch there is. There is nothing to look up, and the answer is None."""
    monkeypatch.setattr(panel_core, "sh", _sh_raising(AssertionError("must not call gh")))
    assert panel._base_tip_now("acme/board", "") is None


def test_every_way_the_call_can_fail_is_a_None_and_not_a_crash(monkeypatch):
    """`sh` runs with `check=True`, so a missing binary is a FileNotFoundError
    and not a CalledProcessError — the distinction that let an earlier sibling
    escape its own except clause and take a review round down with it. A 502
    page, a body that parses but has no object, and the timeout firing are the
    rest of the surface."""
    for exc in (subprocess.CalledProcessError(1, "gh"),
                FileNotFoundError("gh"),
                subprocess.TimeoutExpired("gh", 60)):
        monkeypatch.setattr(panel_core, "sh", _sh_raising(exc))
        assert panel._base_tip_now("acme/board", "main") is None, exc

    for body in ("<html>502</html>", "null", "[]", "{}",
                 json.dumps({"object": {}}), json.dumps({"object": None})):
        monkeypatch.setattr(panel_core, "sh", _sh_returning(body))
        assert panel._base_tip_now("acme/board", "main") is None, body


# --------------------------------------------------------------------------
# end to end: what a round records
# --------------------------------------------------------------------------

PR_DIFF = ("diff --git a/a.py b/a.py\n"
           "index 1111111..2222222 100644\n"
           "--- a/a.py\n"
           "+++ b/a.py\n"
           "@@ -1,0 +1,1 @@\n"
           "+first\n")

CFG = {"github": "acme/board", "path": "/tmp/repo",
       "_rules_baseline": ".harness-rules.sample",
       "review_panel": {"skip_title_patterns": ["^Merge "]},
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}}}


def _run(monkeypatch, tmp_path, title="feat: a thing", merge_base="0ddba5e0",
         base_tip=REF_BODY, cfg=None, moves_to=None, merge_base_after=UNSET,
         fork_point=UNSET, diff_base=UNSET):
    """One panel run with every subprocess replaced, so what is under test is the
    payload rather than any CLI. `base_tip` is the raw body the ref call returns,
    so a test can make that one call fail without touching the others.

    `merge_base` is what GitHub STORES for the PR (`baseRefOid`) and `fork_point`
    is what the compare API answers for the real merge base; they agree unless a
    test separates them, which is #241's defect. `diff_base` is the third commit
    in play (#747): `merge-base(stored base, head)`, which is what `gh pr diff`
    builds from. It defaults to `fork_point` — the stored base is an older tip of
    the base branch and the diff is right anyway — so only a test about a genuine
    mis-scoping has to name it.

    `moves_to` makes the head move mid-round — the race the re-read exists for —
    and `merge_base_after` is what the fork-point read then answers, so the
    moved-head path can be driven through `run()` rather than only at the helper
    (128-F03). The reads are told apart by what they ask for: the opening
    metadata read is a `--json` field list, `_head_sha_now` asks for `headRefOid`
    alone, and `_merge_base_now` is a compare call with its own `--jq`."""
    # conftest.gh_stub knows every `gh` call panel.py makes, so this module no
    # longer has to. That is the point of it: the base-tip read below landed with
    # only one module's stub swept, and 48 tests in five others spent hours
    # emitting a note about a failure that never happened (128-F09).
    calls = []
    fake_sh = gh_stub(meta=pr_meta(title=title, head="aaa111", merge_base=merge_base),
                      merge_base=merge_base, fork_point=fork_point,
                      merge_base_after=merge_base_after, diff_base=diff_base,
                      head_moves_to=moves_to, base_tip=base_tip,
                      diff=PR_DIFF, calls=calls)

    def fake_review(name, model, prompt, effort="", **_kw):  # **_kw: code_tree since #113
        return panel.ReviewerRun([], None, 800, None)

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg or CFG)
    monkeypatch.setattr(panel_core, "sh", fake_sh)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, panel.CoverageRuling()))
    out = tmp_path / "r.json"
    assert panel.run("e2e", 77, post=False, json_file=str(out), record=False,
                     round_no=1, max_rounds=2) == 0
    return json.loads(out.read_text()), calls


def test_a_round_records_both_ends_of_what_it_was_judged_against(monkeypatch, tmp_path):
    """The release in one assertion: before it, `base` was a branch name and the
    left-hand side of the diff the seats actually read was named nowhere."""
    payload, _ = _run(monkeypatch, tmp_path)
    assert payload["base"] == "main"
    assert payload["head_sha"] == "aaa111"
    assert payload["merge_base"] == "0ddba5e0"
    assert payload["base_sha"] == "beef0001"


def test_the_merge_base_is_not_the_base_tip(monkeypatch, tmp_path):
    """The defect this whole release exists to prevent, asserted directly. If one
    of these is ever allowed to stand in for the other, a pre-land check inherits
    a comparison that can only answer "unmoved" — however far the base has run.
    They come from different calls precisely so they cannot collapse."""
    payload, _ = _run(monkeypatch, tmp_path)
    assert payload["merge_base"] != payload["base_sha"]


def test_an_unreadable_base_tip_costs_the_field_and_says_so(monkeypatch, tmp_path):
    """Best-effort, like every other stamp on this path: the round completes, the
    field is null rather than invented, and the reason is in `config_notes` where
    an operator reading the report will meet it. A silent null here is the same
    silence #93 was filed over."""
    payload, _ = _run(monkeypatch, tmp_path,
                      base_tip=subprocess.CalledProcessError(1, "gh"))
    assert payload["base_sha"] is None
    assert payload["merge_base"] == "0ddba5e0", "the other end is unaffected"
    assert any("could not be read" in n for n in payload["config_notes"])


def test_a_gh_too_old_for_baseRefOid_still_records_the_tip(monkeypatch, tmp_path):
    """`gh pr view --json` rejects the whole call on a field it does not know, so
    a `gh` without `baseRefOid` dies upstream of here — but the metadata is also
    what a hand-rolled caller or a future field rename would drop, and one absent
    end must not take the other with it."""
    payload, _ = _run(monkeypatch, tmp_path, merge_base=None)
    assert payload["merge_base"] is None
    assert payload["base_sha"] == "beef0001"


def test_the_two_ends_disagreeing_raises_no_warning(monkeypatch, tmp_path):
    """`base_sha != merge_base` is the ordinary state of every PR whose base
    gained a commit after it forked. A note there would fire on nearly every run
    and be trained away — and what the movement MEANS is #96's verdict, not a
    line in the panel's report."""
    payload, _ = _run(monkeypatch, tmp_path)
    assert not any("moved" in n or "stale" in n for n in payload["config_notes"])


def test_a_skipped_round_keeps_the_free_end_and_buys_nothing(monkeypatch, tmp_path):
    """The skip path exists to be cheap: it fetches no diff and is never recorded
    on the board, so a base tip stamped there would cost an API round trip for a
    value with no consumer. It keeps GitHub's stored base, which is already in
    hand off the metadata read.

    The fork-point read (#241) is held to the same bargain, and it is a newer
    call than this test: it is worth an API round trip on a round that is about
    to spend four seats and a judge, and worth nothing at all on one that returns
    without reading a line."""
    payload, calls = _run(monkeypatch, tmp_path, title="Merge main into feat/x")
    assert payload["reviewed"] is False
    assert payload["merge_base"] == "0ddba5e0"
    assert payload["base_sha"] is None
    api = [a[2] for a in calls if a[:2] == ["gh", "api"]]
    assert not any("/git/ref/heads/" in p for p in api)
    assert not any("/compare/" in p for p in api)


# ----------------------------------- GitHub's stored base is not a merge base
#
# #241. `baseRefOid` is the base branch's TIP as of the PR being opened or of the
# last push to the head branch, and GitHub maintains it for its own purposes
# rather than as a merge base. On PR #187 a commit shared with another PR landed
# on `main` and no head push recomputed it, so the diff carried already-landed
# code and a full round returned 15 judge-confirmed findings about it — with
# `config_notes: []` and nothing anywhere in the payload saying the target was
# wrong. It was caught by a peer noticing the diff looked smaller than GitHub
# advertised.
#
# The load-bearing part of the fix is not the recorded field. It is that a
# mis-scoped round must not be SILENT.
#
# #747 CORRECTS WHAT THE NOTE TESTS FOR, and the correction is the second half of
# this section. `gh pr diff` does not build from `baseRefOid`; measured on three
# constructed PRs, it builds the three-dot diff from `merge-base(baseRefOid,
# head)`. Every test below INJECTS that commit as `diff_base`, so what they pin
# is the panel's behaviour given the diff base — not the inference that GitHub
# computes it that way, which no stubbed test can check and which `panel.run`
# carries the caveat for. So `baseRefOid != fork
# point` is NOT the mis-scoping condition — it is the ordinary state of any
# branch cut from an older commit than the base tip (#270's shape), where that
# merge base IS the fork point and the diff is exactly right. Measured on
# lexray#1656: identical file set, identical line counts, identical per-file
# numstat between `gh pr diff` and the fork-point three-dot range the old note
# told the reader to go and check by hand.
#
# The condition that survives is #187's: the stored base is an ANCESTOR of the
# head, the base branch has since absorbed the commits between it and the true
# fork point, and the diff therefore really is built from a commit behind the
# fork point. That is rare, and a note that fires only there is worth reading —
# which is the whole point, because `config_notes` is where the load-bearing
# scoping caveats live and a caveat that is always present and never true trains
# the reader to skim past them.
# --------------------------------------------------------------------------

def test_the_recorded_base_is_the_fork_point_not_githubs_stored_one(
        monkeypatch, tmp_path):
    """The stored base can be wrong in either direction — older than the fork
    point on #187, newer on #270, where it was the tip of `main` and named a
    commit the branch had never contained. Both follow from treating the two as
    the same thing, so the recorded value comes from a merge base or from
    nowhere."""
    payload, _ = _run(monkeypatch, tmp_path,
                      merge_base="e08372ae", fork_point="e38c1020")
    assert payload["merge_base"] == "e38c1020"


def test_a_diff_built_behind_the_fork_point_is_named_in_config_notes(
        monkeypatch, tmp_path):
    """#187's shape, which is the whole complaint and the one that survives #747.
    The base branch absorbed commits the head branch also has, so the commit
    `gh pr diff` built from sits BEHIND the fork point and the diff carries code
    already landed on the base. A round whose target was built that way has to say
    so, name both commits, and give the reader the range to check a finding
    against — because the next step of the cycle briefs a fixer to resolve every
    confirmed finding without re-deriving it."""
    payload, _ = _run(monkeypatch, tmp_path, merge_base="e08372ae",
                      fork_point="e38c1020", diff_base="e08372ae")
    said = [n for n in payload["config_notes"] if "MIS-SCOPED" in n]
    assert len(said) == 1, payload["config_notes"]
    assert "e08372ae" in said[0] and "e38c1020" in said[0]
    assert "gh pr diff" in said[0], "a reader has to be told which base was used"


def test_a_diff_built_AHEAD_of_the_fork_point_warns_the_same_way(
        monkeypatch, tmp_path):
    """The reversed ancestry, and the reason the note says "omit or include"
    rather than naming already-landed code.

    Take `A—B—H` with the stored base at `B`, then reset the BASE branch back to
    `A` with the head untouched. The predicted diff base is `merge-base(B, H) =
    B`; the fork point is now `merge-base(A, H) = A`. The predicate fires — and
    the defect is the opposite one: the diff OMITS `A..B` rather than carrying
    anything that has landed. A note that told this reader to go looking for
    surplus code would be describing a diff that is missing some, so the wording
    commits to neither direction."""
    payload, _ = _run(monkeypatch, tmp_path, merge_base="bbbb2222",
                      fork_point="aaaa1111", diff_base="bbbb2222")
    said = [n for n in payload["config_notes"] if "MIS-SCOPED" in n]
    assert len(said) == 1, payload["config_notes"]
    assert "omit or include" in said[0], said[0]
    assert "already landed" not in said[0], \
        "the reversed shape drops code from the range rather than adding it"


def test_no_stored_base_at_all_says_the_diff_base_is_unknown(
        monkeypatch, tmp_path):
    """A `gh` that answered no `baseRefOid` but a compare that read fine.

    Distinct from "the stored base agrees with the fork point", and the two were
    one branch until #747's review split them. With nothing stored there is no
    `merge-base(stored, head)` to ask for, so the premise the check rests on
    cannot be evaluated at all — which is not the same as evaluating it and
    finding nothing wrong. Staying silent here would report an unexamined target
    as an examined one."""
    payload, _ = _run(monkeypatch, tmp_path, merge_base=None,
                      fork_point="0ddba5e0")
    assert payload["merge_base"] == "0ddba5e0"
    said = " ".join(payload["config_notes"])
    assert "no stored base" in said and "unverified" in said, payload["config_notes"]
    assert not any("MIS-SCOPED" in n for n in payload["config_notes"])


def test_a_base_branch_that_merely_moved_raises_no_note(monkeypatch, tmp_path):
    """#747, and the reason the old condition had to go. `baseRefOid` is the base
    TIP, so it disagrees with the fork point on every PR cut from an older commit
    than that tip — most of them, on an active integration branch. But the diff
    `gh pr diff` serves is built from `merge-base(baseRefOid, head)`, which in
    that shape IS the fork point, so nothing is mis-scoped and the old note was a
    false positive on every such round.

    Asserted through the DIFF BASE rather than by deleting the check, because the
    check is still real in #187's shape above. The two tests differ only in what
    the second compare answers."""
    payload, _ = _run(monkeypatch, tmp_path, merge_base="46219a54",
                      fork_point="d282366d", diff_base="d282366d")
    assert payload["merge_base"] == "d282366d"
    assert not any("MIS-SCOPED" in n for n in payload["config_notes"]), \
        payload["config_notes"]


def test_a_stored_base_that_agrees_costs_no_second_compare(monkeypatch, tmp_path):
    """The ordinary state of a PR nobody's base moved under. A note that fires on
    every run is a note that gets trained away, and this one has to be legible
    when it fires.

    The extra compare is skipped here rather than merely ignored, and the skip is
    exact rather than a saving: where the stored base already IS the fork point it
    is an ancestor of the head, so a merge base against it can only answer
    itself."""
    payload, calls = _run(monkeypatch, tmp_path,
                          merge_base="0ddba5e0", fork_point="0ddba5e0")
    assert payload["merge_base"] == "0ddba5e0"
    assert not any("MIS-SCOPED" in n for n in payload["config_notes"])
    compares = [a[2] for a in calls
                if a[:2] == ["gh", "api"] and panel._MERGE_BASE_JQ in a]
    assert not any("/compare/0ddba5e0..." in p for p in compares), compares


def test_an_unreadable_diff_base_says_unverified_not_mis_scoped(
        monkeypatch, tmp_path):
    """The stored base disagrees with the fork point and the compare that would
    say whether that mattered could not be read. #241's failure mode is silence;
    #747's is a warning worded as a finding when it is a missing measurement. So
    this path says which one it is and does not claim the target was wrong."""
    payload, _ = _run(monkeypatch, tmp_path, merge_base="46219a54",
                      fork_point="d282366d", diff_base=None)
    said = " ".join(payload["config_notes"])
    assert "unverified rather than wrong" in said
    assert "46219a54" in said and "d282366d" in said
    assert not any("MIS-SCOPED" in n for n in payload["config_notes"])


def test_the_stub_refuses_a_compare_from_anything_it_was_not_taught(monkeypatch):
    """#747's review, and a test about the STUB rather than the panel.

    The diff-base read is told from the fork-point read by what it compares FROM,
    and an earlier cut of that classified "anything that is not the base branch"
    as the diff-base read. That hands the expected answer to a regression that
    compares from the head, from an unrelated sha, or from a misspelled branch —
    green tests over a call nobody meant to make, which is the exact hole
    `strict` exists to close. Both operands are matched now, so the wrong ones
    raise like any untaught call."""
    sh = gh_stub(meta=pr_meta(head="aaa111", merge_base="0ddba5e0"),
                 merge_base="0ddba5e0", fork_point="d282366d",
                 diff_base="d282366d")
    jq = ["--jq", panel._MERGE_BASE_JQ]

    def ask(rng):
        return sh(["gh", "api", f"repos/acme/board/compare/{rng}?per_page=1"] + jq)

    # The two calls panel.py really makes.
    assert ask("main...aaa111").strip() == "d282366d"
    assert ask("0ddba5e0...aaa111").strip() == "d282366d"

    for bad, why in [("aaa111...aaa111", "compared from the HEAD"),
                     ("deadbeef...aaa111", "compared from an unrelated sha"),
                     ("mian...aaa111", "compared from a misspelled branch"),
                     ("main...bbb222", "asked about the wrong head")]:
        try:
            ask(bad)
        except AssertionError:
            continue
        raise AssertionError(f"stub answered a call it should not know: {why} ({bad})")


def test_an_unreadable_fork_point_falls_back_and_says_which_base_it_used(
        monkeypatch, tmp_path):
    """Best-effort like every other stamp here — the round completes — but a
    fallback nobody is told about is the silent mis-scoping this issue is about.
    So the note names the base that was actually recorded and warns that it is
    not a merge base."""
    payload, _ = _run(monkeypatch, tmp_path,
                      merge_base="e08372ae", fork_point=None)
    assert payload["merge_base"] == "e08372ae"
    said = " ".join(payload["config_notes"])
    assert "merge base could not be computed" in said
    assert "e08372ae" in said and "not a merge base" in said


def test_neither_base_available_records_neither_and_says_so(monkeypatch, tmp_path):
    """A `gh` that answered no `baseRefOid` AND a compare that could not be read.
    Naming a commit that never existed is what 128-F11 was filed over, so this
    path claims nothing."""
    payload, _ = _run(monkeypatch, tmp_path, merge_base=None, fork_point=None)
    assert payload["merge_base"] is None
    assert any("neither end of its base" in n for n in payload["config_notes"])


# ------------------------------- the head moving takes the merge base with it

def test_the_merge_base_is_asked_for_as_a_merge_base(monkeypatch):
    """#241. This helper used to answer `gh pr view --json baseRefOid`, which is
    GitHub's STORED base and not a merge base — measured wrong in both directions
    on this repo, older than the fork point on PR #187 and newer on PR #270. The
    field it asks for is the whole of the fix, so the field is what is asserted."""
    seen = []

    def fake(args, **kw):
        seen.append(args)
        return "bbbbbbbb2222\n"

    monkeypatch.setattr(panel_core, "sh", fake)
    assert panel._merge_base_now("acme/board", "main", "aaa111") == "bbbbbbbb2222"
    asked = " ".join(seen[0])
    assert "baseRefOid" not in asked, "asked GitHub for its stored base, not a merge base"
    assert "/compare/main...aaa111" in asked and panel._MERGE_BASE_JQ in asked


def test_the_merge_base_read_is_bounded_like_its_siblings(monkeypatch):
    """On the critical path of a round, for a stamp nothing gates on. A hung `gh`
    must not be able to stall the panel."""
    seen = {}

    def fake(args, **kw):
        seen.update(kw)
        return "b" * 12

    monkeypatch.setattr(panel_core, "sh", fake)
    panel._merge_base_now("acme/board", "main", "aaa111")
    assert seen.get("timeout") == panel.FIX_RANGE_TIMEOUT_S


def test_every_way_the_merge_base_read_can_fail_is_a_None(monkeypatch):
    """Same surface as its siblings, and the same reason: `sh` runs with
    `check=True`, so a missing binary is a FileNotFoundError rather than a
    CalledProcessError. A None here means "could not tell", and the caller says
    so in `config_notes` rather than pairing two ends that do not belong
    together.

    The last two are the shapes `--jq` makes possible and JSON parsing did not:
    it prints a string RAW, so a missing field arrives as the four truthy
    characters `null` and a changed response as arbitrary prose. Either would be
    recorded as a commit id by a reader that only checked for emptiness."""
    for exc in (subprocess.CalledProcessError(1, "gh"),
                FileNotFoundError("gh"),
                subprocess.TimeoutExpired("gh", 60)):
        monkeypatch.setattr(panel_core, "sh", _sh_raising(exc))
        assert panel._merge_base_now("acme/board", "main", "aaa111") is None, exc
    for body in ("", "null\n", "not a sha\n", "zzzzzzzzzz\n"):
        monkeypatch.setattr(panel_core, "sh", _sh_returning(body))
        assert panel._merge_base_now("acme/board", "main", "aaa111") is None, body


def test_a_merge_base_with_an_end_missing_is_not_asked_for(monkeypatch):
    """`compare/main...` is a different endpoint, not a merge base. A PR whose
    head or base could not be read has no range to ask about, and the call is
    skipped rather than sent and mis-parsed."""
    monkeypatch.setattr(panel_core, "sh", _sh_raising(
        AssertionError("no call should have been made")))
    assert panel._merge_base_now("acme/board", "main", "") is None
    assert panel._merge_base_now("acme/board", "", "aaa111") is None
    assert panel._merge_base_now("", "main", "aaa111") is None


# --------------------------------------------------------------------------
# end to end: the moved-head path, through run()
#
# The helpers above are unit-tested, and that was all the cover this path had
# (128-F03): every assertion sat on `_merge_base_now` itself, so deleting
# `merge_base = moved_meta` from run() left the whole file green while silently
# reintroducing the pair-straddling-the-push bug this release exists to prevent.
# These drive run() so the wiring is pinned, not just the callee.
# --------------------------------------------------------------------------

def test_a_head_that_moves_re_reads_the_base_and_records_the_NEW_one(monkeypatch, tmp_path):
    """The wiring 128-F03 found unpinned. `merge_base` must be the one belonging
    to the head that is recorded, not the one read before the push landed."""
    payload, _ = _run(monkeypatch, tmp_path, merge_base="0ddba5e0",
                      moves_to="bbb222", merge_base_after="feed1234")
    assert payload["head_sha"] == "bbb222"
    assert payload["merge_base"] == "feed1234"
    assert any("the merge base moved with it" in n for n in payload["config_notes"])


def test_a_base_that_could_not_be_re_read_is_DROPPED_not_kept(monkeypatch, tmp_path):
    """128-F12. Keeping the earlier head's base stores a merge_base/head_sha pair
    that no programmatic consumer can tell from a good one — and #96 is exactly
    such a consumer. A plausible-but-wrong range is worse than an absent one: the
    first gets acted on, the second gets noticed."""
    payload, _ = _run(monkeypatch, tmp_path, merge_base="0ddba5e0",
                      moves_to="bbb222", merge_base_after=None)
    assert payload["head_sha"] == "bbb222"
    assert payload["merge_base"] is None, "the stale base must not survive the push"
    assert any("DROPPED" in n for n in payload["config_notes"])


def test_a_base_that_was_never_read_says_so_honestly(monkeypatch, tmp_path):
    """128-F11. With no base recorded before the move either, the note must not
    claim the recorded end is 'the one computed for the EARLIER head' — there was
    no earlier one, and a diagnostic that names a commit that never existed sends
    the next reader looking for it."""
    payload, _ = _run(monkeypatch, tmp_path, merge_base=None,
                      moves_to="bbb222", merge_base_after=None)
    assert payload["merge_base"] is None
    notes = " ".join(payload["config_notes"])
    assert "neither end" in notes
    assert "EARLIER head" not in notes


def test_a_re_read_that_agrees_is_a_no_op(monkeypatch, tmp_path):
    """The common case — a push that does not move the merge base. No note, and
    the base survives; a warning here would fire on ordinary pushes and be
    trained away."""
    payload, _ = _run(monkeypatch, tmp_path, merge_base="0ddba5e0",
                      moves_to="bbb222", merge_base_after="0ddba5e0")
    assert payload["merge_base"] == "0ddba5e0"
    assert not any("merge base" in n and "moved with it" in n
                   for n in payload["config_notes"])


def test_a_malformed_commit_id_costs_the_field_and_not_the_round(monkeypatch, tmp_path):
    """128-F13. The ids come off a JSON response and were used as `value[:8]` in
    the diagnostics, so a truthy non-string — a number, an object, anything a
    changed or malformed API shape can produce — raised TypeError outside each
    helper's own except and took down a round whose whole purpose is to degrade
    gracefully. Typed at the boundary now, so a bad shape reads as 'could not
    tell'."""
    for bad in (12345, {"sha": "x"}, ["aaa111"], True):
        payload, _ = _run(monkeypatch, tmp_path, merge_base="0ddba5e0",
                          moves_to="bbb222", merge_base_after=bad)
        assert payload["merge_base"] is None, bad
        assert payload["head_sha"] == "bbb222", bad
