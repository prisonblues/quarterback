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

* `merge_base` — what the reviewed diff was built FROM. `gh pr diff` is the
  three-dot diff, so the seats read `merge_base...head_sha`. Free off metadata
  `run()` already fetches. Moves only when the branch acts.
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
    monkeypatch.setattr(panel, "sh", _sh_returning(REF_BODY))
    assert panel._base_tip_now("acme/board", "main") == "beef0001"


def test_the_ref_endpoint_is_asked_for_by_name(monkeypatch):
    """Matched on the API path so a change of spelling fails here rather than
    quietly falling through to whatever else the caller's `sh` double returns.

    `git/ref/heads/…` and not `commits/…`: the commits endpoint ships the whole
    commit including its file list to deliver one sha, and this call runs on the
    critical path of every reviewed round."""
    seen = []
    monkeypatch.setattr(panel, "sh",
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

    monkeypatch.setattr(panel, "sh", fake)
    panel._base_tip_now("acme/board", "main")
    assert seen.get("timeout") == panel.FIX_RANGE_TIMEOUT_S


def test_no_base_branch_asks_nothing(monkeypatch):
    """An empty ref would build `…/git/ref/heads/` and ask GitHub for every
    branch there is. There is nothing to look up, and the answer is None."""
    monkeypatch.setattr(panel, "sh", _sh_raising(AssertionError("must not call gh")))
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
        monkeypatch.setattr(panel, "sh", _sh_raising(exc))
        assert panel._base_tip_now("acme/board", "main") is None, exc

    for body in ("<html>502</html>", "null", "[]", "{}",
                 json.dumps({"object": {}}), json.dumps({"object": None})):
        monkeypatch.setattr(panel, "sh", _sh_returning(body))
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
         base_tip=REF_BODY, cfg=None):
    """One panel run with every subprocess replaced, so what is under test is the
    payload rather than any CLI. `base_tip` is the raw body the ref call returns,
    so a test can make that one call fail without touching the others."""
    calls = []

    def fake_sh(args, **kw):
        calls.append(args)
        if args[:3] == ["gh", "pr", "view"]:
            meta = {"title": title, "additions": 20, "deletions": 2,
                    "baseRefName": "main", "headRefName": "feat/x",
                    "headRefOid": "aaa111"}
            if merge_base is not None:
                meta["baseRefOid"] = merge_base
            return json.dumps(meta)
        if args[:2] == ["gh", "api"] and "/git/ref/heads/" in args[2]:
            if isinstance(base_tip, BaseException):
                raise base_tip
            return base_tip
        return PR_DIFF

    def fake_review(name, model, prompt, effort=""):
        return panel.ReviewerRun([], None, 800, None)

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg or CFG)
    monkeypatch.setattr(panel, "sh", fake_sh)
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
