"""Where a new finding CAME FROM: introduced by the last fix, or missed by the
last round.

`new_this_round` is binary — did an earlier round raise this defect — so a finding
new to round 2 is one of two very different things. Either the round-1 fix commit
created it (the loop is finding its own damage, and the remedy is smaller, more
conservative fix passes) or round 1 looked straight at it and did not see it (the
remedy is the opposite: spend on coverage, because more rounds genuinely help).
Conflated into one count, neither conclusion is available, including the one an
operator has to draw at the cap.

The measurement is a SIGNAL and not a verdict, and these tests pin that as
carefully as they pin the happy path: a fix can break something at a distance, an
unreadable range has to degrade to "unknown" rather than to a wrong attribution,
and a defect in a file the earlier round was truncated out of is a coverage
failure rather than a reviewer failure and gets its own bucket.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402


TWO_FILES = (
    "diff --git a/a.py b/a.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/a.py\n"
    "+++ b/a.py\n"
    "@@ -1,0 +1,1 @@\n"
    "+first\n"
    "diff --git a/b.py b/b.py\n"
    "index 3333333..4444444 100644\n"
    "--- a/b.py\n"
    "+++ b/b.py\n"
    "@@ -1,0 +1,1 @@\n"
    "+second\n"
)

#: Where b.py's section starts — used to place a budget exactly, so these tests
#: never depend on a hand-counted character total.
B_STARTS = TWO_FILES.index("diff --git a/b.py")


# --------------------------------------------------------------------------
# What a truncated reviewer could not read
# --------------------------------------------------------------------------

def test_an_untruncated_reviewer_read_everything():
    """No budget, or a diff that fits inside it, means nothing was cut. The
    `missed-unread` bucket must never fire for a round that saw the whole diff —
    it would blame the harness for a defect the panel simply missed."""
    assert panel._diff_files_cut(TWO_FILES, None) == set()
    assert panel._diff_files_cut(TWO_FILES, len(TWO_FILES)) == set()


def test_a_file_past_the_cut_is_named():
    """The tail that fell off the end of the prompt. Budgeted to end exactly where
    b.py begins, a.py was read in full and b.py was not read at all."""
    assert panel._diff_files_cut(TWO_FILES, B_STARTS) == {"b.py"}


def test_a_file_STRADDLING_the_cut_counts_as_UNREAD():
    """A reviewer holding half a file's hunks has not read that file. Counting a
    partially-delivered file as read is the optimistic direction, and it is the
    wrong one: it would score a defect in the half nobody received as a reviewer
    miss, which is precisely the attribution this bucket exists to prevent."""
    assert panel._diff_files_cut(TWO_FILES, B_STARTS + 10) == {"b.py"}


# --------------------------------------------------------------------------
# The attribution itself
# --------------------------------------------------------------------------

ADDED = {"harness/loops/panel.py": {10, 11, 12}}


def test_a_defect_on_a_line_the_fix_wrote_is_INTRODUCED():
    """The loop finding its own damage — the case that argues for smaller fix
    passes rather than for more rounds."""
    assert panel._provenance("harness/loops/panel.py", 11, ADDED, set(), True) == "introduced"


def test_a_defect_the_fix_did_not_touch_is_MISSED():
    """Present in the earlier round's diff and not seen. Argues for coverage —
    budget, reviewers, prompts — rather than for a more timid fixer."""
    assert panel._provenance("harness/loops/panel.py", 99, ADDED, set(), True) == "missed"


def test_a_short_path_still_matches_the_diffs_long_one():
    """Reviewers spell paths differently and a finding may carry `panel.py` where
    the diff says `harness/loops/panel.py`. Compared with `==`, every finding from
    a short-path reviewer would read as `missed` — inventing a coverage problem
    out of a spelling difference, and biasing the whole measurement toward the
    bucket that argues for spending more."""
    assert panel._provenance("panel.py", 11, ADDED, set(), True) == "introduced"


def test_two_files_ending_in_the_same_name_are_not_confused():
    """`_same_file` is suffix-aware but not basename-aware, and this is why: a
    defect in one tree's `panel.py` must not be attributed to another's."""
    assert panel._provenance("vendor/panel.py", 11, ADDED, set(), True) == "missed"


def test_a_defect_in_a_file_the_last_round_COULD_NOT_READ_is_its_own_bucket():
    """Not a reviewer failure — a coverage failure, and the only bucket that
    indicts the harness rather than the panel. Folded into `missed` it would read
    as "the reviewers keep missing things" and buy exactly the wrong remedy."""
    got = panel._provenance("far.py", 7, ADDED, {"far.py"}, True)
    assert got == "missed-unread"


def test_unread_beats_unplaceable():
    """A finding with no line number in a file the earlier round never received is
    still squarely a coverage failure. Answering "we could not place it" there
    throws away the one thing actually known about it."""
    assert panel._provenance("far.py", None, ADDED, {"far.py"}, True) == "missed-unread"


def test_an_unreadable_fix_RANGE_is_unknown_not_missed():
    """The failure mode that matters. With no range, every new finding lies
    outside a fix nobody could read, so a naive implementation calls them all
    `missed` — manufacturing a confident, uniform, entirely fictional verdict that
    the reviewers are under-reading. Unknown is the honest answer."""
    assert panel._provenance("harness/loops/panel.py", 11, {}, set(), False) == "unknown"


def test_a_finding_with_no_line_cannot_be_placed():
    """CHANGELOG and README findings routinely carry no line. They are unknown,
    not missed: nothing was established about them either way."""
    assert panel._provenance("CHANGELOG.md", None, ADDED, set(), True) == "unknown"


def test_every_bucket_is_declared():
    """The tally in the payload is built by iterating `PROVENANCE`, so a bucket
    the function can return but the constant does not list would be silently
    dropped from every count."""
    returned = {
        panel._provenance("harness/loops/panel.py", 11, ADDED, set(), True),
        panel._provenance("harness/loops/panel.py", 99, ADDED, set(), True),
        panel._provenance("far.py", 7, ADDED, {"far.py"}, True),
        panel._provenance("x.py", 1, ADDED, set(), False),
    }
    assert returned == set(panel.PROVENANCE)


# --------------------------------------------------------------------------
# What the baseline carries forward
# --------------------------------------------------------------------------

THIS_RUN = {"repo": "acme", "github": "acme/board", "pr": 34, "round": 3}


def _round(tmp_path, name, round_no, **over):
    p = tmp_path / name
    p.write_text(json.dumps({
        "round": round_no, "cycle": "abc123",
        "repo": "acme", "github": "acme/board", "pr": 34,
        "to_fix": [], "dismissed": [], "sonar_findings": [],
        **over,
    }))
    return str(p)


def test_the_fix_range_starts_at_the_LATEST_round_not_the_earliest(tmp_path):
    """The two ends of a baseline set answer different questions and are read from
    opposite ends. `cycle` comes from the EARLIEST round, so every round of a
    cycle shares one id. `head_sha` is the commit the fix pass started from, which
    is the LATEST round's — taking it from the earliest would attribute round 3's
    findings to a range spanning every fix since round 1, scoring round 1's
    repairs as round 2's damage."""
    paths = [_round(tmp_path, "r1.json", 1, head_sha="aaaa1111"),
             _round(tmp_path, "r2.json", 2, head_sha="bbbb2222")]
    b = panel.load_baseline(paths, THIS_RUN)
    assert b.problems == [] and b.rounds == {1, 2}
    assert b.head_sha == "bbbb2222"
    assert b.cycle == "abc123"


def test_order_on_disk_does_not_decide_the_fix_range(tmp_path):
    """Same as above with the paths passed the other way round — the round NUMBER
    decides, not the argument order a caller happened to use."""
    paths = [_round(tmp_path, "r2.json", 2, head_sha="bbbb2222"),
             _round(tmp_path, "r1.json", 1, head_sha="aaaa1111")]
    assert panel.load_baseline(paths, THIS_RUN).head_sha == "bbbb2222"


def test_a_baseline_from_before_head_sha_existed_says_so(tmp_path):
    """Every payload banked before this landed records no commit at all — `base`
    holds a branch NAME. Such a baseline must yield None, so the round reports
    `unknown` rather than attributing findings against a range it invented."""
    b = panel.load_baseline([_round(tmp_path, "r2.json", 2)], THIS_RUN)
    assert b.head_sha is None and b.unread_files == set()


def test_the_unread_files_travel_with_the_baseline(tmp_path):
    """What round 2 could not read is what decides round 3's `missed-unread`, so
    it has to survive the trip between processes."""
    b = panel.load_baseline(
        [_round(tmp_path, "r2.json", 2, head_sha="bbbb2222",
                unread_files=["far.py", "further.py"])], THIS_RUN)
    assert b.unread_files == {"far.py", "further.py"}


def test_a_rejected_baseline_does_not_supply_the_fix_range(tmp_path):
    """A baseline from another cycle has its findings refused, and its commit must
    be refused with them — a concurrent cycle's head is not this cycle's fix range,
    and using it would attribute against an unrelated span of history."""
    paths = [_round(tmp_path, "r1.json", 1, head_sha="aaaa1111"),
             _round(tmp_path, "r2.json", 2, cycle="OTHER", head_sha="bbbb2222")]
    b = panel.load_baseline(paths, THIS_RUN)
    assert any("not this run's" in p for p in b.problems)
    assert b.head_sha == "aaaa1111"


# --------------------------------------------------------------------------
# The shape a consumer reads
# --------------------------------------------------------------------------

def test_a_skipped_payload_answers_the_provenance_keys(tmp_path):
    """`_payload_defaults` exists because the skipped PR — the case a payload is
    FOR — was the one raising KeyError. New keys join it rather than only the
    reviewed path, or reading `payload['provenance_counts']` breaks on exactly the
    payload that has no findings to count."""
    d = panel._payload_defaults()
    assert d["head_sha"] is None
    assert d["unread_files"] == []
    assert d["provenance_counts"] == {}


def test_a_range_that_github_cannot_serve_is_none_not_a_crash():
    """A force-push orphans the earlier head and the compare API 404s. Provenance
    is not gated on, so it must never take a round down with it."""
    assert panel._fix_range_diff("acme/board", None, "bbbb2222") is None
    assert panel._fix_range_diff("acme/board", "aaaa1111", None) is None
    # An unmoved head is not a fix pass, and asking GitHub to compare a commit
    # with itself buys an API call to be told nothing changed.
    assert panel._fix_range_diff("acme/board", "aaaa1111", "aaaa1111") is None


# --------------------------------------------------------------------------
# The wiring, end to end
#
# The helpers above are unit-tested, but the interesting failures live in run():
# whether `head_sha` reaches the payload, whether the fix range is taken between
# the RIGHT two commits, and whether a repeat is left unasked rather than
# attributed. `tests/test_v215.py` drives a full cycle already, but its double
# returns one `headRefOid` for every round — so the head never moves, the range
# is empty by the guard, and every finding there is `unknown`. That is correct
# behaviour and no cover at all for the path that does the work.
# --------------------------------------------------------------------------

PR_DIFF = (
    "diff --git a/app/sync.py b/app/sync.py\n"
    "@@ -1,1 +1,2 @@\n"
    "+mirror = {}\n"
)

#: The fix pass: two lines added at 11 and 12 of app/sync.py. A finding on one of
#: those was introduced by it; one anywhere else was not.
FIX_DIFF = (
    "diff --git a/app/sync.py b/app/sync.py\n"
    "@@ -10,0 +11,2 @@\n"
    "+introduced_by_the_fix()\n"
    "+and_this_one_too()\n"
)

CFG = {
    "github": "acme/e2e",
    "path": "/tmp/acme-e2e",
    "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
    "review_panel": {},
}


def _panel_round(monkeypatch, tmp_path, round_no, findings, head, baseline=()):
    """One panel run with every subprocess replaced, so what is under test is the
    payload the panel builds rather than any CLI."""
    def fake_sh(args, **kw):
        if args[:3] == ["gh", "pr", "view"]:
            return json.dumps({"title": "feat: mirror", "additions": 20,
                               "deletions": 2, "baseRefName": "main",
                               "headRefName": "feat/x", "headRefOid": head})
        # The compare call provenance makes — matched on the API path so a
        # change of spelling fails this test rather than silently falling
        # through to the PR diff and attributing against the wrong thing.
        if args[:2] == ["gh", "api"] and "/compare/" in args[2]:
            return FIX_DIFF
        return PR_DIFF

    def fake_review(name, model, prompt, effort=""):
        return panel.ReviewerRun(
            [panel.Finding("claude", "P2", f, ln, t, "detail")
             for f, ln, t in findings], None, 800, None)

    def fake_adjudicate(clusters, diff, model, pr, budget=None, coverage=None):
        return ([panel.Canonical(id=panel._finding_id(pr, i + 1), severity="P2",
                                 file=f.file, line=f.line, synthesis=f.title,
                                 verdict="confirmed", detail="detail",
                                 reported_by=[f], rationale="real")
                 for i, grp in enumerate(clusters) for f in grp], None, "")

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: CFG)
    monkeypatch.setattr(panel, "sh", fake_sh)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", fake_adjudicate)
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("e2e", 77, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=2) == 0
    return str(out), json.loads(out.read_text())


def test_a_round_records_the_commit_it_reviewed(monkeypatch, tmp_path):
    """Round 1 has nothing to attribute against, but it must still bank the SHA —
    it is the far end of the NEXT round's fix range."""
    _, r1 = _panel_round(monkeypatch, tmp_path, 1,
                   [("app/sync.py", 11, "a stale mirror")], head="aaa111")
    assert r1["head_sha"] == "aaa111"
    # Nothing to attribute in round 1: there is no earlier fix pass.
    assert r1["provenance_counts"] == {}
    assert all(f["provenance"] is None for f in r1["to_fix"])


def test_round_two_splits_its_new_findings_by_where_they_came_from(monkeypatch, tmp_path):
    """The whole point, through the real `run()`: two defects new to round 2, one
    on a line the fix wrote and one nowhere near it, must not read the same."""
    r1_path, _ = _panel_round(monkeypatch, tmp_path, 1,
                        [("app/sync.py", 11, "a stale mirror")], head="aaa111")
    _, r2 = _panel_round(monkeypatch, tmp_path, 2,
                   [("app/sync.py", 11, "the fix left a dangling handle"),
                    ("app/sync.py", 90, "an unrelated defect nobody saw")],
                   head="bbb222", baseline=[r1_path])

    assert r2["head_sha"] == "bbb222"
    got = {f["synthesis"]: f["provenance"] for f in r2["to_fix"]}
    assert got["the fix left a dangling handle"] == "introduced"
    assert got["an unrelated defect nobody saw"] == "missed"
    assert r2["provenance_counts"]["introduced"] == 1
    assert r2["provenance_counts"]["missed"] == 1


def test_a_repeat_is_not_asked_rather_than_answered_unknown(monkeypatch, tmp_path):
    """A defect an earlier round already raised predates the fix pass under
    attribution, so it has no provenance — `null`, not `unknown`. Recorded as
    `unknown` it would inflate the unattributable bucket with findings nobody
    ever intended to attribute, and make an honest measurement look broken."""
    title = "a stale mirror"
    r1_path, _ = _panel_round(monkeypatch, tmp_path, 1,
                        [("app/sync.py", 11, title)], head="aaa111")
    _, r2 = _panel_round(monkeypatch, tmp_path, 2, [("app/sync.py", 11, title)],
                   head="bbb222", baseline=[r1_path])
    repeat = r2["to_fix"][0]
    assert repeat["new_this_round"] is False
    assert repeat["provenance"] is None


def test_an_unmoved_head_attributes_nothing(monkeypatch, tmp_path):
    """No fix pass ran between the rounds, so there is no range and nothing to
    blame it for. This is the case `tests/test_v215.py`'s double happens to
    exercise, and it must degrade to `unknown` rather than to `missed`."""
    r1_path, _ = _panel_round(monkeypatch, tmp_path, 1,
                        [("app/sync.py", 11, "a stale mirror")], head="aaa111")
    _, r2 = _panel_round(monkeypatch, tmp_path, 2,
                   [("app/sync.py", 11, "something else entirely")],
                   head="aaa111", baseline=[r1_path])
    assert r2["to_fix"][0]["provenance"] == "unknown"
    assert any("provenance unavailable" in n for n in r2["config_notes"])
