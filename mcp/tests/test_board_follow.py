"""The tail: SSE decoding, what gets printed, and surviving a dropped connection."""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from conftest import FakeClient
from mcp_server.board import follow as follow_mod
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


def test_a_trailing_frame_without_its_blank_line_is_still_delivered():
    """A connection dropped between the payload and its delimiter still sent a post."""
    assert list(sse_events(['data: {"id": 4}'])) == [{"id": 4}]


def test_a_trailing_frame_cut_off_mid_payload_is_dropped():
    """Which is what a connection cut in the middle of a write actually leaves."""
    assert list(sse_events(['data: {"id": 4, "sum'])) == []


@pytest.mark.parametrize("payload", ["null", "[1, 2]", '"a string"', "7"])
def test_a_frame_that_is_not_an_object_is_skipped(payload):
    """Valid JSON is not enough: every consumer calls `.get` on what it gets."""
    assert list(sse_events([f"data: {payload}", "", 'data: {"id": 8}', ""])) == [{"id": 8}]


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


def status(code: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        str(code), request=httpx.Request("GET", "https://board.example/board"),
        response=httpx.Response(code),
    )


def cursor_file(home):
    written = list((home / ".local" / "state" / "quarterback").glob("board-cursor-*"))
    assert len(written) == 1
    return written[0]


class BoardWithHead(FakeClient):
    """A board whose newest post is far past anything a narrowed query returns.

    Which is the ordinary case rather than a contrived one: heartbeats are ~93%
    of the board and carry its highest ids, so the newest `finding` is routinely
    hundreds of ids behind the end.
    """

    def __init__(self, head: int, **kw) -> None:
        super().__init__(**kw)
        self.head = head
        # This board is new enough to report where it ends, which is what lets a
        # narrowed backlog anchor without a second request (#173).
        self.head_id = head

    def board(self, params):
        self.board_calls.append(dict(params))
        if params.get("limit") == 1:
            return [post(self.head, type="presence")]
        return list(self._board)


class FlakyBoard(FakeClient):
    """Raises `error` on the first `times` calls to /board, then behaves."""

    def __init__(self, error: BaseException, times: int = 1, **kw) -> None:
        super().__init__(**kw)
        self._error, self._times = error, times

    def board(self, params):
        if self._times > 0:
            self._times -= 1
            raise self._error
        return super().board(params)


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


def test_a_server_error_on_the_backlog_is_not_fatal_either(tmp_path):
    """A board restarting during startup must not kill a tail that would recover.

    The stream loop reconnects through a 5xx; the backlog fetch used to treat any
    HTTP status as fatal, so the same outage killed the process depending only on
    which of the two requests it landed on.
    """
    client = FlakyBoard(status(503), board=[], batches=[[post(4)]])
    code, out, err = run_follow(client, max_reconnects=0, home=str(tmp_path))
    assert code == 0 and "#4" in out and "503" in err


def test_a_rejected_token_on_the_backlog_is_still_fatal(tmp_path):
    client = FlakyBoard(status(401), board=[], batches=[[post(4)]])
    code, _out, err = run_follow(client, max_reconnects=0, home=str(tmp_path))
    assert code == 1 and "token" in err


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
    assert cursor_file(tmp_path).read_text().strip() == "12"


def test_presence_still_advances_the_cursor_even_though_it_is_not_printed(tmp_path):
    """Otherwise a quiet board of heartbeats replays them on every reconnect."""
    client = FakeClient(board=[], batches=[[post(5, type="presence")]])
    _code, out, _err = run_follow(client, tail=0, max_reconnects=1, home=str(tmp_path))
    assert out.strip() == ""
    assert client.stream_calls == [0, 5]


def test_a_board_that_is_down_at_startup_still_follows(tmp_path):
    """The backlog is a convenience; failing to fetch it must not stop the tail."""
    client = FlakyBoard(httpx.ConnectError("boom"), board=[], batches=[[post(1)]])
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


def test_a_presence_backlog_is_asked_for_by_type_alone(tmp_path):
    """`type=` is honoured verbatim by the board, heartbeats included.

    So `-t presence` needs no `include_presence` beside it — the pairing looks
    like an omission and has been filed as one, hence the test.
    """
    client = FakeClient(board=[post(1, type="presence")], batches=[[]])
    _code, out, _err = run_follow(client, types=["presence"], max_reconnects=0,
                                  home=str(tmp_path))
    assert client.board_calls[0] == {"limit": 20, "window_min": 0, "type": "presence"}
    assert "#1" in out


# -- `-n` means N lines ------------------------------------------------


def test_several_types_over_fetch_so_that_n_still_means_n_lines(tmp_path):
    """`/board` takes one type, so `-t ask -t finding` is matched here.

    Asking it for exactly `-n` posts fetched `-n` posts of every type, and on a
    board that is mostly heartbeats that left one or two lines where twenty were
    asked for.
    """
    heartbeats = [post(i, type="presence") for i in range(1, 20)]
    client = FakeClient(
        board=[*heartbeats, post(20, type="ask"), post(21, type="finding"),
               post(22, type="ask")],
        batches=[[]],
    )
    _code, out, _err = run_follow(client, types=["ask", "finding"], tail=2,
                                  max_reconnects=0, home=str(tmp_path))
    assert client.board_calls[0]["limit"] == 40  # 2 lines asked for, 40 posts looked at
    assert [ln.split()[1] for ln in out.strip().splitlines()] == ["#21", "#22"]


def test_the_over_fetch_stays_inside_the_servers_limit(tmp_path):
    """`/board` rejects a limit above 1000, and a rejected backlog is no backlog."""
    client = FakeClient(board=[], batches=[[]])
    run_follow(client, types=["ask", "finding"], tail=100, max_reconnects=0,
               home=str(tmp_path))
    assert client.board_calls[0]["limit"] == 1000


# -- anchoring the cursor ----------------------------------------------


def test_the_cursor_is_anchored_at_the_boards_head_not_at_what_matched(tmp_path):
    """`GET /stream?since=0` replays the entire board, hidden heartbeats included."""
    client = BoardWithHead(900, board=[post(3, type="finding")], batches=[[]])
    run_follow(client, types=["finding"], max_reconnects=0, home=str(tmp_path))
    assert client.stream_calls == [900]


def test_a_backlog_that_matched_nothing_still_anchors_the_cursor(tmp_path):
    """The emitted list is empty here, so a cursor read off it is 0 — the flood."""
    client = BoardWithHead(500, board=[], batches=[[]])
    run_follow(client, types=["finding"], max_reconnects=0, home=str(tmp_path))
    assert client.stream_calls == [500]


def test_no_backlog_at_all_still_anchors_the_cursor(tmp_path):
    """`-n 0` asks for no context, not for the board played back from the start."""
    client = BoardWithHead(700, board=[], batches=[[]])
    run_follow(client, tail=0, max_reconnects=0, home=str(tmp_path))
    assert client.stream_calls == [700]


def test_a_failed_backlog_anchors_with_a_request_of_its_own(tmp_path):
    client = FlakyBoard(status(503), board=[post(7)], batches=[[]])
    run_follow(client, max_reconnects=0, home=str(tmp_path))
    assert client.stream_calls == [7]


def test_the_stream_is_never_opened_from_zero_when_the_head_is_unknown(tmp_path):
    """Better to retry the one cheap request than to replay the whole board."""
    client = FlakyBoard(httpx.ConnectError("down"), times=99, board=[], batches=[[post(1)]])
    code, out, err = run_follow(client, max_reconnects=2, home=str(tmp_path))
    assert code == 0
    assert client.stream_calls == []
    assert out == ""
    assert "streaming from the beginning" in err


def test_since_zero_means_no_cursor_rather_than_the_start_of_the_board(tmp_path):
    """Post ids are 1-based, so a 0 handed in carries no information at all."""
    client = BoardWithHead(400, board=[], batches=[[]])
    run_follow(client, since=0, tail=0, max_reconnects=0, home=str(tmp_path))
    assert client.stream_calls == [400]


# -- fatal vs retryable ------------------------------------------------


@pytest.mark.parametrize("code", [400, 404, 422])
def test_a_client_error_on_the_stream_is_fatal_rather_than_retried_forever(tmp_path, code):
    """A bad argument or a wrong URL answers the same every time it is asked."""
    client = FakeClient(board=[], batches=[status(code)])
    rc, _out, err = run_follow(client, tail=0, max_reconnects=5, home=str(tmp_path))
    assert rc == 1
    assert str(code) in err and "retrying cannot" in err
    assert "token" not in err  # not every 4xx is a credential problem
    assert len(client.stream_calls) == 1


def test_a_client_error_on_the_backlog_is_fatal_too(tmp_path):
    client = FlakyBoard(status(422), board=[], batches=[[post(1)]])
    rc, _out, err = run_follow(client, max_reconnects=0, home=str(tmp_path))
    assert rc == 1 and "422" in err
    assert client.stream_calls == []


# -- interrupts and cursor writes --------------------------------------


def test_an_interrupt_during_the_backlog_fetch_is_a_clean_exit(tmp_path):
    """Ctrl-C in the first second of a tail is still Ctrl-C, not a traceback."""
    client = FlakyBoard(KeyboardInterrupt(), board=[], batches=[[post(1)]])
    code, out, _err = run_follow(client, max_reconnects=0, home=str(tmp_path))
    assert code == 0 and out == ""


def test_an_interrupt_while_printing_the_backlog_is_a_clean_exit(tmp_path):
    class Interrupted(io.StringIO):
        def write(self, _s):
            raise KeyboardInterrupt

    client = FakeClient(board=[post(1)], batches=[[]])
    code = follow(
        client, "https://board.example", out=Interrupted(), err=io.StringIO(),
        colour=False, sleep=lambda _s: None, max_reconnects=0,
        env={"HOME": str(tmp_path)},
    )
    assert code == 0


def test_the_cursor_file_is_not_rewritten_for_every_post(tmp_path, monkeypatch):
    """Most posts are heartbeats nobody prints; each was a mkdir, write and rename."""
    writes: list[int] = []
    real = follow_mod.write_cursor

    def counting(base_url, cursor, env=None):
        writes.append(cursor)
        real(base_url, cursor, env)

    monkeypatch.setattr(follow_mod, "write_cursor", counting)
    client = FakeClient(board=[], batches=[[post(i) for i in range(1, 51)]])
    run_follow(client, tail=0, max_reconnects=0, home=str(tmp_path), clock=lambda: 1000.0)
    # The first post, then the flush on the way out — not fifty of them, and not
    # at the cost of the value that has to survive.
    assert writes == [1, 50]
    assert cursor_file(tmp_path).read_text().strip() == "50"


def test_the_backlog_is_recorded_before_the_stream_is_even_opened(tmp_path, monkeypatch):
    """A tail Ctrl-C'd on a quiet board recorded nothing, so --resume reprinted it."""
    client = FakeClient(board=[post(41), post(42)], batches=[[]])
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(
        follow_mod, "write_cursor",
        lambda _url, cursor, env=None: seen.append((len(client.stream_calls), cursor)),
    )
    run_follow(client, presence=True, max_reconnects=0, home=str(tmp_path))
    assert seen[0] == (0, 42)


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs /proc to count fds")
def test_quieting_a_broken_pipe_leaks_no_descriptor():
    """It runs once per process, but a leaked fd in a long tail is still a leak.

    In a subprocess because the function redirects the real stdout, which under
    pytest is the capture buffer.
    """
    package = str(Path(__file__).resolve().parents[1])
    program = (
        "import os, sys;"
        f"sys.path.insert(0, {package!r});"
        "from mcp_server.board.follow import quiet_broken_pipe;"
        "before = os.listdir('/proc/self/fd');"
        "quiet_broken_pipe();"
        "sys.exit(len(os.listdir('/proc/self/fd')) - len(before))"
    )
    assert subprocess.run([sys.executable, "-c", program], check=False).returncode == 0


# ------------------------------------- 408/429 are "ask again", not "you asked wrong"

@pytest.mark.parametrize("code", [408, 429])
def test_a_transient_4xx_does_not_kill_the_tail(tmp_path, code):
    """Round 1 stopped an endless retry loop by making every 4xx fatal, and swept
    in the two that explicitly mean "try again": 408 is a request timeout, 429 is
    rate limiting. A `--follow` left running for hours is the client most likely
    to meet both — it holds a connection open and it is the one that gets
    throttled — so treating them as "this process asked for the wrong thing"
    killed the tail for the one reason it should have survived."""
    client = FlakyBoard(status(code), board=[post(7)], batches=[[]])
    code_out, _out, err = run_follow(client, max_reconnects=0, home=str(tmp_path))
    assert code_out == 0, "a retryable status must not be a fatal exit"
    assert client.stream_calls == [7], "the tail should have carried on and anchored"
    assert "unavailable" in err or err == "" or "qb board" in err


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_a_real_client_error_is_still_fatal(tmp_path, code):
    """The endless loop round 1 fixed has to stay fixed: these come back the same
    however many times they are asked, and retrying one is a spin where the user
    wanted a message."""
    client = FlakyBoard(status(code), board=[post(7)], batches=[[]])
    code_out, _out, _err = run_follow(client, max_reconnects=0, home=str(tmp_path))
    assert code_out != 0, f"HTTP {code} should be fatal"
    assert client.stream_calls == []


# --------------------------------- one request answers both halves (#173)

def test_a_narrowed_backlog_anchors_without_a_second_request(tmp_path):
    """The race in #173 was a race between two reads, so the fix is that there is
    one. A filtered body cannot say where the board ends — the ids in between
    belong to posts the filter dropped — and `X-Board-Head` says it anyway, on
    the response already being fetched.

    Asserting the CALL COUNT rather than the cursor is the point: the old code
    reached the same cursor, one request later, with a window in between where a
    post could land in neither the backlog nor the stream and be lost silently.
    """
    client = BoardWithHead(900, board=[post(3, type="finding")], batches=[[]])
    run_follow(client, types=["finding"], max_reconnects=0, home=str(tmp_path))
    assert client.stream_calls == [900]
    assert len(client.board_calls) == 1, (
        f"anchored in {len(client.board_calls)} requests; the second one is the race"
    )


def test_an_old_board_without_the_header_still_works_the_old_way(tmp_path):
    """The fleet deploys by pushing to `main`, so a client talking to a board that
    predates the header is ordinary rather than exotic. Falling back to the two
    reads keeps the tail working — it does not close the race, which no client can
    do alone, and pretending otherwise would be worse than the race."""
    client = FakeClient(board=[post(3, type="finding")], batches=[[]])  # head_id None
    run_follow(client, types=["finding"], max_reconnects=0, home=str(tmp_path))
    assert len(client.board_calls) == 2, "an old board still needs the head request"


def test_the_header_is_preferred_over_the_body_even_for_the_head_request(tmp_path):
    """`_head` asks for one row and the newest row can be muted, so a body-derived
    answer can sit behind the board's real end. The header cannot."""
    client = BoardWithHead(900, board=[], batches=[[]])
    run_follow(client, tail=0, max_reconnects=0, home=str(tmp_path))
    assert client.stream_calls == [900]
