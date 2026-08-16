"""What a round was judged AGAINST — the other end of `head_sha`'s range (#98).

v2.24 recorded which commit a round read. The base end stayed a branch *name*,
so an empty To-fix list — a round's most consequential output, and the one an
unattended land acts on — could not be checked against anything later. The
claim is only true relative to a base.

**Two fields, because the obvious one field cannot do the job, and that is what
most of this file exists to pin.** #98 proposed stamping GitHub's `baseRefOid`
and comparing it later against the PR's current `baseRefOid`. That field is the
*merge base*: GitHub recomputes it when the head branch is pushed and never when
the base branch advances, because a common ancestor is not moved by commits
added to one side of it. Measured on this repo — PR #87 held `88643c14` across
ten commits of `main`, and `git merge-base` against the moved `main` agreed with
it afterwards. So a check resting on it alone answers "unmoved, the review still
stands" in precisely the case it exists to catch.

The two ends therefore mean different things and are never derived from each
other:

* `merge_base` — the PR's base commit. `gh pr diff` is the three-dot diff, so a
  whole-PR round reads `merge_base...head_sha`. Free off metadata `run()`
  already fetches. Moves only when the branch acts. It is the PR's anchor and
  not always the round's — under v2.28's increment scope the target is
  `since_sha...head_sha` — which is why nothing here asserts it IS what the
  seats read, only that it is recorded and distinct.
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
       "review_panel": {"skip_title_patterns": ["^Merge "]},
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}}}


def _run(monkeypatch, tmp_path, title="feat: a thing", merge_base="0ddba5e0",
         base_tip=REF_BODY, cfg=None, moves_to=None, merge_base_after=UNSET):
    """One panel run with every subprocess replaced, so what is under test is the
    payload rather than any CLI. `base_tip` is the raw body the ref call returns,
    so a test can make that one call fail without touching the others.

    `moves_to` makes the head move mid-round — the race the re-read exists for —
    and `merge_base_after` is what the re-read then answers, so the moved-head
    path can be driven through `run()` rather than only at the helper (128-F03).
    The three reads are told apart by their `--json` field list: the opening
    metadata read asks for many fields, `_head_sha_now` asks for `headRefOid`
    alone and `_merge_base_now` for `baseRefOid` alone."""
    # conftest.gh_stub knows every `gh` call panel.py makes, so this module no
    # longer has to. That is the point of it: the base-tip read below landed with
    # only one module's stub swept, and 48 tests in five others spent hours
    # emitting a note about a failure that never happened (128-F09).
    calls = []
    fake_sh = gh_stub(meta=pr_meta(title=title, head="aaa111", merge_base=merge_base),
                      merge_base=merge_base, merge_base_after=merge_base_after,
                      head_moves_to=moves_to, base_tip=base_tip,
                      diff=PR_DIFF, calls=calls)

    def fake_review(name, model, prompt, effort=""):
        return panel.ReviewerRun([], None, 800, None)

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg or CFG)
    monkeypatch.setattr(panel_core, "sh", fake_sh)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, ""))
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
    assert payload["merge_base"] == "0ddba5e0", "the free end is unaffected"
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
    value with no consumer. It keeps `merge_base`, which is already in hand."""
    payload, calls = _run(monkeypatch, tmp_path, title="Merge main into feat/x")
    assert payload["reviewed"] is False
    assert payload["merge_base"] == "0ddba5e0"
    assert payload["base_sha"] is None
    assert not any("/git/ref/heads/" in a[2] for a in calls if a[:2] == ["gh", "api"])


# ------------------------------- the head moving takes the merge base with it

def test_merge_base_is_re_read_when_the_head_moves(monkeypatch):
    """Round 1 of #128's panel: `head_sha` was re-stamped when the head moved
    mid-round while `merge_base` kept the value GitHub computed for the commit it
    replaced — so the recorded pair was a range no round ever reviewed.

    It is not a corner case here. GitHub recomputes `baseRefOid` on every push to
    the head branch, and the usual reason a head moves on this repo is a merge of
    the base branch INTO the PR (~1.8 integration merges per PR landed, #80),
    which is exactly the push that moves it. The stored range would then begin
    before an integration merge that its right end contains."""
    seen = []

    def fake(args, **kw):
        seen.append(args)
        return json.dumps({"baseRefOid": "bbbbbbbb2222"})

    monkeypatch.setattr(panel_core, "sh", fake)
    assert panel._merge_base_now("acme/board", 128) == "bbbbbbbb2222"
    assert any("baseRefOid" in " ".join(a) for a in seen), \
        "asked GitHub for the wrong field"


def test_the_merge_base_re_read_is_bounded_like_its_siblings(monkeypatch):
    """On the critical path of a round, for a stamp nothing gates on. A hung `gh`
    must not be able to stall the panel."""
    seen = {}

    def fake(args, **kw):
        seen.update(kw)
        return json.dumps({"baseRefOid": "b" * 12})

    monkeypatch.setattr(panel_core, "sh", fake)
    panel._merge_base_now("acme/board", 128)
    assert seen.get("timeout") == panel.FIX_RANGE_TIMEOUT_S


def test_every_way_the_merge_base_re_read_can_fail_is_a_None(monkeypatch):
    """Same surface as its siblings, and the same reason: `sh` runs with
    `check=True`, so a missing binary is a FileNotFoundError rather than a
    CalledProcessError. A None here means "could not tell", and the caller says
    so in `config_notes` rather than pairing two ends that do not belong
    together."""
    for exc in (subprocess.CalledProcessError(1, "gh"),
                FileNotFoundError("gh"),
                subprocess.TimeoutExpired("gh", 60)):
        monkeypatch.setattr(panel_core, "sh", _sh_raising(exc))
        assert panel._merge_base_now("acme/board", 128) is None, exc
    monkeypatch.setattr(panel_core, "sh", lambda *a, **k: "not json")
    assert panel._merge_base_now("acme/board", 128) is None
    monkeypatch.setattr(panel_core, "sh", lambda *a, **k: json.dumps({}))
    assert panel._merge_base_now("acme/board", 128) is None


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
