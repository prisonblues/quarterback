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
import panel_core  # noqa: E402  — `sh` is defined here since #129
import panel_seats  # noqa: E402  — run_cli lives here since #129
from conftest import gh_stub  # noqa: E402


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
    files, total, dropped = panel._changed_files(
        meta(files=[gh_file("app/api/reviews.py", 120, 4),
                    gh_file("app/models/review.py", 40, 0)], total=2))
    assert files == [
        {"path": "app/api/reviews.py", "additions": 120, "deletions": 4},
        {"path": "app/models/review.py", "additions": 40, "deletions": 0},
    ]
    assert (total, dropped) == (2, 0)


def test_paths_come_back_sorted_so_two_runs_of_one_pr_compare_directly():
    files, _, _ = panel._changed_files(
        meta(files=[gh_file("z.py"), gh_file("a.py"), gh_file("m.py")], total=3))
    assert [f["path"] for f in files] == ["a.py", "m.py", "z.py"]


def test_a_pr_that_changed_nothing_is_an_empty_list_and_not_an_error():
    """And `total` is 0, not None: GitHub counted and the answer was none. That is
    knowledge, and the board treats it as such — such a PR is disjoint from
    everything rather than unanswerable."""
    assert panel._changed_files(meta(files=[], total=0)) == ([], 0, 0)


def test_a_blank_path_is_dropped_and_counted_separately():
    """A path is the join key of every collision query built on this. An empty one
    joins to nothing and counts as a file, which is the worst of both.

    `dropped` is returned rather than left to be inferred from the arithmetic,
    because "GitHub paged us short" and "we discarded a malformed row" are two
    different facts with two different fixes — and the partial-list warning is
    only about the first. Inferring it fired the truncation warning on any PR
    with one junk entry (round 1, F16)."""
    files, total, dropped = panel._changed_files(
        meta(files=[gh_file("real.py"), {"path": "  ", "additions": 0, "deletions": 0}],
             total=2))
    assert [f["path"] for f in files] == ["real.py"]
    # The TOTAL still says two, because GitHub said two — and `dropped` accounts
    # for the difference, so nothing has to guess whether the list was truncated.
    assert (total, dropped) == (2, 1)


def test_a_file_missing_its_line_counts_stays_null_rather_than_zero():
    """Rewritten after round 1 (F13): this asserted `0` and was wrong.

    The board's churn columns are nullable on purpose, and the release's whole
    argument is that "absent" and "zero" are different facts. Recording an
    unstated count as 0 makes a file GitHub said nothing about indistinguishable
    from a pure-deletion file — and, worse, made the panel and a hand-rolled
    caller use two different conventions for the same column."""
    files, _, _ = panel._changed_files(meta(files=[{"path": "x.py"}], total=1))
    assert files == [{"path": "x.py", "additions": None, "deletions": None}]


# ---- the count is not derived from the list --------------------------------

def test_the_total_is_githubs_count_and_not_the_lists_length():
    """The property the whole feature rests on. GitHub caps a PR's file list at
    3,000; a caller told only `len(files)` cannot tell a 3,000-file prefix from a
    3,000-file PR, and every collision answer built on the prefix is wrong in the
    direction of "no collision"."""
    files, total, _ = panel._changed_files(
        meta(files=[gh_file("a.py"), gh_file("b.py")], total=2500))
    assert len(files) == 2
    assert total == 2500


def test_an_unstated_count_is_none_and_never_the_lists_length():
    """Rewritten after round 1 (F17): this asserted `total == len(files)` under the
    name "falls back to what can be proved", and agreeing by construction is not
    proof — it stamps a possibly-truncated list "complete" on no evidence.

    None travels to a NULL column that already means "nobody said", which is the
    only honest value and the one the collision endpoint can act on."""
    files, total, _ = panel._changed_files(meta(files=[gh_file("a.py")]))
    assert len(files) == 1
    assert total is None


def test_no_files_field_at_all_is_no_list_rather_than_no_files():
    """Rewritten after round 1 (F17): this asserted `total == 0` while its own name
    said the opposite, and the assertion was what the code did — turning an
    unknown file list into a known zero-file PR, which `/review/collisions` then
    cannot report as unanswered."""
    files, total, dropped = panel._changed_files(meta())
    assert files == []
    assert total is None
    assert dropped == 0


def test_a_non_numeric_count_degrades_instead_of_killing_the_run():
    """F18. `int(total)` on an unexpected `gh` shape raised inside `run()` after
    the PR read and before any review — an uncaught traceback in a neighbourhood
    written throughout to degrade rather than crash."""
    assert panel._changed_files(meta(files=[], total="lots"))[1] is None
    assert panel._changed_files(meta(files=[], total={"n": 3}))[1] is None


# ---- the payload -----------------------------------------------------------

def test_every_payload_carries_the_keys_even_when_the_run_never_got_that_far():
    """`_payload_defaults` is the shape contract: the skip path emits a payload
    too, and a consumer reading `payload['changed_files']` must not have to know
    which exit produced it."""
    defaults = panel._payload_defaults()
    assert defaults["changed_files"] == []
    # None, not 0 (F22). This structure describes a run that never got that far,
    # so the one thing it must not assert is "this PR changed zero files".
    assert defaults["changed_files_total"] is None
    assert defaults["pr_state"] is None and defaults["is_draft"] is None


# ---- end to end, on both exits ---------------------------------------------

def _gh(monkeypatch, meta_json: str, reviewers: dict) -> None:
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: {
        "github": "o/r", "path": "/tmp/r", "reviewers": reviewers,
        "_rules_baseline": ".harness-rules.sample",
        "review_panel": {"skip_title_patterns": ["^Merge "]},
    })
    monkeypatch.setattr(panel_core, "sh", gh_stub(meta=json.loads(meta_json), diff="diff"))


META_FILES = [{"path": "app/api/reviews.py", "additions": 120, "deletions": 4},
              {"path": "harness/loops/panel.py", "additions": 8, "deletions": 1}]


def _meta_json(title: str, files=META_FILES, total: int = 2,
               state: str = "OPEN", draft: bool = False) -> str:
    return json.dumps({"title": title, "additions": 128, "deletions": 5,
                       "baseRefName": "main", "headRefName": "h",
                       "headRefOid": "abc", "files": files, "changedFiles": total,
                       "state": state, "isDraft": draft})


def test_a_reviewed_run_sends_the_paths_with_the_line_count(monkeypatch, capsys):
    reply = '[{"id":"F1","members":[0],"real":true,"reason":"real"}]'
    _gh(monkeypatch, _meta_json("fix: a thing"), {"codex": {"enabled": True}})
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 5))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel_seats, "run_cli", lambda *a, **k: (reply, None))
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


def test_a_skipped_pr_reaches_the_board(monkeypatch, capsys, recorded_runs):
    """#94, and the half that was missing. Emitting the file list was never the
    problem — this path has carried a complete one for releases. It returned
    before `record_run`, so the list reached `--json` and the next round's
    `--baseline` and never the board, and `GET /review/collisions` saw a skipped
    PR as neither subject nor rival: no row to be a subject, no row to be a rival.

    What is recorded is not a review. The payload says `reviewed: false` with the
    reason beside it, and it names no reviewers — so it contributes no scorecard
    and no finding, and every per-reviewer statistic is untouched by construction
    rather than by a filter downstream."""
    _gh(monkeypatch, _meta_json("Merge test into main"), {})
    assert panel.run("r", 1707, post=False, json_out=True) == 0

    assert len(recorded_runs) == 1, "the skipped round never reached the board"
    sent = recorded_runs[0]
    assert sent["reviewed"] is False
    assert "skip pattern" in sent["skip_reason"]
    # The reason it is worth recording at all.
    assert [f["path"] for f in sent["changed_files"]] == [
        "app/api/reviews.py", "harness/loops/panel.py"]
    assert sent["changed_files_total"] == 2
    # And the reason recording it is not the disease it was avoiding: nothing
    # here can become a scorecard or a finding.
    assert not sent.get("reviewers_selected")
    assert not sent.get("reviewers")
    assert sent["to_fix"] == [] and sent["dismissed"] == []


def test_a_skipped_pr_the_board_would_not_take_says_so_in_the_payload(
        monkeypatch, capsys, recorded_runs):
    """#284's rule reaches this exit too, and the ordering is what makes it work:
    the record is attempted BEFORE `--json-file` is written and before the payload
    is printed, so `config_notes` carries the miss in the artefact on disk as well
    as on the wire. Recorded afterwards, the note would exist only in a stderr
    line inside a subprocess nobody reads."""
    monkeypatch.setattr(panel, "record_run",
                        lambda p: "this round was NOT recorded on the board — no `qb`")
    _gh(monkeypatch, _meta_json("Merge test into main"), {})
    assert panel.run("r", 1708, post=False, json_out=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any("NOT recorded on the board" in n for n in payload["config_notes"])


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
    monkeypatch.setattr(panel_seats, "run_cli", lambda *a, **k: (reply, None))
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
    monkeypatch.setattr(panel_seats, "run_cli", lambda *a, **k: (reply, None))
    monkeypatch.setattr(panel, "record_run", lambda p: None)
    panel.run("r", 1704, post=False, json_out=True)
    payload = json.loads(capsys.readouterr().out)
    assert not [n for n in payload["config_notes"] if "file list" in n]


def test_the_pr_state_travels_with_the_file_list(monkeypatch, capsys):
    """Without it a collision query cannot tell a live rival from one merged last
    week, and reports both. Same `gh pr view` call, one field wider — the state is
    as of THIS panel, which is why the payload's timestamp matters."""
    reply = '[{"id":"F1","members":[0],"real":true,"reason":"real"}]'
    _gh(monkeypatch, _meta_json("fix: a thing", state="OPEN", draft=True),
        {"codex": {"enabled": True}})
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 5))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel_seats, "run_cli", lambda *a, **k: (reply, None))
    monkeypatch.setattr(panel, "record_run", lambda p: None)
    panel.run("r", 1705, post=False, json_out=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["pr_state"] == "OPEN" and payload["is_draft"] is True


def test_a_skipped_pr_carries_the_partial_list_warning_too(monkeypatch, capsys):
    """F19, and the reason it was a finding rather than a nitpick: the note was
    built forty lines BELOW the skip branch's early return, so the one exit this
    release argues is most likely to be merged unattended was the one exit that
    said nothing. Four reviewers raised it."""
    _gh(monkeypatch, _meta_json("Merge test into main", total=3000), {})
    assert panel.run("r", 1706, post=False, json_out=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reviewed"] is False
    [note] = [n for n in payload["config_notes"] if "file list came back partial" in n]
    assert "2 of 3,000" in note
    assert payload["pr_state"] == "OPEN"


def test_a_dropped_entry_does_not_masquerade_as_a_truncated_list(monkeypatch, capsys):
    """F16. Discarding one junk entry left `total` at GitHub's count, so the
    arithmetic said "partial" and the run warned that collision queries would
    under-report — when nothing had been truncated at all. Two different facts,
    two different fixes, and only one of them is GitHub paging us short."""
    reply = '[{"id":"F1","members":[0],"real":true,"reason":"real"}]'
    _gh(monkeypatch, _meta_json("fix: a thing",
                                files=[*META_FILES, {"path": "   "}], total=3),
        {"codex": {"enabled": True}})
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 5))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel_seats, "run_cli", lambda *a, **k: (reply, None))
    monkeypatch.setattr(panel, "record_run", lambda p: None)
    panel.run("r", 1707, post=False, json_out=True)
    notes = json.loads(capsys.readouterr().out)["config_notes"]
    assert not [n for n in notes if "came back partial" in n]
    assert [n for n in notes if "no usable path" in n]


def test_a_malformed_file_entry_does_not_kill_the_run():
    """Round 2, F23. `f.get("path")` raised AttributeError on a bare string,
    number or null in the array, and `.strip()` raised on a numeric path — inside
    `run()`, after the PR read and before any review. The board end of this same
    field coerces exactly these shapes into a droppable row, and the two ends of
    one field should not disagree about what they tolerate."""
    files, total, dropped = panel._changed_files(
        meta(files=["bare string", 42, None, {"path": 7}, gh_file("real.py")], total=5))
    assert [f["path"] for f in files] == ["real.py"]
    assert (total, dropped) == (5, 4)
