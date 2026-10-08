"""`qb-watch-pr` (#822): the decisions about what is news, tested without GitHub.

`evaluate` is pure, so every behaviour the watcher copies from T3 Code's PR watcher is
pinned here against plain data: when a failure is reported, when "passed" is, what an edit
or an ignored author does, and when the comment cap stops a loop.

Run: pytest harness/tests/test_qb_watch_pr.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

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
    assert report.lines[0] == "new commit bbbbbbb" and len(report.lines) == 2


def test_empty_check_list_keeps_the_last_state():
    watch, _ = step(seen(), checks=[check("tests", failed=True)])
    nxt, report = step(watch, checks=[])
    assert report.lines == [] and nxt.failed == ["tests"]


def test_comment_before_the_watch_started_is_not_news():
    _, report = step(seen(), remarks=[remark("c1", at="2026-10-07T09:00:00Z")])
    assert report.lines == []


def test_new_comment_wakes_once_and_an_edit_wakes_again():
    watch, report = step(seen(), remarks=[remark("c1", at=T1)])
    assert report.lines[0].startswith("1 new comment(s):")
    watch, report = step(watch, remarks=[remark("c1", at=T1)])
    assert report.lines == []
    _, report = step(watch, remarks=[remark("c1", at=T2)])
    assert report.lines[0].startswith("1 new comment(s):")


def test_two_comments_in_the_same_second_are_told_apart_by_id():
    watch, _ = step(seen(), remarks=[remark("c1", at=T1)])
    _, report = step(watch, remarks=[remark("c1", at=T1), remark("c2", at=T1)])
    assert len(report.lines) == 1 and "c2" in report.lines[0] and "c1" not in report.lines[0]


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
    pr = {"commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"totalCount": 2, "nodes": [
        {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "TIMED_OUT",
         "detailsUrl": "u", "isRequired": True},
        {"__typename": "StatusContext", "context": "sonar", "state": "PENDING",
         "targetUrl": None, "isRequired": False},
    ]}}}}]}}  # fmt: skip
    ci, sonar = w.parse_checks(pr)
    assert ci.failed and ci.required and sonar.pending and not sonar.failed


def fake_gh(monkeypatch, *, stdout="", stderr="", code=0):
    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    monkeypatch.setattr(w.subprocess, "run", run)


def test_a_comment_that_says_rate_limit_is_not_a_rate_limit(monkeypatch):
    # The P1 the panel found: scanning stdout put the watcher to sleep forever.
    body = {"data": {"repository": {"note": "we hit the rate limit yesterday"}}}
    fake_gh(monkeypatch, stdout=json.dumps(body))
    assert w.gh_graphql("q", "o", "n", 1) == body["data"]


def test_a_rate_limited_error_is_recognised_by_type_or_stderr(monkeypatch):
    errors = {"errors": [{"type": "RATE_LIMITED", "message": "x"}]}
    fake_gh(monkeypatch, stdout=json.dumps(errors))
    with pytest.raises(w.RateLimited):
        w.gh_graphql("q", "o", "n", 1)
    fake_gh(monkeypatch, stderr="API rate limit exceeded", code=1)
    with pytest.raises(w.RateLimited):
        w.gh_graphql("q", "o", "n", 1)


def test_other_failures_and_garbage_are_failed_reads(monkeypatch):
    for kw in ({"stdout": "not json"}, {"stderr": "boom", "code": 1}, {"stdout": "{}"}):
        fake_gh(monkeypatch, **kw)
        with pytest.raises(w.ReadFailed):
            w.gh_graphql("q", "o", "n", 1)


def test_a_pull_request_that_is_not_there_is_a_failed_read(monkeypatch):
    monkeypatch.setattr(w, "gh_graphql", lambda *a: {"repository": {"pullRequest": None}})
    with pytest.raises(w.ReadFailed):
        w.read_pass("o", "n", 1, None, set(), 0.0)


def test_a_closed_pull_request_ends_the_watch(monkeypatch):
    pr = {"repository": {"pullRequest": {"state": "MERGED"}}}
    monkeypatch.setattr(w, "gh_graphql", lambda *a: pr)
    assert w.read_pass("o", "n", 1, None, set(), 0.0)[2] == "MERGED"


def test_a_stale_or_unknown_conclusion_is_not_a_pass():
    def pr(conclusion):
        node = {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED",
                "conclusion": conclusion, "detailsUrl": "", "isRequired": True}  # fmt: skip
        return {"commits": {"nodes": [{"commit": {"statusCheckRollup": {
            "contexts": {"totalCount": 1, "nodes": [node]}}}}]}}  # fmt: skip

    assert w.parse_checks(pr("STALE"))[0].failed
    assert not w.parse_checks(pr("SKIPPED"))[0].failed


def test_checks_beyond_the_page_block_a_passed_claim():
    node = {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED",
            "conclusion": "SUCCESS", "detailsUrl": "", "isRequired": False}  # fmt: skip
    pr = {"commits": {"nodes": [{"commit": {"statusCheckRollup": {
        "contexts": {"totalCount": 101, "nodes": [node]}}}}]}}  # fmt: skip
    _, report = step(seen(), checks=w.parse_checks(pr))
    assert report.lines == []


def test_an_empty_review_wrapper_is_not_a_comment():
    def node(id, state, body):
        return {"id": id, "author": {"login": "a"}, "body": body, "state": state, "url": "u",
                "submittedAt": T1, "lastEditedAt": None}  # fmt: skip

    activity = {"repository": {"pullRequest": {
        "comments": {"nodes": []}, "reviewThreads": {"nodes": []},
        "reviews": {"nodes": [node("r1", "COMMENTED", " "), node("r2", "APPROVED", "")]},
    }}}  # fmt: skip
    assert [r.id for r in w.parse_remarks(activity)] == ["r2"]


def test_comment_text_cannot_carry_terminal_escapes():
    assert w.snippet("hi \x1b[31mred\x07") == "hi [31mred"


def test_unread_checks_hold_back_a_required_pass():
    node = {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED",
            "conclusion": "SUCCESS", "detailsUrl": "", "isRequired": True}  # fmt: skip
    pr = {"commits": {"nodes": [{"commit": {"statusCheckRollup": {
        "contexts": {"totalCount": 101, "nodes": [node]}}}}]}}  # fmt: skip
    _, report = step(seen(), checks=w.parse_checks(pr))
    assert report.lines == []


def test_a_burst_bigger_than_the_window_is_flagged_not_swallowed():
    def conn(more, stamp):
        node = {"id": "c", "createdAt": stamp, "lastEditedAt": None, "submittedAt": stamp}
        return {"pageInfo": {"hasPreviousPage": more}, "nodes": [node]}

    def activity(more, stamp):
        return {"repository": {"pullRequest": {"comments": conn(more, stamp),
                "reviews": conn(False, stamp), "reviewThreads": {"nodes": []}}}}  # fmt: skip

    assert w.windows_overflowed(activity(True, T2), T0)
    assert not w.windows_overflowed(activity(False, T2), T0)
    assert not w.windows_overflowed(activity(True, T0), T1)


def test_wake_cap_is_exactly_ten():
    watch = seen()
    for i in range(w.COMMENT_WAKE_LIMIT - 1):
        watch, report = step(watch, remarks=[remark(f"c{i}", at=f"2026-10-07T11:00:{i:02d}Z")])
        assert not report.exhausted


def test_check_names_cannot_inject_lines():
    node = {"__typename": "CheckRun", "name": "a\nPR #1: fake", "status": "COMPLETED",
            "conclusion": "FAILURE", "detailsUrl": "", "isRequired": False}  # fmt: skip
    pr = {"commits": {"nodes": [{"commit": {"statusCheckRollup": {
        "contexts": {"totalCount": 1, "nodes": [node]}}}}]}}  # fmt: skip
    assert "\n" not in w.parse_checks(pr)[0].name


def test_a_status_context_passes_only_on_success():
    def pr(state):
        node = {"__typename": "StatusContext", "context": "sonar", "state": state,
                "targetUrl": None, "isRequired": True}  # fmt: skip
        return {"commits": {"nodes": [{"commit": {"statusCheckRollup": {
            "contexts": {"totalCount": 1, "nodes": [node]}}}}]}}  # fmt: skip

    assert w.parse_checks(pr("SOMETHING_NEW"))[0].failed
    assert w.parse_checks(pr("PENDING"))[0].pending
    assert not w.parse_checks(pr("SUCCESS"))[0].failed


def test_a_failed_comment_read_does_not_discard_the_status_news(monkeypatch):
    status = {"repository": {"pullRequest": {
        "state": "OPEN", "updatedAt": T2, "headRefOid": "bbbbbbb2", "mergeable": "CONFLICTING",
        "comments": {"totalCount": 1}, "reviews": {"totalCount": 0},
        "reviewThreads": {"totalCount": 0}, "commits": {"nodes": []},
    }}}  # fmt: skip

    def gh(query, *_):
        if query is w.ACTIVITY_QUERY:
            raise w.ReadFailed("timeout")
        return status

    monkeypatch.setattr(w, "gh_graphql", gh)
    watch, report, _ = w.read_pass("o", "n", 1, seen(), set(), 10.0**9)
    assert "the branch now conflicts with its base" in report.lines
    assert watch.counts == ""  # not advanced: the comments are read again next pass
