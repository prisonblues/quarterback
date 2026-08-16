"""Row-building decisions — including the one the browser board also had to make."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mcp_server.board.views import (
    NOT_RECORDED,
    age,
    answers_for,
    fleet_rows,
    panel_rows,
    panel_window,
    session_rows,
    staleness,
    ttl,
    unanswered_asks,
)

NOW = datetime(2026, 8, 16, 21, 0, 0, tzinfo=UTC)


def iso(**delta):
    return (NOW + timedelta(**delta)).isoformat()


def test_age_buckets():
    assert age(iso(seconds=-12), NOW) == "12s"
    assert age(iso(minutes=-4), NOW) == "4m"
    assert age(iso(hours=-2, minutes=-13), NOW) == "2h13m"
    assert age(iso(days=-3), NOW) == "3d"
    assert age(None, NOW) == "?"
    assert age("not a timestamp", NOW) == "?"


def test_a_lapsed_lease_says_expired_rather_than_a_negative_age():
    """The board only returns live leases, so this means it lapsed mid-render."""
    assert ttl(iso(seconds=-1), NOW) == "expired"
    assert ttl(iso(minutes=5), NOW) == "5m"
    assert ttl(iso(hours=1, minutes=2), NOW) == "1h02m"


def test_naive_timestamps_are_read_as_utc():
    assert age("2026-08-16T20:59:30", NOW) == "30s"


def test_fleet_lists_agents_before_their_fan_out_and_marks_your_own():
    active = {
        "agents": [
            {"holder": "zeus/a", "device": "zeus", "repo": "quarterback", "branch": "main",
             "title": "t", "expires": iso(minutes=4), "since": iso(minutes=-10), "own": True,
             "session": "s1"}
        ],
        "subagents": [
            {"holder": "zeus/a", "device": "zeus", "label": "Explore",
             "expires": iso(minutes=2), "since": iso(minutes=-1), "parent_session": "s1"}
        ],
    }
    rows = fleet_rows(active, NOW)
    assert [r["kind"] for r in rows] == ["agent", "subagent"]
    assert rows[0]["own"] is True and rows[0]["ttl"] == "4m"
    # A sub-agent inherits its parent's checkout and the board records no repo for
    # it, so the column stays empty rather than being invented.
    assert rows[1]["repo"] == "" and rows[1]["title"] == "Explore" and rows[1]["ttl"] == "2m"


def test_sessions_fall_back_to_the_directory_name_when_untitled():
    rows = session_rows(
        [{"session": "abc", "cwd": "/home/rich/source/quarterback", "live": False,
          "resumable": True, "updated_at": iso(minutes=-30), "size": 4096}],
        NOW,
    )
    assert rows[0]["title"] == "quarterback"
    assert rows[0]["size"] == "4k"
    assert rows[0]["age"] == "30m"


def test_a_session_with_no_transcript_size_says_so_rather_than_zero():
    rows = session_rows([{"session": "a", "size": None, "updated_at": iso()}], NOW)
    assert rows[0]["size"] == "—"


def test_null_is_not_zero_in_the_panel_view():
    """A vendor that does not state a price is not a free vendor."""
    stats = {
        "by_model": [
            {"reviewer": "codex", "model": "gpt", "effort": "max", "runs": 3, "ran": 3,
             "confirmed": 4, "dismissed": 1, "precision": 0.8, "confirmed_per_run": 1.33,
             "total_tokens": 1234, "cost_usd": None, "cost_runs": 0, "token_runs": 3},
            {"reviewer": "claude", "model": "opus", "effort": None, "runs": 3, "ran": 3,
             "confirmed": 4, "dismissed": 0, "precision": None, "confirmed_per_run": None,
             "total_tokens": None, "cost_usd": 0.0, "cost_runs": 3, "token_runs": 0},
        ]
    }
    codex, claude = panel_rows(stats)
    assert codex["cost"] == NOT_RECORDED
    assert codex["tokens"] == "1,234"
    assert codex["precision"] == "80%"
    # Zero cost renders as a price, and is visibly a different claim from silence.
    assert claude["cost"] == "$0.0000"
    assert claude["tokens"] == NOT_RECORDED
    assert claude["precision"] == "—"  # the judge never ruled, not "always wrong"


def test_panel_window_says_what_the_numbers_are_over():
    line = panel_window({"runs": 12, "prs": 5, "repos": 2,
                         "window": {"repo": "prisonblues/quarterback", "judged_only": True}})
    assert "12 run(s)" in line and "prisonblues/quarterback" in line and "judged runs only" in line


def test_an_ask_i_have_answered_stops_counting():
    inbox = [{"id": 10, "type": "ask", "from": "zeus/b", "summary": "?"},
             {"id": 11, "type": "ask", "from": "zeus/c", "summary": "??"}]
    seen = [{"id": 12, "type": "ack", "re": 10, "from": "zeus/me"}]
    pending = unanswered_asks(inbox, seen, "zeus/me")
    assert [p["id"] for p in pending] == [11]


def test_someone_elses_ack_does_not_answer_my_ask():
    inbox = [{"id": 10, "type": "ask", "from": "zeus/b"}]
    seen = [{"id": 12, "type": "ack", "re": 10, "from": "zeus/someone-else"}]
    assert len(unanswered_asks(inbox, seen, "zeus/me")) == 1


def test_a_machine_identity_is_answered_by_any_of_its_agents():
    """`?to=@me` for a bare machine returns its agents' mail, so their replies count.

    Without this the terminal client, which identifies as the machine, reads
    every ask any agent on the box already answered as still outstanding — 20 of
    them on the first real run, and an alert nobody can clear is one nobody reads.
    """
    inbox = [{"id": 10, "type": "ask", "from": "atlas/x"},
             {"id": 11, "type": "ask", "from": "atlas/x"}]
    seen = [{"id": 12, "type": "ack", "re": 10, "from": "zeus/heron-sandy"},
            {"id": 13, "type": "ack", "re": 11, "from": "hermes/other"}]
    assert [p["id"] for p in unanswered_asks(inbox, seen, "zeus")] == [11]


def test_answers_for_is_hierarchical_in_one_direction_only():
    # zeus/a answers zeus's mail; zeus does NOT answer zeus/a's.
    assert answers_for("zeus/a", "zeus") is True
    assert answers_for("zeus", "zeus/a") is False
    assert answers_for("zeus/a", "zeus/a") is True
    assert answers_for("zeus-two/a", "zeus") is False  # not a path boundary
    assert answers_for("anyone", None) is True  # identity unknown: don't over-report


def test_non_ask_mail_is_not_a_pending_ask():
    inbox = [{"id": 10, "type": "note", "from": "zeus/b"}]
    assert unanswered_asks(inbox, [], "zeus/me") == []


def test_staleness_passes_the_boards_own_advice_through():
    """One wording of "you are behind" across the fleet, not a second one here."""
    stale, line = staleness({"stale": True, "advice": "quarterback: pull abc123"})
    assert stale is True and line == "quarterback: pull abc123"
    assert staleness({"stale": False, "advice": None}) == (False, "in sync")
