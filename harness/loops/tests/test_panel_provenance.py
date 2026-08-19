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

The end-to-end half at the bottom is not decoration. Every helper here was unit
tested and green while `unread_files` came back empty on every real run ever made:
`run()` looked its per-reviewer budgets up under the reviewers' DISPLAY labels
(`claude (opus)`) while the budget map is keyed by bare name, so every lookup
missed, every cut set was empty, and the `missed-unread` bucket was unreachable in
production. Nothing that called a helper directly could see it. What pins it is a
test that drives `run()` with a budget small enough to truncate and insists a file
comes back named.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_scope  # noqa: E402  — scope/range readers moved here in #129
import panel_seats  # noqa: E402
import panel_core  # noqa: E402  — `sh` is defined here since #129
from conftest import gh_stub  # noqa: E402



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


def test_a_cut_inside_the_FIRST_file_names_it_and_everything_after():
    """The branch the straddling case argues for, exercised where it actually
    branches: a budget landing inside a.py leaves a.py half-delivered and b.py not
    delivered at all. Every case above cuts at or after b.py, so `cur` was only
    ever the last file — this is the one where it is set and then every remaining
    line trips the condition."""
    assert panel._diff_files_cut(TWO_FILES, B_STARTS - 10) == {"a.py", "b.py"}


def test_a_budget_of_zero_means_nothing_was_read():
    """The record a round with no coverage has to leave. A round that ran no
    reviewer read nothing, and the empty set says the opposite — that it read all
    of it — which hands the next round a `missed` for every defect in a diff
    nobody ever saw."""
    assert panel._diff_files_cut(TWO_FILES, 0) == {"a.py", "b.py"}


# --------------------------------------------------------------------------
# What a path IS — one parser, because two would disagree in silence
# --------------------------------------------------------------------------

def test_a_path_with_a_SPACE_is_read_correctly():
    """Git does not quote a plain space, so `diff --git a/x y.py b/x y.py` holds
    two ` b/`-shaped substrings and splitting on either end mangles it. Mangled,
    the file is named something matching nothing: it is never reported unread and
    its defects come back as reviewer misses. Both halves are the same path, so
    the split point is arithmetic."""
    assert panel._diff_file_path("diff --git a/x b/y.py b/x b/y.py") == "x b/y.py"
    assert panel._diff_file_path("diff --git a/two words.py b/two words.py") == "two words.py"


def test_a_QUOTED_path_is_unquoted():
    r"""Git C-quotes any path with a non-ASCII byte, a quote or a backslash in it
    — `diff --git "a/w\303\251ird.py" "b/w\303\251ird.py"` — and that header holds
    no ` b/` at all. Left unparsed the file drops out of the diff entirely; left
    quoted it is spelled one way here and another by every reviewer reporting a
    finding in it, and `_same_file` matches neither."""
    quoted = r'diff --git "a/w\303\251ird.py" "b/w\303\251ird.py"'
    assert panel._diff_file_path(quoted) == "wéird.py"
    assert panel._diff_file_path(r'+++ "b/w\303\251ird.py"') == "wéird.py"


def test_the_plus_plus_plus_line_is_the_anchor_and_a_deletion_names_nothing():
    """`+++ b/<path>` carries ONE path, so nothing has to be guessed about where
    it ends — it is the authoritative spelling. `+++ /dev/null` is a deletion and
    names no new-side file, so it must not overwrite the header's answer."""
    assert panel._diff_file_path("+++ b/app/sync.py") == "app/sync.py"
    assert panel._diff_file_path("+++ /dev/null") is None


def test_both_helpers_spell_a_path_the_same_way():
    """`_provenance` compares `_diff_added_lines`' keys against `_diff_files_cut`'s
    members through `_same_file`, so the two MUST agree on what a path is. Two
    parsers that disagree misattribute in silence rather than failing."""
    spaced = ("diff --git a/two words.py b/two words.py\n"
              "--- a/two words.py\n"
              "+++ b/two words.py\n"
              "@@ -1,0 +1,1 @@\n"
              "+added\n")
    assert set(panel._diff_added_lines(spaced)) == {"two words.py"}
    assert panel._diff_files_cut(spaced, 5) == {"two words.py"}


def test_an_added_line_that_looks_like_a_header_is_content():
    """A source line reading `++ x` is spelled `+++ x` in a diff. Inside a hunk it
    is an added line, not a `+++` header, and treating it as one renames the file
    mid-hunk."""
    tricky = ("diff --git a/a.py b/a.py\n"
              "--- a/a.py\n"
              "+++ b/a.py\n"
              "@@ -1,0 +1,2 @@\n"
              "+++ b/not-a-file.py\n"
              "+real\n")
    assert panel._diff_added_lines(tricky) == {"a.py": {1, 2}}


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


def test_a_path_that_could_name_TWO_changed_files_is_unknown():
    """The other edge of the same suffix rule. If the fix touched two `panel.py`s
    and a reviewer wrote the bare name, nothing places the finding in either — and
    a coin toss between them is not a measurement. `unknown` is the honest answer;
    `introduced` would be a confident guess."""
    two = {"harness/loops/panel.py": {11}, "vendor/panel.py": {11}}
    assert panel._provenance("panel.py", 11, two, set(), True) == "unknown"


def test_a_defect_in_a_file_the_last_round_COULD_NOT_READ_is_its_own_bucket():
    """Not a reviewer failure — a coverage failure, and the only bucket that
    indicts the harness rather than the panel. Folded into `missed` it would read
    as "the reviewers keep missing things" and buy exactly the wrong remedy."""
    got = panel._provenance("far.py", 7, ADDED, {"far.py"}, True)
    assert got == "missed-unread"


def test_a_round_that_read_NOTHING_could_not_have_missed_anything():
    """A skipped round banks a head_sha and an empty `unread_files`, and empty
    means "no coverage recorded", not "read everything". Read the second way, a
    skip anywhere in a cycle silently converts every later coverage failure into a
    reviewer miss — and erases the truncation record of the last round that did
    read something."""
    assert panel._provenance("anywhere.py", 7, ADDED, set(), True,
                             all_unread=True) == "missed-unread"
    # The fix's own lines still win: that defect was introduced, not unseen.
    assert panel._provenance("harness/loops/panel.py", 11, ADDED, set(), True,
                             all_unread=True) == "introduced"


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
# Reading the fix range — every way it can fail to be one
# --------------------------------------------------------------------------

COMPARE_PATCH = "@@ -10,0 +11,2 @@\n+introduced_by_the_fix()\n+and_this_one_too()"


def _compare(status="ahead", files=(("app/sync.py", COMPARE_PATCH),)) -> str:
    """What `gh api repos/…/compare/a...b --jq …` prints."""
    return json.dumps({"status": status,
                       "files": [{"filename": f, "patch": p} for f, p in files]})


def _sh_returning(body):
    def fake(args, **kw):
        return body
    return fake


def _sh_raising(exc):
    def fake(args, **kw):
        raise exc
    return fake


def test_a_readable_range_comes_back_as_a_diff(monkeypatch):
    """The happy path, reconstructed from the compare API's per-file patches into
    something `_diff_added_lines` reads — which is the only consumer."""
    monkeypatch.setattr(panel_core, "sh", _sh_returning(_compare()))
    diff, why = panel._fix_range_diff("acme/board", "aaaa1111", "bbbb2222")
    assert why is None
    assert panel._diff_added_lines(diff) == {"app/sync.py": {11, 12}}


def test_the_range_is_asked_for_as_json_not_as_a_raw_diff(monkeypatch):
    """`compare/a...b` is the THREE-dot form, so it is only the fix range while the
    branch grew linearly. The `status` field is the sole thing that can tell a
    rewritten branch from a linear one, and it exists only in the JSON body — so
    the call must not be made with the diff media type."""
    seen = []

    def fake(args, **kw):
        seen.append(args)
        return _compare()

    monkeypatch.setattr(panel_core, "sh", fake)
    panel._fix_range_diff("acme/board", "aaaa1111", "bbbb2222")
    assert seen and "--jq" in seen[0] and "Accept: application/vnd.github.v3.diff" not in seen[0]


def test_a_branch_REWRITTEN_between_rounds_is_refused(monkeypatch):
    """The bias three-dot compare carries, and the one case it can be caught in.
    After a force-push the old head is no longer an ancestor, the merge base falls
    back to somewhere on main, and the "fix range" balloons to every line the PR
    ever added — so every finding on a PR-added line reads `introduced` and the
    fixer is confidently blamed for all of it. GitHub calls it `diverged`."""
    monkeypatch.setattr(panel_core, "sh", _sh_returning(_compare(status="diverged")))
    diff, why = panel._fix_range_diff("acme/board", "aaaa1111", "bbbb2222")
    assert diff is None and "diverged" in why and "rewritten" in why


def test_an_empty_compare_is_no_range(monkeypatch):
    """A revert that nets to nothing, or an empty commit. Treated as a readable
    range with zero added lines, every new finding comes back `missed` —
    confidently, and with no note to say the range was empty."""
    monkeypatch.setattr(panel_core, "sh", _sh_returning(_compare(files=())))
    diff, why = panel._fix_range_diff("acme/board", "aaaa1111", "bbbb2222")
    assert diff is None and "changed no line" in why


def test_a_range_too_large_to_hold_is_not_held(monkeypatch):
    """Attribution needs added-line locations, and nothing gates on it. A
    multi-commit range big enough to matter is not worth the memory: past the cap
    it degrades to unknown, which costs a signal, rather than to a resident copy
    of somebody's vendored tree."""
    monkeypatch.setattr(panel_scope, "FIX_RANGE_MAX_CHARS", 40)
    monkeypatch.setattr(panel_core, "sh", _sh_returning(_compare()))
    diff, why = panel._fix_range_diff("acme/board", "aaaa1111", "bbbb2222")
    assert diff is None and "larger than" in why


def test_the_call_is_bounded_by_a_timeout(monkeypatch):
    """A hung `gh` must not hold a review round open indefinitely for an
    attribution nobody gates on."""
    seen = {}

    def fake(args, **kw):
        seen.update(kw)
        return _compare()

    monkeypatch.setattr(panel_core, "sh", fake)
    panel._fix_range_diff("acme/board", "aaaa1111", "bbbb2222")
    assert seen.get("timeout") == panel.FIX_RANGE_TIMEOUT_S


def test_a_missing_end_of_the_range_is_named_before_any_call_is_made(monkeypatch):
    """Two of the three reasons are knowable without asking GitHub, and each reads
    very differently to an operator: a baseline written before `head_sha` existed
    is a one-off that fixes itself next round, an unmoved head means no fix pass
    ran at all."""
    monkeypatch.setattr(panel_core, "sh", _sh_raising(AssertionError("must not call gh")))
    assert panel._fix_range_diff("acme/board", None, "bbbb2222")[1].startswith(
        "the baseline does not record")
    assert "did not record" in panel._fix_range_diff("acme/board", "aaaa1111", None)[1]


def test_an_unmoved_head_is_not_a_github_failure(monkeypatch):
    """`could not read the range aaa111..aaa111` sends the operator hunting for an
    API fault that never happened. Nothing landed between the rounds — that is the
    whole of it, and asking GitHub to compare a commit with itself buys an API
    call to be told so."""
    monkeypatch.setattr(panel_core, "sh", _sh_raising(AssertionError("must not call gh")))
    diff, why = panel._fix_range_diff("acme/board", "aaa111", "aaa111")
    assert diff is None and why == "no commit landed between rounds (head unchanged at aaa111)"


def test_a_range_that_github_cannot_serve_is_a_reason_not_a_crash(monkeypatch):
    """A force-push orphans the earlier head and the compare API 404s. Provenance
    is not gated on, so it must never take a round down with it — and the earlier
    version of this test reached none of this, because every call it made exited
    through a guard clause before `gh` was ever invoked."""
    monkeypatch.setattr(panel_core, "sh", _sh_raising(subprocess.CalledProcessError(1, "gh")))
    diff, why = panel._fix_range_diff("acme/board", "aaaa1111", "bbbb2222")
    assert diff is None and "could not read the range aaaa1111..bbbb2222" in why


def test_no_gh_on_path_is_a_reason_too(monkeypatch):
    """`sh` runs `gh` with `check=True`, so a missing binary is a FileNotFoundError
    and not a CalledProcessError. Caught only on the latter, it escapes `run()` and
    kills a whole review round to protect an attribution nothing gates on — the
    exact outcome the docstring promises will not happen."""
    monkeypatch.setattr(panel_core, "sh", _sh_raising(FileNotFoundError("gh")))
    assert panel._fix_range_diff("acme/board", "aaaa1111", "bbbb2222")[0] is None


def test_a_hung_call_and_a_mangled_body_are_reasons_too(monkeypatch):
    """The other two ways this can fail once it has started: the timeout firing,
    and a body that is not the JSON the `--jq` projection promises."""
    monkeypatch.setattr(panel_core, "sh", _sh_raising(subprocess.TimeoutExpired("gh", 60)))
    assert panel._fix_range_diff("acme/board", "aaaa1111", "bbbb2222")[0] is None
    monkeypatch.setattr(panel_core, "sh", _sh_returning("<html>502</html>"))
    assert panel._fix_range_diff("acme/board", "aaaa1111", "bbbb2222")[0] is None


# --------------------------------------------------------------------------
# What the baseline carries forward
# --------------------------------------------------------------------------

THIS_RUN = {"repo": "acme", "github": "acme/board", "pr": 34, "round": 3}


def _round(tmp_path, name, round_no, **over):
    p = tmp_path / name
    p.write_text(json.dumps({
        "round": round_no, "cycle": "abc123", "reviewed": True,
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


def test_two_payloads_for_ONE_round_resolve_the_same_way_either_order(tmp_path):
    """A re-run after a force-push leaves two payloads claiming round 2 with
    different commits. `max()` breaks a tie by iteration order — i.e. by the order
    the caller happened to pass the paths — so provenance would attribute against
    a different commit depending on how the shell expanded a glob. Broken by
    mtime instead, and the problem note says the ambiguity now covers the commit
    as well as the cycle id."""
    first = _round(tmp_path, "r2a.json", 2, head_sha="aaaa1111")
    second = _round(tmp_path, "r2b.json", 2, head_sha="bbbb2222")
    Path(second).touch()  # written last, so it is the one that describes the PR
    forward = panel.load_baseline([first, second], THIS_RUN)
    backward = panel.load_baseline([second, first], THIS_RUN)
    assert forward.head_sha == backward.head_sha == "bbbb2222"
    assert any("commit and coverage record" in p for p in forward.problems)


def test_a_baseline_from_before_head_sha_existed_says_so(tmp_path):
    """Every payload banked before this landed records no commit at all — `base`
    holds a branch NAME. Such a baseline must yield None, so the round reports
    `unknown` rather than attributing findings against a range it invented."""
    b = panel.load_baseline([_round(tmp_path, "r2.json", 2)], THIS_RUN)
    assert b.head_sha is None and b.unread_files == set()


def test_a_head_sha_that_is_not_a_commit_id_is_refused(tmp_path):
    """The baseline is a file on disk and its `head_sha` is interpolated straight
    into `repos/{repo}/compare/{a}...{b}`. No shell is involved, but a `/`, a `..`
    or a `?` re-points the request at another endpoint or another repo's history —
    whose diff is then used to attribute this round's findings. Absent already
    degrades cleanly to `unknown`, so refusing a value that cannot be a commit
    costs nothing and is said out loud rather than swallowed."""
    b = panel.load_baseline(
        [_round(tmp_path, "r2.json", 2, head_sha="../../graphql?x=1")], THIS_RUN)
    assert b.head_sha is None
    assert any("not a commit id" in p for p in b.problems)


def test_the_unread_files_travel_with_the_baseline(tmp_path):
    """What round 2 could not read is what decides round 3's `missed-unread`, so
    it has to survive the trip between processes."""
    b = panel.load_baseline(
        [_round(tmp_path, "r2.json", 2, head_sha="bbbb2222",
                unread_files=["far.py", "further.py"])], THIS_RUN)
    assert b.unread_files == {"far.py", "further.py"}


def test_an_unread_files_that_is_not_a_list_is_refused(tmp_path):
    """A bare string iterates into a set of single characters, and `_same_file`
    would then suffix-match those against real paths — garbage `missed-unread`
    attributions out of a corrupted field. The findings buckets beside it already
    check their shape; this one has to as well."""
    b = panel.load_baseline(
        [_round(tmp_path, "r2.json", 2, head_sha="bbbb2222", unread_files="far.py")],
        THIS_RUN)
    assert b.unread_files == set()
    assert any("not a list" in p for p in b.problems)


def test_a_SKIPPED_round_is_no_coverage_rather_than_full_coverage(tmp_path):
    """A skipped round banks a head_sha — the next round's fix range has to start
    somewhere — and an empty `unread_files`, because it never fetched a diff to
    name files from. Read as "it read everything", a skip converts every later
    coverage failure into a reviewer miss, and erases the genuine truncation
    record of the last round that did read something."""
    b = panel.load_baseline(
        [_round(tmp_path, "r2.json", 2, head_sha="bbbb2222", reviewed=False)], THIS_RUN)
    assert b.read_nothing is True and b.head_sha == "bbbb2222"
    reviewed = panel.load_baseline(
        [_round(tmp_path, "r3.json", 2, head_sha="cccc3333")], THIS_RUN)
    assert reviewed.read_nothing is False


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


# --------------------------------------------------------------------------
# The wiring, end to end
#
# The helpers above are unit-tested, but the interesting failures live in run():
# whether `head_sha` reaches the payload, whether the fix range is taken between
# the RIGHT two commits, whether a repeat is left unasked rather than attributed,
# and — the one that actually shipped broken — whether the per-reviewer budgets
# are looked up under a key that exists. `tests/test_round_coverage.py` drives a full cycle
# already, but its double returns one `headRefOid` for every round, so the head
# never moves, the range is empty by the guard, and every finding there is
# `unknown`. That is correct behaviour and no cover at all for the path that does
# the work.
# --------------------------------------------------------------------------

PR_DIFF = (
    "diff --git a/app/sync.py b/app/sync.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/app/sync.py\n"
    "+++ b/app/sync.py\n"
    "@@ -1,1 +1,2 @@\n"
    "+mirror = {}\n"
    "diff --git a/app/far.py b/app/far.py\n"
    "index 3333333..4444444 100644\n"
    "--- a/app/far.py\n"
    "+++ b/app/far.py\n"
    "@@ -1,1 +1,2 @@\n"
    "+tail = 1\n"
)

#: Where the second file starts, so a budget can be placed exactly: a reviewer
#: given this many chars read app/sync.py in full and app/far.py not at all.
FAR_STARTS = PR_DIFF.index("diff --git a/app/far.py")

#: The fix pass: two lines added at 11 and 12 of app/sync.py. A finding on one of
#: those was introduced by it; one anywhere else was not.
FIX_COMPARE = _compare()

CFG = {
    "github": "acme/e2e",
    "path": "/tmp/acme-e2e",
    "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
    "review_panel": {},
}


def _cfg(**budgets) -> dict:
    """A panel of one seat per named reviewer, each with its own diff budget (None
    for the whole diff). The multi-seat shapes are what pin the intersection rule
    the README makes a load-bearing claim about."""
    return {**CFG, "reviewers": {
        name: {"enabled": True, "model": "sonnet",
               **({} if budget is None else {"max_diff_chars": budget})}
        for name, budget in budgets.items()}}


def _panel_round(monkeypatch, tmp_path, round_no, findings, head, baseline=(),
                 cfg=None, compare=None, moves_to=None):
    """One panel run with every subprocess replaced, so what is under test is the
    payload the panel builds rather than any CLI."""
    # One shared double (conftest.gh_stub) rather than a bespoke one: it knows
    # every `gh` call panel.py makes, so a call added later is answered here
    # instead of falling through and degrading the round in silence (128-F09).
    fake_sh = gh_stub(
        meta={"title": "feat: mirror", "additions": 20, "deletions": 2,
              "headRefOid": head},
        head_moves_to=moves_to,
        compare=FIX_COMPARE if compare is None else compare,
        diff=PR_DIFF)

    def fake_review(name, model, prompt, effort="", **_kw):  # **_kw: code_tree since #113
        # Only the first seat files, so two seats do not produce two canonical
        # records of one defect. What the extra seats are here for is the diff
        # budget they carry.
        return panel.ReviewerRun(
            [panel.Finding("claude", "P2", f, ln, t, "detail")
             for f, ln, t in findings] if name == "claude" else [], None, 800, None)

    def fake_adjudicate(clusters, diff, model, pr, budget=None, coverage=None, ci="", **_kw):  # **_kw: code_tree/budget_usd since #113
        return ([panel.Canonical(id=panel._finding_id(pr, i + 1), severity="P2",
                                 file=f.file, line=f.line, synthesis=f.title,
                                 verdict="confirmed", detail="detail",
                                 reported_by=[f], rationale="real")
                 for i, grp in enumerate(clusters) for f in grp], None, "")

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg or CFG)
    monkeypatch.setattr(panel_core, "sh", fake_sh)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", fake_adjudicate)
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("e2e", 77, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=2) == 0
    return str(out), json.loads(out.read_text())


def test_a_round_records_the_commit_it_reviewed(monkeypatch, tmp_path):
    """Round 1 has nothing to attribute against, but it must still bank the SHA —
    it is the far end of the NEXT round's fix range — and the coverage record that
    goes with it. An untruncated round read everything, so nothing is named."""
    _, r1 = _panel_round(monkeypatch, tmp_path, 1,
                         [("app/sync.py", 11, "a stale mirror")], head="aaa111")
    assert r1["head_sha"] == "aaa111"
    assert r1["unread_files"] == []
    # Nothing to attribute in round 1: there is no earlier fix pass.
    assert r1["provenance_counts"] == {}
    assert all(f["provenance"] is None for f in r1["to_fix"])


def test_a_truncated_round_BANKS_the_file_it_could_not_read(monkeypatch, tmp_path):
    """The line the whole `missed-unread` bucket hangs off, and the one that was
    dead. `run()` looked each seat's budget up by its display label (`claude
    (sonnet)`) while `budgets` is keyed by bare name, so every lookup returned
    None, `_diff_files_cut` was handed no budget, and `unread_files` came back
    empty on every real run — with 487 unit tests green, because each of them
    called the helper directly with a budget that existed."""
    _, r1 = _panel_round(monkeypatch, tmp_path, 1,
                         [("app/sync.py", 11, "a stale mirror")], head="aaa111",
                         cfg=_cfg(claude=FAR_STARTS))
    assert r1["unread_files"] == ["app/far.py"]


def test_a_defect_in_a_file_the_last_round_never_SAW_is_a_coverage_failure(
        monkeypatch, tmp_path):
    """Round 1 truncated out of app/far.py, round 2 finds a defect in it. The
    round-1 payload has to carry the file across, the loader has to read it back,
    and `_provenance` has to prefer it over `missed` — three seams that were only
    ever tested at their two ends, with a hand-built set standing in for the
    middle."""
    r1_path, r1 = _panel_round(monkeypatch, tmp_path, 1,
                               [("app/sync.py", 11, "a stale mirror")], head="aaa111",
                               cfg=_cfg(claude=FAR_STARTS))
    assert r1["unread_files"] == ["app/far.py"]
    _, r2 = _panel_round(monkeypatch, tmp_path, 2,
                         [("app/far.py", 4, "the tail nobody received")],
                         head="bbb222", baseline=[r1_path])
    got = {f["synthesis"]: f["provenance"] for f in r2["to_fix"]}
    assert got["the tail nobody received"] == "missed-unread"
    assert r2["provenance_counts"]["missed-unread"] == 1


def test_one_seat_that_READ_a_file_clears_it_for_the_whole_round(monkeypatch, tmp_path):
    """A file is unread only if EVERY reviewer that ran was cut on it: one seat
    that read it means the ROUND saw it, and blaming coverage for a defect some
    reviewer could plainly see lets the panel off the hook for its own miss. A
    regression to `set.union` inverts that and passes every single-seat test."""
    _, r1 = _panel_round(monkeypatch, tmp_path, 1,
                         [("app/sync.py", 11, "a stale mirror")], head="aaa111",
                         cfg=_cfg(claude=FAR_STARTS, codex=None))
    assert r1["unread_files"] == []


def test_two_truncated_seats_bank_only_what_BOTH_of_them_missed(monkeypatch, tmp_path):
    """The intersection, where union and intersection actually differ. One seat is
    cut out of app/far.py alone, the other out of both files — so the round saw
    app/sync.py (one seat read it) and nobody saw app/far.py."""
    _, r1 = _panel_round(monkeypatch, tmp_path, 1,
                         [("app/sync.py", 11, "a stale mirror")], head="aaa111",
                         cfg=_cfg(claude=FAR_STARTS, codex=20))
    assert r1["unread_files"] == ["app/far.py"]


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
    blame it for. This is the case `tests/test_round_coverage.py`'s double happens to
    exercise, and it must degrade to `unknown` rather than to `missed` — and say
    that nothing landed, rather than implying GitHub refused something."""
    r1_path, _ = _panel_round(monkeypatch, tmp_path, 1,
                              [("app/sync.py", 11, "a stale mirror")], head="aaa111")
    _, r2 = _panel_round(monkeypatch, tmp_path, 2,
                         [("app/sync.py", 11, "something else entirely")],
                         head="aaa111", baseline=[r1_path])
    assert r2["to_fix"][0]["provenance"] == "unknown"
    assert any("no commit landed between rounds" in n for n in r2["config_notes"])


def test_a_baseline_with_no_commit_degrades_the_whole_round_to_unknown(monkeypatch, tmp_path):
    """Every payload banked before v2.24 records no commit, so the first cycle to
    span the release has a round 2 with nothing to attribute against. Unit-tested
    at `load_baseline`; this is it arriving in a report — every new finding
    `unknown`, and a note saying which end of the range was missing."""
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"round": 1, "cycle": "cyc", "reviewed": True,
                               "repo": "e2e", "github": "acme/e2e", "pr": 77,
                               "to_fix": [], "dismissed": [], "sonar_findings": []}))
    _, r2 = _panel_round(monkeypatch, tmp_path, 2,
                         [("app/sync.py", 11, "a stale mirror")],
                         head="bbb222", baseline=[str(old)])
    assert r2["to_fix"][0]["provenance"] == "unknown"
    assert r2["provenance_counts"]["unknown"] == 1
    assert any("does not record which commit it reviewed" in n for n in r2["config_notes"])


def test_a_head_that_MOVES_while_the_diff_is_fetched_is_noticed(monkeypatch, tmp_path):
    """`headRefOid` is read from the PR metadata before the diff is fetched. A push
    landing in that window leaves the payload naming one commit while the reviewers
    read another, and the next round then attributes against a range that never
    produced the diff anyone reviewed. The reviewed diff is the newer one, so that
    is the commit recorded — and the operator is told."""
    _, r1 = _panel_round(monkeypatch, tmp_path, 1,
                         [("app/sync.py", 11, "a stale mirror")],
                         head="aaa111", moves_to="ccc333")
    assert r1["head_sha"] == "ccc333"
    assert any("the PR head moved from aaa111 to ccc333" in n for n in r1["config_notes"])


# --------------------------------------------------------------------------
# The branches round 2 of the panel found untested
# --------------------------------------------------------------------------

def test_a_finding_with_no_FILE_cannot_be_placed_either():
    """A finding carrying a line but no path is as unplaceable as one carrying a
    path and no line, and belongs in the same bucket. It used to fall through to
    `missed` — a positive claim that the earlier round looked straight at this and
    did not see it, made about a defect that cannot be located anywhere."""
    assert panel._provenance("", 11, {"app/sync.py": {11}}, set(), True) == "unknown"


def test_the_head_RE_READ_is_bounded_by_a_timeout(monkeypatch):
    """It runs on the critical path of every non-skipped round, before any reviewer
    is dispatched, so a hung `gh pr view` stalls the whole panel — and for the same
    ungated attribution `_fix_range_diff` refuses to hang for."""
    seen = {}

    def fake(args, **kw):
        seen.update(kw)
        return json.dumps({"headRefOid": "ccc333"})

    monkeypatch.setattr(panel_core, "sh", fake)
    assert panel._head_sha_now("acme/board", 77) == "ccc333"
    assert seen.get("timeout") == panel.FIX_RANGE_TIMEOUT_S


def test_a_compare_body_that_is_valid_json_but_not_an_OBJECT_is_a_reason(monkeypatch):
    """`<html>502</html>` never reaches the isinstance guard — it dies at
    `json.loads` and leaves through the ValueError arm. A body of `null` or `[]`
    parses cleanly and then has no `.get`, which is the case that guard is for."""
    for body in ("null", "[]"):
        monkeypatch.setattr(panel_core, "sh", _sh_returning(body))
        diff, why = panel._fix_range_diff("acme/board", "aaaa1111", "bbbb2222")
        assert diff is None and "not an object" in why


def test_a_round_where_NO_SEAT_RAN_read_nothing_rather_than_everything(monkeypatch, tmp_path):
    """No LLM seat ran at all, so there is no cut set to intersect and an empty
    `unread_files` would tell the next round this one read the whole diff. It read
    none of it, so every file is named. The guard has to be on whether any seat RAN
    rather than on whether the cut list came back empty — those are two different
    states, and only one of them is zero coverage."""
    _, r1 = _panel_round(monkeypatch, tmp_path, 1, [], head="aaa111",
                         cfg={**CFG, "reviewers": {}})
    assert r1["unread_files"] == ["app/far.py", "app/sync.py"]


def test_the_report_SPLITS_the_new_findings_out_loud(monkeypatch, tmp_path, capsys):
    """The payload carries the split, but the operator deciding whether to go again
    reads the comment. Only the populated buckets appear, and `unknown` is not
    bolded — nothing here may read as a claim about the fix pass that the counts do
    not support."""
    r1_path, _ = _panel_round(monkeypatch, tmp_path, 1,
                              [("app/sync.py", 11, "a stale mirror")], head="aaa111")
    capsys.readouterr()
    _panel_round(monkeypatch, tmp_path, 2,
                 [("app/sync.py", 11, "the fix left a dangling handle"),
                  ("app/sync.py", 90, "an unrelated defect nobody saw")],
                 head="bbb222", baseline=[r1_path])
    line = next(ln for ln in capsys.readouterr().out.splitlines()
                if ln.startswith("  - of those:"))
    assert "**1 introduced** by the last fix pass" in line
    assert "**1 missed** by the last round" in line
    # Nothing landed in these two, so neither may be printed as a zero.
    assert "could not read" not in line and "unattributable" not in line


def test_a_round_that_could_attribute_NOTHING_says_nothing_rather_than_zeroes(
        monkeypatch, tmp_path, capsys):
    """When `unknown` is the only populated bucket, the whole line is withheld.
    Printed, it leads with "**0 introduced**, **0 missed**" directly under a note
    explaining that nothing could be attributed — a bolded claim about the fix
    pass, and a false one. A regression to `if pc:` prints exactly that."""
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"round": 1, "cycle": "cyc", "reviewed": True,
                               "repo": "e2e", "github": "acme/e2e", "pr": 77,
                               "to_fix": [], "dismissed": [], "sonar_findings": []}))
    _, r2 = _panel_round(monkeypatch, tmp_path, 2,
                         [("app/sync.py", 11, "a stale mirror")],
                         head="bbb222", baseline=[str(old)])
    assert r2["provenance_counts"] == {"introduced": 0, "missed": 0,
                                       "missed-unread": 0, "unknown": 1}
    assert "of those:" not in capsys.readouterr().out
