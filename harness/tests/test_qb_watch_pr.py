"""`qb-watch-pr` (#822): the decisions about what is news, tested without GitHub.

`evaluate` is pure, so every behaviour the watcher copies from T3 Code's PR watcher is
pinned here against plain data: when a failure is reported, when "passed" is, what an edit
or an ignored author does, and when the comment cap stops a loop.

Run: pytest harness/tests/test_qb_watch_pr.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "qb-watch-pr"
_loader = importlib.machinery.SourceFileLoader("qb_watch_pr", str(SCRIPT))
_spec = importlib.util.spec_from_loader("qb_watch_pr", _loader)
w = importlib.util.module_from_spec(_spec)
sys.modules["qb_watch_pr"] = w
_loader.exec_module(w)

T0 = "2026-10-07T10:00:00Z"
T1 = "2026-10-07T10:05:00Z"
T2 = "2026-10-07T10:10:00Z"


def check(name, *, failed=False, pending=False, required=False):
    return w.Check(name, failed, pending, required, f"https://ci/{name}")


def remark(id, author="alice", at=T1, body="looks off"):
    return w.Remark(id, author, at, body, f"https://gh/{id}")


def step(watch, *, head="aaaaaaa1", checks=(), mergeable="MERGEABLE", remarks=None, ignored=()):
    return w.evaluate(
        watch, head_sha=head, checks=list(checks), mergeable=mergeable, remarks=remarks,
        ignored=set(ignored),
    )  # fmt: skip


def seen(head="aaaaaaa1", **kw):
    return w.Watch(head_sha=head, remarks_through=T0, **kw)


def test_failed_check_is_reported_while_a_slow_bot_is_still_pending():
    _, report = step(seen(), checks=[check("tests", failed=True), check("bot", pending=True)])
    assert report.lines == ["checks failed: tests https://ci/tests"]


def test_failed_check_is_not_reported_twice():
    watch, _ = step(seen(), checks=[check("tests", failed=True)])
    _, report = step(watch, checks=[check("tests", failed=True)])
    assert report.lines == []


def test_rerun_that_fails_again_is_reported_again():
    watch, _ = step(seen(), checks=[check("tests", failed=True)])
    watch, _ = step(watch, checks=[check("tests", pending=True)])
    _, report = step(watch, checks=[check("tests", failed=True)])
    assert len(report.lines) == 1


def test_passed_waits_for_required_checks_and_ignores_advisory_ones():
    checks = [check("tests", required=True), check("bot", pending=True)]
    _, report = step(seen(), checks=checks)
    assert report.lines == ["all 1 required checks passed on aaaaaaa"]


def test_passed_is_told_once():
    watch, _ = step(seen(), checks=[check("tests")])
    _, report = step(watch, checks=[check("tests")])
    assert report.lines == []


def test_no_required_checks_means_every_check_must_finish():
    _, report = step(seen(), checks=[check("a"), check("b", pending=True)])
    assert report.lines == []


def test_new_push_forgets_what_was_told_about_the_old_head():
    watch, _ = step(seen(), checks=[check("tests", failed=True)])
    _, report = step(watch, head="bbbbbbb2", checks=[check("tests", failed=True)])
    assert len(report.lines) == 1


def test_empty_check_list_keeps_the_last_state():
    watch, _ = step(seen(), checks=[check("tests", failed=True)])
    nxt, report = step(watch, checks=[])
    assert report.lines == [] and nxt.failed == ["tests"]


def test_comment_before_the_watch_started_is_not_news():
    _, report = step(seen(), remarks=[remark("c1", at="2026-10-07T09:00:00Z")])
    assert report.lines == []


def test_new_comment_wakes_once_and_an_edit_wakes_again():
    watch, report = step(seen(), remarks=[remark("c1", at=T1)])
    assert report.lines[0] == "1 new comment(s):"
    watch, report = step(watch, remarks=[remark("c1", at=T1)])
    assert report.lines == []
    _, report = step(watch, remarks=[remark("c1", at=T2)])
    assert report.lines[0] == "1 new comment(s):"


def test_two_comments_in_the_same_second_are_told_apart_by_id():
    watch, _ = step(seen(), remarks=[remark("c1", at=T1)])
    _, report = step(watch, remarks=[remark("c1", at=T1), remark("c2", at=T1)])
    assert report.lines[0] == "1 new comment(s):" and "alice" in report.lines[1]


def test_ignored_author_never_wakes():
    _, report = step(seen(), remarks=[remark("c1", author="Bot")], ignored={"bot"})
    assert report.lines == []


def test_comments_not_read_this_pass_leave_the_cursor_alone():
    watch, _ = step(seen(), remarks=[remark("c1", at=T1)])
    nxt, _ = step(watch, remarks=None)
    assert nxt.remarks_through == T1 and nxt.remark_ids == ["c1"]


def test_conflict_is_reported_once_and_unknown_keeps_the_answer():
    watch, report = step(seen(), mergeable="CONFLICTING")
    assert report.lines == ["the branch now conflicts with its base"]
    watch, report = step(watch, mergeable="UNKNOWN")
    assert report.lines == [] and watch.conflicting
    watch, _ = step(watch, mergeable="MERGEABLE")
    assert not watch.conflicting


def test_ten_comment_only_wakes_in_a_row_exhaust_the_watch():
    watch = seen()
    for i in range(w.COMMENT_WAKE_LIMIT):
        watch, report = step(watch, remarks=[remark(f"c{i}", at=f"2026-10-07T11:00:{i:02d}Z")])
    assert report.exhausted


def test_check_news_resets_the_comment_wake_count():
    watch = seen(wakes=9)
    watch, report = step(watch, checks=[check("tests", failed=True)])
    assert watch.wakes == 0 and not report.exhausted


def test_parse_checks_reads_check_runs_and_status_contexts():
    pr = {"commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": [
        {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "TIMED_OUT",
         "detailsUrl": "u", "isRequired": True},
        {"__typename": "StatusContext", "context": "sonar", "state": "PENDING",
         "targetUrl": None, "isRequired": False},
    ]}}}}]}}  # fmt: skip
    ci, sonar = w.parse_checks(pr)
    assert ci.failed and ci.required and sonar.pending and not sonar.failed
