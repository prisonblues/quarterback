"""Row-building decisions — including the one the browser board also had to make."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mcp_server.board.views import (
    NOT_RECORDED,
    STAGE_UNREPORTED,
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


def test_the_fleet_row_says_how_far_along_the_work_is():
    """The one column that moves. `repo`, `branch` and `title` read identically
    writing the first cut and coming out of the third review round (#262)."""
    rows = fleet_rows({"agents": [
        {"holder": "zeus/a", "repo": "quarterback", "branch": "feat/issue-262",
         "title": "t", "expires": iso(minutes=4), "since": iso(minutes=-10),
         "session": "s1", "stage": "R1F"},
    ]}, NOW)
    assert rows[0]["stage"] == "R1F"


def test_a_lease_that_reported_no_stage_is_not_drawn_as_one():
    """The majority case, and the one a column must not dress up.

    An empty cell reads equally as a clipped column, a rendering fault, or an
    agent with no stage — and "those agents have no stage" is precisely the lie
    #262 is about. `STAGE_UNREPORTED` is not alphanumeric, and a stage is 1-6
    alphanumerics by construction, so the two cannot be confused.
    """
    rows = fleet_rows({
        "agents": [{"holder": "zeus/a", "expires": iso(minutes=4),
                    "since": iso(minutes=-10), "session": "s1"}],
        "subagents": [{"holder": "zeus/a", "label": "Explore", "expires": iso(minutes=2),
                       "since": iso(minutes=-1), "parent_session": "s1"}],
    }, NOW)
    assert [r["stage"] for r in rows] == [STAGE_UNREPORTED, STAGE_UNREPORTED]
    assert not STAGE_UNREPORTED.isalnum()


def test_a_sub_agent_is_not_given_its_parents_stage():
    """The same rule `repo` and `branch` follow. The fan-out of an `R1F` fix pass
    is the clearest case for inheriting one, and it is still an invention —
    nothing on the board said it about the sub-agent."""
    rows = fleet_rows({
        "agents": [{"holder": "zeus/a", "expires": iso(minutes=4), "session": "s1",
                    "since": iso(minutes=-10), "stage": "R1F"}],
        "subagents": [{"holder": "zeus/a", "label": "fix", "expires": iso(minutes=2),
                       "since": iso(minutes=-1), "parent_session": "s1"}],
    }, NOW)
    assert rows[0]["stage"] == "R1F"
    assert rows[1]["stage"] == STAGE_UNREPORTED


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
    # The judge never ruled — which is what the words say, and what a dash does not.
    # The dash is spent elsewhere in this row, on a missing effort, where it means
    # "none" rather than "unknown".
    assert claude["precision"] == NOT_RECORDED
    assert claude["per_run"] == NOT_RECORDED
    assert claude["effort"] == "—"


def test_panel_window_says_what_the_numbers_are_over():
    line = panel_window({"runs": 12, "prs": 5, "repos": 2,
                         "window": {"repo": "prisonblues/quarterback", "judged_only": True}})
    assert "12 run(s)" in line and "prisonblues/quarterback" in line and "judged runs only" in line


def test_a_window_that_does_not_say_which_runs_it_counted_is_not_labelled_judged():
    """An older stats payload has made no claim, and defaulting to True invents one."""
    line = panel_window({"runs": 12, "prs": 5, "repos": 2, "window": {}})
    assert NOT_RECORDED in line
    assert "judged runs only" not in line and "all runs" not in line
    assert "all runs" in panel_window({"window": {"judged_only": False}})


def test_a_timestamp_of_the_wrong_type_is_unparseable_not_an_exception():
    """`fromisoformat(1755370000)` raises TypeError, which no caller here catches."""
    assert age(1755370000, NOW) == "?"
    assert age(True, NOW) == "?"
    assert ttl(12.5, NOW) == "?"


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


def test_an_anonymous_reply_is_not_evidence_that_i_answered():
    """The two None cases are not symmetric, and collapsing them clears live alerts.

    An unknown *me* means every reply counts (an alert nobody can clear is one
    nobody reads); an unknown *author* means no reply counts, because a post whose
    `from` failed to decode is not mine and the ask it answers is still open.
    """
    assert answers_for(None, "zeus") is False
    inbox = [{"id": 10, "type": "ask", "from": "atlas/x"}]
    seen = [{"id": 12, "type": "ack", "re": 10, "from": None}]
    assert [p["id"] for p in unanswered_asks(inbox, seen, "zeus")] == [10]


def test_an_ask_stays_answered_when_the_two_payloads_spell_the_id_differently():
    """`re` and `id` arrive from /board and from /stream; only their meaning is shared.

    A string `"10"` never matched an integer `10`, so the ask came back as pending
    every refresh and could not be cleared by answering it again.
    """
    inbox = [{"id": 10, "type": "ask", "from": "zeus/b"}]
    seen = [{"id": 12, "type": "ack", "re": "10", "from": "zeus/me"}]
    assert unanswered_asks(inbox, seen, "zeus/me") == []


def test_non_ask_mail_is_not_a_pending_ask():
    inbox = [{"id": 10, "type": "note", "from": "zeus/b"}]
    assert unanswered_asks(inbox, [], "zeus/me") == []


def test_staleness_passes_the_boards_own_advice_through():
    """One wording of "you are behind" across the fleet, not a second one here."""
    stale, line = staleness({"stale": True, "advice": "quarterback: pull abc123"})
    assert stale is True and line == "quarterback: pull abc123"
    assert staleness({"stale": False, "advice": None}) == (False, "in sync")
