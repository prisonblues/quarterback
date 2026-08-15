"""The PR's changed-file list, and why the list is not allowed to speak for itself.

The panel told the board a PR changed 2,032 lines and never which files, so the
only paths the board held were the ones findings happened to name — nine, for a
run whose PR touched more. That is a proxy for the diff and not the diff, and it
left "which other PRs does landing this one disturb?" unanswerable (#82).

Three properties carry the feature, and every test here defends one of them:

* **The PR's files, not the round's.** Under #41 a later round reviews only the
  increment. If this narrowed with it, two PRs would be reported as no longer
  colliding because one of them stopped RE-READING a file it still changes.
  Reading the list off the PR metadata rather than off the diff is what makes
  that true by construction — which is also why the skip path, which never
  fetches a diff at all, can carry a complete list.
* **`changed_files_total` is GitHub's count, never `len(files)`.** `gh` pages the
  files connection and GitHub caps a PR's file list at 3,000. The two are allowed
  to disagree, and their disagreement is the only evidence the list is a prefix.
  Derive one from the other and a truncated list reads as a complete one — this
  repo's standing disease, a shortfall presenting as a clean result.
* **A short list is said out loud**, exactly as a short panel is (#75). The
  numbers are in the payload either way; the note is the copy a human reads.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402


def meta(files=None, total=None, **over) -> dict:
    m = {"title": "fix: a thing", "additions": 30, "deletions": 12,
         "baseRefName": "main", "headRefName": "fix/x", "headRefOid": "abc123"}
    if files is not None:
        m["files"] = files
    if total is not None:
        m["changedFiles"] = total
    return {**m, **over}


def gh_file(path: str, additions: int = 1, deletions: int = 0) -> dict:
    return {"path": path, "additions": additions, "deletions": deletions,
            "changeType": "MODIFIED"}


# ---- what comes back -------------------------------------------------------

def test_each_path_carries_its_own_share_of_the_churn():
    """`changed_lines` is one number for the whole PR. Per-file additions and
    deletions come free from the same `gh pr view` call and make that total
    attributable, which is the difference between "this merge is big" and "this
    merge is big IN THE FILE YOUR PR ALSO TOUCHES"."""
    files, total = panel._changed_files(
        meta(files=[gh_file("app/api/reviews.py", 120, 4),
                    gh_file("app/models/review.py", 40, 0)], total=2))
    assert files == [
        {"path": "app/api/reviews.py", "additions": 120, "deletions": 4},
        {"path": "app/models/review.py", "additions": 40, "deletions": 0},
    ]
    assert total == 2


def test_paths_come_back_sorted_so_two_runs_of_one_pr_compare_directly():
    files, _ = panel._changed_files(
        meta(files=[gh_file("z.py"), gh_file("a.py"), gh_file("m.py")], total=3))
    assert [f["path"] for f in files] == ["a.py", "m.py", "z.py"]


def test_a_pr_that_changed_nothing_is_an_empty_list_and_not_an_error():
    assert panel._changed_files(meta(files=[], total=0)) == ([], 0)


def test_a_blank_path_is_dropped_rather_than_stored():
    """A path is the join key of every collision query built on this. An empty
    one joins to nothing and counts as a file, which is the worst of both."""
    files, total = panel._changed_files(
        meta(files=[gh_file("real.py"), {"path": "  ", "additions": 0, "deletions": 0}],
             total=2))
    assert [f["path"] for f in files] == ["real.py"]
    # The TOTAL still says two, because GitHub said two. The list being shorter
    # than the count is precisely the signal the next test is about.
    assert total == 2


def test_a_file_missing_its_line_counts_records_zero_not_null():
    """`gh` states both for every file it returns. A hand-built or future-shaped
    entry that omits them keeps the path — the collision datum — rather than
    losing the row over the number that is not the point."""
    files, _ = panel._changed_files(meta(files=[{"path": "x.py"}], total=1))
    assert files == [{"path": "x.py", "additions": 0, "deletions": 0}]


# ---- the count is not derived from the list --------------------------------

def test_the_total_is_githubs_count_and_not_the_lists_length():
    """The property the whole feature rests on. GitHub caps a PR's file list at
    3,000; a caller told only `len(files)` cannot tell a 3,000-file prefix from a
    3,000-file PR, and every collision answer built on the prefix is wrong in the
    direction of "no collision"."""
    files, total = panel._changed_files(
        meta(files=[gh_file("a.py"), gh_file("b.py")], total=2500))
    assert len(files) == 2
    assert total == 2500


def test_an_absent_count_falls_back_to_what_can_be_proved():
    """An older `gh` without the field. Falling back to the list's own length
    asserts completeness on no evidence, so the fallback is the one value that
    makes the two agree by construction rather than by claim — and callers see
    "these agree", which is all we actually know."""
    files, total = panel._changed_files(meta(files=[gh_file("a.py")]))
    assert total == len(files) == 1


def test_no_files_field_at_all_is_no_list_rather_than_no_files():
    files, total = panel._changed_files(meta())
    assert files == []
    assert total == 0


# ---- the payload -----------------------------------------------------------

def test_every_payload_carries_the_keys_even_when_the_run_never_got_that_far():
    """`_payload_defaults` is the shape contract: the skip path emits a payload
    too, and a consumer reading `payload['changed_files']` must not have to know
    which exit produced it."""
    defaults = panel._payload_defaults()
    assert defaults["changed_files"] == []
    assert defaults["changed_files_total"] == 0


# ---- end to end, on both exits ---------------------------------------------

def _gh(monkeypatch, meta_json: str, reviewers: dict) -> None:
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: {
        "github": "o/r", "path": "/tmp/r", "reviewers": reviewers,
        "review_panel": {"skip_title_patterns": ["^Merge "]},
    })
    monkeypatch.setattr(panel, "sh",
                        lambda args, **k: meta_json if "view" in args else "diff")


META_FILES = [{"path": "app/api/reviews.py", "additions": 120, "deletions": 4},
              {"path": "harness/loops/panel.py", "additions": 8, "deletions": 1}]


def _meta_json(title: str, files=META_FILES, total: int = 2) -> str:
    return json.dumps({"title": title, "additions": 128, "deletions": 5,
                       "baseRefName": "main", "headRefName": "h",
                       "headRefOid": "abc", "files": files, "changedFiles": total})


def test_a_reviewed_run_sends_the_paths_with_the_line_count(monkeypatch, capsys):
    reply = '[{"id":"F1","members":[0],"real":true,"reason":"real"}]'
    _gh(monkeypatch, _meta_json("fix: a thing"), {"codex": {"enabled": True}})
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 5))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: (reply, None))
    monkeypatch.setattr(panel, "record_run", lambda p: None)
    panel.run("r", 1701, post=False, json_out=True)
    payload = json.loads(capsys.readouterr().out)
    assert [f["path"] for f in payload["changed_files"]] == [
        "app/api/reviews.py", "harness/loops/panel.py"]
    assert payload["changed_files_total"] == 2
    assert payload["changed_lines"] == 133


def test_a_skipped_pr_still_sends_its_file_list(monkeypatch, capsys):
    """The exit that never fetches a diff, and the one most likely to be merged
    unattended. A skipped PR collides with everything it touches — the paths are
    already in hand from the metadata read, so there is no excuse for the record
    of it to be the one that cannot answer."""
    _gh(monkeypatch, _meta_json("Merge test into main"), {})
    assert panel.run("r", 1702, post=False, json_out=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reviewed"] is False and "skip pattern" in payload["skip_reason"]
    assert [f["path"] for f in payload["changed_files"]] == [
        "app/api/reviews.py", "harness/loops/panel.py"]
    assert payload["changed_files_total"] == 2
    assert payload["changed_lines"] == 133


def test_a_partial_list_is_said_out_loud_and_not_only_implied(monkeypatch, capsys):
    """The two numbers are in the payload either way. This is the copy a human
    reads — the same treatment #75 gave a short panel, for the same reason: a
    shortfall nobody is told about is a shortfall that reads as a clean result."""
    reply = '[{"id":"F1","members":[0],"real":true,"reason":"real"}]'
    _gh(monkeypatch, _meta_json("fix: a big one", total=3000),
        {"codex": {"enabled": True}})
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 5))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: (reply, None))
    monkeypatch.setattr(panel, "record_run", lambda p: None)
    panel.run("r", 1703, post=False, json_out=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed_files_total"] == 3000
    assert len(payload["changed_files"]) == 2
    [note] = [n for n in payload["config_notes"] if "file list came back partial" in n]
    assert "2 of 3,000" in note


def test_a_complete_list_says_nothing(monkeypatch, capsys):
    """The note has to be absent when the list is whole, or it is noise on every
    run and stops being read on the one run it matters."""
    reply = '[{"id":"F1","members":[0],"real":true,"reason":"real"}]'
    _gh(monkeypatch, _meta_json("fix: a thing"), {"codex": {"enabled": True}})
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 5))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: (reply, None))
    monkeypatch.setattr(panel, "record_run", lambda p: None)
    panel.run("r", 1704, post=False, json_out=True)
    payload = json.loads(capsys.readouterr().out)
    assert not [n for n in payload["config_notes"] if "file list" in n]
