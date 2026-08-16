"""The tail: SSE decoding, what gets printed, and surviving a dropped connection."""

from __future__ import annotations

import io

import httpx
import pytest
from conftest import FakeClient
from mcp_server.board.follow import follow, wants
from mcp_server.client import sse_events


def post(pid, **kw):
    base = {"id": pid, "ts": "2026-08-16T20:35:12+00:00", "from": "zeus/a", "type": "status",
            "summary": f"post {pid}"}
    return {**base, **kw}


# -- SSE decoding ------------------------------------------------------


def test_sse_decoder_reads_the_data_field_only():
    lines = ["event: post", "id: 7", 'data: {"id": 7}', ""]
    assert list(sse_events(lines)) == [{"id": 7}]


def test_keep_alive_comments_are_not_events():
    """sse-starlette pings every few seconds; a ping is not a post."""
    lines = [": ping - 2026-08-16", "", 'data: {"id": 1}', ""]
    assert list(sse_events(lines)) == [{"id": 1}]


def test_multi_line_data_is_rejoined():
    assert list(sse_events(["data: {", 'data: "id": 2}', ""])) == [{"id": 2}]


def test_a_malformed_frame_is_skipped_not_raised():
    """One bad frame must not end a tail that has been running for a day."""
    events = list(sse_events(["data: not json", "", 'data: {"id": 3}', ""]))
    assert events == [{"id": 3}]


def test_trailing_frame_without_a_blank_line_is_dropped():
    # An incomplete frame is exactly what a truncated connection leaves behind.
    assert list(sse_events(['data: {"id": 4}'])) == []


# -- filtering ---------------------------------------------------------


def test_presence_is_hidden_unless_asked_for():
    heartbeat = post(1, type="presence")
    assert wants(heartbeat, None, None, presence=False) is False
    assert wants(heartbeat, None, None, presence=True) is True


def test_naming_presence_as_a_type_is_itself_asking_for_it():
    assert wants(post(1, type="presence"), {"presence"}, None, presence=False) is True


def test_type_and_recipient_filters():
    p = post(1, type="finding", to="zeus/b")
    assert wants(p, {"finding"}, None, False) is True
    assert wants(p, {"ask"}, None, False) is False
    assert wants(p, None, "zeus/b", False) is True
    assert wants(p, None, "zeus/c", False) is False


def test_the_recipient_filter_is_hierarchical_like_the_boards_own():
    """`--to zeus` must mean the same thing live as it did in the backlog.

    The backlog comes from `/board?to=`, which is hierarchical; `/stream` carries
    no filter, so the live half is filtered here. Exact matching made the same
    post appear in one and not the other.
    """
    assert wants(post(1, to="zeus/b"), None, "zeus", False) is True      # to an agent of mine
    assert wants(post(1, to="zeus"), None, "zeus/b", False) is True      # to my machine
    assert wants(post(1, to="hermes/b"), None, "zeus", False) is False
    assert wants(post(1), None, "zeus", False) is False                  # addressed to nobody


# -- the loop ----------------------------------------------------------


def run_follow(client, **kw):
    out, err = io.StringIO(), io.StringIO()
    code = follow(
        client, "https://board.example", out=out, err=err, colour=False,
        sleep=lambda _s: None, env={"HOME": kw.pop("home", "/nonexistent")}, **kw
    )
    return code, out.getvalue(), err.getvalue()


def test_backlog_is_printed_then_the_stream(tmp_path):
    client = FakeClient(board=[post(1), post(2)], batches=[[post(3)]])
    code, out, _ = run_follow(client, max_reconnects=0, home=str(tmp_path))
    assert code == 0
    assert [ln.split()[1] for ln in out.strip().splitlines()] == ["#1", "#2", "#3"]


def test_backlog_is_bounded_by_n_rather_than_replayed_through_the_stream(tmp_path):
    client = FakeClient(board=[post(1)], batches=[[]])
    run_follow(client, tail=5, max_reconnects=0, home=str(tmp_path))
    assert client.board_calls[0]["limit"] == 5
    # And the stream starts *after* the backlog, so nothing is printed twice.
    assert client.stream_calls == [1]


def test_since_skips_the_backlog_entirely(tmp_path):
    client = FakeClient(board=[post(1)], batches=[[]])
    run_follow(client, since=50, max_reconnects=0, home=str(tmp_path))
    assert client.board_calls == []
    assert client.stream_calls == [50]


def test_a_dropped_connection_resumes_from_the_cursor(tmp_path):
    """The point of a tail is being left running; an overnight drop must resume."""
    client = FakeClient(
        board=[],
        batches=[[post(1), post(2)], httpx.ReadTimeout("stalled"), [post(3)]],
    )
    code, out, err = run_follow(client, tail=0, max_reconnects=3, home=str(tmp_path))
    assert code == 0
    # Resumed at the last id seen, never back at 0 — that is the difference
    # between resuming and replaying the day.
    assert client.stream_calls[:4] == [0, 2, 2, 3]
    assert [ln.split()[1] for ln in out.strip().splitlines()] == ["#1", "#2", "#3"]
    assert "reconnecting" in err


def test_a_rejected_token_is_fatal_rather_than_retried_forever(tmp_path):
    error = httpx.HTTPStatusError(
        "401", request=httpx.Request("GET", "https://board.example/stream"),
        response=httpx.Response(401),
    )
    client = FakeClient(board=[], batches=[error])
    code, _, err = run_follow(client, tail=0, max_reconnects=5, home=str(tmp_path))
    assert code == 1
    assert "token" in err


def test_a_server_error_is_retried_not_fatal(tmp_path):
    error = httpx.HTTPStatusError(
        "503", request=httpx.Request("GET", "https://board.example/stream"),
        response=httpx.Response(503),
    )
    client = FakeClient(board=[], batches=[error, [post(9)]])
    code, out, _ = run_follow(client, tail=0, max_reconnects=3, home=str(tmp_path))
    assert code == 0 and "#9" in out


def test_the_cursor_is_persisted_so_resume_picks_up_where_it_stopped(tmp_path):
    client = FakeClient(board=[], batches=[[post(11), post(12)]])
    run_follow(client, tail=0, max_reconnects=0, home=str(tmp_path))
    written = list((tmp_path / ".local" / "state" / "quarterback").glob("board-cursor-*"))
    assert len(written) == 1
    assert written[0].read_text().strip() == "12"


def test_presence_still_advances_the_cursor_even_though_it_is_not_printed(tmp_path):
    """Otherwise a quiet board of heartbeats replays them on every reconnect."""
    client = FakeClient(board=[], batches=[[post(5, type="presence")]])
    _code, out, _err = run_follow(client, tail=0, max_reconnects=1, home=str(tmp_path))
    assert out.strip() == ""
    assert client.stream_calls == [0, 5]


def test_a_board_that_is_down_at_startup_still_follows(tmp_path):
    """The backlog is a convenience; failing to fetch it must not stop the tail."""

    class Broken(FakeClient):
        def board(self, params):
            raise httpx.ConnectError("boom")

    client = Broken(board=[], batches=[[post(1)]])
    code, out, err = run_follow(client, max_reconnects=0, home=str(tmp_path))
    assert code == 0 and "#1" in out and "boom" in err


def test_a_closed_reader_ends_the_tail_quietly(tmp_path):
    """`qb board --follow | head -20` is ordinary use, and head closing is how it ends."""

    class ClosedPipe(io.StringIO):
        def write(self, _s):
            raise BrokenPipeError(32, "Broken pipe")

    client = FakeClient(board=[], batches=[[post(1)]])
    code = follow(
        client, "https://board.example", tail=0, out=ClosedPipe(), err=io.StringIO(),
        colour=False, sleep=lambda _s: None, max_reconnects=0,
        env={"HOME": str(tmp_path)},
    )
    assert code == 0  # not a traceback, and not a non-zero exit


@pytest.mark.parametrize("kind", ["ask", "finding"])
def test_a_single_type_filter_is_pushed_to_the_server(tmp_path, kind):
    client = FakeClient(board=[], batches=[[]])
    run_follow(client, types=[kind], max_reconnects=0, home=str(tmp_path))
    assert client.board_calls[0]["type"] == kind
